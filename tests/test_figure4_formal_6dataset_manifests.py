from __future__ import annotations

from pathlib import Path
import unittest

from contextswarm_mini.config import load_config
from contextswarm_mini.runner import load_tasks


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "configs" / "figure4_formal_6datasets"
DATASETS = ("clever", "icpc_wf_2025", "matholympiadbench", "putnambench", "usaco", "verina")
POLICIES = ("uniform_refill", "task_state", "trace_state", "llm_scheduler")
SEEDS = {1: 1729, 2: 1730, 3: 1731}
SELECTOR_ID = "5dae09c95a6a15c9744a884e495d64fb3f1c1207d5be64f58e282b2e0d5eae0a"


class Figure4FormalSixDatasetManifestTests(unittest.TestCase):
    def test_matrix_has_exactly_72_generated_leaves(self) -> None:
        leaves = sorted(
            path
            for path in MATRIX.glob("*/repeat[123]/*.toml")
            if path.name in {f"{policy}.toml" for policy in POLICIES}
        )
        self.assertEqual(len(leaves), 6 * 3 * 4)

    def test_each_dataset_repeat_block_is_policy_matched(self) -> None:
        for dataset in DATASETS:
            for repeat, seed in SEEDS.items():
                configs = {
                    policy: load_config(
                        MATRIX / dataset / f"repeat{repeat}" / f"{policy}.toml",
                        ROOT,
                    )
                    for policy in POLICIES
                }
                reference = configs[POLICIES[0]]
                self.assertEqual(reference.dataset_name, dataset)
                self.assertEqual(len(load_tasks(reference)), 12)
                self.assertEqual(reference.figure4_phase, "formal")
                self.assertEqual(reference.seed, seed)
                self.assertEqual(reference.selection.seed, 1729)
                self.assertEqual(reference.selection.selection_config_id, SELECTOR_ID)
                self.assertTrue(reference.selection.identity_frozen)
                self.assertTrue(reference.selection.candidate_transfer)
                self.assertFalse(reference.selection.direct_messages)
                self.assertEqual(reference.max_parallel, 24)
                self.assertEqual(reference.aisw_max_in_flight, 24)
                self.assertEqual(reference.initial_agents_per_task, 2)
                self.assertEqual(reference.time_limit_seconds, 3600)
                self.assertEqual(reference.communication, "blackboard")
                for policy, config in configs.items():
                    self.assertEqual(config.allocation.policy, policy)
                    self.assertEqual(config.selection.selection_config_id, SELECTOR_ID)
                    current = config.public_dict()
                    expected = reference.public_dict()
                    current.pop("name")
                    expected.pop("name")
                    current.pop("output_root", None)
                    expected.pop("output_root", None)
                    current["allocation"] = dict(current["allocation"])
                    expected["allocation"] = dict(expected["allocation"])
                    current["allocation"].pop("policy")
                    expected["allocation"].pop("policy")
                    self.assertEqual(current, expected, (dataset, repeat, policy))

    def test_coding_and_formal_judge_contracts_are_explicit(self) -> None:
        for dataset in DATASETS:
            config = load_config(
                MATRIX / dataset / "repeat1" / "uniform_refill.toml",
                ROOT,
            )
            if dataset in {"icpc_wf_2025", "usaco"}:
                self.assertEqual(config.judge_kind, "coding")
                self.assertFalse(config.formal_tools_enabled)
            else:
                self.assertEqual(config.judge_kind, "formal")
                self.assertTrue(config.formal_tools_enabled)
                self.assertTrue(config.formal_tools_require_decl_index)
            self.assertTrue(config.lean_require_result_cache_disabled)


if __name__ == "__main__":
    unittest.main()
