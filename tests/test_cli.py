"""End-to-end tests: the CLI, the tree walker, and the shipped samples.

These run the real subcommands over the real generated trees, so they cover the
wiring between modules and the documented exit codes -- the contract a CI job
actually depends on.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from gnomon.cli import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK, main
from gnomon.errors import TreeError
from gnomon.tree import load_tree

from conftest import na_dst_zone

SAMPLE_CSV = Path(__file__).resolve().parent.parent / "samples" / "scheduled.csv"


def _run(*argv: str) -> tuple[int, str, str]:
    """Invoke the CLI, capturing both streams."""
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# ---- tree loading --------------------------------------------------------


def test_load_tree_finds_zones_and_reads_the_version_label(new_tree: Path) -> None:
    tree = load_tree(new_tree)
    assert tree.label == "2026d"
    assert "America/Vancouver" in tree.zones
    assert not tree.unreadable


def test_load_tree_skips_non_tzif_files(old_tree: Path, tmp_path: Path) -> None:
    import shutil

    copy = tmp_path / "tree"
    shutil.copytree(old_tree, copy)
    (copy / "zone.tab").write_text("# index\nCA\t+4916-12307\tAmerica/Vancouver\n")
    (copy / "README").write_text("not a zone")
    tree = load_tree(copy)
    assert "zone.tab" not in tree.zones  # Skipped by name.
    assert "README" in tree.skipped  # Skipped by magic sniff.


def test_load_tree_reports_a_corrupt_zone_without_giving_up(
    old_tree: Path, tmp_path: Path
) -> None:
    """One bad file must not cost the whole comparison."""
    import shutil

    copy = tmp_path / "tree"
    shutil.copytree(old_tree, copy)
    (copy / "America" / "Broken").write_bytes(b"TZif" + b"\x00" * 60)
    tree = load_tree(copy)
    assert "America/Broken" in tree.unreadable
    assert "America/Vancouver" in tree.zones


def test_load_tree_accepts_a_single_file(new_tree: Path) -> None:
    tree = load_tree(new_tree / "America" / "Vancouver")
    assert list(tree.zones) == ["Vancouver"]


def test_load_tree_rejects_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(TreeError, match="no such path"):
        load_tree(tmp_path / "absent")


def test_load_tree_rejects_a_directory_with_no_zones(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("nothing here")
    with pytest.raises(TreeError, match="no readable TZif files"):
        load_tree(tmp_path)


def test_alias_sets_prefer_the_iana_area_name(new_tree: Path) -> None:
    """America/Vancouver must win over its Canada/Pacific compatibility link."""
    aliases = load_tree(new_tree).alias_sets()
    assert aliases["America/Vancouver"] == ["Canada/Pacific"]
    assert "Canada/Pacific" not in aliases


# ---- diff ----------------------------------------------------------------


def test_diff_finds_the_headline_and_exits_two(old_tree: Path, new_tree: Path) -> None:
    code, out, err = _run("diff", str(old_tree), str(new_tree), "--now", "2026-09-29")
    assert code == EXIT_FINDINGS
    assert err == ""
    assert "FIRST WRONG ANSWER" in out
    assert "2026-11-01" in out
    assert "America/Vancouver" in out
    assert "Canada/Pacific" in out  # Named as an alias, not reported separately.
    assert "CRITICAL" in out


def test_diff_of_a_tree_against_itself_is_clean(new_tree: Path) -> None:
    code, out, _ = _run("diff", str(new_tree), str(new_tree), "--now", "2026-09-29")
    assert code == EXIT_OK
    assert "No divergence" in out


def test_diff_json_is_well_formed(old_tree: Path, new_tree: Path) -> None:
    code, out, _ = _run(
        "diff", str(old_tree), str(new_tree), "--now", "2026-09-29", "--json"
    )
    assert code == EXIT_FINDINGS
    payload = json.loads(out)
    assert payload["tool"] == "gnomon"
    assert payload["kind"] == "tree-diff"
    assert payload["max_severity"] == "CRITICAL"
    assert payload["first_divergence"]["zone"] in {"America/Vancouver", "America/Edmonton"}
    assert payload["right_only"] == ["America/Coyhaique"]
    vancouver = next(z for z in payload["zones"] if z["zone"] == "America/Vancouver")
    assert vancouver["footer"] == {
        "left": "PST8PDT,M3.2.0,M11.1.0",
        "right": "MST7",
        "changed": True,
    }
    assert vancouver["max_abs_delta_seconds"] == 3600


def test_diff_can_restrict_to_one_zone(old_tree: Path, new_tree: Path) -> None:
    code, out, _ = _run(
        "diff", str(old_tree), str(new_tree), "--zone", "Europe/Madrid", "--now", "2026-09-29"
    )
    assert code == EXIT_OK
    assert "No divergence" in out


def test_diff_rejects_an_unknown_zone(old_tree: Path, new_tree: Path) -> None:
    code, _, err = _run("diff", str(old_tree), str(new_tree), "--zone", "Mars/Olympus")
    assert code == EXIT_ERROR
    assert "not present in either tree" in err


def test_history_window_surfaces_only_historical_divergence(
    old_tree: Path, new_tree: Path
) -> None:
    code, out, _ = _run(
        "diff", str(old_tree), str(new_tree), "--now", "2026-09-29",
        "--history", "--zone", "Europe/Chisinau",
    )
    assert code == EXIT_OK  # Historical-only is LOW, below the critical threshold.
    assert "historical-only" in out


def test_fail_on_threshold_is_honoured(old_tree: Path, new_tree: Path) -> None:
    code, _, _ = _run(
        "diff", str(old_tree), str(new_tree), "--now", "2026-09-29",
        "--history", "--zone", "Europe/Chisinau", "--fail-on", "low",
    )
    assert code == EXIT_FINDINGS


def test_bad_fail_on_value_is_an_error(old_tree: Path, new_tree: Path) -> None:
    code, _, err = _run(
        "diff", str(old_tree), str(new_tree), "--fail-on", "catastrophic"
    )
    assert code == EXIT_ERROR
    assert "unknown severity" in err


def test_inverted_window_is_an_error(old_tree: Path, new_tree: Path) -> None:
    code, _, err = _run(
        "diff", str(old_tree), str(new_tree), "--from", "2027-01-01", "--to", "2026-01-01"
    )
    assert code == EXIT_ERROR
    assert "--to must be after --from" in err


def test_bad_date_format_is_an_error(old_tree: Path, new_tree: Path) -> None:
    code, _, err = _run("diff", str(old_tree), str(new_tree), "--now", "29/09/2026")
    assert code == EXIT_ERROR
    assert "YYYY-MM-DD" in err


def test_no_collapse_aliases_reports_each_name(old_tree: Path, new_tree: Path) -> None:
    code, out, _ = _run(
        "diff", str(old_tree), str(new_tree), "--now", "2026-09-29",
        "--no-collapse-aliases", "--json",
    )
    assert code == EXIT_FINDINGS
    names = {zone["zone"] for zone in json.loads(out)["zones"]}
    assert {"America/Vancouver", "Canada/Pacific"} <= names


def test_missing_tree_is_an_error(tmp_path: Path, new_tree: Path) -> None:
    code, _, err = _run("diff", str(tmp_path / "absent"), str(new_tree))
    assert code == EXIT_ERROR
    assert "no such path" in err


# ---- blast ---------------------------------------------------------------


def test_blast_over_the_shipped_sample_data(old_tree: Path, new_tree: Path) -> None:
    code, out, err = _run(
        "blast", str(old_tree), str(new_tree), "--data", str(SAMPLE_CSV),
        "--now", "2026-09-29",
    )
    assert code == EXIT_FINDINGS
    assert err == ""
    assert "CRITICAL" in out
    assert "NEEDS A HUMAN, NOT ARITHMETIC" in out
    assert "CORRECTIONS NEEDED" in out


def test_blast_json_covers_every_class(old_tree: Path, new_tree: Path) -> None:
    """The sample data is meant to exercise all seven classifications."""
    code, out, _ = _run(
        "blast", str(old_tree), str(new_tree), "--data", str(SAMPLE_CSV),
        "--now", "2026-09-29", "--json",
    )
    assert code == EXIT_FINDINGS
    payload = json.loads(out)
    assert set(payload["counts"]) == {
        "agree",
        "shifted",
        "gap-appears",
        "gap-resolves",
        "fold-appears",
        "fold-resolves",
        "zone-missing",
    }
    assert payload["columns"]["zone"] == "zone"
    vancouver = next(r for r in payload["rows"] if r["id"] == "bkg-1001")
    assert vancouver["classification"] == "shifted"
    assert vancouver["delta_seconds"] == -3600
    assert vancouver["is_future"] is True


def test_blast_writes_sql_to_a_file(old_tree: Path, new_tree: Path, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "fix.sql"
    code, out, _ = _run(
        "blast", str(old_tree), str(new_tree), "--data", str(SAMPLE_CSV),
        "--now", "2026-09-29", "--sql", str(target), "--table", "bookings",
        "--instant-column", "starts_at", "--id-column", "booking_id",
    )
    assert code == EXIT_FINDINGS
    assert f"written to {target}" in out
    sql = target.read_text(encoding="utf-8")
    assert "UPDATE bookings SET starts_at" in sql
    assert "WHERE booking_id IN" in sql


def test_blast_sql_to_stdout(old_tree: Path, new_tree: Path) -> None:
    code, out, _ = _run(
        "blast", str(old_tree), str(new_tree), "--data", str(SAMPLE_CSV),
        "--now", "2026-09-29", "--sql", "-",
    )
    assert code == EXIT_FINDINGS
    assert "BEGIN;" in out


def test_blast_reports_a_bad_data_file(old_tree: Path, new_tree: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("alpha,beta\n1,2\n", encoding="utf-8")
    code, _, err = _run("blast", str(old_tree), str(new_tree), "--data", str(bad))
    assert code == EXIT_ERROR
    assert "cannot infer" in err


# ---- resolve -------------------------------------------------------------


def test_resolve_shows_both_answers_and_the_correction(
    old_tree: Path, new_tree: Path
) -> None:
    code, out, _ = _run(
        "resolve", str(old_tree), str(new_tree),
        "--zone", "America/Vancouver", "--wall", "2026-11-15 09:30",
    )
    assert code == EXIT_OK
    assert "2026-11-15 17:30:00Z" in out  # Stale answer, PST.
    assert "2026-11-15 16:30:00Z" in out  # Correct answer, permanent MST.
    assert "they disagree" in out
    assert "-1h" in out


def test_resolve_reports_agreement(old_tree: Path, new_tree: Path) -> None:
    code, out, _ = _run(
        "resolve", str(old_tree), str(new_tree),
        "--zone", "Europe/Madrid", "--wall", "2027-06-01 10:00",
    )
    assert code == EXIT_OK
    assert "the two databases agree" in out


def test_resolve_names_a_multiplicity_change(old_tree: Path, new_tree: Path) -> None:
    code, out, _ = _run(
        "resolve", str(old_tree), str(new_tree),
        "--zone", "America/Vancouver", "--wall", "2026-11-01 01:30",
    )
    assert code == EXIT_OK
    assert "fold" in out
    assert "Multiplicity also changed" in out


def test_resolve_json(old_tree: Path, new_tree: Path) -> None:
    code, out, _ = _run(
        "resolve", str(old_tree), str(new_tree),
        "--zone", "America/Vancouver", "--wall", "2026-12-15 09:00", "--json",
    )
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["delta_seconds"] == -3600
    assert payload["left"]["resolution"]["abbrev"] == "PST"
    assert payload["right"]["resolution"]["abbrev"] == "MST"


def test_resolve_handles_a_zone_only_one_tree_has(old_tree: Path, new_tree: Path) -> None:
    code, out, _ = _run(
        "resolve", str(old_tree), str(new_tree),
        "--zone", "America/Coyhaique", "--wall", "2027-03-01 09:00",
    )
    assert code == EXIT_OK
    assert "zone absent from this database" in out


def test_resolve_rejects_a_zone_in_neither_tree(old_tree: Path, new_tree: Path) -> None:
    code, _, err = _run(
        "resolve", str(old_tree), str(new_tree), "--zone", "Mars/Olympus", "--wall", "2027-01-01"
    )
    assert code == EXIT_ERROR
    assert "in neither tree" in err


def test_resolve_rejects_a_zoned_wall_clock(old_tree: Path, new_tree: Path) -> None:
    code, _, err = _run(
        "resolve", str(old_tree), str(new_tree),
        "--zone", "Europe/Madrid", "--wall", "2027-06-01T10:00:00Z",
    )
    assert code == EXIT_ERROR
    assert "UTC designator" in err


# ---- zones ---------------------------------------------------------------


def test_zones_lists_footers(new_tree: Path) -> None:
    code, out, _ = _run("zones", str(new_tree))
    assert code == EXIT_OK
    assert "America/Vancouver" in out
    assert "MST7" in out
    assert "zones found     : 8" in out


def test_zones_filter(new_tree: Path) -> None:
    code, out, _ = _run("zones", str(new_tree), "--grep", "europe")
    assert code == EXIT_OK
    assert "matched 2 zone(s)" in out
    assert "America/Vancouver" not in out


def test_zones_json_includes_alias_groups(new_tree: Path) -> None:
    code, out, _ = _run("zones", str(new_tree), "--json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["label"] == "2026d"
    assert payload["alias_groups"]["America/Vancouver"] == ["Canada/Pacific"]


def test_zones_reports_warnings_on_a_suspect_file(tmp_path: Path) -> None:
    """A footer contradicting the table must reach the operator."""
    from gnomon.rules import TTInfo, Transition
    from gnomon.timeutil import civil_to_instant
    from gnomon.tzifwrite import write_tzif_file

    write_tzif_file(
        tmp_path / "Odd" / "Zone",
        [TTInfo(-28800, False, "PST"), TTInfo(-25200, True, "PDT")],
        [Transition(civil_to_instant(2026, 11, 1, 9 * 3600), 0)],
        footer="EST5",
    )
    code, out, _ = _run("zones", str(tmp_path), "--json")
    assert code == EXIT_OK
    payload = json.loads(out)
    warnings = payload["zones"][0]["warnings"]
    assert any("footer disagrees" in warning for warning in warnings)


# ---- argument handling ---------------------------------------------------


def test_no_subcommand_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as caught:
        main([], stdout=io.StringIO(), stderr=io.StringIO())
    assert caught.value.code != EXIT_OK


def test_single_file_trees_can_be_diffed(tmp_path: Path) -> None:
    """Two individual zone files, without a surrounding tree."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.write_bytes(na_dst_zone())
    right.write_bytes(na_dst_zone())
    code, out, _ = _run("diff", str(left), str(right), "--now", "2026-09-29")
    assert code == EXIT_OK
    assert "No divergence" in out


