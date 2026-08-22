from __future__ import annotations

import copy
import contextlib
import io
import json
import random
import tempfile
import unittest
from pathlib import Path

from contextswarm_mini.allocator_selection import (
    AllocatorSelectionError,
    PAIRED_SCHEMA,
    RULE_SCHEMA,
    SELECTION_SCHEMA,
    canonical_sha256,
    development_rule,
    load_rule,
    select_allocator,
    write_selection_result,
)
from scripts.select_allocator import main as cli_main


POLICIES = ("uniform_refill", "task_state", "trace_state", "llm_scheduler")


def _contract(repeat_id: str, seed: int) -> dict[str, object]:
    return {
        "dataset": "matholympiadbench",
        "ordered_task_ids": ["task-a", "task-b"],
        "paired_repeat_id": repeat_id,
        "paired_seed": seed,
        "selector_identity": "nustigmergy-v1",
        "selector_config_sha256": "b" * 64,
        "selector_visibility": "project_shared",
        "model": "paper-model",
        "inference_settings": {"thinking": "max", "temperature": 0},
        "evaluator": {"kind": "judge", "profile": "formal"},
        "runtime_limits": {"pi_timeout_seconds": 30, "judge_timeout_seconds": 30},
        "horizon_seconds": 10,
        "total_capacity": 4,
        "initial_allocation": {"task-a": 2, "task-b": 2},
        "candidate_transfer": False,
        "stopping_rule": "full_score_or_horizon",
        "communication": "blackboard",
        "direct_messages_enabled": False,
    }


def _history(first: float, second: float | None = None) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = [{"elapsed_seconds": first, "accepted_score": 1}]
    if second is not None:
        rows.append({"elapsed_seconds": second, "accepted_score": 2})
    return rows


def _arm(policy: str, *, first: float, second: float | None = None, llm_cost: bool = False, bad_cost: bool = False) -> dict[str, object]:
    params = {
        "policy": policy,
        "task_state": {"checker_quality": 1.0, "recent_progress": 1.0},
        "trace_state": {"actionability": 1.0, "drag": 1.0},
        "normalization": {"window": 600},
    }
    history = _history(first, second)
    solver_tokens = 100
    solver_slots = 10.0
    scheduler = {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "latency_seconds": 0.0,
        "capacity_reservations": 0,
        "occupied_capacity_slot_seconds": 0.0,
        "invalid_outputs": 0,
        "fallback_count": 0,
        "horizon_truncations": 0,
    }
    if llm_cost:
        scheduler = {
            "calls": 4,
            "input_tokens": 20,
            "output_tokens": 10,
            "total_tokens": 30,
            "latency_seconds": 1.0,
            "capacity_reservations": 4,
            "occupied_capacity_slot_seconds": 8.0,
            "invalid_outputs": 0,
            "fallback_count": 0,
            "horizon_truncations": 0,
        }
    if bad_cost:
        scheduler["occupied_capacity_slot_seconds"] = 0.0
        scheduler["total_tokens"] = 1
    return {
        "policy": policy,
        "max_score": 2,
        "accepted_score_history": history,
        "final_accepted_score": len(history),
        "time_to_k_seconds": {"1": first, "2": second},
        "nauc": 0.0,  # filled after constructing the row
        "solver_usage": {
            "calls": 4,
            "input_tokens": solver_tokens // 2,
            "output_tokens": solver_tokens // 2,
            "total_tokens": solver_tokens,
            "slot_seconds": solver_slots,
            "max_occupied_slots": 4,
        },
        "scheduler_cost": scheduler,
        "allocation_metrics": {
            "decisions": 4,
            "admitted_decisions": 4,
            "fallbacks": 0,
            "fallback_rate": 0.0,
            "invalid_outputs": 0,
            "horizon_truncations": 0,
        },
        "allocation_parameters": params,
        "allocation_config_sha256": canonical_sha256(params),
    }


def _nauc(history: list[dict[str, float]], horizon: float = 10.0, max_score: float = 2.0) -> float:
    previous = 0.0
    score = 0.0
    area = 0.0
    for row in history:
        elapsed = row["elapsed_seconds"]
        area += score * (elapsed - previous)
        score = row["accepted_score"]
        previous = elapsed
    area += score * (horizon - previous)
    return area / (horizon * max_score)


