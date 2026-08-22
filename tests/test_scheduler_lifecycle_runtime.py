from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import contextswarm_mini.runner as runner_module
from contextswarm_mini.allocation_core import ReadOnlyLLMSchedulerPolicy
from contextswarm_mini.config import load_config
from contextswarm_mini.models import AgentResult
from contextswarm_mini.runner import run_experiment


ROOT = Path(__file__).resolve().parents[1]


def _rows(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _llm_config(base, **allocation_overrides):
    allocation = replace(
        base.allocation,
        policy="llm_scheduler",
        **allocation_overrides,
    )
    return replace(
        base,
        allocation=allocation,
        max_tasks=1,
        max_parallel=1,
        initial_agents_per_task=1,
        max_attempts_per_task=2,
        time_limit_seconds=1,
    )


class SchedulerLifecycleRuntimeTests(unittest.TestCase):
    def _assert_join(self, run_dir: Path, *, expected_outcome: str) -> None:
        decisions = [
            row
            for row in _rows(run_dir / "allocation_decisions.jsonl")
            if row.get("policy") == "llm_scheduler"
            and row.get("scheduler_cost") is not None
        ]
        final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
        scheduler_results = final["allocation_scheduler_agents"]
        events = [
            row
            for row in _rows(run_dir / "events.jsonl")
            if row.get("event") == "allocation_scheduler_finished"
        ]
        self.assertTrue(decisions)
        self.assertEqual(len(scheduler_results), len(decisions))
        self.assertEqual(len(events), len(decisions))
        decision_indexes = [int(row["decision_index"]) for row in decisions]
        result_indexes = [int(row["decision_index"]) for row in scheduler_results]
        event_indexes = [int(row["decision_index"]) for row in events]
        self.assertEqual(len(set(decision_indexes)), len(decisions))
        self.assertEqual(len(set(result_indexes)), len(scheduler_results))
        self.assertEqual(len(set(event_indexes)), len(events))
        decision_call_ids = [str(row["scheduler_call_id"]) for row in decisions]
        result_call_ids = [str(row["scheduler_call_id"]) for row in scheduler_results]
        event_call_ids = [str(row["scheduler_call_id"]) for row in events]
        self.assertEqual(len(set(decision_call_ids)), len(decisions))
        self.assertEqual(len(set(result_call_ids)), len(scheduler_results))
        self.assertEqual(len(set(event_call_ids)), len(events))
        decision_by_index = {int(row["decision_index"]): row for row in decisions}
        result_by_index = {int(row["decision_index"]): row for row in scheduler_results}
        event_by_index = {int(row["decision_index"]): row for row in events}
        self.assertEqual(set(decision_by_index), set(result_by_index))
        self.assertEqual(set(decision_by_index), set(event_by_index))
        for index, decision in decision_by_index.items():
            self.assertEqual(decision["scheduler_outcome"], expected_outcome)
            call_id = decision["scheduler_call_id"]
            self.assertTrue(call_id)
            self.assertEqual(result_by_index[index]["scheduler_call_id"], call_id)
            self.assertEqual(event_by_index[index]["scheduler_call_id"], call_id)
            self.assertEqual(result_by_index[index]["scheduler_outcome"], expected_outcome)
            self.assertEqual(event_by_index[index]["scheduler_outcome"], expected_outcome)
            self.assertEqual(
                (
                    result_by_index[index]["agent_id"],
                    result_by_index[index]["task_id"],
                    result_by_index[index]["episode"],
                ),
                (
                    event_by_index[index]["agent_id"],
                    event_by_index[index]["task_id"],
                    event_by_index[index]["episode"],
                ),
            )
        state = json.loads(
            (run_dir / "elastic_scheduler_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["reservation_slots"], 0)
        self.assertEqual(state["occupied_slots"], 0)
        self.assertEqual(final["health"]["issues"], [])
        self.assertEqual(
            final["health"]["allocation_scheduler_summary_cost_calls"],
            len(decisions),
        )

    def test_prompt_rejection_is_costed_and_admits_bounded_fallback(self) -> None:
        base = load_config("configs/smoke.toml", ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = run_experiment(
                _llm_config(base, prompt_max_bytes=1),
                mock_agent=True,
                output_override=Path(temporary),
            )
            self._assert_join(run_dir, expected_outcome="invalid_output")
            decision = next(
                row
                for row in _rows(run_dir / "allocation_decisions.jsonl")
                if row.get("scheduler_cost") is not None
            )
            self.assertTrue(decision["fallback"])
            self.assertTrue(decision["invalid_output"])
            self.assertEqual(decision["selected_task_id"], "imo2024_p1")

    def _run_first_invocation_failure(self, replacement):
        base = load_config("configs/smoke.toml", ROOT)
        original_choose = ReadOnlyLLMSchedulerPolicy.choose
        seen = False

        def choose(policy, snapshot):
            nonlocal seen
            if not seen:
                seen = True
                original_invoke = policy._invoke
                policy._invoke = replacement
                try:
                    return original_choose(policy, snapshot)
                finally:
                    policy._invoke = original_invoke
            return original_choose(policy, snapshot)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.object(ReadOnlyLLMSchedulerPolicy, "choose", choose):
            return run_experiment(
                _llm_config(base),
                mock_agent=True,
                output_override=Path(temporary.name),
            )

    def test_provider_exception_is_recoverable_and_synthetic_result_is_joined(self) -> None:
        def provider_failure(*_args):
            raise ConnectionError("provider endpoint is unavailable")

        run_dir = self._run_first_invocation_failure(provider_failure)
        self._assert_join(run_dir, expected_outcome="provider_error")
        final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
        first = min(final["allocation_scheduler_agents"], key=lambda row: int(row["decision_index"]))
        self.assertTrue(first["recoverable_invocation_error"])
        self.assertEqual(first["command"], ["<scheduler-invocation-failed>"])

    def test_invalid_invoker_type_is_recoverable_and_joined(self) -> None:
        run_dir = self._run_first_invocation_failure(lambda *_args: object())
        self._assert_join(run_dir, expected_outcome="provider_error")
        final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
        first = min(final["allocation_scheduler_agents"], key=lambda row: int(row["decision_index"]))
        self.assertTrue(first["recoverable_invocation_error"])
        self.assertEqual(first["command"], ["<scheduler-invocation-failed>"])

    def test_malformed_model_output_reuses_staged_real_result(self) -> None:
        original_mock_result = runner_module._mock_result

        class MalformedSchedulerResult(AgentResult):
            def __setattr__(self, name, value):
                if name == "output_tail" and value:
                    value = "{malformed"
                super().__setattr__(name, value)

        def malformed_result(agent_id: str, task_id: str, episode: int):
            result = original_mock_result(agent_id, task_id, episode)
            if task_id == "__allocation__":
                result = MalformedSchedulerResult(**vars(result))
            return result

        base = load_config("configs/smoke.toml", ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(runner_module, "_mock_result", malformed_result):
                run_dir = run_experiment(
                    _llm_config(base),
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            self._assert_join(run_dir, expected_outcome="invalid_output")
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            first = min(final["allocation_scheduler_agents"], key=lambda row: int(row["decision_index"]))
            self.assertEqual(first["command"], ["<mock-agent>"])
            self.assertEqual(first["output_tail"], "{malformed")
            self.assertTrue(first["invalid_output"])

    def test_nested_scheduler_cost_cardinality_is_checked_at_health_closeout(self) -> None:
        original_health = runner_module._run_health

        def tamper_nested_cost(run_dir, *args, **kwargs):
            path = run_dir / "allocation_summary.json"
            summary = json.loads(path.read_text(encoding="utf-8"))
            summary["scheduler_cost"]["calls"] = 999
            path.write_text(
                json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return original_health(run_dir, *args, **kwargs)

        base = load_config("configs/smoke.toml", ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                runner_module,
                "_run_health",
                side_effect=tamper_nested_cost,
            ):
                run_dir = run_experiment(
                    _llm_config(base),
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            health = final["health"]
            self.assertFalse(health["ok"])
            self.assertEqual(health["allocation_scheduler_summary_cost_calls"], 999)
            self.assertNotEqual(
                health["allocation_scheduler_summary_cost_calls"],
                health["allocation_scheduler_charged_decision_count"],
            )
            self.assertIn("allocation_scheduler_cost_cardinality_mismatch", health["issues"])
            self.assertIn("allocation_scheduler_closeout_mismatch", health["issues"])

    def test_charged_scheduler_decision_index_must_be_positive_integer(self) -> None:
        original_choose = ReadOnlyLLMSchedulerPolicy.choose

        def malformed_index(policy, snapshot):
            decision = original_choose(policy, snapshot)
            object.__setattr__(decision, "decision_index", 0)
            return decision

        base = load_config("configs/smoke.toml", ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(ReadOnlyLLMSchedulerPolicy, "choose", malformed_index):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "runner worker/admission failure",
                ):
                    run_experiment(
                        _llm_config(base),
                        mock_agent=True,
                        output_override=Path(temporary),
                    )
            run_dir = next(Path(temporary).iterdir())
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final["status"], "ERROR")
            events = _rows(run_dir / "events.jsonl")
            worker_error = next(
                row for row in events if row.get("event") == "elastic_worker_error"
            )
            self.assertIn(
                "charged scheduler decision is missing a valid decision index",
                worker_error["error"],
            )
            self.assertFalse(
                any(row.get("event") == "allocation_scheduler_finished" for row in events)
            )
