"""Sandboxed execution for search() and execute() tools."""

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .docs import get_cached_mojo_version

MAX_OUTPUT = 8192  # 8KB cap to avoid flooding context
SEARCH_TIMEOUT_S = 5.0  # wall-clock budget for agent-supplied search snippets

# Version-keyed feature-detect cache for --diagnostic-format json.
# Key: (version_string, binary_mtime). Value: bool. Re-detect when the key
# changes (a toolchain upgrade mid-session is not a server restart).
_JSON_DIAG_CACHE: dict[tuple[str, float], bool] = {}

# Per-session in-memory state (stdio server persists per session; reset on restart).
_SESSION_LEDGER: dict = {}
_LEDGER_MAX_KEYS = 2000
_BUILD_ORDINAL = 0


def _next_build_ordinal() -> int:
    """Return a monotonically increasing build ordinal for the session."""
    global _BUILD_ORDINAL
    _BUILD_ORDINAL += 1
    return _BUILD_ORDINAL


def _group_to_dict(g) -> dict:
    """Serialize a diagnostics.Group to the response shape (incl. notes)."""
    return {
        "message": g.message,
        "file": g.file,
        "lines": list(g.lines),
        "count": g.count,
        "origin": g.origin,
        "fixits": list(g.fixits),
        "notes": [_group_to_dict(n) for n in g.notes],
    }


def _suppressed_to_dict(g) -> dict:
    """Serialize a suppressed Group to its one-liner response shape."""
    return {
        "message": g.message,
        "file": g.file,
        "lines": list(g.lines),
        "count": g.count,
    }


def _commit_ledger(records: dict, suppress_keys) -> None:
    """Apply the core's ledger records with LRU eviction (<= _LEDGER_MAX_KEYS)."""
    for key, entry in records.items():
        _SESSION_LEDGER.pop(key, None)   # move-to-end semantics
        _SESSION_LEDGER[key] = entry
    while len(_SESSION_LEDGER) > _LEDGER_MAX_KEYS:
        oldest = next(iter(_SESSION_LEDGER))
        _SESSION_LEDGER.pop(oldest, None)


def _build_project_roots(
    *, wrapper: str | None, source_path: str | None, cwd: str | None
) -> frozenset:
    """Build the realpath-normalized project-root set for origin classification.

    Always includes (when present): the generated wrapper file, the caller
    source path, cwd, and — when source_path resolves outside cwd — the parent
    directory of the source file (so user sibling modules are project-origin).
    Symlinks err toward project-origin by adding BOTH the logical and realpath
    forms of each root.
    """
    roots: set = set()

    def add(p: str) -> None:
        roots.add(str(Path(p)))
        try:
            roots.add(str(Path(p).resolve()))
        except OSError:
            pass

    if wrapper:
        add(wrapper)
    if cwd:
        add(cwd)
    if source_path:
        add(source_path)
        try:
            src_rp = Path(source_path).resolve()
            cwd_rp = Path(cwd).resolve() if cwd else None
            inside = False
            if cwd_rp is not None:
                try:
                    src_rp.relative_to(cwd_rp)
                    inside = True
                except ValueError:
                    inside = False
            if not inside:
                add(str(src_rp.parent))
        except OSError:
            pass
    return frozenset(roots)


def _version_key(mojo_prefix: list[str]) -> tuple[str, float]:
    """Resolve a (version_string, binary_mtime) key for feature-detect caching."""
    version = "unknown"
    mtime = 0.0
    try:
        proc = subprocess.run([*mojo_prefix, "--version"],
                              capture_output=True, text=True, timeout=10)
        version = (proc.stdout or proc.stderr or "unknown").strip().splitlines()[0]
    except Exception:
        pass
    binary = shutil.which(mojo_prefix[0]) if mojo_prefix else None
    if binary:
        try:
            mtime = Path(binary).stat().st_mtime
        except OSError:
            pass
    return (version, mtime)


