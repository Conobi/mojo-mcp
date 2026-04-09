"""Sandboxed execution for search() and execute() tools."""

import concurrent.futures
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_OUTPUT = 8192  # 8KB cap to avoid flooding context


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


def _mojo_cmd(version: str | None, cwd: str | None = None) -> list[str]:
    """Return the mojo command prefix for a given version.

    With a version: uses uvx --from mojo-compiler==<version> mojo (cached per version).
      .mojo-version files use the modular version format (e.g. "25.6.0"), but
      mojo-compiler on PyPI uses a "0."-prefixed format (e.g. "0.25.6.0").
      We normalise automatically.
    Without: uses mojox from the project venv if available, else system mojo.
    """
    if version:
        # Normalise "25.6.0" → "0.25.6.0" for mojo-compiler on PyPI.
        # Already-prefixed versions like "0.25.6.0" are left unchanged.
        if not version.startswith("0."):
            version = f"0.{version}"
        return ["uvx", "--from", f"mojo-compiler=={version}", "mojo"]

    # Check for mojox in the project's venv
    if cwd:
        mojox_bin = Path(cwd).resolve() / ".venv" / "bin" / "mojox"
        if mojox_bin.is_file():
            return [str(mojox_bin)]

    # Fallback: system mojo
    return ["mojo"]


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

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_exec)
    try:
        result_data = future.result(timeout=5)
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False)
        return _json({"error": "search timed out after 5 seconds"})
    except Exception as e:
        executor.shutdown(wait=False)
        return _json({"error": str(e)})
    else:
        executor.shutdown(wait=False)

    if result_data is None:
        return _json({
            "result": None,
            "message": "Search returned no results.",
            "hint": "Try broader terms, or use lookup('module.Symbol') if you know the name.",
        })

    raw = json.dumps(result_data, separators=(",", ":"), default=str)
    truncated = len(raw) > MAX_OUTPUT

    if truncated:
        return _json({
            "result_raw": raw[:MAX_OUTPUT],
            "truncated": True,
            "total_bytes": len(raw),
            "hint": "Result was truncated. Narrow your query to reduce output.",
        })

    return _json({
        "result": result_data,
        "hint": "Use lookup('<package>.<module>.<Symbol>') for full docs on any symbol.",
    })


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

READ_FILE_MAX_BYTES = 100_000


def run_read_file(path: str) -> str:
    """Read a file; return JSON {path, content} or {error}."""
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
        if truncated:
            content += f"\n\n[Truncated at {READ_FILE_MAX_BYTES} bytes]"
        return _json({"path": str(p), "content": content})
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
    """
    version_file, pinned_version = _find_mojo_version_file(cwd)
    mojo_prefix = _mojo_cmd(pinned_version, cwd)

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

        cmd = [*mojo_prefix, "run", *extra_flags, tmp_file]
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
        output = {
            "stdout": result.stdout[:MAX_OUTPUT],
            "returncode": result.returncode,
            "duration_s": duration,
        }
        # Only include stderr when non-empty or on failure
        if result.stderr or result.returncode != 0:
            output["stderr"] = result.stderr[:MAX_OUTPUT]
        if pinned_version:
            output["mojo_version"] = pinned_version
            output["version_file"] = str(version_file)
        # Enrich failed executions with gotcha hints
        if result.returncode != 0:
            output["hint"] = "Use validate(code=...) to check for known gotcha patterns."
            summary = _extract_error_summary(result.stderr)
            if summary:
                output["error_summary"] = summary
            from .gotchas import enrich_error
            version_for_enrich = pinned_version or "0.26.0"
            parts = version_for_enrich.split(".")
            version_for_enrich = ".".join(parts[:3])
            hints = enrich_error(result.stderr, timed_out=False, mojo_version=version_for_enrich)
            if hints:
                output["gotcha_hints"] = hints
    except subprocess.TimeoutExpired:
        from .gotchas import enrich_error
        version_for_enrich = pinned_version or "0.26.0"
        parts = version_for_enrich.split(".")
        version_for_enrich = ".".join(parts[:3])
        hints = enrich_error(code, timed_out=True, mojo_version=version_for_enrich)
        output = {"error": f"execution timed out after {timeout} seconds"}
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

    return _json(output)


# ---------------------------------------------------------------------------
# Version management
# ---------------------------------------------------------------------------

def run_mojo_version(path: str | None = None) -> str:
    """Report global installed version and project-pinned version.

    Returns JSON with:
      - global_version: output of `mojo --version` (or error)
      - pinned_version: version string from nearest .mojo-version file
      - version_file: path to that file
    """
    result: dict = {}

    # Global version
    mojo_path = shutil.which("mojo")
    if mojo_path:
        try:
            proc = subprocess.run(
                ["mojo", "--version"], capture_output=True, text=True, timeout=5
            )
            result["global_version"] = proc.stdout.strip() or proc.stderr.strip()
            result["global_binary"] = mojo_path
        except Exception as e:
            result["global_version_error"] = str(e)
    else:
        result["global_version"] = None
        result["global_binary"] = None

    # Project-pinned version
    version_file, pinned_version = _find_mojo_version_file(path)
    result["pinned_version"] = pinned_version
    result["version_file"] = str(version_file) if version_file else None

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
                "next_step": "Restart Claude Code to load the new version.",
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
                return _json({"status": "unpinned", "removed": str(version_file)})
            return _json({"status": "no_pin_found", "path": str(proj)})

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
            })
        except subprocess.TimeoutExpired:
            return _json({
                "status": "pinned",
                "version_file": str(version_file),
                "pinned_version": version,
                "warning": "cache warm-up timed out; uv will download on first use",
            })
        except Exception as e:
            return _json({
                "status": "pinned",
                "version_file": str(version_file),
                "pinned_version": version,
                "warning": str(e),
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

def run_validate(
    code: str | None = None,
    path: str | None = None,
    mojo_version: str | None = None,
) -> str:
    """Validate Mojo source code against known gotcha patterns.

    Args:
        code:         Mojo source code string. Takes precedence over path.
        path:         Path to a .mojo file to validate.
        mojo_version: Mojo version for filtering. Defaults to global version.
    """
    from .gotchas import validate_code

    if code is None and path is None:
        return _json({
            "error": "Either 'code' or 'path' must be provided.",
            "hint": "validate(code='fn main(): ...') or validate(path='/path/to/file.mojo')",
        })

    if code is None:
        try:
            assert path is not None  # guarded by check above
            p = Path(path).resolve()
            if not p.is_file():
                return _json({"error": f"Not a file: {path}"})
            code = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return _json({"error": str(e)})

    if mojo_version is None:
        try:
            proc = subprocess.run(
                ["mojo", "--version"], capture_output=True, text=True, timeout=5
            )
            version_str = proc.stdout.strip().split()[1] if proc.stdout.strip() else "0.26.0"
            parts = version_str.split(".")
            mojo_version = ".".join(parts[:3])
        except Exception:
            mojo_version = "0.26.0"

    issues = validate_code(code, mojo_version)
    output: dict[str, Any] = {"issues": issues, "count": len(issues)}
    if issues:
        output["hint"] = "Fix the issues above, then use execute(code=...) to test."
    else:
        output["message"] = "No known gotcha patterns matched."
        output["hint"] = "Code looks clean. Use execute(code=...) to run it."
    return _json(output)
