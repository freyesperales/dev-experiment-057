#!/usr/bin/env python3
"""Remove generated samples and caches.

A script rather than a shell one-liner in the Makefile, so it behaves the same on
Windows, macOS and Linux and can be run directly without make.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CACHE_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")


def main() -> int:
    """Delete caches and the generated sample trees, reporting each removal."""
    targets: list[Path] = [_ROOT / "samples" / "trees"]
    for pattern in _CACHE_DIRS:
        targets.extend(_ROOT.rglob(pattern))

    removed = 0
    for target in targets:
        if not target.exists():
            continue
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            print(f"could not remove {target}")
        else:
            print(f"removed {target.relative_to(_ROOT)}")
            removed += 1
    print(f"{removed} path(s) removed. Regenerate samples with: python samples/build_samples.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