def _probe_json_diagnostics(mojo_prefix: list[str]) -> bool:
    """Probe whether the compiler accepts --diagnostic-format json.

    Compiles a tiny empty module; treats an 'invalid/unknown option' style
    rejection of the flag as unsupported. Best-effort; defaults to False on any
    spawn failure (text path is always safe).
    """
    probe = tempfile.mkdtemp(prefix="mojo-mcp-probe-")
    probe_file = f"{probe}/p.mojo"
    try:
        with open(probe_file, "w") as f:
            f.write("def main():\n    pass\n")
        # Build into the probe temp dir (explicit -o + cwd) so the output binary
        # never lands in the caller's working directory.
        proc = subprocess.run(
            [*mojo_prefix, "build", "--diagnostic-format", "json",
             "-o", f"{probe}/probe.bin", probe_file],
            capture_output=True, text=True, timeout=60, cwd=probe)
        blob = (proc.stderr or "") + (proc.stdout or "")
        if re.search(r"(unknown|unrecognized|invalid).*(option|argument|diagnostic-format)",
                     blob, re.IGNORECASE):
            return False
        return True
    except Exception:
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def _supports_json_diagnostics(mojo_prefix: list[str]) -> bool:
    """Return cached feature-detect result, re-probing when the version key changes."""
    key = _version_key(mojo_prefix)
    if key not in _JSON_DIAG_CACHE:
        _JSON_DIAG_CACHE[key] = _probe_json_diagnostics(mojo_prefix)
    return _JSON_DIAG_CACHE[key]


def _json(obj: Any) -> str:
    """Compact JSON serialization for tool responses.

    Uses default=str to handle Path objects and other non-JSON types.
    This is intentional for a response serializer but may mask bugs
    where unexpected types leak into the output dict.
    """
    return json.dumps(obj, separators=(",", ":"), default=str)


def _extract_error_summary(stderr: str) -> str | None:
    """Extract the first error line from Mojo compiler output.

    Mojo errors typically appear as:
      - 'error: <message>'
      - '/path/to/file.mojo:3:5: error: <message>'
    Falls back to the first warning line if no error is found.
    """
    for line in stderr.splitlines():
        stripped = line.strip()
        if re.search(r"\berror:", stripped, re.IGNORECASE):
            return stripped
    for line in stderr.splitlines():
        stripped = line.strip()
        if re.search(r"\bwarning:", stripped, re.IGNORECASE):
            return stripped
    return None


# ---------------------------------------------------------------------------
# Mojo version resolution helpers
# ---------------------------------------------------------------------------

def _find_mojo_version_file(cwd: str | None) -> tuple[Path | None, str | None]:
    """Walk up from cwd looking for a .mojo-version file.

    Returns (file_path, version_string) or (None, None) if not found.
    """
    if cwd is None:
        return None, None
    start = Path(cwd).resolve()
    for directory in [start, *start.parents]:
        candidate = directory / ".mojo-version"
        if candidate.is_file():
            version = candidate.read_text().strip()
            if version:
                return candidate, version
    return None, None


_LEGACY_CALVER_MAJOR_RE = re.compile(r"^(\d+)\.")
_PRERELEASE_SUFFIX_RE = re.compile(r"[abc]\d+|rc\d+", re.IGNORECASE)


def _normalize_mojo_compiler_version(version: str) -> str:
    """Normalize a .mojo-version string to a PyPI `mojo-compiler` version.

    Two schemes coexist on PyPI:
      - Legacy calver (Mojo 24.x/25.x/26.x): published as `0.<major>.<minor>.<patch>`.
        `.mojo-version` files write the un-prefixed form (e.g. `25.6.0`), so we
        prepend `0.` here.
      - Modern semver (Mojo 1.0+): published verbatim (e.g. `1.0.0b1`). No prefix.

    Already-prefixed `0.X.Y.Z` strings are left alone.
    """
    if version.startswith("0."):
        return version
    m = _LEGACY_CALVER_MAJOR_RE.match(version)
    if m and int(m.group(1)) >= 24:
        return f"0.{version}"
    return version


def _is_prerelease(version: str) -> bool:
    """Whether a normalized version string carries a pre-release suffix (b1, a2, rc1)."""
    return bool(_PRERELEASE_SUFFIX_RE.search(version))


