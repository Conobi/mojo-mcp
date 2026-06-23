"""group_diagnostics + order_groups unit tests."""

from mojo_mcp.diagnostics import (
    Diagnostic,
    attach_notes,
    compact_diagnostics,
    group_diagnostics,
    order_groups,
)

ROOTS = frozenset({"main.mojo"})


def _d(kind, msg, file, line, fixits=()):
    return Diagnostic(kind=kind, message=msg, file=file, line=line, fixits=fixits)


def test_identical_class_collapses_with_count_and_lineset():
    diags = [_d("warning", "dep", "e.mojo", n, ("comptime",)) for n in (3, 1, 3, 2)]
    groups = group_diagnostics(attach_notes(diags), ROOTS)
    assert len(groups) == 1
    g = groups[0]
    assert g.count == 4
    assert g.lines == (1, 2, 3)        # sorted distinct
    assert g.fixits == ("comptime",)   # de-duplicated


def test_different_file_yields_separate_groups():
    diags = [_d("warning", "dep", "a.mojo", 1), _d("warning", "dep", "b.mojo", 1)]
    groups = group_diagnostics(attach_notes(diags), ROOTS)
    assert len(groups) == 2


def test_changed_message_is_new_group():
    diags = [_d("error", "msg A", "a.mojo", 1), _d("error", "msg B", "a.mojo", 1)]
    groups = group_diagnostics(attach_notes(diags), ROOTS)
    assert len(groups) == 2


def test_notes_become_attached_note_groups_inheriting_parent_origin():
    diags = [_d("error", "no match", "main.mojo", 5),
             Diagnostic(kind="note", message="candidate", file="dep.mojo", line=9)]
    groups = group_diagnostics(attach_notes(diags), ROOTS)
    assert len(groups) == 1
    g = groups[0]
    assert g.origin == "project"
    assert len(g.notes) == 1
    assert g.notes[0].kind == "note"
    assert g.notes[0].origin == "project"   # inherits parent origin


def _g(kind, msg, file, origin, line):
    from mojo_mcp.diagnostics import Group
    return Group(kind=kind, message=msg, file=file, origin=origin,
                 lines=(line,) if line is not None else (), count=1)


def test_errors_before_warnings_project_before_dependency():
    groups = [
        _g("warning", "w", "a.mojo", "project", 1),
        _g("error", "dep err", "d.mojo", "dependency", 2),
        _g("error", "proj err", "a.mojo", "project", 3),
    ]
    errors, warnings = order_groups(groups)
    assert [g.message for g in errors] == ["proj err", "dep err"]
    assert [g.message for g in warnings] == ["w"]


def test_within_kind_origin_stable_by_file_then_line():
    groups = [
        _g("error", "e2", "b.mojo", "project", 5),
        _g("error", "e1", "a.mojo", "project", 9),
    ]
    errors, _ = order_groups(groups)
    assert [g.file for g in errors] == ["a.mojo", "b.mojo"]


def test_summary_non_suppressible_error_sorts_with_errors():
    groups = [_g("warning", "w", "a.mojo", "project", 1),
              _g("error", "summary", None, "non_suppressible", None)]
    errors, warnings = order_groups(groups)
    assert any(g.message == "summary" for g in errors)


def test_raw_mode_does_not_group_same_message_different_line():
    """raw=True must render each diagnostic as a separate entry (no grouping).

    Two distinct dependency warnings with the same (kind, message, file) but
    different lines (1 and 5) must appear as TWO separate entries, not merged
    into a single count=2 group.
    """
    import json

    warn_line1 = json.dumps({
        "kind": "warning",
        "message": "'alias' is deprecated; use 'comptime'",
        "diagnostic": {"file": "dep/errno.mojo", "location": {"line": 1, "column": 1},
                       "ranges": [], "text": "alias x", "fixIts": []},
    })
    warn_line5 = json.dumps({
        "kind": "warning",
        "message": "'alias' is deprecated; use 'comptime'",
        "diagnostic": {"file": "dep/errno.mojo", "location": {"line": 5, "column": 1},
                       "ranges": [], "text": "alias y", "fixIts": []},
    })
    ndjson = "\n".join([warn_line1, warn_line5])
    r = compact_diagnostics(
        ndjson,
        project_roots=frozenset({"main.mojo"}),
        returncode=0,
        raw_stderr="",
        ledger={},
        build_ordinal=1,
        raw=True,
    )
    # Must produce 2 separate warning groups, not one merged count=2 group
    assert len(r.warnings) == 2, (
        f"raw=True must not group: expected 2 warning entries, got {len(r.warnings)}; "
        f"groups: {[(g.count, g.lines) for g in r.warnings]}"
    )
    assert r.warnings[0].count == 1
    assert r.warnings[1].count == 1


def test_raw_mode_errors_precede_warnings_in_rendered_output():
    """Killing _raw_order mutants 3, 4, 5 (error/warning partition broken).

    In raw=True mode, _raw_order floats errors to the top of the parented list
    before grouping. If the condition is inverted, negated, or case-wrong, warnings
    would appear first. This test uses mixed error+warning input to verify order.
    """
    import json

    error_line = json.dumps({
        "kind": "error",
        "message": "an error message",
        "diagnostic": {"file": "main.mojo", "location": {"line": 1, "column": 1},
                       "ranges": [], "text": "fn main():", "fixIts": []},
    })
    warning_line = json.dumps({
        "kind": "warning",
        "message": "a deprecation warning",
        "diagnostic": {"file": "dep/e.mojo", "location": {"line": 5, "column": 1},
                       "ranges": [], "text": "alias x", "fixIts": []},
    })
    # Warning first in stream, error second — raw=True must float error to top
    ndjson = "\n".join([warning_line, error_line])
    r = compact_diagnostics(
        ndjson,
        project_roots=frozenset({"main.mojo"}),
        returncode=1,
        raw_stderr=ndjson,
        ledger={},
        build_ordinal=1,
        raw=True,
    )
    rendered = r.rendered
    err_pos = rendered.find("an error message")
    warn_pos = rendered.find("a deprecation warning")
    assert err_pos != -1, "error message not in rendered output"
    assert warn_pos != -1, "warning message not in rendered output"
    assert err_pos < warn_pos, (
        f"Error must precede warning in raw mode output "
        f"(err_pos={err_pos}, warn_pos={warn_pos})"
    )
