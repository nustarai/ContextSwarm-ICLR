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
    def _load(self, allocation: str = "", experiment: str = ""):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prompt.toml"
            path.write_text(
                f'extends = ["{ROOT / "configs" / "smoke.toml"}"]\n'
                "\n[experiment]\nmode = \"cps\"\ncommunication = \"blackboard\"\n"
                "max_parallel = 1\ninitial_agents_per_task = 1\nmax_tasks = 1\n"
                "episodes_per_task = 1\ntime_limit_seconds = 1\n"
                + experiment
                + "\n[allocation]\npolicy = \"llm_scheduler\"\n"
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

    def test_negative_piece_prompt_is_explicit_and_public(self) -> None:
        control = self._load()
        self.assertFalse(control.negative_piece_prompt)
        self.assertFalse(control.public_dict()["negative_piece_prompt"])

        treatment = self._load(experiment="negative_piece_prompt = true\n")
        self.assertTrue(treatment.negative_piece_prompt)
        self.assertTrue(treatment.public_dict()["negative_piece_prompt"])

    def test_negative_piece_prompt_must_be_boolean(self) -> None:
        with self.assertRaisesRegex(ConfigError, "experiment.negative_piece_prompt"):
            self._load(experiment="negative_piece_prompt = 1\n")

    def test_paired_negative_piece_manifests_only_change_arm_prompt(self) -> None:
        control = load_config(
            ROOT / "configs" / "formal_1h_cps32_negative_piece_control.toml",
            ROOT,
        )
        treatment = load_config(
            ROOT / "configs" / "formal_1h_cps32_negative_piece_treatment.toml",
            ROOT,
        )
        control_public = control.public_dict()
        treatment_public = treatment.public_dict()
        self.assertFalse(control.negative_piece_prompt)
        self.assertTrue(treatment.negative_piece_prompt)
        self.assertEqual(
            {
                key: value
                for key, value in control_public.items()
                if key not in {"name", "negative_piece_prompt"}
            },
            {
                key: value
                for key, value in treatment_public.items()
                if key not in {"name", "negative_piece_prompt"}
            },
        )

    def test_prompt_bounds_must_be_positive(self) -> None:
        for key in ("prompt_max_bytes", "prompt_max_tokens"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigError, "must be positive"):
                    self._load(f"{key} = 0\n")

    def test_trace_density_components_are_manifest_owned_and_drag_alias_is_canonicalized(self) -> None:
        config = self._load()
        self.assertIn(
            "duplicate_component_weight", config.allocation.trace_state
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy-drag.toml"
            path.write_text(
                f'extends = ["{ROOT / "configs" / "smoke.toml"}"]\n'
                "\n[experiment]\nmode = \"cps\"\ncommunication = \"blackboard\"\n"
                "max_parallel = 1\ninitial_agents_per_task = 1\nmax_tasks = 1\n"
                "episodes_per_task = 1\ntime_limit_seconds = 1\n"
                "\n[allocation]\npolicy = \"trace_state\"\n"
                "[allocation.trace_state]\ndrag = 2.0\n",
                encoding="utf-8",
            )
            legacy = load_config(path, ROOT)
        self.assertEqual(legacy.allocation.trace_state["density_penalty_weight"], 2.0)
        self.assertNotIn("drag", legacy.allocation.trace_state)

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
