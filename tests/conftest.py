"""Shared fixtures.

The sample trees are generated into a temporary directory rather than read from
``samples/``, so the tests pass on a fresh clone whether or not the committed
samples have been regenerated, and cannot be broken by editing them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "samples") not in sys.path:
    sys.path.insert(0, str(_ROOT / "samples"))

from build_samples import build_new_tree, build_old_tree  # noqa: E402

from gnomon.rules import TTInfo, Transition  # noqa: E402
from gnomon.timeutil import civil_to_instant  # noqa: E402
from gnomon.tzifwrite import build_tzif  # noqa: E402

#: 2026-09-29, the date the samples are written around.
NOW = civil_to_instant(2026, 9, 29)


@pytest.fixture(scope="session")
def sample_trees(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Build both synthetic trees once per session, into a temp directory."""
    base = tmp_path_factory.mktemp("trees")
    old = base / "tzdata-2025b"
    new = base / "tzdata-2026d"
    build_old_tree(old)
    build_new_tree(new)
    return old, new


@pytest.fixture(scope="session")
def old_tree(sample_trees: tuple[Path, Path]) -> Path:
    """The 2025-vintage tree."""
    return sample_trees[0]


@pytest.fixture(scope="session")
def new_tree(sample_trees: tuple[Path, Path]) -> Path:
    """The 2026-vintage tree."""
    return sample_trees[1]


def na_dst_zone(
    std_utoff: int = -28800,
    dst_utoff: int = -25200,
    std_abbrev: str = "PST",
    dst_abbrev: str = "PDT",
    *,
    footer: str = "PST8PDT,M3.2.0,M11.1.0",
    years: range = range(2024, 2028),
) -> bytes:
    """Build a North-American-style DST zone for tests."""
    types = [
        TTInfo(std_utoff, False, std_abbrev),
        TTInfo(dst_utoff, True, dst_abbrev),
    ]
    from gnomon.posixtz import TransitionRule

    spring = TransitionRule(kind="M", month=3, week=2, weekday=0, time=2 * 3600)
    autumn = TransitionRule(kind="M", month=11, week=1, weekday=0, time=2 * 3600)
    records: list[Transition] = []
    for year in years:
        records.append(Transition(spring.instant(year, std_utoff), 1))
        records.append(Transition(autumn.instant(year, dst_utoff), 0))
    records.sort(key=lambda record: record.instant)
    return build_tzif(types, records, footer=footer)


def abolished_dst_zone(
    std_utoff: int = -28800,
    dst_utoff: int = -25200,
    std_abbrev: str = "PST",
    dst_abbrev: str = "PDT",
    *,
    footer: str = "MST7",
    final_year: int = 2026,
) -> bytes:
    """Build a zone that kept its DST history and then settled on a fixed offset.

    The transition table stops at the final spring-forward and the footer takes
    over from there, which is the shape ``zic`` produces for a jurisdiction that
    abolished DST. Crucially the footer's offset matches the last transition's,
    so the file is self-consistent -- a zone whose footer contradicts its own
    table is malformed, and gnomon reports it as such.
    """
    from gnomon.posixtz import TransitionRule

    types = [
        TTInfo(std_utoff, False, std_abbrev),
        TTInfo(dst_utoff, True, dst_abbrev),
    ]
    spring = TransitionRule(kind="M", month=3, week=2, weekday=0, time=2 * 3600)
    autumn = TransitionRule(kind="M", month=11, week=1, weekday=0, time=2 * 3600)
    last = spring.instant(final_year, std_utoff)
    records: list[Transition] = []
    for year in range(2024, final_year + 1):
        records.append(Transition(spring.instant(year, std_utoff), 1))
        records.append(Transition(autumn.instant(year, dst_utoff), 0))
    records = sorted(
        (record for record in records if record.instant <= last),
        key=lambda record: record.instant,
    )
    return build_tzif(types, records, footer=footer)


def pulse_zone(
    start: int,
    end: int,
    *,
    std_utoff: int = 0,
    dst_utoff: int = 3600,
    footer: str = "AAA0",
) -> bytes:
    """Build a zone that steps up to `dst_utoff` between two instants.

    It returns to standard time at `end`, so the final transition agrees with the
    fixed footer and the file is self-consistent. Used to place a divergence at a
    chosen instant without dragging a whole DST cycle along.
    """
    types = [TTInfo(std_utoff, False, "AAA"), TTInfo(dst_utoff, True, "BBB")]
    return build_tzif(
        types, [Transition(start, 1), Transition(end, 0)], footer=footer
    )


def fixed_zone(utoff: int, abbrev: str, footer: str) -> bytes:
    """Build a zone with a single fixed offset and no transitions."""
    return build_tzif([TTInfo(utoff, False, abbrev)], [], footer=footer)
