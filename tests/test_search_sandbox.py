"""Regression: a runaway search snippet must not block interpreter shutdown.

Agent-supplied search code runs in a daemon thread. A non-terminating snippet
(e.g. ``while True: pass``) cannot be killed once started, so it is abandoned.
As a *daemon* it can never deadlock CPython's atexit thread-join — the bug that
previously left a ThreadPoolExecutor worker alive and hung the whole process
after the tests had passed.
"""
import json
import threading

from mojo_mcp import sandbox
from mojo_mcp.sandbox import run_search


def test_runaway_search_times_out_and_leaves_only_a_daemon(monkeypatch):
    monkeypatch.setattr(sandbox, "SEARCH_TIMEOUT_S", 0.3)
    before = set(threading.enumerate())

    result = json.loads(run_search("while True:\n    pass", {"x": 1}))
    assert "timed out" in result["error"]

    # The runaway worker is still running, but MUST be a daemon so it can never
    # block interpreter / test-process shutdown.
    leftover = [
        t
        for t in threading.enumerate()
        if t not in before and t.name == "mojo-mcp-search"
    ]
    assert leftover, "expected the runaway search worker to still be alive"
    assert all(t.daemon for t in leftover), "runaway search worker must be a daemon"


def test_normal_search_unaffected():
    result = json.loads(run_search("return sorted(docs.keys())", {"b": 1, "a": 2}))
    assert "error" not in result
    assert result["result"] == ["a", "b"]
