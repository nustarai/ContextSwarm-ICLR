#!/usr/bin/env python3
"""Collect, pair, audit, and select the six-dataset Figure 4 runs.

The runner emits one ``figure4_run_summary.json`` per arm.  This command
turns the four summaries for each dataset/repeat into validated paired blocks,
then (when all three repeats are present) applies the explicit three-repeat
allocator rule independently per dataset.  Keeping datasets separate avoids
pretending that different task/evaluator contracts are one paired block.

No endpoint, credential, or operator capability is read by this script.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

# ``python scripts/collect_figure4_formal_matrix.py`` sets ``sys.path[0]`` to
# the scripts directory rather than the repository root.  Keep the documented
# direct CLI form working without requiring an operator-specific PYTHONPATH.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextswarm_mini.allocation_audit import build_figure4_paired_repeat
from contextswarm_mini.allocator_selection import (
    AllocatorSelectionError,
    load_paired_repeats,
    load_rule,
    select_allocator,
    write_selection_result,
)
from contextswarm_mini.formal_matrix_artifacts import artifact_eligibility
from scripts.audit_figure4 import audit_figure4


DATASETS = ("clever", "icpc_wf_2025", "matholympiadbench", "putnambench", "usaco", "verina")
POLICIES = ("uniform_refill", "task_state", "trace_state", "llm_scheduler")
REPEATS = (1, 2, 3)


def _json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _summary_candidates(root: Path, dataset: str, repeat: int, policy: str) -> list[Path]:
    base = root / dataset / f"repeat-{repeat:02d}" / policy
    if not base.exists():
        return []
    return sorted(
        (path for path in base.glob("*/figure4_run_summary.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )


def _latest_summary(root: Path, dataset: str, repeat: int, policy: str) -> tuple[Path | None, str | None]:
    candidates = _summary_candidates(root, dataset, repeat, policy)
    valid: list[tuple[Path, dict[str, Any]]] = []
    for path in candidates:
        value = _json(path)
        # The runner deliberately emits diagnostic closeout artifacts after a
        # preflight failure.  They have no horizon origin and must never be
        # mistaken for an official paired arm merely because their summary is
        # newer than a completed run.  The strict Figure 4 audit validates the
        # remaining contents after this eligibility filter.
        meta = _json(path.parent / "run_meta.json")
        horizon_started_at = meta.get("horizon_started_at") if meta else None
        if (
            value
            and value.get("policy") == policy
            and isinstance(horizon_started_at, str)
            and bool(horizon_started_at.strip())
        ):
            eligible, _reasons = artifact_eligibility(path.parent, policy=policy)
            if eligible:
                valid.append((path, value))
    if not valid:
        return None, None
    path, value = valid[-1]
    return path, str(value.get("run_id") or path.parent.name)


def _pair_for_repeat(summary_paths: dict[str, Path], *, dataset: str, repeat: int) -> dict[str, Any]:
    audit = audit_figure4({policy: path.parent for policy, path in summary_paths.items()})
    if not audit.get("ok"):
        details = audit.get("errors") or []
        raise ValueError(f"{dataset} repeat {repeat}: strict arm audit failed: {details}")
    summaries = {policy: _json(path) for policy, path in summary_paths.items()}
    if any(value is None for value in summaries.values()):
        raise ValueError(f"{dataset} repeat {repeat}: malformed summary")
    typed = {policy: value for policy, value in summaries.items() if value is not None}
    reference = typed[POLICIES[0]]
    paired_seed = reference.get("paired_seed")
    paired_id = reference.get("paired_repeat_id", repeat)
    if str(paired_id) != str(repeat):
        raise ValueError(
            f"{dataset} repeat {repeat}: summary paired_repeat_id is {paired_id!r}"
        )
    row = build_figure4_paired_repeat(
        paired_repeat_id=f"repeat-{repeat:02d}",
        paired_seed=paired_seed,
        arms=typed,
        comparison_contract=reference.get("comparison_contract", {}),
    )
    # Keep the human-readable ID stable across datasets so one frozen rule can
    # consume each per-dataset JSONL independently.
    row["comparison_contract"]["paired_repeat_id"] = f"repeat-{repeat:02d}"
    row["comparison_contract_sha256"] = _sha256_json(row["comparison_contract"])
    row["dataset"] = dataset
    return row


def _sha256_json(value: Any) -> str:
    from contextswarm_mini.allocator_selection import canonical_sha256

    return canonical_sha256(value)


def collect_dataset(root: Path, output_root: Path, dataset: str, rule_path: Path) -> dict[str, Any]:
    dataset_output = output_root / dataset
    dataset_output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    chosen: dict[str, str] = {}
    for repeat in REPEATS:
        paths: dict[str, Path] = {}
        for policy in POLICIES:
            path, run_id = _latest_summary(root, dataset, repeat, policy)
            if path is None:
                missing.append({"repeat": repeat, "policy": policy})
            else:
                paths[policy] = path
                chosen[f"repeat-{repeat:02d}/{policy}"] = run_id or ""
        if len(paths) == len(POLICIES):
            try:
                rows.append(_pair_for_repeat(paths, dataset=dataset, repeat=repeat))
            except (TypeError, ValueError, KeyError) as exc:
                missing.append({"repeat": repeat, "policy": "paired_block", "reason": str(exc)})

    paired_path = dataset_output / "figure4_paired_repeats.jsonl"
    paired_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    result: dict[str, Any] = {
        "dataset": dataset,
        "expected_repeats": len(REPEATS),
        "paired_repeats": len(rows),
        "missing": missing,
        "chosen_run_ids": chosen,
        "paired_repeats_path": str(paired_path),
        "selection_path": None,
        "selection_status": "pending",
    }
    if len(rows) != len(REPEATS) or missing:
        return result
    try:
        # Re-read through the strict artifact validator before selection.
        load_paired_repeats(paired_path, require_history=True)
        selection = select_allocator(paired_path, rule_path)
    except (AllocatorSelectionError, OSError, ValueError) as exc:
        result["selection_status"] = "invalid"
        result["selection_error"] = str(exc)
        return result
    selection_path = dataset_output / "allocator_selection.json"
    write_selection_result(selection_path, selection)
    result["selection_path"] = str(selection_path)
    result["selection_status"] = selection.get("status", "unknown")
    result["selected_policy"] = selection.get("selected_policy")
    result["selection_phase"] = selection.get("selection_phase")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("runs/figure4_formal_6datasets"),
        help="per-arm run root",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("runs/figure4_formal_6datasets/analysis"),
    )
    parser.add_argument(
        "--rule",
        type=Path,
        default=Path("configs/allocator_selection_rule_formal_3repeat.json"),
    )
    parser.add_argument("--dataset", choices=DATASETS, action="append")
    args = parser.parse_args(argv)
    datasets = tuple(args.dataset or DATASETS)
    report = {
        "schema_version": "contextswarm_figure4_six_dataset_collection_v1",
        "datasets": [collect_dataset(args.root, args.output, dataset, args.rule) for dataset in datasets],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "matrix_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    selected = Counter(
        row.get("selected_policy")
        for row in report["datasets"]
        if row.get("selection_status") == "selected"
    )
    print(json.dumps({"report": str(report_path), "selected_policy_counts": dict(selected), "datasets": report["datasets"]}, ensure_ascii=False, sort_keys=True))
    return 0 if all(row["selection_status"] == "selected" for row in report["datasets"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
