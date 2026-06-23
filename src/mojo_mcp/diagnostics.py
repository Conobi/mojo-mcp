"""Pure diagnostic compaction core (no I/O). See plans/2026-06-22 contract."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

HARD_CAP = 100_000
SOFT_BUDGET = 8192
CHROME_RESERVE = 1024

Key = tuple[str, str, "str | None"]


@dataclass(frozen=True)
class Diagnostic:
    kind: str
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    source_text: str | None = None
    fixits: tuple[str, ...] = ()
    ranges: tuple[tuple[int, int], ...] = ()
    is_summary: bool = False
    parse_error: str | None = None


@dataclass(frozen=True)
class Group:
    kind: str
    message: str
    file: str | None
    origin: str
    lines: tuple[int, ...]
    count: int
    fixits: tuple[str, ...] = ()
    notes: tuple["Group", ...] = ()
    is_synthetic: bool = False  # True for the note-attachment sentinel parent


@dataclass(frozen=True)
class LedgerEntry:
    first_build: int
    last_count: int
    last_lines: frozenset


@dataclass(frozen=True)
class CompactionResult:
    errors: tuple[Group, ...] = ()
    warnings: tuple[Group, ...] = ()
    suppressed: tuple[Group, ...] = ()
    parse_fallback: str | None = None
    truncated_warning_groups: int = 0
    truncated_error_groups: int = 0
    rendered: str = ""
    new_ledger_records: dict = field(default_factory=dict)
    suppress_keys: frozenset = field(default_factory=frozenset)


def parse_ndjson(ndjson: str) -> list[Diagnostic]:
    """Parse NDJSON compiler output into Diagnostic records.

    One Diagnostic per non-blank line. A line whose `diagnostic` body is
    absent is a summary diagnostic (file/line None, is_summary=True). A line
    that is not valid JSON becomes a parse_error Diagnostic so the stream is
    never aborted by one bad line.
    """
    out: list[Diagnostic] = []
    for line in ndjson.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            out.append(Diagnostic(kind="error", message=s[:500],
                                  is_summary=True, parse_error="malformed NDJSON line"))
            continue
        if not isinstance(obj, dict) or "kind" not in obj or "message" not in obj:
            out.append(Diagnostic(kind="error", message=s[:500],
                                  is_summary=True, parse_error="missing kind/message"))
            continue
        kind = str(obj["kind"])
        message = str(obj["message"])
        body = obj.get("diagnostic")
        if not isinstance(body, dict):
            out.append(Diagnostic(kind=kind, message=message, is_summary=True))
            continue
        loc = body.get("location") or {}
        ranges = tuple(
            (int(r["start"]), int(r["end"]))
            for r in body.get("ranges", []) if isinstance(r, dict) and "start" in r and "end" in r
        )
        fixits = tuple(
            str(f["text"]) for f in body.get("fixIts", [])
            if isinstance(f, dict) and "text" in f
        )
        out.append(Diagnostic(
            kind=kind,
            message=message,
            file=body.get("file"),
            line=loc.get("line"),
            column=loc.get("column"),
            source_text=body.get("text"),
            fixits=fixits,
            ranges=ranges,
            is_summary=False,
        ))
    return out


def attach_notes(diags: list[Diagnostic]) -> list[tuple[Diagnostic, list[Diagnostic]]]:
    """Attach each note to the immediately-preceding non-note diagnostic.

    Note association is positional and line-order-defined: a note attaches to
    the most recent emitted non-note. A leading note with no predecessor gets a
    synthetic file=None summary parent (always shown). Returns an ordered list
    of (parent, notes) preserving line order; never reorders a note relative to
    its textual predecessor.
    """
    parented: list[tuple[Diagnostic, list[Diagnostic]]] = []
    for d in diags:
        if d.kind == "note":
            if not parented:
                synthetic = Diagnostic(kind="error", message="(note without preceding diagnostic)",
                                       file=None, is_summary=True)
                parented.append((synthetic, [d]))
            else:
                parented[-1][1].append(d)
        else:
            parented.append((d, []))
    return parented


def classify_origin(file: str | None, project_roots: frozenset) -> str:
    """Classify a diagnostic file as project / dependency / non_suppressible.

    `project_roots` and `file` are both already realpath-normalized by the
    effectful shell (this stays pure). A file is project-origin iff it equals a
    root file or is contained in a root directory (path-segment containment, so
    `/projector` is NOT inside `/proj`). Summary diagnostics (file=None) are
    non_suppressible. cwd-containment is encoded by the shell supplying the cwd
    dir as a root; this function only does the containment test.
    """
    if file is None:
        return "non_suppressible"
    for root in project_roots:
        if file == root:
            return "project"
        rp = root.rstrip("/") + "/"
        if file.startswith(rp):
            return "project"
    return "dependency"


def group_diagnostics(
    parented: list[tuple[Diagnostic, list[Diagnostic]]],
    project_roots: frozenset,
) -> list[Group]:
    """Collapse (parent, notes) pairs into Groups keyed by (kind, message, file).

    Each Group records total count, sorted distinct lines, de-duplicated fixits,
    and its attached note-Groups. A note inherits its parent group's origin and
    is itself rendered as a one-line Group (count==1). Group order follows first
    appearance, preserving determinism via a stable insertion-ordered dict.
    """
    _SYNTHETIC_MSG = "(note without preceding diagnostic)"
    acc: dict[Key, dict] = {}
    order: list[Key] = []
    for parent, notes in parented:
        key = (parent.kind, parent.message, parent.file)
        is_synthetic = bool(
            getattr(parent, "is_summary", False) and parent.message == _SYNTHETIC_MSG
        )
        if key not in acc:
            acc[key] = {"lines": set(), "count": 0, "fixits": [], "file": parent.file,
                        "kind": parent.kind, "message": parent.message,
                        "origin": classify_origin(parent.file, project_roots), "notes": [],
                        "is_synthetic": is_synthetic}
            order.append(key)
        bucket = acc[key]
        bucket["count"] += 1
        if parent.line is not None:
            bucket["lines"].add(parent.line)
        for fx in parent.fixits:
            if fx not in bucket["fixits"]:
                bucket["fixits"].append(fx)
        parent_origin = bucket["origin"]
        for note in notes:
            bucket["notes"].append(Group(
                kind="note", message=note.message, file=note.file,
                origin=parent_origin if parent_origin != "non_suppressible" else "non_suppressible",
                lines=(note.line,) if note.line is not None else (),
                count=1, fixits=note.fixits, notes=(),
            ))
    groups: list[Group] = []
    for key in order:
        b = acc[key]
        groups.append(Group(
            kind=b["kind"], message=b["message"], file=b["file"], origin=b["origin"],
            lines=tuple(sorted(b["lines"])), count=b["count"],
            fixits=tuple(b["fixits"]), notes=tuple(b["notes"]),
            is_synthetic=b["is_synthetic"],
        ))
    return groups


_ORIGIN_RANK = {"non_suppressible": 0, "project": 1, "dependency": 2}


def order_groups(groups: list[Group]) -> tuple[list[Group], list[Group]]:
    """Split into (errors, warnings); each ordered origin-then-(file, line).

    Errors precede warnings (caller concatenates errors first). Within a kind:
    non_suppressible (summary) first, then project-origin, then dependency;
    within an origin, stable by (file or '', first line or -1). Notes travel
    inside their parent Group, so they are never independently ordered here.
    """
    def sort_key(g: Group):
        first_line = g.lines[0] if g.lines else -1
        return (_ORIGIN_RANK.get(g.origin, 3), g.file or "", first_line, g.message)

    errors = sorted([g for g in groups if g.kind == "error"], key=sort_key)
    warnings = sorted([g for g in groups if g.kind == "warning"], key=sort_key)
    return errors, warnings


def decide_suppression(
    warning_groups: list[Group],
    ledger: dict,
    build_ordinal: int,
) -> tuple[list[Group], list[Group], frozenset, dict]:
    """Decide which dependency-origin warning groups to cross-build suppress.

    Eligibility: kind=warning AND origin=dependency only. A group is suppressed
    iff its key is in the ledger AND its count and line-set both equal the
    ledgered values; otherwise it is re-shown in full. An evicted/never-seen key
    re-establishes first_build at the current ordinal (safe direction). Returns
    (shown, suppressed, suppress_keys, new_records). The core only decides; the
    shell commits new_records into the live ledger.
    """
    shown: list[Group] = []
    suppressed: list[Group] = []
    keys: set = set()
    records: dict = {}
    for g in warning_groups:
        key = (g.kind, g.message, g.file)
        if g.origin != "dependency":
            shown.append(g)
            continue
        entry = ledger.get(key)
        cur_lines = frozenset(g.lines)
        if entry is not None and entry.last_count == g.count and entry.last_lines == cur_lines:
            suppressed.append(g)
            keys.add(key)
            records[key] = entry  # unchanged: preserve original first_build
        else:
            shown.append(g)
            first_build = entry.first_build if entry is not None else build_ordinal
            # Spec: a changed group's first_build stays; an evicted key re-establishes it.
            records[key] = LedgerEntry(first_build=first_build, last_count=g.count,
                                       last_lines=cur_lines)
    return shown, suppressed, frozenset(keys), records


def _line_range(lines: tuple[int, ...]) -> str:
    """Render a sorted line tuple as a compact range string (1, 1-133, 1,4,9)."""
    if not lines:
        return ""
    if len(lines) == 1:
        return str(lines[0])
    contiguous = lines[-1] - lines[0] + 1 == len(lines)
    if contiguous:
        return f"{lines[0]}-{lines[-1]}"
    return ",".join(str(n) for n in lines)


def _render_one(g: Group, *, indent: str = "") -> str:
    """Render a single Group (and its notes) to a markdown string."""
    loc = g.file or "(summary)"
    rng = _line_range(g.lines)
    where = f"{loc}:{rng}" if rng else loc
    head = f"{indent}- {g.count}× '{g.message}' — {where}" if g.count != 1 \
        else f"{indent}- {g.message} — {where}"
    if g.fixits:
        head += f"  (fix-it: {', '.join(g.fixits)})"
    parts = [head]
    for note in g.notes:
        parts.append(_render_one(note, indent=indent + "  "))
    return "\n".join(parts)


def render_groups(groups: list[Group]) -> list[str]:
    """Render each group (with its notes) to a single markdown string.

    One string per group; the caller measures and budget-truncates the list.
    Notes are rendered indented immediately after their parent.
    """
    return [_render_one(g) for g in groups]


def expand(result: CompactionResult) -> list[Diagnostic]:
    """Lossless inverse: one representative Diagnostic per distinct tuple.

    Emits one Diagnostic per (kind, message, file, line) recoverable from the
    pre-budget grouped representation (errors, warnings, suppressed, and notes).
    Used by prop_compaction_lossless and prop_idempotent.
    """
    out: list[Diagnostic] = []
    seen: set = set()

    def emit(g: Group) -> None:
        # Synthetic groups (note-attachment sentinels) have no corresponding
        # input diagnostic — skip their own tuple but still recurse into notes.
        if not g.is_synthetic:
            line_set = g.lines or (None,)
            for ln in line_set:
                key = (g.kind, g.message, g.file, ln)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Diagnostic(kind=g.kind, message=g.message, file=g.file,
                                      line=ln, source_text=None, fixits=g.fixits))
        for note in g.notes:
            emit(note)

    for g in (*result.errors, *result.warnings, *result.suppressed):
        emit(g)
    return out


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def budget_truncate(
    error_renders: list[str],
    suppressed_renders: list[str],
    warning_renders: list[str],
    parse_fallback: str | None,
    *,
    hard_cap: int,
    soft_budget: int,
) -> tuple[str, int, int, str | None]:
    """Assemble the rendered region under the byte budget (errors-first).

    Order/accounting per the spec budget equation:
      1. errors fill up to (hard_cap - CHROME_RESERVE); never below the first;
         overflow ⇒ '+N more distinct error groups elided' marker (counted).
      2. suppressed one-liners bounded by chrome + (soft_budget - error_bytes);
         overflow ⇒ '+N more suppressed groups' marker.
      3. warnings fill max(0, soft_budget - error_bytes - suppressed_bytes);
         tail dropped ⇒ 'truncated_warning_groups: N' marker.
      4. parse_fallback (raw stderr) is truncated to fit whatever remains under
         hard_cap with a '+N KB of raw stderr elided' marker.
    Returns (rendered, truncated_warning_groups, truncated_error_groups,
    parse_fallback_text). The total rendered bytes are <= hard_cap on all paths.
    """
    parts: list[str] = []
    teg = 0
    twg = 0
    used = 0
    err_budget = hard_cap - CHROME_RESERVE

    # 1. errors — always at least the first
    shown_errors: list[str] = []
    for i, e in enumerate(error_renders):
        prospective = used + _bytes(e) + 1
        if shown_errors and prospective > err_budget:
            teg = len(error_renders) - i
            break
        shown_errors.append(e)
        used = prospective
    if teg:
        marker = f"- +{teg} more distinct error groups elided"
        shown_errors.append(marker)
        used += _bytes(marker) + 1
    if shown_errors:
        parts.append("## Errors\n" + "\n".join(shown_errors))
    error_bytes = used

    # 2. suppressed one-liners — bounded
    supp_cap = CHROME_RESERVE + max(0, soft_budget - error_bytes)
    shown_supp: list[str] = []
    supp_used = 0
    supp_truncated = 0
    for i, s in enumerate(suppressed_renders):
        if supp_used + _bytes(s) + 1 > supp_cap:
            supp_truncated = len(suppressed_renders) - i
            break
        shown_supp.append(s)
        supp_used += _bytes(s) + 1
    if supp_truncated:
        shown_supp.append(f"- +{supp_truncated} more suppressed groups")
    if shown_supp:
        parts.append("## Suppressed (unchanged since earlier build)\n" + "\n".join(shown_supp))
    used += supp_used

    # 3. warnings — soft budget remainder
    warn_cap = max(0, soft_budget - error_bytes - supp_used)
    shown_warn: list[str] = []
    warn_used = 0
    for i, w in enumerate(warning_renders):
        if warn_used + _bytes(w) + 1 > warn_cap:
            twg = len(warning_renders) - i
            break
        shown_warn.append(w)
        warn_used += _bytes(w) + 1
    if twg:
        shown_warn.append(f"- truncated_warning_groups: {twg}")
    if shown_warn:
        parts.append("## Warnings (compacted)\n" + "\n".join(shown_warn))
    used += warn_used

    # 4. parse_fallback — truncate raw stderr to remaining hard_cap room
    pf_out = None
    if parse_fallback is not None:
        room = hard_cap - used - 200  # reserve for the marker + heading
        if room < 0:
            room = 0
        encoded = parse_fallback.encode("utf-8")
        if len(encoded) > room:
            elided_kb = max(1, (len(encoded) - room) // 1024)
            pf_text = encoded[:room].decode("utf-8", errors="ignore")
            pf_out = pf_text + f"\n… +{elided_kb} KB of raw stderr elided"
        else:
            pf_out = parse_fallback
        parts.append("## Build failed (no parsed diagnostics)\n" + pf_out)

    rendered = "\n\n".join(parts)
    encoded = rendered.encode("utf-8")
    if len(encoded) > hard_cap:
        rendered = encoded[:hard_cap].decode("utf-8", errors="ignore")
    return rendered, twg, teg, pf_out


def _suppressed_render(g: Group, ledger: dict) -> str:
    """Render a one-liner for a cross-build suppressed warning group."""
    entry = ledger.get((g.kind, g.message, g.file))
    k = entry.first_build if entry else "?"
    rng = _line_range(g.lines)
    where = f"{g.file}:{rng}" if rng else (g.file or "")
    return f"- ({g.count}× '{g.message}' in {where} — unchanged since build #{k})"


def _raw_order(parented: list[tuple[Diagnostic, list[Diagnostic]]]) -> list[tuple[Diagnostic, list[Diagnostic]]]:
    """Stable partition: error diagnostics (with their notes) float to the top."""
    err_block, rest = [], []
    for parent, notes in parented:
        (err_block if parent.kind == "error" else rest).append((parent, notes))
    return err_block + rest


def compact_diagnostics(
    ndjson: str,
    *,
    project_roots: frozenset,
    returncode: int,
    raw_stderr: str,
    ledger: dict,
    build_ordinal: int,
    raw: bool = False,
    hard_cap: int = HARD_CAP,
    soft_budget: int = SOFT_BUDGET,
) -> CompactionResult:
    """Orchestrate the pure compaction pipeline (no I/O).

    Pipeline: parse → attach notes → (raw: float errors / normal: group+order) →
    suppression decision (skipped under raw) → render → measure →
    budget-truncate → concatenate. Enforces failure-coherence: returncode != 0
    with zero error groups surfaces raw_stderr as a non-suppressible
    parse_fallback. 'Clean stream AND exit 0' is the only success path.
    """
    diags = parse_ndjson(ndjson)
    parented = attach_notes(diags)

    if raw:
        # raw=True is "ungrouped": one Group per diagnostic — no merging of same
        # (kind, message, file) pairs. We produce each Group directly from the
        # parented pair, preserving error-first ordering (via _raw_order) and
        # keeping notes adjacent to their parent.
        ordered_parented = _raw_order(parented)
        raw_groups: list[Group] = []
        for parent, notes in ordered_parented:
            origin = classify_origin(parent.file, project_roots)
            note_groups = tuple(
                Group(
                    kind="note", message=n.message, file=n.file,
                    origin=origin if origin != "non_suppressible" else "non_suppressible",
                    lines=(n.line,) if n.line is not None else (),
                    count=1, fixits=n.fixits, notes=(),
                )
                for n in notes
            )
            raw_groups.append(Group(
                kind=parent.kind, message=parent.message, file=parent.file,
                origin=origin,
                lines=(parent.line,) if parent.line is not None else (),
                count=1, fixits=parent.fixits, notes=note_groups,
            ))
        errors = [g for g in raw_groups if g.kind == "error"]
        warnings = [g for g in raw_groups if g.kind == "warning"]
        shown_warn, suppressed, keys, records = warnings, [], frozenset(), {}
    else:
        groups = group_diagnostics(parented, project_roots)
        errors, warnings = order_groups(groups)
        shown_warn, suppressed, keys, records = decide_suppression(
            warnings, ledger, build_ordinal)
        # keep ordering of shown warnings stable (decide preserves input order)

    has_errors = bool(errors)
    parse_fallback = None
    if returncode != 0 and not has_errors:
        parse_fallback = raw_stderr or "(build failed: no diagnostics and no stderr)"

    error_renders = render_groups(errors)
    suppressed_renders = [] if raw else [_suppressed_render(g, ledger) for g in suppressed]
    warning_renders = render_groups(shown_warn)

    rendered, twg, teg, pf_out = budget_truncate(
        error_renders, suppressed_renders, warning_renders, parse_fallback,
        hard_cap=hard_cap, soft_budget=soft_budget)

    return CompactionResult(
        errors=tuple(errors),
        warnings=tuple(shown_warn),
        suppressed=tuple(suppressed),
        parse_fallback=pf_out,
        truncated_warning_groups=twg,
        truncated_error_groups=teg,
        rendered=rendered,
        new_ledger_records=records,
        suppress_keys=keys,
    )
