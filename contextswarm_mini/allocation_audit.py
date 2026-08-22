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
_MAX_AUDIT_IDENTIFIER_CHARS = 512
_MAX_AUDIT_REASON_CHARS = 1_000
_FIGURE4_POLICIES = frozenset(
    {"uniform_refill", "task_state", "trace_state", "llm_scheduler"}
)


def _audit_text(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
    max_chars: int = _MAX_AUDIT_IDENTIFIER_CHARS,
) -> str:
    """Validate an artifact identifier/reason without lossy coercion.

    Audit rows are persisted evidence.  Converting ``None``/integers (or a
    scalar string supplied where a JSON array is expected) to text can make a
    malformed row look valid and, worse, change which task a counterfactual
    appears to describe.  Keep this boundary deliberately stricter than the
    legacy compatibility APIs: callers must provide actual strings.
    """

    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > max_chars:
        raise ValueError(f"{name} is too long")
    if not allow_empty and not value:
        raise ValueError(f"{name} must be non-empty")
    if value != value.strip():
        raise ValueError(f"{name} must not have surrounding whitespace")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _audit_text_list(value: Any, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    """Return a deterministic tuple from a JSON-array-like ID field."""

    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list or tuple of strings")
    result = tuple(_audit_text(item, f"{name} item") for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{name} must be non-empty")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must contain unique strings")
    return result


def _frozen_map(value: Mapping[str, Any], *, integer: bool = False) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("expected mapping")
    out: dict[str, Any] = {}
    for key, raw in value.items():
        _audit_text(key, "mapping key")
        if integer:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ValueError(f"allocation for {key!r} must be a non-negative integer")
            out[key] = raw
        else:
            # JSON numbers decode as int/float.  Do not accept numeric strings
            # or booleans here: coercion would let a malformed artifact alter
            # score ordering while still passing validation.
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"score for {key!r} must be a finite number")
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
        # Validate identity fields before any derived vectors are built.  In
        # particular, never use ``str(value)`` here: ``None`` and integers are
        # malformed artifact values, not alternate spellings of an ID.
        state_id = _audit_text(kwargs.pop("state_id", None), "state_id")
        decision_id = _audit_text(kwargs.pop("decision_id", None), "decision_id")
        config_hash = _audit_text(
            kwargs.pop("allocation_config_sha256", None),
            "allocation_config_sha256",
        )
        if _SHA256.fullmatch(state_id) is None or _SHA256.fullmatch(config_hash) is None:
            raise ValueError("state_id and allocation_config_sha256 must be SHA-256 hex strings")
        before = _frozen_map(kwargs.pop("allocation_before"), integer=True)
        task_scores = _frozen_map(kwargs.pop("task_only_scores"))
        increments = _frozen_map(kwargs.pop("trace_increments"))
        totals = _frozen_map(kwargs.pop("trace_total_scores"))
        _same_keys(task_scores, increments, totals)
        tasks = _audit_text_list(kwargs.pop("eligible_task_ids"), "eligible_task_ids")
        if not set(tasks).issubset(before):
            raise ValueError("eligible task is absent from allocation vector")
        if set(task_scores) != set(tasks):
            raise ValueError("score maps must contain exactly the eligible task IDs")
        trace_selected = _audit_text(
            kwargs.pop("trace_state_selected_task_id", ""),
            "trace_state_selected_task_id",
        )
        task_selected = _audit_text(
            kwargs.pop("task_state_selected_task_id", ""),
            "task_state_selected_task_id",
        )
        admitted = _audit_text(kwargs.pop("admitted_task_id", ""), "admitted_task_id")
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
        fallback_reason = _audit_text(
            kwargs.pop("fallback_reason", ""),
            "fallback_reason",
            allow_empty=True,
            max_chars=_MAX_AUDIT_REASON_CHARS,
        )
        record = cls(
            state_id=state_id, decision_id=decision_id,
            eligible_task_ids=tasks, allocation_config_sha256=config_hash,
            task_only_scores=task_scores, trace_increments=increments, trace_total_scores=totals,
            allocation_before=before, trace_state_allocation_after=trace_after, task_state_allocation_after=task_after,
            trace_state_selected_task_id=trace_selected, task_state_selected_task_id=task_selected,
            admitted_task_id=admitted, fallback_reason=fallback_reason,
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
                "capacity_delta_sum": 0, "capacity_conserved": True}

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "AllocationAuditRecord":
        if row.get("schema_version") != AUDIT_SCHEMA:
            raise ValueError("unsupported allocation audit schema")
        if row.get("capacity_delta_sum") != 0 or row.get("capacity_conserved") is not True:
            raise ValueError("invalid emitted capacity-conservation result")
        data = dict(row); data.pop("schema_version", None); data.pop("capacity_delta_sum"); data.pop("capacity_conserved")
        return cls.create(**data)


