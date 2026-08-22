#!/usr/bin/env python3
"""Select the Figure 4 allocator from paired-repeat artifacts.

Examples::

    python3 scripts/select_allocator.py \
      --paired-repeats runs/figure4_paired_repeats.jsonl \
      --rule configs/allocator_selection_rule_dev.json \
      --output runs/allocator_selection.json

The command is intentionally separate from ``audit_figure4.py`` and from the
experiment runner.  It exits non-zero and does not publish an output file when
the rule or paired artifacts fail closed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from contextswarm_mini.allocator_selection import (
    AllocatorSelectionError,
    SELECTION_SCHEMA,
    select_allocator,
    write_selection_result,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paired-repeats", required=True, type=Path, help="Figure 4 paired-repeat JSONL/JSON artifact")
    parser.add_argument("--rule", required=True, type=Path, help="Frozen allocator-selection rule JSON")
    parser.add_argument("--output", required=True, type=Path, help="Destination allocator_selection.json")
    args = parser.parse_args(argv)
    try:
        result = select_allocator(
            args.paired_repeats,
            args.rule,
        )
        if result.get("schema_version") != SELECTION_SCHEMA:
            raise AllocatorSelectionError("selector produced an invalid result schema")
        write_selection_result(args.output, result)
        if result.get("status") != "selected":
            print(
                "allocator-selection: no_selection: no arm passed numeric guardrails",
                file=sys.stderr,
            )
            return 3
    except AllocatorSelectionError as exc:
        # Keep diagnostics bounded and free of artifact values.  The exception
        # itself is authored by the pure validator and contains no secrets.
        print(f"allocator-selection: {exc.code}: {exc}", file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError) as exc:
        print(f"allocator-selection: invalid_artifact: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "schema_version": result["schema_version"],
        "status": result["status"],
        "selected_policy": result.get("selected_policy"),
        "output": args.output.name,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
