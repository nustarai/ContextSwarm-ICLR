from __future__ import annotations

from pathlib import Path
import tomllib
import unittest

from contextswarm_mini.config import load_config
from contextswarm_mini.selection import build_selector
from contextswarm_mini.runner import (
    _selection_comparison_contract_id,
    load_tasks,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = ROOT / "configs" / "figure3_dev"
ARMS = (
    "random",
    "recency",
    "bm25_mmr",
    "smoothed_popularity",
    "feedback_diversity",
    "no_interaction_feedback",
    "unnormalized_feedback",
    "nustigmergy",
)
CANONICAL_FEEDBACK_KINDS = {
    "useful",
    "not_useful",
    "misleading",
    "stale",
    "unsafe",
    "duplicate",
    "diagnostic_useful",
    "needs_refinement",
    "not_used",
    "route_attempted",
    "route_improving",
}


def _path(arm: str) -> Path:
    return CONFIG_ROOT / f"{arm}.toml"


class Figure3DevelopmentManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.configs = {arm: load_config(_path(arm), ROOT) for arm in ARMS}

    def test_exactly_eight_runnable_leaf_arms_exist(self) -> None:
        actual = {
            path.stem
            for path in CONFIG_ROOT.glob("*.toml")
            if not path.name.startswith("_")
        }
        self.assertEqual(actual, set(ARMS))
        self.assertEqual(
            {config.selection.selector_name for config in self.configs.values()},
            set(ARMS),
        )

    def test_leaf_manifests_only_override_arm_identity_output_and_policy(self) -> None:
        for arm in ARMS:
            with self.subTest(arm=arm):
                payload = tomllib.loads(_path(arm).read_text(encoding="utf-8"))
                self.assertEqual(set(payload), {"extends", "experiment", "selection"})
                self.assertEqual(payload["extends"], ["_base.toml"])
                self.assertEqual(
                    set(payload["experiment"]), {"name", "output_root"}
                )
                self.assertEqual(
                    set(payload["selection"]), {"selector_name", "policy_params"}
                )
                self.assertEqual(payload["selection"]["selector_name"], arm)

    def test_all_arm_invariant_inputs_and_paired_contract_ids_match(self) -> None:
        reference = None
        contract_ids = set()
        task_orders = set()
        for arm, config in self.configs.items():
            with self.subTest(arm=arm):
                public = config.public_dict()
                public.pop("name")
                public["selection"] = config.selection.comparison_hash_inputs()
                if reference is None:
                    reference = public
                else:
                    self.assertEqual(public, reference)
                contract_ids.add(_selection_comparison_contract_id(config))
                task_orders.add(tuple(task.slug for task in load_tasks(config)))

                self.assertEqual(config.dataset_name, "matholympiadbench")
                self.assertEqual(config.mode, "cps")
                self.assertEqual(config.communication, "blackboard")
                self.assertEqual(config.time_limit_seconds, 300)
                self.assertEqual(config.max_parallel, 24)
                self.assertEqual(config.aisw_max_in_flight, 24)
                self.assertEqual(config.selection.trace_slot_limit, 8)
                self.assertEqual(config.selection.context_token_budget, 4096)
                self.assertEqual(config.selection.visibility, "project_shared")
                self.assertFalse(config.selection.direct_messages)
                self.assertFalse(config.selection.candidate_transfer)
                self.assertEqual(
                    config.output_root,
                    Path("runs/figure3_dev/matholympiadbench") / arm,
                )
        self.assertEqual(len(contract_ids), 1)
        self.assertEqual(len(task_orders), 1)
        self.assertEqual(len(next(iter(task_orders))), 12)

    def test_every_policy_parameter_is_explicit_and_nu_ablations_are_matched(self) -> None:
        random = self.configs["random"].selection.policy_params
        recency = self.configs["recency"].selection.policy_params
        bm25 = self.configs["bm25_mmr"].selection.policy_params
        popularity = self.configs["smoothed_popularity"].selection.policy_params
        self.assertEqual(random, {"sample_without_replacement": True})
        self.assertEqual(recency, {"primary_sort": "commit_seq_desc"})
        self.assertEqual(
            set(bm25),
            {
                "tokenizer_pattern", "lowercase", "unicode_normalization",
                "fields", "bm25_k1", "bm25_b", "bm25_idf",
                "candidate_depth", "mmr_lambda", "similarity",
                "similarity_idf", "similarity_field_handling",
                "score_precision", "tie_break",
            },
        )
        self.assertEqual(
            set(popularity),
            {
                "alpha", "beta", "positive_kinds", "negative_kinds",
                "score_precision", "tie_break", "feedback_values",
            },
        )

        feedback_arms = (
            "feedback_diversity",
            "no_interaction_feedback",
            "unnormalized_feedback",
            "nustigmergy",
        )
        for arm in feedback_arms:
            with self.subTest(arm=arm):
                params = self.configs[arm].selection.policy_params
                self.assertEqual(
                    set(params["feedback_values"]), CANONICAL_FEEDBACK_KINDS
                )
                self.assertEqual(
                    set(params["weights"]),
                    {"relevance", "evidence", "interaction", "structure", "state"},
                )
                for key in (
                    "interaction_mode", "kappa", "quota", "exploration",
                    "score_precision", "tie_break",
                ):
                    self.assertIn(key, params)

        profiles = {
            arm: dict(self.configs[arm].selection.policy_params)
            for arm in (
                "no_interaction_feedback",
                "unnormalized_feedback",
                "nustigmergy",
            )
        }
        modes = {arm: profile.pop("interaction_mode") for arm, profile in profiles.items()}
        self.assertEqual(
            modes,
            {
                "no_interaction_feedback": "none",
                "unnormalized_feedback": "unnormalized",
                "nustigmergy": "nu",
            },
        )
        self.assertEqual(len({repr(profile) for profile in profiles.values()}), 1)

        for arm, config in self.configs.items():
            with self.subTest(build_selector=arm):
                build_selector(
                    config.selection.selector_name,
                    {"parameters": config.selection.policy_params},
                )

    def test_development_matrix_is_explicitly_blocked_on_uniform_refill(self) -> None:
        base_text = (CONFIG_ROOT / "_base.toml").read_text(encoding="utf-8")
        documentation = (ROOT / "docs" / "figure3_selection_contract.md").read_text(
            encoding="utf-8"
        )
        for config in self.configs.values():
            self.assertEqual(config.allocation.policy, "uniform")
            readiness = config.extra["raw"]["experiment"]["figure3_readiness"]
            self.assertEqual(
                readiness, "dev_mock_blocked_on_issue39_uniform_refill"
            )
        self.assertIn('policy = "uniform"', base_text)
        self.assertIn("NOT the Uniform Refill", base_text)
        self.assertIn("must not be used to report formal Figure 3", documentation)
        self.assertIn("uniform_refill", documentation)

    def test_tracked_matrix_has_no_private_endpoint_or_credential(self) -> None:
        for path in CONFIG_ROOT.glob("*.toml"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(
                    text,
                    r"(?i)(https?://|api[_-]?key|bearer\s|private[_-]?endpoint)",
                )


if __name__ == "__main__":
    unittest.main()
