from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest

from contextswarm_mini.config import load_config
from contextswarm_mini.cps import make_policy
from contextswarm_mini.evaluator import MockEvaluator
from contextswarm_mini.models import AgentResult
from contextswarm_mini.runner import RunLogger, _run_mono, _run_task_workers, load_tasks


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
