from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from contextswarm_mini.config import load_config
from contextswarm_mini.cps import CPSStore, make_policy
from contextswarm_mini.evaluator import MockEvaluator
from contextswarm_mini.models import AgentResult
from contextswarm_mini.provider_diagnostics import provider_diagnostic_class
from contextswarm_mini.runner import (
    RunLogger,
    _run_elastic_cps,
    _run_mono,
    _run_task_workers,
    load_tasks,
)


ROOT = Path(__file__).resolve().parents[1]


class _Broker:
    def __init__(self) -> None:
        self.calls = 0

    @contextmanager
    def session(self, **kwargs):
        self.calls += 1
        yield {
            "CONTEXTSWARM_JUDGE_URL": "http://127.0.0.1:1/test-token",
            "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "9999999999999",
        }


class _FailOncePi:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs):
        self.calls.append(dict(kwargs))
        attempt = len(self.calls)
        now = "2026-01-01T00:00:00+00:00"
        return AgentResult(
            agent_id=str(kwargs["actor_id"]),
            task_id=str(kwargs["task_id"]),
            episode=int(kwargs["episode"]),
            returncode=1 if attempt == 1 else 0,
            started_at=now,
            finished_at=now,
            error_tail="Coordinator response failed" if attempt == 1 else "",
        )


class _FailTwiceThenSucceedPi(_FailOncePi):
    def run(self, **kwargs):
        self.calls.append(dict(kwargs))
        attempt = len(self.calls)
        now = "2026-01-01T00:00:00+00:00"
        return AgentResult(
            agent_id=str(kwargs["actor_id"]),
            task_id=str(kwargs["task_id"]),
            episode=int(kwargs["episode"]),
            returncode=0 if attempt >= 3 else 1,
            started_at=now,
            finished_at=now,
            error_tail="process exited" if attempt < 2 else "",
        )


