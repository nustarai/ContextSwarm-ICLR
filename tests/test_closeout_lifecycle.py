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
    _FrozenCandidate,
    RunLogger,
    _freeze_closeout_candidates,
    _mock_result,
    _run_closeout,
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


class _TerminalResourceEvaluator(_SlowEvaluator):
    retryable = False

    def evaluate(
        self,
        task: Task,
        _candidate: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        del deadline_monotonic
        return Verdict(
            task.slug,
            "RESOURCE_LIMIT",
            0.0,
            0.0,
            {
                "error_kind": "memory_limit_exceeded",
                "retryable": self.retryable,
            },
        )


class _RetryableResourceEvaluator(_TerminalResourceEvaluator):
    retryable = True


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


class _SolverProofThenRetryableCloseout:
    calls: list[str] = []
    contract_sha256 = "a" * 64

    def __init__(self, *, prove_without_sorry: bool = False):
        del prove_without_sorry

    def expected_task_contract_sha256(self, _task: Task) -> str:
        return self.contract_sha256

    def evaluate(
        self,
        task: Task,
        candidate: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        phase = "closeout" if deadline_monotonic is None else "solver"
        self.calls.append(phase)
        if phase == "solver":
            return Verdict(
                task.slug,
                "PROVED",
                1.0,
                0.0,
                {"formal_status": "PROVED"},
                candidate_sha256=digest,
                task_contract_sha256=self.contract_sha256,
                judge_job_id="solver-authority",
            )
        return Verdict(
            task.slug,
            "RESOURCE_LIMIT",
            0.0,
            0.0,
            {
                "error_kind": "memory_limit_exceeded",
                "terminal_reason": "verified_without_sorry",
                "retryable": True,
            },
            candidate_sha256=digest,
            task_contract_sha256=self.contract_sha256,
            judge_job_id="closeout-infra",
        )


class _SolverProofThenConflictCloseout(_SolverProofThenRetryableCloseout):
    def evaluate(
        self,
        task: Task,
        candidate: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        if deadline_monotonic is not None:
            return super().evaluate(
                task,
                candidate,
                deadline_monotonic=deadline_monotonic,
            )
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        return Verdict(
            task.slug,
            "VERIFY_FAIL",
            0.0,
            0.0,
            {"error_kind": "verification_failed", "retryable": False},
            candidate_sha256=digest,
            task_contract_sha256=self.contract_sha256,
            judge_job_id="closeout-conflict",
        )


class _FixedCloseoutEvaluator:
    def __init__(self, verdict: Verdict, contract_sha256: str):
        self.verdict = verdict
        self.contract_sha256 = contract_sha256
        self.calls = 0

    def expected_task_contract_sha256(self, _task: Task) -> str:
        return self.contract_sha256

    def evaluate(
        self,
        task: Task,
        candidate: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        self.calls += 1
        self.assert_closeout(deadline_monotonic)
        observed = self.verdict
        status = str(observed.status or "").strip().upper()
        requires_receipt = status in {
            "PROVED",
            "AC",
            "PASS",
            "PASSED",
            "COMPILES_WITH_SORRY",
            "VERIFY_FAIL",
        }
        return Verdict(
            task.slug,
            observed.status,
            observed.score,
            observed.elapsed_seconds,
            dict(observed.response),
            error=observed.error,
            candidate_sha256=(
                observed.candidate_sha256
                or hashlib.sha256(candidate.read_bytes()).hexdigest()
            ),
            task_contract_sha256=(
                observed.task_contract_sha256 or self.contract_sha256
            ),
            judge_job_id=(
                observed.judge_job_id
                or ("closeout-observation" if requires_receipt else None)
            ),
            cache_reused=observed.cache_reused,
        )

    @staticmethod
    def assert_closeout(deadline_monotonic: float | None) -> None:
        if deadline_monotonic is not None:
            raise AssertionError("fixed evaluator is only valid during closeout")


class CloseoutLifecycleTests(unittest.TestCase):
    def _direct_closeout_fixture(
        self,
        root: Path,
        observed: Verdict,
        *,
        contract_sha256: str,
    ) -> tuple[object, Task, dict[str, _FrozenCandidate], RunLogger, _FixedCloseoutEvaluator, str]:
        config = replace(
            load_config("configs/smoke.toml", ROOT),
            max_tasks=1,
            lean_max_concurrent_evaluations=1,
        )
        task = load_tasks(config)[0]
        candidate = root / "closeout_candidates" / task.slug / "result.lean"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(task.baseline_code, encoding="utf-8")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        frozen = {
            task.slug: _FrozenCandidate(task.slug, candidate, digest),
        }
        evaluator = _FixedCloseoutEvaluator(observed, contract_sha256)
        return config, task, frozen, RunLogger(root), evaluator, digest

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

    def test_retryable_closeout_infra_never_reuses_solver_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _SolverProofThenRetryableCloseout.calls = []
            config = replace(
                load_config("configs/smoke.toml", ROOT),
                max_tasks=1,
                max_parallel=1,
                initial_agents_per_task=1,
                max_attempts_per_task=1,
                time_limit_seconds=2,
                lean_max_concurrent_evaluations=1,
            )
            with patch(
                "contextswarm_mini.runner.MockEvaluator",
                _SolverProofThenRetryableCloseout,
            ):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )

            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            verdict = final["verdicts"]["imo2024_p1"]
            # A solver-phase proof is not enough for AC.  The independent
            # outer closeout failed retryably, so the run must not retain its
            # score or job id as final authority.
            self.assertEqual(final["score"], 0.0)
            self.assertEqual(final["status"], "DEGRADED")
            self.assertFalse(final["health"]["ok"])
            self.assertEqual(verdict["status"], "RESOURCE_LIMIT")
            self.assertEqual(verdict["judge_job_id"], "closeout-infra")
            self.assertTrue(
                verdict["response"]["prior_authoritative_proof_available"]
            )
            self.assertFalse(verdict["response"]["fresh_closeout_confirmed"])
            self.assertEqual(
                verdict["response"]["closeout_infra_incomplete"]["observed_status"],
                "RESOURCE_LIMIT",
            )
            self.assertEqual(
                _SolverProofThenRetryableCloseout.calls,
                ["solver", "closeout"],
            )
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                sum(row["event"] == "closeout_infra_incomplete" for row in events),
                1,
            )
            closeout = next(
                row for row in events if row["event"] == "closeout_evaluation_finished"
            )
            self.assertFalse(closeout["reused_authoritative_verdict"])
            self.assertTrue(closeout["closeout_infra_incomplete"])
            self.assertTrue(closeout["prior_authoritative_proof_available"])
            self.assertFalse(closeout["fresh_closeout_confirmed"])
            self.assertFalse(closeout["scoreboard_recorded"])
            scoreboard = [
                json.loads(line)
                for line in (run_dir / "scoreboard_history.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(len(scoreboard), 1)
            self.assertEqual(scoreboard[0]["judge_job_id"], "solver-authority")

    def test_confirmed_closeout_keeps_original_exact_once_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract = "9" * 64
            observed = Verdict(
                "imo2024_p1",
                "PROVED",
                1.0,
                0.0,
                {"formal_status": "PROVED"},
                task_contract_sha256=contract,
                judge_job_id="closeout-confirmation",
            )
            config, task, frozen, logger, evaluator, digest = self._direct_closeout_fixture(
                root,
                observed,
                contract_sha256=contract,
            )
            prior = Verdict(
                task.slug,
                "PROVED",
                1.0,
                0.0,
                {"formal_status": "PROVED"},
                candidate_sha256=digest,
                task_contract_sha256=contract,
                judge_job_id="solver-authority",
            )

            result = _run_closeout(
                config,
                [task],
                frozen,
                logger,
                evaluator,
                threading.BoundedSemaphore(1),
                reusable_verdicts=[prior],
            )

            final = result[task.slug]
            self.assertEqual(evaluator.calls, 1)
            self.assertEqual(final.status, "PROVED")
            self.assertEqual(final.judge_job_id, "solver-authority")
            self.assertEqual(
                final.response["closeout_authority_confirmed"]["observed_status"],
                "PROVED",
            )
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            confirmed = next(
                row for row in events if row["event"] == "closeout_authority_confirmed"
            )
            self.assertEqual(confirmed["prior_judge_job_id"], "solver-authority")
            self.assertEqual(
                confirmed["observed_judge_job_id"],
                "closeout-confirmation",
            )
            closeout = next(
                row for row in events if row["event"] == "closeout_evaluation_finished"
            )
            self.assertTrue(closeout["authoritative_proof_confirmed"])
            self.assertFalse(closeout["scoreboard_recorded"])

    def test_remote_unknown_closeout_is_never_hidden_by_prior_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract = "8" * 64
            observed = Verdict(
                "imo2024_p1",
                "REMOTE_SETTLEMENT_UNCONFIRMED",
                0.0,
                0.0,
                {"remote_settlement_unconfirmed": True},
            )
            config, task, frozen, logger, evaluator, digest = self._direct_closeout_fixture(
                root,
                observed,
                contract_sha256=contract,
            )
            prior = Verdict(
                task.slug,
                "PROVED",
                1.0,
                0.0,
                candidate_sha256=digest,
                task_contract_sha256=contract,
                judge_job_id="solver-authority",
            )

            result = _run_closeout(
                config,
                [task],
                frozen,
                logger,
                evaluator,
                threading.BoundedSemaphore(1),
                reusable_verdicts=[prior],
            )

            final = result[task.slug]
            self.assertEqual(final.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
            self.assertEqual(final.score, 0.0)
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            closeout = next(
                row for row in events if row["event"] == "closeout_evaluation_finished"
            )
            self.assertFalse(closeout["reused_authoritative_verdict"])
            self.assertFalse(closeout["authority_conflict"])
            self.assertFalse(closeout["scoreboard_recorded"])

    def test_closeout_authority_sha_or_contract_mismatch_is_not_reused(self) -> None:
        for mismatch in ("candidate", "contract"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                contract = "b" * 64
                observed = Verdict("imo2024_p1", "VERIFY_FAIL", 0.0, 0.0)
                config, task, frozen, logger, evaluator, digest = self._direct_closeout_fixture(
                    root,
                    observed,
                    contract_sha256=contract,
                )
                prior = Verdict(
                    task.slug,
                    "PROVED",
                    1.0,
                    0.0,
                    candidate_sha256=("c" * 64 if mismatch == "candidate" else digest),
                    task_contract_sha256=(
                        "d" * 64 if mismatch == "contract" else contract
                    ),
                    judge_job_id="prior-authority",
                )
                result = _run_closeout(
                    config,
                    [task],
                    frozen,
                    logger,
                    evaluator,
                    threading.BoundedSemaphore(1),
                    reusable_verdicts=[prior],
                )

                self.assertEqual(result[task.slug].status, "VERIFY_FAIL")
                self.assertEqual(evaluator.calls, 1)
                events = [
                    json.loads(line)
                    for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                mismatch_event = next(
                    row for row in events if row["event"] == "closeout_authority_mismatch"
                )
                match_field = {
                    "candidate": "candidate_sha256_match",
                    "contract": "task_contract_sha256_match",
                }[mismatch]
                self.assertEqual(
                    mismatch_event[match_field],
                    False,
                )
                self.assertFalse(
                    any(row["event"] == "closeout_authority_conflict" for row in events)
                )

    def test_unbound_proved_verdict_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract = "e" * 64
            observed = Verdict("imo2024_p1", "VERIFY_FAIL", 0.0, 0.0)
            config, task, frozen, logger, evaluator, digest = self._direct_closeout_fixture(
                root,
                observed,
                contract_sha256=contract,
            )
            prior = Verdict(
                task.slug,
                "PROVED",
                1.0,
                0.0,
                candidate_sha256=digest,
                task_contract_sha256=contract,
                judge_job_id=None,
            )
            result = _run_closeout(
                config,
                [task],
                frozen,
                logger,
                evaluator,
                threading.BoundedSemaphore(1),
                reusable_verdicts=[prior],
            )

            self.assertEqual(result[task.slug].status, "VERIFY_FAIL")
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertFalse(
                any(row["event"] == "closeout_infra_incomplete" for row in events)
            )
            self.assertFalse(
                any(row["event"] == "closeout_authority_conflict" for row in events)
            )

    def test_nonretryable_exact_authority_contradiction_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract = "f" * 64
            observed = Verdict(
                "imo2024_p1",
                "VERIFY_FAIL",
                0.0,
                0.0,
                {"error_kind": "verification_failed", "retryable": False},
            )
            config, task, frozen, logger, evaluator, digest = self._direct_closeout_fixture(
                root,
                observed,
                contract_sha256=contract,
            )
            prior = Verdict(
                task.slug,
                "PROVED",
                1.0,
                0.0,
                candidate_sha256=digest,
                task_contract_sha256=contract,
                judge_job_id="prior-authority",
            )
            result = _run_closeout(
                config,
                [task],
                frozen,
                logger,
                evaluator,
                threading.BoundedSemaphore(1),
                reusable_verdicts=[prior],
            )

            conflict = result[task.slug]
            self.assertEqual(conflict.status, "AUTHORITY_CONFLICT")
            self.assertEqual(conflict.score, 0.0)
            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            conflict_event = next(
                row for row in events if row["event"] == "closeout_authority_conflict"
            )
            self.assertEqual(conflict_event["observed_status"], "VERIFY_FAIL")
            self.assertFalse(conflict_event["observed_retryable"])
            closeout = next(
                row for row in events if row["event"] == "closeout_evaluation_finished"
            )
            self.assertTrue(closeout["authority_conflict"])
            self.assertTrue(closeout["scoreboard_recorded"])

    def test_authority_conflict_marks_integrated_run_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_config("configs/smoke.toml", ROOT),
                max_tasks=1,
                max_parallel=1,
                initial_agents_per_task=1,
                max_attempts_per_task=1,
                time_limit_seconds=2,
                lean_max_concurrent_evaluations=1,
            )
            with patch(
                "contextswarm_mini.runner.MockEvaluator",
                _SolverProofThenConflictCloseout,
            ):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )

            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            verdict = final["verdicts"]["imo2024_p1"]
            self.assertEqual(final["status"], "DEGRADED")
            self.assertFalse(final["health"]["ok"])
            self.assertIn(
                "closeout_authority_conflict",
                final["health"]["issues"],
            )
            self.assertEqual(verdict["status"], "AUTHORITY_CONFLICT")
            self.assertEqual(verdict["score"], 0.0)

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

    def test_terminal_nonretryable_resource_limit_is_a_complete_zero_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "contextswarm_mini.runner.MockEvaluator",
                _TerminalResourceEvaluator,
            ):
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

        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["score"], 0.0)
        self.assertEqual(
            final["verdicts"]["imo2024_p1"]["status"],
            "RESOURCE_LIMIT",
        )
        self.assertNotIn("closeout_incomplete", final["health"]["issues"])
        self.assertNotIn(
            "evaluator_infrastructure_error",
            final["health"]["issues"],
        )

    def test_retryable_resource_limit_keeps_closeout_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "contextswarm_mini.runner.MockEvaluator",
                _RetryableResourceEvaluator,
            ):
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
        self.assertIn("closeout_incomplete", final["health"]["issues"])
        self.assertIn(
            "evaluator_infrastructure_error",
            final["health"]["issues"],
        )

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
