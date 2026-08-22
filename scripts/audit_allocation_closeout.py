#!/usr/bin/env python3
"""Read-only closeout audit for the three formal CPS48 allocation arms.

The report intentionally contains only arm labels, counts, issue codes, and JSON
field names.  It never copies artifact values, tool payloads, endpoints, tokens,
or absolute paths into stdout.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
import datetime as dt
import json
from pathlib import Path
import re
from typing import Any, Iterable, Iterator, Mapping


ARM_POLICIES = ("uniform", "formula", "agent")
EXPECTED_TASK_COUNT = 12
EXPECTED_MAX_PARALLEL = 48
EXPECTED_INITIAL_AGENTS_PER_TASK = 4
EXPECTED_HORIZON_SECONDS = 3600
BASE_SOLVER_TOOLS = {"read", "edit", "write", "grep", "find", "ls", "judge_check"}
CPS_SOLVER_TOOLS = {
    "cps_search",
    "cps_publish",
    "cps_inbox",
    "cps_send",
    "cps_ack",
    "cps_actors",
}
EXPECTED_SOLVER_TOOLS = BASE_SOLVER_TOOLS | CPS_SOLVER_TOOLS
REQUIRED_SOLVER_FLAGS = {
    "--no-context-files",
    "--no-skills",
    "--no-prompt-templates",
    "--no-extensions",
}
SOLVER_SYSTEM_PROMPT_MARKERS = {
    "not a general-purpose coding agent",
    "Do not execute shell commands",
    "judge_check tool",
    "never create a local or raw-network fallback",
}
SCHEDULER_SYSTEM_PROMPT_MARKERS = {
    "read-only allocation decision component",
    "You have no tools",
    "must not inspect files",
}
SHELL_NAMES = {"bash", "sh", "zsh", "fish", "dash", "ksh", "shell"}
FORBIDDEN_EVENTS = {
    "run_error",
    "elastic_worker_error",
    "preflight_failed",
    "broker_drain_timeout",
}
VOLATILE_META_FIELDS = {
    "horizon_started_at",
    "name",
    "output_root",
    "repo_root",
    "run_id",
    "started_at",
}
VOLATILE_STARTED_FIELDS = {"at", "name", "run_id"}
EVALUATOR_FIELDS = {
    "lean_env_id",
    "lean_timeout_seconds",
    "lean_max_concurrent_evaluations",
    "lean_verification_profile",
    "lean_judge_mode",
}
BAD_JUDGE_STATUSES = {
    "EVALUATOR_ERROR",
    "EVALUATOR_TIMEOUT",
    "NETWORK_ERROR",
    "PROVENANCE_INVALID",
    "REJECTED_OVERLOADED",
}
BROKER_CONTROL_STATUSES = {
    "ADMISSION_ERROR",
    "BROKER_ERROR",
    "CANDIDATE_SNAPSHOT_ERROR",
    "INVALID_REQUEST",
    "INVALID_TASK_SELECTION",
    "JUDGE_ADMISSION_ERROR",
    "JUDGE_ADMISSION_TIMEOUT",
    "SESSION_PROBE_BUDGET_EXHAUSTED",
    "SNAPSHOT_ERROR",
}
BROKER_SOFT_CONTROL_STATUSES = {
    "SESSION_PROBE_COOLDOWN",
    "SESSION_PROBE_IN_FLIGHT",
}
BROKER_NORMAL_CONTROL_STATUSES = {"OUT_OF_HORIZON", "TASK_CANCELLED"}
RUNNER_VALIDATION_AUTHORS = {"runner"}
PROVED_STATUS_ALIASES = {"PROVED", "AC", "PASS", "PASSED"}
AUTHORITATIVE_VERDICT_STATUSES = {
    "PROVED",
    "COMPILES_WITH_SORRY",
    "VERIFY_FAIL",
}

ENDPOINT_VALUE_RE = re.compile(r"(?:https?|wss?)://", re.IGNORECASE)
HOST_PORT_RE = re.compile(r"^[A-Za-z0-9_.-]+:\d+(?:/|$)")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BROKER_CLOSEOUT_SCHEMA = "contextswarm_judge_broker_closeout_v1"
SECRET_VALUE_RE = re.compile(
    r"(?:\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:authorization|api[ _-]?key|token|secret|password|credential)"
    r"\s*[:=]\s*[^\s,;\"']{4,})",
    re.IGNORECASE,
)
TRANSPORT_ERROR_RE = re.compile(
    r"(?:\b429\b|too many requests|rate[ _-]?limit|evaluator transport|"
    r"judge transport|connection (?:refused|reset|aborted)|remote disconnected)",
    re.IGNORECASE,
)
OOM_RE = re.compile(
    r"(?:out of memory|oom(?:killed| kill)?|cannot allocate memory|memory limit exceeded)",
    re.IGNORECASE,
)
LOCAL_LEAN_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:lean|lake|elan)(?![A-Za-z0-9_.-])", re.IGNORECASE)
INSTALL_RE = re.compile(
    r"(?:elan-init|toolchain\s+install|(?:apt|apt-get|pip|pip3|npm|cargo)\s+install|"
    r"lake\s+(?:update|exe\s+cache\s+get)|mathlib.*(?:download|cache))",
    re.IGNORECASE,
)
RAW_HTTP_RE = re.compile(
    r"(?:\b(?:curl|wget)\b|requests\.(?:get|post|put)|urllib\.request|fetch\s*\()",
    re.IGNORECASE,
)
HEAVY_EXECUTION_RE = re.compile(
    r"(?:xargs\s+-P|gnu\s+parallel|(?:^|\s)parallel(?:\s|$)|multiprocessing|"
    r"ThreadPoolExecutor|ProcessPoolExecutor|subprocess\.(?:Popen|run)|\bnohup\b|\bdisown\b)",
    re.IGNORECASE,
)


def _issue(code: str, *, field_name: str | None = None) -> dict[str, str]:
    item = {"code": code}
    if field_name:
        item["field"] = field_name
    return item


def _add_issue(
    issues: list[dict[str, str]],
    code: str,
    *,
    field_name: str | None = None,
) -> None:
    item = _issue(code, field_name=field_name)
    if item not in issues:
        issues.append(item)


def _load_json(
    path: Path,
    issues: list[dict[str, str]],
    *,
    missing_code: str,
    invalid_code: str,
) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _add_issue(issues, missing_code)
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        _add_issue(issues, invalid_code)
        return None
    if not isinstance(value, dict):
        _add_issue(issues, invalid_code)
        return None
    return value


def _load_jsonl(
    path: Path,
    issues: list[dict[str, str]],
    *,
    missing_code: str,
    invalid_code: str,
) -> list[dict[str, Any]]:
    try:
        handle = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        _add_issue(issues, missing_code)
        return []
    except (OSError, UnicodeError):
        _add_issue(issues, invalid_code)
        return []
    rows: list[dict[str, Any]] = []
    with handle:
        try:
            for raw in handle:
                if not raw.strip():
                    continue
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("JSONL row is not an object")
                rows.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            _add_issue(issues, invalid_code)
            return []
    return rows


def _field_path(parts: Iterable[str]) -> str:
    return ".".join(parts)


def _is_endpoint_key(key: str) -> bool:
    normalized = key.lower()
    return normalized.endswith("_url") or normalized.endswith("_endpoint") or normalized in {
        "url",
        "endpoint",
        "server",
        "host",
    }


def _is_secret_key(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized in {"token", "secret", "password", "credential", "authorization"}
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
        or normalized.endswith("_credential")
        or normalized.endswith("_api_key")
    )


def _is_redacted(value: str) -> bool:
    return value.strip().lower() in {
        "",
        "configured",
        "<configured>",
        "[configured]",
        "redacted",
        "<redacted>",
        "[redacted]",
        "set",
    }


def _sensitive_fields(value: Any, parts: tuple[str, ...] = ()) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            current = parts + (key,)
            if isinstance(item, str) and not _is_redacted(item):
                if _is_endpoint_key(key) and (
                    ENDPOINT_VALUE_RE.search(item) or HOST_PORT_RE.search(item) or item.strip()
                ):
                    yield "endpoint_leak", _field_path(current)
                elif ENDPOINT_VALUE_RE.search(item):
                    yield "endpoint_leak", _field_path(current)
                if _is_secret_key(key):
                    yield "secret_leak", _field_path(current)
                elif SECRET_VALUE_RE.search(item):
                    yield "secret_leak", _field_path(current)
            yield from _sensitive_fields(item, current)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _sensitive_fields(item, parts + (str(index),))
    elif isinstance(value, str) and not _is_redacted(value):
        path = _field_path(parts)
        if ENDPOINT_VALUE_RE.search(value):
            yield "endpoint_leak", path
        if SECRET_VALUE_RE.search(value):
            yield "secret_leak", path


def _serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _normalize_status(value: Any) -> str:
    """Canonicalize artifact statuses before every audit comparison."""

    status = str(value or "").strip().upper()
    return "PROVED" if status in PROVED_STATUS_ALIASES else status


def _option_values(command: list[str], option: str) -> list[str]:
    values: list[str] = []
    for index, token in enumerate(command):
        if token == option and index + 1 < len(command):
            values.append(command[index + 1])
        elif token.startswith(option + "="):
            values.append(token.split("=", 1)[1])
    return values


def _tool_allowlist(command: list[str]) -> set[str]:
    tools: set[str] = set()
    for value in _option_values(command, "--tools"):
        tools.update(part.strip() for part in re.split(r"[,\s]+", value) if part.strip())
    return tools


def _is_scheduler_agent(agent: Mapping[str, Any]) -> bool:
    return str(agent.get("task_id") or "") == "__allocation__" or str(
        agent.get("agent_id") or ""
    ).startswith("allocation-scheduler-")


def _check_agent_command(
    agent: Mapping[str, Any],
    issues: list[dict[str, str]],
    *,
    fast_mode: bool,
) -> None:
    raw_command = agent.get("command")
    if not isinstance(raw_command, list) or not raw_command or not all(
        isinstance(item, str) for item in raw_command
    ):
        _add_issue(issues, "solver_command_missing")
        return
    command = list(raw_command)
    system_prompts = _option_values(command, "--system-prompt")
    if _is_scheduler_agent(agent):
        if "--no-tools" not in command:
            _add_issue(issues, "scheduler_not_isolated")
        if REQUIRED_SOLVER_FLAGS.difference(command):
            _add_issue(issues, "scheduler_isolation_flags_missing")
        if _option_values(command, "--tools"):
            _add_issue(issues, "scheduler_has_tool_allowlist")
        if _option_values(command, "--extension"):
            _add_issue(issues, "scheduler_has_extension")
        if len(system_prompts) != 1 or not all(
            marker in system_prompts[0]
            for marker in SCHEDULER_SYSTEM_PROMPT_MARKERS
        ):
            _add_issue(issues, "scheduler_system_prompt_invalid")
        return

    if any(Path(token).name.lower() in SHELL_NAMES for token in command):
        _add_issue(issues, "solver_command_has_shell")
    missing_flags = REQUIRED_SOLVER_FLAGS.difference(command)
    if missing_flags:
        _add_issue(issues, "solver_isolation_flags_missing")
    if "--no-tools" in command:
        _add_issue(issues, "solver_tools_disabled")
    tools = _tool_allowlist(command)
    if tools != EXPECTED_SOLVER_TOOLS:
        _add_issue(issues, "solver_tool_allowlist_invalid")
    if tools.intersection(SHELL_NAMES):
        _add_issue(issues, "solver_tool_allowlist_has_shell")
    if len(system_prompts) != 1 or not all(
        marker in system_prompts[0]
        for marker in SOLVER_SYSTEM_PROMPT_MARKERS
    ):
        _add_issue(issues, "solver_system_prompt_invalid")
    extensions = [Path(value).name for value in _option_values(command, "--extension")]
    if "pi_solver_tools.mjs" not in extensions:
        _add_issue(issues, "solver_controlled_extension_missing")
    expected_extensions = ["pi_solver_tools.mjs"]
    if fast_mode:
        expected_extensions.append("pi_fast_mode.mjs")
    if Counter(extensions) != Counter(expected_extensions):
        _add_issue(issues, "solver_extension_allowlist_invalid")


def _walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _tool_records(value: Any) -> Iterator[tuple[str, str, Any, str]]:
    """Yield kind, tool name, payload, and call id from nested Pi session rows."""

    for item in _walk_dicts(value):
        raw_type = str(item.get("type") or "").lower().replace("_", "")
        name = item.get("toolName") or item.get("tool_name")
        if not name and "tool" in raw_type:
            name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if "result" in raw_type:
            kind = "result"
            payload = item.get("content", item.get("output", item.get("result")))
        elif "call" in raw_type or "use" in raw_type or any(
            key in item for key in ("arguments", "args", "input")
        ):
            kind = "call"
            payload = item.get("arguments", item.get("args", item.get("input")))
        else:
            continue
        call_id = str(item.get("toolCallId") or item.get("tool_call_id") or item.get("id") or "")
        yield kind, name.strip(), payload, call_id


def _scan_tool_payload(payload: Any, issues: list[dict[str, str]]) -> None:
    text = _serialized(payload)
    if LOCAL_LEAN_RE.search(text):
        _add_issue(issues, "session_local_lean_execution")
    if INSTALL_RE.search(text):
        _add_issue(issues, "session_toolchain_installation")
    if RAW_HTTP_RE.search(text):
        _add_issue(issues, "session_raw_http")
    if HEAVY_EXECUTION_RE.search(text):
        _add_issue(issues, "session_parallel_or_heavy_execution")


def _scan_sensitive_value(
    value: Any,
    issues: list[dict[str, str]],
    prefix: tuple[str, ...],
) -> None:
    for code, field_name in _sensitive_fields(value, prefix):
        _add_issue(issues, code, field_name=field_name)


def _scan_session(
    path: Path,
    issues: list[dict[str, str]],
    *,
    session_index: int,
    scheduler_session: bool,
) -> bool:
    call_names: dict[str, str] = {}
    try:
        handle = path.open("r", encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    with handle:
        try:
            for line_index, raw in enumerate(handle):
                if not raw.strip():
                    continue
                row = json.loads(raw)
                _scan_sensitive_value(
                    row,
                    issues,
                    ("session", str(session_index), str(line_index)),
                )
                for kind, name, payload, call_id in _tool_records(row):
                    normalized_name = name.lower()
                    if kind == "call":
                        if scheduler_session:
                            _add_issue(issues, "scheduler_session_tool_call")
                        if call_id:
                            call_names[call_id] = normalized_name
                        if normalized_name not in EXPECTED_SOLVER_TOOLS:
                            _add_issue(issues, "session_forbidden_tool")
                            _scan_tool_payload(payload, issues)
                    elif kind == "result":
                        associated_name = normalized_name or call_names.get(call_id, "")
                        if associated_name == "judge_check" and TRANSPORT_ERROR_RE.search(
                            _serialized(payload)
                        ):
                            _add_issue(issues, "judge_check_transport_error")
        except (OSError, UnicodeError, json.JSONDecodeError):
            _add_issue(issues, "session_invalid_jsonl")
    return True


def _scan_sessions(
    run_dir: Path,
    issues: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> tuple[dict[str, int], set[tuple[str, str, str]]]:
    index_path = run_dir / "pi_session_index.jsonl"
    if not index_path.exists():
        _add_issue(issues, "session_index_missing")
        return {"session_files_scanned": 0, "session_files_unreadable": 0}, set()
    index_issues: list[dict[str, str]] = []
    rows = _load_jsonl(
        index_path,
        index_issues,
        missing_code="session_index_missing",
        invalid_code="session_index_invalid_jsonl",
    )
    issues.extend(item for item in index_issues if item not in issues)
    root = run_dir.resolve()
    scanned = 0
    solver_scanned = 0
    scheduler_scanned = 0
    unreadable = 0
    seen: set[Path] = set()
    identities: set[tuple[str, str, str]] = set()
    for row_index, row in enumerate(rows):
        scheduler_session = str(row.get("task_id") or "") == "__allocation__"
        raw_path = row.get("session_file")
        if not isinstance(raw_path, str) or not raw_path:
            unreadable += 1
            continue
        candidate = Path(raw_path)
        if candidate.is_absolute():
            unreadable += 1
            _add_issue(issues, "session_path_outside_run")
            continue
        try:
            resolved = (root / candidate).resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            unreadable += 1
            _add_issue(issues, "session_path_outside_run")
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _scan_session(
            resolved,
            issues,
            session_index=row_index,
            scheduler_session=scheduler_session,
        ):
            scanned += 1
            if scheduler_session:
                scheduler_scanned += 1
            else:
                solver_scanned += 1
            identity = _agent_identity(row)
            if identity is None:
                _add_issue(issues, "session_identity_invalid")
            else:
                identities.add(identity)
        else:
            unreadable += 1
    if unreadable:
        _add_issue(issues, "session_files_unreadable")
    if not solver_scanned:
        _add_issue(issues, "solver_sessions_missing")
    return {
        "session_files_scanned": scanned,
        "solver_session_files_scanned": solver_scanned,
        "scheduler_session_files_scanned": scheduler_scanned,
        "session_files_unreadable": unreadable,
    }, identities


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _check_runtime_provenance(
    meta: Mapping[str, Any],
    issues: list[dict[str, str]],
) -> None:
    provenance = meta.get("runtime_provenance")
    if not isinstance(provenance, Mapping):
        _add_issue(issues, "runtime_provenance_missing")
        return
    source_commit = provenance.get("source_commit")
    if not isinstance(source_commit, str) or SOURCE_COMMIT_RE.fullmatch(source_commit) is None:
        _add_issue(
            issues,
            "runtime_source_commit_invalid",
            field_name="run_meta.runtime_provenance.source_commit",
        )
    image_id = provenance.get("image_id")
    if not isinstance(image_id, str) or IMAGE_ID_RE.fullmatch(image_id) is None:
        _add_issue(
            issues,
            "runtime_image_id_invalid",
            field_name="run_meta.runtime_provenance.image_id",
        )


def _check_broker_closeout(
    closeout: Mapping[str, Any] | None,
    issues: list[dict[str, str]],
) -> None:
    if closeout is None:
        return
    if closeout.get("schema_version") != BROKER_CLOSEOUT_SCHEMA:
        _add_issue(issues, "judge_broker_closeout_invalid")
    active_handlers = closeout.get("active_handlers")
    fifo_depth = closeout.get("fifo_depth")
    counts_valid = (
        isinstance(active_handlers, int)
        and not isinstance(active_handlers, bool)
        and active_handlers >= 0
        and isinstance(fifo_depth, int)
        and not isinstance(fifo_depth, bool)
        and fifo_depth >= 0
    )
    if not counts_valid:
        _add_issue(issues, "judge_broker_closeout_invalid")
    if (
        closeout.get("drained") is not True
        or not counts_valid
        or active_handlers != 0
        or fifo_depth != 0
    ):
        _add_issue(issues, "judge_broker_not_drained")


def _contains_running(value: Any) -> bool:
    if isinstance(value, str):
        return _normalize_status(value) == "RUNNING"
    if isinstance(value, dict):
        return any(_contains_running(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_running(item) for item in value)
    return False


def _agent_identity(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    agent_id = row.get("agent_id") or row.get("actor_id")
    task_id = row.get("task_id")
    generation = row.get("episode")
    if generation is None:
        generation = row.get("generation")
    if not isinstance(agent_id, str) or not agent_id:
        return None
    if not isinstance(task_id, str) or not task_id:
        return None
    if generation is None:
        return None
    return agent_id, task_id, str(generation)


def _event_identities(
    rows: Iterable[Mapping[str, Any]],
    issues: list[dict[str, str]],
) -> Counter[tuple[str, str, str]]:
    identities: Counter[tuple[str, str, str]] = Counter()
    for row in rows:
        identity = _agent_identity(row)
        if identity is None:
            _add_issue(issues, "agent_event_identity_invalid")
            continue
        identities[identity] += 1
    if any(count != 1 for count in identities.values()):
        _add_issue(issues, "agent_event_identity_duplicated")
    return identities


def _check_event_chain(
    events: list[dict[str, Any]],
    final_agents: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> dict[str, int]:
    assigned_rows = [row for row in events if row.get("event") == "agent_assigned"]
    admitted_rows = [row for row in events if row.get("event") == "agent_admitted"]
    admission_rows = assigned_rows or admitted_rows
    finished_rows = [row for row in events if row.get("event") == "agent_finished"]
    evaluated_rows = [row for row in events if row.get("event") == "evaluation_finished"]
    if not admission_rows or not finished_rows or not evaluated_rows:
        _add_issue(issues, "agent_event_chain_incomplete")
    admitted = _event_identities(admission_rows, issues)
    finished = _event_identities(finished_rows, issues)
    evaluated = _event_identities(evaluated_rows, issues)
    final_identities = _event_identities(final_agents, issues)
    if admitted != finished or admitted != evaluated or finished != final_identities:
        _add_issue(issues, "agent_event_chain_mismatch")
    return {
        "agent_admissions": sum(admitted.values()),
        "agent_finishes": sum(finished.values()),
        "agent_evaluations": sum(evaluated.values()),
    }


def _check_scheduler_state(
    state: Mapping[str, Any] | None,
    issues: list[dict[str, str]],
) -> None:
    if state is None:
        return
    if state.get("active_slots") != 0:
        _add_issue(issues, "scheduler_not_quiescent")
    tasks = state.get("tasks")
    if isinstance(tasks, dict) and any(
        isinstance(task, dict) and task.get("active_agents") not in (0, None)
        for task in tasks.values()
    ):
        _add_issue(issues, "scheduler_not_quiescent")
    if _contains_running(state):
        _add_issue(issues, "scheduler_running_state")


def _decision_index(row: Mapping[str, Any]) -> int | None:
    value = row.get("decision_index")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _scheduler_result_projection(
    row: Mapping[str, Any],
    *,
    decision: bool,
) -> tuple[Any, ...] | None:
    """Return the exact fields that must join for one scheduler invocation."""

    if decision:
        keys = (
            "agent_id",
            "agent_task_id",
            "agent_episode",
            "agent_returncode",
            "agent_timed_out",
            "agent_cancelled",
            "agent_run_horizon_reached",
        )
    else:
        keys = (
            "agent_id",
            "task_id",
            "episode",
            "returncode",
            "timed_out",
            "cancelled",
            "run_horizon_reached",
        )
    agent_id, task_id, episode, returncode, timed_out, cancelled, horizon = (
        row.get(key) for key in keys
    )
    if (
        not isinstance(agent_id, str)
        or not agent_id
        or not isinstance(task_id, str)
        or not task_id
        or isinstance(episode, bool)
        or not isinstance(episode, int)
        or isinstance(returncode, bool)
        or not isinstance(returncode, int)
        or not isinstance(timed_out, bool)
        or not isinstance(cancelled, bool)
        or not isinstance(horizon, bool)
    ):
        return None
    return (
        agent_id,
        task_id,
        episode,
        returncode,
        timed_out,
        cancelled,
        horizon,
    )


def _indexed_scheduler_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    decision: bool,
    issues: list[dict[str, str]],
) -> dict[int, tuple[Any, ...]]:
    indexed: dict[int, tuple[Any, ...]] = {}
    invalid = False
    for row in rows:
        index = _decision_index(row)
        projection = _scheduler_result_projection(row, decision=decision)
        if index is None or projection is None or index in indexed:
            invalid = True
            continue
        indexed[index] = projection
    if invalid:
        _add_issue(issues, "allocation_scheduler_result_chain_mismatch")
    return indexed


def _check_scheduler_agent_closeout(
    *,
    label: str,
    scheduler_agents: list[dict[str, Any]],
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    allocation_summary: Mapping[str, Any] | None,
    session_identities: set[tuple[str, str, str]],
    issues: list[dict[str, str]],
) -> None:
    scheduler_events = [
        row for row in events if row.get("event") == "allocation_scheduler_finished"
    ]
    agent_decisions = [row for row in decisions if row.get("policy") == "agent"]
    if label != "agent":
        if scheduler_agents or scheduler_events or agent_decisions:
            _add_issue(issues, "unexpected_allocation_scheduler_agent")
        return

    for row in scheduler_agents:
        if row.get("task_id") != "__allocation__":
            _add_issue(issues, "allocation_scheduler_identity_invalid")
        horizon_reached = row.get("run_horizon_reached") is True
        if horizon_reached and row.get("timed_out") is not True:
            _add_issue(issues, "allocation_scheduler_horizon_state_invalid")
        if (
            not horizon_reached
            and (
                row.get("returncode") != 0
                or row.get("timed_out") is True
                or row.get("cancelled") is True
            )
        ) or row.get("mocked") is True:
            _add_issue(issues, "allocation_scheduler_result_failed")
        if row.get("returncode") == 137 or OOM_RE.search(_serialized(row)):
            _add_issue(issues, "allocation_scheduler_oom_or_exit_137")
        _check_agent_command(row, issues, fast_mode=False)

    for row in agent_decisions:
        horizon_reached = row.get("agent_run_horizon_reached") is True
        if horizon_reached:
            valid = (
                row.get("agent_result_valid") is None
                and row.get("fallback") is False
                and str(row.get("disposition") or "") == "not_admitted_horizon"
            )
        else:
            valid = (
                row.get("agent_result_valid") is True
                and row.get("fallback") is False
            )
        if not valid:
            _add_issue(issues, "allocation_scheduler_decision_not_pure_agent")

    final_by_index = _indexed_scheduler_rows(
        scheduler_agents,
        decision=False,
        issues=issues,
    )
    event_by_index = _indexed_scheduler_rows(
        scheduler_events,
        decision=False,
        issues=issues,
    )
    decision_by_index = _indexed_scheduler_rows(
        agent_decisions,
        decision=True,
        issues=issues,
    )
    if (
        len(final_by_index) != len(scheduler_agents)
        or len(event_by_index) != len(scheduler_events)
        or len(decision_by_index) != len(agent_decisions)
        or final_by_index != event_by_index
        or final_by_index != decision_by_index
    ):
        _add_issue(issues, "allocation_scheduler_result_chain_mismatch")

    for index in set(final_by_index).intersection(event_by_index):
        final_command = next(
            (row.get("command") for row in scheduler_agents if _decision_index(row) == index),
            None,
        )
        event_command = next(
            (row.get("command") for row in scheduler_events if _decision_index(row) == index),
            None,
        )
        if final_command != event_command:
            _add_issue(issues, "allocation_scheduler_result_chain_mismatch")

    summary_agent_calls = (
        allocation_summary.get("agent_calls")
        if isinstance(allocation_summary, Mapping)
        else None
    )
    if (
        isinstance(summary_agent_calls, bool)
        or not isinstance(summary_agent_calls, int)
        or summary_agent_calls != len(scheduler_agents)
    ):
        _add_issue(issues, "allocation_scheduler_result_chain_mismatch")

    final_identities = {
        identity
        for identity in (_agent_identity(row) for row in scheduler_agents)
        if identity is not None
    }
    indexed_scheduler_sessions = {
        identity for identity in session_identities if identity[1] == "__allocation__"
    }
    if final_identities != indexed_scheduler_sessions:
        _add_issue(issues, "allocation_scheduler_session_chain_mismatch")


def _horizon_not_admitted(row: Mapping[str, Any]) -> bool:
    disposition = " ".join(
        str(row.get(key) or "")
        for key in ("disposition", "admission_disposition", "fallback_reason", "reason")
    ).lower()
    has_horizon = "horizon" in disposition or "out_of_horizon" in disposition
    has_not_admitted = any(
        phrase in disposition
        for phrase in (
            "not_admitted",
            "not admitted",
            "before admission",
            "out_of_horizon",
            "out of horizon",
        )
    )
    return has_horizon and has_not_admitted


def _check_allocation_decisions(
    rows: list[dict[str, Any]],
    admission_identities: set[tuple[str, str, str]],
    issues: list[dict[str, str]],
) -> None:
    for row in rows:
        agent_id = row.get("assigned_agent_id")
        generation = row.get("assigned_generation")
        selected_task = row.get("selected_task_id")
        if isinstance(agent_id, str) and agent_id:
            if generation is None or not isinstance(selected_task, str) or not selected_task:
                _add_issue(issues, "allocation_decision_assignment_invalid")
                continue
            identity = agent_id, selected_task, str(generation)
            if identity not in admission_identities:
                _add_issue(issues, "allocation_decision_assignment_mismatch")
        elif str(row.get("disposition") or "") == "not_admitted_stale":
            policy = str(row.get("policy") or "").strip().lower()
            pure_stale = (
                row.get("fallback") is False
                and row.get("assigned_generation") is None
            )
            if policy == "agent":
                pure_stale = pure_stale and row.get("agent_result_valid") is True
            elif policy not in {"uniform", "formula"}:
                pure_stale = False
            if not pure_stale:
                _add_issue(issues, "allocation_stale_disposition_invalid")
        elif not _horizon_not_admitted(row):
            _add_issue(issues, "allocation_decision_without_disposition")


def _provenance_value(payload: Mapping[str, Any], name: str) -> Any:
    direct = payload.get(name)
    if direct is not None:
        return direct
    provenance = payload.get("provenance")
    if isinstance(provenance, dict) and provenance.get(name) is not None:
        return provenance.get(name)
    body = payload.get("body")
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed.get(name)
    return None


def _provenance_key(
    payload: Mapping[str, Any],
    *,
    fallback_task_id: Any = None,
) -> tuple[str, str, str, str] | None:
    task_id = _provenance_value(payload, "task_id") or fallback_task_id
    candidate_hash = _provenance_value(payload, "candidate_sha256")
    contract_hash = _provenance_value(payload, "task_contract_sha256")
    judge_job_id = _provenance_value(payload, "judge_job_id")
    if (
        not isinstance(task_id, str)
        or not task_id
        or not _valid_sha256(candidate_hash)
        or not _valid_sha256(contract_hash)
        or not isinstance(judge_job_id, str)
        or not judge_job_id.strip()
    ):
        return None
    return (
        task_id,
        str(candidate_hash).lower(),
        str(contract_hash).lower(),
        judge_job_id.strip(),
    )


def _check_communication_trace(
    rows: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> tuple[int, Counter[tuple[str, str, str, str]]]:
    validation_count = 0
    provenance_keys: Counter[tuple[str, str, str, str]] = Counter()
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("kind") != "validation_result":
            continue
        validation_count += 1
        author = payload.get("author")
        actor_id = row.get("actor_id")
        if author not in RUNNER_VALIDATION_AUTHORS or (
            actor_id is not None and actor_id not in RUNNER_VALIDATION_AUTHORS
        ):
            _add_issue(issues, "validation_result_author_not_runner")
        if not _valid_sha256(_provenance_value(payload, "candidate_sha256")):
            _add_issue(issues, "validation_result_candidate_hash_missing")
        if not _valid_sha256(_provenance_value(payload, "task_contract_sha256")):
            _add_issue(issues, "validation_result_task_contract_hash_missing")
        job_id = _provenance_value(payload, "judge_job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            _add_issue(issues, "validation_result_judge_job_missing")
        if row.get("task_id") != payload.get("task_id"):
            _add_issue(issues, "validation_result_task_mismatch")
        key = _provenance_key(payload, fallback_task_id=row.get("task_id"))
        if key is None:
            _add_issue(issues, "validation_result_provenance_incomplete")
        else:
            provenance_keys[key] += 1
    if not validation_count:
        _add_issue(issues, "validation_results_missing")
    return validation_count, provenance_keys


def _judge_value_has_429(value: Any, *, semantic: bool = False) -> bool:
    if isinstance(value, dict):
        semantic_keys = {
            "status",
            "error",
            "error_code",
            "http_status",
            "status_code",
            "code",
            "response",
            "result",
            "message",
            "detail",
        }
        return any(
            _judge_value_has_429(
                item,
                semantic=semantic or str(key).lower() in semantic_keys,
            )
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_judge_value_has_429(item, semantic=semantic) for item in value)
    if not semantic:
        return False
    return value == 429 or (isinstance(value, str) and re.search(r"\b429\b", value) is not None)


def _judge_row_has_429(row: Mapping[str, Any]) -> bool:
    return _judge_value_has_429(row)


def _broker_control_class(row: Mapping[str, Any], status: str) -> str:
    if status in BROKER_NORMAL_CONTROL_STATUSES:
        return "normal"
    if status in BROKER_SOFT_CONTROL_STATUSES:
        return "soft"
    if (
        status in BROKER_CONTROL_STATUSES
        or status.startswith(
            ("ADMISSION_", "BROKER_", "CANDIDATE_SNAPSHOT_", "INVALID_", "SNAPSHOT_")
        )
    ):
        return "hard"
    if row.get("accepted") is not True:
        return "hard"
    return "none"


def _check_judge_checks(
    rows: list[dict[str, Any]],
    task_ids: set[str],
    issues: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> tuple[set[tuple[str, str, str, str]], int, int, int]:
    accepted_keys: set[tuple[str, str, str, str]] = set()
    hard_control_failures = 0
    soft_controls = 0
    normal_controls = 0
    if not rows:
        _add_issue(issues, "judge_checks_empty")
        return accepted_keys, hard_control_failures, soft_controls, normal_controls
    for row in rows:
        status = _normalize_status(row.get("status"))
        if status in BAD_JUDGE_STATUSES or _judge_row_has_429(row):
            _add_issue(issues, "judge_check_failure_status")
        control_class = _broker_control_class(row, status)
        if control_class == "hard":
            hard_control_failures += 1
            _add_issue(issues, "judge_check_control_failure")
        elif control_class == "soft":
            soft_controls += 1
            _add_issue(warnings, "judge_check_soft_control")
        elif control_class == "normal":
            normal_controls += 1
        if row.get("accepted") is True and not _valid_sha256(row.get("candidate_sha256")):
            _add_issue(issues, "judge_check_candidate_hash_missing")
        if row.get("event") != "judge_check":
            _add_issue(issues, "judge_check_record_invalid")
        if task_ids and row.get("task_id") not in task_ids:
            _add_issue(issues, "judge_check_task_mismatch")
        if row.get("accepted") is True and control_class == "none":
            if not _valid_sha256(row.get("task_contract_sha256")):
                _add_issue(issues, "judge_check_task_contract_hash_missing")
            job_id = row.get("judge_job_id")
            if not isinstance(job_id, str) or not job_id.strip():
                _add_issue(issues, "judge_check_judge_job_missing")
            key = _provenance_key(row)
            if key is None:
                _add_issue(issues, "judge_check_provenance_incomplete")
            else:
                accepted_keys.add(key)
    return accepted_keys, hard_control_failures, soft_controls, normal_controls


def _final_provenance_keys(
    verdicts: Mapping[str, Any],
    issues: list[dict[str, str]],
) -> tuple[
    set[tuple[str, str, str, str]],
    set[tuple[str, str, str, str]],
]:
    keys: set[tuple[str, str, str, str]] = set()
    cache_reused_keys: set[tuple[str, str, str, str]] = set()
    for task_id, raw_verdict in verdicts.items():
        if not isinstance(raw_verdict, Mapping):
            continue
        if raw_verdict.get("task_id") not in (None, task_id):
            _add_issue(issues, "final_verdict_task_mismatch")
        status = _normalize_status(raw_verdict.get("status"))
        if status not in AUTHORITATIVE_VERDICT_STATUSES:
            continue
        key = _provenance_key(raw_verdict, fallback_task_id=task_id)
        if key is None:
            _add_issue(issues, "final_provenance_incomplete")
        else:
            keys.add(key)
            if raw_verdict.get("cache_reused") is True:
                cache_reused_keys.add(key)
    return keys, cache_reused_keys


def _evaluation_provenance_keys(
    events: Iterable[Mapping[str, Any]],
    issues: list[dict[str, str]],
) -> tuple[
    Counter[tuple[str, str, str, str]],
    set[tuple[str, str, str, str]],
]:
    keys: Counter[tuple[str, str, str, str]] = Counter()
    cache_reused_keys: set[tuple[str, str, str, str]] = set()
    for row in events:
        if row.get("event") != "evaluation_finished":
            continue
        status = _normalize_status(row.get("status"))
        if status not in AUTHORITATIVE_VERDICT_STATUSES:
            continue
        key = _provenance_key(row, fallback_task_id=row.get("task_id"))
        if key is None:
            _add_issue(issues, "evaluation_provenance_incomplete")
        else:
            keys[key] += 1
            if row.get("cache_reused") is True:
                cache_reused_keys.add(key)
    return keys, cache_reused_keys


def _has_positive_score(payload: Mapping[str, Any]) -> bool:
    score = _provenance_value(payload, "score")
    return (
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and float(score) > 0.0
    )


def _positive_final_keys(
    verdicts: Mapping[str, Any],
    issues: list[dict[str, str]],
) -> Counter[tuple[str, str, str, str]]:
    keys: Counter[tuple[str, str, str, str]] = Counter()
    for task_id, raw_verdict in verdicts.items():
        if not isinstance(raw_verdict, Mapping) or not _has_positive_score(raw_verdict):
            continue
        key = _provenance_key(raw_verdict, fallback_task_id=task_id)
        if key is None:
            _add_issue(issues, "positive_final_provenance_incomplete")
        else:
            keys[key] += 1
    return keys


def _positive_event_keys(
    rows: Iterable[Mapping[str, Any]],
    *,
    event_name: str | None,
    issues: list[dict[str, str]],
    incomplete_code: str,
) -> Counter[tuple[str, str, str, str]]:
    keys: Counter[tuple[str, str, str, str]] = Counter()
    for row in rows:
        if event_name is not None and row.get("event") != event_name:
            continue
        if not _has_positive_score(row):
            continue
        key = _provenance_key(row, fallback_task_id=row.get("task_id"))
        if key is None:
            _add_issue(issues, incomplete_code)
        else:
            keys[key] += 1
    return keys


def _positive_validation_keys(
    rows: Iterable[Mapping[str, Any]],
    issues: list[dict[str, str]],
) -> Counter[tuple[str, str, str, str]]:
    keys: Counter[tuple[str, str, str, str]] = Counter()
    for row in rows:
        payload = row.get("payload")
        if (
            not isinstance(payload, Mapping)
            or payload.get("kind") != "validation_result"
            or not _has_positive_score(payload)
        ):
            continue
        key = _provenance_key(payload, fallback_task_id=row.get("task_id"))
        if key is None:
            _add_issue(issues, "positive_validation_provenance_incomplete")
        else:
            keys[key] += 1
    return keys


def _proof_credit_keys(
    events: Iterable[Mapping[str, Any]],
    issues: list[dict[str, str]],
) -> Counter[tuple[str, str, str, str]]:
    keys: Counter[tuple[str, str, str, str]] = Counter()
    for row in events:
        if row.get("event") != "judge_proof_credited":
            continue
        key = _provenance_key(row, fallback_task_id=row.get("task_id"))
        if key is None:
            _add_issue(issues, "judge_proof_credit_provenance_incomplete")
        else:
            keys[key] += 1
    return keys


def _check_positive_exact_once(
    *,
    verdicts: Mapping[str, Any],
    events: list[dict[str, Any]],
    scoreboard: list[dict[str, Any]],
    communication_trace: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> dict[str, int]:
    finals = _positive_final_keys(verdicts, issues)
    evaluations = _positive_event_keys(
        events,
        event_name="evaluation_finished",
        issues=issues,
        incomplete_code="positive_evaluation_provenance_incomplete",
    )
    scored = _positive_event_keys(
        scoreboard,
        event_name=None,
        issues=issues,
        incomplete_code="positive_scoreboard_provenance_incomplete",
    )
    validations = _positive_validation_keys(communication_trace, issues)
    promotions = _positive_event_keys(
        events,
        event_name="best_candidate_promoted",
        issues=issues,
        incomplete_code="positive_promotion_provenance_incomplete",
    )
    early_evaluations: Counter[tuple[str, str, str, str]] = Counter()
    for row in events:
        if (
            row.get("event") == "evaluation_finished"
            and row.get("source") == "judge_check"
            and _has_positive_score(row)
        ):
            key = _provenance_key(row, fallback_task_id=row.get("task_id"))
            if key is not None:
                early_evaluations[key] += 1
    credits = _proof_credit_keys(events, issues)

    for actual, code in (
        (evaluations, "positive_final_evaluation_provenance_mismatch"),
        (scored, "positive_scoreboard_provenance_mismatch"),
        (validations, "positive_validation_provenance_mismatch"),
        (promotions, "positive_promotion_provenance_mismatch"),
    ):
        if actual != finals:
            _add_issue(issues, code)
    if credits != early_evaluations:
        _add_issue(issues, "positive_proof_credit_provenance_mismatch")

    for counter in (finals, evaluations, scored, validations, promotions, credits):
        by_task: Counter[str] = Counter()
        for key, count in counter.items():
            by_task[key[0]] += count
        if any(count > 1 for count in by_task.values()):
            _add_issue(issues, "positive_task_credited_multiple_times")
            break
    return {
        "positive_final_verdicts": sum(finals.values()),
        "positive_evaluations": sum(evaluations.values()),
        "positive_scoreboard_rows": sum(scored.values()),
        "positive_validations": sum(validations.values()),
        "positive_promotions": sum(promotions.values()),
        "positive_proof_credits": sum(credits.values()),
    }


def _check_provenance_links(
    *,
    accepted_judge: set[tuple[str, str, str, str]],
    validations: Counter[tuple[str, str, str, str]],
    finals: set[tuple[str, str, str, str]],
    evaluations: Counter[tuple[str, str, str, str]],
    cache_reused_finals: set[tuple[str, str, str, str]],
    cache_reused_evaluations: set[tuple[str, str, str, str]],
    issues: list[dict[str, str]],
) -> None:
    if validations != evaluations:
        _add_issue(issues, "validation_evaluation_provenance_mismatch")
    if finals - set(validations):
        _add_issue(issues, "final_validation_provenance_unlinked")
    if finals - set(evaluations):
        _add_issue(issues, "final_evaluation_provenance_unlinked")
    if cache_reused_evaluations - accepted_judge:
        _add_issue(issues, "cache_reused_evaluation_probe_unlinked")
    if cache_reused_finals - accepted_judge:
        _add_issue(issues, "cache_reused_final_probe_unlinked")


def _normalize_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(dict(meta))
    for key in VOLATILE_META_FIELDS:
        normalized.pop(key, None)
    allocation = normalized.get("allocation")
    if isinstance(allocation, dict):
        allocation.pop("policy", None)
    return normalized


def _normalize_started(started: Mapping[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(dict(started))
    for key in VOLATILE_STARTED_FIELDS:
        normalized.pop(key, None)
    allocation = normalized.get("allocation")
    if isinstance(allocation, dict):
        allocation.pop("policy", None)
    return normalized


def _diff_paths(left: Any, right: Any, prefix: str = "") -> list[str]:
    if type(left) is not type(right):
        return [prefix or "$type"]
    if isinstance(left, dict):
        paths: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                paths.append(child)
            else:
                paths.extend(_diff_paths(left[key], right[key], child))
        return paths
    if isinstance(left, list):
        if len(left) != len(right):
            return [prefix + ".length" if prefix else "length"]
        paths = []
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            child = f"{prefix}.{index}" if prefix else str(index)
            paths.extend(_diff_paths(left_item, right_item, child))
        return paths
    return [] if left == right else [prefix or "$value"]


@dataclass
class ArmAudit:
    label: str
    run_dir: Path
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict, repr=False)
    started: dict[str, Any] = field(default_factory=dict, repr=False)
    tasks: tuple[str, ...] = field(default_factory=tuple, repr=False)
    started_at: dt.datetime | None = field(default=None, repr=False)
    horizon_started_at: dt.datetime | None = field(default=None, repr=False)
    finished_at: dt.datetime | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.errors,
            "counts": dict(sorted(self.counts.items())),
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _utc_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _audit_arm(label: str, run_dir: Path) -> ArmAudit:
    audit = ArmAudit(label, run_dir)
    if not run_dir.is_dir():
        _add_issue(audit.errors, "run_directory_missing")
        return audit

    meta = _load_json(
        run_dir / "run_meta.json",
        audit.errors,
        missing_code="run_meta_missing",
        invalid_code="run_meta_invalid_json",
    )
    final = _load_json(
        run_dir / "final.json",
        audit.errors,
        missing_code="final_missing",
        invalid_code="final_invalid_json",
    )
    allocation_summary = _load_json(
        run_dir / "allocation_summary.json",
        audit.errors,
        missing_code="allocation_summary_missing",
        invalid_code="allocation_summary_invalid_json",
    )
    events = _load_jsonl(
        run_dir / "events.jsonl",
        audit.errors,
        missing_code="events_missing",
        invalid_code="events_invalid_jsonl",
    )
    scheduler_state = _load_json(
        run_dir / "elastic_scheduler_state.json",
        audit.errors,
        missing_code="scheduler_state_missing",
        invalid_code="scheduler_state_invalid_json",
    )
    broker_closeout = _load_json(
        run_dir / "judge_broker_closeout.json",
        audit.errors,
        missing_code="judge_broker_closeout_missing",
        invalid_code="judge_broker_closeout_invalid_json",
    )
    allocation_decisions = _load_jsonl(
        run_dir / "allocation_decisions.jsonl",
        audit.errors,
        missing_code="allocation_decisions_missing",
        invalid_code="allocation_decisions_invalid_jsonl",
    )
    communication_trace = _load_jsonl(
        run_dir / "communication_trace.jsonl",
        audit.errors,
        missing_code="communication_trace_missing",
        invalid_code="communication_trace_invalid_jsonl",
    )
    judge_checks = _load_jsonl(
        run_dir / "judge_checks.jsonl",
        audit.errors,
        missing_code="judge_checks_missing",
        invalid_code="judge_checks_invalid_jsonl",
    )

    # These artifacts either embed CPS text/model decisions or retain
    # intermediate evaluator payloads.  They are not all needed for the
    # structural joins below, but they must still be inside the same
    # fail-closed sensitive-data audit surface.
    scoreboard_path = run_dir / "scoreboard_history.jsonl"
    scoreboard = (
        _load_jsonl(
            scoreboard_path,
            audit.errors,
            missing_code="scoreboard_history_missing",
            invalid_code="scoreboard_history_invalid_jsonl",
        )
        if scoreboard_path.exists()
        else []
    )
    preflight_path = run_dir / "transport_preflight.json"
    preflight = (
        _load_json(
            preflight_path,
            audit.errors,
            missing_code="transport_preflight_missing",
            invalid_code="transport_preflight_invalid_json",
        )
        if preflight_path.exists()
        else None
    )

    if final is not None:
        _scan_sensitive_value(final, audit.errors, ("final",))
    if allocation_summary is not None:
        _scan_sensitive_value(allocation_summary, audit.errors, ("allocation_summary",))
    if scheduler_state is not None:
        _scan_sensitive_value(scheduler_state, audit.errors, ("scheduler_state",))
    if broker_closeout is not None:
        _scan_sensitive_value(
            broker_closeout,
            audit.errors,
            ("judge_broker_closeout",),
        )
    for index, event in enumerate(events):
        _scan_sensitive_value(event, audit.errors, ("events", str(index)))
    for index, decision in enumerate(allocation_decisions):
        _scan_sensitive_value(
            decision,
            audit.errors,
            ("allocation_decisions", str(index)),
        )
    for index, event in enumerate(communication_trace):
        _scan_sensitive_value(
            event,
            audit.errors,
            ("communication_trace", str(index)),
        )
    for index, row in enumerate(judge_checks):
        _scan_sensitive_value(row, audit.errors, ("judge_checks", str(index)))
    for index, row in enumerate(scoreboard):
        _scan_sensitive_value(row, audit.errors, ("scoreboard_history", str(index)))
    if preflight is not None:
        _scan_sensitive_value(preflight, audit.errors, ("transport_preflight",))

    if meta is not None:
        audit.meta = meta
        _scan_sensitive_value(meta, audit.errors, ("run_meta",))
        _check_runtime_provenance(meta, audit.errors)
        expected_meta = {
            "mode": "cps",
            "communication": "blackboard",
            "max_parallel": EXPECTED_MAX_PARALLEL,
            "initial_agents_per_task": EXPECTED_INITIAL_AGENTS_PER_TASK,
            "max_tasks": 0,
            "time_limit_seconds": EXPECTED_HORIZON_SECONDS,
            "lean_require_result_cache_disabled": True,
        }
        for key, expected in expected_meta.items():
            if meta.get(key) != expected:
                _add_issue(audit.errors, "cps48_contract_invalid", field_name=f"run_meta.{key}")
        if not isinstance(meta.get("model"), str) or not meta.get("model"):
            _add_issue(audit.errors, "model_contract_missing", field_name="run_meta.model")
        runtime_limits = meta.get("effective_runtime_limits")
        if (
            not isinstance(runtime_limits, dict)
            or not isinstance(runtime_limits.get("memory_max_bytes"), int)
            or not isinstance(runtime_limits.get("pids_max"), int)
            or runtime_limits.get("process_uid") in (None, 0)
            or runtime_limits.get("process_gid") in (None, 0)
        ):
            _add_issue(audit.errors, "effective_runtime_limits_invalid")
        for key in sorted(EVALUATOR_FIELDS):
            if key not in meta:
                _add_issue(
                    audit.errors,
                    "evaluator_contract_missing",
                    field_name=f"run_meta.{key}",
                )
        allocation = meta.get("allocation")
        if not isinstance(allocation, dict) or allocation.get("policy") != label:
            _add_issue(audit.errors, "allocation_policy_invalid", field_name="run_meta.allocation.policy")
        audit.started_at = _utc_timestamp(meta.get("started_at"))
        audit.horizon_started_at = _utc_timestamp(meta.get("horizon_started_at"))
        if audit.started_at is None:
            _add_issue(audit.errors, "run_timestamp_invalid", field_name="run_meta.started_at")
        if audit.horizon_started_at is None:
            _add_issue(
                audit.errors,
                "run_timestamp_invalid",
                field_name="run_meta.horizon_started_at",
            )

    final_tasks: tuple[str, ...] = ()
    final_agents: list[dict[str, Any]] = []
    scheduler_agents: list[dict[str, Any]] = []
    verdicts: dict[str, Any] = {}
    if final is not None:
        if _normalize_status(final.get("status")) != "COMPLETED" or not final.get(
            "finished_at"
        ):
            _add_issue(audit.errors, "final_incomplete")
        if final.get("mode") != "cps" or final.get("communication") != "blackboard":
            _add_issue(audit.errors, "final_protocol_invalid")
        if final.get("horizon_seconds") != EXPECTED_HORIZON_SECONDS:
            _add_issue(audit.errors, "final_horizon_invalid")
        audit.finished_at = _utc_timestamp(final.get("finished_at"))
        if audit.finished_at is None:
            _add_issue(audit.errors, "run_timestamp_invalid", field_name="final.finished_at")
        verdicts = final.get("verdicts")
        if not isinstance(verdicts, dict):
            _add_issue(audit.errors, "final_verdicts_invalid")
            verdicts = {}
        final_tasks = tuple(sorted(str(task_id) for task_id in verdicts))
        if len(final_tasks) != EXPECTED_TASK_COUNT or final.get("max_score") != EXPECTED_TASK_COUNT:
            _add_issue(audit.errors, "task_bundle_incomplete")
        health = final.get("health")
        if not isinstance(health, dict) or health.get("ok") is not True:
            _add_issue(audit.errors, "run_health_failed_or_missing")
        for verdict in verdicts.values():
            if not isinstance(verdict, dict):
                _add_issue(audit.errors, "final_verdicts_invalid")
                continue
            if _normalize_status(verdict.get("status")) == "RUNNING":
                _add_issue(audit.errors, "running_state_present")
            verdict_status = _normalize_status(verdict.get("status"))
            if verdict_status in BAD_JUDGE_STATUSES or TRANSPORT_ERROR_RE.search(
                _serialized(verdict)
            ):
                _add_issue(audit.errors, "evaluator_transport_error")
            if verdict_status in AUTHORITATIVE_VERDICT_STATUSES:
                if not _valid_sha256(verdict.get("candidate_sha256")):
                    _add_issue(audit.errors, "final_candidate_hash_missing")
                if not _valid_sha256(verdict.get("task_contract_sha256")):
                    _add_issue(audit.errors, "final_task_contract_hash_missing")
                if not isinstance(verdict.get("judge_job_id"), str) or not verdict.get("judge_job_id"):
                    _add_issue(audit.errors, "final_judge_job_missing")

        agents = final.get("agents")
        if not isinstance(agents, list) or not agents:
            _add_issue(audit.errors, "solver_agents_missing")
            agents = []
        final_agents = [agent for agent in agents if isinstance(agent, dict)]
        audit.counts["solver_agents"] = len(agents)
        for agent in agents:
            if not isinstance(agent, dict):
                _add_issue(audit.errors, "solver_agent_record_invalid")
                continue
            if agent.get("mocked"):
                _add_issue(audit.errors, "mock_solver_present")
            returncode = agent.get("returncode")
            tail_text = _serialized(
                {"error_tail": agent.get("error_tail"), "output_tail": agent.get("output_tail")}
            )
            if returncode == 137 or OOM_RE.search(tail_text):
                _add_issue(audit.errors, "solver_oom_or_exit_137")
            if returncode == -9 and not agent.get("cancelled") and not agent.get("timed_out"):
                _add_issue(audit.errors, "solver_unexpected_sigkill")
            if returncode not in (0, None) and not agent.get("cancelled") and not agent.get("timed_out"):
                _add_issue(audit.errors, "solver_process_error")
            _check_agent_command(
                agent,
                audit.errors,
                fast_mode=bool(meta and meta.get("fast_mode") is True),
            )

        raw_scheduler_agents = final.get("allocation_scheduler_agents")
        if not isinstance(raw_scheduler_agents, list) or not all(
            isinstance(item, dict) for item in raw_scheduler_agents
        ):
            _add_issue(audit.errors, "allocation_scheduler_agents_invalid")
            raw_scheduler_agents = []
        scheduler_agents = list(raw_scheduler_agents)
        audit.counts["allocation_scheduler_agents"] = len(scheduler_agents)

        final_allocation = final.get("allocation")
        if not isinstance(final_allocation, dict):
            _add_issue(audit.errors, "final_allocation_missing")
        else:
            if final_allocation.get("policy") != label:
                _add_issue(audit.errors, "final_allocation_policy_invalid")
            if final_allocation.get("initial_pool_size") != EXPECTED_MAX_PARALLEL:
                _add_issue(audit.errors, "cps48_initial_pool_invalid")
            if final_allocation.get("initial_assignments") != EXPECTED_MAX_PARALLEL:
                _add_issue(audit.errors, "cps48_initial_assignments_invalid")
        if not isinstance(final.get("cps"), dict) or final["cps"].get("db") != "cps.sqlite3":
            _add_issue(audit.errors, "cps_summary_invalid")
        cache_evidence = final.get("judge_result_cache")
        if (
            not isinstance(cache_evidence, dict)
            or cache_evidence.get("required_disabled") is not True
            or cache_evidence.get("enabled") is not False
            or cache_evidence.get("backend_ready") is not True
            or cache_evidence.get("requested_env_accepted") is not True
        ):
            _add_issue(audit.errors, "judge_result_cache_evidence_invalid", field_name="final.judge_result_cache")

    if (
        audit.started_at is not None
        and audit.horizon_started_at is not None
        and audit.finished_at is not None
        and not (audit.started_at <= audit.horizon_started_at <= audit.finished_at)
    ):
        _add_issue(audit.errors, "run_timestamp_order_invalid")

    if allocation_summary is not None and allocation_summary.get("policy") != label:
        _add_issue(audit.errors, "allocation_summary_policy_invalid")
    if preflight is None:
        _add_issue(audit.errors, "transport_preflight_missing")
    else:
        lean_preflight = preflight.get("lean")
        result_cache = (
            lean_preflight.get("result_cache")
            if isinstance(lean_preflight, dict)
            else None
        )
        if (
            preflight.get("status") != "ok"
            or not isinstance(result_cache, dict)
            or result_cache.get("enabled") is not False
            or result_cache.get("backend_ready") is not True
            or result_cache.get("requested_env_accepted") is not True
        ):
            _add_issue(
                audit.errors,
                "judge_result_cache_evidence_invalid",
                field_name="transport_preflight.lean.result_cache.enabled",
            )
    _check_broker_closeout(broker_closeout, audit.errors)
    if not (run_dir / "cps.sqlite3").is_file():
        _add_issue(audit.errors, "cps_database_missing")

    started_rows = [row for row in events if row.get("event") == "run_started"]
    finished_rows = [row for row in events if row.get("event") == "run_finished"]
    if len(started_rows) != 1:
        _add_issue(audit.errors, "run_started_invalid")
    else:
        audit.started = started_rows[0]
        raw_tasks = started_rows[0].get("tasks")
        if isinstance(raw_tasks, list) and all(isinstance(item, str) for item in raw_tasks):
            audit.tasks = tuple(raw_tasks)
        else:
            _add_issue(audit.errors, "run_started_tasks_invalid")
        if started_rows[0].get("task_count") != EXPECTED_TASK_COUNT or len(audit.tasks) != EXPECTED_TASK_COUNT:
            _add_issue(audit.errors, "task_bundle_incomplete")
    if len(finished_rows) != 1 or not events or events[-1].get("event") != "run_finished":
        _add_issue(audit.errors, "run_finished_missing_or_not_final")
    elif _normalize_status(finished_rows[0].get("status")) != "COMPLETED":
        _add_issue(audit.errors, "run_finished_incomplete")
    if final_tasks and audit.tasks and set(final_tasks) != set(audit.tasks):
        _add_issue(audit.errors, "task_bundle_mismatch")
    for event in events:
        event_name = str(event.get("event") or "")
        if event_name in FORBIDDEN_EVENTS:
            _add_issue(audit.errors, event_name)
        if event_name == "evaluation_finished" and (
            _normalize_status(event.get("status")) in BAD_JUDGE_STATUSES
            or TRANSPORT_ERROR_RE.search(_serialized(event))
        ):
            _add_issue(audit.errors, "evaluator_transport_error")
        if (
            event_name == "evaluation_finished"
            and _normalize_status(event.get("status")) == "RUNNING"
        ):
            _add_issue(audit.errors, "running_state_present")
        if event_name == "agent_finished":
            if event.get("returncode") == 137 or OOM_RE.search(_serialized(event)):
                _add_issue(audit.errors, "solver_oom_or_exit_137")

    audit.counts.update(_check_event_chain(events, final_agents, audit.errors))
    admission_rows = [row for row in events if row.get("event") == "agent_assigned"] or [
        row for row in events if row.get("event") == "agent_admitted"
    ]
    admission_identities = {
        identity
        for identity in (_agent_identity(row) for row in admission_rows)
        if identity is not None
    }
    _check_scheduler_state(scheduler_state, audit.errors)
    _check_allocation_decisions(
        allocation_decisions,
        admission_identities,
        audit.errors,
    )
    task_ids = set(audit.tasks or final_tasks)
    validation_count, validation_keys = _check_communication_trace(
        communication_trace,
        audit.errors,
    )
    (
        accepted_judge_keys,
        hard_control_failures,
        soft_controls,
        normal_controls,
    ) = _check_judge_checks(
        judge_checks,
        task_ids,
        audit.errors,
        audit.warnings,
    )
    final_keys, cache_reused_final_keys = _final_provenance_keys(
        verdicts,
        audit.errors,
    )
    evaluation_keys, cache_reused_evaluation_keys = _evaluation_provenance_keys(
        events,
        audit.errors,
    )
    _check_provenance_links(
        accepted_judge=accepted_judge_keys,
        validations=validation_keys,
        finals=final_keys,
        evaluations=evaluation_keys,
        cache_reused_finals=cache_reused_final_keys,
        cache_reused_evaluations=cache_reused_evaluation_keys,
        issues=audit.errors,
    )
    audit.counts.update(
        _check_positive_exact_once(
            verdicts=verdicts,
            events=events,
            scoreboard=scoreboard,
            communication_trace=communication_trace,
            issues=audit.errors,
        )
    )
    audit.counts["allocation_decisions"] = len(allocation_decisions)
    audit.counts["validation_results"] = validation_count
    audit.counts["judge_checks"] = len(judge_checks)
    audit.counts["accepted_judge_provenance_keys"] = len(accepted_judge_keys)
    audit.counts["validation_provenance_keys"] = len(validation_keys)
    audit.counts["evaluation_provenance_keys"] = len(evaluation_keys)
    audit.counts["final_provenance_keys"] = len(final_keys)
    audit.counts["judge_control_failures"] = hard_control_failures
    audit.counts["judge_soft_controls"] = soft_controls
    audit.counts["judge_normal_controls"] = normal_controls
    audit.counts["tasks"] = len(audit.tasks or final_tasks)
    session_counts, session_identities = _scan_sessions(
        run_dir,
        audit.errors,
        audit.warnings,
    )
    audit.counts.update(session_counts)
    _check_scheduler_agent_closeout(
        label=label,
        scheduler_agents=scheduler_agents,
        events=events,
        decisions=allocation_decisions,
        allocation_summary=allocation_summary,
        session_identities=session_identities,
        issues=audit.errors,
    )
    return audit


def _cross_arm_issues(audits: Mapping[str, ArmAudit]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    usable = [audits[label] for label in ARM_POLICIES if audits[label].meta and audits[label].started]
    if len(usable) != len(ARM_POLICIES):
        return [{"code": "cross_arm_contract_unavailable"}]
    reference = usable[0]
    mismatch_fields: set[str] = set()
    reference_meta = _normalize_meta(reference.meta)
    reference_started = _normalize_started(reference.started)
    reference_tasks = reference.tasks
    for current in usable[1:]:
        mismatch_fields.update(
            f"run_meta.{path}"
            for path in _diff_paths(reference_meta, _normalize_meta(current.meta))
        )
        mismatch_fields.update(
            f"run_started.{path}"
            for path in _diff_paths(reference_started, _normalize_started(current.started))
        )
        if current.tasks != reference_tasks:
            mismatch_fields.add("run_started.tasks")
    if mismatch_fields:
        issues.append(
            {
                "code": "cross_arm_contract_mismatch",
                "fields": sorted(mismatch_fields)[:100],
            }
        )
    ordered = [audits[label] for label in ARM_POLICIES]
    if all(item.started_at is not None and item.finished_at is not None for item in ordered):
        for previous, current in zip(ordered, ordered[1:]):
            assert previous.finished_at is not None and current.started_at is not None
            if previous.finished_at > current.started_at:
                issues.append(
                    {
                        "code": "cross_arm_run_order_overlap",
                        "previous": previous.label,
                        "current": current.label,
                    }
                )
    return issues


def audit_allocation_closeout(paths: Mapping[str, Path]) -> dict[str, Any]:
    audits = {label: _audit_arm(label, paths[label]) for label in ARM_POLICIES}
    cross_arm_errors = _cross_arm_issues(audits)
    ok = not cross_arm_errors and all(not audit.errors for audit in audits.values())
    return {
        "schema_version": "contextswarm_allocation_closeout_audit_v1",
        "ok": ok,
        "arms": {label: audits[label].public_dict() for label in ARM_POLICIES},
        "cross_arm": {"ok": not cross_arm_errors, "errors": cross_arm_errors},
    }


def audit_single_allocation_closeout(policy: str, run_dir: Path) -> dict[str, Any]:
    if policy not in ARM_POLICIES:
        raise ValueError(f"unsupported allocation policy: {policy}")
    audit = _audit_arm(policy, run_dir)
    return {
        "schema_version": "contextswarm_allocation_closeout_audit_v1",
        "ok": not audit.errors,
        "arms": {policy: audit.public_dict()},
        "cross_arm": {"ok": True, "errors": [], "skipped": True},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only closeout audit for uniform/formula/agent CPS48 runs."
    )
    for label in ARM_POLICIES:
        parser.add_argument(f"--{label}", type=Path, metavar="RUN_DIR")
    parser.add_argument("--single-policy", choices=ARM_POLICIES)
    parser.add_argument("--single-run", type=Path, metavar="RUN_DIR")
    args = parser.parse_args()
    arm_paths = {label: getattr(args, label) for label in ARM_POLICIES}
    single_requested = args.single_policy is not None or args.single_run is not None
    if single_requested:
        if args.single_policy is None or args.single_run is None:
            parser.error("--single-policy and --single-run must be provided together")
        if any(path is not None for path in arm_paths.values()):
            parser.error("single-arm and three-arm audit arguments are mutually exclusive")
        report = audit_single_allocation_closeout(args.single_policy, args.single_run)
    else:
        missing = [f"--{label}" for label, path in arm_paths.items() if path is None]
        if missing:
            parser.error(f"three-arm audit requires {', '.join(missing)}")
        report = audit_allocation_closeout(
            {label: path for label, path in arm_paths.items() if path is not None}
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
