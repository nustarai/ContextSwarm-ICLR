from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import datetime as dt
import hashlib
import json
import os
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from contextswarm_mini.config import ConfigError, load_config
from contextswarm_mini.allocation import AgentAllocationPolicy
from contextswarm_mini.cps import CPSStore, make_policy
from contextswarm_mini.elastic_scheduler import ElasticScheduler
from contextswarm_mini.evaluator import LeanEvaluator, MockEvaluator, _is_proved, normalize_base_url
from contextswarm_mini.judge_broker import JudgeBroker, JudgeBrokerDrainError
from contextswarm_mini.runner import (
    _atomic_promote_candidate,
    _enforce_verdict_provenance,
    _has_authoritative_provenance,
    _run_health,
    _score_time_metrics,
    _verdict_priority,
    load_tasks,
    run_experiment,
)
import contextswarm_mini.runner as runner_module
from contextswarm_mini.models import Verdict
from contextswarm_mini.pi_agent import (
    PiAgent,
    _event_text,
    _event_trace_fields,
    _redact_sensitive_text,
    _usage_fields,
)
from contextswarm_mini.preflight import PreflightError


ROOT = Path(__file__).resolve().parents[1]
_FORMAL_PROVENANCE_ENV = {
    "CONTEXTSWARM_IMAGE_REVISION": "1" * 40,
    "CONTEXTSWARM_SOURCE_COMMIT": "1" * 40,
    "CONTEXTSWARM_IMAGE_ID": "sha256:" + "3" * 64,
}


def _write_fake_pi(root: Path, source: str) -> Path:
    fake = root / "fake-pi"
    fake.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _fake_pi_config(root: Path, fake: Path, *, timeout: int = 5):
    return replace(
        load_config("configs/smoke.toml", ROOT),
        pi_binary=str(fake),
        aisw_enabled=False,
        pi_timeout_seconds=timeout,
    )


