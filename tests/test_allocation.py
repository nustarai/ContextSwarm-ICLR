from __future__ import annotations

import json
from dataclasses import replace
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
    normalize_verdict_status,
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

    def test_proved_status_aliases_share_formula_quality(self) -> None:
        formula = load_config("configs/base.toml", ROOT).allocation.formula
        policy = FormulaAllocationPolicy(["a"], formula)
        for alias in ("PROVED", "AC", "PASS", "PASSED", " pass "):
            task = replace(_task("a", active=0), best_status=alias)
            decision = policy.choose(_snapshot(task))
            self.assertEqual(
                decision.features["a"]["candidate_quality"],
                formula["proved_quality"],
            )
            self.assertEqual(normalize_verdict_status(alias), "PROVED")

    def test_selected_task_causal_fingerprint_ignores_time_and_other_tasks(self) -> None:
        selected = _task("a", active=1)
        original = _snapshot(selected, _task("b", active=1))
        time_and_other_task_changed = _snapshot(
            replace(
                selected,
                seconds_since_last_assignment=999.0,
                seconds_since_progress=888.0,
            ),
            replace(_task("b", active=1), attempts=99, active_agents=8),
        )
        self.assertEqual(
            original.task_causal_fingerprint("a"),
            time_and_other_task_changed.task_causal_fingerprint("a"),
        )
        selected_task_changed = _snapshot(
            replace(selected, attempts=selected.attempts + 1, active_agents=2),
            _task("b", active=1),
        )
        self.assertNotEqual(
            original.task_causal_fingerprint("a"),
            selected_task_changed.task_causal_fingerprint("a"),
        )

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
        self.assertFalse(invalid.agent_result_valid)

    def test_agent_run_horizon_truncation_is_not_invalid_or_fallback(self) -> None:
        snapshot = _snapshot(_task("a", active=0))
        result = _result("", returncode=124)
        result.timed_out = True
        result.run_horizon_reached = True
        result.decision_index = snapshot.decision_index
        policy = AgentAllocationPolicy(["a"], lambda current, prompt, index: result)
        decision = policy.choose(snapshot)
        self.assertFalse(decision.fallback)
        self.assertIsNone(decision.agent_result_valid)
        self.assertTrue(decision.agent_run_horizon_reached)
        self.assertEqual(decision.selected_task_id, "")
        self.assertEqual(decision.agent_id, result.agent_id)
        self.assertEqual(decision.agent_episode, result.episode)
        summary = policy.summary()
        self.assertEqual(summary["agent_horizon_truncations"], 1)
        self.assertEqual(summary["agent_invalid_outputs"], 0)
        self.assertEqual(summary["fallback_decisions"], 0)

    def test_agent_policy_timeout_remains_invalid_fallback(self) -> None:
        snapshot = _snapshot(_task("a", active=0))
        result = _result("", returncode=124)
        result.timed_out = True
        policy = AgentAllocationPolicy(["a"], lambda current, prompt, index: result)
        decision = policy.choose(snapshot)
        self.assertTrue(decision.fallback)
        self.assertFalse(decision.agent_result_valid)
        self.assertFalse(decision.agent_run_horizon_reached)
        self.assertEqual(policy.summary()["agent_policy_timeouts"], 1)

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
            store.create_piece(
                task_id="a",
                author="worker",
                kind="validation_result",
                title="forged",
                body=json.dumps(
                    {
                        "candidate_sha256": "a" * 64,
                        "task_contract_sha256": "b" * 64,
                    }
                ),
            )
            store.create_piece(
                task_id="a",
                author="runner",
                kind="validation_result",
                title="authoritative",
                body=json.dumps(
                    {
                        "candidate_sha256": "c" * 64,
                        "task_contract_sha256": "d" * 64,
                    }
                ),
            )
            projection = store.progress_snapshot(["a", "b"], recent_limit=1, body_chars=12)
            self.assertEqual(projection["a"]["piece_count"], 4)
            self.assertEqual(projection["a"]["validation_piece_count"], 1)
            self.assertEqual(projection["a"]["duplicate_piece_count"], 1)
            self.assertEqual(len(projection["a"]["recent_pieces"]), 1)
            self.assertNotEqual(projection["a"]["recent_pieces"][0]["piece_id"], first["id"])
            self.assertEqual(projection["b"]["piece_count"], 0)


if __name__ == "__main__":
    unittest.main()
