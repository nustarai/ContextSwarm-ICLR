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
    print(
        "run\tmode\tcommunication\tallocation\tscore/max\tnorm_auc\tstatus"
        "\tpieces\tmessages\tdecisions\tfallbacks\tscheduler_s\tscheduler_tokens\tslot_util"
    )
    for path in args.run:
        final = json.loads((path / "final.json").read_text(encoding="utf-8"))
        cps = final.get("cps") or {}
        allocation = final.get("allocation") or {}
        score_time = final.get("score_time") or {}
        print(
            "\t".join(
                [
                    path.name,
                    str(final.get("mode", "")),
                    str(final.get("communication", "")),
                    str(allocation.get("policy", "")),
                    f"{final.get('score', 0)}/{final.get('max_score', 0)}",
                    str(score_time.get("normalized_score_time_auc", 0)),
                    str(final.get("status", "")),
                    str(cps.get("pieces", 0)),
                    str(cps.get("messages", 0)),
                    str(allocation.get("decisions", 0)),
                    str(allocation.get("fallback_decisions", 0)),
                    str(allocation.get("total_latency_seconds", 0)),
                    str(allocation.get("scheduler_total_tokens", 0)),
                    str(allocation.get("compute_slot_utilization", 0)),
                ]
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
