"""Tests for the POSIX TZ footer parser and evaluator."""

from __future__ import annotations

import pytest

from gnomon.errors import PosixTzParseError
from gnomon.posixtz import TransitionRule, parse_posix_tz
from gnomon.timeutil import civil_to_instant, format_instant


def test_fixed_offset_zone_has_no_dst() -> None:
    parsed = parse_posix_tz("MST7")
    assert parsed.std_abbrev == "MST"
    assert parsed.std_utoff == -25200  # POSIX "7" means UTC-07:00.
    assert not parsed.has_dst
    assert parsed.transitions_in_range(0, 10**10) == []


def test_posix_offset_sign_is_inverted() -> None:
    """POSIX offsets are added to local time to get UTC, so the sign flips."""
    assert parse_posix_tz("CET-1CEST,M3.5.0,M10.5.0/3").std_utoff == 3600
    assert parse_posix_tz("PST8PDT,M3.2.0,M11.1.0").std_utoff == -28800


def test_dst_offset_defaults_to_one_hour_ahead() -> None:
    parsed = parse_posix_tz("EST5EDT,M3.2.0,M11.1.0")
    assert parsed.std_utoff == -18000
    assert parsed.dst_utoff == -14400


def test_quoted_numeric_abbreviations() -> None:
    parsed = parse_posix_tz("<-03>3<-02>,M10.3.0/0,M2.3.0/0")
    assert parsed.std_abbrev == "-03"
    assert parsed.dst_abbrev == "-02"
    assert parsed.std_utoff == -10800
    assert parsed.dst_utoff == -7200


def test_north_american_transitions_land_on_the_right_instants() -> None:
    parsed = parse_posix_tz("PST8PDT,M3.2.0,M11.1.0")
    found = parsed.transitions_in_range(
        civil_to_instant(2026, 1, 1), civil_to_instant(2027, 1, 1)
    )
    # Second Sunday in March at 02:00 PST, first Sunday in November at 02:00 PDT.
    assert [format_instant(instant) for instant in found] == [
        "2026-03-08 10:00:00Z",
        "2026-11-01 09:00:00Z",
    ]


def test_dst_membership_across_the_year() -> None:
    parsed = parse_posix_tz("PST8PDT,M3.2.0,M11.1.0")
    assert parsed.utoff_and_abbrev(civil_to_instant(2026, 7, 1)) == (-25200, "PDT", True)
    assert parsed.utoff_and_abbrev(civil_to_instant(2026, 1, 1)) == (-28800, "PST", False)


def test_southern_hemisphere_dst_wraps_the_new_year() -> None:
    """DST that starts in October and ends in February must wrap correctly."""
    parsed = parse_posix_tz("<-03>3<-02>,M10.3.0/0,M2.3.0/0")
    assert parsed.utoff_and_abbrev(civil_to_instant(2026, 12, 15))[2] is True
    assert parsed.utoff_and_abbrev(civil_to_instant(2026, 1, 10))[2] is True
    assert parsed.utoff_and_abbrev(civil_to_instant(2026, 6, 15))[2] is False


def test_week_five_means_the_last_such_weekday() -> None:
    rule = TransitionRule(kind="M", month=10, week=5, weekday=0, time=3 * 3600)
    # The last Sunday of October 2026 is the 25th.
    assert rule.civil_date(2026) == (2026, 10, 25)


def test_week_five_falls_back_when_there_is_no_fifth_occurrence() -> None:
    rule = TransitionRule(kind="M", month=2, week=5, weekday=1, time=0)
    year, month, day = rule.civil_date(2026)
    assert (year, month) == (2026, 2)
    assert day <= 28


def test_julian_rule_skips_february_29() -> None:
    """A "J" rule numbers days 1..365 and never counts the leap day."""
    rule = TransitionRule(kind="J", day=60, time=0)
    assert rule.civil_date(2026) == (2026, 3, 1)  # Not a leap year.
    assert rule.civil_date(2028) == (2028, 3, 1)  # Leap year: still 1 March.


def test_zero_based_rule_counts_february_29() -> None:
    """An "n" rule numbers days 0..365 and does count the leap day."""
    rule = TransitionRule(kind="n", day=59, time=0)
    assert rule.civil_date(2026) == (2026, 3, 1)
    assert rule.civil_date(2028) == (2028, 2, 29)


def test_rule_time_is_not_sign_inverted() -> None:
    """Unlike a zone offset, a rule time is wall clock: /-1 is 23:00 the day before."""
    parsed = parse_posix_tz("XXX0YYY,M3.2.0/-1,M11.1.0")
    assert parsed.dst_start is not None
    assert parsed.dst_start.time == -3600


def test_extended_rule_time_beyond_24h_is_accepted() -> None:
    parsed = parse_posix_tz("XXX0YYY,M3.2.0/167,M11.1.0")
    assert parsed.dst_start is not None
    assert parsed.dst_start.time == 167 * 3600


def test_dst_name_without_rules_warns_and_assumes_year_round() -> None:
    parsed = parse_posix_tz("EST5EDT")
    assert not parsed.has_dst
    assert parsed.warnings
    assert parsed.utoff_and_abbrev(0) == (-14400, "EDT", True)


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "AB5",  # Abbreviation shorter than three characters.
        "PST",  # Missing offset.
        "<PST8",  # Unterminated quote.
        "PST8PDT,M3.2.0",  # Only one rule.
        "PST8PDT,M3.2.0,M11.1.0,extra",  # Trailing junk.
        "PST8PDT,M13.2.0,M11.1.0",  # Month out of range.
        "PST8PDT,M3.6.0,M11.1.0",  # Week out of range.
        "PST8PDT,M3.2.7,M11.1.0",  # Weekday out of range.
        "PST8PDT,J0,M11.1.0",  # Julian day out of range.
        "PST8PDT,J400,M11.1.0",
        "PST8PDT,M3.2.0/999,M11.1.0",  # Rule time beyond the RFC 8536 range.
        "PST8:99PDT,M3.2.0,M11.1.0",  # Minutes out of range.
    ],
)
def test_malformed_specs_are_rejected(spec: str) -> None:
    with pytest.raises(PosixTzParseError):
        parse_posix_tz(spec)


def test_describe_is_readable() -> None:
    assert parse_posix_tz("MST7").describe() == "fixed MST -07:00"
    assert "PST -08:00" in parse_posix_tz("PST8PDT,M3.2.0,M11.1.0").describe()
