from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.audit_allocation_closeout import audit_single_allocation_closeout


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_allocation_closeout.py"
ARMS = ("uniform", "formula", "agent")
ARM_TIMES = {
    "uniform": (
        "2026-08-22T00:00:00+00:00",
        "2026-08-22T00:01:00+00:00",
        "2026-08-22T01:00:00+00:00",
    ),
    "formula": (
        "2026-08-22T01:01:00+00:00",
        "2026-08-22T01:02:00+00:00",
        "2026-08-22T02:01:00+00:00",
    ),
    "agent": (
        "2026-08-22T02:02:00+00:00",
        "2026-08-22T02:03:00+00:00",
        "2026-08-22T03:02:00+00:00",
    ),
}
TASKS = tuple(f"problem_{index:02d}" for index in range(12))
TOOLS = (
    "read,edit,write,grep,find,ls,judge_check,cps_search,cps_publish,"
    "cps_inbox,cps_send,cps_ack,cps_actors"
)
CANDIDATE_HASH = "a" * 64
TASK_CONTRACT_HASH = "b" * 64
SOURCE_COMMIT = "c" * 40
IMAGE_ID = "sha256:" + "d" * 64
SOLVER_SYSTEM_PROMPT = (
    "You are not a general-purpose coding agent. Do not execute shell commands. "
    "Use the judge_check tool and never create a local or raw-network fallback."
)
SCHEDULER_SYSTEM_PROMPT = (
    "You are a read-only allocation decision component. You have no tools and "
    "must not inspect files."
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _solver_command() -> list[str]:
    return [
        "pi",
        "--mode",
        "rpc",
        "--approve",
        "--system-prompt",
        SOLVER_SYSTEM_PROMPT,
        "--no-context-files",
        "--no-skills",
        "--no-prompt-templates",
        "--no-extensions",
        "--tools",
        TOOLS,
        "--extension",
        "/bundle/pi_solver_tools.mjs",
        "--model",
        "paper-model",
    ]


def _scheduler_command() -> list[str]:
    return [
        "pi",
        "--mode",
        "rpc",
        "--approve",
        "--system-prompt",
        SCHEDULER_SYSTEM_PROMPT,
        "--no-tools",
        "--no-context-files",
        "--no-skills",
        "--no-prompt-templates",
        "--no-extensions",
        "--model",
        "paper-model",
    ]


def _scheduler_agent(index: int) -> dict[str, object]:
    return {
        "decision_index": index,
        "agent_id": f"allocation-scheduler-{index}",
        "task_id": "__allocation__",
        "episode": index,
        "returncode": 0,
        "cancelled": False,
        "timed_out": False,
        "run_horizon_reached": False,
        "mocked": False,
        "command": _scheduler_command(),
        "error_tail": "",
        "output_tail": json.dumps(
            {
                "task_id": TASKS[0],
                "reason": "highest expected progress",
                "evidence_piece_ids": [],
            },
            sort_keys=True,
        ),
    }


def _meta(arm: str) -> dict[str, object]:
    started_at, horizon_started_at, _finished_at = ARM_TIMES[arm]
    return {
        "name": f"allocation-{arm}",
        "run_id": f"run-{arm}",
        "started_at": started_at,
        "horizon_started_at": horizon_started_at,
        "repo_root": f"/workspace/{arm}",
        "mode": "cps",
        "communication": "blackboard",
        "dataset_root": "benchmarks/matholympiadbench",
        "problem_ids_path": "benchmarks/matholympiadbench/problem_ids.json",
        "max_parallel": 48,
        "initial_agents_per_task": 4,
        "max_attempts_per_task": 0,
        "cancel_on_proved": True,
        "assignment_policy": "round_robin",
        "episodes_per_task": 2,
        "max_tasks": 0,
        "time_limit_seconds": 3600,
        "seed": 0,
        "model": "paper-model",
        "thinking": "max",
        "fast_mode": False,
        "pi_timeout_seconds": 3600,
        "lean_server_configured": True,
        "effective_runtime_limits": {
            "source": "cgroup_v2",
            "memory_max_bytes": 51_539_607_552,
            "pids_max": 768,
            "cpu_max": "max 100000",
            "process_uid": 1000,
            "process_gid": 1000,
        },
        "lean_env_id": "formal_matholympiadbench",
        "lean_timeout_seconds": 300,
        "lean_max_concurrent_evaluations": 8,
        "lean_verification_profile": "formal_proof",
        "lean_judge_mode": "fast",
        "lean_require_result_cache_disabled": True,
        "runtime_provenance": {
            "source_commit": SOURCE_COMMIT,
            "image_id": IMAGE_ID,
        },
        "allocation": {
            "policy": arm,
            "agent_timeout_seconds": 120,
            "piece_limit_per_task": 3,
            "piece_body_chars": 1200,
            "formula": {"active_balance_weight": 2.0},
        },
    }


def _final(arm: str) -> dict[str, object]:
    _started_at, _horizon_started_at, finished_at = ARM_TIMES[arm]
    verdicts: dict[str, dict[str, object]] = {}
    for index, task in enumerate(TASKS):
        verdict: dict[str, object] = {
            "task_id": task,
            "status": "VERIFY_FAIL" if index == 0 else "TIME_LIMIT",
            "score": 0.0,
            "elapsed_seconds": 1.0,
            "response": {},
            "error": None,
        }
        if index == 0:
            verdict.update(
                {
                    "candidate_sha256": CANDIDATE_HASH,
                    "task_contract_sha256": TASK_CONTRACT_HASH,
                    "judge_job_id": "job-1",
                }
            )
        verdicts[task] = verdict
    return {
        "schema_version": "contextswarm_mini_run_v1",
        "status": "COMPLETED",
        "finished_at": finished_at,
        "mode": "cps",
        "communication": "blackboard",
        "horizon_seconds": 3600,
        "score": 0.0,
        "max_score": 12,
        "verdicts": verdicts,
        "agents": [
            {
                "agent_id": "solver-1",
                "task_id": TASKS[0],
                "episode": 1,
                "returncode": 0,
                "cancelled": False,
                "timed_out": False,
                "mocked": False,
                "command": _solver_command(),
                "error_tail": "",
                "output_tail": "",
            }
        ],
        "allocation_scheduler_agents": (
            [_scheduler_agent(1), _scheduler_agent(2)] if arm == "agent" else []
        ),
        "allocation": {
            "policy": arm,
            "initial_pool_size": 48,
            "initial_assignments": 48,
        },
        "cps": {"db": "cps.sqlite3", "pieces": 1, "messages": 0, "events": 1},
        "judge_result_cache": {
            "required_disabled": True,
            "enabled": False,
            "backend": "memory",
            "backend_ready": True,
            "requested_env_accepted": True,
        },
        "health": {"ok": True, "issues": []},
    }


def _events(arm: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "at": "2026-08-22T00:00:00+00:00",
            "event": "run_started",
            "name": f"allocation-{arm}",
            "run_id": f"run-{arm}",
            "mode": "cps",
            "communication": "blackboard",
            "model": "paper-model",
            "thinking": "max",
            "task_count": 12,
            "tasks": list(TASKS),
            "max_parallel": 48,
            "initial_agents_per_task": 4,
            "time_limit_seconds": 3600,
            "allocation": {"policy": arm, "agent_timeout_seconds": 120},
            "lean_env_id": "formal_matholympiadbench",
            "lean_max_concurrent_evaluations": 8,
        },
        {
            "at": "2026-08-22T00:00:01+00:00",
            "event": "agent_assigned",
            "agent_id": "solver-1",
            "task_id": TASKS[0],
            "episode": 1,
            "allocation_phase": "adaptive",
            "decision_index": 1,
        },
        {
            "at": "2026-08-22T00:09:59+00:00",
            "event": "agent_finished",
            "agent_id": "solver-1",
            "task_id": TASKS[0],
            "episode": 1,
            "returncode": 0,
            "cancelled": False,
            "timed_out": False,
        },
        {
            "at": "2026-08-22T00:10:00+00:00",
            "event": "evaluation_finished",
            "agent_id": "solver-1",
            "task_id": TASKS[0],
            "episode": 1,
            "status": "VERIFY_FAIL",
            "response": {},
            "error": None,
            "candidate_sha256": CANDIDATE_HASH,
            "task_contract_sha256": TASK_CONTRACT_HASH,
            "judge_job_id": "job-1",
        },
        {
            "at": "2026-08-22T01:00:00+00:00",
            "event": "run_finished",
            "status": "COMPLETED",
            "score": 0.0,
        },
    ]
    if arm == "agent":
        rows[-1:-1] = [
            {"event": "allocation_scheduler_finished", **_scheduler_agent(index)}
            for index in (1, 2)
        ]
    return rows


