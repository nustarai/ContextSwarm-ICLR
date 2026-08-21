from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from contextswarm_mini.models import Task, Verdict
from contextswarm_mini.runner import _evaluate_candidate


class _DelayedAvailabilityGate:
    """Act like a busy evaluator gate that becomes available after 30s."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.releases = 0

    def acquire(self, *, timeout: float) -> bool:
        self.timeouts.append(timeout)
        return timeout > 30.0

    def release(self) -> None:
        self.releases += 1


class _RecordingEvaluator:
    def __init__(self) -> None:
        self.deadlines: list[float] = []

    def evaluate(self, task: Task, _candidate: Path, *, deadline_monotonic: float) -> Verdict:
        self.deadlines.append(deadline_monotonic)
        return Verdict(task.slug, "PROVED", 1.0, 0.0)


class EvaluatorAdmissionTests(unittest.TestCase):
    def test_gate_wait_uses_full_remaining_horizon(self) -> None:
        task = Task(
            slug="task",
            root=Path("."),
            problem_text="",
            baseline_code="",
            metadata={},
        )
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
        self.assertEqual(gate.timeouts, [45.0])
        self.assertEqual(gate.releases, 1)
        self.assertEqual(evaluator.deadlines, [145.0])


if __name__ == "__main__":
    unittest.main()
