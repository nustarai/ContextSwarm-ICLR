from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from contextswarm_mini.config import _redact_endpoint, load_config
from contextswarm_mini.cps import CPSStore, make_policy
from contextswarm_mini.evaluator import (
    FORMAL_VERDICT_SCHEMA_VERSION,
    LeanEvaluator,
    MockEvaluator,
    _is_proved,
    normalize_base_url,
)
from contextswarm_mini.runner import load_tasks, run_experiment
from contextswarm_mini.pi_agent import (
    PiAgent,
    _event_text,
    _event_trace_fields,
    _redact_sensitive_text,
    _usage_fields,
)


ROOT = Path(__file__).resolve().parents[1]


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


class MiniRuntimeTests(unittest.TestCase):
    def test_run_summary_redacts_non_loopback_endpoints(self) -> None:
        self.assertEqual(
            _redact_endpoint("http://127.0.0.1:18000/api/lean/jobs"),
            "http://127.0.0.1:18000",
        )
        self.assertEqual(
            _redact_endpoint("https://judge.example/private"),
            "<configured>",
        )

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
            run_dir = run_experiment(
                load_config("configs/smoke.toml", ROOT),
                mock_agent=True,
                output_override=Path(temporary),
            )
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            self.assertEqual(final["schema_version"], "contextswarm_mini_run_v1")
            self.assertEqual(final["mode"], "cps")
            self.assertTrue((run_dir / "cps.sqlite3").exists())
            self.assertTrue((run_dir / "scoreboard_history.jsonl").exists())

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
                task_count = len(load_tasks(load_config(manifest, ROOT)))
                self.assertEqual(
                    len(list((run_dir / "workers").rglob("evaluate.py"))),
                    task_count,
                )
                self.assertEqual(
                    len(list((run_dir / "workers").rglob("formal_query"))),
                    task_count,
                )

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

            cps_env = PiAgent(load_config("configs/cps.toml", ROOT)).environment(
                task_id="task",
                actor_id="actor",
                workdir=ROOT,
                extra_env={"CONTEXTSWARM_CPS_DB": "/run/current.sqlite3"},
            )
            self.assertEqual(cps_env["CONTEXTSWARM_CPS_DB"], "/run/current.sqlite3")

    def test_verdict_helpers(self) -> None:
        self.assertEqual(normalize_base_url("http://judge/api/lean/jobs"), "http://judge")
        canonical = {
            "status": "succeeded",
            "is_valid_no_sorry": True,
            "canonical_verdict": {
                "schema_version": FORMAL_VERDICT_SCHEMA_VERSION,
                "status": "PROVED",
                "score": 1.0,
                "correct": True,
                "cheating": False,
                "source_contract_status": "ok",
                "signature_check_status": "ok",
            },
        }
        self.assertTrue(_is_proved(canonical))
        self.assertFalse(_is_proved({"status": "PROVED"}))
        self.assertFalse(_is_proved({"status": "succeeded", "is_valid_no_sorry": True}))
        self.assertFalse(_is_proved({**canonical, "is_valid_no_sorry": False}))
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
            "Authorization: Basic dXNlcjpwYXNz"
        )
        for secret in (
            "abc.def",
            "private-value",
            "private.example",
            "bare-token",
            "bare-password",
            "bare-credential",
            "dXNlcjpwYXNz",
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
                        candidate_sha256 = hashlib.sha256(
                            str(seen[0]["code"]).encode("utf-8")
                        ).hexdigest()
                        body = {
                            "job_id": "j1",
                            "status": "succeeded",
                            "formal_verdict_schema_version": FORMAL_VERDICT_SCHEMA_VERSION,
                            "is_valid_no_sorry": True,
                            "canonical_verdict": {
                                "schema_version": FORMAL_VERDICT_SCHEMA_VERSION,
                                "status": "PROVED",
                                "score": 1.0,
                                "correct": True,
                                "cheating": False,
                                "source_contract_status": "ok",
                                "signature_check_status": "ok",
                                "solution_hash": candidate_sha256,
                            },
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