def _stage_arm(root: Path, arm: str) -> Path:
    run = root / arm
    run.mkdir(parents=True)
    _write_json(run / "run_meta.json", _meta(arm))
    _write_json(run / "final.json", _final(arm))
    _write_json(
        run / "allocation_summary.json",
        {
            "policy": arm,
            "initial_pool_size": 48,
            "initial_assignments": 48,
            **({"agent_calls": 2} if arm == "agent" else {}),
        },
    )
    _write_jsonl(run / "events.jsonl", _events(arm))
    _write_json(
        run / "elastic_scheduler_state.json",
        {
            "active_slots": 0,
            "remaining_slots": 48,
            "horizon_reached": True,
            "tasks": {
                task: {"active_agents": 0, "solved": False}
                for task in TASKS
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
        run / "transport_preflight.json",
        {
            "schema_version": 1,
            "status": "ok",
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
            "aisw": {"enabled": True},
        },
    )
    _write_jsonl(
        run / "allocation_decisions.jsonl",
        [
            {
                "decision_index": 1,
                "policy": arm,
                "selected_task_id": TASKS[0],
                "assigned_agent_id": "solver-1",
                "assigned_generation": 1,
                "agent_result_valid": True if arm == "agent" else None,
                "fallback": False,
                "disposition": "assigned",
                **(
                    {
                        "agent_returncode": 0,
                        "agent_timed_out": False,
                        "agent_cancelled": False,
                        "agent_id": "allocation-scheduler-1",
                        "agent_task_id": "__allocation__",
                        "agent_episode": 1,
                        "agent_run_horizon_reached": False,
                    }
                    if arm == "agent"
                    else {}
                ),
            },
            {
                "decision_index": 2,
                "policy": arm,
                "selected_task_id": TASKS[0],
                "assigned_agent_id": None,
                "assigned_generation": None,
                "agent_result_valid": True if arm == "agent" else None,
                "fallback": False,
                "fallback_reason": "run horizon reached before admission",
                "disposition": "not_admitted_horizon",
                **(
                    {
                        "agent_returncode": 0,
                        "agent_timed_out": False,
                        "agent_cancelled": False,
                        "agent_id": "allocation-scheduler-2",
                        "agent_task_id": "__allocation__",
                        "agent_episode": 2,
                        "agent_run_horizon_reached": False,
                    }
                    if arm == "agent"
                    else {}
                ),
            },
        ],
    )
    _write_jsonl(
        run / "communication_trace.jsonl",
        [
            {
                "actor_id": "runner",
                "event_type": "piece_created",
                "task_id": TASKS[0],
                "payload": {
                    "author": "runner",
                    "kind": "validation_result",
                    "task_id": TASKS[0],
                    "status": "VERIFY_FAIL",
                    "score": 0.0,
                    "candidate_sha256": CANDIDATE_HASH,
                    "task_contract_sha256": TASK_CONTRACT_HASH,
                    "judge_job_id": "job-1",
                },
            }
        ],
    )
    _write_jsonl(
        run / "judge_checks.jsonl",
        [
            {
                "event": "judge_check",
                "actor_id": "solver-1",
                "task_id": TASKS[0],
                "accepted": True,
                "status": "VERIFY_FAIL",
                "candidate_sha256": CANDIDATE_HASH,
                "task_contract_sha256": TASK_CONTRACT_HASH,
                "judge_job_id": "job-1",
            }
        ],
    )
    (run / "cps.sqlite3").touch()
    session = run / "sessions" / "solver.jsonl"
    _write_jsonl(
        session,
        [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "judge_check",
                            "arguments": {},
                        }
                    ],
                }
            },
            {
                "message": {
                    "role": "toolResult",
                    "content": [
                        {
                            "type": "toolResult",
                            "toolCallId": "call-1",
                            "toolName": "judge_check",
                            "content": [{"type": "text", "text": "VERIFY_FAIL"}],
                        }
                    ],
                }
            },
        ],
    )
    session_index = [
        {
            "actor_id": "solver-1",
            "task_id": TASKS[0],
            "episode": 1,
            "session_file": "sessions/solver.jsonl",
        }
    ]
    if arm == "agent":
        for index in (1, 2):
            scheduler_session = run / "sessions" / f"scheduler-{index}.jsonl"
            _write_jsonl(
                scheduler_session,
                [{"message": {"role": "assistant", "content": []}}],
            )
            session_index.append(
                {
                    "actor_id": f"allocation-scheduler-{index}",
                    "task_id": "__allocation__",
                    "episode": index,
                    "session_file": f"sessions/scheduler-{index}.jsonl",
                }
            )
    _write_jsonl(run / "pi_session_index.jsonl", session_index)
    return run


