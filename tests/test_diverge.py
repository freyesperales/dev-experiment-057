"""Tests for the divergence engine.

Every zone built here is internally consistent -- its POSIX footer agrees with
its final transition -- because that is what a real database contains, and
because gnomon reports the inconsistent case as a defect rather than treating it
as ordinary input.
"""

from __future__ import annotations

import pytest

from gnomon.diverge import Severity, diff_trees, diff_zone
from gnomon.posixtz import TransitionRule
from gnomon.rules import TTInfo, Transition, ZoneRules
from gnomon.timeutil import civil_to_instant, format_instant
from gnomon.tzif import parse_tzif
from gnomon.tzifwrite import build_tzif

from conftest import abolished_dst_zone, fixed_zone, na_dst_zone, pulse_zone

WINDOW_START = civil_to_instant(2025, 1, 1)
WINDOW_END = civil_to_instant(2032, 1, 1)
NOW = civil_to_instant(2026, 9, 29)

_EET_FOOTER = "EET-2EEST,M3.5.0/3,M10.5.0/4"


def _diff(left_bytes: bytes, right_bytes: bytes, *, name: str = "Test/Zone", **kwargs):
    """Parse two zones and diff them over the default window."""
    options = {"window_start": WINDOW_START, "window_end": WINDOW_END, "now": NOW}
    options.update(kwargs)
    return diff_zone(
        name,
        parse_tzif(left_bytes, name=name),
        parse_tzif(right_bytes, name=name),
        **options,
    )


def _zones(**mapping: bytes) -> dict[str, ZoneRules]:
    """Parse a set of zone byte blobs, keyed by a slash-free test name."""
    return {
        name.replace("_", "/"): parse_tzif(data, name=name.replace("_", "/"))
        for name, data in mapping.items()
    }


def _eet_zone(moved_autumn_hour_in: dict[int, int]) -> bytes:
    """An EET zone; `moved_autumn_hour_in` overrides the fall-back hour by year.

    An EET zone normally transitions at 01:00 UTC, which is 03:00 local going in
    and 04:00 local coming out. Overriding one year's fall-back hour produces a
    divergence exactly one hour wide in that year and nowhere else.
    """
    types = [TTInfo(7200, False, "EET"), TTInfo(10800, True, "EEST")]
    spring = TransitionRule(kind="M", month=3, week=5, weekday=0, time=3 * 3600)
    records: list[Transition] = []
    for year in range(2025, 2031):
        hour = moved_autumn_hour_in.get(year, 4)
        autumn = TransitionRule(kind="M", month=10, week=5, weekday=0, time=hour * 3600)
        records.append(Transition(spring.instant(year, 7200), 1))
        records.append(Transition(autumn.instant(year, 10800), 0))
    records.sort(key=lambda record: record.instant)
    return build_tzif(types, records, footer=_EET_FOOTER)


def test_our_fixtures_are_internally_consistent() -> None:
    """Guard the guard: a fixture with a contradictory footer invalidates a test."""
    for data in (
        na_dst_zone(),
        abolished_dst_zone(),
        _eet_zone({}),
        pulse_zone(civil_to_instant(2027, 1, 1), civil_to_instant(2027, 6, 1)),
        fixed_zone(0, "AAA", "AAA0"),
    ):
        assert not parse_tzif(data, name="fixture").warnings


def test_identical_zones_produce_no_intervals() -> None:
    result = _diff(na_dst_zone(), na_dst_zone())
    assert result.verdict == "identical"
    assert result.intervals == ()
    assert result.severity is Severity.INFO
    assert not result.footer_changed


def test_abolished_dst_is_critical_and_open_ended() -> None:
    """The headline case: a footer change that never stops being wrong."""
    result = _diff(na_dst_zone(), abolished_dst_zone(), name="America/Vancouver")

    assert result.verdict == "future-divergence"
    assert result.severity is Severity.CRITICAL
    assert result.footer_changed
    assert result.left_footer == "PST8PDT,M3.2.0,M11.1.0"
    assert result.right_footer == "MST7"
    # The first offset disagreement is the fall-back the stale tree still believes in.
    assert format_instant(result.first_divergence) == "2026-11-01 09:00:00Z"
    assert result.max_abs_delta == 3600
    # It recurs every winter, and the final run reaches the end of the window.
    assert len(result.offset_intervals) >= 5
    assert result.offset_intervals[-1].end is None


def test_one_hour_wide_divergence_is_found() -> None:
    """A fall-back moved by one hour: the case a daily sampler walks straight past."""
    result = _diff(
        _eet_zone({2027: 3}), _eet_zone({}), name="Europe/Chisinau"
    )
    assert result.verdict == "future-divergence"
    offsets = result.offset_intervals
    assert len(offsets) == 1
    assert offsets[0].end is not None
    assert offsets[0].end - offsets[0].start == 3600
    assert offsets[0].delta == 3600
    assert format_instant(offsets[0].start) == "2027-10-31 00:00:00Z"


