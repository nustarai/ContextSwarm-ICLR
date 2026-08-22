from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from contextswarm_mini.config import load_config
from contextswarm_mini.context_piece import main as context_piece_main
from contextswarm_mini.cps import CPSStore
from contextswarm_mini.models import Task, Verdict
from contextswarm_mini.runner import (
    RemoteJudgeSettlementError,
    RunLogger,
    _freeze_closeout_candidates,
    _mock_result,
    load_tasks,
    run_experiment,
)


ROOT = Path(__file__).resolve().parents[1]


class _SlowEvaluator:
    delay_seconds = 0.25

    def __init__(self, *, prove_without_sorry: bool = False):
        del prove_without_sorry

    def evaluate(
        self,
        task: Task,
        _candidate: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        del deadline_monotonic
        time.sleep(self.delay_seconds)
        return Verdict(task.slug, "VERIFY_FAIL", 0.0, self.delay_seconds)


class _ProvingEvaluator(_SlowEvaluator):
    delay_seconds = 0.0

    def evaluate(
        self,
        task: Task,
        _candidate: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        del deadline_monotonic
        return Verdict(task.slug, "PROVED", 1.0, 0.0)


class _RecordingEvaluator(_SlowEvaluator):
    calls: list[tuple[str, Path, float | None, str]] = []
    lock = threading.Lock()

    def evaluate(
        self,
        task: Task,
        candidate: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        payload = candidate.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        with self.lock:
            self.calls.append((task.slug, candidate.resolve(), deadline_monotonic, digest))
        phase = "closeout" if deadline_monotonic is None else "solver"
        return Verdict(
            task.slug,
            "PROVED" if phase == "closeout" else "VERIFY_FAIL",
            1.0 if phase == "closeout" else 0.0,
            0.0,
            {"recorded_phase": phase},
        )


class _CancelledEvaluator(_SlowEvaluator):
    delay_seconds = 0.0

    def evaluate(
        self,
        task: Task,
        _candidate: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        del deadline_monotonic
        return Verdict(task.slug, "CANCELLED", 0.0, 0.0)


class _CloseoutUnsettledEvaluator(_SlowEvaluator):
    closeout_calls = 0

    def __init__(self, *, prove_without_sorry: bool = False):
        del prove_without_sorry
        self.remote_unsettled_jobs = 0
        self.remote_settlement_event = threading.Event()

    def evaluate(
        self,
        task: Task,
        _candidate: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        if deadline_monotonic is not None:
            return Verdict(task.slug, "VERIFY_FAIL", 0.0, 0.0)
        type(self).closeout_calls += 1
        self.remote_unsettled_jobs += 1
        self.remote_settlement_event.set()
        return Verdict(
            task.slug,
            "REMOTE_SETTLEMENT_UNCONFIRMED",
            0.0,
            0.0,
            {"remote_settlement_unconfirmed": True},
        )


class CloseoutLifecycleTests(unittest.TestCase):
    def test_closeout_unknown_remote_is_fatal_and_final_lifecycle_is_not_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_config("configs/parallel.toml", ROOT),
                max_tasks=2,
                max_parallel=1,
                episodes_per_task=1,
                time_limit_seconds=2,
                lean_max_concurrent_evaluations=1,
            )
            _CloseoutUnsettledEvaluator.closeout_calls = 0
            with patch(
                "contextswarm_mini.runner.MockEvaluator",
                _CloseoutUnsettledEvaluator,
            ):
                with self.assertRaises(RemoteJudgeSettlementError):
                    run_experiment(
                        config,
                        mock_agent=True,
                        output_override=Path(temporary),
                    )
            run_dir = next(Path(temporary).iterdir())
            lifecycle = json.loads(
                (run_dir / "judge_broker_closeout.json").read_text(encoding="utf-8")
            )
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))

        self.assertEqual(_CloseoutUnsettledEvaluator.closeout_calls, 1)
        self.assertFalse(lifecycle["drained"])
        self.assertEqual(lifecycle["active_handlers"], 0)
        self.assertEqual(lifecycle["fifo_depth"], 0)
        self.assertEqual(lifecycle["remote_unsettled_jobs"], 1)
        self.assertEqual(final["status"], "ERROR")

    def test_all_modes_use_the_same_frozen_closeout_phase(self) -> None:
        for manifest in ("configs/mono.toml", "configs/parallel.toml", "configs/cps.toml"):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as temporary:
                config = replace(
                    load_config(manifest, ROOT),
                    max_tasks=2,
                    max_parallel=1,
                    max_attempts_per_task=1,
                    time_limit_seconds=2,
                    lean_max_concurrent_evaluations=1,
                )
                _RecordingEvaluator.calls = []
                with patch("contextswarm_mini.runner.MockEvaluator", _RecordingEvaluator):
                    run_dir = run_experiment(
                        config,
                        mock_agent=True,
                        output_override=Path(temporary),
                    )
                final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
                self.assertEqual(len(final["verdicts"]), 2)
                frozen = json.loads(
                    (run_dir / "closeout_candidates.json").read_text(encoding="utf-8")
                )["candidates"]
                self.assertEqual(len(frozen), 2)
                self.assertTrue(all(row["candidate_sha256"] for row in frozen))
                closeout_calls = [call for call in _RecordingEvaluator.calls if call[2] is None]
                self.assertEqual(len(closeout_calls), 2)
                frozen_hashes = {
                    row["task_id"]: row["candidate_sha256"] for row in frozen
                }
                for task_id, candidate, deadline, digest in closeout_calls:
                    self.assertIsNone(deadline)
                    self.assertTrue(candidate.is_relative_to(run_dir / "closeout_candidates"))
                    self.assertEqual(digest, frozen_hashes[task_id])
                self.assertEqual(
                    {
                        row["response"]["recorded_phase"]
                        for row in final["verdicts"].values()
                    },
                    {"closeout"},
                )
                self.assertTrue(
                    all(
                        row["status"]
                        not in {"QUEUED", "PENDING", "RUNNING", "IN_PROGRESS", "STARTED"}
                        for row in final["verdicts"].values()
                    )
                )

                events = [
                    json.loads(line)["event"]
                    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                phase_order = [
                    events.index("run_started"),
                    events.index("horizon_closed"),
                    events.index("candidates_frozen"),
                    events.index("closeout_started"),
                    events.index("closeout_finished"),
                    events.index("run_finished"),
                ]
                self.assertEqual(phase_order, sorted(phase_order))

    def test_closeout_verdict_after_solver_phase_can_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch("contextswarm_mini.runner.MockEvaluator", _ProvingEvaluator):
                config = replace(
                    load_config("configs/parallel.toml", ROOT),
                    max_tasks=2,
                    max_parallel=2,
                    time_limit_seconds=1,
                    lean_max_concurrent_evaluations=1,
                )
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final["score"], 2.0)
            self.assertEqual({row["status"] for row in final["verdicts"].values()}, {"PROVED"})

    def test_incomplete_closeout_marks_run_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch("contextswarm_mini.runner.MockEvaluator", _CancelledEvaluator):
                config = replace(
                    load_config("configs/parallel.toml", ROOT),
                    max_tasks=1,
                    max_parallel=1,
                    time_limit_seconds=1,
                )
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final["status"], "DEGRADED")
            self.assertEqual(final["verdicts"]["imo2024_p1"]["status"], "CANCELLED")

    def test_freeze_is_immutable_when_workspace_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(load_config("configs/parallel.toml", ROOT), max_tasks=1)
            task = load_tasks(config)[0]
            source = root / "workers" / task.slug / "result.lean"
            source.parent.mkdir(parents=True)
            original = task.baseline_code
            source.write_text(original, encoding="utf-8")
            frozen = _freeze_closeout_candidates(
                config,
                [task],
                root,
                RunLogger(root),
            )[task.slug]
            source.write_text("changed after freeze\n", encoding="utf-8")

            self.assertEqual(frozen.path.read_text(encoding="utf-8"), original)
            self.assertEqual(
                frozen.sha256,
                hashlib.sha256(original.encode("utf-8")).hexdigest(),
            )

    def test_cps_releases_solver_slot_before_slow_judge_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch("contextswarm_mini.runner.MockEvaluator", _SlowEvaluator):
                config = replace(
                    load_config("configs/smoke.toml", ROOT),
                    max_tasks=1,
                    max_parallel=1,
                    initial_agents_per_task=1,
                    max_attempts_per_task=2,
                    time_limit_seconds=2,
                    lean_max_concurrent_evaluations=1,
                )
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )

            rows = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            second_assignment = next(
                index
                for index, row in enumerate(rows)
                if row.get("event") == "agent_assigned" and row.get("episode") == 2
            )
            first_evaluation = next(
                index for index, row in enumerate(rows) if row.get("event") == "evaluation_finished"
            )
            self.assertLess(second_assignment, first_evaluation)

    def test_attempt_budget_does_not_cancel_already_admitted_agents(self) -> None:
        exhausted = threading.Event()
        original_event = RunLogger.event

        def record_event(logger: RunLogger, event_type: str, **payload: object) -> None:
            original_event(logger, event_type, **payload)
            if event_type == "task_attempt_budget_exhausted":
                exhausted.set()

        def synchronized_mock(agent_id: str, task_id: str, episode: int):
            if episode == 2:
                self.assertTrue(exhausted.wait(timeout=1.0))
            return _mock_result(agent_id, task_id, episode)

        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_config("configs/smoke.toml", ROOT),
                max_tasks=1,
                max_parallel=2,
                initial_agents_per_task=2,
                max_attempts_per_task=2,
                time_limit_seconds=2,
                lean_max_concurrent_evaluations=2,
            )
            with patch.object(RunLogger, "event", record_event), patch(
                "contextswarm_mini.runner._mock_result", synchronized_mock
            ):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )

            solver_evaluations = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if '"event": "evaluation_finished"' in line
            ]
            self.assertEqual(len(solver_evaluations), 2)
            scheduler_state = json.loads(
                (run_dir / "elastic_scheduler_state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(scheduler_state["active_slots"], 0)
            task_state = next(iter(scheduler_state["tasks"].values()))
            self.assertFalse(task_state["solved"])
            self.assertTrue(task_state["retired"])
            self.assertEqual(
                task_state["retired_reason"],
                "attempt_budget_exhausted",
            )

    def test_unlimited_fast_agents_have_bounded_evaluator_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_config("configs/smoke.toml", ROOT),
                max_tasks=1,
                max_parallel=4,
                initial_agents_per_task=4,
                max_attempts_per_task=0,
                time_limit_seconds=0.05,
                lean_max_concurrent_evaluations=1,
            )
            with patch("contextswarm_mini.runner.MockEvaluator", _SlowEvaluator):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )

            assignments = (run_dir / "elastic_assignments.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            backlog_limit = config.max_parallel + config.lean_max_concurrent_evaluations
            self.assertLessEqual(len(assignments), backlog_limit + config.max_parallel)
            events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "evaluation_backpressure_wait"', events)

    def test_late_solver_evaluation_does_not_publish_feedback(self) -> None:
        class LateEvaluator(_SlowEvaluator):
            delay_seconds = 0.3

        with tempfile.TemporaryDirectory() as temporary:
            with patch("contextswarm_mini.runner.MockEvaluator", LateEvaluator):
                config = replace(
                    load_config("configs/smoke.toml", ROOT),
                    max_tasks=1,
                    max_parallel=1,
                    max_attempts_per_task=1,
                    time_limit_seconds=0.2,
                    lean_max_concurrent_evaluations=1,
                )
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )

            store = CPSStore(run_dir / "cps.sqlite3")
            self.assertEqual(store.summary()["pieces"], 0)
            solver_evaluations = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if '"event": "evaluation_finished"' in line
            ]
            self.assertTrue(solver_evaluations)
            self.assertFalse(solver_evaluations[0]["eligible_for_handoff"])

    def test_worker_cps_surface_rejects_writes_after_horizon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "cps.sqlite3"
            env = {
                "CONTEXTSWARM_CPS_DB": str(db),
                "CONTEXTSWARM_TASK_ID": "task",
                "CONTEXTSWARM_ACTOR_ID": "agent",
                "CONTEXTSWARM_HORIZON_EPOCH_MS": "1",
            }
            with patch.dict(os.environ, env, clear=False), patch("builtins.print"):
                result = context_piece_main(
                    ["create", "--title", "late", "--body", "must not persist"]
                )
            self.assertEqual(result, 2)
            self.assertFalse(db.exists())

    def test_cps_write_rechecks_horizon_after_sqlite_lock_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "cps.sqlite3"
            store = CPSStore(db)
            locker = sqlite3.connect(db, timeout=1, isolation_level=None)
            locker.execute("BEGIN IMMEDIATE")
            outcome: list[BaseException | None] = []
            deadline_epoch_ms = int(time.time() * 1_000) + 75

            def delayed_write() -> None:
                try:
                    store.create_piece(
                        task_id="task",
                        author="agent",
                        kind="handoff",
                        title="late",
                        body="must not commit",
                        deadline_epoch_ms=deadline_epoch_ms,
                    )
                except BaseException as exc:  # captured for the test thread
                    outcome.append(exc)
                else:
                    outcome.append(None)

            thread = threading.Thread(target=delayed_write)
            thread.start()
            time.sleep(0.15)
            locker.execute("ROLLBACK")
            locker.close()
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], RuntimeError)
            self.assertEqual(store.summary()["pieces"], 0)
            self.assertEqual(store.summary()["events"], 0)

    def test_paper_manifests_share_task_model_transport_and_evaluator_contract(self) -> None:
        groups = {
            3600: (
                "configs/mono.toml",
                "configs/parallel.toml",
                "configs/cps.toml",
                "configs/1h_mono.toml",
                "configs/1h_parallel.toml",
                "configs/1h_cps.toml",
                "configs/scale_1h_mono.toml",
                "configs/scale_1h_parallel.toml",
                "configs/scale_1h_cps24.toml",
                "configs/scale_1h_cps48.toml",
                "configs/scale_1h_cps96.toml",
            ),
            180: (
                "configs/3min_mono.toml",
                "configs/3min_parallel.toml",
                "configs/3min_cps.toml",
            ),
        }
        expected_ids = json.loads(
            (ROOT / "benchmarks/matholympiadbench/problem_ids.json").read_text(
                encoding="utf-8"
            )
        )
        expected_task_contract: tuple[tuple[str, str, str, str], ...] | None = None
        semantic_evaluator_contracts: set[tuple[object, ...]] = set()
        transport_contracts: set[tuple[object, ...]] = set()
        runtime_endpoint = "http://127.0.0.1:65535/test-runtime-injection"

        with patch.dict(
            os.environ,
            {"CONTEXTSWARM_JUDGE_URL": runtime_endpoint},
            clear=False,
        ):
            for duration, manifests in groups.items():
                for manifest in manifests:
                    with self.subTest(manifest=manifest):
                        config = load_config(manifest, ROOT)
                        self.assertEqual(config.time_limit_seconds, duration)
                        self.assertEqual(config.pi_timeout_seconds, duration)
                        self.assertEqual(config.lean_server_url, runtime_endpoint)
                        self.assertEqual(config.model, "openai-codex/gpt-5.6-sol")
                        self.assertEqual(config.thinking, "max")
                        self.assertFalse(config.fast_mode)
                        tasks = load_tasks(config)
                        self.assertEqual([task.slug for task in tasks], expected_ids)
                        task_contract = tuple(
                            (
                                task.slug,
                                task.problem_id,
                                task.theorem_name,
                                hashlib.sha256(task.baseline_code.encode("utf-8")).hexdigest(),
                            )
                            for task in tasks
                        )
                        if expected_task_contract is None:
                            expected_task_contract = task_contract
                        self.assertEqual(task_contract, expected_task_contract)
                        semantic_evaluator_contracts.add(
                            (
                                config.lean_env_id,
                                config.lean_timeout_seconds,
                                config.lean_max_concurrent_evaluations,
                                config.lean_verification_profile,
                                config.lean_judge_mode,
                            )
                        )
                        transport_contracts.add(
                            (
                                config.pi_http_idle_timeout_ms,
                                config.pi_retry_enabled,
                                config.pi_retry_max_retries,
                                config.pi_retry_base_delay_ms,
                                config.pi_provider_max_retries,
                                config.pi_provider_max_retry_delay_ms,
                            )
                        )

        self.assertEqual(len(semantic_evaluator_contracts), 1)
        self.assertEqual(semantic_evaluator_contracts.pop()[2], 4)
        self.assertEqual(len(transport_contracts), 1)


if __name__ == "__main__":
    unittest.main()
