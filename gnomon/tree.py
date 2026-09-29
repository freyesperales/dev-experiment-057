"""Discovery of zones inside a compiled zoneinfo tree.

A zoneinfo tree is just a directory of TZif files with a few plain-text index
files mixed in (``zone.tab``, ``leapseconds``, ``tzdata.zi``, ``+VERSION``), and
different packagers include different subsets. Rather than hard-coding a skip
list and hoping, every candidate file is sniffed for the TZif magic, so an
unfamiliar layout degrades into "fewer zones found" rather than a crash or a
bogus zone called ``iso3166.tab``.

Trees also make heavy use of links: ``Canada/Pacific`` and ``America/Vancouver``
are usually the same inode. Identical content is grouped into alias sets so a
single real change is reported once, with its aliases named, instead of six
times.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import TreeError, TzifParseError
from .rules import ZoneRules
from .tzif import looks_like_tzif, parse_tzif

#: Files that are never zones. Sniffing catches these anyway; skipping them by
#: name avoids reading large text indexes only to discard them.
_KNOWN_NON_ZONES: frozenset[str] = frozenset(
    {
        "+VERSION",
        "iso3166.tab",
        "leap-seconds.list",
        "leapseconds",
        "tzdata.zi",
        "zone.tab",
        "zone1970.tab",
        "zonenow.tab",
    }
)

#: Directories inside a tree that hold duplicates of zones found elsewhere and
#: are excluded by default, since including them triples the report for no gain.
_LEGACY_DIRS: frozenset[str] = frozenset({"right", "posix"})

#: Top-level groupings IANA considers canonical. Everything else -- ``Canada/``,
#: ``US/``, ``Etc/``, bare names like ``EST5EDT`` -- is a backward-compatibility
#: link, so when several names share one rule set, a name from this set is the
#: one an operator will recognise and the one worth reporting under.
_CANONICAL_PREFIXES: frozenset[str] = frozenset(
    {
        "Africa",
        "America",
        "Antarctica",
        "Arctic",
        "Asia",
        "Atlantic",
        "Australia",
        "Europe",
        "Indian",
        "Pacific",
    }
)


def _canonical_rank(name: str) -> tuple[int, int, int, str]:
    """Sort key that puts the most recognisable of several alias names first."""
    head = name.split("/", 1)[0]
    return (
        0 if head in _CANONICAL_PREFIXES else 1,
        0 if "/" in name else 1,
        len(name),
        name,
    )


@dataclass(slots=True)
class ZoneTree:
    """A parsed zoneinfo tree.

    Attributes:
        root: Directory the tree was read from.
        label: Release label from ``+VERSION`` when present, else None. This is
            what lets a report say "2025b vs 2026d" instead of "tree A vs B".
        zones: Zone name to parsed rules.
        unreadable: Zone name to the parse error encountered, kept rather than
            dropped so a partially corrupt tree still produces a usable diff
            alongside an explicit list of what could not be read.
        skipped: Paths that did not look like TZif files.
    """

    root: Path
    label: str | None
    zones: dict[str, ZoneRules] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    #: True when this "tree" is a single TZif file. Its zone key is then a
    #: filename rather than an IANA name, so two such trees cannot be aligned by
    #: name and callers must pair their sole zones positionally instead.
    single_file: bool = False

    @property
    def sole_zone(self) -> str:
        """The only zone name, for a single-file tree."""
        if not self.single_file or len(self.zones) != 1:
            raise TreeError(f"{self.root} is not a single-zone tree")
        return next(iter(self.zones))

    @property
    def describe(self) -> str:
        """Short identity for report headers."""
        return f"{self.label or self.root.name} ({self.root})"

    def alias_sets(self) -> dict[str, list[str]]:
        """Group zone names by identical rule content.

        Returns a mapping from the chosen canonical name to its other names.
        The canonical name is chosen by `_canonical_rank`, which prefers an IANA
        area prefix, so ``America/Vancouver`` wins over its ``Canada/Pacific``
        compatibility link rather than the other way round.
        """
        by_signature: dict[str, list[str]] = {}
        for name, rules in self.zones.items():
            digest = hashlib.sha256(rules.signature().encode("utf-8")).hexdigest()
            by_signature.setdefault(digest, []).append(name)
        grouped: dict[str, list[str]] = {}
        for names in by_signature.values():
            names.sort(key=_canonical_rank)
            grouped[names[0]] = names[1:]
        return grouped


def load_tree(root: str | Path, *, include_legacy: bool = False) -> ZoneTree:
    """Walk a zoneinfo tree and parse every TZif file in it.

    Args:
        root: Directory to walk. A single TZif file is also accepted and yields
            a one-zone tree, which is how two individual zone files get diffed.
        include_legacy: Also descend into ``right/`` and ``posix/``.

    Raises:
        TreeError: when `root` does not exist, or contains no readable zones.
    """
    base = Path(root)
    if not base.exists():
        raise TreeError(f"no such path: {base}")

    if base.is_file():
        try:
            rules = parse_tzif(base.read_bytes(), name=base.name, source=str(base))
        except (OSError, TzifParseError) as exc:
            raise TreeError(f"{base} is not a readable TZif file: {exc}") from exc
        return ZoneTree(
            root=base.parent, label=None, zones={base.name: rules}, single_file=True
        )

    tree = ZoneTree(root=base, label=_read_version_label(base))
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        parts = relative.parts
        if not include_legacy and parts and parts[0] in _LEGACY_DIRS:
            continue
        if path.name in _KNOWN_NON_ZONES or path.name.startswith("."):
            continue
        name = "/".join(parts)
        try:
            data = path.read_bytes()
        except OSError as exc:
            tree.unreadable[name] = f"cannot read: {exc}"
            continue
        if not looks_like_tzif(data):
            tree.skipped.append(name)
            continue
        try:
            tree.zones[name] = parse_tzif(data, name=name, source=str(path))
        except TzifParseError as exc:
            tree.unreadable[name] = str(exc)

    if not tree.zones:
        raise TreeError(
            f"{base} contains no readable TZif files "
            f"({len(tree.skipped)} non-TZif files, {len(tree.unreadable)} unreadable)"
        )
    return tree


def _read_version_label(root: Path) -> str | None:
    """Read the ``+VERSION`` release marker that ``zic`` writes into a tree."""
    marker = root / "+VERSION"
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    return text.splitlines()[0].strip() if text else None
