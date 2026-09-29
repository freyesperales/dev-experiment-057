"""Tests for the blast-radius analysis over caller-supplied readings."""

from __future__ import annotations

from pathlib import Path

import pytest

from gnomon.blast import (
    WallRow,
    analyse_rows,
    emit_correction_sql,
    read_wall_rows,
)
from gnomon.diverge import Severity
from gnomon.errors import InputDataError
from gnomon.rules import TTInfo, Transition, ZoneRules
from gnomon.timeutil import civil_to_instant
from gnomon.tzif import parse_tzif
from gnomon.tzifwrite import build_tzif

from conftest import abolished_dst_zone, fixed_zone, na_dst_zone

NOW = civil_to_instant(2026, 9, 29)


def _row(zone: str, year: int, month: int, day: int, hour: int, minute: int = 0,
         row_id: str = "r1") -> WallRow:
    """Build one reading."""
    return WallRow(
        row_id=row_id,
        zone=zone,
        year=year,
        month=month,
        day=day,
        seconds_of_day=hour * 3600 + minute * 60,
    )


def _morocco(*, permanent_utc: bool, ramadan_end_day: int) -> ZoneRules:
    """A negative-DST zone, optionally settling on permanent UTC."""
    types = [TTInfo(3600, False, "+01"), TTInfo(0, True, "+00")]
    records = [
        Transition(civil_to_instant(2026, 2, 15, 3 * 3600) - 3600, 1),
        Transition(civil_to_instant(2026, 3, ramadan_end_day, 2 * 3600), 0),
    ]
    footer = "<+01>-1"
    if permanent_utc:
        records.append(Transition(civil_to_instant(2026, 9, 20, 2 * 3600) - 3600, 1))
        footer = "<+00>0"
    return parse_tzif(
        build_tzif(types, records, footer=footer), name="Africa/Casablanca"
    )


@pytest.fixture
def pair() -> tuple[dict[str, ZoneRules], dict[str, ZoneRules]]:
    """A stale and an authoritative database covering every failure class."""
    left = {
        "America/Vancouver": parse_tzif(na_dst_zone(), name="America/Vancouver"),
        "Africa/Casablanca": _morocco(permanent_utc=False, ramadan_end_day=22),
        "Europe/Madrid": parse_tzif(fixed_zone(3600, "CET", "CET-1"), name="Europe/Madrid"),
    }
    right = {
        "America/Vancouver": parse_tzif(abolished_dst_zone(), name="America/Vancouver"),
        "Africa/Casablanca": _morocco(permanent_utc=True, ramadan_end_day=29),
        "Europe/Madrid": parse_tzif(fixed_zone(3600, "CET", "CET-1"), name="Europe/Madrid"),
        "America/Coyhaique": parse_tzif(fixed_zone(-10800, "-03", "<-03>3"),
                                       name="America/Coyhaique"),
    }
    return left, right


def _classify(pair, row: WallRow) -> tuple[str, int, Severity]:
    """Run one row and return its class, correction and severity."""
    left, right = pair
    report = analyse_rows(
        [row], left, right, left_label="2025b", right_label="2026d", now=NOW
    )
    finding = report.findings[0]
    return finding.classification, finding.delta, finding.severity


def test_agreeing_row_is_clean(pair) -> None:
    # Midsummer: both databases say -07:00.
    assert _classify(pair, _row("America/Vancouver", 2026, 6, 15, 9)) == (
        "agree", 0, Severity.INFO
    )


def test_future_shift_is_critical(pair) -> None:
    """A winter booking in a province that stopped falling back."""
    classification, delta, severity = _classify(
        pair, _row("America/Vancouver", 2026, 12, 15, 9)
    )
    assert classification == "shifted"
    assert delta == -3600  # The stored instant is an hour too late.
    assert severity is Severity.CRITICAL


def test_past_shift_is_only_medium(pair) -> None:
    """The same disagreement in the past changes display, not what is scheduled."""
    classification, _, severity = _classify(
        pair, _row("Africa/Casablanca", 2026, 3, 25, 9)
    )
    assert classification == "shifted"
    assert severity is Severity.MEDIUM


