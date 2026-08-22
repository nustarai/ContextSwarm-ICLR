from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from contextswarm_mini.allocation_core import ReadOnlyLLMSchedulerPolicy
from contextswarm_mini.allocation_audit import AllocationAuditRecord
from contextswarm_mini.config import load_config
from contextswarm_mini.elastic_scheduler import ElasticScheduler
from contextswarm_mini.runner import run_experiment
import contextswarm_mini.runner as runner_module
from contextswarm_mini.models import Verdict


ROOT = Path(__file__).resolve().parents[1]


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _config(policy: str, *, attempts: int = 3):
    base = load_config("configs/smoke.toml", ROOT)
    return replace(
        base,
        allocation=replace(base.allocation, policy=policy),
        max_tasks=2,
        max_parallel=2,
        initial_agents_per_task=1,
        max_attempts_per_task=attempts,
        time_limit_seconds=2,
    )


class Figure4RuntimeTests(unittest.TestCase):
    def test_four_policies_run_and_trace_audits_exact_admissions(self) -> None:
        initial_orders: list[list[str]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for policy in (
                "uniform_refill",
                "task_state",
                "trace_state",
                "llm_scheduler",
            ):
                run_dir = run_experiment(
                    _config(policy),
                    mock_agent=True,
                    output_override=root / policy,
                )
                assignments = _rows(run_dir / "elastic_assignments.jsonl")
                decisions = _rows(run_dir / "allocation_decisions.jsonl")
                summary = json.loads(
                    (run_dir / "allocation_summary.json").read_text(encoding="utf-8")
                )
                initial_orders.append(
                    [
                        str(row["task_id"])
                        for row in assignments
                        if row["allocation_phase"] == "initial"
                    ]
                )
                self.assertTrue(decisions)
                self.assertTrue(all(row["policy"] == policy for row in decisions))
                assigned = [row for row in decisions if row["disposition"] == "assigned"]
                self.assertEqual(summary["adaptive_assignments"], len(assigned))

                audit_path = run_dir / "allocation_audit.jsonl"
                if policy != "trace_state":
                    self.assertFalse(audit_path.exists())
                    continue
                audits = _rows(audit_path)
                self.assertEqual(len(audits), len(assigned))
                self.assertEqual(
                    {str(row["state_id"]) for row in audits},
                    {str(row["state_id"]) for row in assigned},
                )
                task_ids = {str(row["task_id"]) for row in assignments}
                for row in audits:
                    decoded = AllocationAuditRecord.from_dict(row)
                    self.assertEqual(set(decoded.allocation_before), task_ids)
                    self.assertEqual(
                        decoded.admitted_task_id,
                        decoded.trace_state_selected_task_id,
                    )
                    self.assertEqual(decoded.capacity_delta_sum, 0)
                    self.assertTrue(decoded.capacity_conserved)
            self.assertEqual(initial_orders, [initial_orders[0]] * 4)

    def test_llm_reservation_is_atomic_released_and_costed(self) -> None:
        reservation_seen = False
        original_choose = ReadOnlyLLMSchedulerPolicy.choose
        original_admit = ElasticScheduler.admit_reserved

        def observed_choose(policy, snapshot):
            nonlocal reservation_seen
            # The runtime must hold one occupied scheduler slot while the
            # provider call executes, while the immutable policy state remains
            # the pre-reservation Trace-State snapshot.
            reservation_seen = any(
                scheduler.reservation_slots > 0 for scheduler in schedulers
            )
            return original_choose(policy, snapshot)

        def atomic_admit(scheduler, reservation, task_id, *, now=None):
            reserved_before = scheduler.reservation_slots
            active_before = scheduler.active_slots
            occupied_before = scheduler.occupied_slots
            self.assertGreaterEqual(reserved_before, 1)
            assignment = original_admit(
                scheduler,
                reservation,
                task_id,
                now=now,
            )
            self.assertIsNotNone(assignment)
            self.assertEqual(scheduler.reservation_slots, reserved_before - 1)
            self.assertEqual(scheduler.active_slots, active_before + 1)
            self.assertEqual(scheduler.occupied_slots, occupied_before)
            return assignment

        schedulers: list[ElasticScheduler] = []
        original_init = ElasticScheduler.__init__

        def remember_scheduler(scheduler, *args, **kwargs):
            original_init(scheduler, *args, **kwargs)
            schedulers.append(scheduler)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with (
            patch.object(ElasticScheduler, "__init__", remember_scheduler),
            patch.object(ReadOnlyLLMSchedulerPolicy, "choose", observed_choose),
            patch.object(ElasticScheduler, "admit_reserved", atomic_admit),
        ):
            run_dir = run_experiment(
                _config("llm_scheduler", attempts=2),
                mock_agent=True,
                output_override=Path(temporary.name),
            )

        summary = json.loads(
            (run_dir / "allocation_summary.json").read_text(encoding="utf-8")
        )
        scheduler_state = json.loads(
            (run_dir / "elastic_scheduler_state.json").read_text(encoding="utf-8")
        )
        decisions = _rows(run_dir / "allocation_decisions.jsonl")
        self.assertTrue(reservation_seen)
        self.assertEqual(scheduler_state["reservation_slots"], 0)
        self.assertEqual(scheduler_state["occupied_slots"], 0)
        self.assertEqual(summary["scheduler_cost"]["calls"], len(decisions))
        self.assertGreater(summary["scheduler_reserved_slot_seconds"], 0.0)
        self.assertGreater(summary["scheduler_compute_seconds"], 0.0)
        self.assertTrue(
            all(row["scheduler_cost"]["calls"] == 1 for row in decisions)
        )

    def test_llm_provider_exception_falls_back_and_releases_reservation(self) -> None:
        original_choose = ReadOnlyLLMSchedulerPolicy.choose
        raised = False

        def provider_exception(policy, snapshot):
            nonlocal raised
            if not raised:
                raised = True
                original_invoke = policy._invoke
                policy._invoke = lambda *_args: (_ for _ in ()).throw(
                    ConnectionError("provider offline")
                )
                try:
                    return original_choose(policy, snapshot)
                finally:
                    policy._invoke = original_invoke
            return original_choose(policy, snapshot)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.object(
            ReadOnlyLLMSchedulerPolicy,
            "choose",
            provider_exception,
        ):
            run_dir = run_experiment(
                _config("llm_scheduler", attempts=2),
                mock_agent=True,
                output_override=Path(temporary.name),
            )

        decisions = _rows(run_dir / "allocation_decisions.jsonl")
        fallback = next(row for row in decisions if row["fallback"])
        self.assertIn("ConnectionError", fallback["fallback_reason"])
        self.assertEqual(fallback["scheduler_cost"]["calls"], 1)
        # Admission races are expected: the fallback is still a charged
        # decision even when a peer changes the state before reservation
        # revalidation.  Lifecycle correctness is the stable contract.
        self.assertIn(
            fallback["disposition"],
            {
                "assigned",
                "not_admitted_stale",
                "not_admitted_horizon",
                "not_admitted_ineligible",
            },
        )
        scheduler_state = json.loads(
            (run_dir / "elastic_scheduler_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(scheduler_state["reservation_slots"], 0)
        self.assertEqual(scheduler_state["occupied_slots"], 0)
        final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
        self.assertNotIn("runner_or_worker_error", final["health"]["issues"])
        self.assertNotIn("scheduler_reservations_not_released", final["health"]["issues"])
        self.assertNotIn("scheduler_occupied_slots_not_released", final["health"]["issues"])
        summary = json.loads(
            (run_dir / "allocation_summary.json").read_text(encoding="utf-8")
        )
        charged_indexes = {
            int(row["decision_index"])
            for row in decisions
            if row.get("scheduler_cost") is not None
        }
        result_indexes = {
            int(row["decision_index"])
            for row in final["allocation_scheduler_agents"]
        }
        event_indexes = {
            int(row["decision_index"])
            for row in _rows(run_dir / "events.jsonl")
            if row.get("event") == "allocation_scheduler_finished"
        }
        self.assertEqual(result_indexes, charged_indexes)
        self.assertEqual(event_indexes, charged_indexes)
        self.assertEqual(
            final["health"]["allocation_scheduler_charged_decision_count"],
            len(charged_indexes),
        )
        self.assertAlmostEqual(
            summary["scheduler_cost"]["reserved_slot_seconds"],
            summary["scheduler_reserved_slot_seconds"],
        )

    def test_llm_global_state_change_is_stale_and_not_admitted(self) -> None:
        original_choose = ReadOnlyLLMSchedulerPolicy.choose
        first_call_started = threading.Event()
        allow_first_call = threading.Event()
        mutation_scheduler: list[ElasticScheduler] = []
        first_snapshot_tasks: list[str] = []

        def block_first_choose(policy, snapshot):
            if snapshot.decision_index == 1:
                first_snapshot_tasks[:] = list(snapshot.eligible_task_ids)
                first_call_started.set()
                if not allow_first_call.wait(timeout=2.0):
                    raise RuntimeError("timed out waiting for global stale mutation")
            return original_choose(policy, snapshot)

        original_init = ElasticScheduler.__init__

        def remember_scheduler(scheduler, *args, **kwargs):
            original_init(scheduler, *args, **kwargs)
            mutation_scheduler.append(scheduler)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with (
            patch.object(ReadOnlyLLMSchedulerPolicy, "choose", block_first_choose),
            patch.object(ElasticScheduler, "__init__", remember_scheduler),
        ):
            result: list[Path] = []
            failure: list[BaseException] = []

            def run() -> None:
                try:
                    result.append(
                        run_experiment(
                            _config("llm_scheduler", attempts=2),
                            mock_agent=True,
                            output_override=Path(temporary.name),
                        )
                    )
                except BaseException as exc:  # surfaced below with context
                    failure.append(exc)

            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(first_call_started.wait(timeout=2.0))
            self.assertTrue(mutation_scheduler)
            # Mutate the non-selected competitor while decision 1 is still in
            # flight.  This is a global-state change that selected-task-only
            # revalidation cannot observe.
            self.assertGreaterEqual(len(first_snapshot_tasks), 2)
            competitor = first_snapshot_tasks[-1]
            self.assertTrue(mutation_scheduler[0].task_solved(competitor))
            allow_first_call.set()
            worker.join(timeout=5.0)
            if failure:
                raise failure[0]
            self.assertFalse(worker.is_alive())
            run_dir = result[0]

        self.assertTrue(first_call_started.is_set())
        decisions = _rows(run_dir / "allocation_decisions.jsonl")
        stale = [row for row in decisions if row["disposition"] == "not_admitted_stale"]
        self.assertTrue(stale, decisions)
        assignments = _rows(run_dir / "elastic_assignments.jsonl")
        assigned_decision_ids = {
            int(row["decision_index"])
            for row in assignments
            if row.get("decision_index") is not None
        }
        self.assertTrue(
            all(int(row["decision_index"]) not in assigned_decision_ids for row in stale)
        )
        scheduler_state = json.loads(
            (run_dir / "elastic_scheduler_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(scheduler_state["reservation_slots"], 0)
        self.assertEqual(scheduler_state["occupied_slots"], 0)

    def test_llm_rejected_reserved_admission_releases_and_retries_fresh_decision(self) -> None:
        """A ``None`` reservation conversion is stale, never a hidden fallback."""

        original_admit = ElasticScheduler.admit_reserved
        admit_calls = 0

        def reject_first(scheduler, reservation, task_id, *, now=None):
            nonlocal admit_calls
            admit_calls += 1
            if admit_calls == 1:
                # The scheduler intentionally leaves the reservation held on
                # a rejected conversion. Runner code must release it before
                # entering a fresh decision iteration.
                return None
            return original_admit(scheduler, reservation, task_id, now=now)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.object(ElasticScheduler, "admit_reserved", reject_first):
            run_dir = run_experiment(
                _config("llm_scheduler", attempts=2),
                mock_agent=True,
                output_override=Path(temporary.name),
            )

        self.assertGreaterEqual(admit_calls, 2)
        decisions = _rows(run_dir / "allocation_decisions.jsonl")
        stale = [
            row for row in decisions if row.get("disposition") == "not_admitted_stale"
        ]
        self.assertTrue(stale, decisions)
        assignments = _rows(run_dir / "elastic_assignments.jsonl")
        assigned_indices = {
            int(row["decision_index"])
            for row in assignments
            if row.get("decision_index") is not None
        }
        self.assertTrue(
            all(int(row["decision_index"]) not in assigned_indices for row in stale)
        )
        self.assertTrue(
            any(
                row.get("disposition") == "assigned"
                and int(row["decision_index"]) > int(stale[0]["decision_index"])
                for row in decisions
            )
        )
        scheduler_state = json.loads(
            (run_dir / "elastic_scheduler_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(scheduler_state["reservation_slots"], 0)
        self.assertEqual(scheduler_state["occupied_slots"], 0)
        self.assertEqual(scheduler_state["reservations"], {})
        events = _rows(run_dir / "events.jsonl")
        self.assertFalse(
            any(
                row.get("event") in {"run_error", "elastic_worker_error"}
                for row in events
            )
        )

    def test_candidate_terminal_failure_is_recorded_and_slot_refilled(self) -> None:
        """A job-bound terminal failure is one attempt, not an arm abort."""

        class SequenceEvaluator:
            contract = "a" * 64
            terminal_status = "RESOURCE_LIMIT"
            calls = 0
            lock = threading.Lock()

            def __init__(self, *, prove_without_sorry: bool = False):
                del prove_without_sorry
                type(self).calls = 0

            def expected_task_contract_sha256(self, _task):
                return self.contract

            def evaluate(self, task, candidate, *, deadline_monotonic=None):
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
                if deadline_monotonic is None:
                    status = "VERIFY_FAIL"
                    job = "closeout-job"
                else:
                    with self.lock:
                        type(self).calls += 1
                        call = type(self).calls
                    status = self.terminal_status if call == 1 else "VERIFY_FAIL"
                    job = f"solver-job-{call}"
                response = {"formal_status": status}
                if status in {"RESOURCE_LIMIT", "EXECUTION_TIMEOUT"}:
                    response.update(
                        {
                            "error_kind": (
                                "memory_limit_exceeded"
                                if status == "RESOURCE_LIMIT"
                                else "execution_timeout"
                            ),
                            "terminal_reason": "candidate_attempt_limit",
                            # This deliberately exercises the supplied contract:
                            # a status/retryable label is not candidate-
                            # independent evidence of infrastructure failure.
                            "retryable": True,
                        }
                    )
                return Verdict(
                    task.slug,
                    status,
                    0.0,
                    0.0,
                    response,
                    candidate_sha256=digest,
                    task_contract_sha256=self.contract,
                    judge_job_id=job,
                )

        base = load_config("configs/smoke.toml", ROOT)
        config = replace(
            base,
            allocation=replace(base.allocation, policy="task_state"),
            max_tasks=1,
            max_parallel=1,
            initial_agents_per_task=1,
            max_attempts_per_task=2,
            time_limit_seconds=2,
            lean_max_concurrent_evaluations=1,
        )
        for terminal_status in ("RESOURCE_LIMIT", "EXECUTION_TIMEOUT"):
            with self.subTest(status=terminal_status):
                SequenceEvaluator.terminal_status = terminal_status
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                with patch.object(runner_module, "MockEvaluator", SequenceEvaluator):
                    run_dir = run_experiment(
                        config,
                        mock_agent=True,
                        output_override=Path(temporary.name),
                    )

                events = _rows(run_dir / "events.jsonl")
                assignments = _rows(run_dir / "elastic_assignments.jsonl")
                attempts = [
                    row
                    for row in events
                    if row.get("event") == "evaluation_finished"
                    and row.get("phase") == "solver"
                ]
                self.assertEqual(
                    [row["status"] for row in attempts],
                    [terminal_status, "VERIFY_FAIL"],
                )
                self.assertEqual([row["episode"] for row in attempts], [1, 2])
                self.assertEqual([row["generation"] for row in assignments], [1, 2])
                second_assignment_at = next(
                    index
                    for index, row in enumerate(events)
                    if row.get("event") == "agent_assigned" and row.get("episode") == 2
                )
                first_evaluation_at = next(
                    index
                    for index, row in enumerate(events)
                    if row.get("event") == "evaluation_finished"
                    and row.get("phase") == "solver"
                    and row.get("episode") == 1
                )
                self.assertLess(second_assignment_at, first_evaluation_at)
                self.assertFalse(
                    any(
                        row.get("event") in {"run_error", "elastic_worker_error"}
                        for row in events
                    )
                )
                scheduler_state = json.loads(
                    (run_dir / "elastic_scheduler_state.json").read_text(encoding="utf-8")
                )
                self.assertEqual(scheduler_state["active_slots"], 0)
                self.assertTrue(scheduler_state["tasks"]["imo2024_p1"]["retired"])
                final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
                # A candidate-bound terminal receipt (even one carrying a
                # provider-supplied retryable hint) is ordinary zero-progress
                # feedback.  It must not downgrade the arm to infrastructure
                # failure or force closeout-incomplete classification.
                self.assertEqual(final["status"], "COMPLETED")
                self.assertNotIn(
                    "evaluator_infrastructure_error",
                    final["health"]["issues"],
                )
                self.assertNotIn("closeout_incomplete", final["health"]["issues"])
                self.assertEqual(final["verdicts"]["imo2024_p1"]["score"], 0.0)
                self.assertEqual(
                    final["health"]["attempt_verdict_status_counts"][terminal_status],
                    1,
                )
                self.assertEqual(
                    final["health"]["attempt_verdict_status_counts"]["VERIFY_FAIL"],
                    1,
                )


if __name__ == "__main__":
    unittest.main()
