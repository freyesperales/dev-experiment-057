"""A TZif reader, per RFC 8536.

gnomon parses TZif itself instead of delegating to ``zoneinfo``, for three
reasons that all matter to the job:

1. ``zoneinfo`` exposes no transition table. The divergence scan needs the
   breakpoints, not just point lookups, or it degrades into sampling and starts
   missing narrow disagreements.
2. ``zoneinfo`` reads whichever database the *host* ships. gnomon's entire
   purpose is to compare databases the host is not using -- notably the tree a
   Postgres binary was compiled against, which may sit inside a container image
   or a Nix store and be nowhere on the host search path.
3. A CPython build with no system tzdata and no ``tzdata`` wheel has no database
   at all, so a host-dependent implementation would not even run there. This one
   needs nothing but the bytes it is pointed at.

Layout, for reference (RFC 8536 sections 3.1-3.3)::

    header      magic "TZif", version byte, 15 reserved, six uint32 counts
    data block  transition times, type indices, ttinfo records,
                designation bytes, leap records, std/wall flags, ut/local flags
    -- version 2+ only --
    header      a second header, same shape
    data block  same shape, but 64-bit transition times
    footer      NL, POSIX TZ string, NL
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Final

from .errors import PosixTzParseError, TzifParseError
from .posixtz import PosixTz, parse_posix_tz
from .rules import LeapSecond, TTInfo, Transition, ZoneRules, cross_check

MAGIC: Final[bytes] = b"TZif"
HEADER_SIZE: Final[int] = 44
_VERSION_BYTES: Final[dict[bytes, int]] = {
    b"\x00": 1,
    b"1": 1,
    b"2": 2,
    b"3": 3,
    b"4": 4,
}


class _Header:
    """Parsed TZif header counts."""

    __slots__ = ("version", "isutcnt", "isstdcnt", "leapcnt", "timecnt", "typecnt", "charcnt")

    def __init__(self, version: int, counts: tuple[int, ...]) -> None:
        self.version = version
        (self.isutcnt, self.isstdcnt, self.leapcnt, self.timecnt,
         self.typecnt, self.charcnt) = counts

    def block_size(self, time_size: int) -> int:
        """Byte length of the data block that follows this header."""
        return (
            self.timecnt * time_size
            + self.timecnt
            + self.typecnt * 6
            + self.charcnt
            + self.leapcnt * (time_size + 4)
            + self.isstdcnt
            + self.isutcnt
        )


def looks_like_tzif(data: bytes) -> bool:
    """Cheap sniff used when walking a tree full of unrelated files."""
    return len(data) >= HEADER_SIZE and data[:4] == MAGIC


def parse_tzif(data: bytes, *, name: str = "<bytes>", source: str = "") -> ZoneRules:
    """Parse TZif bytes into `ZoneRules`.

    Args:
        data: Complete file contents.
        name: Zone name to record (e.g. ``America/Vancouver``).
        source: Path or description of where the bytes came from.

    Raises:
        TzifParseError: on a bad magic, unknown version, truncation, or any
            internally inconsistent index or count.
    """
    if len(data) < HEADER_SIZE:
        raise TzifParseError(
            f"file is {len(data)} bytes, shorter than a {HEADER_SIZE}-byte header",
            path=source,
        )
    if data[:4] != MAGIC:
        raise TzifParseError(
            f"bad magic {data[:4]!r}, expected {MAGIC!r}", path=source, offset=0
        )

    header = _read_header(data, 0, source)
    cursor = HEADER_SIZE + header.block_size(4)

    posix: PosixTz | None = None
    warnings: list[str] = []

    if header.version >= 2:
        if cursor + HEADER_SIZE > len(data):
            raise TzifParseError(
                "version 2+ file is truncated before its second header",
                path=source,
                offset=cursor,
            )
        second = _read_header(data, cursor, source)
        if second.version != header.version:
            warnings.append(
                f"second header declares version {second.version} but the first "
                f"declares {header.version}; using the 64-bit block regardless"
            )
        body_start = cursor + HEADER_SIZE
        block = _read_block(data, body_start, second, time_size=8, source=source)
        footer_start = body_start + second.block_size(8)
        posix, footer_warnings = _read_footer(data, footer_start, source)
        warnings.extend(footer_warnings)
        effective = second
    else:
        block = _read_block(data, HEADER_SIZE, header, time_size=4, source=source)
        effective = header

    rules = ZoneRules(
        name=name,
        types=block.types,
        transitions=block.transitions,
        posix=posix,
        leap_seconds=block.leap_seconds,
        version=header.version,
        source=source,
        warnings=tuple(warnings),
    )
    extra = cross_check(rules)
    if extra:
        rules.warnings = tuple(warnings) + extra
    if effective.typecnt == 0:
        raise TzifParseError(f"zone {name!r} declares zero local-time types", path=source)
    return rules


def read_tzif_file(path: str | Path, *, name: str | None = None) -> ZoneRules:
    """Read and parse one TZif file from disk."""
    resolved = Path(path)
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise TzifParseError(f"cannot read file: {exc}", path=str(resolved)) from exc
    return parse_tzif(data, name=name or resolved.name, source=str(resolved))


def _read_header(data: bytes, offset: int, source: str) -> _Header:
    """Parse a 44-byte header at `offset`."""
    if data[offset : offset + 4] != MAGIC:
        raise TzifParseError(
            f"expected TZif magic, found {data[offset:offset + 4]!r}",
            path=source,
            offset=offset,
        )
    version_byte = data[offset + 4 : offset + 5]
    if version_byte not in _VERSION_BYTES:
        raise TzifParseError(
            f"unknown TZif version byte {version_byte!r}", path=source, offset=offset + 4
        )
    counts = struct.unpack_from(">6I", data, offset + 20)
    header = _Header(_VERSION_BYTES[version_byte], counts)
    if header.isutcnt not in (0, header.typecnt):
        raise TzifParseError(
            f"isutcnt={header.isutcnt} must be 0 or typecnt={header.typecnt}",
            path=source,
            offset=offset + 20,
        )
    if header.isstdcnt not in (0, header.typecnt):
        raise TzifParseError(
            f"isstdcnt={header.isstdcnt} must be 0 or typecnt={header.typecnt}",
            path=source,
            offset=offset + 20,
        )
    return header


class _Block:
    """Parsed contents of one TZif data block."""

    __slots__ = ("transitions", "types", "leap_seconds")

    def __init__(
        self,
        transitions: tuple[Transition, ...],
        types: tuple[TTInfo, ...],
        leap_seconds: tuple[LeapSecond, ...],
    ) -> None:
        self.transitions = transitions
        self.types = types
        self.leap_seconds = leap_seconds


def _read_block(
    data: bytes, offset: int, header: _Header, *, time_size: int, source: str
) -> _Block:
    """Parse one data block, 32-bit or 64-bit."""
    need = header.block_size(time_size)
    if offset + need > len(data):
        raise TzifParseError(
            f"data block needs {need} bytes but only {len(data) - offset} remain",
            path=source,
            offset=offset,
        )
    cursor = offset
    time_fmt = ">l" if time_size == 4 else ">q"

    instants: list[int] = []
    for _ in range(header.timecnt):
        (value,) = struct.unpack_from(time_fmt, data, cursor)
        instants.append(value)
        cursor += time_size
    for index in range(1, len(instants)):
        if instants[index] <= instants[index - 1]:
            raise TzifParseError(
                f"transition times are not strictly ascending at index {index} "
                f"({instants[index - 1]} then {instants[index]})",
                path=source,
                offset=offset,
            )

    type_indices = list(data[cursor : cursor + header.timecnt])
    cursor += header.timecnt

    raw_types: list[tuple[int, int, int]] = []
    for _ in range(header.typecnt):
        utoff, isdst, abbrev_index = struct.unpack_from(">lBB", data, cursor)
        raw_types.append((utoff, isdst, abbrev_index))
        cursor += 6

    designations = data[cursor : cursor + header.charcnt]
    cursor += header.charcnt

    leap_seconds: list[LeapSecond] = []
    for _ in range(header.leapcnt):
        (occurrence,) = struct.unpack_from(time_fmt, data, cursor)
        cursor += time_size
        (correction,) = struct.unpack_from(">l", data, cursor)
        cursor += 4
        leap_seconds.append(LeapSecond(occurrence=occurrence, correction=correction))

    cursor += header.isstdcnt + header.isutcnt

    types: list[TTInfo] = []
    for position, (utoff, isdst, abbrev_index) in enumerate(raw_types):
        if isdst not in (0, 1):
            raise TzifParseError(
                f"local-time type {position} has dst flag {isdst}, expected 0 or 1",
                path=source,
                offset=offset,
            )
        types.append(
            TTInfo(
                utoff=utoff,
                isdst=bool(isdst),
                abbrev=_designation_at(designations, abbrev_index, position, source),
            )
        )

    transitions: list[Transition] = []
    for position, (instant, type_index) in enumerate(zip(instants, type_indices)):
        if type_index >= len(types):
            raise TzifParseError(
                f"transition {position} references local-time type {type_index} "
                f"but only {len(types)} types are defined",
                path=source,
                offset=offset,
            )
        transitions.append(Transition(instant=instant, type_index=type_index))

    return _Block(tuple(transitions), tuple(types), tuple(leap_seconds))


def _designation_at(buffer: bytes, index: int, position: int, source: str) -> str:
    """Extract the NUL-terminated abbreviation starting at `index`."""
    if index >= len(buffer):
        raise TzifParseError(
            f"local-time type {position} points at designation byte {index} but the "
            f"designation block is {len(buffer)} bytes",
            path=source,
        )
    end = buffer.find(b"\x00", index)
    if end < 0:
        raise TzifParseError(
            f"designation for local-time type {position} is not NUL-terminated",
            path=source,
        )
    return buffer[index:end].decode("ascii", errors="replace")


def _read_footer(data: bytes, offset: int, source: str) -> tuple[PosixTz | None, list[str]]:
    """Parse the version 2+ footer: newline, TZ string, newline."""
    warnings: list[str] = []
    if offset >= len(data):
        warnings.append("version 2+ file ends without a footer")
        return None, warnings
    if data[offset : offset + 1] != b"\n":
        raise TzifParseError(
            f"footer must start with a newline, found {data[offset:offset + 1]!r}",
            path=source,
            offset=offset,
        )
    end = data.find(b"\n", offset + 1)
    if end < 0:
        raise TzifParseError(
            "footer is not terminated by a newline", path=source, offset=offset
        )
    spec = data[offset + 1 : end].decode("ascii", errors="replace")
    if not spec:
        warnings.append(
            "footer is empty, so behaviour past the last transition is unspecified"
        )
        return None, warnings
    try:
        return parse_posix_tz(spec), warnings
    except PosixTzParseError as exc:
        raise TzifParseError(
            f"footer TZ string is malformed: {exc}", path=source, offset=offset + 1
        ) from exc
