from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from contextswarm_mini.allocation import (
    AgentAllocationPolicy,
    EvidencePiece,
    FormulaAllocationPolicy,
    TaskProgress,
    TaskProgressSnapshot,
    UniformAllocationPolicy,
)
from contextswarm_mini.config import load_config
from contextswarm_mini.cps import CPSStore
from contextswarm_mini.models import AgentResult


ROOT = Path(__file__).resolve().parents[1]


def _task(task_id: str, *, active: int, failures: int = 0, piece: EvidencePiece | None = None) -> TaskProgress:
    return TaskProgress(
        task_id=task_id,
        eligible=True,
        solved=False,
        active_agents=active,
        attempts=2,
        completed_attempts=1,
        best_status="VERIFY_FAIL",
        best_score=0.0,
        last_verdict_status="VERIFY_FAIL",
        last_feedback="missing lemma",
        consecutive_failures=failures,
        seconds_since_last_assignment=60.0,
        seconds_since_progress=30.0,
        piece_count=1 if piece else 0,
        validation_piece_count=0,
        strategy_piece_count=1 if piece else 0,
        duplicate_piece_count=0,
        recent_pieces=(piece,) if piece else (),
    )


def _snapshot(*tasks: TaskProgress, index: int = 1) -> TaskProgressSnapshot:
    return TaskProgressSnapshot(index, 120.0, 3480.0, 1, tuple(tasks))


def _result(output: str, *, returncode: int = 0) -> AgentResult:
    return AgentResult(
        agent_id="allocation-scheduler-1",
        task_id="__allocation__",
        episode=1,
        returncode=returncode,
        started_at="start",
        finished_at="finish",
        output_tail=output,
    )


class AllocationPolicyTests(unittest.TestCase):
    def test_uniform_is_deterministic_and_ignores_progress(self) -> None:
        policy = UniformAllocationPolicy(["a", "b"])
        rich = EvidencePiece("piece-1", "proof_strategy", "route", "body", "worker", "now")
        snapshot = _snapshot(_task("a", active=9, failures=9, piece=rich), _task("b", active=0))
        self.assertEqual(policy.choose(snapshot).selected_task_id, "a")
        self.assertEqual(policy.choose(snapshot).selected_task_id, "b")

    def test_formula_uses_frozen_manifest_parameters(self) -> None:
        formula = load_config("configs/base.toml", ROOT).allocation.formula
        policy = FormulaAllocationPolicy(["a", "b"], formula)
        decision = policy.choose(_snapshot(_task("a", active=8), _task("b", active=0)))
        self.assertEqual(decision.selected_task_id, "b")
        self.assertIn("active_balance", decision.features["b"])
        self.assertEqual(policy.summary()["formula_parameters"], formula)

    def test_agent_receives_no_formula_and_validates_strict_json(self) -> None:
        piece = EvidencePiece("piece-1", "proof_strategy", "route", "body", "worker", "now")
        snapshot = _snapshot(_task("a", active=1, piece=piece), _task("b", active=1))
        prompts: list[str] = []

        def invoke(current: TaskProgressSnapshot, prompt: str, index: int) -> AgentResult:
            prompts.append(prompt)
            return _result(
                json.dumps(
                    {
                        "task_id": "b",
                        "reason": "its blocker has a tractable follow-up",
                        "evidence_piece_ids": [],
                    }
                )
            )

        policy = AgentAllocationPolicy(["a", "b"], invoke)
        decision = policy.choose(snapshot)
        self.assertEqual(decision.selected_task_id, "b")
        self.assertFalse(decision.fallback)
        self.assertNotIn("active_balance_weight", prompts[0])
        self.assertNotIn("candidate_quality_weight", prompts[0])

        invalid = AgentAllocationPolicy(
            ["a", "b"],
            lambda current, prompt, index: _result("```json\n{}\n```"),
        ).choose(snapshot)
        self.assertTrue(invalid.fallback)
        self.assertEqual(invalid.selected_task_id, "a")

    def test_cps_progress_projection_is_bounded_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CPSStore(Path(temporary) / "cps.sqlite3")
            first = store.create_piece(
                task_id="a",
                author="w1",
                kind="proof_strategy",
                title="same route",
                body="x" * 100,
            )
            store.create_piece(
                task_id="a",
                author="w2",
                kind="proof_strategy",
                title="same route",
                body="new",
            )
            projection = store.progress_snapshot(["a", "b"], recent_limit=1, body_chars=12)
            self.assertEqual(projection["a"]["piece_count"], 2)
            self.assertEqual(projection["a"]["duplicate_piece_count"], 1)
            self.assertEqual(len(projection["a"]["recent_pieces"]), 1)
            self.assertNotEqual(projection["a"]["recent_pieces"][0]["piece_id"], first["id"])
            self.assertEqual(projection["b"]["piece_count"], 0)


if __name__ == "__main__":
    unittest.main()
