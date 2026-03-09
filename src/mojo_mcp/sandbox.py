"""Sandboxed execution for search() and execute() tools."""

import concurrent.futures
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_OUTPUT = 8192  # 8KB cap to avoid flooding context


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


def _mojo_cmd(version: str | None) -> list[str]:
    """Return the mojo command prefix for a given version.

    With a version: uses uvx --from modular==<version> mojo (cached per version).
    Without:        uses the globally installed mojo binary.
    """
    if version:
        return ["uvx", "--from", f"modular=={version}", "mojo"]
    return ["mojo"]


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
            return json.dumps({"error": "search timed out after 5 seconds"})
        except Exception as e:
            return json.dumps({"error": str(e)})


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
                return json.dumps({"error": f"Access denied: {blocked}"})
            except ValueError:
                pass
        if not p.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        raw = p.read_bytes()
        content = raw[:READ_FILE_MAX_BYTES].decode("utf-8", errors="replace")
        truncated = len(raw) > READ_FILE_MAX_BYTES
        if truncated:
            content += f"\n\n[Truncated at {READ_FILE_MAX_BYTES} bytes]"
        return json.dumps({"path": str(p), "content": content})
    except Exception as e:
        return json.dumps({"error": str(e)})


LIST_FILES_MAX_ENTRIES = 200


def run_list_files(path: str, pattern: str = "**/*.mojo") -> str:
    """List files matching glob; return JSON {path, pattern, files, truncated}."""
    import itertools

    try:
        base = Path(path).resolve()
        if not base.is_dir():
            return json.dumps({"error": f"Not a directory: {path}"})
        gen = base.glob(pattern)
        entries = sorted(str(f) for f in itertools.islice(gen, LIST_FILES_MAX_ENTRIES + 1))
        truncated = len(entries) > LIST_FILES_MAX_ENTRIES
        return json.dumps({
            "path": str(base),
            "pattern": pattern,
            "files": entries[:LIST_FILES_MAX_ENTRIES],
            "truncated": truncated,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

def run_execute(code: str, cwd: str | None = None) -> str:
    """Execute Mojo code in an isolated temp directory.

    If a .mojo-version file is found by walking up from `cwd`, the pinned
    version is run via `uvx --from modular==<version> mojo run` (cached by uv).
    Otherwise falls back to the globally installed `mojo` binary.
    """
    version_file, pinned_version = _find_mojo_version_file(cwd)
    mojo_prefix = _mojo_cmd(pinned_version)

    tmp_dir = tempfile.mkdtemp(prefix="mojo-mcp-")
    tmp_file = f"{tmp_dir}/main.mojo"
    try:
        with open(tmp_file, "w") as f:
            f.write(code)

        result = subprocess.run(
            [*mojo_prefix, "run", tmp_file],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=tmp_dir,
        )
        output = {
            "stdout": result.stdout[:MAX_OUTPUT],
            "stderr": result.stderr[:MAX_OUTPUT],
            "returncode": result.returncode,
        }
        if pinned_version:
            output["mojo_version"] = pinned_version
            output["version_file"] = str(version_file)
    except subprocess.TimeoutExpired:
        output = {"error": "execution timed out after 10 seconds"}
    except FileNotFoundError:
        if pinned_version:
            output = {
                "error": (
                    f"Could not run modular=={pinned_version} via uvx. "
                    "Ensure uv is installed: https://docs.astral.sh/uv/getting-started/installation/"
                )
            }
        else:
            output = {
                "error": (
                    "mojo binary not found. "
                    "Install it with: uv tool install modular  "
                    "Or call the install_mojo tool."
                )
            }
    except Exception as e:
        output = {"error": str(e)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return json.dumps(output, indent=2)


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

    return json.dumps(result, indent=2)


_GITHUB_URL = "git+https://github.com/Conobi/mojo-mcp"


def run_update_server() -> str:
    """Re-fetch the latest mojo-mcp from GitHub into the uvx cache.

    Uses `uvx --refresh` to pull the latest commit. The running process is
    not replaced — the user must restart Claude Code to load the new version.
    """
    uv_path = shutil.which("uv") or shutil.which("uvx")
    if not uv_path:
        return json.dumps({
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
            return json.dumps({
                "status": "updated",
                "version": proc.stdout.strip() or proc.stderr.strip(),
                "next_step": "Restart Claude Code to load the new version.",
            })
        return json.dumps({
            "error": "update failed",
            "stdout": proc.stdout[:MAX_OUTPUT],
            "stderr": proc.stderr[:MAX_OUTPUT],
            "returncode": proc.returncode,
        })
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "update timed out after 120 seconds"})
    except Exception as e:
        return json.dumps({"error": str(e)})


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
        return json.dumps({
            "error": "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
        })

    # Project-level pin
    if project_path is not None:
        proj = Path(project_path).resolve()
        if not proj.is_dir():
            return json.dumps({"error": f"Not a directory: {project_path}"})
        version_file = proj / ".mojo-version"

        if version is None:
            # Remove pin — revert to global
            if version_file.exists():
                version_file.unlink()
                return json.dumps({"status": "unpinned", "removed": str(version_file)})
            return json.dumps({"status": "no_pin_found", "path": str(proj)})

        # Write pin
        version_file.write_text(version + "\n")

        # Warm the uv cache so first execution is fast
        try:
            proc = subprocess.run(
                ["uvx", "--from", f"modular=={version}", "mojo", "--version"],
                capture_output=True, text=True, timeout=120,
            )
            mojo_ver = proc.stdout.strip() or proc.stderr.strip()
            return json.dumps({
                "status": "pinned",
                "version_file": str(version_file),
                "pinned_version": version,
                "mojo_version_output": mojo_ver,
            })
        except subprocess.TimeoutExpired:
            return json.dumps({
                "status": "pinned",
                "version_file": str(version_file),
                "pinned_version": version,
                "warning": "cache warm-up timed out; uv will download on first use",
            })
        except Exception as e:
            return json.dumps({
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
            return json.dumps({
                "status": action,
                "path": shutil.which("mojo"),
                "stdout": proc.stdout[:MAX_OUTPUT],
                "stderr": proc.stderr[:MAX_OUTPUT],
            })
        return json.dumps({
            "error": f"{action} failed",
            "stdout": proc.stdout[:MAX_OUTPUT],
            "stderr": proc.stderr[:MAX_OUTPUT],
            "returncode": proc.returncode,
        })
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "operation timed out after 120 seconds"})
    except Exception as e:
        return json.dumps({"error": str(e)})
