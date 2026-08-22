from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from contextswarm_mini.cli import main
from contextswarm_mini.config import ExperimentConfig, load_config
from contextswarm_mini.runner import load_tasks, plan


ROOT = Path(__file__).resolve().parents[1]

ARMS = {
    "uniform_refill": ROOT / "configs" / "figure4_dev_cps48_uniform_refill.toml",
    "task_state": ROOT / "configs" / "figure4_dev_cps48_task_state.toml",
    "trace_state": ROOT / "configs" / "figure4_dev_cps48_trace_state.toml",
    "llm_scheduler": ROOT / "configs" / "figure4_dev_cps48_llm_scheduler.toml",
}

ORDERED_TASK_IDS = [
    "imo2024_p1",
    "imo2024_p2",
    "imo2024_p3",
    "imo2024_p5",
    "imo2024_p6",
    "uk2024_r1_p1",
    "uk2024_r1_p2",
    "usa2024_p2",
    "imo2023_p2_v2",
    "imo2023_p3",
    "imo2023_p4",
    "imo2023_p5",
]

# Figure 3 has not selected the formal selector yet.  The development arms use
# this complete disabled-selector identity rather than silently choosing an
# arm-specific default.  Replacing it later must update all four arms together.
DEVELOPMENT_SELECTOR_IDENTITY = {
    "enabled": False,
    "visibility": "project_shared",
    "seed": 39,
    "tie_break": "trace_id_asc",
    "direct_messages": False,
    "candidate_transfer": False,
}


def _raw_manifest(config: ExperimentConfig) -> dict[str, object]:
    raw = config.extra.get("raw")
    if not isinstance(raw, dict):
        raise AssertionError("loaded config did not retain its resolved manifest")
    return copy.deepcopy(raw)


def _strip_registered_arm_differences(
    payload: dict[str, object],
) -> tuple[dict[str, object], str, str, str]:
    experiment = payload.get("experiment")
    allocation = payload.get("allocation")
    if not isinstance(experiment, dict) or not isinstance(allocation, dict):
        raise AssertionError("resolved Figure 4 manifest is missing required tables")
    name = str(experiment.pop("name"))
    output_root = str(experiment.pop("output_root"))
    policy = str(allocation.pop("policy"))
    return payload, name, output_root, policy