def test_gap_appears_when_a_transition_moves_later(pair) -> None:
    """Valid under the old rules, inside a spring-forward gap under the new ones."""
    classification, delta, severity = _classify(
        pair, _row("Africa/Casablanca", 2026, 3, 29, 2, 30)
    )
    assert classification == "gap-appears"
    assert delta == 3600
    assert severity is Severity.HIGH


def test_gap_resolves_when_a_transition_moves_later(pair) -> None:
    classification, _, severity = _classify(
        pair, _row("Africa/Casablanca", 2026, 3, 22, 2, 30)
    )
    assert classification == "gap-resolves"
    assert severity is Severity.LOW


def test_fold_appears_when_clocks_start_going_back(pair) -> None:
    classification, _, severity = _classify(
        pair, _row("Africa/Casablanca", 2026, 9, 20, 1, 30)
    )
    assert classification == "fold-appears"
    assert severity is Severity.MEDIUM


def test_fold_resolves_when_dst_is_abolished(pair) -> None:
    """The fall-back hour the stale database believes in stops being ambiguous."""
    classification, delta, severity = _classify(
        pair, _row("America/Vancouver", 2026, 11, 1, 1, 30)
    )
    assert classification == "fold-resolves"
    assert delta == 0
    assert severity is Severity.LOW


def test_missing_zone_is_high(pair) -> None:
    classification, _, severity = _classify(
        pair, _row("America/Coyhaique", 2027, 3, 1, 9)
    )
    assert classification == "zone-missing"
    assert severity is Severity.HIGH


def test_zone_unknown_to_both_is_recorded(pair) -> None:
    left, right = pair
    report = analyse_rows(
        [_row("Mars/Olympus", 2027, 1, 1, 9)],
        left,
        right,
        left_label="a",
        right_label="b",
        now=NOW,
    )
    assert report.unknown_zones == ("Mars/Olympus",)
    assert report.findings[0].classification == "zone-missing"


def test_counts_and_ordering(pair) -> None:
    left, right = pair
    rows = [
        _row("America/Vancouver", 2026, 6, 15, 9, row_id="clean"),
        _row("America/Vancouver", 2026, 12, 15, 9, row_id="broken"),
        _row("America/Vancouver", 2026, 11, 1, 1, 30, row_id="was-ambiguous"),
    ]
    report = analyse_rows(rows, left, right, left_label="a", right_label="b", now=NOW)
    assert report.counts() == {"agree": 1, "shifted": 1, "fold-resolves": 1}
    assert report.max_severity is Severity.CRITICAL
    # Worst first: the critical shift, then the low-severity fold.
    assert [f.row.row_id for f in report.affected] == ["broken", "was-ambiguous"]


def test_needs_human_review_excludes_plain_shifts(pair) -> None:
    left, right = pair
    rows = [
        _row("America/Vancouver", 2026, 12, 15, 9, row_id="shift"),
        _row("Africa/Casablanca", 2026, 3, 29, 2, 30, row_id="gap"),
        _row("Africa/Casablanca", 2026, 9, 20, 1, 30, row_id="fold"),
    ]
    report = analyse_rows(rows, left, right, left_label="a", right_label="b", now=NOW)
    assert {f.row.row_id for f in report.needs_human_review()} == {"gap", "fold"}


def test_corrections_are_grouped_by_delta(pair) -> None:
    left, right = pair
    rows = [
        _row("America/Vancouver", 2026, 12, 15, 9, row_id="a"),
        _row("America/Vancouver", 2027, 1, 20, 9, row_id="b"),
        _row("Africa/Casablanca", 2026, 12, 1, 9, row_id="c"),
    ]
    report = analyse_rows(rows, left, right, left_label="a", right_label="b", now=NOW)
    grouped = report.corrections_by_delta()
    assert sorted(grouped) == [-3600, 3600]
    assert {f.row.row_id for f in grouped[-3600]} == {"a", "b"}
    assert {f.row.row_id for f in grouped[3600]} == {"c"}


