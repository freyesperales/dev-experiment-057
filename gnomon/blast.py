"""Blast radius: which of *your* rows hold the wrong instant, and by how much.

A tree diff tells you two databases disagree. It does not tell you whether you
care. You care exactly to the extent that your application converted local wall
clocks into instants through the stale database and stored the results -- which
is what ``(date + time) AT TIME ZONE tz`` does, and it is the documented way to
schedule from user input.

This module takes a table of the wall-clock readings your users actually
entered, runs each one through both databases' inverse maps, and classifies the
outcome. Rows that come out identical are noise; rows that come out an hour apart
are silent data corruption already sitting in your table, and the correction is
a known constant, so it can be emitted as SQL.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from .diverge import Severity
from .errors import InputDataError
from .rules import WallResolution, ZoneRules
from .timeutil import (
    civil_to_instant,
    format_delta,
    format_instant,
    format_offset,
    parse_naive,
)

RowClass = Literal[
    "agree",
    "shifted",
    "gap-appears",
    "gap-resolves",
    "fold-appears",
    "fold-resolves",
    "zone-missing",
]

#: Human-facing explanation of each class, printed once per report so the
#: classification is auditable rather than a bare label.
CLASS_NOTES: dict[RowClass, str] = {
    "agree": "Both databases map this reading to the same instant.",
    "shifted": (
        "Both databases accept the reading but resolve it to different instants. "
        "This is the silent corruption case: the row already holds the wrong value."
    ),
    "gap-appears": (
        "The reading exists in the old database but falls in a spring-forward gap "
        "in the new one. Postgres does not raise here; it shifts the value."
    ),
    "gap-resolves": (
        "The reading fell in a gap under the old database and is valid under the "
        "new one, so the stored value was a shifted approximation."
    ),
    "fold-appears": (
        "The reading becomes ambiguous under the new database: two instants denote "
        "it, and the conversion silently picks one, so half of these are wrong."
    ),
    "fold-resolves": (
        "The reading was ambiguous under the old database and is now unique."
    ),
    "zone-missing": (
        "The zone is absent from one of the databases, so the conversion errors or "
        "silently falls back to another zone."
    ),
}


@dataclass(frozen=True, slots=True)
class WallRow:
    """One wall-clock reading drawn from caller-supplied data."""

    row_id: str
    zone: str
    year: int
    month: int
    day: int
    seconds_of_day: int
    #: 1-based line number in the source file, for error messages.
    line: int = 0

    @property
    def naive_instant(self) -> int:
        """The reading encoded as an integer by pretending it is UTC."""
        return civil_to_instant(self.year, self.month, self.day, self.seconds_of_day)

    def render(self) -> str:
        """Render the reading back as a wall-clock stamp."""
        hours, rem = divmod(self.seconds_of_day, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d} {hours:02d}:{minutes:02d}:{secs:02d}"


@dataclass(frozen=True, slots=True)
class RowFinding:
    """The verdict for one row across both databases."""

    row: WallRow
    classification: RowClass
    left: WallResolution | None
    right: WallResolution | None
    is_future: bool
    detail: str = ""

    @property
    def delta(self) -> int:
        """Correction in seconds to apply to the stored instant, right minus left."""
        if self.left is None or self.right is None:
            return 0
        return self.right.chosen - self.left.chosen

    @property
    def severity(self) -> Severity:
        """Operational severity of this row."""
        if self.classification == "zone-missing":
            return Severity.HIGH
        if self.classification == "shifted":
            return Severity.CRITICAL if self.is_future else Severity.MEDIUM
        if self.classification == "gap-appears":
            return Severity.HIGH
        if self.classification == "fold-appears":
            return Severity.MEDIUM
        if self.classification in ("gap-resolves", "fold-resolves"):
            return Severity.LOW
        return Severity.INFO

    @property
    def needs_correction(self) -> bool:
        """True when a stored instant should be moved."""
        return self.classification != "agree" and self.delta != 0

    def describe(self) -> str:
        """One-line rendering for the text report."""
        parts = [
            f"{self.row.row_id}  {self.row.render()}  {self.row.zone}",
            f"[{self.severity.label}] {self.classification}",
        ]
        if self.left is not None and self.right is not None:
            parts.append(
                f"stored {format_instant(self.left.chosen)} "
                f"({format_offset(self.left.chosen_utoff)} {self.left.chosen_abbrev})"
                f" -> correct {format_instant(self.right.chosen)} "
                f"({format_offset(self.right.chosen_utoff)} {self.right.chosen_abbrev})"
                f"  correction {format_delta(self.delta)}"
            )
        if self.detail:
            parts.append(self.detail)
        return "  ".join(parts)


@dataclass(frozen=True, slots=True)
class BlastReport:
    """The full blast-radius analysis of a table of readings."""

    findings: tuple[RowFinding, ...]
    left_label: str
    right_label: str
    now: int
    #: Zones referenced by the data that neither database resolves.
    unknown_zones: tuple[str, ...] = ()

    @property
    def affected(self) -> tuple[RowFinding, ...]:
        """Findings that are not clean agreement, worst first."""
        return tuple(
            sorted(
                (f for f in self.findings if f.classification != "agree"),
                key=lambda f: (-int(f.severity), f.row.naive_instant, f.row.row_id),
            )
        )

    @property
    def max_severity(self) -> Severity:
        """Highest severity across all rows."""
        return max((f.severity for f in self.findings), default=Severity.INFO)

    def counts(self) -> dict[str, int]:
        """Row counts per classification, in a stable order."""
        tally: dict[str, int] = {key: 0 for key in CLASS_NOTES}
        for finding in self.findings:
            tally[finding.classification] += 1
        return {key: value for key, value in tally.items() if value}

    def needs_human_review(self) -> tuple[RowFinding, ...]:
        """Rows an arithmetic correction cannot honestly settle.

        Shifting a stored instant by a constant is the right repair when both
        databases agree that the reading denotes *some* instant and merely
        disagree about which. It is not the right repair when the reading has
        become ambiguous or has stopped existing: then the stored value can be
        made consistent with the fixed server, but the original intent -- what
        the person entering "02:30" actually meant -- is no longer recoverable
        from the data. Those rows need asking, not arithmetic.
        """
        return tuple(
            finding
            for finding in self.affected
            if finding.classification in ("gap-appears", "fold-appears", "zone-missing")
        )

    def corrections_by_delta(self) -> dict[int, list[RowFinding]]:
        """Group correctable rows by the constant they need, largest group first."""
        grouped: dict[int, list[RowFinding]] = {}
        for finding in self.findings:
            if finding.needs_correction:
                grouped.setdefault(finding.delta, []).append(finding)
        return dict(sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])))


def analyse_rows(
    rows: Sequence[WallRow],
    left_zones: dict[str, ZoneRules],
    right_zones: dict[str, ZoneRules],
    *,
    left_label: str,
    right_label: str,
    now: int,
) -> BlastReport:
    """Classify every row by running it through both databases' inverse maps."""
    findings: list[RowFinding] = []
    unknown: set[str] = set()

    for row in rows:
        left_rules = left_zones.get(row.zone)
        right_rules = right_zones.get(row.zone)
        if left_rules is None or right_rules is None:
            missing = []
            if left_rules is None:
                missing.append(left_label)
            if right_rules is None:
                missing.append(right_label)
            if left_rules is None and right_rules is None:
                unknown.add(row.zone)
            findings.append(
                RowFinding(
                    row=row,
                    classification="zone-missing",
                    left=None,
                    right=None,
                    is_future=row.naive_instant >= now,
                    detail=f"zone absent from: {', '.join(missing)}",
                )
            )
            continue

        naive = row.naive_instant
        left = left_rules.resolve_wall(naive)
        right = right_rules.resolve_wall(naive)
        classification = _classify(left, right)
        findings.append(
            RowFinding(
                row=row,
                classification=classification,
                left=left,
                right=right,
                is_future=right.chosen >= now,
                detail=_detail(left, right, classification),
            )
        )

    return BlastReport(
        findings=tuple(findings),
        left_label=left_label,
        right_label=right_label,
        now=now,
        unknown_zones=tuple(sorted(unknown)),
    )


