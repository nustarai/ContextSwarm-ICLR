from __future__ import annotations

import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from contextswarm_mini.config import load_config
from contextswarm_mini.cps import CPSStore, make_policy
from contextswarm_mini.evaluator import LeanEvaluator, MockEvaluator, _is_proved, normalize_base_url
from contextswarm_mini.runner import load_tasks, run_experiment
from contextswarm_mini.pi_agent import PiAgent


ROOT = Path(__file__).resolve().parents[1]


class MiniRuntimeTests(unittest.TestCase):
    def test_dataset_and_protocol_manifests(self) -> None:
        tasks = load_tasks(load_config("configs/cps.toml", ROOT))
        self.assertEqual(len(tasks), 12)
        self.assertEqual(tasks[0].slug, "imo2024_p1")
        self.assertIn("theorem", tasks[0].baseline_code)
        mono = load_config("configs/mono.toml", ROOT)
        parallel = load_config("configs/parallel.toml", ROOT)
        self.assertEqual((mono.mode, mono.communication, mono.max_parallel), ("mono", "none", 1))
        self.assertEqual((parallel.mode, parallel.communication), ("parallel", "none"))

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
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = run_experiment(
                load_config("configs/parallel.toml", ROOT),
                mock_agent=True,
                output_override=Path(temporary),
            )
            self.assertFalse((run_dir / "cps.sqlite3").exists())
            worker = run_dir / "workers" / "imo2024_p1"
            self.assertFalse((worker / "context_piece").exists())

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
            fake = root / "fake-pi"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    print(json.dumps({'type': 'agent_end'}), flush=True)\n"
                "    break\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            cfg = replace(
                load_config("configs/smoke.toml", ROOT),
                pi_binary=str(fake),
                aisw_enabled=False,
                pi_timeout_seconds=5,
            )
            result = PiAgent(cfg).run(
                task_id="task",
                actor_id="agent",
                episode=1,
                prompt="hello",
                workdir=root,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.events, 1)

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
