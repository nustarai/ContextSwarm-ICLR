from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contextswarm_mini.allocation_core import ReadOnlyLLMSchedulerPolicy
from contextswarm_mini.config import ConfigError, load_config
from contextswarm_mini.runner import run_experiment


ROOT = Path(__file__).resolve().parents[1]


class PromptConfigTests(unittest.TestCase):
    def _load(self, allocation: str = ""):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prompt.toml"
            path.write_text(
                f'extends = ["{ROOT / "configs" / "smoke.toml"}"]\n'
                "\n[experiment]\nmode = \"cps\"\ncommunication = \"blackboard\"\n"
                "max_parallel = 1\ninitial_agents_per_task = 1\nmax_tasks = 1\n"
                "episodes_per_task = 1\ntime_limit_seconds = 1\n"
                "\n[allocation]\npolicy = \"llm_scheduler\"\n"
                + allocation,
                encoding="utf-8",
            )
            return load_config(path, ROOT)

    def test_prompt_bounds_are_manifest_owned_and_public(self) -> None:
        config = self._load(
            "prompt_max_bytes = 4096\nprompt_max_tokens = 2048\n"
        )
        self.assertEqual(config.allocation.prompt_max_bytes, 4096)
        self.assertEqual(config.allocation.prompt_max_tokens, 2048)
        public = config.public_dict()["allocation"]
        self.assertEqual(public["prompt_max_bytes"], 4096)
        self.assertEqual(public["prompt_max_tokens"], 2048)

    def test_prompt_bounds_have_safe_defaults(self) -> None:
        config = self._load()
        self.assertEqual(config.allocation.prompt_max_bytes, 64 * 1024)
        self.assertEqual(config.allocation.prompt_max_tokens, 64 * 1024)

    def test_prompt_bounds_must_be_positive(self) -> None:
        for key in ("prompt_max_bytes", "prompt_max_tokens"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigError, "must be positive"):
                    self._load(f"{key} = 0\n")

    def test_runner_passes_manifest_bounds_to_llm_policy(self) -> None:
        config = self._load(
            "prompt_max_bytes = 4096\nprompt_max_tokens = 2048\n"
        )
        captured: list[ReadOnlyLLMSchedulerPolicy] = []
        original = ReadOnlyLLMSchedulerPolicy.__init__

        def observe(policy, invoke, fallback_policy=None, **kwargs):
            original(policy, invoke, fallback_policy, **kwargs)
            captured.append(policy)

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(ReadOnlyLLMSchedulerPolicy, "__init__", observe):
                run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )
        self.assertTrue(captured)
        self.assertEqual(captured[0].prompt_max_bytes, 4096)
        self.assertEqual(captured[0].prompt_max_tokens, 2048)


if __name__ == "__main__":
    unittest.main()