class RunnerRecoveryIntegrationTests(unittest.TestCase):
    def test_formal_24_slot_wave_refills_provider_failed_assignment(self):
        """A formal-sized CPS wave keeps its 24-slot contract after provider noise.

        This intentionally exercises 12 tasks × 2 initial agents (24
        in-flight assignments), rather than a one-task unit fixture.  One
        logical actor exhausts its in-session provider retry; the elastic
        scheduler releases that lease and admits a generation-3 replacement
        while the other 23 initial actors continue untouched.  A successful
        replacement may still retain the overload text for forensics, but its
        settled assistant outcome marks that diagnostic as recovered.
        """

        base = load_config("configs/figure4_dev_cps48_uniform_refill.toml", ROOT)
        config = replace(
            base,
            max_parallel=24,
            initial_agents_per_task=2,
            max_tasks=12,
            max_attempts_per_task=3,
            time_limit_seconds=5,
            pi_recovery_enabled=True,
            pi_recovery_max_restarts=1,
            pi_recovery_base_delay_ms=0,
            aisw_enabled=False,
        )
        tasks = load_tasks(config)
        self.assertEqual(len(tasks), 12)

        class _Broker:
            @contextmanager
            def session(self, **kwargs):  # type: ignore[no-untyped-def]
                del kwargs
                yield {}

        class _ProviderPi:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int]] = []
                self.per_actor: dict[str, int] = {}
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def run(self, **kwargs):  # type: ignore[no-untyped-def]
                actor = str(kwargs["actor_id"])
                task_id = str(kwargs["task_id"])
                episode = int(kwargs["episode"])
                with self.lock:
                    attempt = self.per_actor.get(actor, 0) + 1
                    self.per_actor[actor] = attempt
                    self.calls.append((actor, attempt))
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                # Keep enough overlap for the test to exercise the bounded
                # worker pool, without making the test depend on wall-clock
                # scheduling order.
                time.sleep(0.003)
                with self.lock:
                    self.active -= 1

                now = "2026-01-01T00:00:00+00:00"
                failed_actor = f"agent-{tasks[0].slug}-1"
                if actor == failed_actor and attempt <= 2:
                    return AgentResult(
                        agent_id=actor,
                        task_id=task_id,
                        episode=episode,
                        returncode=1,
                        started_at=now,
                        finished_at=now,
                        error_tail=(
                            "Codex error: Our servers are currently overloaded. "
                            "Please try again later."
                        ),
                        transport_diagnostic=True,
                    )

                # Generation-3 replacements model a provider that recovered
                # after the failed logical slot.  Preserve one diagnostic row
                # so the experiment-level classifier is tested on the same
                # shape emitted by Pi in a real recovery.
                recovered = not actor.endswith("-1") and not actor.endswith("-2")
                return AgentResult(
                    agent_id=actor,
                    task_id=task_id,
                    episode=episode,
                    returncode=0,
                    started_at=now,
                    finished_at=now,
                    error_tail=(
                        "Codex error: Our servers are currently overloaded. "
                        "Please try again later."
                        if recovered
                        else ""
                    ),
                    settled=True,
                    assistant_success=True,
                    transport_diagnostic=recovered,
                    transport_recovered=recovered,
                )

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            store = CPSStore(run_dir / "cps.sqlite3")
            policy = make_policy(config.communication, store)
            pi = _ProviderPi()
            results = _run_elastic_cps(
                config,
                tasks,
                run_dir,
                logger,
                MockEvaluator(),
                pi,
                policy,
                mock_agent=False,
                deadline=time.monotonic() + 5.0,
                evaluator_gate=threading.BoundedSemaphore(4),
                judge_broker=_Broker(),
                scheduler_result_sink=[],
            )

            assignments = [
                json.loads(line)
                for line in (run_dir / "elastic_assignments.jsonl").read_text().splitlines()
                if line.strip()
            ]
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
                if line.strip()
            ]

        initial = [row for row in assignments if row["allocation_phase"] == "initial"]
        adaptive = [row for row in assignments if row["allocation_phase"] == "adaptive"]
        self.assertEqual(len(initial), 24)
        # max_attempts_per_task=3 gives each of the 12 tasks exactly one
        # released-slot refill after its two initial leases.
        self.assertEqual(len(adaptive), 12)
        self.assertEqual(len(results), 36)
        self.assertLessEqual(pi.max_active, config.max_parallel)
        self.assertGreater(pi.max_active, 1)

        initial_ids = {row["agent_id"] for row in initial}
        initial_call_counts = {
            actor: count
            for actor, count in pi.per_actor.items()
            if actor in initial_ids
        }
        failed_actor = f"agent-{tasks[0].slug}-1"
        self.assertEqual(initial_call_counts[failed_actor], 2)
        self.assertEqual(
            sum(count == 2 for count in initial_call_counts.values()),
            1,
        )
        self.assertEqual(
            sum(count == 1 for actor, count in initial_call_counts.items() if actor != failed_actor),
            23,
        )
        self.assertTrue(any(row["agent_id"] not in initial_ids for row in adaptive))
        self.assertLessEqual(
            max(int(row["active_slots"]) for row in events if row.get("event") == "agent_assigned"),
            config.max_parallel,
        )

        failed_rows = [
            row
            for row in events
            if row.get("event") == "agent_finished"
            and row.get("agent_id") == failed_actor
        ]
        # Pi's two in-session attempts are represented by one terminal
        # ``agent_finished`` row; the per-attempt evidence lives in the
        # ``agent_recovery_*`` lifecycle events.
        self.assertEqual(len(failed_rows), 1)
        self.assertTrue(
            all(
                provider_diagnostic_class(row.get("error_tail")) == "provider_overload"
                and row.get("returncode") == 1
                for row in failed_rows
            )
        )
        recovery_failures = [
            row
            for row in events
            if row.get("event") == "agent_recovery_failure_observed"
            and row.get("agent_id") == failed_actor
        ]
        self.assertEqual(len(recovery_failures), 2)
        recovered_rows = [
            row
            for row in events
            if row.get("event") == "agent_finished"
            and row.get("transport_recovered") is True
        ]
        self.assertTrue(recovered_rows)
        self.assertTrue(
            all(
                row.get("settled") is True
                and row.get("assistant_success") is True
                and row.get("returncode") == 0
                for row in recovered_rows
            )
        )

    def test_mono_does_not_launch_after_horizon_guard(self):
        base = load_config("configs/mono.toml", ROOT)
        config = replace(base, max_tasks=1)
        tasks = load_tasks(config)
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            pi = _FailOncePi()
            broker = _Broker()
            result, verdicts = _run_mono(
                config,
                tasks,
                run_dir,
                logger,
                MockEvaluator(),
                pi,
                mock_agent=False,
                deadline=time.monotonic() - 0.001,
                evaluator_gate=threading.BoundedSemaphore(1),
                judge_broker=broker,
            )

            self.assertEqual(pi.calls, [])
            self.assertTrue(result.run_horizon_reached)
            self.assertEqual(next(iter(verdicts.values())).status, "TIME_LIMIT")

    def test_parallel_does_not_launch_after_horizon_guard(self):
        base = load_config("configs/parallel.toml", ROOT)
        config = replace(base, max_tasks=1)
        tasks = load_tasks(config)
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            pi = _FailOncePi()
            broker = _Broker()
            results = _run_task_workers(
                config,
                tasks,
                run_dir,
                logger,
                MockEvaluator(),
                pi,
                make_policy(config.communication, None),
                mock_agent=False,
                deadline=time.monotonic() - 0.001,
                evaluator_gate=threading.BoundedSemaphore(1),
                judge_broker=broker,
            )

            self.assertEqual(pi.calls, [])
            self.assertEqual(broker.calls, 0)
            self.assertEqual(results[0][1].status, "TIME_LIMIT")

    def test_parallel_worker_restarts_same_actor_inside_one_broker_session(self):
        base = load_config("configs/parallel.toml", ROOT)
        config = replace(
            base,
            max_tasks=1,
            pi_recovery_enabled=True,
            pi_recovery_max_restarts=1,
            pi_recovery_base_delay_ms=0,
        )
        tasks = load_tasks(config)
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            pi = _FailOncePi()
            broker = _Broker()
            results = _run_task_workers(
                config,
                tasks,
                run_dir,
                logger,
                MockEvaluator(),
                pi,
                make_policy(config.communication, None),
                mock_agent=False,
                deadline=time.monotonic() + 2.0,
                evaluator_gate=threading.BoundedSemaphore(1),
                judge_broker=broker,
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(len(pi.calls), 2)
            self.assertEqual(broker.calls, 1)
            self.assertEqual(
                [(call["actor_id"], call["episode"], call["workdir"]) for call in pi.calls],
                [(pi.calls[0]["actor_id"], 1, pi.calls[0]["workdir"])] * 2,
            )
            events = [
                line
                for line in (run_dir / "events.jsonl").read_text().splitlines()
                if "agent_recovery_" in line
            ]
            self.assertTrue(any("agent_recovery_succeeded" in line for line in events))

    def test_parallel_refills_slot_after_recovery_budget_exhaustion(self):
        base = load_config("configs/parallel.toml", ROOT)
        config = replace(
            base,
            max_tasks=1,
            pi_recovery_enabled=True,
            pi_recovery_max_restarts=1,
            pi_recovery_base_delay_ms=0,
        )
        tasks = load_tasks(config)
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            pi = _FailTwiceThenSucceedPi()
            broker = _Broker()
            results = _run_task_workers(
                config,
                tasks,
                run_dir,
                logger,
                MockEvaluator(),
                pi,
                make_policy(config.communication, None),
                mock_agent=False,
                deadline=time.monotonic() + 2.0,
                evaluator_gate=threading.BoundedSemaphore(1),
                judge_broker=broker,
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(len(pi.calls), 3)
            self.assertEqual(broker.calls, 2)
            self.assertEqual(
                [(call["actor_id"], call["episode"], call["workdir"]) for call in pi.calls],
                [(pi.calls[0]["actor_id"], 1, pi.calls[0]["workdir"])] * 3,
            )
            events = (run_dir / "events.jsonl").read_text().splitlines()
            self.assertTrue(any("agent_refill_scheduled" in line for line in events))
            self.assertTrue(any("agent_refill_succeeded" in line for line in events))

    def test_mono_refills_bundle_after_recovery_budget_exhaustion(self):
        base = load_config("configs/mono.toml", ROOT)
        config = replace(
            base,
            max_tasks=1,
            pi_recovery_enabled=True,
            pi_recovery_max_restarts=1,
            pi_recovery_base_delay_ms=0,
        )
        tasks = load_tasks(config)
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            pi = _FailTwiceThenSucceedPi()
            broker = _Broker()
            result, verdicts = _run_mono(
                config,
                tasks,
                run_dir,
                logger,
                MockEvaluator(),
                pi,
                mock_agent=False,
                deadline=time.monotonic() + 2.0,
                evaluator_gate=threading.BoundedSemaphore(1),
                judge_broker=broker,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(len(verdicts), 1)
            self.assertEqual(len(pi.calls), 3)
            self.assertEqual(broker.calls, 1)
            events = (run_dir / "events.jsonl").read_text().splitlines()
            self.assertTrue(any("agent_refill_scheduled" in line for line in events))
            self.assertTrue(any("agent_refill_succeeded" in line for line in events))


if __name__ == "__main__":
    unittest.main()