def _classify(left: WallResolution, right: WallResolution) -> RowClass:
    """Decide the class of a row from the two resolutions."""
    if right.multiplicity == "gap" and left.multiplicity != "gap":
        return "gap-appears"
    if left.multiplicity == "gap" and right.multiplicity != "gap":
        return "gap-resolves"
    if right.multiplicity == "fold" and left.multiplicity != "fold":
        return "fold-appears"
    if left.multiplicity == "fold" and right.multiplicity != "fold":
        return "fold-resolves"
    if left.chosen != right.chosen:
        return "shifted"
    return "agree"


def _detail(left: WallResolution, right: WallResolution, classification: RowClass) -> str:
    """Extra context worth printing for the trickier classes."""
    if classification == "gap-appears":
        return f"gap is {format_delta(right.gap_shift)} wide; value is pushed forward"
    if classification == "gap-resolves":
        return f"old gap was {format_delta(left.gap_shift)} wide"
    if classification == "fold-appears":
        others = ", ".join(format_instant(i) for i in right.instants[1:])
        return f"also denotes {others}; the conversion picks the earlier one"
    if classification == "fold-resolves":
        others = ", ".join(format_instant(i) for i in left.instants[1:])
        return f"previously also denoted {others}"
    return ""


# ---- input ---------------------------------------------------------------

