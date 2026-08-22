from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextswarm_mini.allocation_core import ReadOnlyLLMSchedulerPolicy
from contextswarm_mini.config import load_config
from contextswarm_mini.runner import run_experiment


ROOT = Path(__file__).resolve().parents[1]


def _rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class SchedulerIndexBindingTests(unittest.TestCase):
    def _config(self):
        base = load_config("configs/smoke.toml", ROOT)
        return replace(
            base,
            allocation=replace(base.allocation, policy="llm_scheduler"),
            max_tasks=1,
            max_parallel=1,
            initial_agents_per_task=1,
            max_attempts_per_task=2,
            time_limit_seconds=1,
        )

    def _run_with_corrupt_decision(self, mutate):
        original_choose = ReadOnlyLLMSchedulerPolicy.choose
        seen = False

        def corrupt(policy, snapshot):
            nonlocal seen
            decision = original_choose(policy, snapshot)
            if not seen:
                seen = True
                decision = mutate(decision)
            return decision

        output = tempfile.TemporaryDirectory()
        self.addCleanup(output.cleanup)
        with patch.object(ReadOnlyLLMSchedulerPolicy, "choose", corrupt):
            # The runner exposes one stable worker/admission exception after
            # latching the malformed-policy failure; the detailed cause is
            # retained in the worker event below.
            with self.assertRaisesRegex(
                RuntimeError, "runner worker/admission failure"
            ):
                run_experiment(
                    self._config(),
                    mock_agent=True,
                    output_override=Path(output.name),
                )
        run_dirs = list(Path(output.name).iterdir())
        self.assertEqual(len(run_dirs), 1)
        return run_dirs[0]

    def _assert_no_orphan_artifact(self, run_dir: Path) -> None:
        final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
        self.assertEqual(final["status"], "ERROR")
        self.assertFalse(final["health"]["ok"])
        self.assertIn("runner_or_worker_error", final["health"]["issues"])
        self.assertEqual(final["allocation_scheduler_agents"], [])
        self.assertEqual(
            _rows(run_dir / "allocation_decisions.jsonl"),
            [],
        )
        events = _rows(run_dir / "events.jsonl")
        self.assertFalse(
            any(row.get("event") == "allocation_scheduler_finished" for row in events)
        )

    def test_policy_index_must_be_positive_and_match_snapshot(self) -> None:
        def corrupt_index(decision, replacement):
            # Exercise the runner boundary even when the frozen core record
            # itself rejects malformed construction.  A custom/hostile policy
            # can still return a low-level-mutated object.
            object.__setattr__(decision, "decision_index", replacement)
            return decision

        for replacement, expected_error in (
            (0, "invalid decision index"),
            (2, "does not match"),
        ):
            with self.subTest(replacement=replacement):
                run_dir = self._run_with_corrupt_decision(
                    lambda decision, replacement=replacement: corrupt_index(
                        decision, replacement
                    )
                )
                self._assert_no_orphan_artifact(run_dir)
                events = _rows(run_dir / "events.jsonl")
                errors = [
                    row
                    for row in events
                    if row.get("event") in {"elastic_worker_error", "run_error"}
                ]
                self.assertTrue(errors)
                self.assertTrue(
                    any(expected_error in str(row.get("error", "")) for row in errors),
                    errors,
                )

    def test_policy_cannot_remove_cost_from_a_staged_invocation(self) -> None:
        def remove_cost(decision):
            object.__setattr__(decision, "scheduler_cost", None)
            object.__setattr__(decision, "scheduler_call_id", "")
            object.__setattr__(decision, "scheduler_outcome", "not_invoked")
            return decision

        run_dir = self._run_with_corrupt_decision(
            remove_cost
        )
        self._assert_no_orphan_artifact(run_dir)
        events = _rows(run_dir / "events.jsonl")
        errors = [
            row
            for row in events
            if row.get("event") in {"elastic_worker_error", "run_error"}
        ]
        self.assertTrue(
            any("staged scheduler result has no scheduler cost" in str(row.get("error", ""))
                for row in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
