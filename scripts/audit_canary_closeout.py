#!/usr/bin/env python3
"""Fail-closed, value-free closeout audit for the real controlled-Judge canary."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


EXPECTED_TOOLS = {
    "read",
    "edit",
    "write",
    "grep",
    "find",
    "ls",
    "judge_check",
    "cps_search",
    "cps_publish",
    "cps_inbox",
    "cps_send",
    "cps_ack",
    "cps_actors",
}
REQUIRED_FLAGS = {
    "--no-context-files",
    "--no-skills",
    "--no-prompt-templates",
    "--no-extensions",
}
SYSTEM_PROMPT_MARKERS = {
    "not a general-purpose coding agent",
    "Do not execute shell commands",
    "judge_check tool",
    "never create a local or raw-network fallback",
}
SHELL_NAMES = {"bash", "sh", "zsh", "fish", "dash", "ksh", "shell"}
BAD_EVENTS = {
    "run_error",
    "elastic_worker_error",
    "preflight_failed",
    "broker_drain_timeout",
    "broker_close_error",
    "broker_closeout_artifact_error",
}
BAD_STATUSES = {
    "EVALUATOR_ERROR",
    "EVALUATOR_TIMEOUT",
    "NETWORK_ERROR",
    "PROVENANCE_INVALID",
    "REJECTED_OVERLOADED",
    "BROKER_ERROR",
    "JUDGE_ADMISSION_ERROR",
    "JUDGE_ADMISSION_TIMEOUT",
    "CANDIDATE_SNAPSHOT_ERROR",
    "SESSION_PROBE_BUDGET_EXHAUSTED",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OVERLOAD_RE = re.compile(r"(?:\b429\b|too many requests|rate[ _-]?limit)", re.IGNORECASE)
OOM_RE = re.compile(
    r"(?:out of memory|oom(?:killed| kill)?|cannot allocate memory|memory limit exceeded)",
    re.IGNORECASE,
)


@dataclass
class Audit:
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    remote_delete_observed: bool = False

    def add(self, code: str, field_name: str | None = None) -> None:
        issue = {"code": code}
        if field_name:
            issue["field"] = field_name
        if issue not in self.errors:
            self.errors.append(issue)

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "contextswarm_controlled_judge_canary_audit_v1",
            "ok": not self.errors,
            "counts": dict(sorted(self.counts.items())),
            "remote_delete_observed": self.remote_delete_observed,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _load_json(path: Path, audit: Audit, name: str) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        audit.add(f"{name}_missing_or_invalid")
        return None
    if not isinstance(value, dict):
        audit.add(f"{name}_missing_or_invalid")
        return None
    return value


def _load_jsonl(path: Path, audit: Audit, name: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        audit.add(f"{name}_missing_or_invalid")
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            audit.add(f"{name}_missing_or_invalid")
            return []
        if not isinstance(value, dict):
            audit.add(f"{name}_missing_or_invalid")
            return []
        rows.append(value)
    return rows


def _serialized(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _option_value(command: list[str], option: str) -> str | None:
    indices = [index for index, value in enumerate(command) if value == option]
    if len(indices) != 1 or indices[0] + 1 >= len(command):
        return None
    return command[indices[0] + 1]


def _check_solver_command(agent: Mapping[str, Any], audit: Audit) -> None:
    command = agent.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) for item in command
    ):
        audit.add("solver_command_invalid")
        return
    executable_names = {Path(item).name.lower() for item in command}
    if executable_names & SHELL_NAMES or "--no-tools" in command:
        audit.add("solver_shell_or_tool_contract_invalid")
    if not REQUIRED_FLAGS.issubset(command):
        audit.add("solver_isolation_flags_missing")
    if _option_value(command, "--mode") != "rpc" or "--approve" not in command:
        audit.add("solver_rpc_contract_invalid")
    tools_value = _option_value(command, "--tools")
    tools = {item.strip() for item in (tools_value or "").split(",") if item.strip()}
    if tools != EXPECTED_TOOLS:
        audit.add("solver_tool_allowlist_invalid")
    extensions = [
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--extension"
    ]
    if len(extensions) != 1 or Path(extensions[0]).name != "pi_solver_tools.mjs":
        audit.add("solver_extension_allowlist_invalid")
    system_prompt = _option_value(command, "--system-prompt") or ""
    if not all(marker in system_prompt for marker in SYSTEM_PROMPT_MARKERS):
        audit.add("solver_system_prompt_invalid")


def _walk_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _delete_observed(*values: Any) -> bool:
    for value in values:
        for item in _walk_mappings(value):
            cancellation = item.get("judge_cancellation")
            if isinstance(cancellation, Mapping) and cancellation.get("attempted") is True:
                return True
    return False


def audit_canary(run_dir: Path) -> dict[str, Any]:
    audit = Audit()
    if not run_dir.is_dir():
        audit.add("run_directory_missing")
        return audit.public_dict()

    meta = _load_json(run_dir / "run_meta.json", audit, "run_meta")
    final = _load_json(run_dir / "final.json", audit, "final")
    preflight = _load_json(run_dir / "transport_preflight.json", audit, "transport_preflight")
    closeout = _load_json(
        run_dir / "judge_broker_closeout.json", audit, "judge_broker_closeout"
    )
    events = _load_jsonl(run_dir / "events.jsonl", audit, "events")
    judge_checks = _load_jsonl(run_dir / "judge_checks.jsonl", audit, "judge_checks")

    if meta is not None:
        expected = {
            "mode": "cps",
            "communication": "blackboard",
            "max_parallel": 1,
            "initial_agents_per_task": 1,
            "max_attempts_per_task": 1,
            "max_tasks": 1,
            "time_limit_seconds": 180,
            "lean_require_result_cache_disabled": True,
        }
        for key, value in expected.items():
            if meta.get(key) != value:
                audit.add("canary_contract_invalid", f"run_meta.{key}")
        allocation = meta.get("allocation")
        if not isinstance(allocation, Mapping) or allocation.get("policy") != "uniform":
            audit.add("canary_contract_invalid", "run_meta.allocation.policy")
        provenance = meta.get("runtime_provenance")
        if not isinstance(provenance, Mapping):
            audit.add("runtime_provenance_missing")
        else:
            if SOURCE_COMMIT_RE.fullmatch(str(provenance.get("source_commit") or "")) is None:
                audit.add("runtime_source_commit_invalid")
            if IMAGE_ID_RE.fullmatch(str(provenance.get("image_id") or "")) is None:
                audit.add("runtime_image_id_invalid")
            if provenance.get("test_only") is True:
                audit.add("mock_runtime_provenance_present")

    agents: list[Mapping[str, Any]] = []
    if final is not None:
        if final.get("status") != "COMPLETED" or final.get("finished_at") in (None, ""):
            audit.add("final_incomplete")
        health = final.get("health")
        if (
            not isinstance(health, Mapping)
            or health.get("ok") is not True
            or bool(health.get("issues"))
        ):
            audit.add("final_health_failed")
        raw_agents = final.get("agents")
        if not isinstance(raw_agents, list) or not raw_agents:
            audit.add("solver_agents_missing")
        else:
            agents = [item for item in raw_agents if isinstance(item, Mapping)]
            if len(agents) != len(raw_agents):
                audit.add("solver_agent_record_invalid")
        for agent in agents:
            if agent.get("mocked") is not False:
                audit.add("mock_solver_present")
            if agent.get("returncode") not in (0, None) and not agent.get("cancelled") and not agent.get("timed_out"):
                audit.add("solver_process_error")
            if agent.get("returncode") == 137 or OOM_RE.search(_serialized(agent)):
                audit.add("solver_oom_or_exit_137")
            _check_solver_command(agent, audit)
        verdicts = final.get("verdicts")
        if not isinstance(verdicts, Mapping) or len(verdicts) != 1:
            audit.add("canary_task_bundle_invalid")
        cache_evidence = final.get("judge_result_cache")
        if (
            not isinstance(cache_evidence, Mapping)
            or cache_evidence.get("required_disabled") is not True
            or cache_evidence.get("enabled") is not False
            or cache_evidence.get("backend_ready") is not True
            or cache_evidence.get("requested_env_accepted") is not True
        ):
            audit.add("judge_result_cache_evidence_invalid")

    if preflight is not None:
        if preflight.get("status") != "ok":
            audit.add("transport_preflight_failed")
        lean = preflight.get("lean")
        if not isinstance(lean, Mapping) or lean.get("requested_env_accepted") is False:
            audit.add("transport_preflight_lean_invalid")
        elif (
            not isinstance(lean.get("result_cache"), Mapping)
            or lean["result_cache"].get("enabled") is not False
            or lean["result_cache"].get("backend_ready") is not True
            or lean["result_cache"].get("requested_env_accepted") is not True
        ):
            audit.add("judge_result_cache_evidence_invalid")

    if closeout is not None:
        if (
            closeout.get("schema_version") != "contextswarm_judge_broker_closeout_v1"
            or closeout.get("drained") is not True
            or closeout.get("active_handlers") != 0
            or closeout.get("fifo_depth") != 0
        ):
            audit.add("judge_broker_not_drained")

    accepted = [row for row in judge_checks if row.get("accepted") is True]
    audit.counts["judge_checks"] = len(judge_checks)
    audit.counts["accepted_judge_checks"] = len(accepted)
    audit.counts["solver_agents"] = len(agents)
    if not accepted:
        audit.add("accepted_judge_check_missing")
    for row in accepted:
        if (
            not _valid_sha256(row.get("candidate_sha256"))
            or not _valid_sha256(row.get("task_contract_sha256"))
            or not isinstance(row.get("judge_job_id"), str)
            or not row.get("judge_job_id")
            or not isinstance(row.get("task_id"), str)
            or not row.get("task_id")
        ):
            audit.add("accepted_judge_check_provenance_invalid")
        if str(row.get("status") or "").upper() in BAD_STATUSES:
            audit.add("accepted_judge_check_failed")

    for event in events:
        if str(event.get("event") or "") in BAD_EVENTS:
            audit.add("runner_worker_or_broker_error")
    combined = _serialized(
        {"events": events, "judge_checks": judge_checks, "final": final or {}}
    )
    if OVERLOAD_RE.search(combined):
        audit.add("judge_overload_or_429")
    if OOM_RE.search(combined):
        audit.add("oom_observed")
    audit.remote_delete_observed = _delete_observed(events, judge_checks, final or {})
    return audit.public_dict()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit one real controlled-Judge canary without printing artifact values."
    )
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    report = audit_canary(args.run_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
