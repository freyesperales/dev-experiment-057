"""Command-line interface.

Exit codes are stable, because the intended home for ``gnomon diff`` is a CI job
or a container start-up check:

* ``0`` -- ran cleanly, nothing at or above the failure threshold
* ``2`` -- findings at or above ``--fail-on`` (default: ``critical``)
* ``1`` -- the tool could not do its job: bad path, corrupt file, bad arguments
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence, TextIO

from .blast import analyse_rows, emit_correction_sql, read_wall_rows
from .diverge import Severity, diff_trees
from .errors import GnomonError
from .report import (
    blast_json,
    render_blast,
    render_resolve,
    render_tree_diff,
    render_zones,
    tree_diff_json,
)
from .timeutil import SECONDS_PER_DAY, civil_to_instant, parse_date, parse_naive
from .tree import ZoneTree, load_tree

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FINDINGS = 2

#: Default comparison window: one year behind, three years ahead. Rows written
#: today for dates further out than three years are rare; historical divergence
#: is reported but is not the point.
_DEFAULT_LOOKBACK_DAYS = 365
_DEFAULT_LOOKAHEAD_DAYS = 3 * 365


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="gnomon",
        description=(
            "Compare two compiled tz databases and find where they silently disagree, "
            "including about which local wall clocks exist at all."
        ),
        epilog=(
            "The left tree is treated as the stale one (what your server believes) and "
            "the right as authoritative (what the world believes)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    diff = subparsers.add_parser(
        "diff", help="compare two zoneinfo trees and report every divergence"
    )
    _add_tree_args(diff)
    _add_window_args(diff)
    diff.add_argument("--zone", action="append", default=None, help="restrict to this zone (repeatable)")
    diff.add_argument("--limit", type=int, default=25, help="max zones to print (default 25)")
    diff.add_argument("--show-identical", action="store_true", help="also list agreeing zones")
    diff.add_argument("--no-collapse-aliases", action="store_true",
                      help="report linked zone names separately instead of once per group")
    diff.add_argument("--include-legacy", action="store_true",
                      help="also descend into right/ and posix/ subdirectories")
    diff.add_argument("--json", action="store_true", help="emit JSON instead of text")
    diff.add_argument("--fail-on", default="critical",
                      help="exit 2 at this severity or above (info|low|medium|high|critical)")

    blast = subparsers.add_parser(
        "blast", help="find which of your stored wall-clock rows hold the wrong instant"
    )
    _add_tree_args(blast)
    blast.add_argument("--data", required=True, help="CSV of wall-clock readings")
    blast.add_argument("--id-col", default=None, help="column holding the row identifier")
    blast.add_argument("--zone-col", default=None, help="column holding the zone name")
    blast.add_argument("--wall-col", default=None, help="column holding a combined wall-clock stamp")
    blast.add_argument("--date-col", default=None, help="column holding the local date")
    blast.add_argument("--time-col", default=None, help="column holding the local time of day")
    blast.add_argument("--now", default=None, help="treat this YYYY-MM-DD as the present")
    blast.add_argument("--limit", type=int, default=40, help="max rows to print (default 40)")
    blast.add_argument("--include-legacy", action="store_true",
                       help="also descend into right/ and posix/ subdirectories")
    blast.add_argument("--json", action="store_true", help="emit JSON instead of text")
    blast.add_argument("--sql", default=None, help="write corrective SQL to this file ('-' for stdout)")
    blast.add_argument("--table", default="your_table", help="table name for --sql")
    blast.add_argument("--instant-column", default="starts_at", help="timestamptz column for --sql")
    blast.add_argument("--id-column", default="id", help="primary-key column for --sql")
    blast.add_argument("--unquoted-ids", action="store_true",
                       help="emit identifiers unquoted in SQL (for integer keys)")
    blast.add_argument("--fail-on", default="critical",
                       help="exit 2 at this severity or above (info|low|medium|high|critical)")

    resolve = subparsers.add_parser(
        "resolve", help="convert one wall-clock reading under both databases, side by side"
    )
    _add_tree_args(resolve)
    resolve.add_argument("--zone", required=True, help="zone name, e.g. America/Vancouver")
    resolve.add_argument("--wall", required=True, help="unzoned wall clock, e.g. '2026-11-15 09:30'")
    resolve.add_argument("--include-legacy", action="store_true",
                         help="also descend into right/ and posix/ subdirectories")
    resolve.add_argument("--json", action="store_true", help="emit JSON instead of text")

    zones = subparsers.add_parser("zones", help="list the zones found in one tree")
    zones.add_argument("tree", help="path to a zoneinfo tree or a single TZif file")
    zones.add_argument("--grep", default=None, help="case-insensitive substring filter")
    zones.add_argument("--limit", type=int, default=60, help="max zones to print (default 60)")
    zones.add_argument("--include-legacy", action="store_true",
                       help="also descend into right/ and posix/ subdirectories")
    zones.add_argument("--json", action="store_true", help="emit JSON instead of text")

    return parser


def _add_tree_args(parser: argparse.ArgumentParser) -> None:
    """Add the two positional tree paths shared by most subcommands."""
    parser.add_argument("left", help="stale tree: the database your server actually uses")
    parser.add_argument("right", help="authoritative tree: current tzdata")


def _add_window_args(parser: argparse.ArgumentParser) -> None:
    """Add the comparison-window options."""
    parser.add_argument("--from", dest="window_from", default=None,
                        help="window start, YYYY-MM-DD (default: one year before --now)")
    parser.add_argument("--to", dest="window_to", default=None,
                        help="window end, YYYY-MM-DD (default: three years after --now)")
    parser.add_argument("--now", default=None,
                        help="treat this YYYY-MM-DD as the present (default: today, UTC)")
    parser.add_argument("--history", action="store_true",
                        help="widen the window back to 1970 to include historical divergence")


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "diff":
            return _cmd_diff(args, out)
        if args.command == "blast":
            return _cmd_blast(args, out, err)
        if args.command == "resolve":
            return _cmd_resolve(args, out)
        if args.command == "zones":
            return _cmd_zones(args, out)
    except GnomonError as exc:
        print(f"gnomon: {exc}", file=err)
        return EXIT_ERROR
    except ValueError as exc:
        print(f"gnomon: {exc}", file=err)
        return EXIT_ERROR
    parser.error(f"unhandled command {args.command!r}")
    return EXIT_ERROR


def _today_instant() -> int:
    """Today's UTC midnight, without consulting any timezone database."""
    import datetime as _dt

    today = _dt.datetime.now(_dt.timezone.utc).date()
    return civil_to_instant(today.year, today.month, today.day)


