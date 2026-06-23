"""parse_ndjson + attach_notes unit and golden tests."""

from __future__ import annotations

from pathlib import Path

from mojo_mcp.diagnostics import Diagnostic, attach_notes, parse_ndjson

FIX = Path(__file__).parent / "fixtures" / "diagnostics"


def test_parses_line_bound_diagnostic():
    line = ('{"diagnostic":{"file":"a.mojo","fixIts":[{"end":{"column":3,"line":1},'
            '"start":{"column":1,"line":1},"text":"def"}],"location":{"column":0,'
            '"line":1},"ranges":[{"end":21,"start":11}],"text":"fn main():"},'
            '"kind":"warning","message":"deprecated"}')
    d = parse_ndjson(line)[0]
    assert d.kind == "warning"
    assert d.message == "deprecated"
    assert d.file == "a.mojo"
    assert d.line == 1
    assert d.column == 0
    assert d.source_text == "fn main():"
    assert d.fixits == ("def",)
    assert d.ranges == ((11, 21),)
    assert d.is_summary is False
    assert d.parse_error is None


def test_summary_line_has_none_file_and_is_summary():
    d = parse_ndjson('{"kind":"error","message":"failed to parse"}')[0]
    assert d.kind == "error"
    assert d.message == "failed to parse"
    assert d.file is None
    assert d.line is None
    assert d.is_summary is True


def test_note_kind_parsed():
    d = parse_ndjson('{"kind":"note","message":"previous definition here",'
                     '"diagnostic":{"file":"a.mojo","location":{"line":2,"column":8},'
                     '"ranges":[],"text":"var x = 1","fixIts":[]}}')[0]
    assert d.kind == "note"
    assert d.line == 2


def test_malformed_line_becomes_parse_error_and_stream_continues():
    stream = 'not json at all\n{"kind":"error","message":"real"}'
    diags = parse_ndjson(stream)
    assert len(diags) == 2
    assert diags[0].parse_error is not None
    assert diags[1].kind == "error" and diags[1].message == "real"


def test_blank_lines_skipped():
    assert parse_ndjson("\n\n") == []


def test_golden_note_summary_fixture():
    diags = parse_ndjson((FIX / "golden_note_summary.ndjson").read_text())
    kinds = [d.kind for d in diags]
    assert kinds == ["warning", "error", "note", "error"]
    assert diags[3].is_summary is True
    assert diags[2].message == "previous definition here"


def test_note_attaches_to_preceding_diagnostic():
    diags = parse_ndjson((FIX / "golden_note_summary.ndjson").read_text())
    parented = attach_notes(diags)
    # warning(no note), error(1 note), summary error(no note)
    messages = [(p.message, [n.message for n in notes]) for p, notes in parented]
    assert messages == [
        ("'fn' is deprecated, use 'def' instead", []),
        ("invalid redefinition of 'x'", ["previous definition here"]),
        ("failed to parse the provided Mojo source module", []),
    ]


def test_two_notes_attach_to_same_parent():
    diags = parse_ndjson((FIX / "golden_two_notes.ndjson").read_text())
    parented = attach_notes(diags)
    last_parent = [p for p, _ in parented if "no matching function" in p.message][0]
    notes = [notes for p, notes in parented if p is last_parent][0]
    assert len(notes) == 2
    assert all("candidate not viable" in n.message for n in notes)


def test_leading_note_attaches_to_synthetic_summary():
    diags = parse_ndjson('{"kind":"note","message":"orphan"}')
    parented = attach_notes(diags)
    assert len(parented) == 1
    parent, notes = parented[0]
    assert parent.file is None and parent.is_summary
    assert [n.message for n in notes] == ["orphan"]
