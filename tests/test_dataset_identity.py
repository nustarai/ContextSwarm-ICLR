from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from contextswarm_mini.cli import main
from contextswarm_mini.config import load_config
from contextswarm_mini.runner import _write_final


ROOT = Path(__file__).resolve().parents[1]


class DatasetIdentityTests(unittest.TestCase):
    def test_validate_reports_selected_bundle_instead_of_legacy_default(self) -> None:
        manifest = ROOT / "configs" / "paper_5min" / "usaco_parallel.toml"
        output = StringIO()
        with redirect_stdout(output):
            status = main(["--config", str(manifest), "validate", "--json"])
        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["dataset"], "usaco")
        self.assertEqual(payload["task_count"], 12)

    def test_final_reports_selected_bundle_instead_of_legacy_default(self) -> None:
        manifest = ROOT / "configs" / "paper_5min" / "icpc_wf_2025_parallel.toml"
        config = load_config(manifest, ROOT)
        with TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            _write_final(
                run_dir,
                config,
                {},
                [],
                status="DRY_RUN",
                cps_summary=None,
            )
            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
        self.assertEqual(final["dataset"], "icpc_wf_2025")
        self.assertEqual(config.public_dict()["dataset"], "icpc_wf_2025")


if __name__ == "__main__":
    unittest.main()