def _row(repeat_id: str, seed: int, *, llm_bad: bool = False, trace_first: float = 1.0) -> dict[str, object]:
    contract = _contract(repeat_id, seed)
    arms: dict[str, dict[str, object]] = {}
    settings = {
        "uniform_refill": (3.0, None),
        "task_state": (2.0, None),
        "trace_state": (trace_first, 1.5),
        "llm_scheduler": (0.5, 1.0),
    }
    for policy in POLICIES:
        first, second = settings[policy]
        arm = _arm(policy, first=first, second=second, llm_cost=policy == "llm_scheduler", bad_cost=False)
        if llm_bad and policy == "llm_scheduler":
            # A finite but over-budget cost is an eligibility failure, while
            # token arithmetic remains internally reconcilable.
            arm["scheduler_cost"]["occupied_capacity_slot_seconds"] = 8.0
            arm["scheduler_cost"]["total_tokens"] = 30
        arm["nauc"] = _nauc(arm["accepted_score_history"])
        arms[policy] = arm
    return {
        "schema_version": PAIRED_SCHEMA,
        "paired_repeat_id": repeat_id,
        "paired_seed": seed,
        "comparison_contract": contract,
        "comparison_contract_sha256": canonical_sha256(contract),
        "arms": arms,
        "registered_contrasts": {},
    }


def _rule(*ids: str) -> dict[str, object]:
    rule = development_rule(validation_repeat_ids=ids)
    rule["minimum_validation_repeats"] = len(ids)
    rule["target_k"] = 1
    rule["bootstrap"]["draws"] = 10_000
    return rule


def _validation_rows(**kwargs: object) -> list[dict[str, object]]:
    return [_row(f"r{i}", 10 + i, **kwargs) for i in range(1, 9)]


