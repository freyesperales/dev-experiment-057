"""The divergence engine: exact, interval-wise comparison of two tz databases.

Sampling is the obvious way to compare two zones -- walk a day at a time and
note where the offsets differ -- and it is wrong in the cases that matter. A
disagreement can be an hour wide (a DST transition moved by one week produces a
seven-day divergence, but a transition moved by one hour produces a one-hour
one), and a daily sample walks straight past it.

This module instead exploits a property of the data: between two consecutive
*breakpoints* of either database, both offsets are constant by construction. So
collecting the union of both zones' breakpoints yields a partition of the window
into segments on which a single comparison is exact. Adjacent segments with the
same verdict are then coalesced, which is what turns "17 one-hour disagreements"
back into "every winter from 2026-11-01 onwards".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Literal

from .rules import TTInfo, ZoneRules
from .timeutil import MAX_INSTANT, format_delta, format_instant, format_offset

ZoneVerdict = Literal[
    "identical",
    "abbrev-only",
    "historical-only",
    "future-divergence",
    "only-in-left",
    "only-in-right",
]


class Severity(IntEnum):
    """Ordered severity, so a CLI threshold is a simple comparison."""

    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def label(self) -> str:
        """Uppercase name used in reports."""
        return self.name

    @classmethod
    def parse(cls, text: str) -> "Severity":
        """Parse a case-insensitive severity name."""
        try:
            return cls[text.strip().upper()]
        except KeyError:
            allowed = ", ".join(level.name.lower() for level in cls)
            raise ValueError(f"unknown severity {text!r}; expected one of {allowed}") from None


@dataclass(frozen=True, slots=True)
class OffsetInterval:
    """A maximal run of time over which two databases disagree.

    `end` is None when the disagreement extends to the end of the window, which
    is the usual shape for a permanent rule change: it does not stop.
    """

    start: int
    end: int | None
    left: TTInfo
    right: TTInfo

    @property
    def delta(self) -> int:
        """Right offset minus left offset, in seconds."""
        return self.right.utoff - self.left.utoff

    @property
    def offsets_differ(self) -> bool:
        """True when the numeric UTC offsets, not just the labels, disagree."""
        return self.left.utoff != self.right.utoff

    def describe(self) -> str:
        """One-line rendering used in the text report."""
        span = f"{format_instant(self.start)} .. " + (
            format_instant(self.end) if self.end is not None else "onwards"
        )
        if self.offsets_differ:
            return (
                f"{span}  {self.left.describe()} vs {self.right.describe()}  "
                f"delta {format_delta(self.delta)}"
            )
        return f"{span}  label {self.left.abbrev} vs {self.right.abbrev} (same offset)"


@dataclass(frozen=True, slots=True)
class ZoneDivergence:
    """The full comparison of one zone across two databases."""

    name: str
    verdict: ZoneVerdict
    intervals: tuple[OffsetInterval, ...] = ()
    aliases: tuple[str, ...] = ()
    #: Footer TZ strings, which is where permanent rule changes actually live.
    left_footer: str | None = None
    right_footer: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def offset_intervals(self) -> tuple[OffsetInterval, ...]:
        """Only the intervals where numeric offsets disagree."""
        return tuple(i for i in self.intervals if i.offsets_differ)

    @property
    def first_divergence(self) -> int | None:
        """Start of the earliest offset disagreement, if any."""
        offsets = self.offset_intervals
        return offsets[0].start if offsets else None

    @property
    def max_abs_delta(self) -> int:
        """Largest absolute offset disagreement in seconds."""
        return max((abs(i.delta) for i in self.offset_intervals), default=0)

    @property
    def severity(self) -> Severity:
        """Operational severity of this zone's divergence.

        Future offset disagreement is CRITICAL because it silently corrupts rows
        written today for dates that have not happened yet, and nothing inside
        the database can detect it. A purely historical disagreement is LOW: it
        changes how old instants are *displayed*, which is visible and fixable.
        """
        if self.verdict == "future-divergence":
            return Severity.CRITICAL
        if self.verdict in ("only-in-left", "only-in-right"):
            return Severity.MEDIUM
        if self.verdict == "historical-only":
            return Severity.LOW
        if self.verdict == "abbrev-only":
            return Severity.INFO
        return Severity.INFO

    @property
    def footer_changed(self) -> bool:
        """True when the POSIX footer differs, i.e. the indefinite future moved."""
        return self.left_footer != self.right_footer


@dataclass(frozen=True, slots=True)
class TreeDivergence:
    """The comparison of two whole trees."""

    left_label: str
    right_label: str
    window_start: int
    window_end: int
    now: int
    zones: tuple[ZoneDivergence, ...]
    left_only: tuple[str, ...] = ()
    right_only: tuple[str, ...] = ()
    unreadable: tuple[tuple[str, str], ...] = ()

    @property
    def critical(self) -> tuple[ZoneDivergence, ...]:
        """Zones whose future answers disagree."""
        return tuple(z for z in self.zones if z.severity >= Severity.CRITICAL)

    @property
    def max_severity(self) -> Severity:
        """Highest severity across all zones."""
        return max((z.severity for z in self.zones), default=Severity.INFO)

    @property
    def first_divergence(self) -> tuple[int, str] | None:
        """Earliest future divergence across the tree, as ``(instant, zone)``."""
        candidates = [
            (z.first_divergence, z.name)
            for z in self.zones
            if z.first_divergence is not None and z.first_divergence >= self.now
        ]
        return min(candidates) if candidates else None

    def by_severity(self) -> tuple[ZoneDivergence, ...]:
        """Zones ordered worst first, then by how soon they break, then by name."""
        return tuple(
            sorted(
                self.zones,
                key=lambda z: (
                    -int(z.severity),
                    z.first_divergence if z.first_divergence is not None else MAX_INSTANT,
                    z.name,
                ),
            )
        )


def diff_zone(
    name: str,
    left: ZoneRules,
    right: ZoneRules,
    *,
    window_start: int,
    window_end: int,
    now: int,
    aliases: tuple[str, ...] = (),
) -> ZoneDivergence:
    """Compare one zone across two databases over ``[window_start, window_end)``."""
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")

    boundaries = sorted(
        {window_start}
        | set(left.breakpoints(window_start, window_end))
        | set(right.breakpoints(window_start, window_end))
    )
    boundaries.append(window_end)

    runs: list[OffsetInterval] = []
    for index in range(len(boundaries) - 1):
        segment_start = boundaries[index]
        segment_end = boundaries[index + 1]
        left_info = left.lookup(segment_start)
        right_info = right.lookup(segment_start)
        if left_info.key() == right_info.key():
            continue
        if (
            runs
            and runs[-1].end == segment_start
            and runs[-1].left.key() == left_info.key()
            and runs[-1].right.key() == right_info.key()
        ):
            previous = runs.pop()
            runs.append(
                OffsetInterval(
                    start=previous.start,
                    end=segment_end,
                    left=previous.left,
                    right=previous.right,
                )
            )
            continue
        runs.append(
            OffsetInterval(
                start=segment_start, end=segment_end, left=left_info, right=right_info
            )
        )

    intervals = tuple(
        OffsetInterval(
            start=run.start,
            end=None if run.end == window_end else run.end,
            left=run.left,
            right=run.right,
        )
        for run in runs
    )

    offset_runs = [i for i in intervals if i.offsets_differ]
    if not intervals:
        verdict: ZoneVerdict = "identical"
    elif not offset_runs:
        verdict = "abbrev-only"
    elif any((i.end is None or i.end > now) for i in offset_runs):
        verdict = "future-divergence"
    else:
        verdict = "historical-only"

    return ZoneDivergence(
        name=name,
        verdict=verdict,
        intervals=intervals,
        aliases=aliases,
        left_footer=left.posix.spec if left.posix else None,
        right_footer=right.posix.spec if right.posix else None,
        warnings=tuple(
            [f"left: {w}" for w in left.warnings] + [f"right: {w}" for w in right.warnings]
        ),
    )


def diff_trees(
    left_zones: dict[str, ZoneRules],
    right_zones: dict[str, ZoneRules],
    *,
    left_label: str,
    right_label: str,
    window_start: int,
    window_end: int,
    now: int,
    aliases: dict[str, list[str]] | None = None,
    collapse_aliases: bool = True,
    unreadable: tuple[tuple[str, str], ...] = (),
) -> TreeDivergence:
    """Compare every shared zone of two trees.

    Args:
        aliases: Canonical name to its other names, from `ZoneTree.alias_sets`.
        collapse_aliases: Report an alias group once, under its canonical name.
            Turning this off reports every name separately, which is what you
            want when checking whether links themselves changed.
    """
    alias_map = aliases or {}
    if collapse_aliases:
        canonical = set(alias_map) or set(left_zones)
        alias_lookup = alias_map
    else:
        canonical = set(left_zones) | set(right_zones)
        alias_lookup = {}

    shared = sorted((set(left_zones) & set(right_zones)) & canonical)
    results: list[ZoneDivergence] = []
    for name in shared:
        results.append(
            diff_zone(
                name,
                left_zones[name],
                right_zones[name],
                window_start=window_start,
                window_end=window_end,
                now=now,
                aliases=tuple(alias_lookup.get(name, ())),
            )
        )

    for name in sorted((set(left_zones) - set(right_zones)) & canonical):
        rules = left_zones[name]
        results.append(
            ZoneDivergence(
                name=name,
                verdict="only-in-left",
                aliases=tuple(alias_lookup.get(name, ())),
                left_footer=rules.posix.spec if rules.posix else None,
            )
        )
    for name in sorted((set(right_zones) - set(left_zones)) & canonical):
        rules = right_zones[name]
        results.append(
            ZoneDivergence(
                name=name,
                verdict="only-in-right",
                right_footer=rules.posix.spec if rules.posix else None,
            )
        )

    return TreeDivergence(
        left_label=left_label,
        right_label=right_label,
        window_start=window_start,
        window_end=window_end,
        now=now,
        zones=tuple(results),
        left_only=tuple(sorted(set(left_zones) - set(right_zones))),
        right_only=tuple(sorted(set(right_zones) - set(left_zones))),
        unreadable=unreadable,
    )


def summarise_footer_change(divergence: ZoneDivergence) -> str:
    """Explain a footer change in one line, since it drives permanent shifts."""
    left = divergence.left_footer or "(none)"
    right = divergence.right_footer or "(none)"
    if left == right:
        return f"footer unchanged: {left}"
    return f"footer {left} -> {right}"


def offset_summary(divergence: ZoneDivergence) -> str:
    """Summarise the offsets involved, for compact listings."""
    offsets = divergence.offset_intervals
    if not offsets:
        return "no offset change"
    deltas = sorted({i.delta for i in offsets})
    rendered = ", ".join(format_delta(d) for d in deltas)
    sample = offsets[0]
    return (
        f"{format_offset(sample.left.utoff)} -> {format_offset(sample.right.utoff)} "
        f"(delta {rendered})"
    )
