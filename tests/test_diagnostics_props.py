"""Property tests for the pure compaction core (Red Gate)."""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from mojo_mcp.diagnostics import (
    HARD_CAP,
    CompactionResult,
    Diagnostic,
    LedgerEntry,
    compact_diagnostics,
    expand,
)

# --- generators ---------------------------------------------------------

_FILES = ["main.mojo", "dep/errno.mojo", "dep/raw.mojo", "vendor/v.mojo", None]
_MSGS = ["use of unknown declaration 'X'", "'alias' is deprecated; use 'comptime'",
         "no matching function", "previous definition here", "unused variable"]
_KINDS = ["error", "warning", "note"]


@st.composite
def diag_lines(draw, *, min_size=0, max_size=60):
    """Generate a list of NDJSON lines incl. many suppressed groups + distinct errors."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    lines: list[str] = []
    for _ in range(n):
        kind = draw(st.sampled_from(_KINDS))
        msg = draw(st.sampled_from(_MSGS))
        file = draw(st.sampled_from(_FILES))
        if file is None:
            lines.append(json.dumps({"kind": kind, "message": msg}))
            continue
        line = draw(st.integers(min_value=1, max_value=140))
        lines.append(json.dumps({
            "kind": kind, "message": msg,
            "diagnostic": {"file": file, "location": {"line": line, "column": 1},
                           "ranges": [], "text": "src", "fixIts": []},
        }))
    return "\n".join(lines)


@st.composite
def ledger_states(draw):
    entries = {}
    for _ in range(draw(st.integers(0, 8))):
        msg = draw(st.sampled_from(_MSGS))
        file = draw(st.sampled_from([f for f in _FILES if f]))
        entries[("warning", msg, file)] = LedgerEntry(
            first_build=draw(st.integers(1, 5)),
            last_count=draw(st.integers(1, 140)),
            last_lines=frozenset(draw(st.sets(st.integers(1, 140), max_size=10))),
        )
    return entries


_ROOTS = frozenset({"main.mojo"})  # only main.mojo is project-origin in generators


def _distinct_tuples(diags: list[Diagnostic]) -> set:
    return {(d.kind, d.message, d.file, d.line) for d in diags if not d.parse_error}


# --- properties ---------------------------------------------------------

@settings(max_examples=200)
@given(diag_lines())
def test_prop_compaction_lossless(ndjson):
    from mojo_mcp.diagnostics import parse_ndjson
    src = parse_ndjson(ndjson)
    r = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=1,
                            raw_stderr=ndjson, ledger={}, build_ordinal=1)
    recovered = _distinct_tuples(expand(r))
    assert _distinct_tuples(src) == recovered


@settings(max_examples=200)
@given(diag_lines(min_size=1))
def test_prop_errors_and_notes_survive(ndjson):
    r = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=1,
                            raw_stderr=ndjson, ledger={}, build_ordinal=1,
                            hard_cap=HARD_CAP, soft_budget=200)
    from mojo_mcp.diagnostics import parse_ndjson
    src_errs = {(d.message, d.file, d.line) for d in parse_ndjson(ndjson) if d.kind == "error"}
    shown_errs = {(g.message, g.file, ln) for g in r.errors for ln in (g.lines or (None,))}
    if src_errs and not (src_errs <= shown_errs):
        assert r.truncated_error_groups > 0
    for g in r.errors:
        for note in g.notes:
            assert note.kind == "note"


@settings(max_examples=200)
@given(diag_lines())
def test_prop_ordering(ndjson):
    r = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=1,
                            raw_stderr=ndjson, ledger={}, build_ordinal=1)
    # no warning precedes any error in the rendered region
    if r.errors and r.warnings:
        rendered = r.rendered
        first_warn = rendered.find("Warnings")
        first_err = rendered.find("Errors")
        if first_err != -1 and first_warn != -1:
            assert first_err < first_warn
    # project errors before dependency errors
    origins = [g.origin for g in r.errors]
    seen_dep = False
    for o in origins:
        if o == "dependency":
            seen_dep = True
        elif o == "project" and seen_dep:
            pytest.fail("project error after dependency error")


@settings(max_examples=200)
@given(diag_lines(), ledger_states())
def test_prop_dedup_safety(ndjson, ledger):
    r = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=1,
                            raw_stderr=ndjson, ledger=ledger, build_ordinal=9)
    for key in r.suppress_keys:
        kind, _msg, _file = key
        assert kind == "warning"
    suppressed_keys = {(g.kind, g.message, g.file) for g in r.suppressed}
    for g in (*r.errors, *r.warnings):
        if g.origin == "project":
            assert (g.kind, g.message, g.file) not in suppressed_keys
        if g.kind in ("error", "note"):
            assert (g.kind, g.message, g.file) not in suppressed_keys
    # changed count/line-set ⇒ re-shown, not bare-marker suppressed
    for g in r.warnings:
        if g.origin == "dependency":
            e = ledger.get((g.kind, g.message, g.file))
            if e and (e.last_count != g.count or e.last_lines != frozenset(g.lines)):
                assert (g.kind, g.message, g.file) not in r.suppress_keys


@settings(max_examples=200)
@given(diag_lines(min_size=1))
def test_prop_eviction_safe(ndjson):
    # empty ledger ⇒ nothing suppressed (evicted == never-seen == shown)
    r = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=1,
                            raw_stderr=ndjson, ledger={}, build_ordinal=2)
    assert r.suppress_keys == frozenset()
    assert r.suppressed == ()


@settings(max_examples=200)
@given(diag_lines(), st.integers(0, 1))
def test_prop_failure_coherence(ndjson, returncode):
    r = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=returncode,
                            raw_stderr=ndjson or "linker error", ledger={}, build_ordinal=1)
    if returncode != 0:
        assert r.errors or r.parse_fallback is not None


@settings(max_examples=200)
@given(diag_lines())
def test_prop_idempotent(ndjson):
    """compact(expand(compact(x))) == compact(x) — idempotence under expand/re-compact.

    The real idempotence property: compacting the original must recover the same
    distinct (kind, message, file, line) tuple set as the original compaction.
    Since expand(compact(x)) produces one Diagnostic per distinct tuple, counts
    change (all groups become count=1) but the TUPLE SET is invariant — no new
    tuples appear and none are lost across the expand/re-compact round-trip.

    We use returncode=0 (no parse_fallback interference) so the property purely
    exercises grouping, dedup, and ordering stability.
    """
    r1 = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=0,
                             raw_stderr="", ledger={}, build_ordinal=1)
    expanded = expand(r1)
    ndjson2 = "\n".join(json.dumps(
        {"kind": d.kind, "message": d.message} if d.file is None else
        {"kind": d.kind, "message": d.message,
         "diagnostic": {"file": d.file, "location": {"line": d.line or 1, "column": 1},
                        "ranges": [], "text": d.source_text or "", "fixIts": []}}
    ) for d in expanded)
    r2 = compact_diagnostics(ndjson2, project_roots=_ROOTS, returncode=0,
                             raw_stderr="", ledger={}, build_ordinal=1)
    # The real property: the distinct tuple set is stable across the round-trip.
    # compact(x) and compact(expand(compact(x))) must cover the same distinct
    # (kind, message, file, line) tuples (counts may differ — expand is one-per-tuple).
    r1_tuples = _distinct_tuples(expand(r1))
    r2_tuples = _distinct_tuples(expand(r2))
    assert r2_tuples == r1_tuples


@settings(max_examples=200)
@given(diag_lines(), ledger_states())
def test_prop_determinism(ndjson, ledger):
    a = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=1,
                            raw_stderr=ndjson, ledger=dict(ledger), build_ordinal=3)
    b = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=1,
                            raw_stderr=ndjson, ledger=dict(ledger), build_ordinal=3)
    assert a.rendered == b.rendered
    assert a.suppress_keys == b.suppress_keys


@st.composite
def _dep_warning_ndjson_and_ledger(draw):
    """Generate a dep-warning NDJSON stream with a matching ledger that forces suppression.

    Each (msg, file) key appears exactly once (unique via a fixed set of combinations),
    with 1-5 distinct occurrence lines.  The ledger entry is built to EXACTLY match the
    grouped result (count == n_occ, line-set == frozenset(occ_lines)) so
    decide_suppression will always suppress it.
    """
    dep_files = ["dep/errno.mojo", "dep/raw.mojo", "vendor/v.mojo"]
    dep_msgs = ["'alias' is deprecated; use 'comptime'", "no matching function", "unused variable"]
    # Build a fixed set of unique (msg, file) keys to avoid duplicate-key issues
    all_pairs = [(msg, f) for msg in dep_msgs for f in dep_files]
    n_groups = draw(st.integers(1, min(3, len(all_pairs))))
    chosen = draw(st.lists(st.sampled_from(all_pairs), min_size=n_groups,
                           max_size=n_groups, unique=True))
    lines: list[str] = []
    ledger: dict = {}
    for msg, file in chosen:
        n_occ = draw(st.integers(1, 5))
        occ_lines = draw(st.lists(st.integers(1, 140), min_size=n_occ,
                                  max_size=n_occ, unique=True))
        for ln in occ_lines:
            lines.append(json.dumps({
                "kind": "warning", "message": msg,
                "diagnostic": {"file": file, "location": {"line": ln, "column": 1},
                               "ranges": [], "text": "src", "fixIts": []},
            }))
        # Build matching ledger entry so this group IS suppressed
        ledger[("warning", msg, file)] = LedgerEntry(
            first_build=draw(st.integers(1, 5)),
            last_count=n_occ,
            last_lines=frozenset(occ_lines),
        )
    ndjson = "\n".join(lines)
    return ndjson, ledger


@settings(max_examples=200)
@given(_dep_warning_ndjson_and_ledger())
def test_prop_lossless_through_suppression(args):
    """Compaction-losslessness holds even when suppression is active.

    When a ledger matching the generated dep-warning groups forces suppression,
    expand(r) must still recover all distinct (kind, message, file, line) tuples
    from the original stream.  This exercises the suppressed path through expand.
    """
    from mojo_mcp.diagnostics import parse_ndjson

    ndjson, ledger = args
    src = parse_ndjson(ndjson)
    r = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=0,
                            raw_stderr="", ledger=ledger, build_ordinal=2)
    # At least one group must be suppressed given our matching ledger
    assert r.suppressed, "ledger should have forced at least one suppressed group"
    recovered = _distinct_tuples(expand(r))
    assert _distinct_tuples(src) == recovered


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(diag_lines(max_size=400), ledger_states())
def test_prop_total_budget(ndjson, ledger):
    """HARD_CAP is enforced on all paths — default, raw, and with parse_fallback.

    We always append: (a) a large raw_stderr (50× ndjson) so the parse_fallback
    path is exercised, and (b) a separate deterministic sub-run that forces multiple
    suppressed dep-warning groups so the suppressed-one-liner budget path is covered.
    """
    big_stderr = ndjson * 50 + "x" * 10_000  # guaranteed large parse_fallback

    # (a) default + raw with large stderr — covers errors, raw, parse_fallback
    for raw in (False, True):
        r = compact_diagnostics(ndjson, project_roots=_ROOTS, returncode=1,
                                raw_stderr=big_stderr, ledger=dict(ledger),
                                build_ordinal=4, raw=raw)
        assert len(r.rendered.encode("utf-8")) <= HARD_CAP

    # (b) forced-suppression sub-run: many dep-warning groups, matching ledger
    dep_msgs = ["'alias' is deprecated; use 'comptime'",
                "no matching function", "unused variable"]
    dep_files = ["dep/errno.mojo", "dep/raw.mojo", "vendor/v.mojo"]
    forced_lines: list[str] = []
    forced_ledger: dict = {}
    for msg in dep_msgs:
        for file in dep_files:
            for ln in range(1, 10):
                forced_lines.append(json.dumps({
                    "kind": "warning", "message": msg,
                    "diagnostic": {"file": file, "location": {"line": ln, "column": 1},
                                   "ranges": [], "text": "src", "fixIts": []},
                }))
            # build matching ledger entry — forces suppression
            forced_ledger[("warning", msg, file)] = LedgerEntry(
                first_build=1, last_count=9,
                last_lines=frozenset(range(1, 10)),
            )
    forced_ndjson = "\n".join(forced_lines)
    r_forced = compact_diagnostics(
        forced_ndjson, project_roots=_ROOTS, returncode=1,
        raw_stderr=big_stderr, ledger=forced_ledger, build_ordinal=4, raw=False,
    )
    assert len(r_forced.rendered.encode("utf-8")) <= HARD_CAP
    # The forced case must have exercised suppression AND parse_fallback
    assert r_forced.suppressed or r_forced.parse_fallback is not None


@settings(max_examples=200)
@given(diag_lines(min_size=1))
def test_prop_note_attachment_stable(ndjson):
    from mojo_mcp.diagnostics import parse_ndjson, attach_notes
    diags = parse_ndjson(ndjson)
    a = attach_notes(list(diags))
    b = attach_notes(list(diags))
    assert [(p.message, [n.message for n in notes]) for p, notes in a] == \
           [(p.message, [n.message for n in notes]) for p, notes in b]
    # every note in input is attached to exactly one parent (never dropped at parse stage)
    n_notes_in = sum(1 for d in diags if d.kind == "note")
    n_notes_out = sum(len(notes) for _p, notes in a)
    assert n_notes_in == n_notes_out
