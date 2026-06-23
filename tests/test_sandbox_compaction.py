"""Effectful-shell wiring tests for diagnostic compaction (subprocess mocked)."""

from unittest.mock import MagicMock, patch

import mojo_mcp.sandbox as sb


def test_feature_detect_caches_on_version_and_mtime(monkeypatch):
    sb._JSON_DIAG_CACHE.clear()
    calls = {"n": 0}

    def fake_probe(mojo_prefix):
        calls["n"] += 1
        return True

    monkeypatch.setattr(sb, "_probe_json_diagnostics", fake_probe)
    monkeypatch.setattr(sb, "_version_key", lambda prefix: ("0.26.3", 1234))
    assert sb._supports_json_diagnostics(["mojo"]) is True
    assert sb._supports_json_diagnostics(["mojo"]) is True
    assert calls["n"] == 1   # second call served from cache


def test_feature_detect_rechecks_when_version_key_changes(monkeypatch):
    sb._JSON_DIAG_CACHE.clear()
    monkeypatch.setattr(sb, "_probe_json_diagnostics", lambda p: True)
    keys = iter([("0.26.3", 1), ("0.27.0", 2)])
    monkeypatch.setattr(sb, "_version_key", lambda prefix: next(keys))
    sb._supports_json_diagnostics(["mojo"])
    assert len(sb._JSON_DIAG_CACHE) == 1
    sb._supports_json_diagnostics(["mojo"])  # new key ⇒ new entry
    assert len(sb._JSON_DIAG_CACHE) == 2


from pathlib import Path


def test_build_project_roots_includes_wrapper_cwd_path_and_parent(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    sibling = tmp_path / "elsewhere"
    sibling.mkdir()
    src = sibling / "entry.mojo"
    src.write_text("x")
    wrapper = "/tmp/mojo-mcp-abc/main.mojo"
    roots = sb._build_project_roots(wrapper=wrapper, source_path=str(src), cwd=str(cwd))
    rp = {str(Path(r)) for r in roots}
    assert str(Path(wrapper).resolve()) in roots or wrapper in roots
    assert str(cwd.resolve()) in roots
    assert str(src.resolve()) in roots
    # parent of path (sibling modules) included because src is outside cwd
    assert str(sibling.resolve()) in roots


def test_build_ordinal_is_monotonic_per_session():
    a = sb._next_build_ordinal()
    b = sb._next_build_ordinal()
    assert b == a + 1


def test_ledger_is_module_scope_dict():
    assert isinstance(sb._SESSION_LEDGER, dict)


# ---------------------------------------------------------------------------
# Task 14 — Wire compaction into run_execute
# ---------------------------------------------------------------------------

def _fake_proc(stdout="", stderr="", rc=0):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=rc)


def test_execute_uses_json_diag_flag_and_compacts(tmp_path):
    import json
    ndjson = ('{"kind":"error","message":"use of unknown declaration \'X\'",'
              '"diagnostic":{"file":"main.mojo","location":{"line":7,"column":1},'
              '"ranges":[],"text":"x","fixIts":[]}}\n'
              '{"kind":"error","message":"failed to parse"}')
    with patch.object(sb, "_supports_json_diagnostics", return_value=True), \
         patch("mojo_mcp.sandbox.subprocess.run",
               return_value=_fake_proc(stdout="", stderr=ndjson, rc=1)), \
         patch("mojo_mcp.sandbox.shutil.rmtree"):
        out = json.loads(sb.run_execute("def main(): pass\n"))
    assert out["returncode"] == 1
    assert "diagnostics" in out
    assert any("unknown declaration" in e["message"] for e in out["diagnostics"]["errors"])
    # --diagnostic-format json was passed
    # (verified by the flag-capture test below)


def test_execute_passes_diagnostic_format_flag(tmp_path):
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _fake_proc(stdout="ok\n", stderr="", rc=0)

    with patch.object(sb, "_supports_json_diagnostics", return_value=True), \
         patch("mojo_mcp.sandbox.subprocess.run", side_effect=fake_run), \
         patch("mojo_mcp.sandbox.shutil.rmtree"):
        sb.run_execute("def main(): pass\n")
    assert "--diagnostic-format" in captured["cmd"]
    assert "json" in captured["cmd"]


def test_execute_text_fallback_when_flag_unsupported():
    import json
    with patch.object(sb, "_supports_json_diagnostics", return_value=False), \
         patch("mojo_mcp.sandbox.subprocess.run",
               return_value=_fake_proc(stdout="", stderr="error: boom", rc=1)) as mr, \
         patch("mojo_mcp.sandbox.shutil.rmtree"):
        out = json.loads(sb.run_execute("def main(): pass\n"))
    # flag NOT passed in fallback
    assert "--diagnostic-format" not in mr.call_args[0][0]
    # failure-coherence still holds (parse_fallback or errors)
    assert "diagnostics" in out


def test_execute_raw_disables_ledger_write():
    import json
    sb._SESSION_LEDGER.clear()
    ndjson = ('{"kind":"warning","message":"dep w",'
              '"diagnostic":{"file":"/dep/e.mojo","location":{"line":1,"column":1},'
              '"ranges":[],"text":"x","fixIts":[]}}')
    with patch.object(sb, "_supports_json_diagnostics", return_value=True), \
         patch("mojo_mcp.sandbox.subprocess.run",
               return_value=_fake_proc(stderr=ndjson, rc=1)), \
         patch("mojo_mcp.sandbox.shutil.rmtree"):
        json.loads(sb.run_execute("def main(): pass\n", raw=True))
    assert sb._SESSION_LEDGER == {}


def test_execute_clean_build_success():
    import json
    with patch.object(sb, "_supports_json_diagnostics", return_value=True), \
         patch("mojo_mcp.sandbox.subprocess.run",
               return_value=_fake_proc(stdout="hi\n", stderr="", rc=0)), \
         patch("mojo_mcp.sandbox.shutil.rmtree"):
        out = json.loads(sb.run_execute("def main(): print('hi')\n"))
    assert out["returncode"] == 0
    assert out["stdout"] == "hi\n"
    assert out["diagnostics"]["parse_fallback"] is None
