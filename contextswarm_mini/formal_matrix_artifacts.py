"""Fail-closed eligibility checks for formal Figure 4 run artifacts.

The matrix supervisor and the offline collector must agree on what counts as
an arm that really finished.  In particular, a terminal ``DEGRADED`` file is
diagnostic evidence, never a usable result.  Keeping this small check in the
runtime package avoids having the supervisor and collector grow subtly
different adoption rules.

This module only reads public run artifacts.  It never opens credentials,
``node.toml``, or endpoint configuration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


REQUIRED_LIFECYCLE_EVENTS = frozenset(
    {
        "run_started",
        "horizon_started",
        "closeout_finished",
        "judge_broker_closed",
        "selection_runtime_closed",
        "run_finished",
    }
)

# These counters represent candidate-independent failures or an incomplete
# scheduler/broker lifecycle.  Candidate timeouts and ordinary cancellations
# are deliberately not included: they are valid attempt outcomes under the
# fixed formal contract.
_INFRASTRUCTURE_COUNTER_PARTS = (
    "infrastructure_error",
    "provider_error",
    "process_error",
    "unexpected_process_error",
    "call_id_error",
    "oom_or_exit_137",
    "nonzero_return",
    "policy_timeout",
    "reservation_leak",
)
_ERROR_MARKERS = (
    "upstream request failed",
    "account is unavailable",
    "oauth",
    "provider transport",
    "coordinator transport",
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _read_events(path: Path) -> tuple[set[str], list[str]]:
    events: set[str] = set()
    errors: list[str] = []
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return events, ["events.jsonl is unreadable"]
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"events.jsonl line {line_number} is invalid JSON")
                continue
            if not isinstance(value, Mapping):
                errors.append(f"events.jsonl line {line_number} is not an object")
                continue
            event = value.get("event")
            if isinstance(event, str) and event:
                events.add(event)
            # Error tails are emitted by the runner as structured strings.  A
            # marker elsewhere in a normal event is not sufficient; only
            # inspect fields that conventionally carry an error/diagnostic.
            for key in ("error", "error_tail", "reason", "message", "exception"):
                item = value.get(key)
                if isinstance(item, str):
                    lowered = item.lower()
                    for marker in _ERROR_MARKERS:
                        if marker in lowered:
                            errors.append(f"{key}:{marker}")
    return events, errors


def _positive_counter(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return False


def artifact_eligibility(run_dir: str | Path, *, policy: str | None = None) -> tuple[bool, list[str]]:
    """Return ``(eligible, reasons)`` for one completed formal arm.

    The reasons are stable, non-sensitive diagnostics suitable for a matrix
    report.  A caller must not treat a score or ``DEGRADED`` status as a
    substitute for this check.
    """

    directory = Path(run_dir)
    reasons: list[str] = []
    final = _read_json(directory / "final.json")
    meta = _read_json(directory / "run_meta.json")
    summary = _read_json(directory / "figure4_run_summary.json")
    closeout = _read_json(directory / "judge_broker_closeout.json")
    preflight = _read_json(directory / "transport_preflight.json")

    if not isinstance(final, Mapping):
        reasons.append("final_missing_or_malformed")
    elif final.get("status") != "COMPLETED":
        reasons.append(f"final_status:{final.get('status', 'missing')}")

    health = final.get("health") if isinstance(final, Mapping) else None
    if not isinstance(health, Mapping) or health.get("ok") is not True:
        reasons.append("health_not_ok")
    else:
        issues = health.get("issues")
        if issues not in ([], None):
            reasons.append("health_has_issues")
        for key, value in health.items():
            if any(part in str(key).lower() for part in _INFRASTRUCTURE_COUNTER_PARTS):
                if _positive_counter(value):
                    reasons.append(f"health_counter:{key}")

    if not isinstance(meta, Mapping):
        reasons.append("run_meta_missing_or_malformed")
    else:
        horizon_started = meta.get("horizon_started_at")
        if not isinstance(horizon_started, str) or not horizon_started.strip():
            reasons.append("horizon_not_started")
        provenance = meta.get("runtime_provenance")
        if not isinstance(provenance, Mapping):
            reasons.append("runtime_provenance_missing")
        elif not all(
            isinstance(provenance.get(key), str) and provenance.get(key).strip()
            for key in ("image_id", "manifest_sha256", "source_commit")
        ):
            reasons.append("runtime_provenance_incomplete")
        if policy is not None:
            allocation = meta.get("allocation")
            if isinstance(allocation, Mapping) and allocation.get("policy") != policy:
                reasons.append("allocation_policy_mismatch")

    if not isinstance(summary, Mapping):
        reasons.append("figure4_summary_missing_or_malformed")
    elif policy is not None and summary.get("policy") != policy:
        reasons.append("summary_policy_mismatch")

    if not isinstance(closeout, Mapping):
        reasons.append("judge_closeout_missing_or_malformed")
    else:
        if closeout.get("drained") is not True:
            reasons.append("judge_closeout_not_drained")
        for key in ("active_handlers", "fifo_depth", "remote_unsettled_jobs"):
            value = closeout.get(key)
            if _positive_counter(value):
                reasons.append(f"judge_closeout_nonzero:{key}")

    if not isinstance(preflight, Mapping) or preflight.get("status") != "ok":
        reasons.append("transport_preflight_not_ok")
    elif not isinstance(preflight.get("aisw"), Mapping) or not str(
        preflight["aisw"].get("nurouter_version", "")
    ).strip():
        reasons.append("transport_version_missing")

    events, event_errors = _read_events(directory / "events.jsonl")
    missing_events = sorted(REQUIRED_LIFECYCLE_EVENTS - events)
    if missing_events:
        reasons.append("lifecycle_missing:" + ",".join(missing_events))
    for item in event_errors:
        reasons.append("event_error:" + item)

    # Preserve order while removing duplicate reasons; this keeps reports
    # deterministic when a malformed artifact trips several checks.
    unique: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            unique.append(reason)
    return not unique, unique


__all__ = ["REQUIRED_LIFECYCLE_EVENTS", "artifact_eligibility"]
