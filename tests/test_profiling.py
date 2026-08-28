from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from contextswarm_mini.config import load_config
from contextswarm_mini.evaluator import candidate_sha256, task_contract_sha256
from contextswarm_mini.judge_broker import JudgeBroker
from contextswarm_mini.models import Task, Verdict
from contextswarm_mini.pi_agent import PiAgent
from contextswarm_mini.profiling import (
    PROFILE_FILENAME,
    PROFILE_SCHEMA_VERSION,
    RunProfiler,
)
from contextswarm_mini.runner import RunLogger


ROOT = Path(__file__).resolve().parents[1]


def _post(url: str, operation: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"{url}/{operation}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        result = json.loads(response.read())
    assert isinstance(result, dict)
    return result


class _ReceiptEvaluator:
    is_mock_evaluator = True

    def __init__(self, status: str) -> None:
        self.status = status

    def expected_task_contract_sha256(self, task: Task) -> str:
        return task_contract_sha256(
            task,
            lean_env_id="mock",
            verification_profile="mock",
            judge_mode="mock",
        )

    def probe_source(
        self,
        task: Task,
        candidate_code: str,
        *,
        deadline_monotonic: float | None,
    ) -> Verdict:
        del deadline_monotonic
        return Verdict(
            task.slug,
            self.status,
            1.0 if self.status == "PROVED" else 0.0,
            0.01,
            response={"mock": True},
            candidate_sha256=candidate_sha256(candidate_code),
            task_contract_sha256=self.expected_task_contract_sha256(task),
            judge_job_id="job-1",
            cache_reused=self.status != "PROVED",
        )


def _receipt_task(root: Path) -> Task:
    return Task(
        slug="task",
        root=root,
        problem_text="problem",
        baseline_code="import Mathlib\ntheorem task : True := by trivial\n",
        metadata={"problem_id": "task", "theorem_name": "task"},
    )


class ProfilingTests(unittest.TestCase):
    """Small, disk-backed tests for the opt-in profiler boundary."""

    def _rows(self, path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_default_off_has_no_profile_file_or_sampler_and_logger_skips_profile_timing(self) -> None:
        # /tmp is a tmpfs in the test container.  Keep all fixtures under the
        # repository's disk-backed filesystem while still using tempfile for
        # automatic, recoverable cleanup.
        with tempfile.TemporaryDirectory(prefix=".contextswarm-profile-", dir=str(ROOT)) as temporary:
            root = Path(temporary)
            with patch.dict(
                os.environ,
                {
                    "CONTEXTSWARM_PROFILE": "0",
                    "CONTEXTSWARM_RESOURCE_PROFILING": "0",
                    "CONTEXTSWARM_PROFILING": "0",
                },
                clear=False,
            ):
                profiler = RunProfiler.from_environment(root, run_id="run-1")
                self.assertFalse(profiler.enabled)
                self.assertIsNone(profiler._sampler_thread)
                profiler.start(root_pid=os.getpid())
                profiler.emit("disabled.event", prompt="must-not-be-written")
                self.assertEqual(profiler.sample_now(force=True), {})
                profiler.close()

                logger = RunLogger(root)
                # A disabled logger event still needs its ordinary JSONL
                # write, but must not take profiling-only clocks.
                with patch(
                    "contextswarm_mini.runner.time.monotonic",
                    side_effect=AssertionError("profiling clock used while disabled"),
                ):
                    logger.event("ordinary_event", value="ok")
                logger.close()

            self.assertFalse((root / PROFILE_FILENAME).exists())
            self.assertEqual({path.name for path in root.iterdir()}, {"events.jsonl"})
            self.assertNotIn(
                "contextswarm-profiler",
                {thread.name for thread in threading.enumerate()},
            )

    def test_enabled_schema_filters_sensitive_fields_and_records_span(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".contextswarm-profile-", dir=str(ROOT)) as temporary:
            root = Path(temporary)
            profiler = RunProfiler(
                root,
                enabled=True,
                heartbeat_interval_seconds=60,
                run_id="run-1",
            )
            profiler.start(root_pid=os.getpid())
            with profiler.span(
                "selection.rank",
                task_id="task-1",
                actor_id="agent-1",
                candidate_count=2,
            ):
                profiler.emit(
                    "security.check",
                    task_id="task-1",
                    actor_id="agent-1",
                    status="ok",
                    accepted=True,
                    candidate_sha256="a" * 64,
                    prompt="PROMPT_SHOULD_NOT_APPEAR",
                    candidate="CANDIDATE_SHOULD_NOT_APPEAR",
                    secret="SECRET_SHOULD_NOT_APPEAR",
                    query="QUERY_SHOULD_NOT_APPEAR",
                    nested={"secret": "NESTED_SHOULD_NOT_APPEAR"},
                )
            profiler.sample_now(force=True)
            profiler.close()

            profile_path = root / PROFILE_FILENAME
            self.assertTrue(profile_path.is_file())
            self.assertEqual(stat.S_IMODE(profile_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            rows = self._rows(profile_path)
            self.assertGreaterEqual(len(rows), 5)
            self.assertTrue(all(row["schema_version"] == PROFILE_SCHEMA_VERSION for row in rows))
            self.assertEqual(
                [row["sequence"] for row in rows],
                list(range(1, len(rows) + 1)),
            )
            events = {str(row["event"]) for row in rows}
            self.assertIn("selection.rank.start", events)
            self.assertIn("selection.rank.end", events)
            self.assertIn("resource.sample", events)
            span_end = next(row for row in rows if row["event"] == "selection.rank.end")
            self.assertIn("wall_seconds", span_end)
            self.assertIn("cpu_user_seconds", span_end)
            self.assertIn("cpu_system_seconds", span_end)
            security = next(row for row in rows if row["event"] == "security.check")
            self.assertEqual(security["candidate_sha256"], "a" * 64)
            self.assertGreaterEqual(int(security.get("dropped_fields", 0)), 5)
            forbidden_keys = {"prompt", "candidate", "secret", "query", "nested"}
            self.assertTrue(forbidden_keys.isdisjoint(security))
            serialized = profile_path.read_text(encoding="utf-8")
            for value in (
                "PROMPT_SHOULD_NOT_APPEAR",
                "CANDIDATE_SHOULD_NOT_APPEAR",
                "SECRET_SHOULD_NOT_APPEAR",
                "QUERY_SHOULD_NOT_APPEAR",
                "NESTED_SHOULD_NOT_APPEAR",
            ):
                self.assertNotIn(value, serialized)

    def test_cgroup_scope_precedes_root_and_snapshot_never_returns_path(self) -> None:
        if os.name != "posix":
            self.skipTest("cgroup v2 probing is Unix-specific")
        scoped = Path("/sys/fs/cgroup/session.slice/run.scope")
        root = Path("/sys/fs/cgroup")
        candidates = RunProfiler._cgroup_candidates("0::/session.slice/run.scope\n")
        self.assertEqual(candidates[0], scoped)
        self.assertEqual(candidates[-1], root)

        # Exercise the actual reader with synthetic lookup roots.  Returned
        # metrics are scalar-only; the local cgroup path must not be present.
        with patch.object(RunProfiler, "_cgroup_candidates", return_value=(scoped, root)):
            snapshot = RunProfiler._cgroup_snapshot()
        rendered = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn("session.slice", rendered)
        self.assertNotIn("/sys/fs/cgroup", rendered)
        self.assertTrue(all(not isinstance(value, (dict, list, str)) for value in snapshot.values()))

    def test_process_registration_is_attributed_and_close_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".contextswarm-profile-", dir=str(ROOT)) as temporary:
            root = Path(temporary)
            profiler = RunProfiler(root, enabled=True, heartbeat_interval_seconds=60, run_id="run-2")
            profiler.start(root_pid=os.getpid())
            profiler.register_process(
                os.getpid(),
                task_id="task-2",
                actor_id="agent-2",
                role="solver",
            )
            profiler.sample_now(force=True)
            profiler.unregister_process(os.getpid(), status="exited")
            profiler.close()
            first_close = (root / PROFILE_FILENAME).read_text(encoding="utf-8")
            profiler.close()
            self.assertEqual(first_close, (root / PROFILE_FILENAME).read_text(encoding="utf-8"))

            rows = self._rows(root / PROFILE_FILENAME)
            registered = [row for row in rows if row["event"] == "resource.process.register"]
            unregistered = [row for row in rows if row["event"] == "resource.process.unregister"]
            samples = [row for row in rows if row["event"] == "resource.process"]
            self.assertTrue(registered)
            self.assertTrue(unregistered)
            self.assertTrue(samples)
            self.assertEqual(registered[-1]["task_id"], "task-2")
            self.assertEqual(registered[-1]["actor_id"], "agent-2")
            self.assertEqual(unregistered[-1]["task_id"], "task-2")
            self.assertTrue(
                any(
                    row.get("task_id") == "task-2" and row.get("actor_id") == "agent-2"
                    for row in samples
                )
            )
            self.assertEqual(sum(row["event"] == "profile.end" for row in rows), 1)

    def test_pi_agent_registers_and_unregisters_spawned_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".contextswarm-profile-", dir=str(ROOT)) as temporary:
            root = Path(temporary)
            fake = root / "fake-pi"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys, time\n"
                "request = json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'id': request['id'], 'type': 'response', 'command': 'prompt', 'success': True}), flush=True)\n"
                "print(json.dumps({'type': 'agent_settled'}), flush=True)\n"
                "time.sleep(0.25)\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            workdir = root / "work"
            workdir.mkdir()
            config = replace(
                load_config("configs/smoke.toml", ROOT),
                pi_binary=str(fake),
                aisw_enabled=False,
                pi_timeout_seconds=5,
            )
            profile_root = root / "profile"
            profiler = RunProfiler(
                profile_root,
                enabled=True,
                heartbeat_interval_seconds=0.1,
                run_id="run-3",
            )
            profiler.start(root_pid=os.getpid())
            result = PiAgent(config, profiler=profiler).run(
                task_id="task-3",
                actor_id="agent-3",
                episode=4,
                prompt="mock prompt",
                workdir=workdir,
            )
            profiler.close()

            self.assertEqual(result.returncode, 0, result.error_tail)
            rows = self._rows(profile_root / PROFILE_FILENAME)
            registered = [row for row in rows if row["event"] == "resource.process.register"]
            unregistered = [row for row in rows if row["event"] == "resource.process.unregister"]
            self.assertTrue(registered)
            self.assertTrue(unregistered)
            self.assertEqual(registered[-1]["task_id"], "task-3")
            self.assertEqual(registered[-1]["actor_id"], "agent-3")
            self.assertEqual(unregistered[-1]["task_id"], "task-3")
            self.assertEqual(unregistered[-1]["actor_id"], "agent-3")
            self.assertEqual(registered[-1]["pid"], unregistered[-1]["pid"])
            self.assertTrue(
                any(
                    row["event"] == "resource.process"
                    and row.get("task_id") == "task-3"
                    and row.get("actor_id") == "agent-3"
                    for row in rows
                )
            )

    def test_judge_receipt_profile_covers_success_and_failure(self) -> None:
        for status in ("PROVED", "VERIFY_FAIL"):
            with self.subTest(status=status), tempfile.TemporaryDirectory(
                prefix=".contextswarm-profile-", dir=str(ROOT)
            ) as temporary:
                root = Path(temporary)
                workdir = root / "worker"
                workdir.mkdir()
                candidate = workdir / "result.lean"
                candidate.write_text(
                    "import Mathlib\ntheorem task : True := by trivial\n",
                    encoding="utf-8",
                )
                profile_root = root / "profile"
                profiler = RunProfiler(
                    profile_root,
                    enabled=True,
                    heartbeat_interval_seconds=60,
                    run_id=f"run-{status.casefold()}",
                )
                profiler.start(root_pid=os.getpid())
                broker = JudgeBroker(
                    _ReceiptEvaluator(status),
                    # One local fake evaluator slot; no external service is
                    # contacted by this focused test.
                    threading.BoundedSemaphore(1),
                    audit_path=root / "audit.jsonl",
                    min_probe_interval_seconds=0,
                    profiler=profiler,
                ).start()
                try:
                    with broker.session(
                        actor_id="agent",
                        workdir=workdir,
                        candidates={"task": (_receipt_task(root), candidate)},
                        deadline_monotonic=time.monotonic() + 3,
                    ) as env:
                        result = _post(env["CONTEXTSWARM_JUDGE_URL"], "judge_check", {})
                finally:
                    broker.close()
                    profiler.close()

                self.assertEqual(result["status"], status)
                rows = self._rows(profile_root / PROFILE_FILENAME)
                receipts = [row for row in rows if row["event"] == "judge.receipt"]
                self.assertEqual(len(receipts), 1)
                receipt = receipts[0]
                self.assertEqual(receipt["status"], status)
                self.assertIn("gate_wait_seconds", receipt)
                self.assertIn("elapsed_seconds", receipt)
                self.assertIn("cache_reused", receipt)
                self.assertEqual(receipt["accepted"], True)


if __name__ == "__main__":
    unittest.main()
