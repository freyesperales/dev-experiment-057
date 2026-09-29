"""Tests for the forward and inverse maps.

The inverse map is where the bug class gnomon hunts actually lives, so the gap
and fold cases get more attention here than the straightforward lookups.
"""

from __future__ import annotations

import pytest

from gnomon.errors import TzifParseError
from gnomon.rules import TTInfo, Transition, ZoneRules, offsets_equal
from gnomon.timeutil import civil_to_instant, format_instant
from gnomon.tzif import parse_tzif

from conftest import abolished_dst_zone, fixed_zone, na_dst_zone


@pytest.fixture
def pacific() -> ZoneRules:
    """A Pacific-style DST zone with an explicit table and a footer."""
    return parse_tzif(na_dst_zone(), name="Test/Pacific")


@pytest.fixture
def permanent() -> ZoneRules:
    """The same zone after abolishing DST: history, then a fixed -07:00."""
    return parse_tzif(abolished_dst_zone(), name="Test/Permanent")


def test_default_type_is_the_first_standard_time() -> None:
    """RFC 8536 prescribes the first non-DST type before the first transition."""
    from gnomon.tzifwrite import build_tzif

    types = [TTInfo(3600, True, "DDD"), TTInfo(0, False, "SSS")]
    rules = parse_tzif(
        build_tzif(types, [Transition(civil_to_instant(2030, 1, 1), 0)], footer="SSS0"),
        name="Test/DstFirst",
    )
    assert rules.default_type.abbrev == "SSS"
    assert rules.lookup(civil_to_instant(1990, 1, 1)).abbrev == "SSS"


def test_lookup_uses_the_table_then_the_footer(permanent: ZoneRules) -> None:
    # Inside the table: the 2025 summer is DST.
    assert permanent.lookup(civil_to_instant(2025, 7, 1)).abbrev == "PDT"
    # Beyond the last transition the footer takes over, permanently.
    assert permanent.lookup(civil_to_instant(2030, 1, 1)).abbrev == "MST"
    assert permanent.lookup(civil_to_instant(2030, 7, 1)).abbrev == "MST"


def test_breakpoints_combine_table_and_footer(pacific: ZoneRules) -> None:
    """Past the table, the footer must still yield yearly breakpoints."""
    found = pacific.breakpoints(civil_to_instant(2030, 1, 1), civil_to_instant(2031, 1, 1))
    assert [format_instant(instant) for instant in found] == [
        "2030-03-10 10:00:00Z",
        "2030-11-03 09:00:00Z",
    ]


def test_breakpoints_are_empty_for_a_fixed_zone() -> None:
    rules = parse_tzif(fixed_zone(0, "UTC", "UTC0"), name="Test/Utc")
    assert rules.breakpoints(0, civil_to_instant(2100, 1, 1)) == []


def test_unique_reading_resolves_to_one_instant(pacific: ZoneRules) -> None:
    naive = civil_to_instant(2026, 7, 15, 12 * 3600)
    resolution = pacific.resolve_wall(naive)
    assert resolution.multiplicity == "unique"
    assert resolution.chosen_utoff == -25200
    assert resolution.chosen == naive + 25200


def test_fold_reading_has_two_candidates(pacific: ZoneRules) -> None:
    """01:30 on the autumn transition day happens twice."""
    naive = civil_to_instant(2026, 11, 1, 1 * 3600 + 30 * 60)
    resolution = pacific.resolve_wall(naive)
    assert resolution.multiplicity == "fold"
    assert len(resolution.instants) == 2
    # An hour apart, PDT first then PST.
    assert resolution.instants[1] - resolution.instants[0] == 3600
    assert resolution.chosen == resolution.instants[0]
    assert resolution.chosen_abbrev == "PDT"


def test_gap_reading_has_no_candidates_and_is_pushed_forward(pacific: ZoneRules) -> None:
    """02:30 on the spring transition day never happens."""
    naive = civil_to_instant(2026, 3, 8, 2 * 3600 + 30 * 60)
    resolution = pacific.resolve_wall(naive)
    assert resolution.multiplicity == "gap"
    assert resolution.instants == ()
    assert resolution.gap_shift == 3600
    # Postgres pushes the value forward by the width of the gap: the offset in
    # force *before* the transition is the one applied.
    assert resolution.chosen == naive + 28800


def test_reading_just_outside_a_gap_is_unique(pacific: ZoneRules) -> None:
    for seconds in (1 * 3600 + 59 * 60, 3 * 3600):
        naive = civil_to_instant(2026, 3, 8, seconds)
        assert pacific.resolve_wall(naive).multiplicity == "unique"


def test_reading_just_outside_a_fold_is_unique(pacific: ZoneRules) -> None:
    for seconds in (0, 2 * 3600 + 1):
        naive = civil_to_instant(2026, 11, 1, seconds)
        assert pacific.resolve_wall(naive).multiplicity == "unique"


def test_fold_and_gap_persist_into_the_footer_governed_region(pacific: ZoneRules) -> None:
    """Folds and gaps generated purely by the footer must be found too."""
    gap = pacific.resolve_wall(civil_to_instant(2031, 3, 9, 2 * 3600 + 30 * 60))
    fold = pacific.resolve_wall(civil_to_instant(2031, 11, 2, 1 * 3600 + 30 * 60))
    assert gap.multiplicity == "gap"
    assert fold.multiplicity == "fold"


def test_permanent_zone_has_no_gaps_or_folds_after_the_change(permanent: ZoneRules) -> None:
    """Once DST is abolished, every reading is unique for ever."""
    for year in (2027, 2030, 2040):
        for month, day, hour in ((3, 8, 2), (11, 1, 1)):
            naive = civil_to_instant(year, month, day, hour * 3600 + 1800)
            assert permanent.resolve_wall(naive).multiplicity == "unique"


def test_negative_dst_produces_a_fold_going_into_the_lower_offset() -> None:
    """Morocco-style negative DST: the clock goes back, so a fold appears."""
    from gnomon.tzifwrite import build_tzif

    types = [TTInfo(3600, False, "+01"), TTInfo(0, True, "+00")]
    records = [Transition(civil_to_instant(2026, 2, 15, 3 * 3600) - 3600, 1)]
    rules = parse_tzif(build_tzif(types, records, footer="<+00>0"), name="Test/Morocco")
    resolution = rules.resolve_wall(civil_to_instant(2026, 2, 15, 2 * 3600 + 30 * 60))
    assert resolution.multiplicity == "fold"


def test_distinct_offsets_includes_footer_offsets(pacific: ZoneRules) -> None:
    assert pacific.distinct_offsets() == (-28800, -25200)


def test_signature_distinguishes_footer_changes() -> None:
    left = parse_tzif(na_dst_zone(), name="a").signature()
    right = parse_tzif(abolished_dst_zone(), name="b").signature()
    assert left != right


def test_offsets_equal_ignores_ordering() -> None:
    first = [TTInfo(0, False, "A"), TTInfo(3600, True, "B")]
    second = [TTInfo(3600, True, "B"), TTInfo(0, False, "A")]
    assert offsets_equal(first, second)
    assert not offsets_equal(first, [TTInfo(0, False, "A")])


def test_zone_with_no_types_is_rejected() -> None:
    with pytest.raises(TzifParseError, match="no local-time types"):
        ZoneRules(name="Test/Empty", types=(), transitions=())