# ---- SQL emission --------------------------------------------------------


def test_sql_groups_updates_and_states_its_assumption(pair) -> None:
    left, right = pair
    rows = [
        _row("America/Vancouver", 2026, 12, 15, 9, row_id="a"),
        _row("America/Vancouver", 2027, 1, 20, 9, row_id="b"),
    ]
    report = analyse_rows(rows, left, right, left_label="2025b", right_label="2026d", now=NOW)
    sql = emit_correction_sql(
        report, table="bookings", instant_column="starts_at", id_column="id"
    )
    assert "UPDATE bookings SET starts_at = starts_at + INTERVAL '-3600 seconds'" in sql
    assert "'a', 'b'" in sql
    assert "2025b" in sql and "2026d" in sql
    assert "does not fix the cause" in sql
    assert sql.count("UPDATE") == 1
    assert "BEGIN;" in sql and "COMMIT;" in sql


def test_sql_escapes_quotes_in_identifiers(pair) -> None:
    left, right = pair
    row = WallRow(row_id="o'brien", zone="America/Vancouver", year=2026, month=12,
                  day=15, seconds_of_day=9 * 3600)
    report = analyse_rows([row], left, right, left_label="a", right_label="b", now=NOW)
    sql = emit_correction_sql(report, table="t", instant_column="c", id_column="id")
    assert "'o''brien'" in sql


def test_sql_can_leave_identifiers_unquoted(pair) -> None:
    left, right = pair
    row = WallRow(row_id="4711", zone="America/Vancouver", year=2026, month=12, day=15,
                  seconds_of_day=9 * 3600)
    report = analyse_rows([row], left, right, left_label="a", right_label="b", now=NOW)
    sql = emit_correction_sql(
        report, table="t", instant_column="c", id_column="id", quote_ids=False
    )
    assert "IN (4711)" in sql


def test_sql_chunks_large_id_lists(pair) -> None:
    left, right = pair
    rows = [
        _row("America/Vancouver", 2026, 12, 15, 9, row_id=f"id-{n}") for n in range(7)
    ]
    report = analyse_rows(rows, left, right, left_label="a", right_label="b", now=NOW)
    sql = emit_correction_sql(
        report, table="t", instant_column="c", id_column="id", max_ids_per_statement=3
    )
    assert sql.count("UPDATE") == 3  # 3 + 3 + 1


def test_sql_says_so_when_nothing_needs_repair(pair) -> None:
    left, right = pair
    report = analyse_rows(
        [_row("Europe/Madrid", 2027, 6, 1, 10)],
        left,
        right,
        left_label="a",
        right_label="b",
        now=NOW,
    )
    sql = emit_correction_sql(report, table="t", instant_column="c", id_column="id")
    assert "No rows require a correction." in sql
    assert "UPDATE" not in sql


def test_sql_lists_rows_arithmetic_cannot_settle(pair) -> None:
    left, right = pair
    rows = [_row("Africa/Casablanca", 2026, 9, 20, 1, 30, row_id="ambiguous")]
    report = analyse_rows(rows, left, right, left_label="a", right_label="b", now=NOW)
    sql = emit_correction_sql(report, table="t", instant_column="c", id_column="id")
    assert "cannot be settled by arithmetic alone" in sql
    assert "ambiguous" in sql


# ---- CSV input -----------------------------------------------------------


