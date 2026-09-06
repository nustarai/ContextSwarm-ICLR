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
WORK_MODE_HEADING = re.compile(r"(?m)^(?P<indent>[ ]*)## Work Mode[ ]*\n\n")
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
            "Judge already owns the Lean/Mathlib toolchain",
            "toolchain, downloads,",
            "compilation, tests, and verification",
            "runner-injected, session-scoped capability",
            "mandatory early Judge checkpoint",
            "Do not wait for a polished proof",
            "Never invoke local `lean`, `lake`, `elan`",
            "Do not install or download Lean, Mathlib",
            "start background, detached, or",
            "parallel processes",
            "Never call a raw Judge or evaluator HTTP endpoint",
            "Never fall back to",
            "Independent construction does not prohibit Lean syntax",
            "bounded declaration search such as `find`, `exact`, or `apply`",
            "Do not inspect unrelated files",
        )
        for name, prompt in self._all_solver_prompts().items():
            with self.subTest(prompt=name):
                self.assertIn(SOLVER_EXECUTION_CONTRACT, prompt)
                for phrase in required_phrases:
                    self.assertIn(phrase, prompt)
                self.assertIsNone(re.search(r"https?://", prompt))

    def test_cps_early_judge_checkpoint_precedes_communication(self) -> None:
        prompt = self._all_solver_prompts()["cps"]
        self.assertLess(
            prompt.index("mandatory early Judge checkpoint"),
            prompt.index("Before trying a route, use `cps_search`"),
        )

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
            marker = WORK_MODE_HEADING.search(source)
            with self.subTest(problem=path.parent.name):
                self.assertIsNotNone(marker)
                assert marker is not None
                indent = marker.group("indent")
                after = source[marker.end() :]
                next_heading = re.search(
                    rf"(?m)^{re.escape(indent)}## [^#]",
                    after,
                )
                actual = (
                    after[: next_heading.start()].rstrip("\n")
                    if next_heading
                    else after.rstrip("\n")
                )
                self.assertEqual(actual, render_problem_work_mode(indent=indent))

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