def test_single_file_trees_diverging_are_compared(tmp_path: Path) -> None:
    """A real divergence between two lone files must still be found."""
    from conftest import abolished_dst_zone

    left = tmp_path / "Vancouver.old"
    right = tmp_path / "Vancouver.new"
    left.write_bytes(na_dst_zone())
    right.write_bytes(abolished_dst_zone())
    code, out, _ = _run("diff", str(left), str(right), "--now", "2026-09-29")
    assert code == EXIT_FINDINGS
    assert "future-divergence" in out
    assert "2026-11-01" in out


def test_resolve_works_on_single_files(tmp_path: Path) -> None:
    from conftest import abolished_dst_zone

    left = tmp_path / "old"
    right = tmp_path / "new"
    left.write_bytes(na_dst_zone())
    right.write_bytes(abolished_dst_zone())
    code, out, _ = _run(
        "resolve", str(left), str(right), "--zone", "ignored", "--wall", "2026-12-15 09:00"
    )
    assert code == EXIT_OK
    assert "they disagree" in out


def test_blast_refuses_single_file_trees(tmp_path: Path) -> None:
    lone = tmp_path / "Vancouver"
    lone.write_bytes(na_dst_zone())
    code, _, err = _run("blast", str(lone), str(lone), "--data", str(SAMPLE_CSV))
    assert code == EXIT_ERROR
    assert "single TZif file" in err
