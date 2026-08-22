from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.audit_figure4 import POLICIES, SUMMARY_SCHEMA, audit_figure4


TASKS = ["task-a", "task-b"]
CONTRACT = {
    "selector_identity": "selector-v1",
    "selector_visibility": "project_shared",
    "model": "paper-model",
    "tasks": TASKS,
    "evaluator": {"kind": "judge", "profile": "formal"},
    "horizon_seconds": 60,
    "total_capacity": 4,
    "initial_allocation": {"task-a": 2, "task-b": 2},
    "candidate_transfer": False,
    "stopping": "full_score_or_horizon",
    "direct_messages": False,
}


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _summary(policy: str) -> dict[str, object]:
    scheduler = {key: 0 for key in ("calls", "input_tokens", "output_tokens", "total_tokens")}
    scheduler.update({"latency_seconds": 0, "reserved_slot_seconds": 0})
    if policy == "llm_scheduler":
        scheduler["calls"] = 1
    return {
        "schema_version": SUMMARY_SCHEMA,
        "run_id": f"run-{policy}",
        "comparison_contract_id": "c" * 64,
        "comparison_contract": CONTRACT,
        "policy": policy,
        "paired_seed": 7,
        "repeat": 1,
        "task_order": TASKS,
        "horizon_seconds": 60,
        "total_capacity": 4,
        "initial_allocation": {"task-a": 2, "task-b": 2},
        "accepted_score_history": [{"elapsed_seconds": 1, "score": 0}],
        "final_accepted_score": 0,
        "time_to_k": {"1": None},
        "nauc": 0,
        "solver_usage": {"calls": 1, "input_tokens": 2, "output_tokens": 3},
        "evaluator_usage": {"calls": 1},
        "scheduler_cost": scheduler,
        "allocation_metrics": {"decisions": 1, "fallbacks": 0},
    }


def _audit_row() -> dict[str, object]:
    return {
        "schema_version": "contextswarm_allocation_audit_v1",
        "state_id": "s1",
        "decision_id": "d1",
        "allocation_config_sha256": "a" * 64,
        "eligible_task_ids": TASKS,
        "task_only_scores": {"task-a": 1, "task-b": 0},
        "trace_increments": {"task-a": 0, "task-b": 0},
        "trace_total_scores": {"task-a": 1, "task-b": 0},
        "trace_state_selected_task_id": "task-a",
        "task_state_selected_task_id": "task-a",
        "admitted_task_id": "task-a",
        "fallback_reason": "",
        "allocation_before": {"task-a": 2, "task-b": 1},
        "trace_state_allocation_after": {"task-a": 3, "task-b": 1},
        "task_state_allocation_after": {"task-a": 3, "task-b": 1},
        "active_slots_before": 3,
        "active_slots_after": 4,
        "free_slots_before": 1,
        "free_slots_after": 0,
        "scheduler_reserved_slots_before": 0,
        "scheduler_reserved_slots_after": 0,
        "total_capacity": 4,
        "capacity_delta_sum": 0,
        "capacity_conserved": True,
    }


def _make_fixture(root: Path, *, bad_policy: str | None = None) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for policy in POLICIES:
        run = root / policy
        paths[policy] = run
        _write(run / "run_meta.json", {"allocation": {"policy": policy}})
        summary = _summary(policy)
        if bad_policy == policy:
            summary["comparison_contract"] = {**CONTRACT, "model": "different"}
        _write(run / "figure4_run_summary.json", summary)
        _write(run / "allocation_decisions.jsonl", {"decision_id": "d1", "policy": policy})
        if policy == "trace_state":
            _write(run / "allocation_audit.jsonl", _audit_row())
    return paths


class Figure4AuditTests(unittest.TestCase):
    def test_valid_four_arm_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = audit_figure4(_make_fixture(Path(directory)))
            self.assertTrue(report["ok"], report)

    def test_contract_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = audit_figure4(_make_fixture(Path(directory), bad_policy="trace_state"))
            self.assertFalse(report["ok"])
            self.assertTrue(any(item["code"] == "comparison_contract_mismatch" for item in report["errors"]))

    def test_sensitive_field_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = _make_fixture(Path(directory))
            _write(paths["uniform_refill"] / "figure4_run_summary.json", {**_summary("uniform_refill"), "api_key": "secret"})
            report = audit_figure4(paths)
            self.assertFalse(report["ok"])
            self.assertTrue(any(item["code"] == "sensitive_field_present" for item in report["arms"]["uniform_refill"]["errors"]))

    def test_trace_capacity_delta_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = _make_fixture(Path(directory))
            row = _audit_row()
            row["capacity_delta_sum"] = 1
            _write(paths["trace_state"] / "allocation_audit.jsonl", row)
            report = audit_figure4(paths)
            self.assertFalse(report["ok"])

    def test_requires_exact_policy_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = _make_fixture(Path(directory))
            paths.pop("llm_scheduler")
            report = audit_figure4(paths)
            self.assertFalse(report["ok"])
            self.assertEqual(report["errors"][0]["code"], "policy_set_invalid")


if __name__ == "__main__":
    unittest.main()
