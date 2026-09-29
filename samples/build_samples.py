#!/usr/bin/env python3
"""Build two synthetic zoneinfo trees that reproduce the 2025b/2026d incident.

These are *synthetic approximations*, not IANA releases. They were hand-built so
that this repository stays small, has no third-party binaries in it, and runs on
a machine with no tz database at all. The offsets, abbreviations and transition
dates follow the real 2026 changes closely enough to reproduce the failure modes
being demonstrated, and no further:

* ``America/Vancouver`` -- British Columbia stopped changing its clocks in 2026
  and settled on permanent -07:00, relabelled MST. A 2025-vintage database still
  believes in a fall-back on 2026-11-01, so from that date the two disagree by an
  hour, for ever. This is the headline case.
* ``America/Edmonton`` -- Alberta likewise settled on permanent -06:00 (CST).
  Both databases agree through the summer and split on 2026-11-01.
* ``Canada/Pacific`` -- a link to Vancouver, present so alias collapsing is
  exercised by the sample and not only by the tests.
* ``Africa/Casablanca`` -- Morocco moved to permanent UTC on 2026-09-20, so this
  divergence has *already started*. Its Ramadan-window transition also shifts by
  a week between the two vintages, which is what produces readings that exist in
  one database and fall in a gap in the other.
* ``Europe/Chisinau`` -- differs only in a historical transition. Included to show
  that gnomon separates "changes how old rows display" from "writes new rows
  wrong".
* ``America/Whitehorse`` -- same offset in both, different abbreviation. The
  control for "byte-different, behaviourally identical".
* ``Europe/Madrid`` -- identical in both. The control for a clean result.
* ``America/Coyhaique`` -- present only in the newer tree, as new zones regularly
  are.

Run ``python samples/build_samples.py`` to (re)generate the trees.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gnomon.posixtz import TransitionRule  # noqa: E402
from gnomon.rules import TTInfo, Transition  # noqa: E402
from gnomon.timeutil import civil_to_instant  # noqa: E402
from gnomon.tzifwrite import write_tzif_file  # noqa: E402

SAMPLES = Path(__file__).resolve().parent
OLD_TREE = SAMPLES / "trees" / "tzdata-2025b"
NEW_TREE = SAMPLES / "trees" / "tzdata-2026d"

# North American DST rules in force before the 2026 changes: second Sunday in
# March and first Sunday in November, both at 02:00 local.
_NA_DST_START = TransitionRule(kind="M", month=3, week=2, weekday=0, time=2 * 3600)
_NA_DST_END = TransitionRule(kind="M", month=11, week=1, weekday=0, time=2 * 3600)

# EU rules: last Sunday in March and October, at 01:00 UTC.
_EU_DST_START = TransitionRule(kind="M", month=3, week=5, weekday=0, time=2 * 3600)
_EU_DST_END = TransitionRule(kind="M", month=10, week=5, weekday=0, time=3 * 3600)


def _na_cycle(
    std_utoff: int,
    dst_utoff: int,
    first_year: int,
    last_year: int,
    *,
    stop_after: int | None = None,
) -> list[Transition]:
    """Generate a North American spring/autumn cycle as transition records.

    Type index 0 is standard time and 1 is DST, matching how the type tables
    below are ordered. `stop_after` truncates the list at an instant, which is
    how a tree that abolished DST mid-series is expressed: the transitions up to
    the change are real history and stay, and the footer takes over afterwards.
    """
    records: list[Transition] = []
    for year in range(first_year, last_year + 1):
        records.append(Transition(_NA_DST_START.instant(year, std_utoff), 1))
        records.append(Transition(_NA_DST_END.instant(year, dst_utoff), 0))
    records.sort(key=lambda record: record.instant)
    if stop_after is not None:
        records = [record for record in records if record.instant <= stop_after]
    return records


def _eu_cycle(std_utoff: int, dst_utoff: int, first_year: int, last_year: int) -> list[Transition]:
    """Generate an EU spring/autumn cycle as transition records."""
    records: list[Transition] = []
    for year in range(first_year, last_year + 1):
        records.append(Transition(_EU_DST_START.instant(year, std_utoff), 1))
        records.append(Transition(_EU_DST_END.instant(year, dst_utoff), 0))
    return sorted(records, key=lambda record: record.instant)


def build_old_tree(root: Path) -> None:
    """Write the 2025-vintage tree: nothing knows about the 2026 changes yet."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "+VERSION").write_text("2025b\n", encoding="ascii")

    pacific = [TTInfo(-28800, False, "PST"), TTInfo(-25200, True, "PDT")]
    vancouver = _na_cycle(-28800, -25200, 2020, 2037)
    write_tzif_file(root / "America" / "Vancouver", pacific, vancouver,
                    footer="PST8PDT,M3.2.0,M11.1.0")
    write_tzif_file(root / "Canada" / "Pacific", pacific, vancouver,
                    footer="PST8PDT,M3.2.0,M11.1.0")

    mountain = [TTInfo(-25200, False, "MST"), TTInfo(-21600, True, "MDT")]
    write_tzif_file(root / "America" / "Edmonton", mountain,
                    _na_cycle(-25200, -21600, 2020, 2037),
                    footer="MST7MDT,M3.2.0,M11.1.0")

    # Yukon: permanent -07:00, but labelled with a numeric abbreviation in this
    # vintage. Same behaviour as the newer tree, different label.
    write_tzif_file(root / "America" / "Whitehorse", [TTInfo(-25200, False, "-07")], [],
                    footer="<-07>7")

    _write_casablanca(root, ramadan_end_day=22, permanent_utc=False)
    _write_madrid(root)
    _write_chisinau(root, autumn_local_hour=3)


