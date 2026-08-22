from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from contextswarm_mini.allocation import TaskProgress, TaskProgressSnapshot
from contextswarm_mini.allocation_trace_bridge import TraceProjectionBridge
from contextswarm_mini.config import load_config
from contextswarm_mini.models import Verdict
from contextswarm_mini.runner import (
    _core_snapshot_from_legacy,
    _verdict_checker_outcome_ids,
)


ROOT = Path(__file__).resolve().parents[1]


def _task(task_id: str, *, checker_outcome_ids: tuple[str, ...] = ()) -> TaskProgress:
    return TaskProgress(
        task_id=task_id,
        eligible=True,
        solved=False,
        active_agents=1,
        attempts=1,
        completed_attempts=1,
        best_status="VERIFY_FAIL",
        best_score=0.25,
        last_verdict_status="VERIFY_FAIL",
        last_feedback="",
        consecutive_failures=1,
        seconds_since_last_assignment=30.0,
        seconds_since_progress=60.0,
        piece_count=99,
        validation_piece_count=50,
        strategy_piece_count=40,
        duplicate_piece_count=9,
        recent_pieces=(),
        checker_outcome_ids=checker_outcome_ids,
    )


class RunnerAllocationTraceBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = load_config("configs/figure4_dev_cps48_task_state.toml", ROOT)
        self.legacy = TaskProgressSnapshot(
            decision_index=3,
            elapsed_seconds=10.0,
            remaining_seconds=100.0,
            free_slots=2,
            tasks=(_task("a"), _task("b")),
        )

    def test_task_state_snapshot_ignores_trace_store_and_has_zero_projection(self) -> None:
        # The helper has no store parameter at all. Omitting trace_view is the
        # only Task-State path and keeps CPS counts/pieces outside the core.
        core = _core_snapshot_from_legacy(self.legacy, self.base)
        self.assertEqual(core.trace_watermark, "")
        self.assertTrue(all(task.trace == task.trace.__class__() for task in core.tasks))
        self.assertTrue(all(task.trace_reference_ids == () for task in core.tasks))

    def test_bounded_projection_enters_trace_snapshot_and_state_identity(self) -> None:
        view = TraceProjectionBridge(
            synthetic_features={"a": {"actionability": 0.75}}
        ).read(["a", "b"])
        trace_config = replace(
            self.base,
            allocation=replace(self.base.allocation, policy="trace_state"),
        )
        zero = _core_snapshot_from_legacy(self.legacy, trace_config)
        projected = _core_snapshot_from_legacy(
            self.legacy,
            trace_config,
            trace_view=view,
        )
        self.assertEqual(projected.tasks[0].trace.actionability, 0.75)
        self.assertEqual(projected.trace_watermark, view.watermark)
        self.assertNotEqual(projected.state_id, zero.state_id)

    def test_task_state_helper_never_constructs_projection_bridge(self) -> None:
        with patch(
            "contextswarm_mini.allocation_trace_bridge.TraceProjectionBridge.read",
            side_effect=AssertionError("Task-State must not read trace state"),
        ):
            core = _core_snapshot_from_legacy(self.legacy, self.base)
        self.assertEqual(core.trace_watermark, "")

    def test_checker_receipt_ids_are_carried_into_core_state_identity(self) -> None:
        legacy = TaskProgressSnapshot(
            decision_index=4,
            elapsed_seconds=10.0,
            remaining_seconds=100.0,
            free_slots=2,
            tasks=(_task("a", checker_outcome_ids=("judge-17",)), _task("b")),
        )
        core = _core_snapshot_from_legacy(legacy, self.base)
        self.assertEqual(core.tasks[0].checker_outcome_ids, ("judge-17",))
        self.assertIn("judge-17", core.tasks[0].public_dict()["checker_outcome_ids"])

    def test_verdict_receipt_extraction_is_explicit_and_bounded(self) -> None:
        verdict = Verdict(
            "a",
            "VERIFY_FAIL",
            0.0,
            0.1,
            response={
                "receipt_id": "receipt-9",
                "response": {"outcome_id": "outcome-9"},
                "private_receipt_id": "/tmp/private/receipt",
            },
            judge_job_id="job-9",
        )
        self.assertEqual(
            _verdict_checker_outcome_ids(verdict),
            ("job-9", "outcome-9", "receipt-9"),
        )


if __name__ == "__main__":
    unittest.main()