def append_allocation_audit(path: Path, record: AllocationAuditRecord) -> None:
    if not isinstance(record, AllocationAuditRecord):
        raise ValueError("record must be AllocationAuditRecord")
    # Frozen dataclasses can still be constructed directly (or produced with
    # ``dataclasses.replace``), bypassing ``create``.  Re-run the complete
    # schema/capacity validation at the persistence boundary so an invalid
    # in-memory record can never become authoritative audit evidence.
    validated = AllocationAuditRecord.from_dict(record.as_dict())
    append_jsonl(path, validated.as_dict())


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


def canonical_json_sha256(value: Any) -> str:
    """Hash the exact JSON value emitted by an artifact.

    Callers must never trust a digest embedded in the value being hashed.  In
    particular, the comparison-contract digest is computed from the emitted
    contract object itself, not copied from a caller-provided ``sha256`` key.
    """

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact value must be finite JSON data") from exc
    return hashlib.sha256(encoded).hexdigest()


def _finite_number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return 0.0 if result == 0.0 else result


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _usage_counts(
    raw: Mapping[str, Any] | None,
    *,
    slot_aliases: Sequence[str] = (),
) -> dict[str, Any]:
    source = dict(raw or {})
    calls = _count(source.get("calls", 0), "usage.calls")
    input_tokens = _count(source.get("input_tokens", 0), "usage.input_tokens")
    output_tokens = _count(source.get("output_tokens", 0), "usage.output_tokens")
    cache_read = _count(
        source.get("cache_read_tokens", 0), "usage.cache_read_tokens"
    )
    cache_write = _count(
        source.get("cache_write_tokens", 0), "usage.cache_write_tokens"
    )
    minimum_total = input_tokens + output_tokens
    total_tokens = _count(
        source.get("total_tokens", minimum_total), "usage.total_tokens"
    )
    if total_tokens < minimum_total:
        raise ValueError("usage.total_tokens cannot be less than input + output")
    slots = 0.0
    for alias in slot_aliases:
        if alias in source:
            slots = _finite_number(source[alias], f"usage.{alias}")
            break
    return {
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": total_tokens,
        "slot_seconds": slots,
    }


