"""Tests for the TZif reader and writer.

The most valuable test here is `test_agrees_with_stdlib_zoneinfo`: it checks
gnomon's parser and offset lookup against CPython's own, entirely independent
TZif implementation over thousands of instants. Two implementations written from
the same RFC agreeing on every sampled instant -- including in the region
governed only by the POSIX footer -- is much stronger evidence than any
hand-written expectation table.
"""

from __future__ import annotations

import datetime as _dt
import io
import struct
import zoneinfo
from pathlib import Path

import pytest

from gnomon.errors import TzifParseError
from gnomon.rules import TTInfo, Transition
from gnomon.timeutil import SECONDS_PER_DAY, civil_to_instant
from gnomon.tzif import MAGIC, looks_like_tzif, parse_tzif, read_tzif_file
from gnomon.tzifwrite import TzifWriteError, build_tzif, write_tzif_file

from conftest import abolished_dst_zone, fixed_zone, na_dst_zone


def test_round_trip_through_our_own_reader() -> None:
    data = na_dst_zone()
    rules = parse_tzif(data, name="Test/Pacific")
    assert rules.version == 2
    assert [t.abbrev for t in rules.types] == ["PST", "PDT"]
    assert rules.posix is not None
    assert rules.posix.spec == "PST8PDT,M3.2.0,M11.1.0"
    assert len(rules.transitions) == 8  # Four years, two transitions each.
    assert not rules.warnings


def test_written_file_is_accepted_by_stdlib_zoneinfo() -> None:
    """The writer must emit genuine TZif, not something only we can read."""
    data = na_dst_zone()
    parsed = zoneinfo.ZoneInfo.from_file(io.BytesIO(data), key="Test/Pacific")
    summer = _dt.datetime(2026, 7, 1, tzinfo=parsed)
    winter = _dt.datetime(2026, 12, 1, tzinfo=parsed)
    assert summer.utcoffset() == _dt.timedelta(hours=-7)
    assert summer.tzname() == "PDT"
    assert winter.utcoffset() == _dt.timedelta(hours=-8)
    assert winter.tzname() == "PST"


@pytest.mark.parametrize(
    "data,label",
    [
        (na_dst_zone(), "dst-cycle"),
        (abolished_dst_zone(), "abolished-dst"),
        (fixed_zone(0, "UTC", "UTC0"), "fixed-utc"),
        (
            na_dst_zone(3600, 7200, "CET", "CEST", footer="CET-1CEST,M3.5.0,M10.5.0/3"),
            "european",
        ),
    ],
)
def test_agrees_with_stdlib_zoneinfo(data: bytes, label: str) -> None:
    """Cross-validate against CPython's independent TZif implementation.

    Sampled every six hours over 2023-2032, which spans both the explicit
    transition table and the footer-governed region beyond it.
    """
    ours = parse_tzif(data, name=label)
    theirs = zoneinfo.ZoneInfo.from_file(io.BytesIO(data), key=label)
    start = civil_to_instant(2023, 1, 1)
    end = civil_to_instant(2032, 1, 1)
    mismatches: list[str] = []
    for instant in range(start, end, 6 * 3600):
        moment = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(
            seconds=instant
        )
        expected_offset = moment.astimezone(theirs).utcoffset()
        assert expected_offset is not None
        expected_name = moment.astimezone(theirs).tzname()
        actual = ours.lookup(instant)
        if actual.utoff != int(expected_offset.total_seconds()):
            mismatches.append(f"{moment}: offset {actual.utoff} vs {expected_offset}")
        elif actual.abbrev != expected_name:
            mismatches.append(f"{moment}: abbrev {actual.abbrev!r} vs {expected_name!r}")
        if len(mismatches) > 5:
            break
    assert not mismatches, f"{label}: " + "; ".join(mismatches)


def test_inverse_map_agrees_with_stdlib_for_unambiguous_readings() -> None:
    """Our wall-to-instant map must match datetime's for ordinary readings."""
    data = na_dst_zone()
    ours = parse_tzif(data, name="Test/Pacific")
    theirs = zoneinfo.ZoneInfo.from_file(io.BytesIO(data), key="Test/Pacific")
    for month in range(1, 13):
        for day in (5, 20):
            naive = civil_to_instant(2026, month, day, 12 * 3600)
            resolution = ours.resolve_wall(naive)
            aware = _dt.datetime(2026, month, day, 12, tzinfo=theirs)
            assert resolution.multiplicity == "unique"
            assert resolution.chosen == int(aware.timestamp())


def test_reader_rejects_bad_magic() -> None:
    with pytest.raises(TzifParseError, match="bad magic"):
        parse_tzif(b"NOPE" + b"\x00" * 60)


def test_reader_rejects_short_file() -> None:
    with pytest.raises(TzifParseError, match="shorter than"):
        parse_tzif(MAGIC + b"\x00" * 4)


def test_reader_rejects_unknown_version() -> None:
    data = bytearray(na_dst_zone())
    data[4:5] = b"9"
    with pytest.raises(TzifParseError, match="unknown TZif version"):
        parse_tzif(bytes(data))


