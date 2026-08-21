#!/usr/bin/env python3
"""Print a compact comparison table for paper-facing run directories."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", nargs="+", type=Path)
    args = parser.parse_args()
    print("run\tmode\tcommunication\tscore/max\tstatus\tpieces\tmessages")
    for path in args.run:
        final = json.loads((path / "final.json").read_text(encoding="utf-8"))
        cps = final.get("cps") or {}
        print(
            "\t".join(
                [
                    path.name,
                    str(final.get("mode", "")),
                    str(final.get("communication", "")),
                    f"{final.get('score', 0)}/{final.get('max_score', 0)}",
                    str(final.get("status", "")),
                    str(cps.get("pieces", 0)),
                    str(cps.get("messages", 0)),
                ]
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