def _resolve_now(raw: str | None) -> int:
    """Resolve the ``--now`` option."""
    return parse_date(raw) if raw else _today_instant()


def _align(left: ZoneTree, right: ZoneTree) -> tuple[dict, dict]:
    """Return both zone maps, keyed so shared zones line up.

    Zones normally align by IANA name. They cannot when either side is a single
    TZif file, because its key is a filename: comparing ``./old/Vancouver``
    against ``./new/Vancouver.tzif`` by name would find nothing in common and
    report two unrelated zones rather than one divergence. When both sides are
    single files, their sole zones are paired positionally instead.
    """
    if left.single_file and right.single_file:
        key = left.sole_zone
        return dict(left.zones), {key: right.zones[right.sole_zone]}
    return dict(left.zones), dict(right.zones)


def _cmd_diff(args: argparse.Namespace, out: TextIO) -> int:
    """Run the ``diff`` subcommand."""
    now = _resolve_now(args.now)
    window_start = (
        parse_date(args.window_from)
        if args.window_from
        else (
            civil_to_instant(1970, 1, 1)
            if args.history
            else now - _DEFAULT_LOOKBACK_DAYS * SECONDS_PER_DAY
        )
    )
    window_end = (
        parse_date(args.window_to)
        if args.window_to
        else now + _DEFAULT_LOOKAHEAD_DAYS * SECONDS_PER_DAY
    )
    if window_end <= window_start:
        raise ValueError("--to must be after --from")
    threshold = Severity.parse(args.fail_on)

    left = load_tree(args.left, include_legacy=args.include_legacy)
    right = load_tree(args.right, include_legacy=args.include_legacy)

    left_zones, right_zones = _align(left, right)
    if args.zone:
        wanted = set(args.zone)
        missing = wanted - (set(left_zones) | set(right_zones))
        if missing:
            raise ValueError(
                f"zone(s) not present in either tree: {', '.join(sorted(missing))}"
            )
        left_zones = {k: v for k, v in left_zones.items() if k in wanted}
        right_zones = {k: v for k, v in right_zones.items() if k in wanted}
        aliases: dict[str, list[str]] | None = {name: [] for name in wanted}
    else:
        aliases = left.alias_sets()

    divergence = diff_trees(
        left_zones,
        right_zones,
        left_label=left.describe,
        right_label=right.describe,
        window_start=window_start,
        window_end=window_end,
        now=now,
        aliases=aliases,
        collapse_aliases=not args.no_collapse_aliases,
        unreadable=tuple(sorted(left.unreadable.items())) + tuple(sorted(right.unreadable.items())),
    )

    if args.json:
        json.dump(tree_diff_json(divergence), out, indent=2)
        out.write("\n")
    else:
        out.write(render_tree_diff(divergence, limit=args.limit, show_identical=args.show_identical))

    return EXIT_FINDINGS if divergence.max_severity >= threshold else EXIT_OK


