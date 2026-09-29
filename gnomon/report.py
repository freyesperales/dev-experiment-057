"""Rendering: a readable text report, and a machine-readable JSON one.

Both views are built from the same objects, and neither summarises away the
evidence. A report that says "17 zones differ" is useless for deciding what to
do; a report that names the first wrong instant, how many days away it is, and
which footer change caused it can be acted on without re-deriving anything.
"""

from __future__ import annotations

from typing import Any

from .blast import CLASS_NOTES, BlastReport
from .diverge import Severity, TreeDivergence, ZoneDivergence, offset_summary, summarise_footer_change
from .rules import ZoneRules
from .timeutil import SECONDS_PER_DAY, format_delta, format_instant, format_offset
from .tree import ZoneTree

_RULE = "=" * 78
_THIN = "-" * 78


def render_tree_diff(
    divergence: TreeDivergence, *, limit: int = 25, show_identical: bool = False
) -> str:
    """Render a tree comparison as text."""
    out: list[str] = [
        _RULE,
        "gnomon  tz database divergence",
        _RULE,
        f"  left  (assumed stale)  : {divergence.left_label}",
        f"  right (authoritative)  : {divergence.right_label}",
        f"  window                 : {format_instant(divergence.window_start)} .. "
        f"{format_instant(divergence.window_end)}",
        f"  'now'                  : {format_instant(divergence.now)}",
        "",
    ]

    headline = divergence.first_divergence
    if headline is not None:
        instant, zone = headline
        days = (instant - divergence.now) // SECONDS_PER_DAY
        when = f"in {days} day(s)" if days >= 0 else f"{-days} day(s) ago"
        out.append(f"  FIRST WRONG ANSWER     : {format_instant(instant)} ({when}) in {zone}")
    else:
        out.append("  FIRST WRONG ANSWER     : none inside the window")
    out.append(f"  worst severity         : {divergence.max_severity.label}")

    tally: dict[str, int] = {}
    for zone in divergence.zones:
        tally[zone.verdict] = tally.get(zone.verdict, 0) + 1
    out.append(
        "  verdicts               : "
        + (", ".join(f"{count} {name}" for name, count in sorted(tally.items())) or "none")
    )
    if divergence.unreadable:
        out.append(f"  unreadable zones       : {len(divergence.unreadable)}")
    out.append("")

    interesting = [
        zone
        for zone in divergence.by_severity()
        if show_identical or zone.verdict != "identical"
    ]
    if not interesting:
        out.append("No divergence: the two databases agree on every shared zone in the window.")
        out.append(_RULE)
        return "\n".join(out) + "\n"

    out.append(f"ZONES ({len(interesting)} with a difference, showing up to {limit})")
    out.append(_THIN)
    for zone in interesting[:limit]:
        out.extend(_render_zone(zone, divergence.now))
    if len(interesting) > limit:
        out.append(f"... {len(interesting) - limit} more; raise --limit or use --json to see all.")

    if divergence.unreadable:
        out.append("")
        out.append("UNREADABLE")
        out.append(_THIN)
        for name, problem in divergence.unreadable[:limit]:
            out.append(f"  {name}: {problem}")

    out.append("")
    out.append(_RULE)
    return "\n".join(out) + "\n"


def _render_zone(zone: ZoneDivergence, now: int) -> list[str]:
    """Render one zone's block."""
    header = f"[{zone.severity.label:8}] {zone.name}  ({zone.verdict})"
    lines = ["", header]
    if zone.aliases:
        lines.append(f"    aliases: {', '.join(zone.aliases)}")
    if zone.verdict in ("only-in-left", "only-in-right"):
        lines.append("    present in only one database; conversions for it are not comparable")
        return lines
    if zone.footer_changed:
        lines.append(f"    {summarise_footer_change(zone)}")
    if zone.offset_intervals:
        lines.append(f"    {offset_summary(zone)}")
    future = [i for i in zone.intervals if i.end is None or i.end > now]
    past = [i for i in zone.intervals if not (i.end is None or i.end > now)]
    for interval in future[:4]:
        lines.append(f"    future  {interval.describe()}")
    if len(future) > 4:
        lines.append(f"    future  ... {len(future) - 4} more interval(s)")
    for interval in past[:2]:
        lines.append(f"    past    {interval.describe()}")
    if len(past) > 2:
        lines.append(f"    past    ... {len(past) - 2} more interval(s)")
    for warning in zone.warnings[:3]:
        lines.append(f"    note: {warning}")
    return lines