def _select_mojo(version: str | None, cwd: str | None = None) -> tuple[list[str], str]:
    """Resolve the Mojo compiler for a project, preferring its own toolchain.

    Priority (most faithful first):
      1. ``<cwd>/.venv/bin/mojox`` — the project's package-aware frontend, exactly
         what ``uv run mojox`` / ``run_tests.sh`` invoke.
      2. ``<cwd>/.venv/bin/mojo`` — the project's raw compiler binary.
      3. ``uvx --from mojo-compiler==<version> mojo`` — a version-pinned install,
         used only when the project ships no usable venv toolchain. Versions are
         normalized via `_normalize_mojo_compiler_version` to match the two PyPI
         schemes (legacy `0.`-prefixed calver vs modern semver); pre-release pins
         add ``--prerelease=allow`` so uv considers pre-release transitive deps.
      4. ``mojo`` — the system binary, last resort.

    Returns ``(command_prefix, source_label)`` where ``source_label`` is one of
    ``project-venv-mojox`` / ``project-venv-mojo`` / ``uvx-pin`` / ``system``.

    Preferring the project's own venv (steps 1–2) over the pin (step 3) makes the
    MCP a faithful oracle: it runs the identical binary the project's own tests
    run, rather than a same-version-string duplicate that can silently diverge.
    """
    if cwd:
        bin_dir = Path(cwd).resolve() / ".venv" / "bin"
        mojox_bin = bin_dir / "mojox"
        if mojox_bin.is_file():
            return [str(mojox_bin)], "project-venv-mojox"
        mojo_bin = bin_dir / "mojo"
        if mojo_bin.is_file():
            return [str(mojo_bin)], "project-venv-mojo"

    if version:
        normalized = _normalize_mojo_compiler_version(version)
        cmd = ["uvx", "--from", f"mojo-compiler=={normalized}"]
        if _is_prerelease(normalized):
            cmd += ["--prerelease=allow"]
        cmd += ["mojo"]
        return cmd, "uvx-pin"

    return ["mojo"], "system"


def _mojo_cmd(version: str | None, cwd: str | None = None) -> list[str]:
    """Return just the resolved compiler command prefix (see `_select_mojo`)."""
    return _select_mojo(version, cwd)[0]


_VERSION_TOKEN_RE = re.compile(r"\d+\.\d+[0-9A-Za-z.\-]*")


def _version_token(value: str | None) -> str | None:
    """Extract the first version-like token from a `--version` line (or None)."""
    if not value:
        return None
    match = _VERSION_TOKEN_RE.search(value)
    return match.group(0) if match else value.strip()


