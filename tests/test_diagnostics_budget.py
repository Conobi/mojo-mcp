"""render_groups, budget_truncate, compact_diagnostics, expand tests."""

from pathlib import Path

from mojo_mcp.diagnostics import (
    HARD_CAP,
    CompactionResult,
    Diagnostic,
    Group,
    LedgerEntry,
    budget_truncate,
    compact_diagnostics,
    expand,
    render_groups,
    _suppressed_render,
    _render_one,
    _line_range,
)

FIX = Path(__file__).parent / "fixtures" / "diagnostics"


def _g(kind, msg, file, origin, lines, count, fixits=(), notes=()):
    return Group(kind=kind, message=msg, file=file, origin=origin,
                 lines=tuple(lines), count=count, fixits=fixits, notes=notes)


def test_line_range_empty():
    """Killing _line_range mutant 2 (returns 'XXXX' instead of '' for empty)."""
    assert _line_range(()) == "", f"Empty tuple should yield '', got: {_line_range(())!r}"


def test_line_range_single_element():
    """Killing _line_range mutants 3 (!=1→format branch), 4 (==2→scalar), 5 (str(None))."""
    assert _line_range((42,)) == "42", (
        f"Single-element tuple should yield '42', got: {_line_range((42,))!r}")
    # Specifically: must not return range format or 'None'
    assert _line_range((1,)) == "1", (
        f"Single-element (1,) must yield '1', got: {_line_range((1,))!r}")


def test_line_range_contiguous():
    """Killing _line_range mutant 7 (contiguous=None, never uses dash format).

    A contiguous run must produce 'start-end', not 'start,start+1,...,end'.
    """
    assert _line_range((1, 2, 3)) == "1-3", (
        f"Contiguous (1,2,3) should yield '1-3', got: {_line_range((1,2,3))!r}")
    assert _line_range((5, 6)) == "5-6", (
        f"Contiguous (5,6) should yield '5-6', got: {_line_range((5,6))!r}")
    # The existing test checks 1-133 but via render; this is a direct unit check
    assert _line_range(tuple(range(1, 134))) == "1-133", (
        f"Contiguous range 1-133 should yield '1-133'")


def test_render_collapses_count_and_lines():
    g = _g("warning", "'alias' is deprecated; use 'comptime'", "errno.mojo",
           "dependency", range(1, 134), 133, fixits=("comptime",))
    rendered = render_groups([g])[0]
    assert "133×" in rendered
    assert "errno.mojo" in rendered
    assert "1" in rendered and "133" in rendered
    assert "comptime" in rendered


def test_render_includes_notes_after_parent():
    note = _g("note", "previous definition here", "a.mojo", "project", [2], 1)
    g = _g("error", "invalid redefinition", "a.mojo", "project", [3], 1, notes=(note,))
    rendered = render_groups([g])[0]
    assert rendered.index("invalid redefinition") < rendered.index("previous definition here")


def test_expand_yields_one_diagnostic_per_distinct_tuple():
    g = _g("warning", "dep", "e.mojo", "dependency", [1, 2, 3], 3)
    from mojo_mcp.diagnostics import CompactionResult
    r = CompactionResult(warnings=(g,))
    diags = expand(r)
    tuples = {(d.kind, d.message, d.file, d.line) for d in diags}
    assert tuples == {("warning", "dep", "e.mojo", 1),
                      ("warning", "dep", "e.mojo", 2),
                      ("warning", "dep", "e.mojo", 3)}


def test_errors_never_truncated_below_first_under_tiny_cap():
    err = "- error one — a.mojo:1"
    rendered, twg, teg, pf = budget_truncate(
        [err, "- error two — a.mojo:2"], [], ["- warn — b.mojo:1"], None,
        hard_cap=len(err) + 5, soft_budget=10)
    assert "error one" in rendered
    assert teg >= 1            # at least the second error elided, counted
    assert "warn" not in rendered


def test_warning_tail_dropped_with_count_marker():
    warns = [f"- warn {i} — f.mojo:{i}" for i in range(50)]
    rendered, twg, teg, pf = budget_truncate(
        ["- err — a.mojo:1"], [], warns, None, hard_cap=HARD_CAP, soft_budget=120)
    assert twg > 0
    assert "truncated_warning_groups" in rendered or f"{twg}" in rendered