def tree_diff_json(divergence: TreeDivergence) -> dict[str, Any]:
    """Serialise a tree comparison."""
    headline = divergence.first_divergence
    return {
        "tool": "gnomon",
        "kind": "tree-diff",
        "left": divergence.left_label,
        "right": divergence.right_label,
        "window": {
            "start": divergence.window_start,
            "end": divergence.window_end,
            "start_utc": format_instant(divergence.window_start),
            "end_utc": format_instant(divergence.window_end),
        },
        "now": divergence.now,
        "max_severity": divergence.max_severity.label,
        "first_divergence": (
            {"instant": headline[0], "utc": format_instant(headline[0]), "zone": headline[1]}
            if headline
            else None
        ),
        "counts": {
            "zones_compared": len(divergence.zones),
            "critical": len(divergence.critical),
            "left_only": len(divergence.left_only),
            "right_only": len(divergence.right_only),
            "unreadable": len(divergence.unreadable),
        },
        "zones": [_zone_json(zone) for zone in divergence.by_severity()],
        "left_only": list(divergence.left_only),
        "right_only": list(divergence.right_only),
        "unreadable": [{"zone": name, "error": problem} for name, problem in divergence.unreadable],
    }


def _zone_json(zone: ZoneDivergence) -> dict[str, Any]:
    """Serialise one zone comparison."""
    return {
        "zone": zone.name,
        "verdict": zone.verdict,
        "severity": zone.severity.label,
        "aliases": list(zone.aliases),
        "footer": {
            "left": zone.left_footer,
            "right": zone.right_footer,
            "changed": zone.footer_changed,
        },
        "first_divergence": zone.first_divergence,
        "first_divergence_utc": (
            format_instant(zone.first_divergence) if zone.first_divergence is not None else None
        ),
        "max_abs_delta_seconds": zone.max_abs_delta,
        "intervals": [
            {
                "start": interval.start,
                "start_utc": format_instant(interval.start),
                "end": interval.end,
                "end_utc": format_instant(interval.end) if interval.end is not None else None,
                "left": {
                    "utoff": interval.left.utoff,
                    "abbrev": interval.left.abbrev,
                    "isdst": interval.left.isdst,
                },
                "right": {
                    "utoff": interval.right.utoff,
                    "abbrev": interval.right.abbrev,
                    "isdst": interval.right.isdst,
                },
                "delta_seconds": interval.delta,
                "offsets_differ": interval.offsets_differ,
            }
            for interval in zone.intervals
        ],
        "warnings": list(zone.warnings),
    }


def render_blast(report: BlastReport, *, limit: int = 40) -> str:
    """Render a blast-radius analysis as text."""
    out: list[str] = [
        _RULE,
        "gnomon  blast radius over supplied wall-clock readings",
        _RULE,
        f"  stored through (stale) : {report.left_label}",
        f"  authoritative          : {report.right_label}",
        f"  rows analysed          : {len(report.findings)}",
        f"  worst severity         : {report.max_severity.label}",
        "",
    ]
    counts = report.counts()
    out.append("CLASSIFICATION")
    out.append(_THIN)
    for name, count in counts.items():
        out.append(f"  {count:6d}  {name}")
        out.append(f"          {CLASS_NOTES[name]}")
    if report.unknown_zones:
        out.append("")
        out.append(f"  zones unknown to both databases: {', '.join(report.unknown_zones)}")

    affected = report.affected
    out.append("")
    if not affected:
        out.append("Every row resolves identically under both databases. Nothing to repair.")
        out.append(_RULE)
        return "\n".join(out) + "\n"

    out.append(f"AFFECTED ROWS ({len(affected)}, worst first, showing up to {limit})")
    out.append(_THIN)
    for finding in affected[:limit]:
        out.append(f"  {finding.describe()}")
    if len(affected) > limit:
        out.append(f"  ... {len(affected) - limit} more; raise --limit or use --json.")

    review = report.needs_human_review()
    if review:
        out.append("")
        out.append(f"NEEDS A HUMAN, NOT ARITHMETIC ({len(review)})")
        out.append(_THIN)
        out.append("  These readings became ambiguous, stopped existing, or name a zone one")
        out.append("  database does not have. A constant correction can make them match what")
        out.append("  the fixed server computes, but the original intent is not recoverable")
        out.append("  from the stored data -- confirm them with whoever entered them.")
        for finding in review:
            out.append(
                f"    {finding.row.row_id}  {finding.row.render()}  {finding.row.zone}  "
                f"({finding.classification})"
            )

    grouped = report.corrections_by_delta()
    if grouped:
        out.append("")
        out.append("CORRECTIONS NEEDED")
        out.append(_THIN)
        for delta, findings in grouped.items():
            out.append(f"  {len(findings):6d} row(s)  {format_delta(delta)}  ({delta:+d} seconds)")
        out.append("")
        out.append("  Emit SQL for these with: gnomon blast ... --sql out.sql --table <t>")
        out.append("  Repairing rows does not fix the cause. Rebuild or repoint the server")
        out.append("  at current tzdata, or the next write reintroduces the same error.")
    out.append("")
    out.append(_RULE)
    return "\n".join(out) + "\n"


