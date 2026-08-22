from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import unittest

from contextswarm_mini.models import Task
from contextswarm_mini.prompts import (
    SOLVER_EXECUTION_CONTRACT,
    build_finalization_prompt,
    build_mono_prompt,
    build_task_prompt,
    render_problem_work_mode,
)


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = ROOT / "benchmarks" / "matholympiadbench"
WORK_MODE_MARKER = "        ## Work Mode\n\n"
NEXT_SECTION = "\n        ## "
CPS_TOOLS = (
    "cps_search",
    "cps_publish",
    "cps_inbox",
    "cps_send",
    "cps_ack",
    "cps_actors",
)


def _task(slug: str = "sample") -> Task:
    return Task(
        slug=slug,
        root=Path("benchmarks") / slug,
        problem_text="example",
        baseline_code="theorem example : True := by sorry",
        metadata={},
    )


class PromptContractTests(unittest.TestCase):
    def _all_solver_prompts(self) -> dict[str, str]:
        task = _task()
        return {
            "cps": build_task_prompt(
                task,
                task_workspace="tasks/sample",
                agent_id="worker-sample-e1",
                episode=1,
                communication_enabled=True,
            ),
            "parallel": build_task_prompt(
                task,
                task_workspace="tasks/sample",
                agent_id="worker-sample-e1",
                episode=1,
                communication_enabled=False,
            ),
            "mono": build_mono_prompt(
                [task],
                workspace="workers/mono",
                communication_enabled=False,
            ),
            "finalization": build_finalization_prompt(task),
        }

    def test_every_solver_prompt_contains_remote_only_judge_contract(self) -> None:
        required_phrases = (
            "Use only `judge_check`",
            "`CONTEXTSWARM_JUDGE_URL` is reserved for that tool",
            "Never invoke local `lean`, `lake`, `elan`",
            "Do not install or download Lean, Mathlib",
            "start background, detached, or",
            "parallel processes",
            "Never call a raw Judge or evaluator HTTP endpoint",
            "Never fall back to",
        )
        for name, prompt in self._all_solver_prompts().items():
            with self.subTest(prompt=name):
                self.assertIn(SOLVER_EXECUTION_CONTRACT, prompt)
                for phrase in required_phrases:
                    self.assertIn(phrase, prompt)
                self.assertIsNone(re.search(r"https?://", prompt))

    def test_only_cps_prompt_exposes_controlled_communication_tools(self) -> None:
        prompts = self._all_solver_prompts()
        for tool in CPS_TOOLS:
            self.assertIn(tool, prompts["cps"])
            for name in ("parallel", "mono"):
                with self.subTest(tool=tool, prompt=name):
                    self.assertNotIn(tool, prompts[name])
        self.assertNotIn("context_piece", prompts["cps"])

    def test_all_problem_work_modes_match_the_canonical_contract(self) -> None:
        paths = sorted(BENCHMARK_ROOT.glob("*/problem.md"))
        self.assertEqual(len(paths), 12)
        for path in paths:
            source = path.read_text(encoding="utf-8")
            _, marker, after = source.partition(WORK_MODE_MARKER)
            with self.subTest(problem=path.parent.name):
                self.assertEqual(marker, WORK_MODE_MARKER)
                next_section = after.find(NEXT_SECTION)
                actual = after[:next_section] if next_section >= 0 else after.rstrip("\n")
                self.assertEqual(actual, render_problem_work_mode())

    def test_problem_work_mode_sync_check_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/sync_problem_work_mode.py", "--check"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
