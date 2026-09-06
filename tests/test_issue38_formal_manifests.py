from __future__ import annotations

from pathlib import Path
import unittest

from contextswarm_mini.config import load_config
from contextswarm_mini.runner import load_tasks


ROOT = Path(__file__).resolve().parents[1]
MATRIX_ROOT = ROOT / "configs" / "issue38_formal"
DATASETS = {
    "usaco": ("coding", "", "coding_usaco_contest"),
    "putnambench": ("formal", "formal_putnambench", "formal_proof"),
    "matholympiadbench": ("formal", "formal_matholympiadbench", "formal_proof"),
    "clever": ("formal", "formal_clever", "formal_proof"),
    "verina": ("formal", "formal_verina", "formal_proof"),
}
SELECTORS = {
    "bm25_mmr",
    "feedback_diversity",
    "no_interaction_feedback",
    "nustigmergy",
    "random",
    "recency",
    "smoothed_popularity",
    "unnormalized_feedback",
}
REPEATS = {1: 1729, 2: 1730, 3: 1731}


class Issue38FormalManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = []
        for dataset in DATASETS:
            for repeat, seed in REPEATS.items():
                for selector in sorted(SELECTORS):
                    path = MATRIX_ROOT / dataset / f"repeat{repeat}" / f"{selector}.toml"
                    config = load_config(path, ROOT)
                    tasks = load_tasks(config)
                    cls.rows.append((dataset, repeat, seed, selector, config, tasks))

    def test_matrix_has_exactly_120_independent_arms(self) -> None:
        self.assertEqual(len(self.rows), 5 * 3 * 8)
        self.assertEqual(
            {(d, r, s) for d, r, _seed, s, _config, _tasks in self.rows},
            {(d, r, s) for d in DATASETS for r in REPEATS for s in SELECTORS},
        )

    def test_each_arm_has_the_same_policy_neutral_contract(self) -> None:
        reference = {}
        for dataset, repeat, seed, selector, config, tasks in self.rows:
            with self.subTest(dataset=dataset, repeat=repeat, selector=selector):
                kind, env_id, profile = DATASETS[dataset]
                self.assertEqual(len(tasks), 12)
                self.assertEqual(config.dataset_name, dataset)
                self.assertEqual(config.judge_kind, kind)
                self.assertEqual(config.lean_env_id, env_id)
                self.assertEqual(config.lean_verification_profile, profile)
                self.assertEqual(config.mode, "cps")
                self.assertEqual(config.communication, "blackboard")
                self.assertEqual(config.max_parallel, 24)
                self.assertEqual(config.aisw_max_in_flight, 24)
                self.assertEqual(config.initial_agents_per_task, 2)
                self.assertEqual(config.time_limit_seconds, 3600)
                self.assertEqual(config.max_tasks, 12)
                self.assertEqual(config.episodes_per_task, 2)
                self.assertEqual(config.seed, seed)
                self.assertEqual(config.selection.seed, seed)
                self.assertEqual(config.selection.selector_name, selector)
                self.assertEqual(config.selection.selector_version, "issue38_formal_v1")
                self.assertTrue(config.selection.enabled)
                self.assertFalse(config.selection.direct_messages)
                self.assertFalse(config.selection.candidate_transfer)
                self.assertEqual(config.allocation.policy, "uniform_refill")
                self.assertTrue(config.lean_require_result_cache_disabled)
                self.assertEqual(config.lean_max_concurrent_evaluations, 4)
                public = config.public_dict()
                public.pop("name", None)
                public["seed"] = 0
                public["selection"] = dict(public["selection"])
                public["selection"].pop("selector_name")
                public["selection"].pop("policy_params")
                public["selection"].pop("selection_config_id")
                public["selection"]["seed"] = 0
                public["allocation"] = dict(public["allocation"])
                if dataset not in reference:
                    reference[dataset] = public
                else:
                    self.assertEqual(public, reference[dataset])


if __name__ == "__main__":
    unittest.main()
