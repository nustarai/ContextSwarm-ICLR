from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest

from contextswarm_mini.agent_recovery import run_with_recovery
from contextswarm_mini.checkpoint import CHECKPOINT_SCHEMA_VERSION, CheckpointStore
from contextswarm_mini.config import CheckpointConfig, load_config
from contextswarm_mini.cps import CPSStore, make_policy
from contextswarm_mini.models import AgentResult, Verdict
from contextswarm_mini.runner import (
    RunLogger,
    _checkpoint_context,
    _run_elastic_cps,
    load_tasks,
)


ROOT = Path(__file__).resolve().parents[1]


def _result(*, returncode: int, timed_out: bool = False) -> AgentResult:
    return AgentResult(
        agent_id="worker-a",
        task_id="task-a",
        episode=1,
        returncode=returncode,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        timed_out=timed_out,
        error_tail="/home/secret/provider-token=do-not-copy" if returncode else "",
        output_tail="partial route /tmp/private-response" if returncode else "",
    )


class _Broker:
    @contextmanager
    def session(self, **_kwargs):
        yield {
            "CONTEXTSWARM_JUDGE_URL": "http://127.0.0.1:1/test-token",
            "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "9999999999999",
        }


class _PartialThenSuccessPi:
    """Expose whether a fresh assignment receives the prior checkpoint."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.handoff_seen: list[tuple[str, str]] = []

    def run(self, **kwargs):
        self.calls.append(dict(kwargs))
        attempt = len(self.calls)
        workdir = Path(kwargs["workdir"])
        if attempt >= 3:
            metadata = json.loads(
                (workdir / "checkpoint" / "checkpoint.json").read_text()
            )
            candidate = (workdir / "checkpoint" / "result.lean").read_text()
            self.handoff_seen.append((metadata["terminal_reason"], candidate))
        (workdir / "result.lean").write_text(f"partial-{attempt}\n")
        now = "2026-01-01T00:00:00+00:00"
        return AgentResult(
            agent_id=str(kwargs["actor_id"]),
            task_id=str(kwargs["task_id"]),
            episode=int(kwargs["episode"]),
            returncode=0 if attempt >= 3 else 1,
            started_at=now,
            finished_at=now,
            error_tail="process failure" if attempt < 3 else "",
        )


class _PartialThenBaselineThenSuccessPi(_PartialThenSuccessPi):
    """Leave the active candidate at baseline on the second failed attempt."""

    def run(self, **kwargs):
        self.calls.append(dict(kwargs))
        attempt = len(self.calls)
        workdir = Path(kwargs["workdir"])
        if attempt >= 3:
            metadata = json.loads(
                (workdir / "checkpoint" / "checkpoint.json").read_text()
            )
            candidate = (workdir / "checkpoint" / "result.lean").read_text()
            self.handoff_seen.append((metadata["candidate"]["source"], candidate))
        if attempt == 1:
            (workdir / "result.lean").write_text("partial-1\n")
        now = "2026-01-01T00:00:00+00:00"
        return AgentResult(
            agent_id=str(kwargs["actor_id"]),
            task_id=str(kwargs["task_id"]),
            episode=int(kwargs["episode"]),
            returncode=0 if attempt >= 3 else 1,
            started_at=now,
            finished_at=now,
            error_tail="process failure" if attempt < 3 else "",
        )


class _SkippedEvaluator:
    is_mock_evaluator = True

    def expected_task_contract_sha256(self, _task) -> str:
        return "a" * 64

    def evaluate(
        self,
        task,
        candidate_path: Path,
        *,
        deadline_monotonic=None,
        cancel_event=None,
        settlement_callback=None,
    ) -> Verdict:
        del deadline_monotonic, cancel_event, settlement_callback
        return Verdict(
            task.slug,
            "MOCK_SKIPPED",
            0.0,
            0.0,
            {"candidate_bytes": candidate_path.stat().st_size},
            candidate_sha256="b" * 64,
            task_contract_sha256="a" * 64,
        )


class _FeedbackPi:
    def run(self, **kwargs):
        (Path(kwargs["workdir"]) / "result.lean").write_text(
            "partial candidate\n", encoding="utf-8"
        )
        now = "2026-01-01T00:00:00+00:00"
        return AgentResult(
            agent_id=str(kwargs["actor_id"]),
            task_id=str(kwargs["task_id"]),
            episode=int(kwargs["episode"]),
            returncode=0,
            started_at=now,
            finished_at=now,
        )


class _FeedbackEvaluator:
    is_mock_evaluator = True

    def expected_task_contract_sha256(self, _task) -> str:
        return "a" * 64

    def evaluate(
        self,
        task,
        candidate_path: Path,
        *,
        deadline_monotonic=None,
        cancel_event=None,
        settlement_callback=None,
    ) -> Verdict:
        del deadline_monotonic, cancel_event, settlement_callback
        return Verdict(
            task.slug,
            "VERIFY_FAIL",
            0.0,
            0.0,
            {"error_message": "counterexample: reject this route", "mock": True},
            candidate_sha256=hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
            task_contract_sha256="a" * 64,
        )


class CheckpointStoreTests(unittest.TestCase):
    def test_manifest_enables_only_the_checkpoint_treatment_surface(self) -> None:
        baseline = load_config("configs/formal_1h_cps32_profiled_clean.toml", ROOT)
        treatment = load_config(
            "configs/formal_1h_cps32_profiled_checkpoint.toml", ROOT
        )
        self.assertFalse(baseline.checkpoint.enabled)
        self.assertEqual(
            treatment.checkpoint,
            CheckpointConfig(
                enabled=True,
                transfer=True,
                publish=True,
                max_candidate_bytes=2 * 1024 * 1024,
                max_summary_chars=6000,
                max_context_items=6,
            ),
        )
        baseline_public = baseline.public_dict()
        treatment_public = treatment.public_dict()
        treatment_public.pop("name")
        baseline_public.pop("name")
        treatment_public.pop("checkpoint")
        baseline_public.pop("checkpoint")
        self.assertEqual(treatment_public, baseline_public)

    def test_save_is_immutable_bounded_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_root = root / "workers" / "task-a"
            task_root.mkdir(parents=True)
            candidate = task_root / "result.lean"
            candidate.write_text("partial proof\n", encoding="utf-8")
            store = CheckpointStore(root, max_context_items=1, max_summary_chars=512)
            baseline = hashlib.sha256(b"baseline\n").hexdigest()
            ref = store.save(
                task_id="task-a",
                task_root=task_root,
                candidate_path=candidate,
                candidate_filename="result.lean",
                baseline_sha256=baseline,
                actor_id="worker-a",
                episode=1,
                recovery_attempt=0,
                result=_result(returncode=1, timed_out=True).as_dict(),
                retry_pending=False,
                context={
                    "completed_work": [
                        {
                            "piece_id": "piece-1",
                            "kind": "lemma",
                            "title": "route",
                            "body": "/home/ubuntu/private-token",
                        },
                        {"kind": "ignored", "title": "second", "body": "second"},
                    ],
                    "ruled_out": [],
                    "next_step": "run judge_check",
                },
                feedback="feedback /tmp/raw-response",
            )
            self.assertEqual(ref.record["schema_version"], CHECKPOINT_SCHEMA_VERSION)
            self.assertTrue(ref.record["unverified"])
            self.assertFalse(ref.record["score_eligible"])
            self.assertEqual(ref.record["terminal_reason"], "timeout")
            self.assertEqual(ref.record["latest_validation"]["status"], "NONE")
            self.assertIsNone(ref.record["latest_validation"]["best_candidate_sha256"])
            self.assertTrue(ref.candidate_changed_from_baseline)
            self.assertEqual(ref.candidate_sha256, hashlib.sha256(b"partial proof\n").hexdigest())
            self.assertEqual(ref.candidate_path.read_text(), "partial proof\n")
            self.assertNotIn("/home/ubuntu/private-token", json.dumps(ref.record))
            self.assertNotIn("/home/secret", json.dumps(ref.record))
            self.assertNotIn("/tmp/raw-response", json.dumps(ref.record))
            self.assertEqual((task_root / "checkpoints" / "latest.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "checkpoints").stat().st_mode & 0o777, 0o700)
            self.assertEqual((task_root / "checkpoints").stat().st_mode & 0o777, 0o700)

            candidate.write_text("newer\n", encoding="utf-8")
            second = store.save(
                task_id="task-a",
                task_root=task_root,
                candidate_path=candidate,
                candidate_filename="result.lean",
                baseline_sha256=baseline,
                actor_id="worker-a",
                episode=1,
                recovery_attempt=1,
                result=_result(returncode=1).as_dict(),
                retry_pending=True,
            )
            self.assertEqual(second.sequence, ref.sequence + 1)
            self.assertEqual(ref.candidate_path.read_text(), "partial proof\n")
            self.assertEqual(second.record["retry_pending"], True)

    def test_materialize_respects_transfer_flag_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_root = root / "workers" / "task-a"
            task_root.mkdir(parents=True)
            candidate = task_root / "result.cpp"
            candidate.write_text("partial\n")
            ref = CheckpointStore(root).save(
                task_id="task-a",
                task_root=task_root,
                candidate_path=candidate,
                candidate_filename="result.cpp",
                baseline_sha256="0" * 64,
                actor_id="worker-a",
                episode=1,
                recovery_attempt=0,
                result=_result(returncode=1).as_dict(),
                retry_pending=False,
            )
            no_transfer = root / "fresh-no-transfer"
            CheckpointStore(root).materialize_for_agent(
                ref,
                no_transfer,
                candidate_filename="result.cpp",
                transfer_candidate=False,
            )
            self.assertTrue((no_transfer / "checkpoint.json").is_file())
            self.assertFalse((no_transfer / "result.cpp").exists())
            transfer = root / "fresh-transfer"
            CheckpointStore(root).materialize_for_agent(
                ref,
                transfer,
                candidate_filename="result.cpp",
                transfer_candidate=True,
            )
            self.assertEqual((transfer / "result.cpp").read_text(), "partial\n")

    def test_rejects_path_like_candidate_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_root = root / "task"
            task_root.mkdir()
            candidate = task_root / "result.lean"
            candidate.write_text("x")
            with self.assertRaises(ValueError):
                CheckpointStore(root).save(
                    task_id="task",
                    task_root=task_root,
                    candidate_path=candidate,
                    candidate_filename="../escape",
                    baseline_sha256="0" * 64,
                    actor_id="agent",
                    episode=1,
                    recovery_attempt=0,
                    result=_result(returncode=1).as_dict(),
                    retry_pending=False,
                )

    def test_candidate_symlink_is_recorded_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_root = root / "task"
            task_root.mkdir()
            outside = root / "outside.lean"
            outside.write_text("private candidate\n")
            candidate = task_root / "result.lean"
            candidate.symlink_to(outside)
            ref = CheckpointStore(root).save(
                task_id="task",
                task_root=task_root,
                candidate_path=candidate,
                candidate_filename="result.lean",
                baseline_sha256="0" * 64,
                actor_id="agent",
                episode=1,
                recovery_attempt=0,
                result=_result(returncode=1).as_dict(),
                retry_pending=False,
            )
            self.assertEqual(ref.record["candidate"]["status"], "not_regular")
            self.assertIsNone(ref.candidate_path)
            self.assertNotIn("private candidate", json.dumps(ref.record))

    def test_candidate_outside_task_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_root = root / "task"
            task_root.mkdir()
            outside = root / "outside.lean"
            outside.write_text("candidate\n")
            with self.assertRaises(ValueError):
                CheckpointStore(root).save(
                    task_id="task",
                    task_root=task_root,
                    candidate_path=outside,
                    candidate_filename="result.lean",
                    baseline_sha256="0" * 64,
                    actor_id="agent",
                    episode=1,
                    recovery_attempt=0,
                    result=_result(returncode=1).as_dict(),
                    retry_pending=False,
                )

    def test_recent_message_ledger_keeps_acknowledged_direct_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CPSStore(Path(temporary) / "cps.sqlite3")
            message = store.send_message(
                task_id="task-a",
                sender="agent-a",
                recipient="agent-b",
                body="counterexample found; do not repeat this route",
            )
            self.assertTrue(store.ack_message(message["id"], "agent-b"))
            rows = store.recent_messages(task_id="task-a", limit=2)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["body"], message["body"])
            self.assertTrue(rows[0]["acked_at"])

    def test_checkpoint_context_preserves_message_only_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CPSStore(Path(temporary) / "cps.sqlite3")
            store.send_message(
                task_id="task-a",
                sender="agent-a",
                recipient="agent-b",
                body="counterexample found; do not repeat this route",
            )
            context = _checkpoint_context(
                store,
                "task-a",
                max_items=6,
                max_chars=6000,
            )
            self.assertEqual(context["source"], "cps_recent_evidence")
            self.assertTrue(any("counterexample" in row["body"] for row in context["ruled_out"]))


class CheckpointRecoveryTests(unittest.TestCase):
    def test_result_sink_sees_failed_attempt_before_retry_and_success(self) -> None:
        rows: list[tuple[int, bool, int]] = []
        calls = 0

        def invoke(_attempt: int) -> AgentResult:
            nonlocal calls
            calls += 1
            return _result(returncode=1 if calls == 1 else 0)

        result = run_with_recovery(
            invoke,
            task_id="task-a",
            actor_id="worker-a",
            episode=1,
            deadline_monotonic=time.monotonic() + 10,
            max_restarts=1,
            base_delay_seconds=0,
            on_result=lambda value, attempt, retry: rows.append(
                (attempt, retry, value.returncode)
            ),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(rows, [(0, True, 1), (1, False, 0)])


class CheckpointRunnerIntegrationTests(unittest.TestCase):
    def test_final_judge_feedback_refreshes_latest_checkpoint(self) -> None:
        base = load_config("configs/smoke.toml", ROOT)
        config = replace(
            base,
            max_tasks=1,
            max_parallel=1,
            initial_agents_per_task=1,
            max_attempts_per_task=1,
            time_limit_seconds=2,
            checkpoint=CheckpointConfig(enabled=True, transfer=True, publish=True),
        )
        task = load_tasks(config)[0]
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            store = CPSStore(run_dir / "cps.sqlite3")
            policy = make_policy(config.communication, store)
            results = _run_elastic_cps(
                config,
                [task],
                run_dir,
                logger,
                _FeedbackEvaluator(),
                _FeedbackPi(),
                policy,
                mock_agent=False,
                deadline=time.monotonic() + 2,
                evaluator_gate=threading.BoundedSemaphore(1),
                judge_broker=_Broker(),
                scheduler_result_sink=[],
            )
            self.assertEqual([(verdict.status) for _, verdict in results], ["VERIFY_FAIL"])
            checkpoint_root = run_dir / "workers" / task.slug / "checkpoints"
            latest = json.loads((checkpoint_root / "latest.json").read_text())
            metadata = json.loads(
                (checkpoint_root / latest["metadata"]).read_text()
            )
            self.assertEqual(metadata["latest_validation"]["status"], "VERIFY_FAIL")
            self.assertIn(
                "counterexample",
                metadata["latest_validation"]["feedback"],
            )
            self.assertTrue(metadata["unverified"])
            self.assertFalse(metadata["score_eligible"])
            checkpoint_pieces = [
                row
                for row in store.progress_snapshot(
                    [task.slug], recent_limit=20, body_chars=8_000
                )[task.slug]["recent_pieces"]
                if row["kind"] == "checkpoint"
            ]
            self.assertEqual(len(checkpoint_pieces), 1)
            self.assertEqual(
                json.loads(checkpoint_pieces[0]["body"])["latest_validation"]["status"],
                "VERIFY_FAIL",
            )

    def test_failed_partial_is_persisted_published_and_seen_by_refill(self) -> None:
        base = load_config("configs/smoke.toml", ROOT)
        config = replace(
            base,
            max_tasks=1,
            max_parallel=1,
            initial_agents_per_task=1,
            max_attempts_per_task=2,
            time_limit_seconds=2,
            pi_recovery_enabled=True,
            pi_recovery_max_restarts=1,
            pi_recovery_base_delay_ms=0,
            checkpoint=CheckpointConfig(
                enabled=True,
                transfer=True,
                publish=True,
                max_candidate_bytes=2 * 1024 * 1024,
                max_summary_chars=6000,
                max_context_items=6,
            ),
        )
        task = load_tasks(config)[0]
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            store = CPSStore(run_dir / "cps.sqlite3")
            policy = make_policy(config.communication, store)
            pi = _PartialThenSuccessPi()
            results = _run_elastic_cps(
                config,
                [task],
                run_dir,
                logger,
                _SkippedEvaluator(),
                pi,
                policy,
                mock_agent=False,
                deadline=time.monotonic() + 2,
                evaluator_gate=threading.BoundedSemaphore(1),
                judge_broker=_Broker(),
                scheduler_result_sink=[],
            )
            self.assertEqual(len(pi.calls), 3)
            self.assertEqual(pi.handoff_seen, [("process_failure", "partial-2\n")])
            checkpoint_root = run_dir / "workers" / task.slug / "checkpoints"
            self.assertTrue((checkpoint_root / "latest.json").is_file())
            self.assertGreaterEqual(len(list(checkpoint_root.glob("*/checkpoint.json"))), 3)
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
            self.assertGreaterEqual(
                sum(row.get("event") == "checkpoint_saved" for row in events), 3
            )
            self.assertTrue(any(row.get("event") == "checkpoint_handoff" for row in events))
            published = [
                row
                for row in store.progress_snapshot(
                    [task.slug], recent_limit=20, body_chars=8_000
                )[task.slug]["recent_pieces"]
                if row["kind"] == "checkpoint"
            ]
            self.assertGreaterEqual(len(published), 1)
            published_payload = json.loads(published[0]["body"])
            self.assertTrue(published_payload["unverified"])
            self.assertFalse(published_payload["score_eligible"])
            self.assertLessEqual(len(published[0]["body"].encode("utf-8")), 7_500)
            self.assertTrue(all(not verdict.status.startswith("PROVED") for _, verdict in results))

    def test_changed_checkpoint_is_carried_forward_over_unchanged_closeout(self) -> None:
        base = load_config("configs/smoke.toml", ROOT)
        config = replace(
            base,
            max_tasks=1,
            max_parallel=1,
            initial_agents_per_task=1,
            max_attempts_per_task=3,
            pi_recovery_enabled=False,
            checkpoint=CheckpointConfig(enabled=True, transfer=True, publish=False),
            time_limit_seconds=2,
        )
        task = load_tasks(config)[0]
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            store = CPSStore(run_dir / "cps.sqlite3")
            policy = make_policy(config.communication, store)
            pi = _PartialThenBaselineThenSuccessPi()
            _run_elastic_cps(
                config,
                [task],
                run_dir,
                logger,
                _SkippedEvaluator(),
                pi,
                policy,
                mock_agent=False,
                deadline=time.monotonic() + 2,
                evaluator_gate=threading.BoundedSemaphore(1),
                judge_broker=_Broker(),
                scheduler_result_sink=[],
            )
            self.assertEqual(pi.handoff_seen, [("carry_forward_changed_checkpoint", "partial-1\n")])
            latest = json.loads(
                (run_dir / "workers" / task.slug / "checkpoints" / "latest.json").read_text()
            )
            metadata = json.loads(
                (
                    run_dir
                    / "workers"
                    / task.slug
                    / "checkpoints"
                    / latest["metadata"]
                ).read_text()
            )
            self.assertEqual(metadata["candidate"]["source"], "carry_forward_changed_checkpoint")
            self.assertTrue(metadata["candidate"]["changed_from_baseline"])


if __name__ == "__main__":
    unittest.main()
