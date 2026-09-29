"""POSIX TZ string parser and evaluator (the TZif v2+ footer).

Why this module carries so much weight
--------------------------------------
Modern ``zic`` defaults to the *slim* output format: a zone's TZif file contains
explicit transitions only up to the last rule change, and everything after that
is described by a single POSIX TZ string in the file's footer. So when a
jurisdiction abolishes DST, the change shows up almost entirely as a different
footer -- ``PST8PDT,M3.2.0,M11.1.0`` becomes ``MST7`` -- with no new transition
records at all. A diff tool that only compares transition tables sees two
nearly identical files and reports nothing, while every future timestamp the two
databases produce disagrees by an hour. Evaluating the footer is therefore not
an optional refinement; it is the load-bearing part.

Grammar (POSIX.1 plus the TZif v3 extensions of RFC 8536 section 3.3.1)::

    TZ     := stdname stdoffset [ dstname [ dstoffset ] [ "," rule "," rule ] ]
    name   := 3+ alphabetic characters | "<" alnum-plus-minus ">"
    offset := [+-]hh[:mm[:ss]]
    rule   := date [ "/" time ]
    date   := Jn | n | Mm.w.d
    time   := [+-]hh[:mm[:ss]]        default 02:00:00

Sign convention: a POSIX offset is the value *added to local time to obtain
UTC*, i.e. the negation of the UTC offset everyone else means by "UTC-08:00".
Getting this backwards is the classic footgun; ``utoff`` below is always the
conventional UTC offset, negative west of Greenwich.
"""

from __future__ import annotations

import calendar
import datetime as _dt
from dataclasses import dataclass
from typing import Final, Literal

from .errors import PosixTzParseError
from .timeutil import civil_to_instant, format_offset, is_leap, year_of

#: Widest DST-rule time-of-day permitted by RFC 8536 for TZif version 3.
_MAX_RULE_TIME: Final[int] = 167 * 3600 + 59 * 60 + 59
_DEFAULT_RULE_TIME: Final[int] = 2 * 3600

RuleKind = Literal["J", "n", "M"]


@dataclass(frozen=True, slots=True)
class TransitionRule:
    """A yearly DST transition date drawn from a POSIX TZ string."""

    kind: RuleKind
    #: For kind "J"/"n": the day number. For kind "M": unused.
    day: int = 0
    #: For kind "M": month 1-12, week 1-5 (5 meaning "last"), weekday 0-6 (Sun=0).
    month: int = 0
    week: int = 0
    weekday: int = 0
    #: Local seconds after midnight at which the transition occurs.
    time: int = _DEFAULT_RULE_TIME

    def civil_date(self, year: int) -> tuple[int, int, int]:
        """Resolve this rule to a concrete ``(year, month, day)`` in `year`."""
        if self.kind == "M":
            return _nth_weekday(year, self.month, self.week, self.weekday)
        if self.kind == "J":
            # Julian day 1..365; February 29 is never counted.
            day_of_year = self.day
            if is_leap(year) and day_of_year >= 60:
                day_of_year += 1
            return _from_day_of_year(year, day_of_year)
        # kind == "n": zero-based day of year, February 29 *is* counted.
        return _from_day_of_year(year, self.day + 1)

    def instant(self, year: int, utoff_before: int) -> int:
        """Return the UTC instant of this transition.

        `utoff_before` is the UTC offset in force immediately *before* the
        transition, because the rule's time field is expressed in the local
        time that is still running when the clock jumps.
        """
        year_, month, day = self.civil_date(year)
        return civil_to_instant(year_, month, day, self.time) - utoff_before


def _from_day_of_year(year: int, day_of_year: int) -> tuple[int, int, int]:
    """Convert a 1-based day-of-year into ``(year, month, day)``."""
    total = 366 if is_leap(year) else 365
    clamped = min(max(day_of_year, 1), total)
    resolved = _dt.date(year, 1, 1) + _dt.timedelta(days=clamped - 1)
    return resolved.year, resolved.month, resolved.day