def test_parse_fallback_truncated_to_fit_hard_cap():
    huge = "x" * 500_000
    rendered, twg, teg, pf = budget_truncate(
        [], [], [], huge, hard_cap=HARD_CAP, soft_budget=SOFT_BUDGET) if False else \
        budget_truncate([], [], [], huge, hard_cap=1000, soft_budget=200)
    assert len(rendered.encode("utf-8")) <= 1000
    assert pf is not None
    assert "elided" in rendered.lower() or "raw stderr" in rendered.lower()


from mojo_mcp.diagnostics import SOFT_BUDGET  # noqa: E402


def test_clean_stream_exit0_is_success():
    r = compact_diagnostics("", project_roots=frozenset({"main.mojo"}),
                            returncode=0, raw_stderr="", ledger={}, build_ordinal=1)
    assert r.errors == ()
    assert r.parse_fallback is None
    assert r.rendered == ""


def test_nonzero_exit_no_json_yields_parse_fallback():
    r = compact_diagnostics("", project_roots=frozenset({"main.mojo"}),
                            returncode=1, raw_stderr="ld: undefined symbol",
                            ledger={}, build_ordinal=1)
    assert r.parse_fallback is not None
    assert "undefined symbol" in r.rendered


def test_golden_note_summary_roundtrips():
    text = (FIX / "golden_note_summary.ndjson").read_text()
    r = compact_diagnostics(text, project_roots=frozenset({"note2.mojo"}),
                            returncode=1, raw_stderr=text, ledger={}, build_ordinal=1)
    assert any("invalid redefinition" in g.message for g in r.errors)
    note_parents = [g for g in r.errors if g.notes]
    assert any(n.message == "previous definition here"
               for g in note_parents for n in g.notes)


def test_raw_mode_floats_errors_first_no_grouping_no_ledger():
    text = (FIX / "golden_two_notes.ndjson").read_text()
    r = compact_diagnostics(text, project_roots=frozenset({"note.mojo"}),
                            returncode=1, raw_stderr=text, ledger={},
                            build_ordinal=1, raw=True)
    assert r.suppress_keys == frozenset()
    assert r.new_ledger_records == {}
    assert "no matching function" in r.rendered
    assert len(r.rendered.encode("utf-8")) <= HARD_CAP


def test_expand_dedup_across_duplicate_keys():
    """Killing expand mutants 7 (break→continue) and 8 (seen.add(None)→seen.add(key)).

    If dedup is broken a group with N identical (kind, msg, file, line) tuples would
    emit N Diagnostics instead of 1. This test verifies the de-duplication gate.
    """
    # Two groups with the same key — only one Diagnostic should appear
    g1 = _g("error", "dup", "a.mojo", "project", [5], 2)
    g2 = _g("error", "dup", "a.mojo", "project", [5], 1)
    r = CompactionResult(errors=(g1, g2))
    diags = expand(r)
    # Exactly one Diagnostic for (error, dup, a.mojo, 5)
    matching = [(d.kind, d.message, d.file, d.line) for d in diags
                if d.kind == "error" and d.message == "dup" and d.file == "a.mojo" and d.line == 5]
    assert len(matching) == 1, f"Expected 1 deduped entry, got {len(matching)}: {matching}"


def test_expand_preserves_fixits():
    """Killing expand mutant 14 (fixits=None instead of fixits=g.fixits).

    expand() must carry fixits from the Group through to the emitted Diagnostics.
    """
    g = _g("error", "bad call", "b.mojo", "project", [3], 1, fixits=("use bar",))
    r = CompactionResult(errors=(g,))
    diags = expand(r)
    assert len(diags) == 1
    assert diags[0].fixits == ("use bar",), f"fixits not preserved: {diags[0].fixits}"