def blast_json(report: BlastReport) -> dict[str, Any]:
    """Serialise a blast-radius analysis."""
    return {
        "tool": "gnomon",
        "kind": "blast",
        "left": report.left_label,
        "right": report.right_label,
        "now": report.now,
        "max_severity": report.max_severity.label,
        "counts": report.counts(),
        "unknown_zones": list(report.unknown_zones),
        "corrections": [
            {"delta_seconds": delta, "rows": [f.row.row_id for f in findings]}
            for delta, findings in report.corrections_by_delta().items()
        ],
        "rows": [
            {
                "id": finding.row.row_id,
                "line": finding.row.line,
                "zone": finding.row.zone,
                "wall": finding.row.render(),
                "classification": finding.classification,
                "severity": finding.severity.label,
                "is_future": finding.is_future,
                "delta_seconds": finding.delta,
                "left": _resolution_json(finding.left),
                "right": _resolution_json(finding.right),
                "detail": finding.detail,
            }
            for finding in report.findings
        ],
    }


def _resolution_json(resolution: Any) -> dict[str, Any] | None:
    """Serialise a `WallResolution`."""
    if resolution is None:
        return None
    return {
        "multiplicity": resolution.multiplicity,
        "instants": list(resolution.instants),
        "chosen": resolution.chosen,
        "chosen_utc": format_instant(resolution.chosen),
        "utoff": resolution.chosen_utoff,
        "abbrev": resolution.chosen_abbrev,
        "gap_shift": resolution.gap_shift,
    }


def render_resolve(
    zone: str,
    wall: str,
    left_label: str,
    right_label: str,
    left: ZoneRules | None,
    right: ZoneRules | None,
    naive_instant: int,
) -> str:
    """Render a single wall-clock conversion under both databases side by side."""
    out = [
        _RULE,
        f"gnomon  resolve  {zone}  {wall}",
        _RULE,
    ]
    for label, rules in ((left_label, left), (right_label, right)):
        out.append("")
        out.append(f"{label}")
        if rules is None:
            out.append("  zone absent from this database")
            continue
        resolution = rules.resolve_wall(naive_instant)
        out.append(f"  multiplicity : {resolution.multiplicity}")
        if resolution.instants:
            out.append(
                "  candidates   : "
                + ", ".join(format_instant(i) for i in resolution.instants)
            )
        else:
            out.append(
                f"  candidates   : none; the reading falls in a "
                f"{format_delta(resolution.gap_shift)} gap"
            )
        out.append(
            f"  resolves to  : {format_instant(resolution.chosen)}  "
            f"({format_offset(resolution.chosen_utoff)} {resolution.chosen_abbrev})"
        )
        out.append(f"  footer       : {rules.posix.spec if rules.posix else '(none)'}")

    if left is not None and right is not None:
        left_res = left.resolve_wall(naive_instant)
        right_res = right.resolve_wall(naive_instant)
        delta = right_res.chosen - left_res.chosen
        out.append("")
        out.append(_THIN)
        if delta == 0 and left_res.multiplicity == right_res.multiplicity:
            out.append("  VERDICT: the two databases agree on this reading.")
        else:
            out.append(
                f"  VERDICT: they disagree. Correction to a stored value: "
                f"{format_delta(delta)} ({delta:+d} seconds)."
            )
            if left_res.multiplicity != right_res.multiplicity:
                out.append(
                    f"  Multiplicity also changed: {left_res.multiplicity} -> "
                    f"{right_res.multiplicity}."
                )
    out.append(_RULE)
    return "\n".join(out) + "\n"


def render_zones(tree: ZoneTree, *, pattern: str | None = None, limit: int = 60) -> str:
    """List the zones discovered in one tree."""
    names = sorted(tree.zones)
    if pattern:
        needle = pattern.lower()
        names = [name for name in names if needle in name.lower()]
    aliases = tree.alias_sets()
    out = [
        _RULE,
        f"gnomon  zones in {tree.describe}",
        _RULE,
        f"  zones found     : {len(tree.zones)}",
        f"  alias groups    : {len(aliases)}",
        f"  unreadable      : {len(tree.unreadable)}",
        f"  non-TZif skipped: {len(tree.skipped)}",
        "",
    ]
    if pattern:
        out.append(f"  filter {pattern!r} matched {len(names)} zone(s)")
        out.append("")
    for name in names[:limit]:
        rules = tree.zones[name]
        footer = rules.posix.spec if rules.posix else "(no footer)"
        out.append(
            f"  {name:34} v{rules.version}  "
            f"{len(rules.transitions):5d} transitions  {footer}"
        )
    if len(names) > limit:
        out.append(f"  ... {len(names) - limit} more; raise --limit or narrow with --grep.")
    if tree.unreadable:
        out.append("")
        out.append("UNREADABLE")
        out.append(_THIN)
        for name, problem in sorted(tree.unreadable.items())[:limit]:
            out.append(f"  {name}: {problem}")
    out.append(_RULE)
    return "\n".join(out) + "\n"
