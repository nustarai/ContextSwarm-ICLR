"""Immutable same-state allocation audits and Figure 4 metric artifacts.

This module intentionally has no dependency on allocator implementations.  A
runner supplies one frozen snapshot and the two hypothetical choices; the
helpers validate the resulting integer vectors before publishing JSONL/JSON.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .artifacts import append_jsonl, atomic_write_json

AUDIT_SCHEMA = "contextswarm_allocation_audit_v1"
RUN_SCHEMA = "contextswarm_figure4_run_summary_v1"
PAIRED_SCHEMA = "contextswarm_figure4_paired_repeat_v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _frozen_map(value: Mapping[str, Any], *, integer: bool = False) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("expected mapping")
    out: dict[str, Any] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError("mapping keys must be non-empty strings")
        if integer:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ValueError(f"allocation for {key!r} must be a non-negative integer")
            out[key] = raw
        else:
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError(f"score for {key!r} must be finite")
            out[key] = number
    return MappingProxyType(out)


def _same_keys(*maps: Mapping[str, Any]) -> None:
    if not maps:
        return
    keys = set(maps[0])
    if any(set(item) != keys for item in maps[1:]):
        raise ValueError("audit task maps must contain exactly the same task IDs")


def _vector_after(before: Mapping[str, int], selected: str) -> dict[str, int]:
    result = dict(before)
    if selected:
        if selected not in result:
            raise ValueError("selected task is absent from allocation vector")
        result[selected] += 1
    return result


def _validate_capacity(total: int, active: int, reserved: int, free: int) -> None:
    for name, value in (("total_capacity", total), ("active", active), ("reserved", reserved), ("free", free)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if active + reserved + free != total:
        raise ValueError("active + reserved + free must equal total capacity")


@dataclass(frozen=True)
class AllocationAuditRecord:
    """One admitted Trace-State dispatch and its same-state counterfactual."""

    state_id: str
    decision_id: str
    eligible_task_ids: tuple[str, ...]
    allocation_config_sha256: str
    task_only_scores: Mapping[str, float]
    trace_increments: Mapping[str, float]
    trace_total_scores: Mapping[str, float]
    allocation_before: Mapping[str, int]
    trace_state_allocation_after: Mapping[str, int]
    task_state_allocation_after: Mapping[str, int]
    trace_state_selected_task_id: str
    task_state_selected_task_id: str
    admitted_task_id: str
    fallback_reason: str
    active_slots_before: int
    active_slots_after: int
    free_slots_before: int
    free_slots_after: int
    scheduler_reserved_slots_before: int
    scheduler_reserved_slots_after: int
    total_capacity: int
    capacity_delta_sum: int = 0
    capacity_conserved: bool = True

    @classmethod
    def create(cls, **kwargs: Any) -> "AllocationAuditRecord":
        before = _frozen_map(kwargs.pop("allocation_before"), integer=True)
        task_scores = _frozen_map(kwargs.pop("task_only_scores"))
        increments = _frozen_map(kwargs.pop("trace_increments"))
        totals = _frozen_map(kwargs.pop("trace_total_scores"))
        _same_keys(task_scores, increments, totals)
        tasks = tuple(str(x) for x in kwargs.pop("eligible_task_ids"))
        if not tasks or any(not task for task in tasks) or len(set(tasks)) != len(tasks):
            raise ValueError("eligible_task_ids must be non-empty, unique task IDs")
        if not set(tasks).issubset(before):
            raise ValueError("eligible task is absent from allocation vector")
        if set(task_scores) != set(tasks):
            raise ValueError("score maps must contain exactly the eligible task IDs")
        trace_selected = str(kwargs.pop("trace_state_selected_task_id", "") or "")
        task_selected = str(kwargs.pop("task_state_selected_task_id", "") or "")
        admitted = str(kwargs.pop("admitted_task_id", "") or "")
        if not admitted:
            raise ValueError("audit rows are emitted only for admitted Trace-State decisions")
        if admitted != trace_selected:
            raise ValueError("admitted task must equal Trace-State selected task")
        trace_after = _frozen_map(kwargs.pop("trace_state_allocation_after", _vector_after(before, admitted)), integer=True)
        task_after = _frozen_map(kwargs.pop("task_state_allocation_after", _vector_after(before, task_selected)), integer=True)
        _same_keys(before, trace_after, task_after)
        if dict(trace_after) != _vector_after(before, admitted):
            raise ValueError("Trace-State after vector must be before plus its admitted one-hot")
        if dict(task_after) != _vector_after(before, task_selected):
            raise ValueError("Task-State after vector must be before plus its selected one-hot")
        for key, value in totals.items():
            if abs(value - (task_scores[key] + increments[key])) > 1e-12:
                raise ValueError("trace_total_scores must equal task_only_scores + trace_increments")
        if trace_selected not in tasks or task_selected not in tasks:
            raise ValueError("both selected tasks must be eligible")
        def best(scores: Mapping[str, float]) -> str:
            peak = max(scores[key] for key in tasks)
            return min(key for key in tasks if scores[key] == peak)
        if best(totals) != trace_selected or best(task_scores) != task_selected:
            raise ValueError("selected tasks must be the lexicographic score argmax")
        total = kwargs.pop("total_capacity")
        _validate_capacity(total, kwargs.get("active_slots_before"), kwargs.get("scheduler_reserved_slots_before"), kwargs.get("free_slots_before"))
        _validate_capacity(total, kwargs.get("active_slots_after"), kwargs.get("scheduler_reserved_slots_after"), kwargs.get("free_slots_after"))
        if sum(before.values()) != kwargs["active_slots_before"] or sum(trace_after.values()) != kwargs["active_slots_after"] or sum(task_after.values()) != kwargs["active_slots_after"]:
            raise ValueError("allocation vectors do not match active slot counts")
        if kwargs["active_slots_after"] != kwargs["active_slots_before"] + 1 or kwargs["free_slots_after"] != kwargs["free_slots_before"] - 1 or kwargs["scheduler_reserved_slots_after"] != kwargs["scheduler_reserved_slots_before"]:
            raise ValueError("an admitted audit row must consume exactly one free solver slot")
        delta = sum(trace_after[k] - task_after[k] for k in before)
        if delta != 0:
            raise ValueError("trace/task allocation delta must conserve capacity")
        if not _SHA256.fullmatch(str(kwargs.get("state_id", ""))) or not _SHA256.fullmatch(str(kwargs.get("allocation_config_sha256", ""))):
            raise ValueError("state_id and allocation_config_sha256 must be SHA-256 hex strings")
        record = cls(
            state_id=str(kwargs.pop("state_id")), decision_id=str(kwargs.pop("decision_id")),
            eligible_task_ids=tasks, allocation_config_sha256=str(kwargs.pop("allocation_config_sha256")),
            task_only_scores=task_scores, trace_increments=increments, trace_total_scores=totals,
            allocation_before=before, trace_state_allocation_after=trace_after, task_state_allocation_after=task_after,
            trace_state_selected_task_id=trace_selected, task_state_selected_task_id=task_selected,
            admitted_task_id=admitted, fallback_reason=str(kwargs.pop("fallback_reason", "") or ""),
            active_slots_before=kwargs.pop("active_slots_before"), active_slots_after=kwargs.pop("active_slots_after"),
            free_slots_before=kwargs.pop("free_slots_before"), free_slots_after=kwargs.pop("free_slots_after"),
            scheduler_reserved_slots_before=kwargs.pop("scheduler_reserved_slots_before"), scheduler_reserved_slots_after=kwargs.pop("scheduler_reserved_slots_after"),
            total_capacity=total, capacity_delta_sum=0, capacity_conserved=True,
        )
        if kwargs:
            raise ValueError(f"unknown audit fields: {sorted(kwargs)}")
        return record

    def as_dict(self) -> dict[str, Any]:
        return {"schema_version": AUDIT_SCHEMA, "state_id": self.state_id, "decision_id": self.decision_id,
                "eligible_task_ids": list(self.eligible_task_ids), "allocation_config_sha256": self.allocation_config_sha256,
                "task_only_scores": dict(self.task_only_scores), "trace_increments": dict(self.trace_increments), "trace_total_scores": dict(self.trace_total_scores),
                "allocation_before": dict(self.allocation_before), "trace_state_allocation_after": dict(self.trace_state_allocation_after), "task_state_allocation_after": dict(self.task_state_allocation_after),
                "trace_state_selected_task_id": self.trace_state_selected_task_id, "task_state_selected_task_id": self.task_state_selected_task_id, "admitted_task_id": self.admitted_task_id, "fallback_reason": self.fallback_reason,
                "active_slots_before": self.active_slots_before, "active_slots_after": self.active_slots_after, "free_slots_before": self.free_slots_before, "free_slots_after": self.free_slots_after,
                "scheduler_reserved_slots_before": self.scheduler_reserved_slots_before, "scheduler_reserved_slots_after": self.scheduler_reserved_slots_after, "total_capacity": self.total_capacity,
                "capacity_delta_sum": self.capacity_delta_sum, "capacity_conserved": self.capacity_conserved}

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "AllocationAuditRecord":
        if row.get("schema_version") != AUDIT_SCHEMA:
            raise ValueError("unsupported allocation audit schema")
        if row.get("capacity_delta_sum") != 0 or row.get("capacity_conserved") is not True:
            raise ValueError("invalid emitted capacity-conservation result")
        data = dict(row); data.pop("schema_version", None); data.pop("capacity_delta_sum"); data.pop("capacity_conserved")
        return cls.create(**data)


def append_allocation_audit(path: Path, record: AllocationAuditRecord) -> None:
    append_jsonl(path, record.as_dict())


def read_allocation_audits(path: Path, *, expected_config_sha256: str | None = None, expected_state_ids: Iterable[str] | None = None) -> list[AllocationAuditRecord]:
    expected = set(expected_state_ids or ())
    rows: list[AllocationAuditRecord] = []
    try: lines = path.read_text(encoding="utf-8").splitlines()
    except OSError: return rows
    for index, line in enumerate(lines, 1):
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result: raise ValueError(f"duplicate JSON key {key!r}")
                result[key] = value
            return result
        try: raw = json.loads(line, object_pairs_hook=unique_object, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON number {value}")))
        except (json.JSONDecodeError, ValueError) as exc: raise ValueError(f"malformed audit JSONL line {index}") from exc
        if not isinstance(raw, Mapping): raise ValueError(f"audit line {index} is not an object")
        record = AllocationAuditRecord.from_dict(raw)
        if expected_config_sha256 and record.allocation_config_sha256 != expected_config_sha256: raise ValueError(f"stale config at line {index}")
        if expected and record.state_id not in expected: raise ValueError(f"stale state at line {index}")
        rows.append(record)
    return rows


def validate_capacity_conservation(record: AllocationAuditRecord) -> bool:
    """Revalidate an already-created/decoded record, failing closed."""
    if record.capacity_delta_sum != 0 or record.capacity_conserved is not True:
        raise ValueError("invalid emitted capacity-conservation result")
    return AllocationAuditRecord.create(**{k: v for k, v in record.as_dict().items() if k not in {"schema_version", "capacity_delta_sum", "capacity_conserved"}}) is not None


def _nauc(history: Sequence[Mapping[str, Any]], horizon: float, max_score: float) -> tuple[float, float | None, dict[str, float]]:
    if not math.isfinite(horizon) or not math.isfinite(max_score) or horizon <= 0 or max_score <= 0: raise ValueError("invalid horizon/max_score")
    points = [(float(row["elapsed_seconds"]), float(row.get("accepted_score", row.get("score", 0)))) for row in history]
    previous, score, area = 0.0, 0.0, 0.0
    first: float | None = None; times: dict[str, float] = {}
    for elapsed, value in points:
        if not math.isfinite(elapsed) or not math.isfinite(value) or elapsed < previous or elapsed < 0 or elapsed > horizon or value < score or value < 0 or value > max_score:
            raise ValueError("accepted score history must be finite, bounded, and monotonic")
        area += score * (elapsed - previous); score = value; previous = elapsed
        if score > 0 and first is None: first = elapsed
        for k in range(1, int(score) + 1): times.setdefault(str(k), elapsed)
    area += score * max(0.0, horizon - previous)
    return (area / (horizon * max_score) if horizon and max_score else 0.0), first, times


def build_figure4_run_summary(*, run_id: str, policy: str, paired_seed: int | str, repeat: int | str, comparison_contract_id: str = "", comparison_contract: Mapping[str, Any] | None = None, task_order: Sequence[str] = (), horizon_seconds: float, total_capacity: int, initial_allocation: Mapping[str, int], accepted_score_history: Sequence[Mapping[str, Any]], max_score: float = 1.0, solver_usage: Mapping[str, Any] | None = None, evaluator_usage: Mapping[str, Any] | None = None, scheduler_cost: Mapping[str, Any] | None = None, allocation_metrics: Mapping[str, Any] | None = None, allocation_parameters: Mapping[str, Any] | None = None, allocation_config_sha256: str = "") -> dict[str, Any]:
    nauc, first, times = _nauc(accepted_score_history, float(horizon_seconds), float(max_score))
    history = [dict(item) for item in accepted_score_history]
    final = max((float(item.get("accepted_score", item.get("score", 0))) for item in history), default=0.0)
    scheduler = dict(scheduler_cost or {})
    scheduler.setdefault("calls", 0); scheduler.setdefault("input_tokens", 0); scheduler.setdefault("output_tokens", 0); scheduler.setdefault("total_tokens", int(scheduler["input_tokens"]) + int(scheduler["output_tokens"])); scheduler.setdefault("latency_seconds", 0.0); scheduler.setdefault("reserved_slot_seconds", scheduler.get("occupied_capacity_slot_seconds", 0.0))
    alloc = dict(allocation_metrics or {}); alloc.setdefault("decisions", 0); alloc.setdefault("fallbacks", alloc.get("fallback_decisions", 0)); alloc.setdefault("fallback_rate", alloc["fallbacks"] / alloc["decisions"] if alloc["decisions"] else 0.0)
    contract = dict(comparison_contract or {})
    contract_hash = _canonical_sha256(contract)
    return {"schema_version": RUN_SCHEMA, "run_id": str(run_id), "policy": str(policy), "paired_seed": paired_seed, "repeat": repeat, "paired_repeat_id": str(repeat), "comparison_contract_id": comparison_contract_id or contract_hash, "comparison_contract": contract, "task_order": list(task_order), "horizon_seconds": float(horizon_seconds), "total_capacity": int(total_capacity), "initial_allocation": dict(initial_allocation), "accepted_score_history": history, "final_accepted_score": final, "max_score": float(max_score), "time_to_k": times, "time_to_k_seconds": times, "nauc": nauc, "solver_usage": dict(solver_usage or {}), "evaluator_usage": dict(evaluator_usage or {}), "scheduler_cost": scheduler, "llm_scheduler_cost": scheduler, "allocation_metrics": alloc, "allocation_decisions": alloc["decisions"], "fallback_decisions": alloc["fallbacks"], "fallback_rate": alloc["fallback_rate"], "allocation_parameters": dict(allocation_parameters or {}), "allocation_config_sha256": allocation_config_sha256, "comparison_contract_sha256": contract_hash}


def write_figure4_run_summary(path: Path, summary: Mapping[str, Any]) -> None:
    if summary.get("schema_version") != RUN_SCHEMA: raise ValueError("invalid Figure 4 summary schema")
    atomic_write_json(path, summary)


def build_figure4_paired_repeat(*, paired_repeat_id: str, paired_seed: int | str, arms: Mapping[str, Mapping[str, Any]], comparison_contract: Mapping[str, Any] | None = None, registered_contrasts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    required = {"uniform_refill", "task_state", "trace_state", "llm_scheduler"}
    if set(arms) != required: raise ValueError("paired repeat must contain exactly the four Figure 4 arms")
    arm_rows = {str(k): dict(v) for k, v in arms.items()}
    for policy, row in arm_rows.items():
        if row.get("policy", policy) != policy:
            raise ValueError("paired arm policy does not match its key")
        if "nauc" not in row or "final_accepted_score" not in row:
            raise ValueError("paired arm is missing bootstrap metrics")
    contrasts = dict(registered_contrasts or {})
    if not contrasts:
        for name, left, right in (
            ("trace_state_minus_task_state", "trace_state", "task_state"),
            ("task_state_minus_uniform_refill", "task_state", "uniform_refill"),
            ("trace_state_minus_uniform_refill", "trace_state", "uniform_refill"),
            ("llm_scheduler_minus_trace_state", "llm_scheduler", "trace_state"),
        ):
            contrasts[name] = {
                metric: float(arm_rows[left][metric]) - float(arm_rows[right][metric])
                for metric in ("nauc", "final_accepted_score")
            }
    contract = dict(comparison_contract or {})
    return {"schema_version": PAIRED_SCHEMA, "paired_repeat_id": str(paired_repeat_id), "paired_seed": paired_seed, "comparison_contract": contract, "comparison_contract_sha256": _canonical_sha256(contract), "arms": arm_rows, "registered_contrasts": contrasts}


def append_figure4_paired_repeat(path: Path, row: Mapping[str, Any]) -> None:
    if row.get("schema_version") != PAIRED_SCHEMA: raise ValueError("invalid paired repeat schema")
    append_jsonl(path, row)


__all__ = ["AllocationAuditRecord", "append_allocation_audit", "read_allocation_audits", "validate_capacity_conservation", "build_figure4_run_summary", "write_figure4_run_summary", "build_figure4_paired_repeat", "append_figure4_paired_repeat"]