def _nth_weekday(year: int, month: int, week: int, weekday: int) -> tuple[int, int, int]:
    """Resolve ``Mm.w.d`` to a date. Week 5 means the last such weekday."""
    days_in_month = calendar.monthrange(year, month)[1]
    # Python's weekday(): Monday=0..Sunday=6. POSIX: Sunday=0..Saturday=6.
    first_posix_weekday = (_dt.date(year, month, 1).weekday() + 1) % 7
    first_match = 1 + (weekday - first_posix_weekday) % 7
    if week == 5:
        day = first_match
        while day + 7 <= days_in_month:
            day += 7
    else:
        day = first_match + 7 * (week - 1)
        if day > days_in_month:  # A 5th occurrence that does not exist.
            day -= 7
    return year, month, day


@dataclass(frozen=True, slots=True)
class PosixTz:
    """An evaluated POSIX TZ string.

    When `dst_abbrev` is None the zone has a single fixed offset forever, which
    is exactly the shape a jurisdiction takes on after abolishing DST.
    """

    spec: str
    std_abbrev: str
    std_utoff: int
    dst_abbrev: str | None = None
    dst_utoff: int | None = None
    dst_start: TransitionRule | None = None
    dst_end: TransitionRule | None = None
    #: Non-fatal notes raised while parsing (reported, never silently dropped).
    warnings: tuple[str, ...] = ()

    @property
    def has_dst(self) -> bool:
        """True when this footer describes a recurring DST cycle."""
        return self.dst_abbrev is not None and self.dst_start is not None

    def utoff_and_abbrev(self, instant: int) -> tuple[int, str, bool]:
        """Return ``(utoff, abbreviation, is_dst)`` in force at `instant`."""
        if not self.has_dst:
            if self.dst_abbrev is not None and self.dst_utoff is not None:
                # "EST5EDT" with no rules: DST all year. Real tzdata never
                # emits this, but a hand-edited footer can, so honour it.
                return self.dst_utoff, self.dst_abbrev, True
            return self.std_utoff, self.std_abbrev, False
        if self._is_dst_at(instant):
            assert self.dst_utoff is not None and self.dst_abbrev is not None
            return self.dst_utoff, self.dst_abbrev, True
        return self.std_utoff, self.std_abbrev, False

    def _is_dst_at(self, instant: int) -> bool:
        """Decide DST membership, handling southern-hemisphere wrap-around."""
        assert self.dst_start is not None and self.dst_end is not None
        assert self.dst_utoff is not None
        year = year_of(instant)
        start = self.dst_start.instant(year, self.std_utoff)
        end = self.dst_end.instant(year, self.dst_utoff)
        if start <= end:
            return start <= instant < end
        # DST spans the new year: in effect outside the [end, start) window.
        return instant >= start or instant < end

    def transitions_in_range(self, start_instant: int, end_instant: int) -> list[int]:
        """List every transition instant in ``[start_instant, end_instant)``."""
        if not self.has_dst:
            return []
        assert self.dst_start is not None and self.dst_end is not None
        assert self.dst_utoff is not None
        found: list[int] = []
        # Widen by a year on each side: a rule time may shift by whole days.
        for year in range(year_of(start_instant) - 1, year_of(end_instant) + 2):
            for rule, utoff_before in (
                (self.dst_start, self.std_utoff),
                (self.dst_end, self.dst_utoff),
            ):
                moment = rule.instant(year, utoff_before)
                if start_instant <= moment < end_instant:
                    found.append(moment)
        return sorted(set(found))

    def describe(self) -> str:
        """Human-readable one-liner used in reports."""
        if not self.has_dst:
            return f"fixed {self.std_abbrev} {format_offset(self.std_utoff)}"
        assert self.dst_abbrev is not None and self.dst_utoff is not None
        return (
            f"{self.std_abbrev} {format_offset(self.std_utoff)} / "
            f"{self.dst_abbrev} {format_offset(self.dst_utoff)} with yearly rules"
        )


