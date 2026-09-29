"""Platform-independent civil-time arithmetic.

`datetime.timestamp()` and `time.mktime()` both consult the *host* timezone
database.  gnomon exists precisely because host and target databases disagree,
so it must never ask the host anything.  Every conversion below is pure
proleptic-Gregorian arithmetic over `datetime.date.toordinal`, which is exact
for years 1..9999 on every platform.
"""

from __future__ import annotations

import datetime as _dt
from typing import Final

_EPOCH_ORDINAL: Final[int] = _dt.date(1970, 1, 1).toordinal()
SECONDS_PER_DAY: Final[int] = 86_400

#: Widest instant range representable as a naive UTC `datetime`.
MIN_INSTANT: Final[int] = (_dt.date(1, 1, 1).toordinal() - _EPOCH_ORDINAL) * SECONDS_PER_DAY
MAX_INSTANT: Final[int] = (
    _dt.date(9999, 12, 31).toordinal() - _EPOCH_ORDINAL
) * SECONDS_PER_DAY + SECONDS_PER_DAY - 1


def days_from_civil(year: int, month: int, day: int) -> int:
    """Return days since 1970-01-01 for a proleptic-Gregorian date."""
    return _dt.date(year, month, day).toordinal() - _EPOCH_ORDINAL


def civil_to_instant(year: int, month: int, day: int, seconds_of_day: int = 0) -> int:
    """Convert a civil date plus a second-of-day offset to a Unix instant.

    `seconds_of_day` may be negative or exceed 86400; POSIX TZ strings in TZif
    version 3 files are explicitly allowed to use times in [-604799, 604799],
    which denote instants on neighbouring days.
    """
    return days_from_civil(year, month, day) * SECONDS_PER_DAY + seconds_of_day


def instant_to_civil(instant: int) -> _dt.datetime:
    """Convert a Unix instant to a naive `datetime` denoting UTC civil time."""
    if not MIN_INSTANT <= instant <= MAX_INSTANT:
        raise ValueError(f"instant {instant} is outside representable range")
    days, rem = divmod(instant, SECONDS_PER_DAY)
    return _dt.datetime.combine(
        _dt.date.fromordinal(days + _EPOCH_ORDINAL), _dt.time()
    ) + _dt.timedelta(seconds=rem)


def format_instant(instant: int) -> str:
    """Render a Unix instant as an unambiguous UTC stamp."""
    try:
        return instant_to_civil(instant).strftime("%Y-%m-%d %H:%M:%SZ")
    except ValueError:
        return f"instant:{instant}"


def year_of(instant: int) -> int:
    """Return the UTC calendar year containing `instant`."""
    return instant_to_civil(instant).year


def is_leap(year: int) -> bool:
    """Return True when `year` is a Gregorian leap year."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def format_offset(seconds: int) -> str:
    """Render a UTC offset in seconds as ``+HH:MM`` / ``+HH:MM:SS``."""
    sign = "-" if seconds < 0 else "+"
    total = abs(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if secs:
        return f"{sign}{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{sign}{hours:02d}:{minutes:02d}"


def format_delta(seconds: int) -> str:
    """Render a signed duration compactly, e.g. ``+1h`` or ``-30m`` or ``0``."""
    if seconds == 0:
        return "0"
    sign = "-" if seconds < 0 else "+"
    total = abs(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    chunks: list[str] = []
    if hours:
        chunks.append(f"{hours}h")
    if minutes:
        chunks.append(f"{minutes}m")
    if secs or not chunks:
        chunks.append(f"{secs}s")
    return sign + "".join(chunks)


def parse_date(text: str) -> int:
    """Parse ``YYYY-MM-DD`` into the Unix instant of its UTC midnight."""
    try:
        parsed = _dt.date.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"expected a YYYY-MM-DD date, got {text!r}") from exc
    return days_from_civil(parsed.year, parsed.month, parsed.day) * SECONDS_PER_DAY


def parse_naive(text: str) -> tuple[int, int, int, int]:
    """Parse a wall-clock stamp into ``(year, month, day, seconds_of_day)``.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM-DD HH:MM``, ``YYYY-MM-DDTHH:MM:SS`` and
    fractional seconds (truncated).  A trailing ``Z`` or numeric UTC offset is
    rejected: the whole point of this input is that it is *unzoned* wall time,
    and silently accepting an offset would hide the bug class gnomon hunts.
    """
    raw = text.strip()
    if not raw:
        raise ValueError("empty wall-clock stamp")
    if raw.endswith(("Z", "z")):
        raise ValueError(
            f"{raw!r} carries a UTC designator; gnomon needs unzoned wall time"
        )
    body = raw.replace("T", " ", 1)
    datepart, _, timepart = body.partition(" ")
    if any(ch in timepart for ch in "+") or timepart.count("-") > 0:
        raise ValueError(
            f"{raw!r} carries a UTC offset; gnomon needs unzoned wall time"
        )
    try:
        parsed_date = _dt.date.fromisoformat(datepart)
    except ValueError as exc:
        raise ValueError(f"bad date in wall-clock stamp {raw!r}") from exc
    seconds = 0
    if timepart:
        fields = timepart.split(":")
        if len(fields) > 3:
            raise ValueError(f"bad time in wall-clock stamp {raw!r}")
        try:
            hours = int(fields[0])
            minutes = int(fields[1]) if len(fields) > 1 else 0
            secs = int(float(fields[2])) if len(fields) > 2 else 0
        except ValueError as exc:
            raise ValueError(f"bad time in wall-clock stamp {raw!r}") from exc
        if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= secs <= 60):
            raise ValueError(f"time out of range in wall-clock stamp {raw!r}")
        seconds = hours * 3600 + minutes * 60 + min(secs, 59)
    return parsed_date.year, parsed_date.month, parsed_date.day, seconds
