from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "allocation_contract_v1.json"


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scores(snapshot: dict[str, object], *, zero_trace: bool = False) -> tuple[dict[str, float], dict[str, float]]:
    parameters = snapshot["parameters"]
    assert isinstance(parameters, dict)
    task_parameters = parameters["task_state"]
    trace_parameters = parameters["trace_state"]
    assert isinstance(task_parameters, dict)
    assert isinstance(trace_parameters, dict)
    tasks = snapshot["tasks"]
    assert isinstance(tasks, dict)

    task_scores: dict[str, float] = {}
    trace_increments: dict[str, float] = {}
    for task_id, raw_task in tasks.items():
        assert isinstance(task_id, str)
        assert isinstance(raw_task, dict)
        ordinary = raw_task["ordinary"]
        trace = raw_task["trace"]
        assert isinstance(ordinary, dict)
        assert isinstance(trace, dict)
        denominator = 1.0 + float(raw_task["active_leases"])
        task_scores[task_id] = round((
            float(task_parameters["quality_weight"]) * float(ordinary["checker_quality"])
            + float(task_parameters["progress_recency_weight"]) * float(ordinary["improvement_recency"])
            + float(task_parameters["starvation_weight"]) * float(ordinary["starvation"])
            - float(task_parameters["failure_penalty"]) * float(ordinary["failure_no_improvement"])
        ) / denominator, 8)

        if zero_trace:
            trace_increments[task_id] = 0.0
            continue
        density = (
            float(trace_parameters["duplicate_component_weight"]) * float(trace["duplication"])
            + float(trace_parameters["refutation_component_weight"]) * float(trace["refutation"])
            + float(trace_parameters["staleness_component_weight"]) * float(trace["staleness"])
            + float(trace_parameters["lineage_stagnation_component_weight"])
            * float(trace["lineage_stagnation"])
        )
        trace_increments[task_id] = round((
            float(trace_parameters["frontier_weight"]) * float(trace["frontier"])
            + float(trace_parameters["association_weight"]) * float(trace["evidence_association"])
            + float(trace_parameters["positive_feedback_weight"]) * float(trace["positive_feedback"])
            - float(trace_parameters["negative_feedback_weight"]) * float(trace["negative_feedback"])
            - float(trace_parameters["density_penalty_weight"]) * density
        ) / denominator, 8)
    return task_scores, trace_increments


def _select(scores: dict[str, float], eligible: list[str]) -> str:
    return min(eligible, key=lambda task_id: (-scores[task_id], task_id))


class AllocationContractFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_identity_and_policy_names_are_frozen(self) -> None:
        self.assertEqual(
            self.fixture["canonical_policy_names"],
            ["uniform_refill", "task_state", "trace_state", "llm_scheduler"],
        )
        snapshot = dict(self.fixture["snapshot"])
        state_id = snapshot.pop("state_id")
        self.assertEqual(state_id, _canonical_sha256(snapshot))
        self.assertEqual(
            self.fixture["expected"]["allocation_audit"]["state_id"],
            state_id,
        )

    def test_capacity_and_active_lease_semantics(self) -> None:
        snapshot = self.fixture["snapshot"]
        self.assertEqual(
            snapshot["active_solver_slots"]
            + snapshot["scheduler_reserved_slots"]
            + snapshot["free_slots"],
            snapshot["total_capacity"],
        )
        tasks = snapshot["tasks"]
        self.assertEqual(
            sum(task["active_leases"] for task in tasks.values()),
            snapshot["active_solver_slots"],
        )
        eligible = snapshot["eligible_task_ids"]
        uniform_choice = min(
            eligible,
            key=lambda task_id: (tasks[task_id]["active_leases"], task_id),
        )
        self.assertEqual(
            uniform_choice,
            self.fixture["expected"]["uniform_refill_selected_task_id"],
        )

    def test_scores_zero_increment_and_no_double_count(self) -> None:
        snapshot = self.fixture["snapshot"]
        expected = self.fixture["expected"]
        task_scores, trace_increments = _scores(snapshot)
        trace_scores = {
            task_id: task_scores[task_id] + trace_increments[task_id]
            for task_id in task_scores
        }
        self.assertEqual(task_scores, expected["task_only_scores"])
        self.assertEqual(trace_increments, expected["trace_increments"])
        self.assertEqual(trace_scores, expected["trace_total_scores"])
        eligible = snapshot["eligible_task_ids"]
        self.assertEqual(_select(task_scores, eligible), expected["task_state_selected_task_id"])
        self.assertEqual(_select(trace_scores, eligible), expected["trace_state_selected_task_id"])

        zero_task_scores, zero_increments = _scores(snapshot, zero_trace=True)
        zero_trace_scores = {
            task_id: zero_task_scores[task_id] + zero_increments[task_id]
            for task_id in zero_task_scores
        }
        self.assertEqual(zero_trace_scores, zero_task_scores)
        self.assertEqual(
            _select(zero_trace_scores, eligible),
            expected["zero_increment_selected_task_id"],
        )
        for task in snapshot["tasks"].values():
            self.assertTrue(
                set(task["ordinary"]["checker_outcome_ids"]).isdisjoint(
                    task["trace"]["source_outcome_ids"]
                )
            )

    def test_counterfactual_conserves_capacity(self) -> None:
        audit = self.fixture["expected"]["allocation_audit"]
        trace_after = audit["trace_state_allocation_after"]
        task_after = audit["task_state_allocation_after"]
        delta = sum(trace_after[key] - task_after[key] for key in trace_after)
        self.assertEqual(delta, 0)
        self.assertEqual(delta, audit["capacity_delta_sum"])
        self.assertTrue(audit["capacity_conserved"])
        self.assertEqual(sum(trace_after.values()), audit["active_slots_after"])
        self.assertEqual(sum(task_after.values()), audit["active_slots_after"])
        self.assertEqual(
            audit["active_slots_after"]
            + audit["free_slots_after"]
            + audit["scheduler_reserved_slots_after"],
            audit["total_capacity"],
        )

    def test_cost_artifacts_and_fixed_fields_are_complete(self) -> None:
        self.assertEqual(
            set(self.fixture["llm_scheduler_cost"]),
            {
                "calls",
                "input_tokens",
                "output_tokens",
                "latency_seconds",
                "capacity_reservations",
                "occupied_capacity_slot_seconds",
                "invalid_outputs",
                "fallback_count",
            },
        )
        self.assertEqual(
            set(self.fixture["artifact_schemas"]),
            {
                "allocation_decisions.jsonl",
                "allocation_audit.jsonl",
                "figure4_run_summary.json",
                "figure4_paired_repeats.jsonl",
            },
        )
        schemas = set(self.fixture["artifact_schemas"].values())
        self.assertEqual(schemas, set(self.fixture["artifact_required_fields"]))
        audit = self.fixture["expected"]["allocation_audit"]
        self.assertTrue(
            set(self.fixture["artifact_required_fields"][audit["schema_version"]])
            .issubset(audit)
        )
        fixed = set(self.fixture["fixed_arm_comparison_fields"])
        self.assertTrue(
            {
                "selector_identity",
                "trace_visibility",
                "ordered_task_ids",
                "paired_seed",
                "evaluator_contract_sha256",
                "horizon_seconds",
                "total_capacity",
                "candidate_transfer",
                "direct_messages_enabled",
            }.issubset(fixed)
        )


if __name__ == "__main__":
    unittest.main()
