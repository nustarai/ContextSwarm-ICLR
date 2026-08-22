from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.audit_canary_closeout import audit_canary


CANDIDATE = "a" * 64
CONTRACT = "b" * 64


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _command() -> list[str]:
    tools = (
        "read,edit,write,grep,find,ls,judge_check,cps_search,cps_publish,"
        "cps_actors,cps_inbox,cps_send,cps_ack"
    )
    prompt = (
        "You are not a general-purpose coding agent. Do not execute shell commands. "
        "Use the judge_check tool and never create a local or raw-network fallback."
    )
    return [
        "pi",
        "--mode",
        "rpc",
        "--approve",
        "--system-prompt",
        prompt,
        "--no-context-files",
        "--no-skills",
        "--no-prompt-templates",
        "--no-extensions",
        "--tools",
        tools,
        "--extension",
        "/opt/contextswarm/contextswarm_mini/pi_solver_tools.mjs",
    ]


def _stage(root: Path) -> Path:
    run = root / "canary"
    run.mkdir()
    _write_json(
        run / "run_meta.json",
        {
            "mode": "cps",
            "communication": "blackboard",
            "max_parallel": 1,
            "initial_agents_per_task": 1,
            "max_attempts_per_task": 1,
            "max_tasks": 1,
            "time_limit_seconds": 180,
            "lean_require_result_cache_disabled": True,
            "allocation": {"policy": "uniform"},
            "runtime_provenance": {
                "source_commit": "c" * 40,
                "image_id": "sha256:" + "d" * 64,
            },
        },
    )
    _write_json(
        run / "transport_preflight.json",
        {
            "status": "ok",
            "aisw": {"enabled": True},
            "lean": {
                "ok": True,
                "requested_env_accepted": True,
                "result_cache": {
                    "enabled": False,
                    "backend": "memory",
                    "backend_ready": True,
                    "requested_env_accepted": True,
                },
            },
        },
    )
    _write_json(
        run / "judge_broker_closeout.json",
        {
            "schema_version": "contextswarm_judge_broker_closeout_v1",
            "drained": True,
            "active_handlers": 0,
            "fifo_depth": 0,
            "remote_unsettled_jobs": 0,
        },
    )
    _write_json(
        run / "final.json",
        {
            "status": "COMPLETED",
            "finished_at": "2026-08-22T00:03:00+00:00",
            "verdicts": {"task-1": {"status": "VERIFY_FAIL", "score": 0.0}},
            "health": {
                "ok": True,
                "issues": [],
                "oom_or_exit_137_count": 0,
                "allocation_scheduler_oom_or_exit_137_count": 0,
            },
            "judge_result_cache": {
                "required_disabled": True,
                "enabled": False,
                "backend": "memory",
                "backend_ready": True,
                "requested_env_accepted": True,
            },
            "agents": [
                {
                    "agent_id": "agent-task-1-1",
                    "task_id": "task-1",
                    "episode": 1,
                    "mocked": False,
                    "returncode": 0,
                    "cancelled": False,
                    "timed_out": False,
                    "command": _command(),
                    "error_tail": "",
                    "output_tail": "",
                }
            ],
        },
    )
    _write_jsonl(
        run / "events.jsonl",
        [
            {"event": "run_started", "at": "2026-08-22T00:00:00+00:00"},
            {"event": "agent_finished", "returncode": 0},
            {"event": "run_finished", "status": "COMPLETED"},
        ],
    )
    _write_jsonl(
        run / "judge_checks.jsonl",
        [
            {
                "event": "judge_check",
                "actor_id": "agent-task-1-1",
                "task_id": "task-1",
                "accepted": True,
                "status": "VERIFY_FAIL",
                "candidate_sha256": CANDIDATE,
                "task_contract_sha256": CONTRACT,
                "judge_job_id": "job-1",
            }
        ],
    )
    return run


def _codes(report: dict[str, object]) -> set[str]:
    return {
        str(item["code"])
        for item in report["errors"]  # type: ignore[index]
        if isinstance(item, dict) and "code" in item
    }