def _scheduler_cost(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(raw or {})
    usage = _usage_counts(source)
    occupied = _finite_number(
        source.get(
            "occupied_capacity_slot_seconds",
            source.get(
                "reserved_slot_seconds", source.get("occupied_slot_seconds", 0.0)
            ),
        ),
        "scheduler_cost.occupied_capacity_slot_seconds",
    )
    reservations = _count(
        source.get("capacity_reservations", source.get("reservations", 0)),
        "scheduler_cost.capacity_reservations",
    )
    result = {
        key: value for key, value in usage.items() if key != "slot_seconds"
    }
    result.update(
        {
            # Model/provider latency is reported independently.  It is never
            # substituted for capacity occupancy.
            "latency_seconds": _finite_number(
                source.get("latency_seconds", 0.0),
                "scheduler_cost.latency_seconds",
            ),
            "capacity_reservations": reservations,
            "occupied_capacity_slot_seconds": occupied,
            "reserved_slot_seconds": occupied,
            "invalid_outputs": _count(
                source.get("invalid_outputs", 0),
                "scheduler_cost.invalid_outputs",
            ),
            "fallback_count": _count(
                source.get("fallback_count", 0),
                "scheduler_cost.fallback_count",
            ),
            "horizon_truncations": _count(
                source.get("horizon_truncations", 0),
                "scheduler_cost.horizon_truncations",
            ),
        }
    )
    return result


def _allocation_counts(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(raw or {})
    decisions = _count(
        source.get("decisions", source.get("decision_count", 0)),
        "allocation_metrics.decisions",
    )
    fallbacks = _count(
        source.get(
            "fallbacks",
            source.get("fallback_decisions", source.get("fallback_count", 0)),
        ),
        "allocation_metrics.fallbacks",
    )
    invalid = _count(
        source.get("invalid_outputs", source.get("invalid_output_count", 0)),
        "allocation_metrics.invalid_outputs",
    )
    horizon = _count(
        source.get(
            "horizon_truncations", source.get("horizon_truncation_count", 0)
        ),
        "allocation_metrics.horizon_truncations",
    )
    stale = _count(
        source.get("stale_decisions", source.get("stale_decision_count", 0)),
        "allocation_metrics.stale_decisions",
    )
    admitted = _count(
        source.get("admitted_decisions", source.get("admitted_count", 0)),
        "allocation_metrics.admitted_decisions",
    )
    for name, count in (
        ("fallbacks", fallbacks),
        ("invalid_outputs", invalid),
        ("horizon_truncations", horizon),
        ("stale_decisions", stale),
        ("admitted_decisions", admitted),
    ):
        if count > decisions:
            raise ValueError(f"allocation_metrics.{name} cannot exceed decisions")
    return {
        "decisions": decisions,
        "admitted_decisions": admitted,
        "fallbacks": fallbacks,
        "fallback_rate": fallbacks / decisions if decisions else 0.0,
        "invalid_outputs": invalid,
        "horizon_truncations": horizon,
        "stale_decisions": stale,
    }


def _nauc(
    history: Sequence[Mapping[str, Any]],
    horizon: float,
    max_score: float,
) -> tuple[float, dict[str, float | None], list[dict[str, Any]]]:
    horizon = _finite_number(horizon, "horizon_seconds", minimum=1e-300)
    max_score = _finite_number(max_score, "max_score", minimum=1e-300)
    previous = 0.0
    score = 0.0
    area = 0.0
    times: dict[str, float | None] = {
        str(k): None for k in range(1, int(math.floor(max_score)) + 1)
    }
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(history):
        if not isinstance(raw, Mapping):
            raise ValueError("accepted score history rows must be mappings")
        elapsed = _finite_number(raw.get("elapsed_seconds"), f"history[{index}].elapsed")
        value = _finite_number(
            raw.get("accepted_score", raw.get("score", 0.0)),
            f"history[{index}].accepted_score",
        )
        if elapsed < previous or elapsed > horizon or value < score or value > max_score:
            raise ValueError(
                "accepted score history must be bounded and monotonic in time and score"
            )
        area += score * (elapsed - previous)
        score = value
        previous = elapsed
        for k in range(1, int(math.floor(score)) + 1):
            key = str(k)
            if key in times and times[key] is None:
                times[key] = elapsed
        row = dict(raw)
        row["elapsed_seconds"] = elapsed
        row["accepted_score"] = value
        # Do not retain an ambiguous per-candidate score alias in the
        # cumulative accepted-score trajectory.
        row.pop("score", None)
        normalized.append(row)
    area += score * (horizon - previous)
    return area / (horizon * max_score), times, normalized


def build_figure4_run_summary(
    *,
    run_id: str,
    policy: str,
    paired_seed: int | str,
    repeat: int | str,
    paired_repeat_id: int | str | None = None,
    comparison_contract_id: str = "",
    comparison_contract: Mapping[str, Any] | None = None,
    task_order: Sequence[str] = (),
    horizon_seconds: float,
    total_capacity: int,
    initial_allocation: Mapping[str, int],
    accepted_score_history: Sequence[Mapping[str, Any]],
    max_score: float = 1.0,
    solver_usage: Mapping[str, Any] | None = None,
    evaluator_usage: Mapping[str, Any] | None = None,
    scheduler_cost: Mapping[str, Any] | None = None,
    allocation_metrics: Mapping[str, Any] | None = None,
    allocation_parameters: Mapping[str, Any] | None = None,
    allocation_config_sha256: str = "",
) -> dict[str, Any]:
    """Build a validated per-arm Figure 4 summary.

    Slot accounting has one invariant: solver occupancy plus real scheduler
    reservation occupancy may not exceed ``capacity * fixed horizon``.
    Evaluator resources are reported separately and do not consume CPS slots.
    """

    run = str(run_id).strip()
    canonical_policy = str(policy).strip()
    if not run:
        raise ValueError("run_id must be non-empty")
    if canonical_policy not in _FIGURE4_POLICIES:
        raise ValueError("summary policy must be a canonical Figure 4 policy")
    if isinstance(total_capacity, bool) or not isinstance(total_capacity, int) or total_capacity <= 0:
        raise ValueError("total_capacity must be a positive integer")
    horizon = _finite_number(horizon_seconds, "horizon_seconds", minimum=1e-300)
    maximum = _finite_number(max_score, "max_score", minimum=1e-300)

    tasks = [str(task_id) for task_id in task_order]
    if any(not task_id for task_id in tasks) or len(set(tasks)) != len(tasks):
        raise ValueError("task_order must contain unique non-empty task IDs")
    initial: dict[str, int] = {}
    for raw_task, raw_count in initial_allocation.items():
        task_id = str(raw_task)
        if not task_id or task_id in initial:
            raise ValueError("initial_allocation task IDs must be unique and non-empty")
        initial[task_id] = _count(raw_count, f"initial_allocation[{task_id!r}]")
    if tasks and set(initial) != set(tasks):
        raise ValueError("initial_allocation must contain exactly task_order")
    if sum(initial.values()) > total_capacity:
        raise ValueError("initial allocation cannot exceed total capacity")

    nauc, times, history = _nauc(accepted_score_history, horizon, maximum)
    final = history[-1]["accepted_score"] if history else 0.0

    solver = _usage_counts(
        solver_usage,
        slot_aliases=("slot_seconds", "solver_slot_seconds", "solver_agent_seconds"),
    )
    evaluator_source = dict(evaluator_usage or {})
    evaluator = {
        "calls": _count(evaluator_source.get("calls", 0), "evaluator_usage.calls"),
        "admissions": _count(
            evaluator_source.get("admissions", 0), "evaluator_usage.admissions"
        ),
        "terminal_receipts": _count(
            evaluator_source.get(
                "terminal_receipts",
                evaluator_source.get("terminal_verdict_count", 0),
            ),
            "evaluator_usage.terminal_receipts",
        ),
    }
    for key, value in evaluator_source.items():
        if key not in evaluator and key not in {"terminal_verdict_count"}:
            evaluator[key] = value
    scheduler = _scheduler_cost(scheduler_cost)
    alloc = _allocation_counts(allocation_metrics)

    occupied = solver["slot_seconds"] + scheduler["occupied_capacity_slot_seconds"]
    available = horizon * total_capacity
    tolerance = max(1e-9, available * 1e-9)
    if occupied > available + tolerance:
        raise ValueError("solver + scheduler occupied slot-seconds exceed capacity")
    max_occupied = (solver_usage or {}).get("max_occupied_slots")
    if max_occupied is not None:
        max_occupied = _count(max_occupied, "solver_usage.max_occupied_slots")
        if max_occupied > total_capacity:
            raise ValueError("maximum occupied slots exceed total capacity")
        solver["max_occupied_slots"] = max_occupied
    solver["solver_agent_seconds"] = solver["slot_seconds"]

    try:
        parameters = json.loads(
            json.dumps(dict(allocation_parameters or {}), allow_nan=False, sort_keys=True)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("allocation_parameters must be finite JSON data") from exc
    computed_allocation_hash = canonical_json_sha256(parameters)
    if allocation_config_sha256:
        if not _SHA256.fullmatch(allocation_config_sha256):
            raise ValueError("allocation_config_sha256 must be SHA-256 hex")
        if computed_allocation_hash != allocation_config_sha256:
            raise ValueError(
                "allocation_config_sha256 does not match allocation_parameters"
            )
    else:
        allocation_config_sha256 = computed_allocation_hash

    try:
        contract = json.loads(
            json.dumps(dict(comparison_contract or {}), allow_nan=False, sort_keys=True)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("comparison_contract must be finite JSON data") from exc
    contract_sha256 = canonical_json_sha256(contract)
    return {
        "schema_version": RUN_SCHEMA,
        "run_id": run,
        "policy": canonical_policy,
        "paired_seed": paired_seed,
        "repeat": repeat,
        "paired_repeat_id": repeat if paired_repeat_id is None else paired_repeat_id,
        "comparison_contract_id": str(comparison_contract_id),
        "comparison_contract": contract,
        "comparison_contract_sha256": contract_sha256,
        "task_order": tasks,
        "ordered_task_ids": tasks,
        "horizon_seconds": horizon,
        "total_capacity": total_capacity,
        "initial_allocation": initial,
        "accepted_score_history": history,
        "final_accepted_score": final,
        "max_score": maximum,
        "time_to_k": times,
        "time_to_k_seconds": times,
        "nauc": nauc,
        "solver_usage": solver,
        "evaluator_usage": evaluator,
        "scheduler_cost": scheduler,
        "llm_scheduler_cost": scheduler,
        "capacity_usage": {
            "solver_slot_seconds": solver["slot_seconds"],
            "scheduler_reserved_slot_seconds": scheduler[
                "occupied_capacity_slot_seconds"
            ],
            "occupied_slot_seconds": occupied,
            "available_slot_seconds": available,
            "within_capacity": True,
        },
        "allocation_metrics": alloc,
        "allocation_decisions": alloc["decisions"],
        "fallback_decisions": alloc["fallbacks"],
        "fallback_rate": alloc["fallback_rate"],
        "allocation_parameters": parameters,
        "allocation_config_sha256": allocation_config_sha256,
    }


def write_figure4_run_summary(path: Path, summary: Mapping[str, Any]) -> None:
    if summary.get("schema_version") != RUN_SCHEMA:
        raise ValueError("invalid Figure 4 summary schema")
    contract = summary.get("comparison_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("Figure 4 summary comparison_contract must be an object")
    expected_contract = canonical_json_sha256(contract)
    if summary.get("comparison_contract_sha256") != expected_contract:
        raise ValueError("Figure 4 comparison contract hash mismatch")
    if summary.get("comparison_contract_id") not in {None, "", expected_contract}:
        raise ValueError("Figure 4 comparison contract ID mismatch")
    parameters = summary.get("allocation_parameters")
    allocation_digest = summary.get("allocation_config_sha256")
    if not isinstance(parameters, Mapping) or not isinstance(allocation_digest, str):
        raise ValueError("Figure 4 allocation parameters/hash are missing")
    if canonical_json_sha256(parameters) != allocation_digest:
        raise ValueError("Figure 4 allocation config hash mismatch")
    atomic_write_json(path, summary)


def build_figure4_paired_repeat(
    *,
    paired_repeat_id: str,
    paired_seed: int | str,
    arms: Mapping[str, Mapping[str, Any]],
    comparison_contract: Mapping[str, Any] | None = None,
    registered_contrasts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    required = set(_FIGURE4_POLICIES)
    if set(arms) != required:
        raise ValueError(
            "paired repeat must contain exactly the four Figure 4 arms"
        )
    arm_rows = {str(policy): dict(row) for policy, row in arms.items()}
    for policy, row in arm_rows.items():
        if row.get("policy", policy) != policy:
            raise ValueError("paired arm policy does not match its key")
        if "nauc" not in row or "final_accepted_score" not in row:
            raise ValueError("paired arm is missing bootstrap metrics")
        _finite_number(row["nauc"], f"arms.{policy}.nauc")
        _finite_number(
            row["final_accepted_score"],
            f"arms.{policy}.final_accepted_score",
        )
    contrasts = dict(registered_contrasts or {})
    if not contrasts:
        for name, left, right in (
            ("trace_state_minus_task_state", "trace_state", "task_state"),
            ("task_state_minus_uniform_refill", "task_state", "uniform_refill"),
            ("trace_state_minus_uniform_refill", "trace_state", "uniform_refill"),
            ("llm_scheduler_minus_trace_state", "llm_scheduler", "trace_state"),
        ):
            contrasts[name] = {
                metric: float(arm_rows[left][metric])
                - float(arm_rows[right][metric])
                for metric in ("nauc", "final_accepted_score")
            }
    contract = dict(comparison_contract or {})
    return {
        "schema_version": PAIRED_SCHEMA,
        "paired_repeat_id": str(paired_repeat_id),
        "paired_seed": paired_seed,
        "comparison_contract": contract,
        "comparison_contract_sha256": canonical_json_sha256(contract),
        "arms": arm_rows,
        "registered_contrasts": contrasts,
    }


def append_figure4_paired_repeat(path: Path, row: Mapping[str, Any]) -> None:
    if row.get("schema_version") != PAIRED_SCHEMA: raise ValueError("invalid paired repeat schema")
    append_jsonl(path, row)


__all__ = [
    "AllocationAuditRecord",
    "append_allocation_audit",
    "read_allocation_audits",
    "validate_capacity_conservation",
    "canonical_json_sha256",
    "build_figure4_run_summary",
    "write_figure4_run_summary",
    "build_figure4_paired_repeat",
    "append_figure4_paired_repeat",
]
