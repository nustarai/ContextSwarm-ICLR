from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from contextswarm_mini.models import Task, Verdict
from contextswarm_mini.runner import _evaluate_candidate


class _DelayedAvailabilityGate:
    """Act like a busy evaluator gate that becomes available after polling."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.releases = 0

    def acquire(self, *, timeout: float) -> bool:
        self.timeouts.append(timeout)
        return len(self.timeouts) >= 3

    def release(self) -> None:
        self.releases += 1


class _RecordingEvaluator:
    def __init__(self) -> None:
        self.deadlines: list[float] = []
        self.remote_unsettled_jobs = 0

    def evaluate(self, task: Task, _candidate: Path, *, deadline_monotonic: float) -> Verdict:
        self.deadlines.append(deadline_monotonic)
        return Verdict(task.slug, "PROVED", 1.0, 0.0)


class _UnsettledEvaluator(_RecordingEvaluator):
    def evaluate(self, task: Task, _candidate: Path, *, deadline_monotonic: float) -> Verdict:
        self.deadlines.append(deadline_monotonic)
        self.remote_unsettled_jobs += 1
        return Verdict(
            task.slug,
            "NETWORK_ERROR",
            0.0,
            0.0,
            {"remote_settlement_unconfirmed": True},
        )


class EvaluatorAdmissionTests(unittest.TestCase):
    def _task(self) -> Task:
        return Task(
            slug="task",
            root=Path("."),
            problem_text="",
            baseline_code="",
            metadata={},
        )

    def test_gate_wait_polls_through_full_remaining_horizon(self) -> None:
        task = self._task()
        gate = _DelayedAvailabilityGate()
        evaluator = _RecordingEvaluator()

        with patch("contextswarm_mini.runner.time.monotonic", return_value=100.0):
            verdict = _evaluate_candidate(
                evaluator,
                task,
                Path("result.lean"),
                deadline=145.0,
                gate=gate,  # type: ignore[arg-type]
            )

        self.assertEqual(verdict.status, "PROVED")
        self.assertEqual(gate.timeouts, [0.1, 0.1, 0.1])
        self.assertEqual(gate.releases, 1)
        self.assertEqual(evaluator.deadlines, [145.0])

    def test_unknown_remote_retains_current_permit(self) -> None:
        evaluator = _UnsettledEvaluator()
        gate = threading.BoundedSemaphore(1)

        verdict = _evaluate_candidate(
            evaluator,
            self._task(),
            Path("result.lean"),
            deadline=10_000_000.0,
            gate=gate,
        )

        self.assertEqual(verdict.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)
        self.assertFalse(gate.acquire(blocking=False))

    def test_existing_remote_latch_rejects_without_calling_evaluator(self) -> None:
        evaluator = _RecordingEvaluator()
        evaluator.remote_unsettled_jobs = 1
        gate = threading.BoundedSemaphore(1)

        verdict = _evaluate_candidate(
            evaluator,
            self._task(),
            Path("result.lean"),
            deadline=10_000_000.0,
            gate=gate,
        )

        self.assertEqual(verdict.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
        self.assertEqual(evaluator.deadlines, [])
        self.assertTrue(gate.acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