def _write_csv(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_columns_are_inferred_from_common_names(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "rows.csv",
        "id,zone,local_time\n1,America/Vancouver,2026-12-15 09:00:00\n",
    )
    rows, mapping = read_wall_rows(source)
    assert mapping.id_col == "id"
    assert mapping.zone_col == "zone"
    assert mapping.wall_col == "local_time"
    assert rows[0].zone == "America/Vancouver"
    assert rows[0].seconds_of_day == 9 * 3600


def test_split_date_and_time_columns(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "rows.csv",
        "pk,tzid,date,time\nx,Europe/Madrid,2027-06-01,10:30\n",
    )
    rows, mapping = read_wall_rows(source)
    assert mapping.wall_col is None
    assert (mapping.date_col, mapping.time_col) == ("date", "time")
    assert rows[0].seconds_of_day == 10 * 3600 + 30 * 60


def test_explicit_column_names_win(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "rows.csv",
        "a,b,c\n7,Europe/Madrid,2027-06-01 10:00\n",
    )
    rows, mapping = read_wall_rows(source, id_col="a", zone_col="b", wall_col="c")
    assert mapping.id_col == "a"
    assert rows[0].row_id == "7"


def test_utf8_bom_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "rows.csv"
    path.write_bytes(
        b"\xef\xbb\xbfid,zone,local_time\n1,Europe/Madrid,2027-06-01 10:00\n"
    )
    rows, mapping = read_wall_rows(path)
    assert mapping.id_col == "id"
    assert len(rows) == 1


def test_ragged_row_is_reported_by_line(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "rows.csv",
        "id,zone,local_time\n1,Europe/Madrid,2027-06-01 10:00\n2,Europe/Madrid,oops,extra\n",
    )
    with pytest.raises(InputDataError, match="line 3.*4 fields but the header declares 3"):
        read_wall_rows(source)


def test_unparseable_stamp_is_reported_by_line(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "rows.csv", "id,zone,local_time\n1,Europe/Madrid,not-a-date\n"
    )
    with pytest.raises(InputDataError, match="line 2"):
        read_wall_rows(source)


def test_zoned_stamp_is_rejected(tmp_path: Path) -> None:
    """An offset in the data would hide the very bug being looked for."""
    source = _write_csv(
        tmp_path / "rows.csv", "id,zone,local_time\n1,Europe/Madrid,2027-06-01T10:00:00Z\n"
    )
    with pytest.raises(InputDataError, match="UTC designator"):
        read_wall_rows(source)


def test_offset_bearing_stamp_is_rejected(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "rows.csv", "id,zone,local_time\n1,Europe/Madrid,2027-06-01T10:00:00+02:00\n"
    )
    with pytest.raises(InputDataError, match="UTC offset"):
        read_wall_rows(source)


def test_empty_zone_cell_is_rejected(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "rows.csv", "id,zone,local_time\n1,,2027-06-01 10:00\n"
    )
    with pytest.raises(InputDataError, match="empty 'zone'"):
        read_wall_rows(source)


def test_unknown_explicit_column_lists_the_available_ones(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "rows.csv", "id,zone,local_time\n1,Europe/Madrid,2027-06-01 10:00\n"
    )
    with pytest.raises(InputDataError, match="no column 'nope'.*available: id, zone"):
        read_wall_rows(source, wall_col="nope")


def test_uninferable_columns_are_an_error(tmp_path: Path) -> None:
    source = _write_csv(tmp_path / "rows.csv", "alpha,beta\n1,2\n")
    with pytest.raises(InputDataError, match="cannot infer"):
        read_wall_rows(source)


def test_header_without_rows_is_an_error(tmp_path: Path) -> None:
    source = _write_csv(tmp_path / "rows.csv", "id,zone,local_time\n")
    with pytest.raises(InputDataError, match="no data rows"):
        read_wall_rows(source)


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(InputDataError, match="cannot read"):
        read_wall_rows(tmp_path / "absent.csv")


def test_row_without_an_id_falls_back_to_its_line(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "rows.csv", "id,zone,local_time\n,Europe/Madrid,2027-06-01 10:00\n"
    )
    rows, _ = read_wall_rows(source)
    assert rows[0].row_id == "line-2"


def test_date_only_stamp_is_midnight(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "rows.csv", "id,zone,local_time\n1,Europe/Madrid,2027-06-01\n"
    )
    rows, _ = read_wall_rows(source)
    assert rows[0].seconds_of_day == 0


def test_wall_row_renders_back_to_its_reading() -> None:
    assert _row("Europe/Madrid", 2027, 6, 1, 10, 30).render() == "2027-06-01 10:30:00"
