from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from contextswarm_mini.config import load_config
from contextswarm_mini.models import Task
from contextswarm_mini.preflight import PreflightError, run_preflight
from contextswarm_mini.prompts import (
    CODING_EXECUTION_CONTRACT,
    build_finalization_prompt,
    build_mono_prompt,
    build_task_prompt,
)


ROOT = Path(__file__).resolve().parents[1]


def _coding_task(slug: str = "sample") -> Task:
    return Task(
        slug=slug,
        root=ROOT / "benchmarks" / "icpc_wf_2025" / slug,
        problem_text="solve the problem",
        baseline_code="#include <bits/stdc++.h>\nint main() {}\n",
        metadata={"language": "cpp", "candidate_filename": "result.cpp"},
    )


def _coding_health(*, usaco: bool = False, ready_workers: int = 64) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "service": "contextswarm-judge",
        "api_version": "v1",
        "resident_service_version": "test-version",
        "evaluate_endpoint": "/api/judge/evaluate",
        "package_root": "/judge/packages",
        "oj_base_url": "http://127.0.0.1:10086",
        "coding_jobs": {
            "enabled": True,
            "worker_count": 64,
            "queue_size": 0,
            "status_counts": {"running": 0},
        },
        "configured_workers": 64,
        "ready_workers": ready_workers,
        "busy_workers": 0,
        "queued_jobs": 0,
    }
    if usaco:
        payload["legacy_usaco"] = {
            "enabled": True,
            "ready": True,
            "problem_count": 12,
            "ready_problem_count": 12,
        }
    return payload


class CodingPromptTests(unittest.TestCase):
    def test_task_prompts_use_cpp_without_formal_vocabulary(self) -> None:
        task = _coding_task()
        prompts = (
            build_task_prompt(
                task,
                task_workspace="task",
                agent_id="worker",
                episode=1,
                communication_enabled=False,
            ),
            build_mono_prompt([task], workspace="mono", communication_enabled=False),
            build_finalization_prompt(task),
        )
        for prompt in prompts:
            self.assertIn("result.cpp", prompt)
            self.assertIn("judge_check", prompt)
            self.assertNotIn("result.lean", prompt)
            self.assertNotIn("Lean/Mathlib", prompt)
            self.assertNotIn("local `lean`", prompt)
        self.assertIn(CODING_EXECUTION_CONTRACT, prompts[0])

    def test_mono_rejects_mixed_candidate_languages(self) -> None:
        formal = replace(_coding_task(), metadata={"language": "lean"})
        with self.assertRaises(ValueError):
            build_mono_prompt([_coding_task(), formal], workspace="mono", communication_enabled=False)


class CodingPiPathGuardTests(unittest.TestCase):
    def test_result_cpp_is_readable_and_writable_when_bound(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        harness = r'''
import { pathToFileURL } from "node:url";
const [extensionPath, workdir] = process.argv.slice(1);
const listeners = new Map();
const pi = { on(name, cb) { listeners.set(name, cb); }, registerTool() {} };
const ext = await import(pathToFileURL(extensionPath).href);
ext.default(pi);
const guard = listeners.get("tool_call");
const invoke = async (toolName, path) => (await guard({toolName, input:{path}}, {cwd:workdir}))?.block === true;
process.stdout.write(JSON.stringify({
  read_cpp: await invoke("read", "result.cpp"),
  write_cpp: await invoke("write", "result.cpp"),
  read_lean: await invoke("read", "result.lean"),
  write_lean: await invoke("write", "result.lean"),
  bash_formal: (await guard({toolName:"bash", input:{command:"python3 evaluate.py"}}, {cwd:workdir}))?.block === true,
}));
'''
        with tempfile.TemporaryDirectory() as raw:
            workdir = Path(raw)
            (workdir / "problem.md").write_text("statement\n", encoding="utf-8")
            (workdir / "result.cpp").write_text("int main() {}\n", encoding="utf-8")
            (workdir / "baseline").mkdir()
            (workdir / "baseline" / "baseline.cpp").write_text("int main() {}\n", encoding="utf-8")
            env = dict(os.environ)
            env["CONTEXTSWARM_WORKDIR"] = str(workdir)
            env["CONTEXTSWARM_CANDIDATE_FILENAME"] = "result.cpp"
            result = subprocess.run(
                [node, "--input-type=module", "-e", harness,
                 str(ROOT / "contextswarm_mini" / "pi_solver_tools.mjs"), str(workdir)],
                cwd=workdir,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertFalse(observed["read_cpp"])
        self.assertFalse(observed["write_cpp"])
        self.assertTrue(observed["read_lean"])
        self.assertTrue(observed["write_lean"])
        self.assertTrue(observed["bash_formal"])


class CodingPreflightTests(unittest.TestCase):
    def _config(self, dataset: str):
        base = load_config("configs/smoke.toml", ROOT)
        return replace(
            base,
            judge_kind="coding",
            formal_tools_enabled=False,
            aisw_enabled=False,
            lean_server_url="http://judge.invalid",
            dataset_root=Path("benchmarks") / dataset,
        )

    def test_coding_preflight_uses_coding_health_without_kernel_probe(self) -> None:
        config = self._config("icpc_wf_2025")
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                patch("contextswarm_mini.preflight.CodingEvaluator.health", return_value=_coding_health()),
                patch("contextswarm_mini.preflight._kernel_probe", side_effect=AssertionError("Lean probe used")),
            ):
                report = run_preflight(config, Path(raw))
        self.assertEqual(report["judge_kind"], "coding")
        self.assertEqual(report["coding"]["dataset"], "icpc")
        self.assertEqual(report["coding"]["capacity"]["ready_workers"], 64)
        self.assertFalse(report["formal_tools"]["enabled"])
        self.assertNotIn("lean", report["coding"])

    def test_usaco_preflight_requires_complete_inventory(self) -> None:
        config = self._config("usaco")
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                patch("contextswarm_mini.preflight.CodingEvaluator.health", return_value=_coding_health(usaco=True)),
            ):
                report = run_preflight(config, Path(raw))
        self.assertEqual(report["coding"]["dataset"], "usaco")
        self.assertEqual(report["coding"]["legacy_usaco"]["problem_count"], 12)

        incomplete = _coding_health(usaco=True)
        incomplete["legacy_usaco"] = {
            "enabled": True,
            "ready": False,
            "problem_count": 12,
            "ready_problem_count": 11,
        }
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                patch("contextswarm_mini.preflight.CodingEvaluator.health", return_value=incomplete),
            ):
                with self.assertRaisesRegex(PreflightError, "USACO dataset"):
                    run_preflight(config, Path(raw))


if __name__ == "__main__":
    unittest.main()
