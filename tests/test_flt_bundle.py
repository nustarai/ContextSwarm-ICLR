from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "benchmarks" / "fermat_last_theorem"


class FltBundleTests(unittest.TestCase):
    def test_statement_only_bundle_is_pinned_and_has_one_task(self) -> None:
        manifest = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["dataset"], "fermat_last_theorem")
        self.assertEqual(manifest["lean_env_id"], "formal_flt")
        self.assertEqual(manifest["verification_profile"], "formal_flt")
        self.assertEqual(manifest["task_count"], 1)
        self.assertEqual(json.loads((BUNDLE / "problem_ids.json").read_text()), ["fermat_last_theorem"])

        task = BUNDLE / "fermat_last_theorem"
        metadata = json.loads((task / "metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(metadata["strict_from_scratch"])
        self.assertEqual(metadata["imports"], ["Mathlib"])
        self.assertEqual(metadata["theorem_name"], "fermat_last_theorem")

        baseline = (task / "baseline" / "fermat_last_theorem.lean").read_text(encoding="utf-8")
        self.assertIn("theorem fermat_last_theorem", baseline)
        self.assertIn("sorry", baseline)
        self.assertNotIn("Theorems.", baseline)
        self.assertNotIn("P2M.", baseline)
        self.assertNotIn("fermat_last_theorem n hn a b c ha hb hc", baseline)

        integrity = json.loads((BUNDLE / "benchmark_integrity.json").read_text(encoding="utf-8"))
        self.assertEqual(
            integrity["entries"]["fermat_last_theorem"]["baseline_sha256"],
            hashlib.sha256((task / "baseline" / "fermat_last_theorem.lean").read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
