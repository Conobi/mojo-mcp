"""Sandboxed execution for search() and execute() tools."""

import concurrent.futures
import json
import shutil
import subprocess
import tempfile

MAX_OUTPUT = 8192  # 8KB cap to avoid flooding context


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


READ_FILE_MAX_BYTES = 100_000


def run_read_file(path: str) -> str:
    """Read a file; return JSON {path, content} or {error}."""
    from pathlib import Path

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
    from pathlib import Path

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


def run_execute(code: str) -> str:
    """Execute Mojo code in an isolated temp directory.

    Writes the agent's code to a temp .mojo file and runs `mojo run`.
    Cleans up after itself regardless of outcome.
    """
    tmp_dir = tempfile.mkdtemp(prefix="mojo-mcp-")
    tmp_file = f"{tmp_dir}/main.mojo"
    try:
        with open(tmp_file, "w") as f:
            f.write(code)

        result = subprocess.run(
            ["mojo", "run", tmp_file],
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
    except subprocess.TimeoutExpired:
        output = {"error": "execution timed out after 10 seconds"}
    except FileNotFoundError:
        output = {
            "error": (
                "mojo binary not found. "
                "Install it with: uv tool install modular"
            )
        }
    except Exception as e:
        output = {"error": str(e)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return json.dumps(output, indent=2)
