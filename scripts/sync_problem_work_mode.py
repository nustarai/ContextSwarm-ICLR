#!/usr/bin/env python3
"""Synchronize the canonical solver Work Mode across benchmark statements."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contextswarm_mini.prompts import render_problem_work_mode


BENCHMARK_ROOT = ROOT / "benchmarks" / "matholympiadbench"
MARKER = "        ## Work Mode\n\n"
NEXT_SECTION = "\n        ## "


def synchronized_text(source: str) -> str:
    """Replace one Work Mode section while preserving any later sections."""

    before, marker, after = source.partition(MARKER)
    if not marker:
        raise ValueError("missing canonical Work Mode marker")
    next_section = after.find(NEXT_SECTION)
    suffix = after[next_section:] if next_section >= 0 else "\n"
    return before + MARKER + render_problem_work_mode() + suffix


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift without editing problem statements",
    )
    args = parser.parse_args()

    paths = sorted(BENCHMARK_ROOT.glob("*/problem.md"))
    if not paths:
        print("no benchmark problem statements found", file=sys.stderr)
        return 2

    drifted: list[Path] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        try:
            expected = synchronized_text(source)
        except ValueError as exc:
            print(f"{path.relative_to(ROOT)}: {exc}", file=sys.stderr)
            return 2
        if source == expected:
            continue
        drifted.append(path)
        if not args.check:
            path.write_text(expected, encoding="utf-8")

    if args.check and drifted:
        for path in drifted:
            print(f"out of sync: {path.relative_to(ROOT)}", file=sys.stderr)
        return 1
    if not args.check:
        print(f"synchronized {len(drifted)} of {len(paths)} problem statements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