class _ImmediateProofEvaluator:
    """Return a broker proof and independently confirm its frozen closeout."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self.probe_calls = 0
        self.evaluate_calls = 0
        self.closeout_receipts: list[tuple[Path, str, str]] = []
        self.lock = threading.Lock()
        self.probe_barrier: threading.Barrier | None = None

    def expected_task_contract_sha256(self, _task) -> str:
        return "a" * 64

    def probe_source(
        self,
        task,
        candidate_code: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        del deadline_monotonic
        digest = hashlib.sha256(candidate_code.encode("utf-8")).hexdigest()
        with self.lock:
            self.probe_calls += 1
            call_index = self.probe_calls
        if self.probe_barrier is not None:
            self.probe_barrier.wait(timeout=3)
        for candidate in self.output_root.rglob("result.lean"):
            try:
                if candidate.read_text(encoding="utf-8") == candidate_code:
                    candidate.write_text(candidate_code + "-- changed after probe\n", encoding="utf-8")
            except OSError:
                continue
        return Verdict(
            task.slug,
            "PASSED",
            1.0,
            0.01,
            candidate_sha256=digest,
            task_contract_sha256="a" * 64,
            judge_job_id=f"judge-job-{call_index}",
        )

    def evaluate(
        self,
        task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        if deadline_monotonic is not None:
            raise AssertionError("solver evaluation must reuse the broker proof")
        if "closeout_candidates" not in candidate_path.parts:
            raise AssertionError("closeout must evaluate the frozen candidate")
        digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        with self.lock:
            self.evaluate_calls += 1
            call_index = self.evaluate_calls
            job_id = f"closeout-confirmation-{call_index}"
            self.closeout_receipts.append((candidate_path, digest, job_id))
        return Verdict(
            task.slug,
            "PROVED",
            1.0,
            0.02,
            {"formal_status": "PROVED"},
            candidate_sha256=digest,
            task_contract_sha256="a" * 64,
            judge_job_id=job_id,
        )


class _ProbeAndFinalProofEvaluator:
    """Expose a proof on both paths so a test can force their commit race."""

    def __init__(self) -> None:
        self.probe_calls = 0
        self.evaluate_calls = 0
        self.solver_evaluate_calls = 0
        self.closeout_evaluate_calls = 0
        self.closeout_receipts: list[tuple[Path, str, str]] = []
        self.final_started = threading.Event()
        self.allow_final_return = threading.Event()
        self.final_evaluated = threading.Event()
        self.lock = threading.Lock()

    def expected_task_contract_sha256(self, _task) -> str:
        return "b" * 64

    def probe_source(
        self,
        task,
        candidate_code: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        del deadline_monotonic
        with self.lock:
            self.probe_calls += 1
        return Verdict(
            task.slug,
            "PROVED",
            1.0,
            0.01,
            candidate_sha256=hashlib.sha256(candidate_code.encode("utf-8")).hexdigest(),
            task_contract_sha256="b" * 64,
            judge_job_id="probe-proof",
        )

    def evaluate(
        self,
        task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        if deadline_monotonic is None:
            if "closeout_candidates" not in candidate_path.parts:
                raise AssertionError("closeout must evaluate the frozen candidate")
            with self.lock:
                self.evaluate_calls += 1
                self.closeout_evaluate_calls += 1
                job_id = f"closeout-confirmation-{self.closeout_evaluate_calls}"
                self.closeout_receipts.append((candidate_path, digest, job_id))
            return Verdict(
                task.slug,
                "PROVED",
                1.0,
                0.02,
                {"formal_status": "PROVED"},
                candidate_sha256=digest,
                task_contract_sha256="b" * 64,
                judge_job_id=job_id,
            )
        with self.lock:
            self.evaluate_calls += 1
            self.solver_evaluate_calls += 1
        self.final_started.set()
        if not self.allow_final_return.wait(timeout=3):
            raise AssertionError("early callback did not enter the shared commit barrier")
        self.final_evaluated.set()
        return Verdict(
            task.slug,
            "PROVED",
            1.0,
            0.02,
            candidate_sha256=digest,
            task_contract_sha256="b" * 64,
            judge_job_id="final-proof",
        )


class MiniRuntimeTests(unittest.TestCase):
    def test_run_health_degrades_on_stable_judge_control_failures_only(self) -> None:
        failure_statuses = (
            "BROKER_ERROR",
            "JUDGE_ADMISSION_ERROR",
            "JUDGE_ADMISSION_TIMEOUT",
            "CANDIDATE_SNAPSHOT_ERROR",
            "SESSION_PROBE_BUDGET_EXHAUSTED",
            "INVALID_REQUEST",
            "INVALID_TASK_SELECTION",
        )
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "events.jsonl").write_text("", encoding="utf-8")
            config = load_config("configs/parallel.toml", ROOT)
            verdict = Verdict("task", "VERIFY_FAIL", 0.0, 0.0)
            for status in (*failure_statuses, "OUT_OF_HORIZON"):
                with self.subTest(status=status):
                    (run_dir / "judge_checks.jsonl").write_text(
                        json.dumps({"status": status}) + "\n", encoding="utf-8"
                    )
                    health = _run_health(
                        run_dir,
                        config,
                        {"task": verdict},
                        [],
                        [verdict],
                        expected_task_count=1,
                    )
                    if status == "OUT_OF_HORIZON":
                        self.assertTrue(health["ok"], health)
                        self.assertEqual(health["judge_probe_infrastructure_error_count"], 0)
                    else:
                        self.assertFalse(health["ok"], health)
                        self.assertIn("judge_probe_infrastructure_error", health["issues"])
                        self.assertEqual(health["judge_probe_infrastructure_error_count"], 1)

    def test_dataset_and_protocol_manifests(self) -> None:
        tasks = load_tasks(load_config("configs/cps.toml", ROOT))
        self.assertEqual(len(tasks), 12)
        self.assertEqual(tasks[0].slug, "imo2024_p1")
        self.assertIn("theorem", tasks[0].baseline_code)
        mono = load_config("configs/mono.toml", ROOT)
        parallel = load_config("configs/parallel.toml", ROOT)
        self.assertEqual((mono.mode, mono.communication, mono.max_parallel), ("mono", "none", 1))
        self.assertEqual((parallel.mode, parallel.communication), ("parallel", "none"))
        transport_fields = (
            "pi_http_idle_timeout_ms",
            "pi_retry_enabled",
            "pi_retry_max_retries",
            "pi_retry_base_delay_ms",
            "pi_provider_max_retries",
            "pi_provider_max_retry_delay_ms",
        )
        expected = tuple(getattr(mono, field) for field in transport_fields)
        evaluator_fields = (
            "lean_timeout_seconds",
            "lean_max_lifecycle_seconds",
            "lean_max_concurrent_evaluations",
            "lean_verification_profile",
            "lean_judge_mode",
        )
        expected_evaluator = tuple(
            getattr(mono, field) for field in evaluator_fields
        )
        self.assertEqual(mono.lean_max_lifecycle_seconds, 3_600)
        for manifest in (
            "configs/parallel.toml",
            "configs/cps.toml",
            "configs/scale_1h_cps24.toml",
            "configs/scale_1h_cps48.toml",
            "configs/scale_1h_cps96.toml",
        ):
            config = load_config(manifest, ROOT)
            self.assertEqual(tuple(getattr(config, field) for field in transport_fields), expected)
            self.assertEqual(
                tuple(getattr(config, field) for field in evaluator_fields),
                expected_evaluator,
            )

        allocation_arms = [
            load_config(f"configs/allocation_1h_cps48_{name}.toml", ROOT)
            for name in ("uniform", "formula", "agent")
        ]
        contracts = []
        for config in allocation_arms:
            public = config.public_dict()
            public.pop("name")
            allocation = dict(public.pop("allocation"))
            allocation.pop("policy")
            contracts.append((public, allocation))
        self.assertEqual(contracts, [contracts[0]] * 3)
        self.assertEqual([config.allocation.policy for config in allocation_arms], ["uniform", "formula", "agent"])

    def test_cps_store_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CPSStore(Path(temporary) / "cps.sqlite3")
            policy = make_policy("hybrid", store)
            policy.publish("task", "agent-a", title="route", body="try induction", kind="proof_strategy")
            policy.send("task", "agent-a", "check the induction boundary", recipient="agent-b")
            store.create_piece(
                task_id="__global__",
                author="agent-a",
                kind="strategy",
                title="global hint",
                body="reuse a finite induction lemma",
            )
            digest = policy.digest("task", "agent-b", "induction")
            self.assertIn("try induction", digest)
            self.assertIn("induction boundary", digest)
            self.assertIn("global hint", digest)
            self.assertEqual(store.summary()["pieces"], 2)
            self.assertEqual(store.summary()["messages"], 1)

    def test_mock_run_writes_final_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {
                    "CONTEXTSWARM_IMAGE_REVISION": "",
                    "CONTEXTSWARM_SOURCE_COMMIT": "",
                    "CONTEXTSWARM_IMAGE_ID": "",
                },
            ):
                run_dir = run_experiment(
                    load_config("configs/smoke.toml", ROOT),
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final["schema_version"], "contextswarm_mini_run_v1")
            self.assertEqual(final["mode"], "cps")
            self.assertTrue(final["health"]["ok"], final["health"])
            self.assertEqual(
                final["health"]["assigned_count"],
                final["health"]["evaluated_count"],
            )
            meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(
                meta["runtime_provenance"],
                {
                    "source_commit": "test-only-mock-source",
                    "image_id": "test-only-mock-image",
                    "test_only": True,
                },
            )
            closeout = json.loads(
                (run_dir / "judge_broker_closeout.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                closeout,
                {
                    "schema_version": "contextswarm_judge_broker_closeout_v1",
                    "drained": True,
                    "active_handlers": 0,
                    "fifo_depth": 0,
                    "remote_unsettled_jobs": 0,
                },
            )
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            event_names = [row["event"] for row in events]
            self.assertLess(
                event_names.index("judge_broker_closed"),
                event_names.index("run_finished"),
            )
            self.assertTrue((run_dir / "cps.sqlite3").exists())
            self.assertTrue((run_dir / "scoreboard_history.jsonl").exists())

    def test_formal_run_rejects_missing_or_invalid_runtime_provenance_before_start(self) -> None:
        config = replace(
            load_config("configs/parallel.toml", ROOT),
            lean_server_url="http://127.0.0.1:1",
            max_tasks=1,
        )
        invalid_env = {
            "CONTEXTSWARM_IMAGE_REVISION": "not-a-commit",
            "CONTEXTSWARM_SOURCE_COMMIT": "",
            "CONTEXTSWARM_IMAGE_ID": "sha256:not-an-image",
        }
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            with patch.dict(os.environ, invalid_env), self.assertRaisesRegex(
                ConfigError, "immutable image revision and image ID"
            ):
                run_experiment(config, output_override=output_root)
            self.assertEqual(list(output_root.iterdir()), [])

            mismatched_env = {
                "CONTEXTSWARM_IMAGE_REVISION": "1" * 40,
                "CONTEXTSWARM_SOURCE_COMMIT": "2" * 40,
                "CONTEXTSWARM_IMAGE_ID": "sha256:" + "3" * 64,
            }
            with patch.dict(os.environ, mismatched_env), self.assertRaisesRegex(
                ConfigError, "matching the baked source commit"
            ):
                run_experiment(config, output_override=output_root)
            self.assertEqual(list(output_root.iterdir()), [])

    def test_broker_drain_timeout_is_fatal_before_health_and_final(self) -> None:
        original_close = JudgeBroker.close

        def close_then_report_timeout(broker, *, timeout_seconds=None):
            original_close(broker, timeout_seconds=timeout_seconds)
            raise JudgeBrokerDrainError(
                {
                    "drained": False,
                    "active_handlers": 1,
                    "fifo_depth": 2,
                    "remote_unsettled_jobs": 3,
                }
            )

        health_observations: list[tuple[bool, list[str]]] = []
        original_health = runner_module._run_health

        def observe_health(run_dir, *args, **kwargs):
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            health_observations.append(
                (
                    (run_dir / "judge_broker_closeout.json").exists(),
                    [row["event"] for row in events],
                )
            )
            return original_health(run_dir, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            with patch.object(JudgeBroker, "close", close_then_report_timeout), patch.object(
                runner_module, "_run_health", side_effect=observe_health
            ), self.assertRaises(JudgeBrokerDrainError):
                run_experiment(
                    replace(
                        load_config("configs/smoke.toml", ROOT),
                        max_tasks=1,
                        max_parallel=1,
                        initial_agents_per_task=1,
                        max_attempts_per_task=1,
                    ),
                    mock_agent=True,
                    output_override=output_root,
                )
            run_dir = next(output_root.iterdir())
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            event_names = [row["event"] for row in events]
            closeout = json.loads(
                (run_dir / "judge_broker_closeout.json").read_text(encoding="utf-8")
            )

        self.assertEqual(final["status"], "ERROR")
        self.assertFalse(final["health"]["ok"])
        self.assertFalse(closeout["drained"])
        self.assertEqual(closeout["active_handlers"], 1)
        self.assertEqual(closeout["fifo_depth"], 2)
        self.assertEqual(closeout["remote_unsettled_jobs"], 3)
        self.assertNotIn("judge_broker_closed", event_names)
        self.assertLess(
            event_names.index("broker_drain_timeout"),
            event_names.index("run_error"),
        )
        self.assertEqual(len(health_observations), 1)
        closeout_existed, events_before_health = health_observations[0]
        self.assertTrue(closeout_existed)
        self.assertIn("broker_drain_timeout", events_before_health)
        self.assertIn("run_error", events_before_health)

    def test_malformed_private_judge_url_never_reaches_failure_artifacts(self) -> None:
        private_marker = "operator-private-preflight-marker"
        config = replace(
            load_config("configs/smoke.toml", ROOT),
            aisw_enabled=False,
            pi_binary=sys.executable,
            lean_server_url=f"http://judge.invalid/{private_marker}\nfragment",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            with patch.dict(os.environ, _FORMAL_PROVENANCE_ENV), self.assertRaises(
                PreflightError
            ) as raised:
                run_experiment(config, output_override=output_root)
            run_dirs = list(output_root.iterdir())
            self.assertEqual(len(run_dirs), 1)
            run_dir = run_dirs[0]
            rendered = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (run_dir / "run_meta.json", run_dir / "events.jsonl", run_dir / "final.json")
            )
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))

        self.assertNotIn(private_marker, str(raised.exception))
        self.assertNotIn(private_marker, rendered)
        failure = next(row for row in events if row["event"] == "preflight_failed")
        self.assertIn("invalid_request_configuration", failure["error"])
        self.assertEqual(final["status"], "PREFLIGHT_FAILED")

    def test_runner_and_elastic_worker_exceptions_are_redacted_in_artifacts(self) -> None:
        private_marker = "operator-private-runner-marker"
        private_error = RuntimeError(
            f"failed via https://judge.invalid/{private_marker} "
            f"token={private_marker}-token /host/private/{private_marker}.txt"
        )
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            config = replace(
                load_config("configs/smoke.toml", ROOT),
                max_tasks=1,
                max_parallel=1,
                max_attempts_per_task=1,
                time_limit_seconds=1,
            )
            with patch.object(
                runner_module,
                "_run_elastic_cps",
                side_effect=private_error,
            ), self.assertRaises(RuntimeError):
                run_experiment(
                    config,
                    mock_agent=True,
                    output_override=output_root / "run-error",
                )
            run_error_dir = next((output_root / "run-error").iterdir())

            with patch.object(
                runner_module,
                "build_task_prompt",
                side_effect=private_error,
            ), self.assertRaisesRegex(RuntimeError, "runner worker/admission failure"):
                run_experiment(
                    config,
                    mock_agent=True,
                    output_override=output_root / "elastic-error",
                )
            elastic_dir = next((output_root / "elastic-error").iterdir())

            for run_dir, event_name in (
                (run_error_dir, "run_error"),
                (elastic_dir, "elastic_worker_error"),
            ):
                rendered = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in (run_dir / "events.jsonl", run_dir / "final.json")
                )
                events = [
                    json.loads(line)
                    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                failure = next(row for row in events if row["event"] == event_name)
                self.assertNotIn(private_marker, rendered)
                self.assertIn("<redacted-", failure["error"])
                self.assertIn("<redacted-", failure["traceback"])

            elastic_events = [
                json.loads(line)
                for line in (elastic_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            elastic_event_names = [row["event"] for row in elastic_events]
            elastic_final = json.loads(
                (elastic_dir / "final.json").read_text(encoding="utf-8")
            )
            assignments = [
                json.loads(line)
                for line in (elastic_dir / "elastic_assignments.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(assignments), 1)
            self.assertEqual(elastic_final["status"], "ERROR")
            self.assertLess(
                elastic_event_names.index("judge_broker_closed"),
                elastic_event_names.index("run_error"),
            )

    def test_elastic_cps_reuses_slots_and_keeps_task_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_config("configs/smoke.toml", ROOT),
                max_tasks=3,
                max_parallel=2,
                initial_agents_per_task=1,
                max_attempts_per_task=2,
                time_limit_seconds=2,
            )
            run_dir = run_experiment(
                config,
                mock_agent=True,
                output_override=Path(temporary),
            )
            assignments = [
                json.loads(line)
                for line in (run_dir / "elastic_assignments.jsonl").read_text().splitlines()
            ]
            self.assertGreaterEqual(len(assignments), 3)
            self.assertEqual(
                set(json.loads((run_dir / "final.json").read_text())["verdicts"]),
                {"imo2024_p1", "imo2024_p2", "imo2024_p3"},
            )
            self.assertTrue((run_dir / "elastic_scheduler_state.json").exists())

    def test_three_allocation_policies_share_initial_pool_and_log_decisions(self) -> None:
        initial_orders: list[list[str]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for policy_name in ("uniform", "formula", "agent"):
                base = load_config("configs/smoke.toml", ROOT)
                config = replace(
                    base,
                    allocation=replace(base.allocation, policy=policy_name),
                    max_tasks=2,
                    max_parallel=2,
                    initial_agents_per_task=1,
                    max_attempts_per_task=3,
                    time_limit_seconds=2,
                )
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=root / policy_name,
                )
                assignments = [
                    json.loads(line)
                    for line in (run_dir / "elastic_assignments.jsonl").read_text().splitlines()
                ]
                initial = [row for row in assignments if row["allocation_phase"] == "initial"]
                initial_orders.append([row["task_id"] for row in initial])
                decisions = [
                    json.loads(line)
                    for line in (run_dir / "allocation_decisions.jsonl").read_text().splitlines()
                ]
                self.assertGreaterEqual(len(decisions), 1)
                self.assertTrue(all(row["policy"] == policy_name for row in decisions))
                summary = json.loads((run_dir / "allocation_summary.json").read_text())
                self.assertEqual(summary["policy"], policy_name)
                final = json.loads((run_dir / "final.json").read_text())
                self.assertEqual(final["allocation"]["policy"], policy_name)
                self.assertIn("normalized_score_time_auc", final["score_time"])
            self.assertEqual(initial_orders, [initial_orders[0]] * 3)

    def test_agent_scheduler_calls_run_concurrently_for_released_slots(self) -> None:
        active = 0
        max_active = 0
        choose_count = 0
        counter_lock = threading.Lock()
        first_pair_barrier = threading.Barrier(2)
        original_choose = AgentAllocationPolicy.choose

        def observed_choose(policy, snapshot):
            nonlocal active, max_active, choose_count
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
                choose_count += 1
                synchronize = choose_count <= 2
            try:
                if synchronize:
                    first_pair_barrier.wait(timeout=1)
                return original_choose(policy, snapshot)
            finally:
                with counter_lock:
                    active -= 1

        with tempfile.TemporaryDirectory() as temporary:
            base = load_config("configs/smoke.toml", ROOT)
            config = replace(
                base,
                allocation=replace(base.allocation, policy="agent"),
                max_tasks=4,
                max_parallel=4,
                initial_agents_per_task=1,
                max_attempts_per_task=3,
                time_limit_seconds=2,
            )
            with patch.object(AgentAllocationPolicy, "choose", observed_choose):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            summary = json.loads((run_dir / "allocation_summary.json").read_text())
            assignments = [
                json.loads(line)
                for line in (run_dir / "elastic_assignments.jsonl").read_text().splitlines()
            ]
            decisions = [
                json.loads(line)
                for line in (run_dir / "allocation_decisions.jsonl").read_text().splitlines()
            ]
            final = json.loads((run_dir / "final.json").read_text())
            self.assertGreaterEqual(max_active, 2)
            self.assertGreaterEqual(summary["decisions"], 2)
            self.assertEqual(summary["fallback_decisions"], 0)
            self.assertTrue(
                any(row["disposition"] == "not_admitted_stale" for row in decisions)
            )
            self.assertTrue(
                all(
                    "execution_snapshot" in row
                    for row in decisions
                    if row["disposition"] == "assigned"
                )
            )
            self.assertTrue(
                all(
                    row["decision_index"]
                    == row["snapshot"]["decision_index"]
                    == row["execution_snapshot"]["decision_index"]
                    for row in decisions
                    if "execution_snapshot" in row
                )
            )
            self.assertTrue(final["health"]["ok"], final["health"])
            self.assertNotIn("allocation_scheduler_fallback", final["health"]["issues"])
            self.assertTrue(
                all(
                    sum(row["task_id"] == task_id for row in assignments) <= 3
                    for task_id in {row["task_id"] for row in assignments}
                )
            )

    def test_deterministic_policy_last_task_solve_race_is_stale_not_invalid(self) -> None:
        original_next_for = ElasticScheduler.next_assignment_for

        for policy_name in ("uniform", "formula"):
            injected = False

            def solve_before_admission(scheduler, task_id, *, now=None):
                nonlocal injected
                if not injected:
                    injected = True
                    scheduler.task_solved(task_id)
                    return None
                return original_next_for(scheduler, task_id, now=now)

            with tempfile.TemporaryDirectory() as temporary:
                base = load_config("configs/smoke.toml", ROOT)
                config = replace(
                    base,
                    allocation=replace(base.allocation, policy=policy_name),
                    max_tasks=1,
                    max_parallel=1,
                    initial_agents_per_task=1,
                    max_attempts_per_task=2,
                    time_limit_seconds=1,
                )
                with patch.object(
                    ElasticScheduler,
                    "next_assignment_for",
                    solve_before_admission,
                ):
                    run_dir = run_experiment(
                        config,
                        mock_agent=True,
                        output_override=Path(temporary),
                    )
                final = json.loads((run_dir / "final.json").read_text())
                decisions = [
                    json.loads(line)
                    for line in (run_dir / "allocation_decisions.jsonl").read_text().splitlines()
                ]
                self.assertTrue(final["health"]["ok"], (policy_name, final["health"]))
                self.assertEqual(len(decisions), 1)
                self.assertEqual(decisions[0]["disposition"], "not_admitted_stale")
                self.assertFalse(decisions[0]["fallback"])

    def test_agent_scheduler_failure_is_visible_and_degrades_run(self) -> None:
        original_mock_result = runner_module._mock_result

        def failed_scheduler_result(agent_id: str, task_id: str, episode: int):
            result = original_mock_result(agent_id, task_id, episode)
            if task_id == "__allocation__":
                result.returncode = 9
                result.timed_out = True
                result.cancelled = True
                result.error_tail = "scheduler failed"
            return result

        with tempfile.TemporaryDirectory() as temporary:
            base = load_config("configs/smoke.toml", ROOT)
            config = replace(
                base,
                allocation=replace(base.allocation, policy="agent"),
                max_tasks=2,
                max_parallel=2,
                initial_agents_per_task=1,
                max_attempts_per_task=2,
                time_limit_seconds=2,
            )
            with patch.object(runner_module, "_mock_result", failed_scheduler_result):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )

            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            health = final["health"]
            self.assertEqual(final["status"], "DEGRADED")
            self.assertFalse(health["ok"])
            self.assertGreater(health["allocation_scheduler_result_count"], 0)
            self.assertEqual(
                health["allocation_scheduler_result_count"],
                health["allocation_scheduler_finished_event_count"],
            )
            self.assertGreater(health["allocation_scheduler_nonzero_return_count"], 0)
            self.assertGreater(health["allocation_scheduler_timeout_count"], 0)
            self.assertGreater(health["allocation_scheduler_policy_timeout_count"], 0)
            self.assertEqual(health["allocation_scheduler_horizon_truncation_count"], 0)
            self.assertGreater(health["allocation_scheduler_cancelled_count"], 0)
            self.assertGreater(health["allocation_scheduler_invalid_output_count"], 0)
            self.assertGreater(health["allocation_scheduler_fallback_count"], 0)
            self.assertIn("allocation_scheduler_process_error", health["issues"])
            self.assertIn("allocation_scheduler_timeout", health["issues"])
            self.assertIn("allocation_scheduler_cancelled", health["issues"])
            self.assertIn("allocation_scheduler_invalid_output", health["issues"])
            self.assertIn("allocation_scheduler_fallback", health["issues"])
            self.assertTrue(all(row["task_id"] != "__allocation__" for row in final["agents"]))
            self.assertEqual(
                len(final["allocation_scheduler_agents"]),
                health["allocation_scheduler_result_count"],
            )
            self.assertTrue(
                all(row["task_id"] == "__allocation__" for row in final["allocation_scheduler_agents"])
            )
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            scheduler_events = [
                row for row in events if row["event"] == "allocation_scheduler_finished"
            ]
            self.assertTrue(scheduler_events)
            self.assertTrue(all(row["returncode"] == 9 for row in scheduler_events))

    def test_agent_scheduler_natural_horizon_timeout_is_audited_without_degradation(self) -> None:
        original_mock_result = runner_module._mock_result

        def horizon_scheduler_result(agent_id: str, task_id: str, episode: int):
            result = original_mock_result(agent_id, task_id, episode)
            if task_id == "__allocation__":
                result.returncode = 124
                result.timed_out = True
                result.run_horizon_reached = True
                result.error_tail = "overall run horizon elapsed"
            return result

        with tempfile.TemporaryDirectory() as temporary:
            base = load_config("configs/smoke.toml", ROOT)
            config = replace(
                base,
                allocation=replace(base.allocation, policy="agent"),
                max_tasks=1,
                max_parallel=1,
                initial_agents_per_task=1,
                max_attempts_per_task=2,
                time_limit_seconds=0.1,
            )
            with patch.object(runner_module, "_mock_result", horizon_scheduler_result):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            final = json.loads((run_dir / "final.json").read_text())
            health = final["health"]
            decisions = [
                json.loads(line)
                for line in (run_dir / "allocation_decisions.jsonl").read_text().splitlines()
            ]
            self.assertTrue(health["ok"], health)
            self.assertEqual(health["allocation_scheduler_horizon_truncation_count"], 1)
            self.assertEqual(health["allocation_scheduler_policy_timeout_count"], 0)
            self.assertEqual(health["allocation_scheduler_invalid_output_count"], 0)
            self.assertEqual(health["allocation_scheduler_fallback_count"], 0)
            self.assertEqual(decisions[0]["disposition"], "not_admitted_horizon")
            self.assertIsNone(decisions[0]["agent_result_valid"])
            self.assertTrue(decisions[0]["agent_run_horizon_reached"])
            scheduler_result = final["allocation_scheduler_agents"][0]
            self.assertEqual(scheduler_result["decision_index"], decisions[0]["decision_index"])
            self.assertEqual(
                health["allocation_scheduler_summary_agent_calls"],
                health["allocation_scheduler_result_count"],
            )

    def test_scheduler_decision_index_join_mismatch_degrades_run(self) -> None:
        original_choose = AgentAllocationPolicy.choose

        def corrupt_join(policy, snapshot):
            decision = original_choose(policy, snapshot)
            if decision.agent_episode is not None:
                decision.agent_episode += 1000
            return decision

        with tempfile.TemporaryDirectory() as temporary:
            base = load_config("configs/smoke.toml", ROOT)
            config = replace(
                base,
                allocation=replace(base.allocation, policy="agent"),
                max_tasks=1,
                max_parallel=1,
                initial_agents_per_task=1,
                max_attempts_per_task=2,
                time_limit_seconds=1,
            )
            with patch.object(AgentAllocationPolicy, "choose", corrupt_join):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            final = json.loads((run_dir / "final.json").read_text())
            self.assertEqual(final["status"], "DEGRADED")
            self.assertIn(
                "allocation_scheduler_closeout_mismatch",
                final["health"]["issues"],
            )

    def test_scheduler_duplicate_decision_index_degrades_exact_join(self) -> None:
        original_choose = AgentAllocationPolicy.choose

        def duplicate_index(policy, snapshot):
            decision = original_choose(policy, snapshot)
            decision.decision_index = 1
            return decision

        with tempfile.TemporaryDirectory() as temporary:
            base = load_config("configs/smoke.toml", ROOT)
            config = replace(
                base,
                allocation=replace(base.allocation, policy="agent"),
                max_tasks=1,
                max_parallel=1,
                initial_agents_per_task=1,
                max_attempts_per_task=3,
                time_limit_seconds=1,
            )
            with patch.object(AgentAllocationPolicy, "choose", duplicate_index):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            final = json.loads((run_dir / "final.json").read_text())
            decisions = [
                json.loads(line)
                for line in (run_dir / "allocation_decisions.jsonl").read_text().splitlines()
            ]
            self.assertGreaterEqual(len(decisions), 2)
            self.assertLess(len({row["decision_index"] for row in decisions}), len(decisions))
            self.assertIn(
                "allocation_scheduler_closeout_mismatch",
                final["health"]["issues"],
            )

    def test_scheduler_summary_agent_calls_mismatch_degrades_closeout(self) -> None:
        original_summary = AgentAllocationPolicy.summary

        def incorrect_agent_calls(policy):
            summary = original_summary(policy)
            summary["agent_calls"] += 1
            return summary

        with tempfile.TemporaryDirectory() as temporary:
            base = load_config("configs/smoke.toml", ROOT)
            config = replace(
                base,
                allocation=replace(base.allocation, policy="agent"),
                max_tasks=1,
                max_parallel=1,
                initial_agents_per_task=1,
                max_attempts_per_task=2,
                time_limit_seconds=1,
            )
            with patch.object(AgentAllocationPolicy, "summary", incorrect_agent_calls):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            final = json.loads((run_dir / "final.json").read_text())
            health = final["health"]
            self.assertNotEqual(
                health["allocation_scheduler_summary_agent_calls"],
                health["allocation_scheduler_result_count"],
            )
            self.assertIn("allocation_scheduler_closeout_mismatch", health["issues"])

    def test_agent_scheduler_horizon_non_admission_is_not_a_fallback(self) -> None:
        original_choose = AgentAllocationPolicy.choose

        def delayed_choose(policy, snapshot):
            decision = original_choose(policy, snapshot)
            time.sleep(0.1)
            return decision

        with tempfile.TemporaryDirectory() as temporary:
            base = load_config("configs/smoke.toml", ROOT)
            config = replace(
                base,
                allocation=replace(base.allocation, policy="agent"),
                max_tasks=1,
                max_parallel=1,
                initial_agents_per_task=1,
                max_attempts_per_task=2,
                time_limit_seconds=0.05,
            )
            with patch.object(AgentAllocationPolicy, "choose", delayed_choose):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            decisions = [
                json.loads(line)
                for line in (run_dir / "allocation_decisions.jsonl").read_text().splitlines()
            ]
            self.assertTrue(decisions)
            self.assertEqual(decisions[-1]["disposition"], "not_admitted_horizon")
            self.assertFalse(decisions[-1]["fallback"])
            self.assertEqual(final["health"]["allocation_scheduler_fallback_count"], 0)
            self.assertNotIn("allocation_scheduler_fallback", final["health"]["issues"])

    def test_pass_best_priority_cannot_be_replaced_by_verify_fail(self) -> None:
        best = Verdict("p", " pass ", 1.0, 0.0)
        rejected = Verdict("p", "verify_fail", 0.0, 0.0)
        self.assertGreater(_verdict_priority(best), _verdict_priority(rejected))
        if _verdict_priority(rejected) >= _verdict_priority(best):
            best = rejected
        self.assertEqual(best.status, " pass ")

    def test_score_time_auc_records_verified_proof_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "run_meta.json").write_text(
                json.dumps({"started_at": "2026-08-21T00:00:00+00:00"})
            )
            (run_dir / "scoreboard_history.jsonl").write_text(
                json.dumps(
                    {
                        "at": "2026-08-21T00:00:01+00:00",
                        "task_id": "p1",
                        "score": 1.0,
                    }
                )
                + "\n"
            )
            score_time = _score_time_metrics(run_dir, horizon_seconds=10, max_score=2)
            self.assertEqual(score_time["time_to_first_proof_seconds"], 1.0)
            self.assertEqual(score_time["normalized_score_time_auc"], 0.45)

    def test_score_time_auc_prefers_monotonic_horizon_offset_over_wall_clock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "run_meta.json").write_text(
                json.dumps(
                    {
                        "started_at": "2026-08-21T00:00:00+00:00",
                        "horizon_started_at": "2026-08-21T00:10:00+00:00",
                    }
                )
            )
            (run_dir / "scoreboard_history.jsonl").write_text(
                json.dumps(
                    {
                        "at": "2099-01-01T00:00:00+00:00",
                        "horizon_elapsed_seconds": 2.0,
                        "task_id": "p1",
                        "score": 1.0,
                    }
                )
                + "\n"
            )
            score_time = _score_time_metrics(run_dir, horizon_seconds=10, max_score=2)
            self.assertEqual(score_time["time_to_first_proof_seconds"], 2.0)
            self.assertEqual(score_time["normalized_score_time_auc"], 0.4)

    def test_preflight_time_is_outside_the_run_horizon(self) -> None:
        remaining_at_dispatch: list[float] = []

        def delayed_preflight(_config, _run_dir):
            time.sleep(0.08)

        def capture_workers(*_args, deadline: float, **_kwargs):
            remaining_at_dispatch.append(deadline - time.monotonic())
            return []

        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                load_config("configs/parallel.toml", ROOT),
                lean_server_url="http://127.0.0.1:1",
                max_tasks=1,
                time_limit_seconds=0.3,
            )
            with patch.dict(os.environ, _FORMAL_PROVENANCE_ENV), patch.object(
                runner_module, "run_preflight", delayed_preflight
            ), patch.object(runner_module, "_run_task_workers", capture_workers):
                run_dir = run_experiment(
                    config,
                    mock_agent=False,
                    output_override=Path(temporary),
                )
            meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
            artifact_started = dt.datetime.fromisoformat(meta["started_at"])
            horizon_started = dt.datetime.fromisoformat(meta["horizon_started_at"])
            self.assertGreaterEqual((horizon_started - artifact_started).total_seconds(), 0.06)
            self.assertGreater(remaining_at_dispatch[0], 0.24)
            self.assertEqual(
                meta["runtime_provenance"],
                {
                    "source_commit": _FORMAL_PROVENANCE_ENV[
                        "CONTEXTSWARM_IMAGE_REVISION"
                    ],
                    "image_id": _FORMAL_PROVENANCE_ENV["CONTEXTSWARM_IMAGE_ID"],
                },
            )

    def test_best_promotion_is_hash_bound_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            best = root / "best" / "result.lean"
            source = "import Mathlib\ntheorem task : True := by trivial\n"
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            candidate.write_text(source, encoding="utf-8")
            verdict = Verdict(
                "task",
                "VERIFY_FAIL",
                0.0,
                0.1,
                candidate_sha256=digest,
                task_contract_sha256="a" * 64,
                judge_job_id="job-1",
            )
            self.assertTrue(
                _has_authoritative_provenance(
                    verdict,
                    candidate,
                    expected_task_contract_sha256="a" * 64,
                    allow_mock_provenance=False,
                )
            )
            self.assertEqual(_atomic_promote_candidate(candidate, best, digest), digest)
            self.assertEqual(best.read_text(encoding="utf-8"), source)

            candidate.write_text(source + "-- changed\n", encoding="utf-8")
            self.assertFalse(
                _has_authoritative_provenance(
                    verdict,
                    candidate,
                    expected_task_contract_sha256="a" * 64,
                    allow_mock_provenance=False,
                )
            )
            with self.assertRaises(ValueError):
                _atomic_promote_candidate(candidate, best, digest)
            self.assertEqual(best.read_text(encoding="utf-8"), source)

    def test_scored_or_authoritative_verdict_requires_exact_candidate_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "result.lean"
            candidate.write_text("theorem t : True := by trivial\n", encoding="utf-8")
            expected_contract = "2" * 64
            mismatched = Verdict(
                "task",
                "PROVED",
                1.0,
                0.1,
                {"mock": True},
                candidate_sha256="0" * 64,
                task_contract_sha256="1" * 64,
            )
            rejected = _enforce_verdict_provenance(
                mismatched,
                candidate,
                expected_task_contract_sha256=expected_contract,
                allow_mock_provenance=True,
            )
            self.assertEqual(rejected.status, "PROVENANCE_INVALID")
            self.assertEqual(rejected.score, 0.0)
            authoritative_failure = Verdict(
                "task",
                "VERIFY_FAIL",
                0.0,
                0.1,
                {"mock": True},
                candidate_sha256="0" * 64,
                task_contract_sha256="1" * 64,
            )
            self.assertEqual(
                _enforce_verdict_provenance(
                    authoritative_failure,
                    candidate,
                    expected_task_contract_sha256=expected_contract,
                    allow_mock_provenance=True,
                ).status,
                "PROVENANCE_INVALID",
            )
            unscored = Verdict("task", "TIME_LIMIT", 0.0, 0.1)
            self.assertIs(
                _enforce_verdict_provenance(
                    unscored,
                    candidate,
                    expected_task_contract_sha256=expected_contract,
                    allow_mock_provenance=False,
                ),
                unscored,
            )

    def test_valid_sha256_with_wrong_expected_task_contract_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "result.lean"
            candidate.write_text("theorem t : True := by trivial\n", encoding="utf-8")
            verdict = Verdict(
                "task",
                "PROVED",
                1.0,
                0.1,
                candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),
                task_contract_sha256="1" * 64,
                judge_job_id="job-wrong-contract",
            )

            rejected = _enforce_verdict_provenance(
                verdict,
                candidate,
                expected_task_contract_sha256="2" * 64,
                allow_mock_provenance=False,
            )

            self.assertEqual(rejected.status, "PROVENANCE_INVALID")
            self.assertEqual(rejected.score, 0.0)

    def test_all_modes_reject_unbound_positive_verdict_before_scoreboard(self) -> None:
        def unbound_positive(evaluator, task, candidate_path, *, deadline_monotonic=None):
            return Verdict(
                task.slug,
                "PROVED",
                1.0,
                0.0,
                {"mock": True},
                candidate_sha256="0" * 64,
                task_contract_sha256="1" * 64,
            )

        manifests = {
            "mono": "configs/mono.toml",
            "parallel": "configs/parallel.toml",
            "cps": "configs/smoke.toml",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(MockEvaluator, "evaluate", unbound_positive):
                for mode, manifest in manifests.items():
                    config = replace(
                        load_config(manifest, ROOT),
                        max_tasks=1,
                        max_parallel=1,
                        initial_agents_per_task=1,
                        max_attempts_per_task=1,
                        episodes_per_task=1,
                        time_limit_seconds=2,
                    )
                    run_dir = run_experiment(
                        config,
                        mock_agent=True,
                        output_override=root / mode,
                    )
                    final = json.loads((run_dir / "final.json").read_text())
                    self.assertEqual(final["score"], 0.0, mode)
                    self.assertEqual(
                        next(iter(final["verdicts"].values()))["status"],
                        "PROVENANCE_INVALID",
                        mode,
                    )
                    self.assertIn("verdict_provenance_invalid", final["health"]["issues"])
                    scoreboard = [
                        json.loads(line)
                        for line in (run_dir / "scoreboard_history.jsonl").read_text().splitlines()
                    ]
                    self.assertTrue(scoreboard, mode)
                    self.assertTrue(
                        all(row["status"] == "PROVENANCE_INVALID" for row in scoreboard),
                        mode,
                    )
                    self.assertEqual(final["score_time"]["normalized_score_time_auc"], 0.0)

    def test_all_modes_credit_broker_proof_immediately_and_reuse_frozen_candidate(self) -> None:
        fake_source = (
            "import json, os, pathlib, sys, time, urllib.request\n"
            "request = json.loads(sys.stdin.readline())\n"
            "cwd = pathlib.Path.cwd()\n"
            "tasks_root = cwd / 'tasks'\n"
            "targets = sorted(tasks_root.glob('*/result.lean')) if tasks_root.is_dir() else [cwd / 'result.lean']\n"
            "for target in targets:\n"
            " slug = target.parent.name\n"
            " source = 'import Mathlib\\ntheorem frozen_proof : True := by trivial\\n-- ' + cwd.name + '-' + slug + '\\n'\n"
            " target.write_text(source)\n"
            " payload = {'task_id': slug} if tasks_root.is_dir() else {}\n"
            " probe = urllib.request.Request(os.environ['CONTEXTSWARM_JUDGE_URL'] + '/judge_check', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')\n"
            " with urllib.request.urlopen(probe, timeout=3) as response: json.loads(response.read())\n"
            "time.sleep(0.15)\n"
            "print(json.dumps({'id': request['id'], 'type': 'response', 'command': 'prompt', 'success': True}), flush=True)\n"
            "print(json.dumps({'type': 'agent_end', 'willRetry': False}), flush=True)\n"
            "print(json.dumps({'type': 'agent_settled'}), flush=True)\n"
            "sys.stdin.read()\n"
        )
        manifests = {
            "mono": "configs/mono.toml",
            "parallel": "configs/parallel.toml",
            "cps": "configs/smoke.toml",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(root, fake_source)
            for mode, manifest in manifests.items():
                with self.subTest(mode=mode):
                    output_root = root / mode
                    evaluator = _ImmediateProofEvaluator(output_root)
                    base = load_config(manifest, ROOT)
                    config = replace(
                        base,
                        pi_binary=str(fake),
                        aisw_enabled=False,
                        lean_server_url="http://127.0.0.1:1",
                        max_tasks=1,
                        max_parallel=1,
                        initial_agents_per_task=1,
                        max_attempts_per_task=1,
                        episodes_per_task=1,
                        time_limit_seconds=2,
                    )
                    with patch.dict(os.environ, _FORMAL_PROVENANCE_ENV), patch.object(
                        runner_module, "run_preflight", return_value=None
                    ), patch.object(runner_module, "LeanEvaluator", return_value=evaluator):
                        run_dir = run_experiment(
                            config,
                            mock_agent=False,
                            output_override=output_root,
                        )

                    final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
                    scoreboard = [
                        json.loads(line)
                        for line in (run_dir / "scoreboard_history.jsonl").read_text().splitlines()
                    ]
                    events = [
                        json.loads(line)
                        for line in (run_dir / "events.jsonl").read_text().splitlines()
                    ]
                    positive = [row for row in scoreboard if row["score"] >= 1.0]
                    self.assertEqual(final["score"], 1.0)
                    self.assertEqual(len(positive), 1)
                    self.assertEqual(positive[0]["source"], "judge_check")
                    self.assertIsInstance(positive[0]["horizon_elapsed_seconds"], float)
                    self.assertEqual(evaluator.probe_calls, 1)
                    self.assertEqual(evaluator.evaluate_calls, 1)
                    self.assertEqual(len(evaluator.closeout_receipts), 1)
                    credited_index = next(
                        index for index, row in enumerate(events) if row["event"] == "judge_proof_credited"
                    )
                    finished_index = next(
                        index for index, row in enumerate(events) if row["event"] == "agent_finished"
                    )
                    self.assertLess(credited_index, finished_index)
                    task_id = next(iter(final["verdicts"]))
                    if mode == "mono":
                        frozen = run_dir / "workers" / "mono" / "tasks" / task_id / "result.lean"
                    elif mode == "parallel":
                        frozen = run_dir / "workers" / task_id / "result.lean"
                    else:
                        frozen = run_dir / "workers" / task_id / "best" / "result.lean"
                    self.assertNotIn("changed after probe", frozen.read_text(encoding="utf-8"))
                    self.assertEqual(
                        hashlib.sha256(frozen.read_bytes()).hexdigest(),
                        positive[0]["candidate_sha256"],
                    )
                    closeout_path, closeout_sha256, closeout_job_id = (
                        evaluator.closeout_receipts[0]
                    )
                    self.assertEqual(
                        closeout_path.resolve(),
                        (
                            run_dir
                            / "closeout_candidates"
                            / task_id
                            / "result.lean"
                        ).resolve(),
                    )
                    self.assertEqual(closeout_sha256, positive[0]["candidate_sha256"])
                    final_verdict = final["verdicts"][task_id]
                    self.assertEqual(
                        final_verdict["judge_job_id"],
                        positive[0]["judge_job_id"],
                    )
                    confirmation = next(
                        row
                        for row in events
                        if row["event"] == "closeout_authority_confirmed"
                    )
                    self.assertEqual(
                        confirmation["prior_judge_job_id"],
                        positive[0]["judge_job_id"],
                    )
                    self.assertEqual(
                        confirmation["observed_judge_job_id"],
                        closeout_job_id,
                    )
                    self.assertNotEqual(closeout_job_id, final_verdict["judge_job_id"])

    def test_cps_concurrent_proofs_credit_once_cancel_peers_and_promote_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, os, pathlib, sys, time, urllib.request\n"
                "request = json.loads(sys.stdin.readline())\n"
                "cwd = pathlib.Path.cwd()\n"
                "run_dir = cwd.parents[3]\n"
                "(run_dir / ('ready-' + cwd.name)).write_text('ready')\n"
                "barrier_deadline = time.monotonic() + 2\n"
                "while len(list(run_dir.glob('ready-*'))) < 2 and time.monotonic() < barrier_deadline: time.sleep(0.01)\n"
                "candidate = cwd / 'result.lean'\n"
                "source = 'theorem frozen_' + cwd.name.replace('-', '_') + ' : True := by trivial\\n'\n"
                "candidate.write_text(source)\n"
                "probe = urllib.request.Request(os.environ['CONTEXTSWARM_JUDGE_URL'] + '/judge_check', data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')\n"
                "with urllib.request.urlopen(probe, timeout=4) as response: json.loads(response.read())\n"
                "time.sleep(1.0)\n"
                "print(json.dumps({'id': request['id'], 'type': 'response', 'command': 'prompt', 'success': True}), flush=True)\n"
                "print(json.dumps({'type': 'agent_end', 'willRetry': False}), flush=True)\n"
                "print(json.dumps({'type': 'agent_settled'}), flush=True)\n"
                "sys.stdin.read()\n",
            )
            output_root = root / "run"
            evaluator = _ImmediateProofEvaluator(output_root)
            # Force both Judge handlers through evaluation before either
            # authoritative callback can solve the task and cancel its peer.
            # The prior ready-file barrier only proved that both Pi processes
            # had started, so this test was scheduler-dependent with a
            # single-slot evaluator gate.
            evaluator.probe_barrier = threading.Barrier(2)
            base = load_config("configs/smoke.toml", ROOT)
            config = replace(
                base,
                pi_binary=str(fake),
                aisw_enabled=False,
                lean_server_url="http://127.0.0.1:1",
                max_tasks=1,
                max_parallel=2,
                initial_agents_per_task=2,
                max_attempts_per_task=2,
                lean_max_concurrent_evaluations=2,
                time_limit_seconds=4,
            )
            with patch.dict(os.environ, _FORMAL_PROVENANCE_ENV), patch.object(
                runner_module, "run_preflight", return_value=None
            ), patch.object(runner_module, "LeanEvaluator", return_value=evaluator):
                run_dir = run_experiment(
                    config,
                    mock_agent=False,
                    output_override=output_root,
                )

            scoreboard = [
                json.loads(line)
                for line in (run_dir / "scoreboard_history.jsonl").read_text().splitlines()
            ]
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
            assignments = [
                json.loads(line)
                for line in (run_dir / "elastic_assignments.jsonl").read_text().splitlines()
            ]
            positive = [row for row in scoreboard if row["score"] >= 1.0]
            promotions = [
                row
                for row in events
                if row["event"] == "best_candidate_promoted" and row.get("source") == "judge_check"
            ]
            validation_pieces = [
                item
                for item in CPSStore(run_dir / "cps.sqlite3").search(
                    task_id=assignments[0]["task_id"], query="", limit=20
                )
                if item["kind"] == "validation_result"
            ]
            self.assertEqual(evaluator.probe_calls, 2)
            self.assertEqual(evaluator.evaluate_calls, 1)
            self.assertEqual(len(evaluator.closeout_receipts), 1)
            self.assertEqual(len(assignments), 2)
            self.assertEqual(len(positive), 1)
            self.assertEqual(len(promotions), 1)
            self.assertEqual(len(validation_pieces), 1)
            self.assertEqual(
                sum(row["event"] == "judge_proof_credited" for row in events),
                1,
            )
            best = (
                run_dir
                / "workers"
                / assignments[0]["task_id"]
                / "best"
                / "result.lean"
            )
            self.assertEqual(
                hashlib.sha256(best.read_bytes()).hexdigest(),
                positive[0]["candidate_sha256"],
            )
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final["score"], 1.0)
            task_id = positive[0]["task_id"]
            final_verdict = final["verdicts"][task_id]
            self.assertEqual(final_verdict["judge_job_id"], positive[0]["judge_job_id"])
            closeout_path, closeout_sha256, closeout_job_id = evaluator.closeout_receipts[0]
            self.assertEqual(
                closeout_path.resolve(),
                (run_dir / "closeout_candidates" / task_id / "result.lean").resolve(),
            )
            self.assertEqual(closeout_sha256, positive[0]["candidate_sha256"])
            confirmation = next(
                row for row in events if row["event"] == "closeout_authority_confirmed"
            )
            self.assertEqual(
                confirmation["prior_judge_job_id"],
                positive[0]["judge_job_id"],
            )
            self.assertEqual(confirmation["observed_judge_job_id"], closeout_job_id)
            self.assertEqual(sum(agent["cancelled"] for agent in final["agents"]), 2)

    def test_cps_early_callback_and_peer_final_share_exactly_once_commit_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, os, pathlib, sys, time, urllib.request\n"
                "request = json.loads(sys.stdin.readline())\n"
                "cwd = pathlib.Path.cwd()\n"
                "run_dir = cwd.parents[3]\n"
                "candidate = cwd / 'result.lean'\n"
                "candidate.write_text('theorem race_' + cwd.name.replace('-', '_') + ' : True := by trivial\\n')\n"
                "if cwd.name.endswith('-1'):\n"
                " probe = urllib.request.Request(os.environ['CONTEXTSWARM_JUDGE_URL'] + '/judge_check', data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')\n"
                " with urllib.request.urlopen(probe, timeout=4) as response: json.loads(response.read())\n"
                "else:\n"
                " deadline = time.monotonic() + 3\n"
                " while not (run_dir / 'early-provenance').exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
                "print(json.dumps({'id': request['id'], 'type': 'response', 'command': 'prompt', 'success': True}), flush=True)\n"
                "print(json.dumps({'type': 'agent_end', 'willRetry': False}), flush=True)\n"
                "print(json.dumps({'type': 'agent_settled'}), flush=True)\n"
                "sys.stdin.read()\n",
            )
            evaluator = _ProbeAndFinalProofEvaluator()
            output_root = root / "run"
            original_promote = runner_module._atomic_promote_source
            original_snapshot_provenance = runner_module._has_authoritative_snapshot_provenance

            def hold_before_early_commit(*args, **kwargs):
                valid = original_snapshot_provenance(*args, **kwargs)
                run_dir = next(output_root.iterdir())
                (run_dir / "early-provenance").write_text("ready", encoding="utf-8")
                if not evaluator.final_started.wait(timeout=3):
                    raise AssertionError("peer final evaluator did not start")
                return valid

            def hold_early_prepare(source, destination, expected_sha256):
                evaluator.allow_final_return.set()
                if not evaluator.final_evaluated.wait(timeout=3):
                    raise AssertionError("peer final evaluator did not reach the race barrier")
                return original_promote(source, destination, expected_sha256)

            config = replace(
                load_config("configs/smoke.toml", ROOT),
                pi_binary=str(fake),
                aisw_enabled=False,
                lean_server_url="http://127.0.0.1:1",
                max_tasks=1,
                max_parallel=2,
                initial_agents_per_task=2,
                max_attempts_per_task=2,
                lean_max_concurrent_evaluations=1,
                time_limit_seconds=5,
            )
            with patch.dict(os.environ, _FORMAL_PROVENANCE_ENV), patch.object(
                runner_module, "run_preflight", return_value=None
            ), patch.object(
                runner_module, "LeanEvaluator", return_value=evaluator
            ), patch.object(
                runner_module,
                "_has_authoritative_snapshot_provenance",
                side_effect=hold_before_early_commit,
            ), patch.object(
                runner_module, "_atomic_promote_source", side_effect=hold_early_prepare
            ):
                run_dir = run_experiment(
                    config,
                    mock_agent=False,
                    output_override=output_root,
                )

            scoreboard = [
                json.loads(line)
                for line in (run_dir / "scoreboard_history.jsonl").read_text().splitlines()
            ]
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
            positive = [row for row in scoreboard if row["score"] >= 1.0]
            superseded = [
                row
                for row in scoreboard
                if row.get("response", {}).get("reason") == "proof_superseded_by_peer"
            ]
            task_id = positive[0]["task_id"]
            validation_pieces = [
                item
                for item in CPSStore(run_dir / "cps.sqlite3").search(
                    task_id=task_id, query="", limit=20
                )
                if item["kind"] == "validation_result"
            ]
            promotions = [
                row for row in events if row["event"] == "best_candidate_promoted"
            ]

            self.assertEqual(evaluator.probe_calls, 1)
            self.assertEqual(evaluator.evaluate_calls, 2)
            self.assertEqual(evaluator.solver_evaluate_calls, 1)
            self.assertEqual(evaluator.closeout_evaluate_calls, 1)
            self.assertEqual(len(evaluator.closeout_receipts), 1)
            self.assertEqual(len(positive), 1)
            self.assertEqual(positive[0]["source"], "judge_check")
            self.assertEqual(len(superseded), 1)
            self.assertEqual(len(promotions), 1)
            self.assertEqual(len(validation_pieces), 1)
            self.assertEqual(
                sum(row["event"] == "judge_proof_credited" for row in events),
                1,
            )
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final["score"], 1.0)
            self.assertTrue(final["health"]["ok"], final["health"])
            final_verdict = final["verdicts"][task_id]
            self.assertEqual(final_verdict["judge_job_id"], positive[0]["judge_job_id"])
            closeout_path, closeout_sha256, closeout_job_id = evaluator.closeout_receipts[0]
            self.assertEqual(
                closeout_path.resolve(),
                (run_dir / "closeout_candidates" / task_id / "result.lean").resolve(),
            )
            self.assertEqual(closeout_sha256, positive[0]["candidate_sha256"])
            confirmation = next(
                row for row in events if row["event"] == "closeout_authority_confirmed"
            )
            self.assertEqual(
                confirmation["prior_judge_job_id"],
                positive[0]["judge_job_id"],
            )
            self.assertEqual(confirmation["observed_judge_job_id"], closeout_job_id)

    def test_cps_callback_failures_are_global_fatal_without_partial_credit(self) -> None:
        fake_source = (
            "import json, os, pathlib, sys, urllib.request\n"
            "request = json.loads(sys.stdin.readline())\n"
            "candidate = pathlib.Path.cwd() / 'result.lean'\n"
            "candidate.write_text('theorem callback_failure : True := by trivial\\n')\n"
            "probe = urllib.request.Request(os.environ['CONTEXTSWARM_JUDGE_URL'] + '/judge_check', data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')\n"
            "try:\n"
            " urllib.request.urlopen(probe, timeout=3).read()\n"
            "except Exception:\n"
            " pass\n"
            "print(json.dumps({'id': request['id'], 'type': 'response', 'command': 'prompt', 'success': True}), flush=True)\n"
            "print(json.dumps({'type': 'agent_end', 'willRetry': False}), flush=True)\n"
            "print(json.dumps({'type': 'agent_settled'}), flush=True)\n"
            "sys.stdin.read()\n"
        )
        original_event = runner_module.RunLogger.event
        original_scoreboard = runner_module.RunLogger.scoreboard

        for failure_point in (
            "candidate_promotion",
            "validation_publish",
            "promotion_event",
            "credit_event",
            "scoreboard",
        ):
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fake = _write_fake_pi(root, fake_source)
                output_root = root / "run"
                evaluator = _ImmediateProofEvaluator(output_root)
                config = replace(
                    load_config("configs/smoke.toml", ROOT),
                    pi_binary=str(fake),
                    aisw_enabled=False,
                    lean_server_url="http://127.0.0.1:1",
                    max_tasks=1,
                    max_parallel=1,
                    initial_agents_per_task=1,
                    max_attempts_per_task=3,
                    time_limit_seconds=4,
                )

                def maybe_fail_event(logger, event_type, **payload):
                    target = {
                        "promotion_event": "best_candidate_promoted",
                        "credit_event": "judge_proof_credited",
                    }.get(failure_point)
                    if event_type == target:
                        raise OSError("injected callback artifact failure")
                    return original_event(logger, event_type, **payload)

                def fail_positive_scoreboard(logger, verdict, **kwargs):
                    if verdict.score >= 1.0:
                        raise OSError("injected callback scoreboard failure")
                    return original_scoreboard(logger, verdict, **kwargs)

                with ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ, _FORMAL_PROVENANCE_ENV))
                    stack.enter_context(
                        patch.object(runner_module, "run_preflight", return_value=None)
                    )
                    stack.enter_context(
                        patch.object(runner_module, "LeanEvaluator", return_value=evaluator)
                    )
                    if failure_point == "candidate_promotion":
                        stack.enter_context(
                            patch.object(
                                runner_module,
                                "_atomic_promote_source",
                                side_effect=OSError("injected callback promotion failure"),
                            )
                        )
                    elif failure_point == "validation_publish":
                        stack.enter_context(
                            patch.object(
                                runner_module,
                                "_publish_authoritative_validation",
                                side_effect=OSError("injected callback publish failure"),
                            )
                        )
                    elif failure_point in {"promotion_event", "credit_event"}:
                        stack.enter_context(
                            patch.object(
                                runner_module.RunLogger,
                                "event",
                                new=maybe_fail_event,
                            )
                        )
                    else:
                        stack.enter_context(
                            patch.object(
                                runner_module.RunLogger,
                                "scoreboard",
                                new=fail_positive_scoreboard,
                            )
                        )
                    with self.assertRaisesRegex(
                        RuntimeError, "runner worker/admission failure"
                    ):
                        run_experiment(
                            config,
                            mock_agent=False,
                            output_override=output_root,
                        )

                run_dir = next(output_root.iterdir())
                final = json.loads(
                    (run_dir / "final.json").read_text(encoding="utf-8")
                )
                assignments = [
                    json.loads(line)
                    for line in (run_dir / "elastic_assignments.jsonl").read_text().splitlines()
                ]
                events = [
                    json.loads(line)
                    for line in (run_dir / "events.jsonl").read_text().splitlines()
                ]
                scoreboard_path = run_dir / "scoreboard_history.jsonl"
                scoreboard = (
                    [
                        json.loads(line)
                        for line in scoreboard_path.read_text().splitlines()
                    ]
                    if scoreboard_path.exists()
                    else []
                )
                judge_rows = [
                    json.loads(line)
                    for line in (run_dir / "judge_checks.jsonl").read_text().splitlines()
                ]
                event_names = [row["event"] for row in events]

                self.assertEqual(final["status"], "ERROR")
                self.assertEqual(final["score"], 0.0)
                self.assertEqual(final["verdicts"], {})
                self.assertEqual(len(assignments), 1)
                self.assertFalse(any(row["score"] >= 1.0 for row in scoreboard))
                self.assertEqual(evaluator.probe_calls, 1)
                self.assertEqual(evaluator.evaluate_calls, 0)
                self.assertEqual(judge_rows[-1]["status"], "BROKER_ERROR")
                self.assertLess(
                    event_names.index("judge_broker_closed"),
                    event_names.index("run_error"),
                )
                run_error = next(row for row in events if row["event"] == "run_error")
                self.assertIn("runner worker/admission failure", run_error["error"])

    def test_baselines_do_not_receive_cps_surface(self) -> None:
        for manifest in ("configs/mono.toml", "configs/parallel.toml"):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as temporary:
                run_dir = run_experiment(
                    load_config(manifest, ROOT),
                    mock_agent=True,
                    output_override=Path(temporary),
                )
                self.assertFalse((run_dir / "cps.sqlite3").exists())
                self.assertFalse((run_dir / "communication_trace.jsonl").exists())
                helpers = list((run_dir / "workers").rglob("context_piece"))
                self.assertEqual(helpers, [])

    def test_baseline_child_environment_drops_stale_cps_capabilities(self) -> None:
        stale = {
            "CONTEXTSWARM_CPS_DB": "/tmp/stale.sqlite3",
            "CONTEXTSWARM_ACTORS_FILE": "/tmp/stale-actors.json",
            "CONTEXTSWARM_HORIZON_EPOCH_MS": "9999999999999",
            "CONTEXTSWARM_ASSIGNMENT_FILE": "/tmp/stale-assignments.jsonl",
            "CONTEXTSWARM_BEST_CANDIDATE_FILE": "/tmp/stale-result.lean",
            "CONTEXTSWARM_TASK_ROOT": "/tmp/stale-task",
            "CONTEXTSWARM_CPS_FUTURE_CAPABILITY": "stale",
        }
        with patch.dict(os.environ, stale, clear=False):
            for manifest in ("configs/mono.toml", "configs/parallel.toml"):
                with self.subTest(manifest=manifest):
                    agent = PiAgent(load_config(manifest, ROOT))
                    env = agent.environment(
                        task_id="fresh-task",
                        actor_id="fresh-actor",
                        workdir=ROOT,
                    )
                    self.assertTrue(stale.keys().isdisjoint(env))
                    self.assertEqual(env["CONTEXTSWARM_TASK_ID"], "fresh-task")
                    self.assertEqual(env["CONTEXTSWARM_ACTOR_ID"], "fresh-actor")

            with self.assertRaisesRegex(ValueError, "unsupported solver environment"):
                PiAgent(load_config("configs/cps.toml", ROOT)).environment(
                    task_id="task",
                    actor_id="actor",
                    workdir=ROOT,
                    extra_env={"CONTEXTSWARM_CPS_DB": "/run/current.sqlite3"},
                )

    def test_verdict_helpers(self) -> None:
        self.assertEqual(normalize_base_url("http://judge/api/lean/jobs"), "http://judge")
        self.assertTrue(_is_proved({"status": "PROVED"}))
        self.assertTrue(_is_proved({"status": "succeeded", "is_valid_no_sorry": True}))
        self.assertFalse(_is_proved({"status": "succeeded", "is_valid_no_sorry": False}))
        self.assertFalse(_is_proved({"status": "queued"}))
        self.assertEqual(MockEvaluator().health()["mock"], True)

    def test_pi_rpc_transport_with_fake_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, sys\n"
                "request = json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'id': request['id'], 'type': 'response', 'command': 'prompt', 'success': True}), flush=True)\n"
                "print(json.dumps({'type': 'agent_end', 'willRetry': False}), flush=True)\n"
                "print(json.dumps({'type': 'agent_settled'}), flush=True)\n"
                "sys.stdin.read()\n",
            )
            cfg = _fake_pi_config(root, fake)
            result = PiAgent(cfg).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="hello",
                workdir=root,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.events, 3)

    def test_isolated_pi_command_disables_tools_and_context_discovery(self) -> None:
        config = replace(load_config("configs/smoke.toml", ROOT), fast_mode=True)
        command = PiAgent(config).command(isolated=True)
        for flag in (
            "--no-tools",
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
            "--no-extensions",
        ):
            self.assertIn(flag, command)
        self.assertNotIn("--extension", command)

    def test_pi_rpc_waits_through_retry_until_agent_settled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, pathlib, select, sys, time\n"
                "request = json.loads(sys.stdin.readline())\n"
                "args = sys.argv[1:]\n"
                "session_dir = pathlib.Path(args[args.index('--session-dir') + 1])\n"
                "session_id = args[args.index('--session-id') + 1]\n"
                "session_dir.mkdir(parents=True, exist_ok=True)\n"
                "(session_dir / f'2026-08-21T00-00-00-000Z_{session_id}.jsonl').write_text('{}\\n')\n"
                "events = [\n"
                " {'id': request['id'], 'type': 'response', 'command': 'prompt', 'success': True},\n"
                " {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'error', 'errorMessage': 'request timed out'}},\n"
                " {'type': 'agent_end', 'willRetry': True},\n"
                "]\n"
                "for event in events: print(json.dumps(event), flush=True)\n"
                "time.sleep(0.2)\n"
                "if select.select([sys.stdin], [], [], 0)[0]:\n"
                " print('stdin closed at agent_end', file=sys.stderr, flush=True); sys.exit(9)\n"
                "events = [\n"
                " {'type': 'auto_retry_start', 'attempt': 1, 'maxAttempts': 10, 'delayMs': 2000, 'errorMessage': 'request timed out'},\n"
                " {'type': 'agent_start'},\n"
                " {'type': 'message_start', 'message': {'role': 'assistant', 'content': []}},\n"
                " {'type': 'message_update', 'usage': {'input': 10, 'output': 2, 'cacheRead': 1, 'cacheWrite': 0, 'totalTokens': 13}, 'assistantMessageEvent': {'type': 'text_delta', 'delta': 'recovered'}},\n"
                " {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop', 'content': [{'type': 'text', 'text': 'recovered'}]}},\n"
                " {'type': 'auto_retry_end', 'success': True, 'attempt': 1},\n"
                " {'type': 'agent_end', 'willRetry': False},\n"
                " {'type': 'agent_settled'},\n"
                "]\n"
                "for event in events: print(json.dumps(event), flush=True)\n"
                "sys.stdin.read()\n",
            )
            trace = root / "run" / "pi_events.jsonl"
            workdir = root / "worker"
            workdir.mkdir()
            result = PiAgent(_fake_pi_config(root, fake), trace_path=trace).run(
                task_id="task",
                actor_id="agent-1",
                episode=1,
                prompt="hello",
                workdir=workdir,
            )
            self.assertEqual(result.returncode, 0, result.error_tail)
            self.assertEqual(result.output_tail, "recovered")
            self.assertIn("request timed out", result.error_tail)
            self.assertIn("--session-dir", result.command)
            self.assertIn("--session-id", result.command)
            settings = json.loads((workdir / ".pi" / "settings.json").read_text())
            self.assertEqual(settings["httpIdleTimeoutMs"], 600_000)
            self.assertEqual(settings["retry"]["maxRetries"], 10)
            self.assertEqual(settings["retry"]["provider"]["maxRetries"], 0)
            session_dir = Path(result.command[result.command.index("--session-dir") + 1])
            session_id = result.command[result.command.index("--session-id") + 1]
            self.assertEqual(
                [path.name for path in session_dir.glob(f"*_{session_id}.jsonl")],
                [f"2026-08-21T00-00-00-000Z_{session_id}.jsonl"],
            )
            rows = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual([row["type"] for row in rows].count("agent_end"), 2)
            self.assertEqual(rows[-1]["type"], "agent_settled")
            self.assertTrue(next(row for row in rows if row["type"] == "agent_end")["will_retry"])
            retry = next(row for row in rows if row["type"] == "auto_retry_start")
            self.assertEqual((retry["retry_attempt"], retry["error_category"]), (1, "timeout"))

    def test_pi_rpc_fails_closed_without_agent_settled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, sys\n"
                "json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'type': 'agent_end', 'willRetry': False}), flush=True)\n",
            )
            started = time.monotonic()
            result = PiAgent(_fake_pi_config(root, fake)).run(
                task_id="task", actor_id="agent", episode=1, prompt="hello", workdir=root
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exited before agent_settled after agent_end", result.error_tail)
            self.assertLess(time.monotonic() - started, 2)

    def test_pi_rpc_reports_retry_exhaustion_after_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, sys\n"
                "json.loads(sys.stdin.readline())\n"
                "events = [\n"
                " {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'error', 'errorMessage': 'private <redacted-url> timed out'}},\n"
                " {'type': 'agent_end', 'willRetry': False},\n"
                " {'type': 'auto_retry_end', 'success': False, 'attempt': 10, 'finalError': 'private <redacted-url> timed out'},\n"
                " {'type': 'agent_settled'},\n"
                "]\n"
                "for event in events: print(json.dumps(event), flush=True)\n"
                "sys.stdin.read()\n",
            )
            result = PiAgent(_fake_pi_config(root, fake)).run(
                task_id="task", actor_id="agent", episode=1, prompt="hello", workdir=root
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("settled with an error", result.error_tail)

    def test_pi_rpc_prompt_rejection_is_immediate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, sys\n"
                "request = json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'id': request['id'], 'type': 'response', 'command': 'prompt', 'success': False, 'error': 'prompt rejected'}), flush=True)\n"
                "sys.stdin.read()\n",
            )
            started = time.monotonic()
            result = PiAgent(_fake_pi_config(root, fake)).run(
                task_id="task", actor_id="agent", episode=1, prompt="hello", workdir=root
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("prompt rejected", result.error_tail)
            self.assertLess(time.monotonic() - started, 2)

    def test_pi_rpc_timeout_waiting_for_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, sys, time\n"
                "json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'type': 'agent_end', 'willRetry': True}), flush=True)\n"
                "time.sleep(30)\n",
            )
            result = PiAgent(_fake_pi_config(root, fake, timeout=1)).run(
                task_id="task", actor_id="agent", episode=1, prompt="hello", workdir=root
            )
            self.assertTrue(result.timed_out)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("deadline elapsed before agent_settled", result.error_tail)

    def test_pi_rpc_cancel_waiting_for_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, sys, time\n"
                "json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'type': 'agent_end', 'willRetry': True}), flush=True)\n"
                "time.sleep(30)\n",
            )
            cancel = threading.Event()
            threading.Timer(0.2, cancel.set).start()
            result = PiAgent(_fake_pi_config(root, fake)).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="hello",
                workdir=root,
                cancel_event=cancel,
            )
            self.assertTrue(result.cancelled)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cancelled before agent_settled", result.error_tail)

    def test_pi_rpc_drains_stderr_after_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, sys\n"
                "json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'type': 'agent_settled'}), flush=True)\n"
                "for _ in range(256): print('x' * 1024, file=sys.stderr)\n"
                "sys.stderr.flush(); sys.stdin.read()\n",
            )
            result = PiAgent(_fake_pi_config(root, fake)).run(
                task_id="task", actor_id="agent", episode=1, prompt="hello", workdir=root
            )
            self.assertEqual(result.returncode, 0, result.error_tail)
            self.assertLessEqual(len(result.error_tail), 4_000)

    def test_pi_rpc_concurrent_runs_keep_trace_index_and_sessions_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, pathlib, sys, time\n"
                "request = json.loads(sys.stdin.readline())\n"
                "args = sys.argv[1:]\n"
                "session_dir = pathlib.Path(args[args.index('--session-dir') + 1])\n"
                "session_id = args[args.index('--session-id') + 1]\n"
                "session_dir.mkdir(parents=True, exist_ok=True)\n"
                "(session_dir / f'2026-08-21T00-00-00-000Z_{session_id}.jsonl').write_text('{}\\n')\n"
                "print(json.dumps({'id': request['id'], 'type': 'response', 'command': 'prompt', 'success': True}), flush=True)\n"
                "print(json.dumps({'type': 'agent_start'}), flush=True)\n"
                "time.sleep(0.2)\n"
                "print(json.dumps({'type': 'agent_end', 'willRetry': False}), flush=True)\n"
                "print(json.dumps({'type': 'agent_settled'}), flush=True)\n"
                "sys.stdin.read()\n",
            )
            trace = root / "run" / "pi_events.jsonl"
            agent = PiAgent(_fake_pi_config(root, fake), trace_path=trace)
            workdirs = [root / "worker-a", root / "worker-b"]
            for workdir in workdirs:
                workdir.mkdir()

            def run_one(index: int):
                return agent.run(
                    task_id=f"task-{index}",
                    actor_id=f"agent-{index}",
                    episode=1,
                    prompt="hello",
                    workdir=workdirs[index],
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(run_one, range(2)))

            self.assertEqual([result.returncode for result in results], [0, 0])
            rows = [json.loads(line) for line in trace.read_text().splitlines()]
            index_rows = [
                json.loads(line)
                for line in trace.with_name("pi_session_index.jsonl").read_text().splitlines()
            ]
            session_ids = {row["session_id"] for row in index_rows}
            self.assertEqual(len(index_rows), 2)
            self.assertEqual(len(session_ids), 2)
            self.assertEqual({row["session_id"] for row in rows}, session_ids)
            index_by_session = {row["session_id"]: row for row in index_rows}
            session_locations: set[Path] = set()
            for result in results:
                session_dir = Path(result.command[result.command.index("--session-dir") + 1])
                session_id = result.command[result.command.index("--session-id") + 1]
                session_locations.add(session_dir)
                session_files = list(session_dir.glob(f"*_{session_id}.jsonl"))
                self.assertEqual(len(session_files), 1)
                index_row = index_by_session[session_id]
                indexed_dir = Path(index_row["session_dir"])
                indexed_file = Path(index_row["session_file"])
                if not indexed_dir.is_absolute():
                    indexed_dir = trace.parent / indexed_dir
                if not indexed_file.is_absolute():
                    indexed_file = trace.parent / indexed_file
                self.assertEqual(indexed_dir.resolve(), session_dir.resolve())
                self.assertEqual(indexed_file.resolve(), session_files[0].resolve())
                self.assertEqual(
                    sum(
                        row["type"] == "agent_settled" and row["session_id"] == session_id
                        for row in rows
                    ),
                    1,
                )
            self.assertEqual(len(session_locations), 2)

    def test_pi_rpc_redacts_stderr_secret_split_across_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, sys, time\n"
                "json.loads(sys.stdin.readline())\n"
                "sys.stderr.write('Bearer '); sys.stderr.flush()\n"
                "time.sleep(0.3)\n"
                "sys.stderr.write('cross-chunk-secret\\n'); sys.stderr.flush()\n"
                "print(json.dumps({'type': 'agent_settled'}), flush=True)\n"
                "sys.stdin.read()\n",
            )
            result = PiAgent(_fake_pi_config(root, fake)).run(
                task_id="task", actor_id="agent", episode=1, prompt="hello", workdir=root
            )
            self.assertEqual(result.returncode, 0, result.error_tail)
            self.assertNotIn("cross-chunk-secret", result.error_tail)
            self.assertIn("<redacted>", result.error_tail)

    def test_pi_rpc_redacts_unlabelled_opaque_tokens_from_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, sys\n"
                "json.loads(sys.stdin.readline())\n"
                "opaque = 'Z' * 64\n"
                "print(json.dumps({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': 'stdout ' + opaque[:32]}}), flush=True)\n"
                "print(json.dumps({'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': opaque[32:]}}), flush=True)\n"
                "print('stderr ' + opaque, file=sys.stderr, flush=True)\n"
                "print(json.dumps({'type': 'agent_settled'}), flush=True)\n"
                "sys.stdin.read()\n",
            )
            result = PiAgent(_fake_pi_config(root, fake)).run(
                task_id="task", actor_id="agent", episode=1, prompt="hello", workdir=root
            )
            opaque = "Z" * 64
            self.assertEqual(result.returncode, 0, result.error_tail)
            self.assertNotIn(opaque, result.output_tail)
            self.assertNotIn(opaque, result.error_tail)
            self.assertIn("<redacted-secret>", result.output_tail)
            self.assertIn("<redacted-secret>", result.error_tail)

    def test_pi_rpc_timeout_and_cancel_fail_closed_when_sigterm_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, signal, sys, time\n"
                "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
                "json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'type': 'agent_end', 'willRetry': True}), flush=True)\n"
                "time.sleep(30)\n",
            )
            with self.subTest(reason="timeout"):
                timed_out = PiAgent(_fake_pi_config(root, fake, timeout=1)).run(
                    task_id="timeout",
                    actor_id="timeout-agent",
                    episode=1,
                    prompt="hello",
                    workdir=root,
                )
                self.assertTrue(timed_out.timed_out)
                self.assertNotEqual(timed_out.returncode, 0)

            with self.subTest(reason="cancel"):
                cancel = threading.Event()
                threading.Timer(0.2, cancel.set).start()
                cancelled = PiAgent(_fake_pi_config(root, fake)).run(
                    task_id="cancel",
                    actor_id="cancel-agent",
                    episode=1,
                    prompt="hello",
                    workdir=root,
                    cancel_event=cancel,
                )
                self.assertTrue(cancelled.cancelled)
                self.assertNotEqual(cancelled.returncode, 0)

    def test_pi_rpc_caught_processing_error_fails_closed_when_child_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = _write_fake_pi(
                root,
                "import json, signal, sys, time\n"
                "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
                "json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'type': 'agent_start'}), flush=True)\n"
                "time.sleep(30)\n",
            )
            with patch("contextswarm_mini.pi_agent._event_text", side_effect=ValueError("bad event")):
                result = PiAgent(_fake_pi_config(root, fake)).run(
                    task_id="task", actor_id="agent", episode=1, prompt="hello", workdir=root
                )
            self.assertIn("bad event", result.error_tail)
            self.assertNotEqual(result.returncode, 0)

    def test_pi_rpc_event_parsing_and_redaction(self) -> None:
        text_delta = {
            "type": "message_update",
            "usage": {"input": 10, "output": 2, "cacheRead": 3, "cacheWrite": 4, "totalTokens": 19},
            "assistantMessageEvent": {"type": "text_delta", "delta": "visible"},
        }
        self.assertEqual(_event_text(text_delta), "visible")
        self.assertEqual(
            _usage_fields(text_delta),
            {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_tokens": 3,
                "cache_write_tokens": 4,
                "total_tokens": 19,
            },
        )
        for hidden_type in ("thinking_delta", "toolcall_delta"):
            hidden = {
                "type": "message_update",
                "assistantMessageEvent": {"type": hidden_type, "delta": "secret"},
            }
            self.assertEqual(_event_text(hidden), "")
        tool_error = {
            "type": "tool_execution_end",
            "toolName": "bash",
            "isError": True,
            "result": {"content": [{"type": "text", "text": "request timed out"}]},
        }
        fields = _event_trace_fields(tool_error)
        self.assertEqual((fields["tool_name"], fields["tool_error"], fields["error_category"]), ("bash", True, "timeout"))
        redacted = _redact_sensitive_text(
            "Bearer abc.def api_key='private-value' endpoint=https://private.example/path "
            "token=bare-token password: bare-password credential='bare-credential' "
            "Authorization: Basic dXNlcjpwYXNz opaque=" + "Z" * 64
        )
        for secret in (
            "abc.def",
            "private-value",
            "private.example",
            "bare-token",
            "bare-password",
            "bare-credential",
            "dXNlcjpwYXNz",
            "Z" * 64,
        ):
            self.assertNotIn(secret, redacted)

    def test_lean_http_contract_and_polling(self) -> None:
        tasks = load_tasks(load_config("configs/cps.toml", ROOT))
        task = tasks[0]
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "result.lean"
            candidate.write_text(task.baseline_code.replace("by sorry", "by\n  sorry"), encoding="utf-8")
            seen: list[dict[str, object]] = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args: object) -> None:
                    return

                def do_GET(self) -> None:  # noqa: N802
                    if self.path == "/healthz":
                        body = {"ok": True, "accepted_lean_env_ids": ["formal_matholympiadbench"]}
                    else:
                        body = {
                            "job_id": "j1",
                            "status": "succeeded",
                            "is_valid_no_sorry": True,
                        }
                    raw = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def do_POST(self) -> None:  # noqa: N802
                    length = int(self.headers.get("Content-Length", "0"))
                    seen.append(json.loads(self.rfile.read(length)))
                    raw = b'{"job_id":"j1","status":"queued"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                evaluator = LeanEvaluator(
                    f"http://127.0.0.1:{server.server_port}",
                    lean_env_id="formal_matholympiadbench",
                    timeout_seconds=5,
                    poll_interval_seconds=0.01,
                )
                verdict = evaluator.evaluate(task, candidate)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
            self.assertEqual(verdict.status, "PROVED")
            self.assertEqual(seen[0]["problem_id"], task.problem_id)
            self.assertEqual(seen[0]["lean_env_id"], "formal_matholympiadbench")


if __name__ == "__main__":
    unittest.main()