def _cmd_blast(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """Run the ``blast`` subcommand."""
    now = _resolve_now(args.now)
    threshold = Severity.parse(args.fail_on)

    left = load_tree(args.left, include_legacy=args.include_legacy)
    right = load_tree(args.right, include_legacy=args.include_legacy)
    for label, tree in (("left", left), ("right", right)):
        if tree.single_file:
            raise ValueError(
                f"the {label} path is a single TZif file, but blast matches rows by "
                f"IANA zone name and a lone file has only a filename. Point both "
                f"sides at zoneinfo trees, or use 'gnomon resolve' for one reading."
            )
    rows, mapping = read_wall_rows(
        args.data,
        id_col=args.id_col,
        zone_col=args.zone_col,
        wall_col=args.wall_col,
        date_col=args.date_col,
        time_col=args.time_col,
    )
    report = analyse_rows(
        rows,
        left.zones,
        right.zones,
        left_label=left.describe,
        right_label=right.describe,
        now=now,
    )

    if args.json:
        payload = blast_json(report)
        payload["columns"] = {
            "id": mapping.id_col,
            "zone": mapping.zone_col,
            "wall": mapping.wall_col,
            "date": mapping.date_col,
            "time": mapping.time_col,
        }
        json.dump(payload, out, indent=2)
        out.write("\n")
    else:
        out.write(render_blast(report, limit=args.limit))

    if args.sql:
        sql = emit_correction_sql(
            report,
            table=args.table,
            instant_column=args.instant_column,
            id_column=args.id_column,
            quote_ids=not args.unquoted_ids,
        )
        if args.sql == "-":
            out.write("\n")
            out.write(sql)
        else:
            target = Path(args.sql)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(sql, encoding="utf-8")
            except OSError as exc:
                print(f"gnomon: cannot write {target}: {exc}", file=err)
                return EXIT_ERROR
            if not args.json:
                out.write(f"\nCorrective SQL written to {target}\n")

    return EXIT_FINDINGS if report.max_severity >= threshold else EXIT_OK


def _cmd_resolve(args: argparse.Namespace, out: TextIO) -> int:
    """Run the ``resolve`` subcommand."""
    left = load_tree(args.left, include_legacy=args.include_legacy)
    right = load_tree(args.right, include_legacy=args.include_legacy)
    year, month, day, seconds = parse_naive(args.wall)
    naive = civil_to_instant(year, month, day, seconds)
    # A single TZif file is keyed by its filename, so honour --zone by name where
    # a tree was given and fall back to the sole zone where a file was.
    left_rules = (
        left.zones[left.sole_zone] if left.single_file else left.zones.get(args.zone)
    )
    right_rules = (
        right.zones[right.sole_zone] if right.single_file else right.zones.get(args.zone)
    )
    if left_rules is None and right_rules is None:
        raise ValueError(f"zone {args.zone!r} is in neither tree")

    if args.json:
        from .report import _resolution_json

        payload = {
            "tool": "gnomon",
            "kind": "resolve",
            "zone": args.zone,
            "wall": args.wall,
            "left": {
                "label": left.describe,
                "resolution": _resolution_json(
                    left_rules.resolve_wall(naive) if left_rules else None
                ),
            },
            "right": {
                "label": right.describe,
                "resolution": _resolution_json(
                    right_rules.resolve_wall(naive) if right_rules else None
                ),
            },
        }
        if left_rules is not None and right_rules is not None:
            payload["delta_seconds"] = (
                right_rules.resolve_wall(naive).chosen - left_rules.resolve_wall(naive).chosen
            )
        json.dump(payload, out, indent=2)
        out.write("\n")
        return EXIT_OK

    out.write(
        render_resolve(
            args.zone, args.wall, left.describe, right.describe, left_rules, right_rules, naive
        )
    )
    return EXIT_OK


def _cmd_zones(args: argparse.Namespace, out: TextIO) -> int:
    """Run the ``zones`` subcommand."""
    tree = load_tree(args.tree, include_legacy=args.include_legacy)
    if args.json:
        json.dump(_zones_json(tree, args.grep), out, indent=2)
        out.write("\n")
    else:
        out.write(render_zones(tree, pattern=args.grep, limit=args.limit))
    return EXIT_OK


def _zones_json(tree: ZoneTree, pattern: str | None) -> dict[str, object]:
    """Serialise a zone listing."""
    names = sorted(tree.zones)
    if pattern:
        needle = pattern.lower()
        names = [name for name in names if needle in name.lower()]
    return {
        "tool": "gnomon",
        "kind": "zones",
        "root": str(tree.root),
        "label": tree.label,
        "zone_count": len(tree.zones),
        "alias_groups": {k: v for k, v in tree.alias_sets().items()},
        "zones": [
            {
                "zone": name,
                "version": tree.zones[name].version,
                "transitions": len(tree.zones[name].transitions),
                "footer": tree.zones[name].posix.spec if tree.zones[name].posix else None,
                "warnings": list(tree.zones[name].warnings),
            }
            for name in names
        ],
        "unreadable": tree.unreadable,
        "skipped": tree.skipped,
    }


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