class AllocatorSelectionTests(unittest.TestCase):
    def test_checked_in_development_rule_is_frozen_and_parseable(self) -> None:
        rule = load_rule(Path(__file__).resolve().parents[1] / "configs" / "allocator_selection_rule_dev.json")
        self.assertEqual(rule["minimum_validation_repeats"], 8)
        self.assertEqual(rule["bootstrap"]["draws"], 10_000)
        self.assertTrue(rule["guardrails"]["require_per_block"])

    def test_selects_highest_eligible_nauc_and_emits_identity(self) -> None:
        rows = _validation_rows()
        result = select_allocator(rows, _rule(*[f"r{i}" for i in range(1, 9)]))
        self.assertEqual(result["schema_version"], SELECTION_SCHEMA)
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected_policy"], "trace_state")
        self.assertEqual(result["validation_repeat_ids"], [f"r{i}" for i in range(1, 9)])
        self.assertEqual(result["arms"]["trace_state"]["allocation_config_sha256"], rows[0]["arms"]["trace_state"]["allocation_config_sha256"])
        self.assertEqual(result["paired_contrasts"]["trace_state_minus_task_state"]["bootstrap_ci95"]["lower"] > 0, True)

    def test_cost_guardrail_filters_llm_before_metric_ranking(self) -> None:
        rows = _validation_rows(llm_bad=True)
        # Make LLM's metric highest while keeping its malformed total-token
        # accounting; the arm must be ineligible rather than selected.
        for row in rows:
            arm = row["arms"]["llm_scheduler"]
            arm["accepted_score_history"] = _history(0.1, 0.2)
            arm["time_to_k_seconds"] = {"1": 0.1, "2": 0.2}
            arm["nauc"] = _nauc(arm["accepted_score_history"])
        result = select_allocator(rows, _rule(*[f"r{i}" for i in range(1, 9)]))
        self.assertFalse(result["arms"]["llm_scheduler"]["eligible"])
        self.assertNotEqual(result["selected_policy"], "llm_scheduler")

    def test_permutation_of_rows_is_deterministic(self) -> None:
        rows = _validation_rows()
        shuffled = copy.deepcopy(rows)
        random.Random(4).shuffle(shuffled)
        ids = [f"r{i}" for i in range(1, 9)]
        result_a = select_allocator(rows, _rule(*ids))
        result_b = select_allocator(shuffled, _rule(*ids))
        self.assertEqual(result_a, result_b)

    def test_exact_tie_uses_registry_order(self) -> None:
        rows = _validation_rows(trace_first=1.0)
        # Give Uniform and Task identical trajectories and config identities;
        # both pass, so the fixed registry order decides the tie.
        for row in rows:
            for policy in ("uniform_refill", "task_state"):
                arm = row["arms"][policy]
                arm["accepted_score_history"] = _history(1.0, 2.0)
                arm["time_to_k_seconds"] = {"1": 1.0, "2": 2.0}
                arm["final_accepted_score"] = 2
                arm["nauc"] = _nauc(arm["accepted_score_history"])
            arm = row["arms"]["trace_state"]
            arm["accepted_score_history"] = _history(1.0, 2.0)
            arm["time_to_k_seconds"] = {"1": 1.0, "2": 2.0}
            arm["final_accepted_score"] = 2
            arm["nauc"] = _nauc(arm["accepted_score_history"])
        result = select_allocator(rows, _rule(*[f"r{i}" for i in range(1, 9)]))
        self.assertEqual(result["selected_policy"], "uniform_refill")

    def test_duplicate_repeat_and_contract_tampering_fail_closed(self) -> None:
        rows = [_row("r1", 11), _row("r1", 12)]
        with self.assertRaises(AllocatorSelectionError):
            select_allocator(rows, _rule("r1", "r1"))
        rows = [_row("r1", 11), _row("r2", 12)]
        rows[1]["comparison_contract"]["model"] = "other-model"
        with self.assertRaises(AllocatorSelectionError):
            select_allocator(rows, _rule("r1", "r2"))

    def test_exact_four_arms_and_config_hash_fail_closed(self) -> None:
        row = _row("r1", 11)
        del row["arms"]["uniform_refill"]
        with self.assertRaises(AllocatorSelectionError):
            select_allocator([row], _rule("r1"))
        row = _row("r1", 11)
        row["arms"]["task_state"]["allocation_parameters"]["normalization"]["window"] = 1
        with self.assertRaises(AllocatorSelectionError):
            select_allocator([row], _rule("r1"))

    def test_missing_cost_or_history_fails_closed(self) -> None:
        row = _row("r1", 11)
        del row["arms"]["task_state"]["scheduler_cost"]
        with self.assertRaises(AllocatorSelectionError):
            select_allocator([row], _rule("r1"))

    def test_rule_requires_explicit_split_and_thresholds(self) -> None:
        row = _row("r1", 11)
        rule = _rule(*[f"r{i}" for i in range(1, 9)])
        del rule["guardrails"]["fallback_rate_max"]
        with self.assertRaises(AllocatorSelectionError):
            select_allocator([row], rule)
        rule = _rule(*[f"r{i}" for i in range(1, 9)])
        rule["bootstrap"]["draws"] = 9_999
        with self.assertRaises(AllocatorSelectionError):
            select_allocator(_validation_rows(), rule)
        rule = _rule(*[f"r{i}" for i in range(1, 9)])
        rule["guardrails"]["require_per_block"] = False
        with self.assertRaises(AllocatorSelectionError):
            select_allocator(_validation_rows(), rule)
        rule = _rule(*[f"missing{i}" for i in range(1, 9)])
        with self.assertRaises(AllocatorSelectionError):
            select_allocator([row], rule)

    def test_cost_cardinality_and_huge_values_fail_closed(self) -> None:
        rows = _validation_rows()
        rows[0]["arms"]["llm_scheduler"]["scheduler_cost"]["capacity_reservations"] = 3
        with self.assertRaises(AllocatorSelectionError):
            select_allocator(rows, _rule(*[f"r{i}" for i in range(1, 9)]))
        rows = _validation_rows()
        rows[0]["arms"]["task_state"]["solver_usage"]["total_tokens"] = 10**10000
        with self.assertRaises(AllocatorSelectionError):
            select_allocator(rows, _rule(*[f"r{i}" for i in range(1, 9)]))

    def test_cli_writes_result_and_leaves_no_partial_file_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paired = root / "figure4_paired_repeats.jsonl"
            paired.write_text("\n".join(json.dumps(_row(f"r{i}", 10 + i), sort_keys=True) for i in range(1, 9)) + "\n")
            rule_path = root / "rule.json"
            rule_path.write_text(json.dumps(_rule(*[f"r{i}" for i in range(1, 9)]), sort_keys=True))
            output = root / "allocator_selection.json"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli_main(["--paired-repeats", str(paired), "--rule", str(rule_path), "--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text())["selected_policy"], "trace_state")
            output.unlink()
            paired.write_text("not-json\n")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertNotEqual(cli_main(["--paired-repeats", str(paired), "--rule", str(rule_path), "--output", str(output)]), 0)
            self.assertFalse(output.exists())
        row = _row("r1", 11)
        del row["arms"]["trace_state"]["accepted_score_history"]
        with self.assertRaises(AllocatorSelectionError):
            select_allocator([row], _rule("r1"))

    def test_atomic_result_write(self) -> None:
        rows = _validation_rows()
        result = select_allocator(rows, _rule(*[f"r{i}" for i in range(1, 9)]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allocator_selection.json"
            write_selection_result(path, result)
            persisted = json.loads(path.read_text())
        self.assertEqual(persisted, result)


if __name__ == "__main__":
    unittest.main()
