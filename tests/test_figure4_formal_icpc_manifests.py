from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from contextswarm_mini.config import ConfigError, load_config
from contextswarm_mini.runner import (
    _selection_capabilities,
    _selection_comparison_contract_id,
    load_tasks,
    plan,
)


ROOT = Path(__file__).resolve().parents[1]
FORMAL_ROOT = ROOT / "configs" / "figure4_formal_icpc"
POLICIES = ("uniform_refill", "task_state", "trace_state", "llm_scheduler")


class Figure4FormalIcpcManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.configs = {
            policy: load_config(FORMAL_ROOT / f"{policy}.toml", ROOT)
            for policy in POLICIES
        }

    def test_four_leaves_are_formal_and_use_recency_identity(self) -> None:
        identities = set()
        common_contracts = None
        for policy, config in self.configs.items():
            with self.subTest(policy=policy):
                self.assertEqual(config.figure4_phase, "formal")
                self.assertEqual(config.allocation.policy, policy)
                self.assertTrue(config.selection.enabled)
                self.assertTrue(config.selection.identity_frozen)
                self.assertEqual(config.selection.selector_name, "recency")
                self.assertEqual(config.selection.selector_version, "icpc_formal_v1")
                self.assertEqual(config.seed, 1729)
                self.assertEqual(config.selection.seed, 1729)
                self.assertEqual(
                    config.selection.policy_params,
                    {"primary_sort": "commit_seq_desc"},
                )
                self.assertFalse(config.selection.direct_messages)
                self.assertTrue(config.selection.candidate_transfer)
                identities.add(config.selection.selection_config_id)
                # The runner's complete config hash intentionally includes the
                # registered allocation-policy treatment.  Compare the
                # policy-neutral public contract here instead.
                current_contract = config.public_dict()
                current_contract.pop("name")
                current_contract["allocation"] = dict(current_contract["allocation"])
                current_contract["allocation"].pop("policy")
                if common_contracts is None:
                    common_contracts = current_contract
                else:
                    self.assertEqual(current_contract, common_contracts)
        self.assertEqual(len(identities), 1)

    def test_candidate_handoff_is_allowed_only_by_formal_figure4_gate(self) -> None:
        config = self.configs["trace_state"]
        self.assertEqual(_selection_capabilities(config), (True, False, True))

        with self.assertRaisesRegex(ConfigError, "outside formal Figure 4"):
            _selection_capabilities(replace(config, figure4_phase=""))

        unsafe_selection = replace(config.selection, direct_messages=True)
        with self.assertRaisesRegex(ConfigError, "disable direct messages"):
            _selection_capabilities(replace(config, selection=unsafe_selection))

    def test_common_icpc_runtime_boundary_is_identical(self) -> None:
        reference = None
        for policy, config in self.configs.items():
            tasks = load_tasks(config)
            with self.subTest(policy=policy):
                self.assertEqual(config.dataset_name, "icpc_wf_2025")
                self.assertEqual(len(tasks), 12)
                self.assertEqual(config.max_parallel, 24)
                self.assertEqual(config.initial_agents_per_task, 2)
                self.assertEqual(config.time_limit_seconds, 3600)
                self.assertEqual(config.aisw_max_in_flight, 24)
                self.assertEqual(config.judge_kind, "coding")
                self.assertTrue(config.lean_require_result_cache_disabled)
                current = config.public_dict()
                current.pop("name")
                current["allocation"] = dict(current["allocation"])
                current["allocation"].pop("policy")
                current["output_root"] = "runs/figure4_formal_icpc"
                if reference is None:
                    reference = current
                else:
                    self.assertEqual(current, reference)

    def test_plan_exposes_formal_phase_and_candidate_handoff(self) -> None:
        for policy, config in self.configs.items():
            with self.subTest(policy=policy):
                payload = plan(config, load_tasks(config))
                self.assertEqual(payload["figure4_phase"], "formal")
                self.assertTrue(payload["selection"]["candidate_transfer"])
                self.assertEqual(payload["allocation"]["policy"], policy)


if __name__ == "__main__":
    unittest.main()
