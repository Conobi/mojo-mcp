"""decide_suppression unit tests (eligibility, change-detection, eviction)."""

from mojo_mcp.diagnostics import Group, LedgerEntry, decide_suppression


def _w(msg, file, lines, origin="dependency"):
    return Group(kind="warning", message=msg, file=file, origin=origin,
                 lines=tuple(lines), count=len(lines))


def test_unchanged_dependency_warning_is_suppressed():
    g = _w("alias dep", "d.mojo", [1, 2, 3])
    ledger = {("warning", "alias dep", "d.mojo"):
              LedgerEntry(first_build=1, last_count=3, last_lines=frozenset({1, 2, 3}))}
    shown, suppressed, keys, records = decide_suppression([g], ledger, build_ordinal=5)
    assert g.file in {s.file for s in suppressed}
    assert ("warning", "alias dep", "d.mojo") in keys
    assert shown == []


def test_changed_count_is_reshown_not_suppressed():
    g = _w("alias dep", "d.mojo", [1, 2, 3, 4, 5])  # was 3, now 5
    ledger = {("warning", "alias dep", "d.mojo"):
              LedgerEntry(first_build=1, last_count=3, last_lines=frozenset({1, 2, 3}))}
    shown, suppressed, keys, records = decide_suppression([g], ledger, build_ordinal=5)
    assert shown == [g]
    assert suppressed == []
    assert keys == frozenset()
    # ledger updated to the new count/lines
    assert records[("warning", "alias dep", "d.mojo")].last_count == 5


def test_project_warning_never_suppressed():
    g = _w("proj", "main.mojo", [1], origin="project")
    ledger = {("warning", "proj", "main.mojo"):
              LedgerEntry(first_build=1, last_count=1, last_lines=frozenset({1}))}
    shown, suppressed, keys, records = decide_suppression([g], ledger, build_ordinal=2)
    assert shown == [g]
    assert keys == frozenset()


def test_evicted_key_shown_in_full_and_first_build_reestablished():
    g = _w("alias dep", "d.mojo", [1, 2, 3])
    shown, suppressed, keys, records = decide_suppression([g], {}, build_ordinal=7)
    assert shown == [g]
    assert keys == frozenset()
    assert records[("warning", "alias dep", "d.mojo")].first_build == 7


def test_unchanged_suppression_preserves_original_first_build():
    g = _w("alias dep", "d.mojo", [1, 2, 3])
    ledger = {("warning", "alias dep", "d.mojo"):
              LedgerEntry(first_build=2, last_count=3, last_lines=frozenset({1, 2, 3}))}
    _shown, _suppressed, _keys, records = decide_suppression([g], ledger, build_ordinal=9)
    assert records[("warning", "alias dep", "d.mojo")].first_build == 2