#: Key under which `csv.DictReader` parks fields beyond the declared header.
_EXTRA_KEY = "__gnomon_surplus_fields__"

#: Column names tried when the caller does not name them explicitly.
_ID_CANDIDATES = ("id", "row_id", "pk", "uuid", "key")
_ZONE_CANDIDATES = ("zone", "tz", "timezone", "time_zone", "tzid", "zone_name")
_WALL_CANDIDATES = ("wall", "local", "local_time", "wall_time", "starts_at_local", "when")
_DATE_CANDIDATES = ("date", "local_date", "day")
_TIME_CANDIDATES = ("time", "local_time_of_day", "clock")


@dataclass(frozen=True, slots=True)
class ColumnMap:
    """Which CSV columns hold the pieces of a reading."""

    id_col: str
    zone_col: str
    wall_col: str | None = None
    date_col: str | None = None
    time_col: str | None = None


def read_wall_rows(
    path: str | Path,
    *,
    id_col: str | None = None,
    zone_col: str | None = None,
    wall_col: str | None = None,
    date_col: str | None = None,
    time_col: str | None = None,
) -> tuple[list[WallRow], ColumnMap]:
    """Read wall-clock readings from a CSV file.

    Any column names not supplied are inferred from a small list of common
    spellings. Inference failures are errors naming the available headers,
    because a blast-radius report built on the wrong column is worse than none.

    Raises:
        InputDataError: on a missing file, absent headers, or unparseable cells.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise InputDataError(f"cannot read {source}: {exc}") from exc

    # restkey/restval are named so a ragged row is caught and reported by line
    # number rather than crashing deep inside the row loop.
    reader = csv.DictReader(text.splitlines(), restkey=_EXTRA_KEY, restval="")
    if reader.fieldnames is None:
        raise InputDataError(f"{source} has no header row")
    headers = [name.strip() for name in reader.fieldnames]

    resolved_id = _pick(headers, id_col, _ID_CANDIDATES, "row identifier", source)
    resolved_zone = _pick(headers, zone_col, _ZONE_CANDIDATES, "zone name", source)
    resolved_wall: str | None = None
    resolved_date: str | None = None
    resolved_time: str | None = None

    if wall_col or not (date_col or time_col):
        try:
            resolved_wall = _pick(
                headers, wall_col, _WALL_CANDIDATES, "wall-clock stamp", source
            )
        except InputDataError:
            if wall_col:
                raise
            resolved_wall = None
    if resolved_wall is None:
        resolved_date = _pick(headers, date_col, _DATE_CANDIDATES, "local date", source)
        resolved_time = _pick(headers, time_col, _TIME_CANDIDATES, "local time", source)

    mapping = ColumnMap(
        id_col=resolved_id,
        zone_col=resolved_zone,
        wall_col=resolved_wall,
        date_col=resolved_date,
        time_col=resolved_time,
    )

    rows: list[WallRow] = []
    for line, record in enumerate(reader, start=2):
        extra = record.pop(_EXTRA_KEY, None)
        if extra:
            raise InputDataError(
                f"{source} line {line}: row has {len(headers) + len(extra)} fields but the "
                f"header declares {len(headers)}. Unquoted commas inside a value are the "
                f"usual cause; surplus values were: {', '.join(map(str, extra))}"
            )
        cleaned = {
            (key.strip() if key else ""): (value or "").strip()
            for key, value in record.items()
        }
        zone = cleaned.get(mapping.zone_col, "")
        if not zone:
            raise InputDataError(f"{source} line {line}: empty {mapping.zone_col!r}")
        if mapping.wall_col is not None:
            stamp = cleaned.get(mapping.wall_col, "")
        else:
            assert mapping.date_col is not None and mapping.time_col is not None
            date_part = cleaned.get(mapping.date_col, "")
            time_part = cleaned.get(mapping.time_col, "")
            stamp = f"{date_part} {time_part}".strip()
        try:
            year, month, day, seconds = parse_naive(stamp)
        except ValueError as exc:
            raise InputDataError(f"{source} line {line}: {exc}") from exc
        rows.append(
            WallRow(
                row_id=cleaned.get(mapping.id_col, "") or f"line-{line}",
                zone=zone,
                year=year,
                month=month,
                day=day,
                seconds_of_day=seconds,
                line=line,
            )
        )

    if not rows:
        raise InputDataError(f"{source} contains a header but no data rows")
    return rows, mapping


def _pick(
    headers: Sequence[str],
    explicit: str | None,
    candidates: Sequence[str],
    what: str,
    source: Path,
) -> str:
    """Resolve one column name, explicitly or by inference."""
    if explicit:
        if explicit not in headers:
            raise InputDataError(
                f"{source} has no column {explicit!r}; available: {', '.join(headers)}"
            )
        return explicit
    lowered = {name.lower(): name for name in headers}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    raise InputDataError(
        f"cannot infer the {what} column of {source}; name it explicitly. "
        f"Available columns: {', '.join(headers)}"
    )


def emit_correction_sql(
    report: BlastReport,
    *,
    table: str,
    instant_column: str,
    id_column: str,
    quote_ids: bool = True,
    max_ids_per_statement: int = 500,
) -> str:
    """Render corrective SQL for rows whose stored instant is wrong.

    The correction assumes the stored value was produced by the *left* (stale)
    database and that the right-hand database is authoritative. That assumption
    is stated in the output, because applying this to rows written by a
    third database would move them to a third wrong answer.
    """
    grouped = report.corrections_by_delta()
    lines: list[str] = [
        "-- Generated by gnomon. Review before running.",
        f"-- Stored values are assumed to have been written through: {report.left_label}",
        f"-- Authoritative database: {report.right_label}",
        "--",
        "-- This repairs rows that were already written. It does not fix the cause:",
        "-- rebuild or repoint the server at current tzdata, or every future write",
        "-- reintroduces the same error.",
        "",
    ]
    review = report.needs_human_review()
    if review:
        lines.append(
            f"-- {len(review)} row(s) below cannot be settled by arithmetic alone: the"
        )
        lines.append(
            "-- reading is now ambiguous, has no instant at all, or names an unknown"
        )
        lines.append(
            "-- zone. Any UPDATE for them only matches what the fixed server would"
        )
        lines.append("-- compute; it does not recover what the user meant. Confirm these:")
        for finding in review:
            lines.append(
                f"--   {finding.row.row_id}  {finding.row.render()}  {finding.row.zone}  "
                f"({finding.classification})"
            )
        lines.append("")

    if not grouped:
        lines.append("-- No rows require a correction.")
        return "\n".join(lines) + "\n"

    lines.append("BEGIN;")
    for delta, findings in grouped.items():
        lines.append("")
        lines.append(
            f"-- {len(findings)} row(s) need {format_delta(delta)} "
            f"({delta:+d} seconds); classes: "
            + ", ".join(sorted({f.classification for f in findings}))
        )
        identifiers = [f.row.row_id for f in findings]
        for chunk_start in range(0, len(identifiers), max_ids_per_statement):
            chunk = identifiers[chunk_start : chunk_start + max_ids_per_statement]
            rendered = ", ".join(_sql_literal(value, quote_ids) for value in chunk)
            lines.append(
                f"UPDATE {table} SET {instant_column} = {instant_column} "
                f"+ INTERVAL '{delta} seconds' WHERE {id_column} IN ({rendered});"
            )
    lines.append("")
    lines.append("-- Verify the row count matches the comment above, then:")
    lines.append("-- COMMIT;   (or ROLLBACK; if it does not)")
    return "\n".join(lines) + "\n"


def _sql_literal(value: str, quote: bool) -> str:
    """Render an identifier as a SQL literal, escaping single quotes."""
    if not quote:
        return value
    return "'" + value.replace("'", "''") + "'"