def _stage_positive_uniform(run: Path) -> None:
    final = _final("uniform")
    final_verdict = final["verdicts"][TASKS[0]]
    final_verdict["status"] = "PROVED"
    final_verdict["score"] = 1.0
    final["score"] = 1.0
    _write_json(run / "final.json", final)

    trace = [
        json.loads(line)
        for line in (run / "communication_trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    trace[0]["payload"].update({"status": "PROVED", "score": 1.0})
    _write_jsonl(run / "communication_trace.jsonl", trace)

    events = _events("uniform")
    evaluation = next(
        row for row in events if row.get("event") == "evaluation_finished"
    )
    evaluation.update(
        {
            "status": "PROVED",
            "score": 1.0,
            "source": "judge_check",
            "scoreboard_recorded": False,
        }
    )
    agent_finished = next(row for row in events if row.get("event") == "agent_finished")
    events.remove(evaluation)
    events.remove(agent_finished)
    admission_index = next(
        index
        for index, row in enumerate(events)
        if row.get("event") == "agent_assigned"
    )
    events[admission_index + 1 : admission_index + 1] = [
        {
            "at": "2026-08-22T00:05:00+00:00",
            "event": "judge_proof_credited",
            "agent_id": "solver-1",
            "task_id": TASKS[0],
            "episode": 1,
            "candidate_sha256": CANDIDATE_HASH,
            "task_contract_sha256": TASK_CONTRACT_HASH,
            "judge_job_id": "job-1",
        },
        evaluation,
        {
            "at": "2026-08-22T00:05:01+00:00",
            "event": "best_candidate_promoted",
            "agent_id": "solver-1",
            "task_id": TASKS[0],
            "episode": 1,
            "status": "PROVED",
            "score": 1.0,
            "candidate_sha256": CANDIDATE_HASH,
            "task_contract_sha256": TASK_CONTRACT_HASH,
            "judge_job_id": "job-1",
        },
        agent_finished,
    ]
    _write_jsonl(run / "events.jsonl", events)

    judge_rows = [
        json.loads(line)
        for line in (run / "judge_checks.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    judge_rows[0]["status"] = "PROVED"
    _write_jsonl(run / "judge_checks.jsonl", judge_rows)
    _write_jsonl(
        run / "scoreboard_history.jsonl",
        [
            {
                "at": "2026-08-22T00:05:00+00:00",
                "horizon_elapsed_seconds": 240.0,
                "source": "judge_check",
                "agent_id": "solver-1",
                "task_id": TASKS[0],
                "episode": 1,
                "status": "PROVED",
                "score": 1.0,
                "candidate_sha256": CANDIDATE_HASH,
                "task_contract_sha256": TASK_CONTRACT_HASH,
                "judge_job_id": "job-1",
            }
        ],
    )


def _run_audit(paths: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT)]
    for arm in ARMS:
        command.extend([f"--{arm}", str(paths[arm])])
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_single_audit(
    policy: str,
    run: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--single-policy",
            policy,
            "--single-run",
            str(run),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _codes(report: dict[str, object], arm: str) -> set[str]:
    arms = report["arms"]
    assert isinstance(arms, dict)
    arm_report = arms[arm]
    assert isinstance(arm_report, dict)
    errors = arm_report["errors"]
    assert isinstance(errors, list)
    return {str(item["code"]) for item in errors if isinstance(item, dict)}


def _warning_codes(report: dict[str, object], arm: str) -> set[str]:
    arm_report = report["arms"][arm]
    return {
        str(item["code"])
        for item in arm_report["warnings"]
        if isinstance(item, dict)
    }


def _error_fields(report: dict[str, object], arm: str) -> set[str]:
    arm_report = report["arms"][arm]
    return {
        str(item["field"])
        for item in arm_report["errors"]
        if isinstance(item, dict) and item.get("field")
    }


class AllocationCloseoutAuditTests(unittest.TestCase):
    def _clean_runs(self, root: Path) -> dict[str, Path]:
        return {arm: _stage_arm(root, arm) for arm in ARMS}

    def test_clean_three_arm_closeout_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = _run_audit(self._clean_runs(Path(temporary)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["schema_version"], "contextswarm_allocation_closeout_audit_v1")
        self.assertTrue(all(report["arms"][arm]["ok"] for arm in ARMS))
        self.assertEqual(report["arms"]["uniform"]["counts"]["agent_admissions"], 1)
        self.assertEqual(report["arms"]["uniform"]["counts"]["agent_finishes"], 1)
        self.assertEqual(report["arms"]["uniform"]["counts"]["agent_evaluations"], 1)

    def test_three_arm_closeout_rejects_overlap_or_wrong_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            formula_meta = _meta("formula")
            formula_meta["started_at"] = "2026-08-22T00:59:59+00:00"
            formula_meta["horizon_started_at"] = "2026-08-22T01:00:00+00:00"
            _write_json(paths["formula"] / "run_meta.json", formula_meta)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn(
            "cross_arm_run_order_overlap",
            {item["code"] for item in report["cross_arm"]["errors"]},
        )

    def test_single_arm_requires_consistent_disabled_cache_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = _stage_arm(Path(temporary), "uniform")
            preflight = json.loads(
                (run / "transport_preflight.json").read_text(encoding="utf-8")
            )
            preflight["lean"]["result_cache"]["enabled"] = True
            _write_json(run / "transport_preflight.json", preflight)
            result = _run_single_audit("uniform", run)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "judge_result_cache_evidence_invalid",
            _codes(json.loads(result.stdout), "uniform"),
        )

        with tempfile.TemporaryDirectory() as temporary:
            run = _stage_arm(Path(temporary), "uniform")
            final = _final("uniform")
            final.pop("judge_result_cache")
            _write_json(run / "final.json", final)
            result = _run_single_audit("uniform", run)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "judge_result_cache_evidence_invalid",
            _codes(json.loads(result.stdout), "uniform"),
        )

    def test_single_arm_rejects_missing_naive_or_internally_reversed_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = _stage_arm(Path(temporary), "uniform")
            meta = _meta("uniform")
            meta["started_at"] = "2026-08-22T00:00:00"
            meta["horizon_started_at"] = "2026-08-21T23:59:00+00:00"
            _write_json(run / "run_meta.json", meta)
            result = _run_single_audit("uniform", run)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn("run_timestamp_invalid", _codes(report, "uniform"))

    def test_single_arm_api_and_cli_gate_only_that_arm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = _stage_arm(Path(temporary), "uniform")
            api_report = audit_single_allocation_closeout("uniform", run)
            cli_result = _run_single_audit("uniform", run)

            run.joinpath("judge_broker_closeout.json").unlink()
            failed_result = _run_single_audit("uniform", run)
        self.assertTrue(api_report["ok"])
        self.assertEqual(set(api_report["arms"]), {"uniform"})
        self.assertTrue(api_report["cross_arm"]["skipped"])
        self.assertEqual(cli_result.returncode, 0, cli_result.stdout + cli_result.stderr)
        cli_report = json.loads(cli_result.stdout)
        self.assertEqual(set(cli_report["arms"]), {"uniform"})
        self.assertTrue(cli_report["cross_arm"]["skipped"])
        self.assertEqual(failed_result.returncode, 1)
        self.assertIn(
            "judge_broker_closeout_missing",
            _codes(json.loads(failed_result.stdout), "uniform"),
        )

    def test_missing_final_worker_error_and_evaluator_error_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            paths["uniform"].joinpath("final.json").unlink()
            formula_events = _events("formula")[:-1]
            formula_events.append({"event": "elastic_worker_error", "error": "worker failed"})
            _write_jsonl(paths["formula"] / "events.jsonl", formula_events)
            agent_final = _final("agent")
            agent_final["verdicts"][TASKS[0]]["status"] = "EVALUATOR_ERROR"
            agent_final["status"] = "DEGRADED"
            _write_json(paths["agent"] / "final.json", agent_final)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn("final_missing", _codes(report, "uniform"))
        self.assertIn("elastic_worker_error", _codes(report, "formula"))
        self.assertIn("evaluator_transport_error", _codes(report, "agent"))

    def test_exit_137_oom_and_evaluator_429_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            final = _final("uniform")
            final["agents"][0]["returncode"] = 137
            final["agents"][0]["error_tail"] = "process was OOM-killed"
            _write_json(paths["uniform"] / "final.json", final)
            events = _events("formula")
            evaluation = next(row for row in events if row["event"] == "evaluation_finished")
            evaluation["status"] = "EVALUATOR_ERROR"
            evaluation["error"] = "429 Too Many Requests"
            _write_jsonl(paths["formula"] / "events.jsonl", events)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn("solver_oom_or_exit_137", _codes(report, "uniform"))
        self.assertIn("evaluator_transport_error", _codes(report, "formula"))

    def test_shell_capability_and_forbidden_session_actions_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            final = _final("agent")
            command = final["agents"][0]["command"]
            command[command.index("--tools") + 1] = TOOLS + ",bash"
            _write_json(paths["agent"] / "final.json", final)
            _write_jsonl(
                paths["agent"] / "sessions" / "solver.jsonl",
                [
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "id": "bad-call",
                                    "name": "bash",
                                    "arguments": {
                                        "command": (
                                            "elan toolchain install stable; lean result.lean; "
                                            "curl judge-service; xargs -P 8"
                                        )
                                    },
                                }
                            ],
                        }
                    }
                ],
            )
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        codes = _codes(json.loads(result.stdout), "agent")
        self.assertIn("solver_tool_allowlist_has_shell", codes)
        self.assertIn("session_forbidden_tool", codes)
        self.assertIn("session_local_lean_execution", codes)
        self.assertIn("session_toolchain_installation", codes)
        self.assertIn("session_raw_http", codes)
        self.assertIn("session_parallel_or_heavy_execution", codes)

    def test_missing_or_invalid_index_and_unreadable_session_are_hard_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            paths["uniform"].joinpath("pi_session_index.jsonl").unlink()
            paths["formula"].joinpath("pi_session_index.jsonl").write_text(
                "not-json\n",
                encoding="utf-8",
            )
            paths["agent"].joinpath("sessions", "solver.jsonl").unlink()
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn("session_index_missing", _codes(report, "uniform"))
        self.assertIn("session_index_invalid_jsonl", _codes(report, "formula"))
        self.assertIn("solver_sessions_missing", _codes(report, "formula"))
        self.assertIn("session_files_unreadable", _codes(report, "agent"))
        self.assertIn("solver_sessions_missing", _codes(report, "agent"))

    def test_assignment_finish_evaluation_identity_chain_must_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            uniform_events = [
                row for row in _events("uniform") if row["event"] != "agent_finished"
            ]
            _write_jsonl(paths["uniform"] / "events.jsonl", uniform_events)
            formula_events = _events("formula")
            evaluation = next(
                row for row in formula_events if row["event"] == "evaluation_finished"
            )
            evaluation["task_id"] = TASKS[1]
            _write_jsonl(paths["formula"] / "events.jsonl", formula_events)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn("agent_event_chain_incomplete", _codes(report, "uniform"))
        self.assertIn("agent_event_chain_mismatch", _codes(report, "uniform"))
        self.assertIn("agent_event_chain_mismatch", _codes(report, "formula"))

    def test_scheduler_must_quiesce_and_decisions_need_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            state = {
                "active_slots": 1,
                "tasks": {
                    TASKS[0]: {"active_agents": 1, "status": "RUNNING"},
                },
            }
            _write_json(paths["uniform"] / "elastic_scheduler_state.json", state)
            _write_jsonl(
                paths["formula"] / "allocation_decisions.jsonl",
                [
                    {
                        "decision_index": 1,
                        "selected_task_id": TASKS[0],
                        "assigned_agent_id": None,
                        "fallback_reason": "selected task became ineligible",
                    }
                ],
            )
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn("scheduler_not_quiescent", _codes(report, "uniform"))
        self.assertIn("scheduler_running_state", _codes(report, "uniform"))
        self.assertIn("allocation_decision_without_disposition", _codes(report, "formula"))

    def test_broker_closeout_is_mandatory_drained_and_timeout_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            paths["uniform"].joinpath("judge_broker_closeout.json").unlink()
            _write_json(
                paths["formula"] / "judge_broker_closeout.json",
                {
                    "schema_version": "contextswarm_judge_broker_closeout_v1",
                    "drained": False,
                    "active_handlers": 0,
                    "fifo_depth": 0,
                    "remote_unsettled_jobs": 1,
                },
            )
            agent_events = _events("agent")
            agent_events.insert(-1, {"event": "broker_drain_timeout"})
            _write_jsonl(paths["agent"] / "events.jsonl", agent_events)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn("judge_broker_closeout_missing", _codes(report, "uniform"))
        self.assertIn("judge_broker_not_drained", _codes(report, "formula"))
        self.assertIn("broker_drain_timeout", _codes(report, "agent"))

    def test_validation_provenance_and_judge_check_integrity_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            _write_jsonl(
                paths["agent"] / "communication_trace.jsonl",
                [
                    {
                        "actor_id": "solver-1",
                        "event_type": "piece_created",
                        "task_id": TASKS[0],
                        "payload": {
                            "author": "solver-1",
                            "kind": "validation_result",
                            "task_id": TASKS[0],
                        },
                    }
                ],
            )
            for arm, status in zip(
                ARMS,
                (
                    "REMOTE_SETTLEMENT_UNCONFIRMED",
                    "NETWORK_ERROR",
                    "REJECTED_OVERLOADED",
                ),
            ):
                row = {
                    "event": "judge_check",
                    "task_id": TASKS[0],
                    "accepted": True,
                    "status": status,
                    "candidate_sha256": CANDIDATE_HASH,
                }
                if arm == "uniform":
                    row["candidate_sha256"] = None
                if arm == "agent":
                    row["status_code"] = 429
                _write_jsonl(paths[arm] / "judge_checks.jsonl", [row])
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn("judge_check_failure_status", _codes(report, "uniform"))
        self.assertIn("judge_check_control_failure", _codes(report, "uniform"))
        self.assertIn("judge_check_candidate_hash_missing", _codes(report, "uniform"))
        self.assertIn("judge_check_failure_status", _codes(report, "formula"))
        self.assertIn("judge_check_failure_status", _codes(report, "agent"))
        agent_codes = _codes(report, "agent")
        self.assertIn("validation_result_author_not_runner", agent_codes)
        self.assertIn("validation_result_candidate_hash_missing", agent_codes)
        self.assertIn("validation_result_task_contract_hash_missing", agent_codes)

    def test_contract_mismatch_and_sensitive_meta_fail_without_value_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            formula_meta = _meta("formula")
            formula_meta["model"] = "different-model"
            _write_json(paths["formula"] / "run_meta.json", formula_meta)
            private_value = "https" + "://example.invalid/private-value"
            agent_meta = _meta("agent")
            agent_meta["lean_server_url"] = private_value
            agent_meta["access_token"] = "private-test-value"
            _write_json(paths["agent"] / "run_meta.json", agent_meta)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        cross_errors = report["cross_arm"]["errors"]
        self.assertEqual(cross_errors[0]["code"], "cross_arm_contract_mismatch")
        self.assertIn("run_meta.model", cross_errors[0]["fields"])
        self.assertIn("endpoint_leak", _codes(report, "agent"))
        self.assertIn("secret_leak", _codes(report, "agent"))
        self.assertNotIn(private_value, result.stdout)
        self.assertNotIn("private-test-value", result.stdout)

    def test_runtime_provenance_is_mandatory_and_strictly_formatted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            uniform_meta = _meta("uniform")
            uniform_meta.pop("runtime_provenance")
            _write_json(paths["uniform"] / "run_meta.json", uniform_meta)

            formula_meta = _meta("formula")
            formula_meta["runtime_provenance"]["source_commit"] = "not-a-commit"
            _write_json(paths["formula"] / "run_meta.json", formula_meta)

            agent_meta = _meta("agent")
            agent_meta["runtime_provenance"]["image_id"] = "not-an-image-id"
            _write_json(paths["agent"] / "run_meta.json", agent_meta)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn("runtime_provenance_missing", _codes(report, "uniform"))
        self.assertIn("runtime_source_commit_invalid", _codes(report, "formula"))
        self.assertIn("runtime_image_id_invalid", _codes(report, "agent"))

    def test_valid_runtime_provenance_must_match_across_arms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            formula_meta = _meta("formula")
            formula_meta["runtime_provenance"]["source_commit"] = "e" * 40
            _write_json(paths["formula"] / "run_meta.json", formula_meta)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertTrue(report["arms"]["formula"]["ok"])
        cross_errors = report["cross_arm"]["errors"]
        self.assertEqual(cross_errors[0]["code"], "cross_arm_contract_mismatch")
        self.assertIn(
            "run_meta.runtime_provenance.source_commit",
            cross_errors[0]["fields"],
        )

    def test_empty_judge_audit_and_hard_controls_fail_but_soft_controls_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            _write_jsonl(paths["uniform"] / "judge_checks.jsonl", [])
            formula_rows = json.loads(
                (paths["formula"] / "judge_checks.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            _write_jsonl(
                paths["formula"] / "judge_checks.jsonl",
                [
                    formula_rows,
                    {
                        "event": "judge_check",
                        "task_id": TASKS[0],
                        "accepted": False,
                        "status": "SESSION_PROBE_COOLDOWN",
                    },
                    {
                        "event": "judge_check",
                        "task_id": TASKS[0],
                        "accepted": False,
                        "status": "SESSION_PROBE_IN_FLIGHT",
                    },
                    {
                        "event": "judge_check",
                        "task_id": TASKS[0],
                        "accepted": False,
                        "status": "OUT_OF_HORIZON",
                    },
                ],
            )
            agent_rows = [
                json.loads(line)
                for line in (paths["agent"] / "judge_checks.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            agent_rows.append(
                {
                    "event": "judge_check",
                    "task_id": TASKS[0],
                    "accepted": False,
                    "status": "SESSION_PROBE_BUDGET_EXHAUSTED",
                }
            )
            _write_jsonl(paths["agent"] / "judge_checks.jsonl", agent_rows)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertIn("judge_checks_empty", _codes(report, "uniform"))
        self.assertEqual(report["arms"]["formula"]["counts"]["judge_control_failures"], 0)
        self.assertEqual(report["arms"]["formula"]["counts"]["judge_soft_controls"], 2)
        self.assertEqual(report["arms"]["formula"]["counts"]["judge_normal_controls"], 1)
        self.assertIn("judge_check_soft_control", _warning_codes(report, "formula"))
        self.assertIn("judge_check_control_failure", _codes(report, "agent"))

    def test_judge_provenance_rejection_is_a_hard_run_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            rows = [
                json.loads(line)
                for line in (paths["uniform"] / "judge_checks.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            rows[0]["status"] = "PROVENANCE_INVALID"
            rows[0]["proved"] = False
            _write_jsonl(paths["uniform"] / "judge_checks.jsonl", rows)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "judge_check_failure_status",
            _codes(json.loads(result.stdout), "uniform"),
        )

    def test_provenance_join_allows_direct_final_judge_but_cache_reuse_requires_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            for arm in ARMS:
                _write_jsonl(
                    paths[arm] / "judge_checks.jsonl",
                    [
                        {
                            "event": "judge_check",
                            "actor_id": "solver-1",
                            "task_id": TASKS[0],
                            "accepted": True,
                            "status": "VERIFY_FAIL",
                            "candidate_sha256": "c" * 64,
                            "task_contract_sha256": TASK_CONTRACT_HASH,
                            "judge_job_id": "intermediate-probe-job",
                        }
                    ],
                )
            direct_result = _run_audit(paths)
            self.assertEqual(
                direct_result.returncode,
                0,
                direct_result.stdout + direct_result.stderr,
            )

            agent_final = _final("agent")
            agent_final["verdicts"][TASKS[0]]["cache_reused"] = True
            _write_json(paths["agent"] / "final.json", agent_final)
            agent_events = _events("agent")
            evaluation = next(
                row for row in agent_events if row.get("event") == "evaluation_finished"
            )
            evaluation["cache_reused"] = True
            _write_jsonl(paths["agent"] / "events.jsonl", agent_events)
            cached_result = _run_audit(paths)
        self.assertEqual(cached_result.returncode, 1)
        codes = _codes(json.loads(cached_result.stdout), "agent")
        self.assertIn("cache_reused_evaluation_probe_unlinked", codes)
        self.assertIn("cache_reused_final_probe_unlinked", codes)

    def test_validation_evaluation_and_final_provenance_must_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            trace = [
                json.loads(line)
                for line in (paths["agent"] / "communication_trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            trace[0]["payload"]["judge_job_id"] = "different-job"
            _write_jsonl(paths["agent"] / "communication_trace.jsonl", trace)
            judge = [
                json.loads(line)
                for line in (paths["formula"] / "judge_checks.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            judge[0].pop("judge_job_id")
            _write_jsonl(paths["formula"] / "judge_checks.jsonl", judge)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        agent_codes = _codes(report, "agent")
        self.assertIn("validation_evaluation_provenance_mismatch", agent_codes)
        self.assertIn("final_validation_provenance_unlinked", agent_codes)
        self.assertIn("judge_check_judge_job_missing", _codes(report, "formula"))
        self.assertIn("judge_check_provenance_incomplete", _codes(report, "formula"))

    def test_exact_duplicate_validation_is_not_hidden_by_provenance_join(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            trace = [
                json.loads(line)
                for line in (paths["uniform"] / "communication_trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            trace.append(json.loads(json.dumps(trace[0])))
            _write_jsonl(paths["uniform"] / "communication_trace.jsonl", trace)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "validation_evaluation_provenance_mismatch",
            _codes(json.loads(result.stdout), "uniform"),
        )

    def test_sensitive_final_events_judge_and_session_report_only_field_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            endpoint = "https" + "://private.invalid/operator-path"
            secret = "unit-test-secret-value"
            final = _final("agent")
            final["agents"][0]["output_tail"] = endpoint
            _write_json(paths["agent"] / "final.json", final)
            events = _events("agent")
            evaluation = next(
                row for row in events if row.get("event") == "evaluation_finished"
            )
            evaluation["error"] = f"token={secret}"
            _write_jsonl(paths["agent"] / "events.jsonl", events)
            judge = [
                json.loads(line)
                for line in (paths["agent"] / "judge_checks.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            judge[0]["diagnostic"] = endpoint
            _write_jsonl(paths["agent"] / "judge_checks.jsonl", judge)
            trace = [
                json.loads(line)
                for line in (paths["agent"] / "communication_trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            trace[0]["payload"]["diagnostic"] = f"token={secret}"
            _write_jsonl(paths["agent"] / "communication_trace.jsonl", trace)
            _write_jsonl(
                paths["agent"] / "sessions" / "solver.jsonl",
                [
                    {
                        "message": {
                            "role": "tool",
                            "content": [{"type": "text", "text": f"Bearer {secret}"}],
                        }
                    }
                ],
            )
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        fields = _error_fields(report, "agent")
        self.assertIn("final.agents.0.output_tail", fields)
        self.assertIn("events.3.error", fields)
        self.assertIn("judge_checks.0.diagnostic", fields)
        self.assertIn("communication_trace.0.payload.diagnostic", fields)
        self.assertTrue(any(field.startswith("session.0.0") for field in fields))
        self.assertNotIn(endpoint, result.stdout)
        self.assertNotIn(secret, result.stdout)

    def test_scheduler_stale_decision_and_scheduler_artifacts_close_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            decisions = [
                json.loads(line)
                for line in (paths["agent"] / "allocation_decisions.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            decisions[1].update(
                {
                    "disposition": "not_admitted_stale",
                    "agent_result_valid": True,
                    "fallback": False,
                    "fallback_reason": "",
                }
            )
            _write_jsonl(paths["agent"] / "allocation_decisions.jsonl", decisions)
            valid = _run_audit(paths)
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)

            decisions[1]["fallback"] = True
            _write_jsonl(paths["agent"] / "allocation_decisions.jsonl", decisions)
            (paths["agent"] / "sessions" / "scheduler-2.jsonl").unlink()
            invalid = _run_audit(paths)
        self.assertEqual(invalid.returncode, 1)
        codes = _codes(json.loads(invalid.stdout), "agent")
        self.assertIn("allocation_stale_disposition_invalid", codes)
        self.assertIn("allocation_scheduler_decision_not_pure_agent", codes)
        self.assertIn("allocation_scheduler_session_chain_mismatch", codes)

    def test_non_agent_stale_decision_does_not_require_agent_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            for arm in ("uniform", "formula"):
                decisions = [
                    json.loads(line)
                    for line in (paths[arm] / "allocation_decisions.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                decisions[1].update(
                    {
                        "disposition": "not_admitted_stale",
                        "agent_result_valid": None,
                        "fallback": False,
                        "fallback_reason": "",
                    }
                )
                _write_jsonl(paths[arm] / "allocation_decisions.jsonl", decisions)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_agent_stale_decision_requires_valid_nonfallback_result(self) -> None:
        for field, value in (("agent_result_valid", False), ("fallback", True)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                paths = self._clean_runs(Path(temporary))
                decisions = [
                    json.loads(line)
                    for line in (paths["agent"] / "allocation_decisions.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                decisions[1].update(
                    {
                        "disposition": "not_admitted_stale",
                        "agent_result_valid": True,
                        "fallback": False,
                        "fallback_reason": "",
                        field: value,
                    }
                )
                _write_jsonl(
                    paths["agent"] / "allocation_decisions.jsonl",
                    decisions,
                )
                result = _run_audit(paths)
            self.assertEqual(result.returncode, 1)
            codes = _codes(json.loads(result.stdout), "agent")
            self.assertIn("allocation_stale_disposition_invalid", codes)
            self.assertIn("allocation_scheduler_decision_not_pure_agent", codes)

    def test_scheduler_duplicate_decision_index_fails_for_each_artifact(self) -> None:
        for artifact in ("final", "event", "decision"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as temporary:
                paths = self._clean_runs(Path(temporary))
                if artifact == "final":
                    final = _final("agent")
                    final["allocation_scheduler_agents"][1]["decision_index"] = 1
                    _write_json(paths["agent"] / "final.json", final)
                elif artifact == "event":
                    events = _events("agent")
                    scheduler_events = [
                        row
                        for row in events
                        if row.get("event") == "allocation_scheduler_finished"
                    ]
                    scheduler_events[1]["decision_index"] = 1
                    _write_jsonl(paths["agent"] / "events.jsonl", events)
                else:
                    decisions = [
                        json.loads(line)
                        for line in (paths["agent"] / "allocation_decisions.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                    ]
                    decisions[1]["decision_index"] = 1
                    _write_jsonl(
                        paths["agent"] / "allocation_decisions.jsonl",
                        decisions,
                    )
                result = _run_audit(paths)
            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "allocation_scheduler_result_chain_mismatch",
                _codes(json.loads(result.stdout), "agent"),
            )

    def test_scheduler_event_and_decision_result_fields_must_match_final(self) -> None:
        mutations = (
            ("event", "agent_id", "different-scheduler"),
            ("event", "task_id", "different-task"),
            ("event", "episode", 999),
            ("event", "returncode", 9),
            ("event", "timed_out", True),
            ("event", "cancelled", True),
            ("event", "run_horizon_reached", True),
            ("decision", "agent_id", "different-scheduler"),
            ("decision", "agent_task_id", "different-task"),
            ("decision", "agent_episode", 999),
            ("decision", "agent_returncode", 9),
            ("decision", "agent_timed_out", True),
            ("decision", "agent_cancelled", True),
            ("decision", "agent_run_horizon_reached", True),
        )
        for artifact, field, value in mutations:
            with (
                self.subTest(artifact=artifact, field=field),
                tempfile.TemporaryDirectory() as temporary,
            ):
                paths = self._clean_runs(Path(temporary))
                if artifact == "event":
                    events = _events("agent")
                    scheduler_event = next(
                        row
                        for row in events
                        if row.get("event") == "allocation_scheduler_finished"
                    )
                    scheduler_event[field] = value
                    _write_jsonl(paths["agent"] / "events.jsonl", events)
                else:
                    decisions = [
                        json.loads(line)
                        for line in (paths["agent"] / "allocation_decisions.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                    ]
                    decisions[0][field] = value
                    _write_jsonl(
                        paths["agent"] / "allocation_decisions.jsonl",
                        decisions,
                    )
                result = _run_audit(paths)
            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "allocation_scheduler_result_chain_mismatch",
                _codes(json.loads(result.stdout), "agent"),
            )

    def test_scheduler_summary_agent_calls_must_match_result_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            _write_json(
                paths["agent"] / "allocation_summary.json",
                {
                    "policy": "agent",
                    "initial_pool_size": 48,
                    "initial_assignments": 48,
                    "agent_calls": 3,
                },
            )
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "allocation_scheduler_result_chain_mismatch",
            _codes(json.loads(result.stdout), "agent"),
        )

    def test_statuses_are_trimmed_casefolded_and_proved_aliases_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            final = _final("agent")
            final["status"] = " cOmPlEtEd "
            final["verdicts"][TASKS[0]]["status"] = " passed "
            _write_json(paths["agent"] / "final.json", final)

            events = _events("agent")
            evaluation = next(
                row for row in events if row.get("event") == "evaluation_finished"
            )
            evaluation["status"] = " aC "
            run_finished = next(
                row for row in events if row.get("event") == "run_finished"
            )
            run_finished["status"] = " CoMpLeTeD "
            _write_jsonl(paths["agent"] / "events.jsonl", events)

            judge_rows = [
                json.loads(line)
                for line in (paths["agent"] / "judge_checks.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            judge_rows[0]["status"] = "ProVed"
            _write_jsonl(paths["agent"] / "judge_checks.jsonl", judge_rows)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_whitespace_mixed_case_evaluator_error_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            events = _events("formula")
            evaluation = next(
                row for row in events if row.get("event") == "evaluation_finished"
            )
            evaluation["status"] = " evaluator_Error "
            _write_jsonl(paths["formula"] / "events.jsonl", events)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "evaluator_transport_error",
            _codes(json.loads(result.stdout), "formula"),
        )

    def test_early_judge_credit_before_agent_finish_preserves_closeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            _stage_positive_uniform(paths["uniform"])
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_duplicate_positive_credit_chain_fails_exact_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            run = paths["uniform"]
            _stage_positive_uniform(run)

            trace = [
                json.loads(line)
                for line in (run / "communication_trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            trace.append(json.loads(json.dumps(trace[0])))
            _write_jsonl(run / "communication_trace.jsonl", trace)

            scoreboard = [
                json.loads(line)
                for line in (run / "scoreboard_history.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            scoreboard.append(json.loads(json.dumps(scoreboard[0])))
            _write_jsonl(run / "scoreboard_history.jsonl", scoreboard)

            events = [
                json.loads(line)
                for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            for event_name in ("judge_proof_credited", "best_candidate_promoted"):
                duplicate = next(row for row in events if row.get("event") == event_name)
                events.insert(-1, json.loads(json.dumps(duplicate)))
            _write_jsonl(run / "events.jsonl", events)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        codes = _codes(json.loads(result.stdout), "uniform")
        self.assertIn("positive_task_credited_multiple_times", codes)
        self.assertIn("positive_scoreboard_provenance_mismatch", codes)
        self.assertIn("positive_validation_provenance_mismatch", codes)
        self.assertIn("positive_promotion_provenance_mismatch", codes)
        self.assertIn("positive_proof_credit_provenance_mismatch", codes)

    def test_closeout_rejects_any_solver_extension_outside_exact_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            final = _final("uniform")
            command = final["agents"][0]["command"]
            command.extend(["--extension", "/tmp/arbitrary.mjs"])
            _write_json(paths["uniform"] / "final.json", final)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "solver_extension_allowlist_invalid",
            _codes(json.loads(result.stdout), "uniform"),
        )

    def test_fast_mode_closeout_requires_exact_two_extension_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._clean_runs(Path(temporary))
            for arm in ARMS:
                meta = _meta(arm)
                meta["fast_mode"] = True
                _write_json(paths[arm] / "run_meta.json", meta)
                final = _final(arm)
                command = final["agents"][0]["command"]
                command.extend(["--extension", "/bundle/pi_fast_mode.mjs"])
                _write_json(paths[arm] / "final.json", final)
            result = _run_audit(paths)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
