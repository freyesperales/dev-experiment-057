"""Queryable zone rules: the forward map, and -- the point of gnomon -- the inverse.

The forward map (instant -> UTC offset) is what every timezone library exposes.
The *inverse* map (local wall clock -> instant) is where the interesting damage
lives, because it is not a function: a wall-clock reading can denote zero
instants (it fell in a spring-forward gap), exactly one, or two (it fell in an
autumn fold). Postgres's ``(date + time) AT TIME ZONE tz`` is precisely this
inverse map, and it is the documented way to turn user-entered local time into a
stored instant. When two tz databases disagree, they disagree about the inverse
map -- including about how many instants a wall clock even has -- and that
disagreement is written into the row and never revisited.

So `ZoneRules` exposes both directions, and `resolve_wall` classifies the
multiplicity explicitly rather than quietly picking a winner the way the
underlying databases do.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Literal, Sequence

from .posixtz import PosixTz
from .timeutil import MAX_INSTANT, MIN_INSTANT, format_offset

#: How far either side of a candidate instant we look for relevant offsets.
#: No real zone has ever had a UTC offset outside +/-26h, and DST rule times in
#: TZif v3 may push a transition up to a week away, so 8 days is ample.
_INVERSE_PROBE_WINDOW = 8 * 86_400

Multiplicity = Literal["unique", "gap", "fold"]


@dataclass(frozen=True, slots=True)
class TTInfo:
    """One local-time type: a UTC offset with its abbreviation and DST flag."""

    utoff: int
    isdst: bool
    abbrev: str

    def describe(self) -> str:
        """Render as ``PST -08:00`` / ``PDT -07:00 (dst)``."""
        suffix = " (dst)" if self.isdst else ""
        return f"{self.abbrev} {format_offset(self.utoff)}{suffix}"

    def key(self) -> tuple[int, bool, str]:
        """Comparable identity used when diffing two databases."""
        return (self.utoff, self.isdst, self.abbrev)


@dataclass(frozen=True, slots=True)
class Transition:
    """An explicit transition record: at `instant`, local-time type changes."""

    instant: int
    type_index: int


@dataclass(frozen=True, slots=True)
class LeapSecond:
    """A leap-second record from the TZif leap table."""

    occurrence: int
    correction: int


@dataclass(frozen=True, slots=True)
class WallResolution:
    """The result of mapping one wall-clock reading through one database.

    `instants` holds every Unix instant that denotes the given local reading:
    empty for a gap, one for the ordinary case, two for a fold. `chosen` is the
    single instant a typical implementation would store -- the earliest
    candidate for a fold, and for a gap the reading shifted forward by the size
    of the gap, which is what Postgres does.
    """

    instants: tuple[int, ...]
    multiplicity: Multiplicity
    chosen: int
    #: Offset applied to produce `chosen`.
    chosen_utoff: int
    chosen_abbrev: str
    #: For a gap, how far `chosen` was pushed past the requested reading.
    gap_shift: int = 0


@dataclass(slots=True)
class ZoneRules:
    """Everything gnomon needs to know about one zone in one database."""

    name: str
    types: tuple[TTInfo, ...]
    transitions: tuple[Transition, ...]
    posix: PosixTz | None = None
    leap_seconds: tuple[LeapSecond, ...] = ()
    version: int = 1
    #: Origin of the bytes, for reports.
    source: str = ""
    #: Non-fatal oddities noticed while parsing or cross-checking.
    warnings: tuple[str, ...] = ()
    _instants: list[int] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not self.types:
            from .errors import TzifParseError

            raise TzifParseError(
                f"zone {self.name!r} declares no local-time types", path=self.source
            )
        self._instants = [t.instant for t in self.transitions]

    # ---- forward map -----------------------------------------------------

    @property
    def default_type(self) -> TTInfo:
        """Type in force before the first transition.

        RFC 8536 section 4 prescribes the first non-DST type, falling back to
        the first type when every type is a DST type.
        """
        for candidate in self.types:
            if not candidate.isdst:
                return candidate
        return self.types[0]

    @property
    def last_explicit_instant(self) -> int | None:
        """Instant of the final explicit transition, if any."""
        return self._instants[-1] if self._instants else None

    def lookup(self, instant: int) -> TTInfo:
        """Return the local-time type in force at `instant`."""
        if self.posix is not None and self._posix_governs(instant):
            utoff, abbrev, isdst = self.posix.utoff_and_abbrev(instant)
            return TTInfo(utoff=utoff, isdst=isdst, abbrev=abbrev)
        if not self._instants or instant < self._instants[0]:
            return self.default_type
        index = bisect.bisect_right(self._instants, instant) - 1
        return self.types[self.transitions[index].type_index]

    def _posix_governs(self, instant: int) -> bool:
        """True when the footer, not the transition table, describes `instant`."""
        if not self._instants:
            return True
        return instant >= self._instants[-1]

    # ---- breakpoints, for interval-wise diffing --------------------------

    def breakpoints(self, start_instant: int, end_instant: int) -> list[int]:
        """Every instant in ``[start, end)`` where this zone's offset may change.

        Combines explicit transitions with the transitions generated by the
        POSIX footer beyond the transition table. Between two consecutive
        breakpoints the zone's offset is constant by construction, which is what
        makes the divergence scan in `diverge` exact rather than sampled.
        """
        found = [i for i in self._instants if start_instant <= i < end_instant]
        if self.posix is not None:
            governed_from = self._instants[-1] if self._instants else MIN_INSTANT
            posix_start = max(start_instant, governed_from)
            if posix_start < end_instant:
                found.extend(self.posix.transitions_in_range(posix_start, end_instant))
        return sorted(set(found))

    # ---- inverse map: the part that matters ------------------------------

    def wall_to_instants(self, naive_instant: int) -> list[int]:
        """Map a wall-clock reading to every instant that denotes it.

        `naive_instant` is the reading interpreted *as if it were UTC*, which is
        just a compact way to carry ``(year, month, day, second-of-day)`` as one
        integer. Returns 0, 1 or 2 instants, ascending.
        """
        candidates: set[int] = set()
        for utoff in self._nearby_offsets(naive_instant):
            instant = naive_instant - utoff
            if not MIN_INSTANT <= instant <= MAX_INSTANT:
                continue
            if self.lookup(instant).utoff == utoff:
                candidates.add(instant)
        return sorted(candidates)

    def _nearby_offsets(self, naive_instant: int) -> set[int]:
        """Collect every UTC offset plausibly in force around a reading."""
        low = max(naive_instant - _INVERSE_PROBE_WINDOW, MIN_INSTANT)
        high = min(naive_instant + _INVERSE_PROBE_WINDOW, MAX_INSTANT)
        offsets = {self.lookup(low).utoff, self.lookup(high).utoff}
        for moment in self.breakpoints(low, high):
            offsets.add(self.lookup(moment).utoff)
            if moment - 1 >= MIN_INSTANT:
                offsets.add(self.lookup(moment - 1).utoff)
        return offsets

    def resolve_wall(self, naive_instant: int) -> WallResolution:
        """Map a wall-clock reading to the single instant an app would store.

        Gap and fold policy mirrors what a caller actually gets in practice:

        * fold -- the earliest candidate wins, matching Postgres and the
          ``fold=0`` default of Python's own ``datetime``;
        * gap -- the reading is pushed forward by the width of the gap, which is
          what Postgres's ``AT TIME ZONE`` does. It does not raise; that silence
          is a large part of why this bug class survives to production.
        """
        candidates = self.wall_to_instants(naive_instant)
        if len(candidates) == 1:
            info = self.lookup(candidates[0])
            return WallResolution(
                instants=(candidates[0],),
                multiplicity="unique",
                chosen=candidates[0],
                chosen_utoff=info.utoff,
                chosen_abbrev=info.abbrev,
            )
        if len(candidates) >= 2:
            info = self.lookup(candidates[0])
            return WallResolution(
                instants=tuple(candidates),
                multiplicity="fold",
                chosen=candidates[0],
                chosen_utoff=info.utoff,
                chosen_abbrev=info.abbrev,
            )
        chosen, shift = self._resolve_gap(naive_instant)
        info = self.lookup(chosen)
        return WallResolution(
            instants=(),
            multiplicity="gap",
            chosen=chosen,
            chosen_utoff=info.utoff,
            chosen_abbrev=info.abbrev,
            gap_shift=shift,
        )

    def _resolve_gap(self, naive_instant: int) -> tuple[int, int]:
        """Push a nonexistent reading forward across the gap that swallowed it.

        Returns ``(instant, shift_seconds)``. The offset in force *before* the
        gap is the one that defines where the reading lands, so the result is
        ``naive - utoff_before``, which sits just past the transition.
        """
        low = max(naive_instant - _INVERSE_PROBE_WINDOW, MIN_INSTANT)
        high = min(naive_instant + _INVERSE_PROBE_WINDOW, MAX_INSTANT)
        best: tuple[int, int] | None = None
        for moment in self.breakpoints(low, high):
            before = self.lookup(max(moment - 1, MIN_INSTANT)).utoff
            after = self.lookup(moment).utoff
            if after <= before:  # Not a forward jump; cannot create a gap.
                continue
            gap_width = after - before
            gap_start_naive = moment + before
            if gap_start_naive <= naive_instant < gap_start_naive + gap_width:
                shifted = naive_instant - before
                candidate = (shifted, gap_width)
                if best is None or candidate[0] < best[0]:
                    best = candidate
        if best is not None:
            return best
        # Defensive: a reading with no candidates and no locatable gap should be
        # unreachable. Fall back to the offset in force at the reading rather
        # than inventing a value, and surface it as a zero-width shift.
        return naive_instant - self.lookup(naive_instant).utoff, 0

    # ---- misc ------------------------------------------------------------

    def distinct_offsets(self) -> tuple[int, ...]:
        """All UTC offsets this zone can take, ascending."""
        offsets = {t.utoff for t in self.types}
        if self.posix is not None:
            offsets.add(self.posix.std_utoff)
            if self.posix.dst_utoff is not None:
                offsets.add(self.posix.dst_utoff)
        return tuple(sorted(offsets))

    def signature(self) -> str:
        """Compact identity used to spot zones that are byte-different but equivalent."""
        parts = [f"v{self.version}"]
        parts.extend(f"{t.instant}:{self.types[t.type_index].key()}" for t in self.transitions)
        parts.append(self.posix.spec if self.posix else "-")
        return "|".join(parts)


def cross_check(rules: ZoneRules) -> tuple[str, ...]:
    """Report internal inconsistencies in a parsed zone.

    A well-formed file produced by ``zic`` agrees with its own footer at the
    boundary. Disagreement means the file was hand-edited or truncated, and the
    operator wants to know before trusting any diff built on it.
    """
    problems: list[str] = []
    if rules.posix is None:
        if rules.version >= 2 and rules.transitions:
            problems.append(
                "version 2+ file has no POSIX footer, so behaviour past the last "
                "transition is undefined rather than specified"
            )
        return tuple(problems)
    last = rules.last_explicit_instant
    if last is None:
        return tuple(problems)
    table_type = rules.types[rules.transitions[-1].type_index]
    footer_utoff, footer_abbrev, _ = rules.posix.utoff_and_abbrev(last)
    if table_type.utoff != footer_utoff:
        problems.append(
            f"footer disagrees with the final transition at the boundary: table says "
            f"{table_type.describe()}, footer says {footer_abbrev} "
            f"{format_offset(footer_utoff)}"
        )
    return tuple(problems)


def offsets_equal(left: Sequence[TTInfo], right: Sequence[TTInfo]) -> bool:
    """True when two type tables describe the same set of local-time types."""
    return sorted(t.key() for t in left) == sorted(t.key() for t in right)
