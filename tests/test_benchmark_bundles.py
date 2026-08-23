from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = ROOT / "benchmarks"
SOURCE_REVISION = "cfed6e508193a0eeebdc56e1e8846f70a0ecc635"
FORMAL_BUNDLES = {
    "putnambench": "formal_putnambench",
    "matholympiadbench": "formal_matholympiadbench",
    "clever": "formal_clever",
    "verina": "formal_verina",
}


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class BenchmarkBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = _load_json(BENCHMARK_ROOT / "catalog.json")
        cls.entries = {
            entry["dataset"]: entry for entry in cls.catalog["bundles"]
        }

    def test_catalog_pins_the_six_audited_bundles(self) -> None:
        self.assertEqual(
            set(self.entries),
            {
                "usaco",
                "icpc_wf_2025",
                "putnambench",
                "matholympiadbench",
                "clever",
                "verina",
            },
        )
        self.assertEqual(
            self.catalog["contextswarm_source"],
            {
                "repository": "https://github.com/shiyegao/ContextSwarm",
                "revision": SOURCE_REVISION,
            },
        )
        runnable = {
            name
            for name, entry in self.entries.items()
            if entry["runnable_with_contextswarm_mini"]
        }
        self.assertEqual(runnable, {"matholympiadbench"})

    def test_manifests_select_twelve_complete_public_tasks(self) -> None:
        for dataset, catalog_entry in self.entries.items():
            root = BENCHMARK_ROOT / catalog_entry["path"]
            manifest = _load_json(root / "manifest.json")
            problem_ids = _load_json(root / "problem_ids.json")
            with self.subTest(dataset=dataset):
                self.assertEqual(len(problem_ids), 12)
                self.assertEqual(len(set(problem_ids)), 12)
                self.assertEqual(manifest["dataset"], dataset)
                self.assertEqual(manifest["task_count"], len(problem_ids))
                self.assertEqual(
                    manifest["benchmark_revision"],
                    catalog_entry["benchmark_revision"],
                )
                self.assertEqual(
                    manifest["contextswarm_source"]["revision"],
                    SOURCE_REVISION,
                )
                self.assertTrue((root / manifest["integrity_manifest"]).is_file())

            if dataset in FORMAL_BUNDLES:
                self.assertEqual(
                    manifest["lean_env_id"], FORMAL_BUNDLES[dataset]
                )
                for problem_id in problem_ids:
                    task = root / problem_id
                    metadata = _load_json(task / "metadata.json")
                    with self.subTest(dataset=dataset, problem_id=problem_id):
                        self.assertEqual(metadata["slug"], problem_id)
                        self.assertTrue((task / "problem.md").is_file())
                        self.assertEqual(len(list((task / "baseline").glob("*.lean"))), 1)
            elif dataset == "icpc_wf_2025":
                self.assertEqual(manifest["worker_baseline"], "neutral_cpp_skeleton_v1")
                for problem_id in problem_ids:
                    task = root / problem_id
                    with self.subTest(dataset=dataset, problem_id=problem_id):
                        self.assertTrue((task / "problem.md").is_file())
                        self.assertEqual(len(list((task / "baseline").glob("*.cpp"))), 1)

    def test_formal_baselines_match_the_integrity_authority(self) -> None:
        for dataset in FORMAL_BUNDLES:
            root = BENCHMARK_ROOT / dataset
            problem_ids = _load_json(root / "problem_ids.json")
            integrity = _load_json(root / "benchmark_integrity.json")
            self.assertEqual(set(integrity["entries"]), set(problem_ids))
            for problem_id in problem_ids:
                baseline = next((root / problem_id / "baseline").glob("*.lean"))
                actual = hashlib.sha256(baseline.read_bytes()).hexdigest()
                with self.subTest(dataset=dataset, problem_id=problem_id):
                    self.assertEqual(
                        actual,
                        integrity["entries"][problem_id]["baseline_sha256"],
                    )

    def test_usaco_public_projection_matches_the_fixed_manifest(self) -> None:
        root = BENCHMARK_ROOT / "usaco"
        problem_ids = _load_json(root / "problem_ids.json")
        public = _load_json(root / "public_dataset" / "usaco_2025_dict.json")
        self.assertEqual(list(public), problem_ids)
        for problem_id, record in public.items():
            with self.subTest(problem_id=problem_id):
                self.assertEqual(record["problem_id"], problem_id)
                self.assertEqual(record["num_tests"], 0)
                self.assertGreater(record["expected_judge_test_count"], 0)
                self.assertIn("source_provenance", record)
                self.assertIn("benchmark_contract_id", record)
        marker = root / "public_dataset" / "usaco_2025" / (
            ".contextswarm_usaco_public_dataset_v1"
        )
        self.assertIn("hidden tests", marker.read_text(encoding="utf-8"))

    def test_superseded_semantic_contracts_are_absent(self) -> None:
        superseded = {
            "matholympiadbench": {"imo2023_p2"},
            "clever": {
                "cleverbench_18",
                "cleverbench_54",
                "cleverbench_89",
                "cleverbench_102",
            },
        }
        for dataset, problem_ids in superseded.items():
            root = BENCHMARK_ROOT / dataset
            selected = set(_load_json(root / "problem_ids.json"))
            for problem_id in problem_ids:
                with self.subTest(dataset=dataset, problem_id=problem_id):
                    self.assertNotIn(problem_id, selected)
                    self.assertFalse((root / problem_id).exists())

    def test_production_evaluators_are_not_redistributed(self) -> None:
        self.assertEqual(list(BENCHMARK_ROOT.rglob("evaluate.py")), [])

    def test_icpc_worker_bundle_has_statements_but_no_public_solution_seed(self) -> None:
        """The worker-visible ICPC bundle must not replay public AC code."""

        root = BENCHMARK_ROOT / "icpc_wf_2025"
        problem_ids = _load_json(root / "problem_ids.json")
        neutral_hash: str | None = None
        for problem_id in problem_ids:
            task = root / problem_id
            statement = (task / "problem.md").read_text(encoding="utf-8")
            baseline = next((task / "baseline").glob("*.cpp"))
            baseline_text = baseline.read_text(encoding="utf-8")
            with self.subTest(problem_id=problem_id):
                self.assertTrue(statement.startswith("Problem "))
                self.assertNotRegex(statement, r"(?i)https?://|public ac|submission-.*ac")
                self.assertIn("Neutral starting skeleton", baseline_text)
                self.assertNotIn("struct Task", baseline_text)
                digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
                if neutral_hash is None:
                    neutral_hash = digest
                self.assertEqual(digest, neutral_hash)


if __name__ == "__main__":
    unittest.main()