def _cli_json(argv: list[str]) -> tuple[int, dict[str, object], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    # Whether a developer happens to export the Judge URL must not alter the
    # checked development plan.
    with patch.dict(os.environ, {"CONTEXTSWARM_JUDGE_URL": ""}), redirect_stdout(
        stdout
    ), redirect_stderr(stderr):
        status = main(argv)
    return status, json.loads(stdout.getvalue()), stderr.getvalue()


class Figure4DevelopmentManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.configs = {
            policy: load_config(manifest, ROOT) for policy, manifest in ARMS.items()
        }
        cls.tasks = {
            policy: load_tasks(config) for policy, config in cls.configs.items()
        }

    def test_exactly_four_registered_development_arms_exist(self) -> None:
        expected = {path.name for path in ARMS.values()}
        actual = {
            path.name
            for path in (ROOT / "configs").glob("figure4_dev_cps48_*.toml")
        }
        self.assertEqual(actual, expected)
        self.assertEqual(
            {config.allocation.policy for config in self.configs.values()},
            set(ARMS),
        )

    def test_resolved_arms_differ_only_by_name_output_and_policy(self) -> None:
        common_raw: dict[str, object] | None = None
        common_public: dict[str, object] | None = None
        seen_names: set[str] = set()
        seen_outputs: set[str] = set()

        for policy, config in self.configs.items():
            with self.subTest(policy=policy):
                raw, name, output_root, selected_policy = (
                    _strip_registered_arm_differences(_raw_manifest(config))
                )
                self.assertEqual(selected_policy, policy)
                self.assertEqual(
                    name,
                    f"mobench-figure4-dev-cps48-{policy.replace('_', '-')}",
                )
                self.assertEqual(output_root, f"runs/figure4_dev/{policy}")
                self.assertNotIn(name, seen_names)
                self.assertNotIn(output_root, seen_outputs)
                seen_names.add(name)
                seen_outputs.add(output_root)

                if common_raw is None:
                    common_raw = raw
                else:
                    self.assertEqual(raw, common_raw)

                public = config.public_dict()
                public.pop("name")
                allocation = public["allocation"]
                self.assertIsInstance(allocation, dict)
                assert isinstance(allocation, dict)
                self.assertEqual(allocation.pop("policy"), policy)
                if common_public is None:
                    common_public = public
                else:
                    self.assertEqual(public, common_public)

    def test_common_comparison_contract_is_frozen(self) -> None:
        for policy, config in self.configs.items():
            tasks = self.tasks[policy]
            task_ids = [task.slug for task in tasks]
            session_plan = plan(config, tasks)
            selection = _raw_manifest(config).get("selection")

            with self.subTest(policy=policy):
                self.assertEqual(selection, DEVELOPMENT_SELECTOR_IDENTITY)
                self.assertEqual(config.mode, "cps")
                self.assertEqual(config.communication, "blackboard")
                self.assertEqual(config.dataset_name, "matholympiadbench")
                self.assertEqual(
                    config.dataset_root, Path("benchmarks/matholympiadbench")
                )
                self.assertEqual(
                    config.problem_ids_path,
                    Path("benchmarks/matholympiadbench/problem_ids.json"),
                )
                self.assertEqual(task_ids, ORDERED_TASK_IDS)
                self.assertEqual(config.max_tasks, 0)

                self.assertEqual(config.max_parallel, 48)
                self.assertEqual(config.initial_agents_per_task, 4)
                self.assertEqual(len(tasks) * config.initial_agents_per_task, 48)
                self.assertEqual(session_plan["planned_agent_sessions"], 48)
                self.assertEqual(config.lean_max_concurrent_evaluations, 4)
                self.assertEqual(config.aisw_max_in_flight, 12)

                self.assertEqual(config.model, "openai-codex/gpt-5.6-sol")
                self.assertEqual(config.thinking, "max")
                self.assertFalse(config.fast_mode)
                self.assertEqual(config.judge_kind, "formal")
                self.assertEqual(config.lean_env_id, "formal_matholympiadbench")
                self.assertEqual(config.lean_verification_profile, "formal_proof")
                self.assertEqual(config.lean_judge_mode, "fast")
                self.assertEqual(config.lean_timeout_seconds, 300)
                self.assertEqual(config.lean_max_lifecycle_seconds, 3600)

                self.assertEqual(config.seed, 39)
                self.assertEqual(config.time_limit_seconds, 180)
                self.assertEqual(config.pi_timeout_seconds, 180)
                self.assertTrue(config.cancel_on_proved)
                self.assertEqual(config.max_attempts_per_task, 0)
                self.assertEqual(config.episodes_per_task, 2)
                self.assertEqual(config.assignment_policy, "round_robin")

    def test_cli_plan_and_validate_all_four_arms(self) -> None:
        common_plan: dict[str, object] | None = None
        common_validation: dict[str, object] | None = None

        for policy, manifest in ARMS.items():
            with self.subTest(policy=policy, command="plan"):
                status, payload, stderr = _cli_json(
                    ["--config", str(manifest), "plan", "--json"]
                )
                self.assertEqual(status, 0, stderr)
                self.assertEqual(stderr, "")
                self.assertEqual(payload["name"], self.configs[policy].name)
                self.assertEqual(payload["tasks"], ORDERED_TASK_IDS)
                self.assertEqual(payload["task_count"], 12)
                self.assertEqual(payload["max_parallel"], 48)
                self.assertEqual(payload["initial_agents_per_task"], 4)
                self.assertEqual(payload["planned_agent_sessions"], 48)
                self.assertEqual(payload["allocation"]["policy"], policy)

                comparable_plan = copy.deepcopy(payload)
                comparable_plan.pop("name")
                comparable_plan["allocation"].pop("policy")
                if common_plan is None:
                    common_plan = comparable_plan
                else:
                    self.assertEqual(comparable_plan, common_plan)

            with self.subTest(policy=policy, command="validate"):
                status, payload, stderr = _cli_json(
                    ["--config", str(manifest), "validate", "--json"]
                )
                self.assertEqual(status, 0, stderr)
                self.assertEqual(stderr, "")
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["dataset"], "matholympiadbench")
                self.assertEqual(payload["tasks"], ORDERED_TASK_IDS)
                self.assertEqual(payload["task_count"], 12)
                self.assertEqual(payload["manifest"], str(manifest.resolve()))

                comparable_validation = copy.deepcopy(payload)
                comparable_validation.pop("manifest")
                if common_validation is None:
                    common_validation = comparable_validation
                else:
                    self.assertEqual(comparable_validation, common_validation)


if __name__ == "__main__":
    unittest.main()
