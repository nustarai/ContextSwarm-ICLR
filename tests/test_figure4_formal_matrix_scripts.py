from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from contextswarm_mini.formal_matrix_artifacts import (
    REQUIRED_LIFECYCLE_EVENTS,
    artifact_eligibility,
    is_recovered_transport_event,
)

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
            errored = base / "errored"
            completed.mkdir(parents=True)
            failed.mkdir()
            errored.mkdir()
            for directory, run_id, horizon, status in (
                (completed, "valid-run", "2026-08-23T00:00:00+00:00", "COMPLETED"),
                (failed, "preflight-run", None, "PREFLIGHT_FAILED"),
                (errored, "error-run", "2026-08-23T00:00:00+00:00", "ERROR"),
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
                    json.dumps(
                        {
                            "status": status,
                            "health": {"ok": status == "COMPLETED", "issues": []},
                        }
                    ),
                    encoding="utf-8",
                )
                if status == "COMPLETED":
                    (directory / "judge_broker_closeout.json").write_text(
                        json.dumps(
                            {
                                "drained": True,
                                "active_handlers": 0,
                                "fifo_depth": 0,
                                "remote_unsettled_jobs": 0,
                            }
                        ),
                        encoding="utf-8",
                    )
                    (directory / "transport_preflight.json").write_text(
                        json.dumps({"status": "ok", "aisw": {"nurouter_version": "test"}}),
                        encoding="utf-8",
                    )
                    (directory / "events.jsonl").write_text(
                        "".join(json.dumps({"event": event}) + "\n" for event in REQUIRED_LIFECYCLE_EVENTS),
                        encoding="utf-8",
                    )
                    meta = json.loads((directory / "run_meta.json").read_text(encoding="utf-8"))
                    meta["runtime_provenance"] = {
                        "image_id": "image",
                        "manifest_sha256": "manifest",
                        "source_commit": "commit",
                    }
                    (directory / "run_meta.json").write_text(json.dumps(meta), encoding="utf-8")
            # Candidate ordering is mtime-based; make the invalid diagnostic
            # summary newest to exercise the observed formal-run failure mode.
            failed_summary = failed / "figure4_run_summary.json"
            failed_summary.touch()
            (errored / "figure4_run_summary.json").touch()
            path, run_id = COLLECTOR._latest_summary(
                root, "clever", 1, "uniform_refill"
            )
            self.assertEqual(path, completed / "figure4_run_summary.json")
            self.assertEqual(run_id, "valid-run")

    def test_degraded_terminal_artifact_is_not_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            (run / "final.json").write_text(
                json.dumps({"status": "DEGRADED", "health": {"ok": False}}),
                encoding="utf-8",
            )
            eligible, reasons = artifact_eligibility(run)
            self.assertFalse(eligible)
            self.assertIn("final_status:DEGRADED", reasons)

    def test_recovered_transport_diagnostic_is_not_an_experiment_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            (run / "final.json").write_text(
                json.dumps({"status": "COMPLETED", "health": {"ok": True, "issues": []}}),
                encoding="utf-8",
            )
            (run / "run_meta.json").write_text(
                json.dumps(
                    {
                        "run_id": "recovered-run",
                        "horizon_started_at": "2026-08-23T00:00:00+00:00",
                        "runtime_provenance": {
                            "image_id": "image",
                            "manifest_sha256": "manifest",
                            "source_commit": "commit",
                        },
                        "allocation": {"policy": "uniform_refill"},
                    }
                ),
                encoding="utf-8",
            )
            (run / "figure4_run_summary.json").write_text(
                json.dumps({"policy": "uniform_refill"}), encoding="utf-8"
            )
            (run / "judge_broker_closeout.json").write_text(
                json.dumps(
                    {
                        "drained": True,
                        "active_handlers": 0,
                        "fifo_depth": 0,
                        "remote_unsettled_jobs": 0,
                    }
                ),
                encoding="utf-8",
            )
            (run / "transport_preflight.json").write_text(
                json.dumps({"status": "ok", "aisw": {"nurouter_version": "test"}}),
                encoding="utf-8",
            )
            recovered = {
                "event": "agent_finished",
                "returncode": 0,
                "settled": True,
                "assistant_success": True,
                "timed_out": False,
                "cancelled": False,
                "transport_diagnostic": True,
                "transport_recovered": True,
                "error_tail": "message_end: upstream request failed",
            }
            self.assertTrue(is_recovered_transport_event(recovered))
            (run / "events.jsonl").write_text(
                "".join(
                    json.dumps({"event": event}) + "\n"
                    for event in REQUIRED_LIFECYCLE_EVENTS
                )
                + json.dumps(recovered)
                + "\n",
                encoding="utf-8",
            )
            eligible, reasons = artifact_eligibility(run, policy="uniform_refill")
            self.assertTrue(eligible, reasons)

            # The same diagnostic is a real artifact error when settlement or
            # the final assistant outcome is absent/failed.
            unrecovered = dict(recovered)
            unrecovered["transport_recovered"] = False
            unrecovered["assistant_success"] = False
            (run / "events.jsonl").write_text(
                "".join(
                    json.dumps({"event": event}) + "\n"
                    for event in REQUIRED_LIFECYCLE_EVENTS
                )
                + json.dumps(unrecovered)
                + "\n",
                encoding="utf-8",
            )
            eligible, reasons = artifact_eligibility(run, policy="uniform_refill")
            self.assertFalse(eligible)
            self.assertTrue(any(reason.startswith("event_error:error_tail:") for reason in reasons))


if __name__ == "__main__":
    unittest.main()
