#!/usr/bin/env python3
"""Entry point.

With no arguments this runs a guided demonstration over the bundled synthetic
databases, which reproduce the 2025b/2026d divergence. With arguments it is the
CLI, so ``python main.py diff A B`` and ``python -m gnomon.cli diff A B`` are the
same thing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gnomon.cli import main as cli_main  # noqa: E402

OLD_TREE = _ROOT / "samples" / "trees" / "tzdata-2025b"
NEW_TREE = _ROOT / "samples" / "trees" / "tzdata-2026d"
SAMPLE_CSV = _ROOT / "samples" / "scheduled.csv"

#: The date the bundled samples are narrated around. Passing it explicitly keeps
#: the demo's output stable no matter when it is run.
DEMO_NOW = "2026-09-29"

_STEPS: list[tuple[str, str, list[str]]] = [
    (
        "1. Which zones disagree, and when does the first wrong answer land?",
        "Compares two whole databases interval by interval. The footer change is "
        "what drives a permanent shift, so it is reported next to each verdict.",
        ["diff", str(OLD_TREE), str(NEW_TREE), "--now", DEMO_NOW],
    ),
    (
        "2. One reading, both databases, side by side.",
        "This is the conversion an application performs when a user types a local "
        "time. The two databases return instants an hour apart.",
        [
            "resolve",
            str(OLD_TREE),
            str(NEW_TREE),
            "--zone",
            "America/Vancouver",
            "--wall",
            "2026-11-15 09:30",
        ],
    ),
    (
        "3. Which of my stored rows are already wrong?",
        "Runs a table of wall-clock readings through both inverse maps and "
        "classifies each row, then emits SQL for the ones arithmetic can repair.",
        [
            "blast",
            str(OLD_TREE),
            str(NEW_TREE),
            "--data",
            str(SAMPLE_CSV),
            "--now",
            DEMO_NOW,
            "--sql",
            "-",
            "--table",
            "bookings",
            "--instant-column",
            "starts_at",
            "--id-column",
            "booking_id",
        ],
    ),
]


def ensure_samples() -> None:
    """Generate the synthetic databases if they are not already present."""
    if (OLD_TREE / "+VERSION").is_file() and (NEW_TREE / "+VERSION").is_file():
        return
    print("Generating the synthetic sample databases ...\n")
    result = subprocess.run(
        [sys.executable, str(_ROOT / "samples" / "build_samples.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"sample generation failed with exit code {result.returncode}")


def demo() -> int:
    """Run the three views over the bundled samples and report the exit codes."""
    ensure_samples()
    print("=" * 78)
    print("gnomon -- demonstration over two synthetic tz databases")
    print("=" * 78)
    print(
        "\nThe left database is a 2025-vintage build; the right is current. In 2026\n"
        "British Columbia, Alberta and the Northwest Territories stopped changing\n"
        "their clocks and Morocco settled on permanent UTC. A server still compiled\n"
        "against the older data keeps converting local wall clocks with the old\n"
        "rules, silently, and writes the results into your tables.\n"
    )
    codes: list[int] = []
    for heading, explanation, argv in _STEPS:
        print()
        print("#" * 78)
        print(f"# {heading}")
        print(f"# {explanation}")
        print("#" * 78)
        print()
        codes.append(cli_main(argv))

    print()
    print("=" * 78)
    print("Exit codes:", ", ".join(str(code) for code in codes))
    print("  0 = clean, 2 = findings at or above the --fail-on threshold, 1 = error.")
    print("A CI job or container start-up check can gate on exit code 2 alone.")
    print("=" * 78)
    # The demo itself succeeds when the tool behaved as documented.
    return 0 if codes == [2, 0, 2] else 1


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(cli_main(sys.argv[1:]))
    raise SystemExit(demo())