def test_suppressed_render_fallback_question_mark_when_no_entry():
    """Killing _suppressed_render mutant 4 (fallback "?" → "XX?XX").

    When the ledger has no entry for the group, the rendered string must show '#?'
    as the build number. This exercises the `entry is None` branch.
    """
    g_text = ('{"kind":"warning","message":"orphan dep",'
              '"diagnostic":{"file":"dep/x.mojo","location":{"line":2,"column":1},'
              '"ranges":[],"text":"x","fixIts":[]}}')
    # Empty ledger: no entry, so fallback "?" must appear
    r = compact_diagnostics(g_text, project_roots=frozenset({"main.mojo"}),
                            returncode=1, raw_stderr=g_text,
                            ledger={}, build_ordinal=1)
    # First build: group is shown (not suppressed), ledger now has the entry.
    # Second build: ledger matches → suppressed → rendered with build #1
    ledger2 = dict(r.new_ledger_records)
    r2 = compact_diagnostics(g_text, project_roots=frozenset({"main.mojo"}),
                             returncode=1, raw_stderr=g_text,
                             ledger=ledger2, build_ordinal=2)
    assert "unchanged since build #1" in r2.rendered

    # Now force a suppressed render where ledger lookup would miss (manual call).
    from mojo_mcp.diagnostics import _suppressed_render, Group
    g = Group(kind="warning", message="orphan dep", file="dep/x.mojo",
              origin="dependency", lines=(2,), count=1)
    # ledger has no entry for this group: fallback should be '?'
    s = _suppressed_render(g, {})
    assert "#?" in s, f"Expected '#?' in suppressed render, got: {s!r}"


def test_suppressed_render_format_with_line_range():
    """Killing _suppressed_render mutants 5, 6, 7, 8, 9 (rng/where mutations).

    The rendered one-liner must include: the file path, a line range, the build
    number, and the count. Mutations that corrupt rng/where produce strings
    missing the file:lines component.
    """
    from mojo_mcp.diagnostics import _suppressed_render, Group
    g = Group(kind="warning", message="alias dep", file="dep/errno.mojo",
              origin="dependency", lines=(1, 2, 3), count=3)
    ledger = {("warning", "alias dep", "dep/errno.mojo"):
              LedgerEntry(first_build=4, last_count=3, last_lines=frozenset({1, 2, 3}))}
    s = _suppressed_render(g, ledger)
    # Must contain file path, range, and build number
    assert "dep/errno.mojo" in s, f"file missing: {s!r}"
    assert "1-3" in s, f"range '1-3' missing: {s!r}"
    assert "#4" in s, f"build number missing: {s!r}"
    assert "3×" in s, f"count missing: {s!r}"


def test_suppressed_render_format_no_lines():
    """Killing _suppressed_render mutants 7, 8, 9 on the 'no rng' path.

    When a group has no lines the 'where' should be just the file path (not None
    or 'XXXX' or the file ANDed with empty string).
    """
    from mojo_mcp.diagnostics import _suppressed_render, Group
    g = Group(kind="warning", message="alias dep", file="dep/errno.mojo",
              origin="dependency", lines=(), count=1)
    ledger = {("warning", "alias dep", "dep/errno.mojo"):
              LedgerEntry(first_build=2, last_count=1, last_lines=frozenset())}
    s = _suppressed_render(g, ledger)
    assert "dep/errno.mojo" in s, f"file missing on no-lines path: {s!r}"
    assert "None" not in s, f"'None' leaked into rendered string: {s!r}"
    assert "XXXX" not in s, f"'XXXX' leaked into rendered string: {s!r}"
    assert "#2" in s, f"build number missing: {s!r}"


def test_render_one_summary_placeholder():
    """Killing _render_one mutants 4 and 5 (summary placeholder text mutation).

    When a group has no file, _render_one must use exactly '(summary)' — not
    'XX(summary)XX' or '(SUMMARY)'.
    """
    g = _g("error", "build failed", None, "non_suppressible", [], 1)
    rendered = _render_one(g)
    assert "(summary)" in rendered, f"Expected '(summary)' in: {rendered!r}"
    assert "SUMMARY" not in rendered, f"'SUMMARY' (wrong case) found in: {rendered!r}"
    assert "XX" not in rendered, f"'XX' placeholder leaked in: {rendered!r}"


def test_render_one_count_two_uses_multi_count_format():
    """Killing _render_one mutant 11 (count != 2 instead of count != 1).

    A group with count==2 must use the 'NX' format, not the single-item format.
    The mutation flips the boundary: count==2 gets the single-line format instead.
    """
    g = _g("warning", "dep msg", "a.mojo", "project", [3, 5], 2)
    rendered = _render_one(g)
    assert "2×" in rendered, f"Expected '2×' (multi-count) for count=2, got: {rendered!r}"