def test_reader_rejects_truncation_before_second_header() -> None:
    data = na_dst_zone()
    with pytest.raises(TzifParseError, match="truncated|data block needs"):
        parse_tzif(data[: len(data) // 2])


def test_reader_rejects_non_ascending_transitions() -> None:
    """Hand-built bytes with out-of-order transitions must be refused."""
    types = [TTInfo(0, False, "AAA"), TTInfo(3600, True, "BBB")]
    good = build_tzif(types, [Transition(1000, 1), Transition(2000, 0)], footer="AAA0")
    # The 64-bit block's first transition time sits right after its header.
    header_end = good.index(MAGIC, 4) + 44
    broken = bytearray(good)
    struct.pack_into(">q", broken, header_end, 5000)  # Now 5000 then 2000.
    with pytest.raises(TzifParseError, match="strictly ascending"):
        parse_tzif(bytes(broken))


def test_reader_rejects_out_of_range_type_index() -> None:
    types = [TTInfo(0, False, "AAA"), TTInfo(3600, True, "BBB")]
    good = build_tzif(types, [Transition(1000, 1), Transition(2000, 0)], footer="AAA0")
    header_end = good.index(MAGIC, 4) + 44
    index_offset = header_end + 2 * 8  # Past both 64-bit transition times.
    broken = bytearray(good)
    broken[index_offset] = 7
    with pytest.raises(TzifParseError, match="references local-time type"):
        parse_tzif(bytes(broken))


def test_reader_rejects_malformed_footer() -> None:
    data = na_dst_zone()
    broken = data[: data.rindex(b"\n", 0, len(data) - 1) + 1] + b"!!!bad!!!\n"
    with pytest.raises(TzifParseError, match="footer TZ string is malformed"):
        parse_tzif(broken)


def test_empty_footer_is_a_warning_not_an_error() -> None:
    data = na_dst_zone()
    trimmed = data[: data.rindex(b"\n", 0, len(data) - 1) + 1] + b"\n"
    rules = parse_tzif(trimmed, name="Test/NoFooter")
    assert rules.posix is None
    assert any("footer is empty" in warning for warning in rules.warnings)


def test_cross_check_flags_a_footer_that_contradicts_the_table() -> None:
    """A footer disagreeing with the last transition is reported, not ignored."""
    types = [TTInfo(-28800, False, "PST"), TTInfo(-25200, True, "PDT")]
    records = [Transition(civil_to_instant(2026, 11, 1, 9 * 3600), 0)]
    # The table ends on standard time -08:00; the footer claims a fixed -05:00.
    data = build_tzif(types, records, footer="EST5")
    rules = parse_tzif(data, name="Test/Contradiction")
    assert any("footer disagrees" in warning for warning in rules.warnings)


def test_looks_like_tzif_sniffs_only_the_magic() -> None:
    assert looks_like_tzif(na_dst_zone())
    assert not looks_like_tzif(b"# zone.tab\nUS\t+1234\tAmerica/New_York\n")
    assert not looks_like_tzif(b"TZif")  # Too short for a header.


def test_read_tzif_file_reports_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(TzifParseError, match="cannot read file"):
        read_tzif_file(tmp_path / "absent")


def test_write_tzif_file_creates_parents(tmp_path: Path) -> None:
    target = write_tzif_file(
        tmp_path / "deep" / "nest" / "Zone", [TTInfo(0, False, "UTC")], [], footer="UTC0"
    )
    assert target.is_file()
    assert parse_tzif(target.read_bytes(), name="Zone").types[0].abbrev == "UTC"


def test_writer_rejects_an_empty_type_table() -> None:
    with pytest.raises(TzifWriteError, match="at least one local-time type"):
        build_tzif([], [])


def test_writer_rejects_unsorted_transitions() -> None:
    types = [TTInfo(0, False, "AAA")]
    with pytest.raises(TzifWriteError, match="strictly ascend"):
        build_tzif(types, [Transition(2000, 0), Transition(1000, 0)])


def test_writer_rejects_a_dangling_type_index() -> None:
    with pytest.raises(TzifWriteError, match="references type"):
        build_tzif([TTInfo(0, False, "AAA")], [Transition(1000, 3)])


def test_writer_rejects_unsupported_version() -> None:
    with pytest.raises(TzifWriteError, match="unsupported output version"):
        build_tzif([TTInfo(0, False, "AAA")], [], version=1)


def test_duplicate_abbreviations_share_designation_bytes() -> None:
    """Two types with the same label must not bloat the designation block."""
    types = [TTInfo(0, False, "SAME"), TTInfo(3600, True, "SAME")]
    data = build_tzif(types, [], footer="SAME0")
    rules = parse_tzif(data, name="Test/Shared")
    assert [t.abbrev for t in rules.types] == ["SAME", "SAME"]
    assert data.count(b"SAME\x00") == 2  # Once per block, not once per type.


def test_transitions_outside_int32_survive_in_the_64_bit_block() -> None:
    """A far-future transition must be dropped from the v1 block but kept in v2."""
    far = civil_to_instant(2200, 6, 1)
    assert far > 2**31 - 1
    types = [TTInfo(0, False, "AAA"), TTInfo(3600, True, "BBB")]
    data = build_tzif(types, [Transition(far, 1)], footer="AAA0")
    rules = parse_tzif(data, name="Test/Far")
    assert [record.instant for record in rules.transitions] == [far]
    assert rules.lookup(far + SECONDS_PER_DAY).abbrev == "AAA"  # Footer governs.