def test_same_offset_different_label_is_abbrev_only() -> None:
    result = _diff(
        fixed_zone(-25200, "-07", "<-07>7"),
        fixed_zone(-25200, "MST", "MST7"),
        name="America/Whitehorse",
    )
    assert result.verdict == "abbrev-only"
    assert result.severity is Severity.INFO
    assert result.offset_intervals == ()
    assert result.intervals[0].offsets_differ is False


def test_historical_only_divergence_is_low_severity() -> None:
    result = _diff(
        _eet_zone({2025: 3}), _eet_zone({}), name="Europe/Chisinau"
    )
    assert result.verdict == "historical-only"
    assert result.severity is Severity.LOW
    assert result.first_divergence is not None
    assert result.first_divergence < NOW


def test_each_dst_window_is_its_own_interval() -> None:
    """Runs must coalesce within a window but never merge across a gap."""
    types = [TTInfo(0, False, "AAA"), TTInfo(3600, True, "BBB")]
    records: list[Transition] = []
    for month in range(1, 13, 2):
        records.append(Transition(civil_to_instant(2027, month, 1), 1))
        records.append(Transition(civil_to_instant(2027, month + 1, 1), 0))
    pulsed = build_tzif(types, records, footer="AAA0")
    result = _diff(fixed_zone(0, "AAA", "AAA0"), pulsed)
    assert len(result.offset_intervals) == 6
    assert all(interval.delta == 3600 for interval in result.offset_intervals)


def test_window_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="window_end must be after"):
        _diff(na_dst_zone(), na_dst_zone(), window_end=WINDOW_START)


def test_divergence_outside_the_window_is_not_reported() -> None:
    """Between the last spring forward and the first missed fall back, both agree."""
    result = _diff(
        na_dst_zone(),
        abolished_dst_zone(),
        window_start=civil_to_instant(2026, 4, 1),
        window_end=civil_to_instant(2026, 10, 1),
    )
    assert result.verdict == "abbrev-only"
    assert result.offset_intervals == ()


# ---- tree-level ----------------------------------------------------------


def test_diff_trees_partitions_shared_and_exclusive_zones() -> None:
    madrid = na_dst_zone(3600, 7200, "CET", "CEST", footer="CET-1CEST,M3.5.0,M10.5.0/3")
    left = _zones(
        America_Vancouver=na_dst_zone(), Europe_Madrid=madrid, Gone_Zone=fixed_zone(0, "UTC", "UTC0")
    )
    right = _zones(
        America_Vancouver=abolished_dst_zone(),
        Europe_Madrid=madrid,
        New_Zone=fixed_zone(0, "UTC", "UTC0"),
    )

    result = diff_trees(
        left,
        right,
        left_label="2025b",
        right_label="2026d",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=NOW,
        collapse_aliases=False,
    )
    assert result.left_only == ("Gone/Zone",)
    assert result.right_only == ("New/Zone",)
    assert result.max_severity is Severity.CRITICAL
    verdicts = {zone.name: zone.verdict for zone in result.zones}
    assert verdicts["America/Vancouver"] == "future-divergence"
    assert verdicts["Europe/Madrid"] == "identical"
    assert verdicts["Gone/Zone"] == "only-in-left"
    assert verdicts["New/Zone"] == "only-in-right"


def test_first_divergence_picks_the_soonest_future_break() -> None:
    flat = fixed_zone(0, "AAA", "AAA0")
    result = diff_trees(
        _zones(Z_Soon=flat, Z_Later=flat),
        _zones(
            Z_Soon=pulse_zone(civil_to_instant(2026, 12, 1), civil_to_instant(2027, 2, 1)),
            Z_Later=pulse_zone(civil_to_instant(2028, 12, 1), civil_to_instant(2029, 2, 1)),
        ),
        left_label="a",
        right_label="b",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=NOW,
        collapse_aliases=False,
    )
    headline = result.first_divergence
    assert headline is not None
    assert headline[1] == "Z/Soon"
    assert format_instant(headline[0]) == "2026-12-01 00:00:00Z"


def test_by_severity_orders_worst_first_then_soonest() -> None:
    flat = fixed_zone(0, "AAA", "AAA0")
    result = diff_trees(
        _zones(Z_A=flat, Z_B=flat, Z_C=flat),
        _zones(
            Z_A=fixed_zone(0, "XXX", "XXX0"),  # abbrev-only: same offset, new label
            Z_B=pulse_zone(civil_to_instant(2028, 1, 1), civil_to_instant(2028, 6, 1)),
            Z_C=pulse_zone(civil_to_instant(2027, 1, 1), civil_to_instant(2027, 6, 1)),
        ),
        left_label="a",
        right_label="b",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        now=NOW,
        collapse_aliases=False,
    )
    assert [zone.name for zone in result.by_severity()] == ["Z/C", "Z/B", "Z/A"]


def test_severity_parsing_accepts_names_and_rejects_junk() -> None:
    assert Severity.parse("critical") is Severity.CRITICAL
    assert Severity.parse("  LOW ") is Severity.LOW
    with pytest.raises(ValueError, match="unknown severity"):
        Severity.parse("catastrophic")