def parse_posix_tz(spec: str) -> PosixTz:
    """Parse a POSIX TZ string into a `PosixTz`.

    Raises:
        PosixTzParseError: on any malformed component. The footer is the only
            description of a zone's indefinite future, so a footer that cannot
            be read is an error, never a shrug.
    """
    text = spec.strip()
    if not text:
        raise PosixTzParseError("empty TZ string", spec=spec)
    warnings: list[str] = []
    cursor = 0

    std_abbrev, cursor = _parse_name(text, cursor, spec)
    std_posix_offset, cursor = _parse_offset(text, cursor, spec, required=True)
    assert std_posix_offset is not None
    std_utoff = -std_posix_offset

    if cursor >= len(text):
        return PosixTz(
            spec=text,
            std_abbrev=std_abbrev,
            std_utoff=std_utoff,
            warnings=tuple(warnings),
        )

    dst_abbrev, cursor = _parse_name(text, cursor, spec)
    dst_posix_offset, cursor = _parse_offset(text, cursor, spec, required=False)
    if dst_posix_offset is None:
        # POSIX default: DST runs one hour ahead of standard time.
        dst_utoff = std_utoff + 3600
    else:
        dst_utoff = -dst_posix_offset

    dst_start: TransitionRule | None = None
    dst_end: TransitionRule | None = None
    if cursor < len(text):
        if text[cursor] != ",":
            raise PosixTzParseError(
                f"expected a comma before DST rules at position {cursor}", spec=spec
            )
        cursor += 1
        dst_start, cursor = _parse_rule(text, cursor, spec)
        if cursor >= len(text) or text[cursor] != ",":
            raise PosixTzParseError(
                "DST start rule must be followed by a comma and an end rule", spec=spec
            )
        cursor += 1
        dst_end, cursor = _parse_rule(text, cursor, spec)
        if cursor != len(text):
            raise PosixTzParseError(
                f"trailing junk {text[cursor:]!r} after DST rules", spec=spec
            )
    else:
        warnings.append(
            "DST abbreviation given without transition rules; treating DST as in "
            "force year-round (POSIX leaves this implementation-defined)"
        )

    return PosixTz(
        spec=text,
        std_abbrev=std_abbrev,
        std_utoff=std_utoff,
        dst_abbrev=dst_abbrev,
        dst_utoff=dst_utoff,
        dst_start=dst_start,
        dst_end=dst_end,
        warnings=tuple(warnings),
    )


def _parse_name(text: str, cursor: int, spec: str) -> tuple[str, int]:
    """Parse a zone abbreviation, quoted (``<...>``) or bare alphabetic."""
    if cursor >= len(text):
        raise PosixTzParseError("expected a zone abbreviation", spec=spec)
    if text[cursor] == "<":
        close = text.find(">", cursor)
        if close < 0:
            raise PosixTzParseError("unterminated '<' in zone abbreviation", spec=spec)
        name = text[cursor + 1 : close]
        if not name or not all(ch.isalnum() or ch in "+-" for ch in name):
            raise PosixTzParseError(f"bad quoted abbreviation {name!r}", spec=spec)
        return name, close + 1
    end = cursor
    while end < len(text) and text[end].isalpha():
        end += 1
    name = text[cursor:end]
    if len(name) < 3:
        raise PosixTzParseError(
            f"zone abbreviation {name!r} is shorter than 3 characters", spec=spec
        )
    return name, end


