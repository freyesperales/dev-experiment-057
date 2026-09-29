"""A TZif writer, used to build synthetic zone databases.

This exists so gnomon can be tested and demonstrated without a tz database on
the machine, and without checking megabytes of third-party binaries into the
repository. It also turns out to be the most useful thing in the package for a
reader who wants to reproduce an incident: you can express "what my container
believed" and "what the world now believes" as two short Python literals, write
them out, and diff them.

The output is a genuine RFC 8536 version 2 file -- a 32-bit block, a 64-bit
block and a POSIX footer -- so it round-trips through ``zoneinfo``, ``zdump``
and gnomon's own reader alike.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterable, Sequence

from .errors import GnomonError
from .rules import TTInfo, Transition

_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_MAGIC = b"TZif"


class TzifWriteError(GnomonError):
    """The requested zone cannot be encoded as a valid TZif file."""


def build_tzif(
    types: Sequence[TTInfo],
    transitions: Sequence[Transition] = (),
    *,
    footer: str | None = None,
    version: int = 2,
) -> bytes:
    """Encode a zone as TZif bytes.

    Args:
        types: Local-time types. Put standard time first: readers treat the
            first non-DST type as the one in force before the first transition.
        transitions: Strictly ascending transition records.
        footer: POSIX TZ string describing behaviour past the last transition.
            Omit only when the zone genuinely has no defined future, which real
            databases avoid.
        version: 2 or 3. Version 3 only signals that the footer may use the
            extended rule-time range.

    Raises:
        TzifWriteError: on empty type lists, out-of-order transitions, or type
            indices that do not exist.
    """
    if version not in (2, 3):
        raise TzifWriteError(f"unsupported output version {version}, expected 2 or 3")
    if not types:
        raise TzifWriteError("a zone needs at least one local-time type")
    ordered = list(transitions)
    for index in range(1, len(ordered)):
        if ordered[index].instant <= ordered[index - 1].instant:
            raise TzifWriteError(
                f"transitions must strictly ascend; index {index} has instant "
                f"{ordered[index].instant} after {ordered[index - 1].instant}"
            )
    for record in ordered:
        if not 0 <= record.type_index < len(types):
            raise TzifWriteError(
                f"transition at {record.instant} references type "
                f"{record.type_index}, but {len(types)} types are defined"
            )

    designations, indices = _build_designations(types)
    narrow = [r for r in ordered if _INT32_MIN <= r.instant <= _INT32_MAX]

    out = bytearray()
    out += _encode_block(types, indices, designations, narrow, time_size=4, version=version)
    out += _encode_block(types, indices, designations, ordered, time_size=8, version=version)
    out += b"\n" + (footer or "").encode("ascii") + b"\n"
    return bytes(out)


def write_tzif_file(
    path: str | Path,
    types: Sequence[TTInfo],
    transitions: Sequence[Transition] = (),
    *,
    footer: str | None = None,
    version: int = 2,
) -> Path:
    """Encode a zone and write it to `path`, creating parent directories."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_tzif(types, transitions, footer=footer, version=version))
    return target


def _build_designations(types: Iterable[TTInfo]) -> tuple[bytes, list[int]]:
    """Pack abbreviations into one NUL-separated buffer, sharing duplicates."""
    buffer = bytearray()
    positions: dict[str, int] = {}
    indices: list[int] = []
    for info in types:
        if info.abbrev not in positions:
            positions[info.abbrev] = len(buffer)
            buffer += info.abbrev.encode("ascii") + b"\x00"
        indices.append(positions[info.abbrev])
    return bytes(buffer), indices


def _encode_block(
    types: Sequence[TTInfo],
    indices: Sequence[int],
    designations: bytes,
    transitions: Sequence[Transition],
    *,
    time_size: int,
    version: int,
) -> bytes:
    """Encode one header plus its data block."""
    time_fmt = ">l" if time_size == 4 else ">q"
    out = bytearray()
    out += _MAGIC
    out += str(version).encode("ascii")
    out += b"\x00" * 15
    out += struct.pack(
        ">6I",
        len(types),  # isutcnt
        len(types),  # isstdcnt
        0,  # leapcnt
        len(transitions),
        len(types),
        len(designations),
    )
    for record in transitions:
        out += struct.pack(time_fmt, record.instant)
    out += bytes(record.type_index for record in transitions)
    for info, abbrev_index in zip(types, indices):
        out += struct.pack(">lBB", info.utoff, 1 if info.isdst else 0, abbrev_index)
    out += designations
    # Standard/wall and UT/local indicators: declare every type as wall-clock
    # local time, which is what zic emits for ordinary zones.
    out += b"\x00" * len(types)
    out += b"\x00" * len(types)
    return bytes(out)
