from __future__ import annotations

from pathlib import Path
import unittest

from contextswarm_mini.config import load_config
from contextswarm_mini.runner import load_tasks, plan


ROOT = Path(__file__).resolve().parents[1]
PAPER_CONFIG_ROOT = ROOT / "configs" / "paper_5min"

DATASETS = {
    "matholympiadbench": {
        "env_id": "formal_matholympiadbench",
        "profile": "formal_proof",
        "kind": "formal",
    },
    "putnambench": {
        "env_id": "formal_putnambench",
        "profile": "formal_proof",
        "kind": "formal",
    },
    "clever": {
        "env_id": "formal_clever",
        "profile": "formal_proof",
        "kind": "formal",
    },
    "verina": {
        "env_id": "formal_verina",
        "profile": "formal_proof",
        "kind": "formal",
    },
    "icpc_wf_2025": {
        "env_id": "",
        "profile": "coding_icpc_contest",
        "kind": "coding",
    },
    "usaco": {
        "env_id": "",
        "profile": "coding_usaco_contest",
        "kind": "coding",
    },
}


def _manifest(dataset: str, mode: str) -> Path:
    return PAPER_CONFIG_ROOT / f"{dataset}_{mode}.toml"


class PaperManifestTests(unittest.TestCase):
    def test_exactly_twelve_paper_cells_are_present(self) -> None:
        expected = {
            _manifest(dataset, mode).name
            for dataset in DATASETS
            for mode in ("mono", "parallel")
        }
        actual = {
            path.name
            for path in PAPER_CONFIG_ROOT.glob("*.toml")
            if not path.name.startswith("_")
        }
        self.assertEqual(actual, expected)

    def test_all_cells_load_twelve_tasks_and_have_mode_specific_sessions(self) -> None:
        seen_outputs: set[Path] = set()
        for dataset, expected in DATASETS.items():
            for mode in ("mono", "parallel"):
                with self.subTest(dataset=dataset, mode=mode):
                    config = load_config(_manifest(dataset, mode), ROOT)
                    tasks = load_tasks(config)
                    session_plan = plan(config, tasks)
                    self.assertEqual(len(tasks), 12)
                    self.assertEqual(config.max_tasks, 12)
                    self.assertEqual(config.time_limit_seconds, 300)
                    self.assertEqual(config.initial_agents_per_task, 1)
                    self.assertEqual(config.max_attempts_per_task, 0)
                    self.assertTrue(config.cancel_on_proved)
                    self.assertEqual(config.pi_timeout_seconds, 300)
                    self.assertEqual(config.model, "openai-codex/gpt-5.6-sol")
                    self.assertEqual(config.thinking, "max")
                    self.assertFalse(config.fast_mode)
                    self.assertEqual(config.communication, "none")
                    self.assertEqual(config.lean_max_concurrent_evaluations, 12)
                    self.assertEqual(config.aisw_max_in_flight, 12)
                    self.assertEqual(config.judge_kind, expected["kind"])
                    self.assertEqual(config.lean_env_id, expected["env_id"])
                    self.assertEqual(
                        config.lean_verification_profile, expected["profile"]
                    )
                    self.assertEqual(config.formal_tools_enabled, expected["kind"] == "formal")
                    self.assertFalse(config.formal_tools_require_decl_index)
                    if mode == "mono":
                        self.assertEqual(config.max_parallel, 1)
                        self.assertEqual(session_plan["planned_agent_sessions"], 1)
                    else:
                        self.assertEqual(config.max_parallel, 12)
                        self.assertEqual(session_plan["planned_agent_sessions"], 12)
                    output = config.resolved_output_root
                    self.assertNotIn(output, seen_outputs)
                    seen_outputs.add(output)

    def test_pairs_only_change_manifest_selected_session_allocation(self) -> None:
        for dataset in DATASETS:
            mono = load_config(_manifest(dataset, "mono"), ROOT)
            parallel = load_config(_manifest(dataset, "parallel"), ROOT)
            with self.subTest(dataset=dataset):
                mono_contract = mono.public_dict()
                parallel_contract = parallel.public_dict()
                for key in ("name", "mode", "max_parallel"):
                    mono_contract.pop(key)
                    parallel_contract.pop(key)
                self.assertEqual(mono_contract, parallel_contract)

    def test_tracked_manifests_do_not_embed_private_capabilities(self) -> None:
        for path in PAPER_CONFIG_ROOT.glob("*.toml"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotRegex(text, r"(?i)(https?://|node\.toml|api[_-]?key|token)")


if __name__ == "__main__":
    unittest.main()