def build_new_tree(root: Path) -> None:
    """Write the 2026-vintage tree, with the year's rule changes applied."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "+VERSION").write_text("2026d\n", encoding="ascii")

    # British Columbia: history up to the final spring-forward, then a fixed
    # -07:00 for ever. The type table keeps PST because it is still the label on
    # every pre-2026 instant; MST arrives via the footer.
    last_spring = _NA_DST_START.instant(2026, -28800)
    pacific = [TTInfo(-28800, False, "PST"), TTInfo(-25200, True, "PDT")]
    vancouver = _na_cycle(-28800, -25200, 2020, 2026, stop_after=last_spring)
    write_tzif_file(root / "America" / "Vancouver", pacific, vancouver, footer="MST7")
    write_tzif_file(root / "Canada" / "Pacific", pacific, vancouver, footer="MST7")

    # Alberta: same shape, settling on -06:00 and the CST label.
    mountain = [TTInfo(-25200, False, "MST"), TTInfo(-21600, True, "MDT")]
    write_tzif_file(
        root / "America" / "Edmonton",
        mountain,
        _na_cycle(-25200, -21600, 2020, 2026, stop_after=_NA_DST_START.instant(2026, -25200)),
        footer="CST6",
    )

    write_tzif_file(root / "America" / "Whitehorse", [TTInfo(-25200, False, "MST")], [],
                    footer="MST7")

    _write_casablanca(root, ramadan_end_day=29, permanent_utc=True)
    _write_madrid(root)
    _write_chisinau(root, autumn_local_hour=4)

    # A zone that simply did not exist in the older release.
    write_tzif_file(root / "America" / "Coyhaique", [TTInfo(-10800, False, "-03")], [],
                    footer="<-03>3")


def _write_casablanca(root: Path, *, ramadan_end_day: int, permanent_utc: bool) -> None:
    """Write Morocco: a negative-DST zone that went to permanent UTC in 2026.

    `ramadan_end_day` is the March date on which the clock springs back to +01:00.
    It moves by a week between the two vintages, which is routine for Ramadan
    windows and is what makes some readings exist in one tree and not the other.
    """
    plus_one = TTInfo(3600, False, "+01")
    utc = TTInfo(0, True, "+00")
    types = [plus_one, utc]
    records = [
        # Clocks go back to +00:00 for Ramadan: a fold.
        Transition(civil_to_instant(2026, 2, 15, 3 * 3600) - 3600, 1),
        # And forward again afterwards: a gap.
        Transition(civil_to_instant(2026, 3, ramadan_end_day, 2 * 3600), 0),
    ]
    if permanent_utc:
        records.append(Transition(civil_to_instant(2026, 9, 20, 2 * 3600) - 3600, 1))
        footer = "<+00>0"
    else:
        footer = "<+01>-1"
    write_tzif_file(root / "Africa" / "Casablanca", types, records, footer=footer)


def _write_madrid(root: Path) -> None:
    """Write a zone that is identical in both trees."""
    types = [TTInfo(3600, False, "CET"), TTInfo(7200, True, "CEST")]
    write_tzif_file(root / "Europe" / "Madrid", types, _eu_cycle(3600, 7200, 2020, 2037),
                    footer="CET-1CEST,M3.5.0,M10.5.0/3")


def _write_chisinau(root: Path, *, autumn_local_hour: int) -> None:
    """Write a zone whose only difference is a historical transition time.

    Moldova has followed EU transition times since 2022, falling back at 04:00
    local rather than 03:00. A database that still encodes the older hour for the
    earlier years produces different answers for a one-hour window in each of
    those autumns, and identical answers from 2023 onwards. That split is the
    whole point of the zone: it is the shape of a change that alters how old rows
    display without putting a single new row at risk.

    An EET zone transitions at 01:00 UTC in both directions, which is 03:00 local
    going into summer and 04:00 local coming out of it. The footer below says
    exactly that, and the transition records are generated to match, so the file
    is self-consistent.
    """
    types = [TTInfo(7200, False, "EET"), TTInfo(10800, True, "EEST")]
    spring = TransitionRule(kind="M", month=3, week=5, weekday=0, time=3 * 3600)
    settled_autumn = TransitionRule(kind="M", month=10, week=5, weekday=0, time=4 * 3600)
    legacy_autumn = TransitionRule(
        kind="M", month=10, week=5, weekday=0, time=autumn_local_hour * 3600
    )
    records: list[Transition] = []
    for year in range(2020, 2038):
        autumn = legacy_autumn if year < 2023 else settled_autumn
        records.append(Transition(spring.instant(year, 7200), 1))
        records.append(Transition(autumn.instant(year, 10800), 0))
    write_tzif_file(root / "Europe" / "Chisinau", types,
                    sorted(records, key=lambda record: record.instant),
                    footer="EET-2EEST,M3.5.0/3,M10.5.0/4")


def main() -> int:
    """Generate both trees and report what was written."""
    build_old_tree(OLD_TREE)
    build_new_tree(NEW_TREE)
    for tree in (OLD_TREE, NEW_TREE):
        zones = sorted(
            path.relative_to(tree).as_posix()
            for path in tree.rglob("*")
            if path.is_file() and path.name != "+VERSION"
        )
        label = (tree / "+VERSION").read_text(encoding="ascii").strip()
        print(f"{tree}  label={label}  zones={len(zones)}")
        for zone in zones:
            print(f"    {zone}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
