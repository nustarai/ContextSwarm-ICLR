from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tests.{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load_script("run_figure4_formal_matrix")
COLLECTOR = _load_script("collect_figure4_formal_matrix")


class Figure4FormalMatrixScriptTests(unittest.TestCase):
    def test_health_gate_rejects_transient_or_zero_capacity(self) -> None:
        self.assertFalse(RUNNER._health_ready({"ok": False}))
        self.assertFalse(
            RUNNER._health_ready(
                {
                    "ok": True,
                    "group_admission": {"enabled": True, "status": "ready"},
                    "capacity_state": "DEGRADED",
                    "available_service_units": 0,
                }
            )
        )
        self.assertTrue(
            RUNNER._health_ready(
                {
                    "ok": True,
                    "group_admission": {"enabled": True, "status": "ready"},
                    "capacity_state": "AVAILABLE",
                    "available_service_units": 24,
                }
            )
        )

    def test_health_gate_uses_direct_workers_when_group_is_disabled(self) -> None:
        base = {
            "ok": True,
            "group_admission": {"enabled": False, "status": "disabled"},
            "capacity_error_kind": "admission_disabled",
            "capacity_state": "DEGRADED",
            "available_service_units": 0,
        }
        self.assertFalse(RUNNER._health_ready(base | {"ready_workers": 0, "active_workers": 9}))
        self.assertTrue(RUNNER._health_ready(base | {"ready_workers": 2, "active_workers": 9}))

    def test_collector_ignores_newer_preflight_only_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base = root / "clever" / "repeat-01" / "uniform_refill"
            completed = base / "completed"
            failed = base / "failed"
            completed.mkdir(parents=True)
            failed.mkdir()
            for directory, run_id, horizon, status in (
                (completed, "valid-run", "2026-08-23T00:00:00+00:00", "DEGRADED"),
                (failed, "preflight-run", None, "PREFLIGHT_FAILED"),
            ):
                (directory / "figure4_run_summary.json").write_text(
                    json.dumps({"policy": "uniform_refill", "run_id": run_id}),
                    encoding="utf-8",
                )
                (directory / "run_meta.json").write_text(
                    json.dumps({"run_id": run_id, "horizon_started_at": horizon}),
                    encoding="utf-8",
                )
                (directory / "final.json").write_text(
                    json.dumps({"status": status}),
                    encoding="utf-8",
                )
            # Candidate ordering is mtime-based; make the invalid diagnostic
            # summary newest to exercise the observed formal-run failure mode.
            failed_summary = failed / "figure4_run_summary.json"
            failed_summary.touch()
            path, run_id = COLLECTOR._latest_summary(
                root, "clever", 1, "uniform_refill"
            )
            self.assertEqual(path, completed / "figure4_run_summary.json")
            self.assertEqual(run_id, "valid-run")


if __name__ == "__main__":
    unittest.main()
