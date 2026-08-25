from __future__ import annotations

import importlib.util
import json
from collections import deque
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from contextswarm_mini.formal_matrix_artifacts import (
    REQUIRED_LIFECYCLE_EVENTS,
    artifact_eligibility,
    is_recovered_transport_event,
)
from contextswarm_mini.provider_diagnostics import provider_diagnostic_class

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
    def test_formal_wave_stops_after_bounded_provider_burst(self) -> None:
        """A formal-sized wave must stop, not endlessly refill, on overload."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "runs"
            state = Path(raw) / "state.json"
            logs = Path(raw) / "logs"
            launches: list[tuple[object, object]] = []
            stopped: list[object] = []

            class FakeProcess:
                def __init__(self, pid: int) -> None:
                    self.pid = pid
                    self.returncode: int | None = None

                def poll(self) -> int | None:
                    return self.returncode

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    self.returncode = -15
                    return self.returncode

            def fake_launch(slot, *, repo, root, urls, caches, log_root):  # type: ignore[no-untyped-def]
                del repo, urls, caches, log_root
                index = len(launches)
                run = (
                    root
                    / slot.dataset
                    / f"repeat-{slot.repeat:02d}"
                    / slot.policy
                    / f"fake-{index:02d}"
                )
                run.mkdir(parents=True)
                if index == 0:
                    row = {
                        "event": "agent_finished",
                        "returncode": 0,
                        "settled": True,
                        "assistant_success": True,
                        "timed_out": False,
                        "cancelled": False,
                        "transport_diagnostic": True,
                        "transport_recovered": True,
                        "error_tail": "Codex error: Our servers are currently overloaded",
                    }
                elif index in {1, 2, 3}:
                    row = {
                        "event": "agent_finished",
                        "returncode": 1,
                        "settled": True,
                        "assistant_success": False,
                        "timed_out": False,
                        "cancelled": False,
                        "error_tail": "Judge candidate failed: VERIFY_FAIL",
                    }
                else:
                    row = {
                        "event": "agent_finished",
                        "returncode": 1,
                        "settled": True,
                        "assistant_success": False,
                        "timed_out": False,
                        "cancelled": False,
                        "error_tail": (
                            "Codex error: Our servers are currently overloaded. "
                            "Please try again later."
                        ),
                    }
                (run / "events.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
                process = FakeProcess(10_000 + index)
                slot.pid = process.pid
                slot.status = "starting"
                launches.append((slot, process))
                return process

            def fake_stop(process) -> None:  # type: ignore[no-untyped-def]
                stopped.append(process)
                process.returncode = -15

            with (
                patch.object(RUNNER, "_health_ok", return_value=True),
                patch.object(RUNNER, "_launch", side_effect=fake_launch),
                patch.object(RUNNER, "_terminate_process", side_effect=fake_stop),
            ):
                returncode = RUNNER.run(
                    Path(raw),
                    state_path=state,
                    log_root=logs,
                    root=root,
                    max_attempts=99,
                    retry_seconds=1.0,
                    max_infrastructure_failures=3,
                )

            # 6 datasets × 4 policies are admitted once, then the provider
            # breaker stops the wave.  No slot gets a second launch.
            self.assertEqual(returncode, 4)
            self.assertEqual(len(launches), 24)
            self.assertEqual(len(stopped), 24)
            self.assertEqual(len({process.pid for _slot, process in launches}), 24)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["event"], "stopped_for_provider_diagnosis")
            self.assertTrue(
                all(
                    row["last_reason"] == "provider_infrastructure_burst"
                    for row in payload["slots"]
                )
            )

    def test_formal_wave_refills_only_affected_slot_after_provider_recovery(self) -> None:
        """An isolated provider exit must not discard the other 23 arms."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "runs"
            state = Path(raw) / "state.json"
            logs = Path(raw) / "logs"
            launches: list[tuple[object, object]] = []
            counts: dict[tuple[str, int, str], int] = {}

            class FakeProcess:
                def __init__(self, pid: int, returncode: int) -> None:
                    self.pid = pid
                    self.returncode = returncode

                def poll(self) -> int | None:
                    return self.returncode

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return self.returncode

            def write_valid_artifact(run: Path, slot, run_id: str) -> None:  # type: ignore[no-untyped-def]
                run.mkdir(parents=True)
                (run / "final.json").write_text(
                    json.dumps({"status": "COMPLETED", "health": {"ok": True, "issues": []}}),
                    encoding="utf-8",
                )
                (run / "run_meta.json").write_text(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "dataset": slot.dataset,
                            "horizon_started_at": "2026-08-25T00:00:00+00:00",
                            "runtime_provenance": {
                                "image_id": "image",
                                "manifest_sha256": "manifest",
                                "source_commit": "commit",
                            },
                            "allocation": {"policy": slot.policy},
                        }
                    ),
                    encoding="utf-8",
                )
                (run / "figure4_run_summary.json").write_text(
                    json.dumps({"policy": slot.policy}), encoding="utf-8"
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
                lifecycle = (
                    "run_started",
                    "horizon_started",
                    "closeout_finished",
                    "judge_broker_closed",
                    "selection_runtime_closed",
                    "run_finished",
                )
                rows = [{"event": event} for event in lifecycle]
                rows.append(
                    {
                        "event": "agent_finished",
                        "returncode": 0,
                        "settled": True,
                        "assistant_success": True,
                        "timed_out": False,
                        "cancelled": False,
                    }
                )
                (run / "events.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
                )

            def fake_launch(slot, *, repo, root, urls, caches, log_root):  # type: ignore[no-untyped-def]
                del repo, urls, caches, log_root
                key = (slot.dataset, int(slot.repeat), slot.policy)
                attempt = counts.get(key, 0) + 1
                counts[key] = attempt
                index = len(launches)
                run = (
                    root
                    / slot.dataset
                    / f"repeat-{slot.repeat:02d}"
                    / slot.policy
                    / f"fake-{index:02d}"
                )
                if index == 0 and attempt == 1:
                    # This is the only failed arm.  Its event is visible to
                    # the provider breaker but remains below the burst limit.
                    run.mkdir(parents=True)
                    (run / "events.jsonl").write_text(
                        json.dumps(
                            {
                                "event": "agent_finished",
                                "returncode": 1,
                                "settled": True,
                                "assistant_success": False,
                                "timed_out": False,
                                "cancelled": False,
                                "error_tail": (
                                    "Codex error: Our servers are currently overloaded. "
                                    "Please try again later."
                                ),
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    returncode = 1
                else:
                    write_valid_artifact(run, slot, f"valid-{index:02d}")
                    returncode = 0
                process = FakeProcess(20_000 + index, returncode)
                slot.pid = process.pid
                slot.status = "starting"
                launches.append((slot, process))
                return process

            original_slots_for_wave = RUNNER._slots_for_wave
            with (
                patch.object(RUNNER, "_health_ok", return_value=True),
                patch.object(RUNNER, "_launch", side_effect=fake_launch),
                patch.object(RUNNER, "_slots_for_wave", side_effect=lambda repeat: (
                    original_slots_for_wave(repeat) if repeat == 1 else []
                )),
            ):
                returncode = RUNNER.run(
                    Path(raw),
                    state_path=state,
                    log_root=logs,
                    root=root,
                    max_attempts=3,
                    retry_seconds=0.0,
                    max_infrastructure_failures=3,
                )

            self.assertEqual(returncode, 0)
            self.assertEqual(len(launches), 25)
            self.assertEqual(sorted(counts.values()).count(2), 1)
            self.assertEqual(sorted(counts.values()).count(1), 23)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["event"], "all_repeats_finished")
            self.assertTrue(all(row["status"] == "finished" for row in payload["slots"]))

    def test_provider_overload_classifier_is_contextual(self) -> None:
        self.assertEqual(
            provider_diagnostic_class(
                "Codex error: Our servers are currently overloaded. Please try again later."
            ),
            "provider_overload",
        )
        self.assertEqual(
            provider_diagnostic_class("An error occurred while processing your request..."),
            "provider_overload",
        )
        self.assertIsNone(provider_diagnostic_class("Judge candidate failed: VERIFY_FAIL"))
        self.assertIsNone(provider_diagnostic_class("candidate prose: please try again later"))

    def test_provider_burst_ignores_recovered_rows_and_candidate_failures(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            slot = RUNNER.Slot(dataset="clever", repeat=1, policy="uniform_refill")
            run = root / "clever" / "repeat-01" / "uniform_refill" / "run-1"
            run.mkdir(parents=True)
            recovered = {
                "event": "agent_finished",
                "returncode": 0,
                "settled": True,
                "assistant_success": True,
                "timed_out": False,
                "cancelled": False,
                "transport_diagnostic": True,
                "transport_recovered": True,
                "error_tail": "Codex error: Our servers are currently overloaded",
            }
            candidate = {
                "event": "agent_finished",
                "returncode": 1,
                "settled": True,
                "assistant_success": False,
                "timed_out": False,
                "cancelled": False,
                "error_tail": "Judge candidate failed: VERIFY_FAIL",
            }
            overload = {
                "event": "agent_finished",
                "returncode": 1,
                "settled": True,
                "assistant_success": False,
                "timed_out": False,
                "cancelled": False,
                "error_tail": "Codex error: Our servers are currently overloaded. Please try again later.",
            }
            rows = [recovered, candidate] + [overload] * RUNNER.INFRA_ERROR_LIMIT
            (run / "events.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            offsets: dict[str, int] = {}
            recent: deque[tuple[float, str, str]] = deque()
            # One recovered row and one ordinary candidate outcome do not
            # count; the remaining overload rows reach the bounded threshold.
            burst = RUNNER._provider_infra_burst(root, [slot], offsets, recent, now=100.0)
            self.assertIsNotNone(burst)
            assert burst is not None
            self.assertEqual((burst[1], burst[2]), ("provider_overload", RUNNER.INFRA_ERROR_LIMIT))

            # The append cursor makes each poll idempotent: old rows are not
            # counted a second time (the still-open window may, correctly,
            # continue reporting the already-open breaker).
            repeated = RUNNER._provider_infra_burst(root, [slot], offsets, recent, now=101.0)
            self.assertIsNotNone(repeated)
            assert repeated is not None
            self.assertEqual(repeated[2], RUNNER.INFRA_ERROR_LIMIT)

    def test_cancelled_overload_is_still_candidate_independent_evidence(self) -> None:
        row = {
            "event": "agent_finished",
            "returncode": -15,
            "cancelled": True,
            "error_tail": "Codex error: Our servers are currently overloaded",
        }
        self.assertEqual(RUNNER._provider_infra_class(row["error_tail"]), "provider_overload")

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
                "error_tail": "message_end: Codex error: Our servers are currently overloaded",
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
