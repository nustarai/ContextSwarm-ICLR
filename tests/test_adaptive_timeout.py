from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from contextswarm_mini.config import load_config
from contextswarm_mini.evaluator import LeanEvaluator
from contextswarm_mini.formal_tools import DeclarationIndex, FormalToolPolicy
from contextswarm_mini.judge_broker import JudgeBroker
from contextswarm_mini.models import Task, Verdict
from contextswarm_mini.prompts import build_task_prompt
from contextswarm_mini.profiling import RunProfiler
from contextswarm_mini.timeout_policy import normalize_agent_timeout


ROOT = Path(__file__).resolve().parents[1]


def _post(url: str, operation: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"{url}/{operation}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        value = json.loads(response.read().decode("utf-8"))
    assert isinstance(value, dict)
    return value


class _TimeoutEvaluator:
    is_mock_evaluator = False
    timeout_seconds = 300

    def __init__(self) -> None:
        self.timeouts: list[int | None] = []

    def expected_task_contract_sha256(self, task: Task) -> str:
        return hashlib.sha256(task.slug.encode("utf-8")).hexdigest()

    def probe_source(
        self,
        task: Task,
        source: str,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: object | None = None,
        timeout_seconds: int | None = None,
    ) -> Verdict:
        del source, deadline_monotonic, cancel_event
        self.timeouts.append(timeout_seconds)
        return Verdict(
            task.slug,
            "VERIFY_FAIL",
            0.0,
            0.01,
            {},
            candidate_sha256="a" * 64,
            task_contract_sha256=self.expected_task_contract_sha256(task),
            judge_job_id=f"job-{len(self.timeouts)}",
        )


def _task(root: Path) -> Task:
    source = "import Mathlib\ntheorem task : True := by sorry\n"
    return Task(
        slug="task",
        root=root,
        problem_text="Prove True.",
        baseline_code=source,
        metadata={"problem_id": "task", "theorem_name": "task"},
    )


def _policy() -> FormalToolPolicy:
    return FormalToolPolicy(
        enabled=True,
        surface_version="adaptive-timeout-test-v1",
        evaluate_calls_per_task=8,
        evaluate_backend_jobs_per_task=8,
        query_calls_per_task=8,
        query_backend_probes_per_task=8,
        max_candidate_bytes=1024 * 1024,
        command_timeout_seconds=30,
        declaration_index=DeclarationIndex(None),
    )


class AdaptiveTimeoutTests(unittest.TestCase):
    def test_enabled_prompt_requires_deliberate_budget_choice_and_keeps_baseline_quiet(self) -> None:
        task = _task(ROOT)
        enabled = build_task_prompt(
            task,
            task_workspace="tasks/task",
            agent_id="worker-task-e1",
            episode=1,
            communication_enabled=False,
            formal_tools_enabled=True,
            agent_timeout_enabled=True,
        )
        disabled = build_task_prompt(
            task,
            task_workspace="tasks/task",
            agent_id="worker-task-e1",
            episode=1,
            communication_enabled=False,
            formal_tools_enabled=True,
        )
        self.assertIn("Agent-proposed validation budget", enabled)
        self.assertIn("timeout_seconds", enabled)
        self.assertIn("evaluate.py --timeout", enabled)
        self.assertIn("EXECUTION_TIMEOUT", enabled)
        self.assertNotIn("Agent-proposed validation budget", disabled)

    def test_solver_schema_and_formal_guard_follow_capability_bit(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        harness = r"""
import { pathToFileURL } from "node:url";
const [extensionPath, workdir] = process.argv.slice(1);
const listeners = new Map();
const definitions = {};
const pi = {
  on(name, callback) { listeners.set(name, callback); },
  registerTool(definition) { definitions[definition.name] = definition; },
};
const extension = await import(pathToFileURL(extensionPath).href);
extension.default(pi);
const guard = listeners.get("tool_call");
const check = async (command) => (await guard(
  { toolName: "bash", input: { command } },
  { cwd: workdir },
))?.block === true;
process.stdout.write(JSON.stringify({
  schema: definitions.judge_check.parameters.properties.timeout_seconds ?? null,
  enabled_command_blocked: await check("python3 evaluate.py --timeout 60"),
  malformed_command_blocked: await check("python3 evaluate.py --timeout nope"),
}));
"""
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            (workdir / "evaluate.py").write_text("# staged helper\n", encoding="utf-8")
            base_env = os.environ | {
                "CONTEXTSWARM_WORKDIR": str(workdir),
                "CONTEXTSWARM_CANDIDATE_FILENAME": "result.lean",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
            }
            enabled = subprocess.run(
                [
                    node,
                    "--input-type=module",
                    "--eval",
                    harness,
                    str(ROOT / "contextswarm_mini" / "pi_solver_tools.mjs"),
                    str(workdir),
                ],
                cwd=workdir,
                env=base_env | {"CONTEXTSWARM_AGENT_TIMEOUT_ENABLED": "1"},
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            disabled = subprocess.run(
                [
                    node,
                    "--input-type=module",
                    "--eval",
                    harness,
                    str(ROOT / "contextswarm_mini" / "pi_solver_tools.mjs"),
                    str(workdir),
                ],
                cwd=workdir,
                env=base_env | {"CONTEXTSWARM_AGENT_TIMEOUT_ENABLED": "0"},
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        enabled_value = json.loads(enabled.stdout)
        self.assertEqual(enabled_value["schema"]["minimum"], 5)
        self.assertNotIn("maximum", enabled_value["schema"])
        self.assertFalse(enabled_value["enabled_command_blocked"])
        self.assertTrue(enabled_value["malformed_command_blocked"])
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        disabled_value = json.loads(disabled.stdout)
        self.assertIsNone(disabled_value["schema"])
        self.assertTrue(disabled_value["enabled_command_blocked"])

    def test_normalization_clamps_both_bounds_and_honors_evaluator_cap(self) -> None:
        self.assertEqual(normalize_agent_timeout(60).effective_seconds, 60)
        self.assertEqual(normalize_agent_timeout(999).effective_seconds, 300)
        self.assertTrue(normalize_agent_timeout(999).clamped)
        self.assertEqual(normalize_agent_timeout(1).effective_seconds, 5)
        self.assertEqual(
            normalize_agent_timeout(300, configured_timeout_seconds=30).effective_seconds,
            30,
        )
        with self.assertRaises(ValueError):
            normalize_agent_timeout(True)

    def test_treatment_config_advertises_capability_and_baseline_does_not(self) -> None:
        baseline = load_config("configs/formal_1h_cps32_profiled_clean.toml", ROOT)
        treatment = load_config(
            "configs/formal_1h_cps32_profiled_adaptive_timeout.toml", ROOT
        )
        self.assertFalse(baseline.judge_agent_timeout_enabled)
        self.assertTrue(treatment.judge_agent_timeout_enabled)
        self.assertFalse(baseline.public_dict()["judge_agent_timeout_enabled"])
        self.assertTrue(treatment.public_dict()["judge_agent_timeout_enabled"])
        self.assertEqual(baseline.lean_timeout_seconds, treatment.lean_timeout_seconds)
        self.assertEqual(baseline.max_parallel, treatment.max_parallel)
        self.assertEqual(baseline.time_limit_seconds, treatment.time_limit_seconds)

    def test_broker_clamps_and_audits_judge_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = _task(root)
            workdir = root / "worker"
            workdir.mkdir()
            (workdir / "result.lean").write_text(task.baseline_code, encoding="utf-8")
            evaluator = _TimeoutEvaluator()
            broker = JudgeBroker(
                evaluator,
                threading.BoundedSemaphore(1),
                audit_path=root / "judge_checks.jsonl",
                formal_policy=_policy(),
                agent_timeout_enabled=True,
                min_probe_interval_seconds=0,
                drain_timeout_seconds=1,
            ).start()
            try:
                with broker.session(
                    actor_id="worker",
                    workdir=workdir,
                    candidates={task.slug: (task, workdir / "result.lean")},
                    deadline_monotonic=10**9,
                ) as env:
                    normal = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "judge_check",
                        {"timeout_seconds": 60},
                    )
                    clamped = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "judge_check",
                        {"timeout_seconds": 999},
                    )
                self.assertEqual(normal["effective_timeout_seconds"], 60)
                self.assertFalse(normal["timeout_clamped"])
                self.assertEqual(clamped["requested_timeout_seconds"], 999)
                self.assertEqual(clamped["effective_timeout_seconds"], 300)
                self.assertTrue(clamped["timeout_clamped"])
                self.assertEqual(evaluator.timeouts, [60, 300])
                rows = [
                    json.loads(line)
                    for line in (root / "judge_checks.jsonl").read_text().splitlines()
                ]
                self.assertEqual(
                    [row["effective_timeout_seconds"] for row in rows], [60, 300]
                )
            finally:
                broker.close()

    def test_evaluate_local_uses_the_same_timeout_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = _task(root)
            workdir = root / "worker"
            workdir.mkdir()
            (workdir / "result.lean").write_text(task.baseline_code, encoding="utf-8")
            evaluator = _TimeoutEvaluator()
            broker = JudgeBroker(
                evaluator,
                threading.BoundedSemaphore(1),
                audit_path=root / "judge_checks.jsonl",
                formal_audit_path=root / "formal_tool_calls.jsonl",
                formal_policy=_policy(),
                agent_timeout_enabled=True,
                min_probe_interval_seconds=0,
                drain_timeout_seconds=1,
            ).start()
            try:
                with broker.session(
                    actor_id="worker",
                    workdir=workdir,
                    candidates={task.slug: (task, workdir / "result.lean")},
                    deadline_monotonic=10**9,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "evaluate_local",
                        {"timeout_seconds": 60},
                    )
                self.assertEqual(result["effective_timeout_seconds"], 60)
                self.assertEqual(evaluator.timeouts, [60])
                rows = [
                    json.loads(line)
                    for line in (root / "formal_tool_calls.jsonl").read_text().splitlines()
                ]
                self.assertEqual(rows[0]["effective_timeout_seconds"], 60)
            finally:
                broker.close()

    def test_profiling_allowlist_keeps_timeout_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profiler = RunProfiler(root, enabled=True, run_id="run-1")
            profiler.emit(
                "judge.receipt",
                requested_timeout_seconds=999,
                effective_timeout_seconds=300,
                timeout_clamped=True,
                timeout_source="agent_requested",
            )
            profiler.close()
            row = json.loads((root / "profiling.jsonl").read_text().splitlines()[0])
        self.assertEqual(row["requested_timeout_seconds"], 999)
        self.assertEqual(row["effective_timeout_seconds"], 300)
        self.assertTrue(row["timeout_clamped"])
        self.assertEqual(row["timeout_source"], "agent_requested")
        self.assertNotIn("dropped_fields", row)

    def test_disabled_broker_rejects_timeout_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = _task(root)
            workdir = root / "worker"
            workdir.mkdir()
            (workdir / "result.lean").write_text(task.baseline_code, encoding="utf-8")
            broker = JudgeBroker(
                _TimeoutEvaluator(),
                threading.BoundedSemaphore(1),
                audit_path=root / "judge_checks.jsonl",
                formal_policy=_policy(),
                drain_timeout_seconds=1,
            ).start()
            try:
                with broker.session(
                    actor_id="worker",
                    workdir=workdir,
                    candidates={task.slug: (task, workdir / "result.lean")},
                    deadline_monotonic=10**9,
                ) as env:
                    result = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "judge_check",
                        {"timeout_seconds": 60},
                    )
                self.assertEqual(result["status"], "INVALID_REQUEST")
            finally:
                broker.close()

    def test_lean_payload_uses_one_backend_attempt_for_custom_budget(self) -> None:
        class RecordingLean(LeanEvaluator):
            def __init__(self) -> None:
                super().__init__("http://unused", lean_env_id="test")
                self.payloads: list[dict[str, object]] = []

            def _request(self, method, path, payload=None, *, timeout_seconds=None, cancel_event=None):  # type: ignore[no-untyped-def]
                del method, path, timeout_seconds, cancel_event
                self.payloads.append(dict(payload or {}))
                return {
                    "job_id": f"job-{len(self.payloads)}",
                    "status": "failed",
                    "formal_status": "VERIFY_FAIL",
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = _task(root)
            evaluator = RecordingLean()
            evaluator.probe_source(task, "import Mathlib\ntheorem task : True := by sorry\n", timeout_seconds=60)
            evaluator.probe_source(task, "import Mathlib\ntheorem task : True := by sorry\n--2\n", timeout_seconds=999)
            evaluator.probe_source(task, "import Mathlib\ntheorem task : True := by sorry\n--3\n")
        self.assertEqual(evaluator.payloads[0]["timeout"], 60)
        self.assertEqual(evaluator.payloads[0]["max_retries"], 0)
        self.assertEqual(evaluator.payloads[1]["timeout"], 300)
        self.assertEqual(evaluator.payloads[1]["max_retries"], 0)
        self.assertEqual(evaluator.payloads[2]["timeout"], 300)
        self.assertEqual(evaluator.payloads[2]["max_retries"], 1)


if __name__ == "__main__":
    unittest.main()
