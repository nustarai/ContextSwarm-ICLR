from __future__ import annotations

import importlib.util
import json
from collections import deque
import datetime as dt
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
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
    def test_adoption_identity_accepts_real_launcher_shape_without_repo_argv(self) -> None:
        """The persisted Popen PID is checked like an actual run_docker child.

        ``Popen(..., cwd=repo)`` does not put the checkout path in argv.  The
        launcher eventually execs ``docker run`` and retains only the manifest
        and output arguments.  Exercise that exact procfs shape with a real
        child process so adoption cannot regress to an argv-only repository
        check that rejects every live arm.
        """

        slot = RUNNER.Slot(dataset="clever", repeat=1, policy="uniform_refill")
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / "long_lived_child.py"
            helper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(helper),
                    "docker",
                    "run",
                    "scripts/run_docker.sh",
                    "--config",
                    slot.manifest,
                    "--output",
                    slot.output_root,
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                trusted = False
                for _ in range(100):
                    trusted = RUNNER._trusted_child_pid(process.pid, slot, ROOT)
                    if trusted:
                        break
                    time.sleep(0.01)
                self.assertTrue(trusted)
                adopted = RUNNER._adopt_process(process.pid, slot, ROOT)
                self.assertIsNotNone(adopted)
                assert adopted is not None
                self.assertIsNone(adopted.poll())
                self.assertFalse(
                    RUNNER._trusted_child_pid(
                        process.pid,
                        slot,
                        ROOT.parent / "a-different-checkout",
                    )
                )
            finally:
                try:
                    os.kill(process.pid, signal.SIGTERM)
                except OSError:
                    pass
                process.wait(timeout=5)

    def test_resume_discovers_live_horizon_when_state_heartbeat_lagged(self) -> None:
        """A child admitted just before supervisor loss is adopted by scan.

        The state writer and the child write different files.  A crash between
        those writes leaves ``run_meta.json`` with a horizon but no
        ``horizon_run_id`` in ``state.json``.  Re-launching here would create a
        duplicate arm, so exercise the real procfs fallback with a launcher-
        shaped child.
        """

        slot = RUNNER.Slot(dataset="clever", repeat=1, policy="uniform_refill")
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw) / "runs" / slot.dataset / "repeat-01" / slot.policy
            run = base / "lagged-horizon"
            run.mkdir(parents=True)
            started = dt.datetime.now(dt.timezone.utc).isoformat()
            (run / "run_meta.json").write_text(
                json.dumps(
                    {
                        "run_id": "lagged-horizon",
                        "dataset": slot.dataset,
                        "started_at": started,
                        "horizon_started_at": started,
                        "allocation": {"policy": slot.policy},
                    }
                ),
                encoding="utf-8",
            )
            helper = Path(raw) / "long_lived_child.py"
            helper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(helper),
                    "docker",
                    "run",
                    "scripts/run_docker.sh",
                    "--config",
                    slot.manifest,
                    "--output",
                    slot.output_root,
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            adopted: dict[int, object] = {}
            try:
                for _ in range(100):
                    if RUNNER._trusted_child_pid(process.pid, slot, ROOT):
                        break
                    time.sleep(0.01)
                else:
                    self.fail("launcher-shaped child never became discoverable")
                RUNNER._resume_slot(
                    slot,
                    {"pid": process.pid, "attempts": 1, "horizon_run_id": ""},
                    root=Path(raw) / "runs",
                    repo=ROOT,
                    processes=adopted,
                )
                self.assertEqual(slot.status, "running")
                self.assertEqual(slot.pid, process.pid)
                self.assertEqual(slot.horizon_run_id, "lagged-horizon")
                self.assertIn(process.pid, adopted)
            finally:
                try:
                    os.kill(process.pid, signal.SIGTERM)
                except OSError:
                    pass
                process.wait(timeout=5)

    def test_resume_quarantines_pre_admission_child_without_run_metadata(self) -> None:
        """A launcher race before ``run_meta`` cannot create a duplicate slot."""

        slot = RUNNER.Slot(dataset="clever", repeat=1, policy="uniform_refill")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "runs"
            helper = Path(raw) / "long_lived_child.py"
            helper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(helper),
                    "docker",
                    "run",
                    "scripts/run_docker.sh",
                    "--config",
                    slot.manifest,
                    "--output",
                    slot.output_root,
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                for _ in range(100):
                    if RUNNER._trusted_child_pid(process.pid, slot, ROOT):
                        break
                    time.sleep(0.01)
                else:
                    self.fail("launcher-shaped child never became discoverable")
                slot_processes: dict[int, object] = {}
                RUNNER._resume_slot(
                    slot,
                    {"pid": process.pid, "attempts": 1, "horizon_run_id": ""},
                    root=root,
                    repo=ROOT,
                    processes=slot_processes,
                )
                self.assertEqual(slot.status, "pending")
                self.assertEqual(slot.last_reason, "pre_admission_process_terminated")
                self.assertIsNotNone(process.poll())
                for _ in range(100):
                    if process.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(process.poll())
            finally:
                if process.poll() is None:
                    try:
                        os.kill(process.pid, signal.SIGKILL)
                    except OSError:
                        pass
                process.wait(timeout=5)

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
                    # This child crosses the fixed horizon before the burst
                    # is observed.  The supervisor must preserve it for
                    # adoption while stopping only pre-admission children.
                    (run / "run_meta.json").write_text(
                        json.dumps({
                            "run_id": f"horizon-{index:02d}",
                            "dataset": slot.dataset,
                            "horizon_started_at": "2026-08-25T00:00:00+00:00",
                            "allocation": {"policy": slot.policy},
                        }),
                        encoding="utf-8",
                    )
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
            # breaker stops the wave.  The first child has a persisted horizon
            # and remains alive for supervisor adoption; no slot gets a second
            # launch.
            self.assertEqual(returncode, 4)
            self.assertEqual(len(launches), 24)
            self.assertEqual(len(stopped), 23)
            self.assertEqual(len({process.pid for _slot, process in launches}), 24)
            self.assertNotIn(launches[0][1], stopped)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["event"], "stopped_for_provider_diagnosis")
            self.assertTrue(payload["slots"][0]["horizon_run_id"])
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

    def test_formal_wave_continues_unfinished_arms_after_supervisor_restart(self) -> None:
        """A replacement supervisor adopts 23 live arms and refills one slot.

        This is the experiment-level failure mode seen in the interrupted
        Figure 4 wave: the provider emits a candidate-independent burst while
        one child is still pre-admission, the supervisor exits, and the other
        children have already persisted their fixed horizon.  A second
        invocation must not launch duplicate arms or discard their state.  It
        adopts the 23 identity-checked children and launches exactly one
        replacement for the affected slot.
        """

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "runs"
            state = Path(raw) / "state.json"
            logs = Path(raw) / "logs"
            phase = {"value": 1}
            launches: list[dict[str, object]] = []
            stopped: list[object] = []
            adopted: list[object] = []
            processes: dict[int, object] = {}
            original_slots_for_wave = RUNNER._slots_for_wave

            def write_artifact(run: Path, slot, run_id: str, *, terminal: bool) -> None:  # type: ignore[no-untyped-def]
                run.mkdir(parents=True, exist_ok=True)
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
                lifecycle = sorted(REQUIRED_LIFECYCLE_EVENTS)
                rows = [{"event": event} for event in lifecycle]
                rows.append(
                    {
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
                )
                (run / "events.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
                )
                if not terminal:
                    return
                (run / "final.json").write_text(
                    json.dumps({"status": "COMPLETED", "health": {"ok": True, "issues": []}}),
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

            class FakeProcess:
                def __init__(self, pid: int, run: Path, slot) -> None:  # type: ignore[no-untyped-def]
                    self.pid = pid
                    self.run = run
                    self.slot = slot
                    self.returncode: int | None = None
                    self.adopted = False
                    self.finished = False

                def poll(self) -> int | None:
                    if self.returncode is not None:
                        return self.returncode
                    if phase["value"] >= 2 and self.adopted and not self.finished:
                        write_artifact(self.run, self.slot, f"complete-{self.pid}", terminal=True)
                        self.finished = True
                        self.returncode = 0
                    return self.returncode

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    if self.returncode is None:
                        self.returncode = -15
                    return self.returncode

            failed_key: tuple[str, int, str] | None = None

            def fake_launch(slot, *, repo, root, urls, caches, log_root):  # type: ignore[no-untyped-def]
                del repo, urls, caches, log_root
                index = len(launches)
                key = (slot.dataset, int(slot.repeat), slot.policy)
                run = (
                    root
                    / slot.dataset
                    / f"repeat-{slot.repeat:02d}"
                    / slot.policy
                    / f"attempt-{index:02d}"
                )
                if phase["value"] == 1 and index == 0:
                    # No horizon means this is the one pre-admission child
                    # that the burst breaker may stop and later refill.
                    run.mkdir(parents=True)
                    overload = {
                        "event": "agent_finished",
                        "returncode": 1,
                        "settled": True,
                        "assistant_success": False,
                        "error_tail": (
                            "Codex error: Our servers are currently overloaded. "
                            "Please try again later."
                        ),
                    }
                    (run / "events.jsonl").write_text(
                        "".join(json.dumps(overload) + "\n" for _ in range(RUNNER.INFRA_ERROR_LIMIT)),
                        encoding="utf-8",
                    )
                    nonlocal_failed[0] = key
                else:
                    write_artifact(
                        run,
                        slot,
                        f"live-{index:02d}",
                        terminal=phase["value"] >= 2,
                    )
                process = FakeProcess(50_000 + index, run, slot)
                if phase["value"] >= 2:
                    # The replacement arm has already produced a complete
                    # terminal artifact; let the supervisor observe its
                    # normal child exit on the next poll.
                    process.finished = True
                    process.returncode = 0
                process_by_pid[process.pid] = process
                slot.pid = process.pid
                slot.status = "starting"
                launches.append(
                    {
                        "phase": phase["value"],
                        "key": key,
                        "pid": process.pid,
                    }
                )
                return process

            # Keep the mutable key in a list so the nested launcher remains
            # compatible with Python's assignment rules.
            nonlocal_failed: list[tuple[str, int, str] | None] = [None]
            process_by_pid = processes

            def fake_adopt(pid, slot, repo):  # type: ignore[no-untyped-def]
                del repo
                process = process_by_pid.get(pid)
                if process is None or getattr(process, "returncode", None) is not None:
                    return None
                process.adopted = True
                adopted.append(process)
                return process

            def fake_stop(process) -> None:  # type: ignore[no-untyped-def]
                stopped.append(process)
                process.returncode = -15

            def slots_for_wave(repeat: int):  # type: ignore[no-untyped-def]
                return original_slots_for_wave(repeat) if repeat == 1 else []

            common = {
                "state_path": state,
                "log_root": logs,
                "root": root,
                "max_attempts": 3,
                "retry_seconds": 0.0,
                "max_infrastructure_failures": 3,
                "resume": True,
            }
            with (
                patch.object(
                    RUNNER,
                    "_discover_capabilities",
                    return_value=({"formal": "formal", "coding": "coding"}, {}),
                ),
                patch.object(RUNNER, "_health_ok", return_value=True),
                patch.object(RUNNER, "_slots_for_wave", side_effect=slots_for_wave),
                patch.object(RUNNER, "_launch", side_effect=fake_launch),
                patch.object(RUNNER, "_adopt_process", side_effect=fake_adopt),
                patch.object(RUNNER, "_terminate_process", side_effect=fake_stop),
                patch.object(RUNNER.time, "sleep", side_effect=lambda _seconds: None),
            ):
                first_code = RUNNER.run(Path(raw), **common)

            self.assertEqual(first_code, 4)
            failed_key = nonlocal_failed[0]
            self.assertIsNotNone(failed_key)
            self.assertEqual(len(launches), 24)
            self.assertEqual(len(stopped), 1)
            self.assertEqual(len(adopted), 0)
            first_state = json.loads(state.read_text(encoding="utf-8"))
            first_rows = first_state["slots"]
            self.assertEqual(sum(bool(row["horizon_run_id"]) for row in first_rows), 23)
            self.assertEqual(sum(row["pid"] is None for row in first_rows), 1)

            phase["value"] = 2
            with (
                patch.object(
                    RUNNER,
                    "_discover_capabilities",
                    return_value=({"formal": "formal", "coding": "coding"}, {}),
                ),
                patch.object(RUNNER, "_health_ok", return_value=True),
                patch.object(RUNNER, "_slots_for_wave", side_effect=slots_for_wave),
                patch.object(RUNNER, "_launch", side_effect=fake_launch),
                patch.object(RUNNER, "_adopt_process", side_effect=fake_adopt),
                patch.object(RUNNER, "_terminate_process", side_effect=fake_stop),
                patch.object(RUNNER.time, "sleep", side_effect=lambda _seconds: None),
            ):
                second_code = RUNNER.run(Path(raw), **common)

            self.assertEqual(second_code, 0)
            self.assertEqual(len(adopted), 23)
            self.assertEqual(len(launches), 25)
            self.assertEqual(
                [row["key"] for row in launches if row["phase"] == 2],
                [failed_key],
            )
            final_state = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(final_state["event"], "all_repeats_finished")
            self.assertTrue(all(row["status"] == "finished" for row in final_state["slots"]))

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

    def test_provider_offsets_are_namespaced_by_full_run_path(self) -> None:
        """Two arms may independently use the same local run-directory name."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            slots = [
                RUNNER.Slot(dataset="clever", repeat=1, policy="uniform_refill"),
                RUNNER.Slot(dataset="usaco", repeat=1, policy="uniform_refill"),
            ]
            overload = {
                "event": "agent_finished",
                "returncode": 1,
                "settled": True,
                "assistant_success": False,
                "timed_out": False,
                "cancelled": False,
                "error_tail": "Codex error: Our servers are currently overloaded",
            }
            for slot in slots:
                run = root / slot.dataset / "repeat-01" / slot.policy / "run-1"
                run.mkdir(parents=True)
                (run / "events.jsonl").write_text(
                    "".join(json.dumps(overload) + "\n" for _ in range(10)),
                    encoding="utf-8",
                )
            offsets: dict[str, int] = {}
            recent: deque[tuple[float, str, str]] = deque()
            burst = RUNNER._provider_infra_burst(root, slots, offsets, recent, now=100.0)
            self.assertIsNotNone(burst)
            assert burst is not None
            self.assertEqual(burst[2], RUNNER.INFRA_ERROR_LIMIT)

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
        self.assertFalse(RUNNER._health_ready(base | {"ready_workers": 2, "workspace_ready": False}))

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

    def test_runner_refresh_ignores_cross_bound_run_metadata(self) -> None:
        """A forensic directory cannot bind a different dataset/policy slot."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            slot = RUNNER.Slot(dataset="clever", repeat=1, policy="uniform_refill")
            directory = root / "clever" / "repeat-01" / "uniform_refill" / "wrong"
            directory.mkdir(parents=True)
            (directory / "run_meta.json").write_text(
                json.dumps(
                    {
                        "run_id": "wrong-binding",
                        "dataset": "usaco",
                        "horizon_started_at": "2026-08-25T00:00:00+00:00",
                        "allocation": {"policy": "task_state"},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(RUNNER._latest_run(root, slot), (None, None))

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

    def test_scheduler_fallback_and_full_score_cancellation_remain_eligible(self) -> None:
        """Charged LLM fallback outcomes do not invalidate a complete arm.

        The runner records a scheduler provider failure (including a Pi RPC
        cancelled by ``solver_completed``) as a deterministic fallback.  It
        must remain visible in the cost ledger without turning the whole arm
        into infrastructure failure.
        """

        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            (run / "final.json").write_text(
                json.dumps(
                    {
                        "status": "COMPLETED",
                        "health": {
                            "ok": True,
                            "issues": [],
                            "allocation_scheduler_nonzero_return_count": 1,
                            "allocation_scheduler_provider_error_count": 1,
                            "allocation_scheduler_summary_cost_provider_errors": 1,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run / "run_meta.json").write_text(
                json.dumps(
                    {
                        "run_id": "scheduler-fallback",
                        "dataset": "usaco",
                        "horizon_started_at": "2026-08-23T00:00:00+00:00",
                        "runtime_provenance": {
                            "image_id": "image",
                            "manifest_sha256": "manifest",
                            "source_commit": "commit",
                        },
                        "allocation": {"policy": "llm_scheduler"},
                    }
                ),
                encoding="utf-8",
            )
            (run / "figure4_run_summary.json").write_text(
                json.dumps({"policy": "llm_scheduler"}), encoding="utf-8"
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
            scheduler_event = {
                "event": "allocation_scheduler_finished",
                "task_id": "__allocation__",
                "returncode": -15,
                "cancelled": True,
                "recoverable_invocation_error": True,
                "scheduler_outcome": "provider_error",
                "error_tail": "Pi RPC was cancelled before agent_settled",
            }
            (run / "events.jsonl").write_text(
                "".join(
                    json.dumps({"event": event}) + "\n"
                    for event in REQUIRED_LIFECYCLE_EVENTS
                )
                + json.dumps(scheduler_event)
                + "\n",
                encoding="utf-8",
            )
            eligible, reasons = artifact_eligibility(run, policy="llm_scheduler")
            self.assertTrue(eligible, reasons)

            # Structural scheduler joins remain fail-closed even though
            # ordinary provider/fallback counters are benign outcomes.
            final = json.loads((run / "final.json").read_text(encoding="utf-8"))
            final["health"]["allocation_scheduler_call_id_error_count"] = 1
            (run / "final.json").write_text(json.dumps(final), encoding="utf-8")
            eligible, reasons = artifact_eligibility(run, policy="llm_scheduler")
            self.assertFalse(eligible)
            self.assertIn(
                "health_counter:allocation_scheduler_call_id_error_count",
                reasons,
            )

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