def _effective_mojo_version(
    cmd: list[str], source: str, pinned_version: str | None
) -> str | None:
    """Best-effort version of the compiler `execute` would actually run.

    For ``uvx-pin`` the effective version *is* the pin (uvx installs exactly that),
    so it is returned without a network round-trip. For a project venv or the
    system binary, ``<cmd> --version`` is invoked and a version token parsed out.
    Returns None when the binary cannot be run.
    """
    if source == "uvx-pin":
        return _normalize_mojo_compiler_version(pinned_version) if pinned_version else None
    try:
        proc = subprocess.run(
            [*cmd, "--version"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    text = proc.stdout.strip() or proc.stderr.strip()
    return _version_token(text) if text else None


def _versions_disagree(pinned: str | None, effective: str | None) -> bool:
    """Loose inequality tolerant of ``25.6`` vs ``25.6.0`` and ``0.``-prefixing."""
    a, b = _version_token(pinned), _version_token(effective)
    if not a or not b:
        return False
    a, b = a.lstrip("v"), b.lstrip("v")
    for x, y in ((a, b), (_normalize_mojo_compiler_version(a),
                          _normalize_mojo_compiler_version(b))):
        if x == y or x.startswith(y) or y.startswith(x):
            return False
    return True


def _find_mojo_packages(cwd: str | None) -> Path | None:
    """Find the mojo_packages directory in the project's venv.

    Returns the path if it exists and contains .mojopkg files, else None.
    """
    if not cwd:
        return None
    import glob
    venv = Path(cwd).resolve() / ".venv"
    if not venv.is_dir():
        return None
    # Search for mojo_packages in the venv's site-packages
    pattern = str(venv / "lib" / "python*" / "site-packages" / "mojo_packages")
    matches = glob.glob(pattern)
    for match in matches:
        pkg_dir = Path(match)
        if pkg_dir.is_dir() and any(pkg_dir.glob("*.mojopkg")):
            return pkg_dir
    return None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def run_search(code: str, docs: dict) -> str:
    """Execute agent-written Python code against the docs dict.

    The agent's code runs in a restricted exec() with no imports or I/O.
    It receives `docs` as its only variable and must return a value.
    Returns a wrapped result: {"result": ..., "hint": ...}.
    """

    def _exec() -> Any:
        restricted_builtins = {
            "len": len,
            "list": list,
            "dict": dict,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "set": set,
            "tuple": tuple,
            "sorted": sorted,
            "filter": filter,
            "map": map,
            "zip": zip,
            "enumerate": enumerate,
            "range": range,
            "sum": sum,
            "max": max,
            "min": min,
            "any": any,
            "all": all,
            "isinstance": isinstance,
            "type": type,
            "repr": repr,
        }
        global_ns: dict = {"__builtins__": restricted_builtins}
        local_ns: dict = {"docs": docs}

        # Wrap multi-line code in a function so the agent can use return
        indented = "\n".join(f"    {line}" for line in code.splitlines())
        wrapped = f"def _search(docs):\n{indented}\n_result = _search(docs)"

        exec(wrapped, global_ns, local_ns)  # noqa: S102
        return local_ns.get("_result")

    _box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            _box["value"] = _exec()
        except Exception as e:  # noqa: BLE001
            _box["error"] = e

    # Run the agent snippet in a DAEMON thread. A non-terminating snippet
    # (e.g. ``while True: pass``) cannot be killed once started; as a daemon it
    # is abandoned at interpreter exit instead of deadlocking atexit's
    # thread-join (CPython concurrent.futures.thread._python_exit) — which is
    # what a plain ThreadPoolExecutor worker did, hanging the whole process.
    t = threading.Thread(target=_runner, name="mojo-mcp-search", daemon=True)
    t.start()
    t.join(timeout=SEARCH_TIMEOUT_S)
    if t.is_alive():
        return _json({"error": f"search timed out after {SEARCH_TIMEOUT_S:g} seconds"})
    if "error" in _box:
        return _json({"error": str(_box["error"])})
    result_data = _box.get("value")

    if result_data is None:
        out = {
            "result": None,
            "message": "Search returned no results.",
            "hint": "Try broader terms, or use lookup('module.Symbol') if you know the name.",
        }
    else:
        raw = json.dumps(result_data, separators=(",", ":"), default=str)
        truncated = len(raw) > MAX_OUTPUT
        if truncated:
            out = {
                "result_raw": raw[:MAX_OUTPUT],
                "truncated": True,
                "total_bytes": len(raw),
                "hint": "Result was truncated. Narrow your query to reduce output.",
            }
        else:
            out = {
                "result": result_data,
                "hint": "Use lookup('<package>.<module>.<Symbol>') for full docs on any symbol.",
            }
    mv = get_cached_mojo_version()
    if mv:
        out["mojo_version"] = mv
    return _json(out)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

READ_FILE_MAX_BYTES = 100_000


def run_read_file(path: str) -> str:
    """Read a file; return JSON {path, content} with optional truncation metadata and hints."""
    _BLOCKED = {Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev")}
    try:
        p = Path(path).resolve()
        for blocked in _BLOCKED:
            try:
                p.relative_to(blocked)
                return _json({"error": f"Access denied: {blocked}"})
            except ValueError:
                pass
        if not p.is_file():
            return _json({"error": f"Not a file: {path}"})
        raw = p.read_bytes()
        content = raw[:READ_FILE_MAX_BYTES].decode("utf-8", errors="replace")
        truncated = len(raw) > READ_FILE_MAX_BYTES
        output: dict[str, Any] = {"path": str(p), "content": content}
        if truncated:
            output["truncated"] = True
            output["total_bytes"] = len(raw)
            hint_parts = [f"File is {len(raw) // 1024}KB (truncated at {READ_FILE_MAX_BYTES // 1024}KB)."]
            hint_parts.append("Use the host's file reading tool with offset to see the rest.")
            if str(p).endswith(".mojo"):
                hint_parts.append(f"Use validate(path='{p}') to check for known issues.")
            output["hint"] = " ".join(hint_parts)
        elif str(p).endswith(".mojo"):
            output["hint"] = f"Use validate(path='{p}') to check this file for known issues."
        return _json(output)
    except Exception as e:
        return _json({"error": str(e)})


LIST_FILES_MAX_ENTRIES = 200


def run_list_files(path: str = ".", pattern: str = "**/*.mojo") -> str:
    """List files matching glob; return JSON with count, hints, and empty-state message."""
    import itertools

    try:
        base = Path(path).resolve()
        if not base.is_dir():
            return _json({"error": f"Not a directory: {path}"})
        gen = base.glob(pattern)
        entries = sorted(str(f) for f in itertools.islice(gen, LIST_FILES_MAX_ENTRIES + 1))
        truncated = len(entries) > LIST_FILES_MAX_ENTRIES
        files = entries[:LIST_FILES_MAX_ENTRIES]
        output: dict[str, Any] = {
            "path": str(base),
            "pattern": pattern,
            "files": files,
            "count": len(files),
        }
        if truncated:
            output["truncated"] = True
            output["hint"] = "Showing first 200 files. Narrow the pattern to see specific files."
        elif not files:
            output["message"] = f"0 files matching {pattern} in {base}"
            output["hint"] = "Try a different pattern or directory."
        else:
            output["hint"] = "Use read_file('<path>') to inspect a file, or validate(path='<path>') to check it."
        return _json(output)
    except Exception as e:
        return _json({"error": str(e)})


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

def run_execute(
    code: str,
    cwd: str | None = None,
    include_paths: list[str] | None = None,
    defines: dict[str, str] | None = None,
    timeout: int = 30,
    raw: bool = False,
) -> str:
    """Execute Mojo code in an isolated temp directory.

    The temp file lives in mkdtemp but the subprocess runs from `cwd` (when
    provided) so that relative include paths like ``-I .`` resolve against the
    user's project root rather than the temp directory.

    Args:
        code:          Complete Mojo source (must contain ``fn main()``).
        cwd:           Project directory.  Used to locate ``.mojo-version`` and
                       as the working directory for the Mojo process.
        include_paths: Extra ``-I <path>`` flags appended before the source
                       file.  Paths are interpreted relative to ``cwd``.
        defines:       ``-D KEY=VALUE`` (or ``-D KEY`` when value is empty/None)
                       compile-time defines.  E.g. ``{"ASSERT": "all"}``.
        timeout:       Process timeout in seconds (default 30).
        raw:           When True, skip ledger writes and return ungrouped
                       diagnostics (one Group per diagnostic, no cross-rebuild
                       dedup). Useful for escape-hatch inspection.
    """
    version_file, pinned_version = _find_mojo_version_file(cwd)
    mojo_prefix, mojo_source = _select_mojo(pinned_version, cwd)

    # Auto-inject installed Mojo packages from the project's venv.
    # mojox does this automatically, but for version-pinned projects
    # (which use bare mojo via uvx), we need to do it manually.
    mojo_packages = _find_mojo_packages(cwd)

    # Build -I / -D flags
    extra_flags: list[str] = []
    if mojo_packages:
        extra_flags.extend(["-I", str(mojo_packages)])
    for path in (include_paths or []):
        extra_flags.extend(["-I", path])
    for key, val in (defines or {}).items():
        if val:
            extra_flags.extend(["-D", f"{key}={val}"])
        else:
            extra_flags.extend(["-D", key])

    # Subprocess working directory: user's project root when given, otherwise
    # the temp dir (keeps backward-compatibility for standalone snippets).
    run_cwd = str(Path(cwd).resolve()) if cwd else None

    output: dict[str, Any]
    tmp_dir = tempfile.mkdtemp(prefix="mojo-mcp-")
    tmp_file = f"{tmp_dir}/main.mojo"
    try:
        with open(tmp_file, "w") as f:
            f.write(code)

        # Build subprocess environment with library paths
        import os
        run_env = os.environ.copy()
        if mojo_packages:
            lib_dir = mojo_packages / "lib"
            if lib_dir.is_dir():
                existing = run_env.get("LD_LIBRARY_PATH", "")
                run_env["LD_LIBRARY_PATH"] = f"{lib_dir}:{existing}" if existing else str(lib_dir)

        use_json = _supports_json_diagnostics(mojo_prefix)
        diag_flags = ["--diagnostic-format", "json"] if use_json else []
        cmd = [*mojo_prefix, "run", *diag_flags, *extra_flags, tmp_file]
        t0 = time.monotonic()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=run_cwd or tmp_dir,
            env=run_env,
        )
        duration = round(time.monotonic() - t0, 1)
        from .diagnostics import compact_diagnostics
        project_roots = _build_project_roots(
            wrapper=tmp_file, source_path=None, cwd=run_cwd)
        build_ordinal = _next_build_ordinal()
        # Resolve diagnostic file paths the same way roots are (realpath) so the
        # pure core's containment test is consistent. The compiler already emits
        # absolute paths for the wrapper; relative dep paths stay as-is.
        comp = compact_diagnostics(
            result.stderr if use_json else "",
            project_roots=project_roots,
            returncode=result.returncode,
            raw_stderr=result.stderr,
            ledger=dict(_SESSION_LEDGER),
            build_ordinal=build_ordinal,
            raw=raw,
        )
        if not raw:
            _commit_ledger(comp.new_ledger_records, comp.suppress_keys)
        output = {
            "stdout": result.stdout[:MAX_OUTPUT],
            "returncode": result.returncode,
            "duration_s": duration,
            "diagnostics": {
                "errors": [_group_to_dict(g) for g in comp.errors],
                "warnings": [_group_to_dict(g) for g in comp.warnings],
                "suppressed": [_suppressed_to_dict(g) for g in comp.suppressed],
                "parse_fallback": comp.parse_fallback,
                "truncated_warning_groups": comp.truncated_warning_groups,
            },
            "diagnostics_md": comp.rendered,
        }
        if pinned_version:
            output["mojo_version"] = pinned_version
            output["version_file"] = str(version_file)
        if result.returncode != 0:
            output["hint"] = "Use validate(code=...) to check for known gotcha patterns."
            from .gotchas import enrich_error
            version_for_enrich = ".".join((pinned_version or "0.26.0").split(".")[:3])
            hints = enrich_error(result.stderr, timed_out=False, mojo_version=version_for_enrich)
            if hints:
                output["gotcha_hints"] = hints
    except subprocess.TimeoutExpired:
        from .gotchas import enrich_error
        version_for_enrich = pinned_version or "0.26.0"
        parts = version_for_enrich.split(".")
        version_for_enrich = ".".join(parts[:3])
        hints = enrich_error(code, timed_out=True, mojo_version=version_for_enrich)
        output = {
            "error": f"execution timed out after {timeout} seconds",
            "hint": "Use validate(code=...) to check for known gotcha patterns.",
        }
        if hints:
            output["gotcha_hints"] = hints
    except FileNotFoundError:
        if pinned_version:
            output = {
                "error": (
                    f"Could not run mojo-compiler=={pinned_version} via uvx. "
                    "Ensure uv is installed: https://docs.astral.sh/uv/getting-started/installation/"
                )
            }
        else:
            output = {
                "error": (
                    "mojo binary not found. "
                    "Install it with: uv add mojox  "
                    "Or: uv tool install mojo  "
                    "Or call the install_mojo tool."
                )
            }
    except Exception as e:
        output = {"error": str(e)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Always surface which toolchain actually ran, so the caller can tell a
    # project-venv run (faithful) from a uvx-pin or system fallback at a glance.
    output.setdefault("mojo_source", mojo_source)
    return _json(output)


# ---------------------------------------------------------------------------
# Version management
# ---------------------------------------------------------------------------

def run_mojo_version(path: str | None = None) -> str:
    """Report the Mojo compiler `execute` will actually run for `path`.

    Returns JSON with:
      - global_version: `mojo --version` of the system binary. This is only the
        system-wide fallback and is NOT necessarily what runs for a given project.
      - pinned_version / version_file: nearest `.mojo-version` pin, if any.
      - effective_source: which toolchain `execute` selects for `path`
        (`project-venv-mojox` / `project-venv-mojo` / `uvx-pin` / `system`).
      - effective_command: the resolved command prefix (space-joined).
      - effective_version: the version that command reports / installs.
      - warning: present when the project venv's version diverges from the pin.
    """
    result: dict[str, Any] = {}

    # Global (system-wide) binary — reported for context, not as "what runs here".
    mojo_path = shutil.which("mojo")
    if mojo_path:
        try:
            proc = subprocess.run(
                ["mojo", "--version"], capture_output=True, text=True, timeout=5
            )
            result["global_version"] = proc.stdout.strip() or proc.stderr.strip()
        except Exception as e:
            result["global_version_error"] = str(e)
    else:
        result["global_version"] = None
        result["hint"] = "Use install_mojo() to install Mojo."

    # Project-pinned version (declared) and the effective compiler (what runs).
    version_file, pinned_version = _find_mojo_version_file(path)
    result["pinned_version"] = pinned_version
    result["version_file"] = str(version_file) if version_file else None

    cmd, source = _select_mojo(pinned_version, path)
    result["effective_source"] = source
    result["effective_command"] = " ".join(cmd)
    # Skip the system-binary shellout when no mojo is installed — keeps the
    # no-mojo path fast and side-effect free.
    if source == "system" and not mojo_path:
        result["effective_version"] = None
    else:
        result["effective_version"] = _effective_mojo_version(cmd, source, pinned_version)

    if source in ("project-venv-mojox", "project-venv-mojo") and _versions_disagree(
        pinned_version, result["effective_version"]
    ):
        result["warning"] = (
            f"Project venv Mojo ({result['effective_version']}) differs from the "
            f"pinned version ({pinned_version}). `execute` runs the venv binary — "
            f"the same one `uv run` uses — so it may accept syntax the pin would "
            f"reject. Run `uv sync` in the project to realign the venv with "
            f".mojo-version."
        )

    result.setdefault(
        "hint",
        "`effective_*` is the compiler `execute` runs for this path; "
        "`global_version` is only the system-wide binary.",
    )

    return _json(result)


_GITHUB_URL = "git+https://github.com/Conobi/mojo-mcp"


def run_update_server() -> str:
    """Re-fetch the latest mojo-mcp from GitHub into the uvx cache.

    Uses `uvx --refresh` to pull the latest commit. The running process is
    not replaced — the user must restart Claude Code to load the new version.
    """
    uv_path = shutil.which("uv") or shutil.which("uvx")
    if not uv_path:
        return _json({
            "error": "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
        })

    try:
        proc = subprocess.run(
            ["uvx", "--refresh", "--from", _GITHUB_URL, "mojo-mcp", "--version"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            return _json({
                "status": "updated",
                "version": proc.stdout.strip() or proc.stderr.strip(),
                "hint": "Restart the host application to load the new version.",
            })
        return _json({
            "error": "update failed",
            "stdout": proc.stdout[:MAX_OUTPUT],
            "stderr": proc.stderr[:MAX_OUTPUT],
            "returncode": proc.returncode,
        })
    except subprocess.TimeoutExpired:
        return _json({"error": "update timed out after 120 seconds"})
    except Exception as e:
        return _json({"error": str(e)})


def run_install_mojo(version: str | None = None, project_path: str | None = None) -> str:
    """Install or upgrade Mojo, optionally pinning a project to a specific version.

    Behaviours:
      - project_path + version  → write .mojo-version, warm uvx cache for that version
      - project_path only       → remove .mojo-version (revert to global)
      - version only            → uv tool install modular==<version> globally
      - neither                 → uv tool install modular (latest) globally
    """
    uv_path = shutil.which("uv")
    if not uv_path:
        return _json({
            "error": "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
        })

    # Project-level pin
    if project_path is not None:
        proj = Path(project_path).resolve()
        if not proj.is_dir():
            return _json({"error": f"Not a directory: {project_path}"})
        version_file = proj / ".mojo-version"

        if version is None:
            # Remove pin — revert to global
            if version_file.exists():
                version_file.unlink()
                return _json({
                    "status": "unpinned",
                    "removed": str(version_file),
                    "hint": "Use mojo_version() to check the active version.",
                })
            return _json({
                "status": "no_pin_found",
                "path": str(proj),
                "hint": "Use install_mojo(version=..., project_path=...) to pin a version.",
            })

        # Idempotent: already pinned to the same version
        if version_file.exists() and version_file.read_text().strip() == version:
            return _json({
                "status": "already_pinned",
                "version": version,
                "version_file": str(version_file),
                "hint": "Use execute(code=...) to test with this version.",
            })

        # Write pin
        version_file.write_text(version + "\n")

        # Warm the uv cache so first execution is fast
        try:
            proc = subprocess.run(
                _mojo_cmd(version) + ["--version"],
                capture_output=True, text=True, timeout=120,
            )
            mojo_ver = proc.stdout.strip() or proc.stderr.strip()
            return _json({
                "status": "pinned",
                "version_file": str(version_file),
                "pinned_version": version,
                "mojo_version_output": mojo_ver,
                "hint": "Use mojo_version() to verify, then execute(code=...) to test.",
            })
        except subprocess.TimeoutExpired:
            return _json({
                "status": "pinned",
                "version_file": str(version_file),
                "pinned_version": version,
                "warning": "cache warm-up timed out; uv will download on first use",
                "hint": "Use mojo_version() to verify, then execute(code=...) to test.",
            })
        except Exception as e:
            return _json({
                "status": "pinned",
                "version_file": str(version_file),
                "pinned_version": version,
                "warning": str(e),
                "hint": "Use mojo_version() to verify, then execute(code=...) to test.",
            })

    # Global install / upgrade
    mojo_path = shutil.which("mojo")
    pkg = f"modular=={version}" if version else "modular"

    if mojo_path and version is None:
        # Already installed, no version requested → upgrade
        cmd = ["uv", "tool", "upgrade", "modular"]
        action = "upgraded"
    else:
        cmd = ["uv", "tool", "install", pkg]
        action = "installed"

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            return _json({
                "status": action,
                "path": shutil.which("mojo"),
                "stdout": proc.stdout[:MAX_OUTPUT],
                "stderr": proc.stderr[:MAX_OUTPUT],
                "hint": "Use mojo_version() to verify, then execute(code=...) to test.",
            })
        return _json({
            "error": f"{action} failed",
            "stdout": proc.stdout[:MAX_OUTPUT],
            "stderr": proc.stderr[:MAX_OUTPUT],
            "returncode": proc.returncode,
        })
    except subprocess.TimeoutExpired:
        return _json({"error": "operation timed out after 120 seconds"})
    except Exception as e:
        return _json({"error": str(e)})


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

def _detect_mojo_version() -> str:
    """Auto-detect installed Mojo version, falling back to '0.26.0'."""
    try:
        proc = subprocess.run(
            ["mojo", "--version"], capture_output=True, text=True, timeout=5
        )
        version_str = proc.stdout.strip().split()[1] if proc.stdout.strip() else "0.26.0"
        parts = version_str.split(".")
        return ".".join(parts[:3])
    except Exception:
        return "0.26.0"


def _validate_single_file(
    file_path: Path,
    mojo_version: str,
    category: str | None,
) -> dict[str, Any]:
    """Validate one .mojo file and return its result dict."""
    from .gotchas import validate_code

    code = file_path.read_text(encoding="utf-8", errors="replace")
    issues = validate_code(code, mojo_version, category=category, path=str(file_path))
    result: dict[str, Any] = {"path": str(file_path), "issues": issues, "count": len(issues)}
    if category:
        result["category"] = category
    return result


def run_validate(
    code: str | None = None,
    path: str | None = None,
    mojo_version: str | None = None,
    category: str | None = None,
) -> str:
    """Validate Mojo source code against known gotcha patterns.

    Args:
        code:         Mojo source code string. Takes precedence over path.
        path:         Path to a .mojo file or directory to validate.
                      Directories are scanned recursively for .mojo files.
        mojo_version: Mojo version for filtering. Defaults to global version.
        category:     Filter by pattern category (e.g. "security"). None = all.
    """
    from .gotchas import validate_code

    if code is None and path is None:
        return _json({
            "error": "Either 'code' or 'path' must be provided.",
            "hint": "validate(code='def main(): ...') or validate(path='/path/to/file.mojo')",
        })

    if mojo_version is None:
        mojo_version = _detect_mojo_version()

    # --- Directory mode: recursive scan ---
    if code is None and path is not None:
        p = Path(path).resolve()

        if p.is_dir():
            mojo_files = sorted(p.rglob("*.mojo"))
            if not mojo_files:
                return _json({
                    "error": f"No .mojo files found in {path}",
                    "hint": "validate(path='/path/to/project/') — directory must contain .mojo files.",
                })

            files_with_issues: list[dict[str, Any]] = []
            total_issues = 0
            for mf in mojo_files:
                try:
                    result = _validate_single_file(mf, mojo_version, category)
                except Exception as e:
                    result = {"path": str(mf), "issues": [], "count": 0, "error": str(e)}
                if result["count"] > 0 or "error" in result:
                    files_with_issues.append(result)
                    total_issues += result["count"]

            output: dict[str, Any] = {
                "files_scanned": len(mojo_files),
                "files_with_issues": len(files_with_issues),
                "total_issues": total_issues,
                "results": files_with_issues,
            }
            if category:
                output["category"] = category
            if not files_with_issues:
                label = f"{category} " if category else ""
                output["message"] = f"All {len(mojo_files)} files clean — no {label}patterns matched."
            return _json(output)

        # --- Single file mode ---
        if not p.is_file():
            return _json({
                "error": f"Not a file or directory: {path}",
                "hint": "validate(path='/path/to/file.mojo') or validate(path='/path/to/dir/')",
            })
        try:
            code = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return _json({
                "error": str(e),
                "hint": "validate(code='def main(): ...') or validate(path='/path/to/file.mojo')",
            })

    assert code is not None  # guaranteed by early returns above
    issues = validate_code(code, mojo_version, category=category, path=path)
    output = {"issues": issues, "count": len(issues)}
    if category:
        output["category"] = category
    if issues:
        output["hint"] = "Fix the issues above, then use execute(code=...) to test."
    else:
        label = f"{category} " if category else ""
        output["message"] = f"No {label}patterns matched."
        output["hint"] = "Code looks clean. Use execute(code=...) to run it."
    return _json(output)