def _parse_offset(
    text: str, cursor: int, spec: str, *, required: bool
) -> tuple[int | None, int]:
    """Parse ``[+-]hh[:mm[:ss]]`` into signed seconds, POSIX sign preserved."""
    start = cursor
    sign = 1
    if cursor < len(text) and text[cursor] in "+-":
        sign = -1 if text[cursor] == "-" else 1
        cursor += 1
    fields: list[int] = []
    while len(fields) < 3:
        digits_start = cursor
        while cursor < len(text) and text[cursor].isdigit():
            cursor += 1
        if digits_start == cursor:
            break
        fields.append(int(text[digits_start:cursor]))
        if len(fields) < 3 and cursor < len(text) and text[cursor] == ":":
            cursor += 1
        else:
            break
    if not fields:
        if required:
            raise PosixTzParseError(
                f"expected a UTC offset at position {start}", spec=spec
            )
        return None, start
    hours = fields[0]
    minutes = fields[1] if len(fields) > 1 else 0
    seconds = fields[2] if len(fields) > 2 else 0
    if hours > 167 or minutes > 59 or seconds > 59:
        raise PosixTzParseError(
            f"offset {hours}:{minutes}:{seconds} out of range", spec=spec
        )
    return sign * (hours * 3600 + minutes * 60 + seconds), cursor


def _parse_rule(text: str, cursor: int, spec: str) -> tuple[TransitionRule, int]:
    """Parse ``Jn`` / ``n`` / ``Mm.w.d`` with an optional ``/time`` suffix."""
    if cursor >= len(text):
        raise PosixTzParseError("expected a DST transition rule", spec=spec)
    kind: RuleKind
    day = month = week = weekday = 0

    if text[cursor] == "J":
        kind = "J"
        cursor += 1
        day, cursor = _parse_int(text, cursor, spec, "Julian day")
        if not 1 <= day <= 365:
            raise PosixTzParseError(f"Julian day {day} not in 1..365", spec=spec)
    elif text[cursor] == "M":
        kind = "M"
        cursor += 1
        month, cursor = _parse_int(text, cursor, spec, "month")
        cursor = _expect(text, cursor, ".", spec)
        week, cursor = _parse_int(text, cursor, spec, "week")
        cursor = _expect(text, cursor, ".", spec)
        weekday, cursor = _parse_int(text, cursor, spec, "weekday")
        if not 1 <= month <= 12:
            raise PosixTzParseError(f"month {month} not in 1..12", spec=spec)
        if not 1 <= week <= 5:
            raise PosixTzParseError(f"week {week} not in 1..5", spec=spec)
        if not 0 <= weekday <= 6:
            raise PosixTzParseError(f"weekday {weekday} not in 0..6", spec=spec)
    else:
        kind = "n"
        day, cursor = _parse_int(text, cursor, spec, "day of year")
        if not 0 <= day <= 365:
            raise PosixTzParseError(f"day of year {day} not in 0..365", spec=spec)

    rule_time = _DEFAULT_RULE_TIME
    if cursor < len(text) and text[cursor] == "/":
        cursor += 1
        # A rule time is a wall-clock time of day, so unlike a zone offset it is
        # NOT sign-inverted: "/-1" means 23:00 on the preceding day.
        parsed, cursor = _parse_offset(text, cursor, spec, required=True)
        assert parsed is not None
        rule_time = parsed
        if abs(rule_time) > _MAX_RULE_TIME:
            raise PosixTzParseError(
                f"rule time {rule_time}s exceeds the RFC 8536 range", spec=spec
            )

    return (
        TransitionRule(
            kind=kind, day=day, month=month, week=week, weekday=weekday, time=rule_time
        ),
        cursor,
    )


def _parse_int(text: str, cursor: int, spec: str, what: str) -> tuple[int, int]:
    """Parse a run of decimal digits."""
    start = cursor
    while cursor < len(text) and text[cursor].isdigit():
        cursor += 1
    if start == cursor:
        raise PosixTzParseError(f"expected {what} at position {start}", spec=spec)
    return int(text[start:cursor]), cursor


def _expect(text: str, cursor: int, char: str, spec: str) -> int:
    """Consume a required literal character."""
    if cursor >= len(text) or text[cursor] != char:
        found = text[cursor : cursor + 1] or "end of string"
        raise PosixTzParseError(
            f"expected {char!r} at position {cursor}, found {found!r}", spec=spec
        )
    return cursor + 1
