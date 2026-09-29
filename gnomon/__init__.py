"""gnomon -- find where two tz databases silently disagree, and which rows it broke.

A gnomon is the part of a sundial that casts the shadow: the piece that turns the
passage of time into a reading you can act on. This package does the same for two
compiled tz databases, by answering the question no tool inside a database can
answer about itself -- "is the timezone data I am using the data the world is
using, and if not, what have I already written down wrong?"

Public surface::

    from gnomon import load_tree, diff_trees, analyse_rows, read_wall_rows

Everything is pure standard library, offline, and reads only the bytes it is
pointed at.
"""

from __future__ import annotations

from .blast import (
    BlastReport,
    RowFinding,
    WallRow,
    analyse_rows,
    emit_correction_sql,
    read_wall_rows,
)
from .diverge import (
    OffsetInterval,
    Severity,
    TreeDivergence,
    ZoneDivergence,
    diff_trees,
    diff_zone,
)
from .errors import (
    GnomonError,
    InputDataError,
    PosixTzParseError,
    TreeError,
    TzifParseError,
)
from .posixtz import PosixTz, TransitionRule, parse_posix_tz
from .rules import TTInfo, Transition, WallResolution, ZoneRules
from .tree import ZoneTree, load_tree
from .tzif import parse_tzif, read_tzif_file
from .tzifwrite import build_tzif, write_tzif_file

__version__ = "1.0.0"

__all__ = [
    "BlastReport",
    "GnomonError",
    "InputDataError",
    "OffsetInterval",
    "PosixTz",
    "PosixTzParseError",
    "RowFinding",
    "Severity",
    "TTInfo",
    "Transition",
    "TransitionRule",
    "TreeDivergence",
    "TreeError",
    "TzifParseError",
    "WallResolution",
    "WallRow",
    "ZoneDivergence",
    "ZoneRules",
    "ZoneTree",
    "__version__",
    "analyse_rows",
    "build_tzif",
    "diff_trees",
    "diff_zone",
    "emit_correction_sql",
    "load_tree",
    "parse_posix_tz",
    "parse_tzif",
    "read_tzif_file",
    "read_wall_rows",
    "write_tzif_file",
]
