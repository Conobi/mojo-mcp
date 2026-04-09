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
    """

    def _exec() -> str:
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
        return json.dumps(local_ns.get("_result"), indent=2, default=str)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_exec)
        try:
            result = future.result(timeout=5)
            return result[:MAX_OUTPUT]
        except concurrent.futures.TimeoutError:
            return _json({"error": "search timed out after 5 seconds"})
        except Exception as e:
            return _json({"error": str(e)})


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


def run_list_files(path: str, pattern: str = "**/*.mojo") -> str:
    """List files matching glob; return JSON {path, pattern, files, truncated}."""
    import itertools

    try:
        base = Path(path).resolve()
        if not base.is_dir():
            return _json({"error": f"Not a directory: {path}"})
        gen = base.glob(pattern)
        entries = sorted(str(f) for f in itertools.islice(gen, LIST_FILES_MAX_ENTRIES + 1))
        truncated = len(entries) > LIST_FILES_MAX_ENTRIES
        return _json({
            "path": str(base),
            "pattern": pattern,
            "files": entries[:LIST_FILES_MAX_ENTRIES],
            "truncated": truncated,
        })
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=run_cwd or tmp_dir,
            env=run_env,
        )
        output = {
            "stdout": result.stdout[:MAX_OUTPUT],
            "stderr": result.stderr[:MAX_OUTPUT],
            "returncode": result.returncode,
        }
        if pinned_version:
            output["mojo_version"] = pinned_version
            output["version_file"] = str(version_file)
        # Enrich failed executions with gotcha hints
        if result.returncode != 0:
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
        return _json({"error": "Either 'code' or 'path' must be provided."})

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
    return _json({"issues": issues, "count": len(issues)})
