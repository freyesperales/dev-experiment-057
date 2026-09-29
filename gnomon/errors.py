"""Exception hierarchy for gnomon.

Every failure is explicit and carries the artefact it came from, so a bad TZif
file in a 600-zone tree names itself instead of vanishing into a traceback.
"""

from __future__ import annotations


class GnomonError(Exception):
    """Base class for every error raised by gnomon."""


class TzifParseError(GnomonError):
    """A TZif file could not be parsed.

    Attributes:
        path: Origin of the bytes, for operator-facing messages.
        offset: Byte offset at which parsing failed, when known.
    """

    def __init__(self, message: str, *, path: str | None = None, offset: int | None = None) -> None:
        self.path = path
        self.offset = offset
        parts = [message]
        if path is not None:
            parts.append(f"path={path!r}")
        if offset is not None:
            parts.append(f"byte_offset={offset}")
        super().__init__(" ".join(parts))


class PosixTzParseError(GnomonError):
    """The POSIX TZ footer string of a TZif v2+ file is malformed."""

    def __init__(self, message: str, *, spec: str | None = None) -> None:
        self.spec = spec
        super().__init__(f"{message} (spec={spec!r})" if spec is not None else message)


class TreeError(GnomonError):
    """A zoneinfo tree could not be read or contains no usable zones."""


class InputDataError(GnomonError):
    """Caller-supplied tabular data (CSV) is malformed or mis-mapped."""