def test_render_one_note_indent_is_two_spaces():
    """Killing _render_one mutant 19 (indent=None for note recursive call).

    Notes must be indented with '  ' (two spaces) relative to their parent.
    Passing None as indent would crash or produce 'None-' prefix.
    """
    note = _g("note", "see here", "a.mojo", "project", [2], 1)
    g = _g("error", "bad ref", "a.mojo", "project", [3], 1, notes=(note,))
    rendered = _render_one(g)
    lines = rendered.split("\n")
    assert len(lines) == 2, f"Expected parent + 1 note line, got: {lines!r}"
    assert lines[1].startswith("  -"), (
        f"Note line must start with '  -' (two-space indent), got: {lines[1]!r}")


def test_render_one_includes_file_colon_range():
    """Killing _render_one mutant 6 (rng = None, drops line range from output).

    When a group has lines, the rendered string must include 'file:range'.
    """
    g = _g("warning", "deprecated", "a.mojo", "project", [5, 6, 7], 3)
    rendered = _render_one(g)
    assert "a.mojo:5-7" in rendered, f"Expected 'a.mojo:5-7' in: {rendered!r}"


def test_render_one_multiple_fixits_comma_separated():
    """Killing _render_one mutant 15 (fixit join separator 'XX, XX').

    Multiple fixits must be joined by ', ' not 'XX, XX'.
    """
    g = _g("warning", "foo", "a.mojo", "project", [1], 1, fixits=("bar", "baz"))
    rendered = _render_one(g)
    assert "bar, baz" in rendered, f"Expected 'bar, baz' in: {rendered!r}"
    assert "XX" not in rendered, f"'XX' separator leaked in: {rendered!r}"


def test_decide_suppression_records_correct_last_lines():
    """Killing decide_suppression mutant 29 (last_lines=None instead of cur_lines).

    When a group's ledger is updated (changed or evicted), the returned record
    must have last_lines equal to the current frozenset of lines — not None —
    so the next build can correctly compare and suppress.
    """
    from mojo_mcp.diagnostics import Group, decide_suppression

    def _w(msg, file, lines):
        return Group(kind="warning", message=msg, file=file, origin="dependency",
                     lines=tuple(lines), count=len(lines))

    # Evicted key: no entry in ledger, creates a new record
    g = _w("alias dep", "d.mojo", [1, 2, 3])
    _shown, _suppressed, _keys, records = decide_suppression([g], {}, build_ordinal=5)
    entry = records[("warning", "alias dep", "d.mojo")]
    assert entry.last_lines == frozenset({1, 2, 3}), (
        f"last_lines should be frozenset({{1,2,3}}), got {entry.last_lines!r}")

    # Changed group: old entry had different count
    old_entry = __import__("mojo_mcp.diagnostics", fromlist=["LedgerEntry"]).LedgerEntry(
        first_build=1, last_count=2, last_lines=frozenset({1, 2}))
    _shown, _suppressed, _keys, records2 = decide_suppression(
        [g], {("warning", "alias dep", "d.mojo"): old_entry}, build_ordinal=6)
    entry2 = records2[("warning", "alias dep", "d.mojo")]
    assert entry2.last_lines == frozenset({1, 2, 3}), (
        f"last_lines should be frozenset({{1,2,3}}), got {entry2.last_lines!r}")


def test_suppressed_marker_present_when_ledger_matches():
    g_text = ('{"kind":"warning","message":"alias dep",'
              '"diagnostic":{"file":"dep/e.mojo","location":{"line":1,"column":1},'
              '"ranges":[],"text":"x","fixIts":[]}}')
    ledger = {("warning", "alias dep", "dep/e.mojo"):
              LedgerEntry(first_build=1, last_count=1, last_lines=frozenset({1}))}
    r = compact_diagnostics(g_text, project_roots=frozenset({"main.mojo"}),
                            returncode=1, raw_stderr=g_text, ledger=ledger, build_ordinal=2)
    assert ("warning", "alias dep", "dep/e.mojo") in r.suppress_keys
    assert "unchanged since build #1" in r.rendered