class CanaryCloseoutTests(unittest.TestCase):
    def test_valid_real_canary_passes_without_requiring_delete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = audit_canary(_stage(Path(raw)))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["counts"]["accepted_judge_checks"], 1)
        self.assertFalse(report["remote_delete_observed"])
        self.assertNotIn("oom_observed", _codes(report))

    def test_oom_detection_uses_explicit_values_not_health_field_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = _stage(Path(raw))
            zero_count = audit_canary(run)

            final = json.loads((run / "final.json").read_text(encoding="utf-8"))
            final["health"]["oom_or_exit_137_count"] = 1
            _write_json(run / "final.json", final)
            positive_count = audit_canary(run)

            final["health"]["oom_or_exit_137_count"] = 0
            final["agents"][0]["returncode"] = 137
            _write_json(run / "final.json", final)
            exit_137 = audit_canary(run)

        self.assertNotIn("oom_observed", _codes(zero_count), zero_count)
        self.assertIn("oom_observed", _codes(positive_count), positive_count)
        self.assertIn("oom_observed", _codes(exit_137), exit_137)
        self.assertIn("solver_oom_or_exit_137", _codes(exit_137), exit_137)

    def test_requires_accepted_probe_with_complete_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = _stage(Path(raw))
            _write_jsonl(run / "judge_checks.jsonl", [])
            missing = audit_canary(run)
            _write_jsonl(
                run / "judge_checks.jsonl",
                [
                    {
                        "accepted": True,
                        "task_id": "task-1",
                        "status": "VERIFY_FAIL",
                        "candidate_sha256": "bad",
                        "task_contract_sha256": CONTRACT,
                        "judge_job_id": "job-1",
                    }
                ],
            )
            malformed = audit_canary(run)
        self.assertIn("accepted_judge_check_missing", _codes(missing))
        self.assertIn("accepted_judge_check_provenance_invalid", _codes(malformed))

    def test_fails_closed_on_mock_shell_bad_health_overload_and_broker_drain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = _stage(Path(raw))
            final = json.loads((run / "final.json").read_text(encoding="utf-8"))
            final["agents"][0]["mocked"] = True
            final["agents"][0]["command"].append("bash")
            final["agents"][0]["error_tail"] = "HTTP 429 too many requests; out of memory"
            final["health"] = {"ok": False, "issues": ["runner_or_worker_error"]}
            _write_json(run / "final.json", final)
            closeout = json.loads(
                (run / "judge_broker_closeout.json").read_text(encoding="utf-8")
            )
            closeout["drained"] = False
            closeout["remote_unsettled_jobs"] = 1
            _write_json(run / "judge_broker_closeout.json", closeout)
            _write_jsonl(run / "events.jsonl", [{"event": "elastic_worker_error"}])
            report = audit_canary(run)
        codes = _codes(report)
        self.assertTrue(
            {
                "mock_solver_present",
                "solver_shell_or_tool_contract_invalid",
                "final_health_failed",
                "judge_broker_not_drained",
                "runner_worker_or_broker_error",
                "judge_overload_or_429",
                "oom_observed",
            }.issubset(codes),
            report,
        )

    def test_remote_settlement_status_and_event_are_hard_failures(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = _stage(Path(raw))
            checks = [
                json.loads(line)
                for line in (run / "judge_checks.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            checks[0]["status"] = "REMOTE_SETTLEMENT_UNCONFIRMED"
            _write_jsonl(run / "judge_checks.jsonl", checks)
            events = [
                json.loads(line)
                for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            events.append({"event": "remote_settlement_unconfirmed"})
            _write_jsonl(run / "events.jsonl", events)
            report = audit_canary(run)

        self.assertIn("accepted_judge_check_failed", _codes(report))
        self.assertIn("runner_worker_or_broker_error", _codes(report))

    def test_reports_delete_when_a_real_cancellation_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run = _stage(Path(raw))
            final = json.loads((run / "final.json").read_text(encoding="utf-8"))
            final["verdicts"]["task-1"]["response"] = {
                "judge_cancellation": {"attempted": True, "succeeded": True}
            }
            _write_json(run / "final.json", final)
            report = audit_canary(run)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["remote_delete_observed"])


if __name__ == "__main__":
    unittest.main()
