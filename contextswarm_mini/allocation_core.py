"""Pure, auditable allocation policies for the registered allocation arms.

This module deliberately has no dependency on the runner, configuration loader,
or CPS projection implementation.  Callers construct one immutable snapshot per
decision and policies return a decision without mutating run state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Callable, ClassVar, Mapping


POLICY_UNIFORM_REFILL = "uniform_refill"
POLICY_TASK_STATE = "task_state"
POLICY_TRACE_STATE = "trace_state"
POLICY_LLM_SCHEDULER = "llm_scheduler"
MAX_SNAPSHOT_TASKS = 512
MAX_TRACE_REFERENCES_PER_TASK = 100
MAX_IDENTIFIER_CHARS = 512


def _finite(name: str, value: float, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return 0.0 if result == 0.0 else result


def _unit_interval(name: str, value: float) -> float:
    result = _finite(name, value)
    if result > 1.0 or result < 0.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return result


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _weight_dict(instance: object) -> dict[str, float]:
    return {item.name: float(getattr(instance, item.name)) for item in fields(instance)}


def _freeze_json(value: Any) -> Any:
    """Detach and deeply freeze a JSON-compatible manifest fragment."""

    try:
        detached = json.loads(json.dumps(value, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ValueError("allocation_parameters must be finite JSON data") from exc
    if isinstance(detached, dict):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in detached.items()})
    if isinstance(detached, list):
        return tuple(_freeze_json(item) for item in detached)
    return detached


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class TraceFeatures:
    """Normalized trace-only evidence; zero is a neutral/absent projection.

    ``evidence_association`` is the registered ``V`` term.  It is deliberately
    not named validation: authoritative checker outcomes belong only in
    :class:`TaskState` and must not be counted a second time through the trace.
    """

    actionability: float = 0.0
    evidence_association: float = 0.0
    positive_feedback: float = 0.0
    negative_feedback: float = 0.0
    drag: float = 0.0

    def __post_init__(self) -> None:
        for item in fields(self):
            object.__setattr__(
                self,
                item.name,
                _unit_interval(f"trace.{item.name}", getattr(self, item.name)),
            )

    def public_dict(self) -> dict[str, float]:
        return _weight_dict(self)

    as_dict = public_dict


@dataclass(frozen=True)
class TaskState:
    """One task's normalized, causal state at an allocation decision."""

    task_id: str
    eligible: bool
    active_allocations: int
    checker_quality: float = 0.0
    recent_progress: float = 0.0
    starvation: float = 0.0
    failure_no_progress: float = 0.0
    trace: TraceFeatures = field(default_factory=TraceFeatures)
    trace_reference_ids: tuple[str, ...] = ()
    checker_outcome_ids: tuple[str, ...] = ()
    trace_source_outcome_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        task_id = str(self.task_id).strip()
        if not task_id:
            raise ValueError("task_id must be non-empty")
        if len(task_id) > MAX_IDENTIFIER_CHARS:
            raise ValueError(f"task_id must be at most {MAX_IDENTIFIER_CHARS} characters")
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be a boolean")
        if not isinstance(self.trace, TraceFeatures):
            raise ValueError("trace must be TraceFeatures")
        identifier_fields: dict[str, tuple[str, ...]] = {}
        for name in (
            "trace_reference_ids",
            "checker_outcome_ids",
            "trace_source_outcome_ids",
        ):
            values = tuple(str(value).strip() for value in getattr(self, name))
            if any(not value for value in values):
                raise ValueError(f"{name} must contain non-empty strings")
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must be unique")
            if len(values) > MAX_TRACE_REFERENCES_PER_TASK:
                raise ValueError(
                    f"{name} must contain at most {MAX_TRACE_REFERENCES_PER_TASK} values"
                )
            if any(len(value) > MAX_IDENTIFIER_CHARS for value in values):
                raise ValueError(f"{name} values must be at most {MAX_IDENTIFIER_CHARS} characters")
            identifier_fields[name] = values
        if set(identifier_fields["checker_outcome_ids"]) & set(
            identifier_fields["trace_source_outcome_ids"]
        ):
            raise ValueError("checker_outcome_ids and trace_source_outcome_ids must be disjoint")
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(
            self,
            "active_allocations",
            _nonnegative_int("active_allocations", self.active_allocations),
        )
        for name in (
            "checker_quality",
            "recent_progress",
            "starvation",
            "failure_no_progress",
        ):
            object.__setattr__(self, name, _unit_interval(name, getattr(self, name)))
        for name, values in identifier_fields.items():
            object.__setattr__(self, name, values)

    def public_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "task_id": self.task_id,
            "eligible": self.eligible,
            "active_allocations": self.active_allocations,
            "checker_quality": self.checker_quality,
            "recent_progress": self.recent_progress,
            "starvation": self.starvation,
            "failure_no_progress": self.failure_no_progress,
        }
        if include_trace:
            result["trace"] = self.trace.public_dict()
            result["trace_reference_ids"] = list(self.trace_reference_ids)
            result["trace_source_outcome_ids"] = list(self.trace_source_outcome_ids)
        result["checker_outcome_ids"] = list(self.checker_outcome_ids)
        return result

    as_dict = public_dict


@dataclass(frozen=True)
class AllocationStateSnapshot:
    """Immutable common state supplied to every registered allocation arm."""

    SCHEMA_VERSION: ClassVar[str] = "contextswarm_allocation_state_v1"

    decision_id: str
    decision_index: int
    elapsed_seconds: float
    remaining_seconds: float
    total_capacity: int
    active_solver_slots: int
    scheduler_reserved_slots: int
    free_slots: int
    tasks: tuple[TaskState, ...]
    trace_watermark: str = ""
    allocation_config_sha256: str = ""
    allocation_parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        decision_id = str(self.decision_id).strip()
        if not decision_id:
            raise ValueError("decision_id must be non-empty")
        tasks = tuple(self.tasks)
        if any(not isinstance(task, TaskState) for task in tasks):
            raise ValueError("tasks must contain only TaskState records")
        if len(tasks) > MAX_SNAPSHOT_TASKS:
            raise ValueError(f"snapshot must contain at most {MAX_SNAPSHOT_TASKS} tasks")
        task_ids = [task.task_id for task in tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("task IDs must be unique within a snapshot")
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(
            self, "decision_index", _nonnegative_int("decision_index", self.decision_index)
        )
        object.__setattr__(
            self, "elapsed_seconds", _finite("elapsed_seconds", self.elapsed_seconds, minimum=0.0)
        )
        object.__setattr__(
            self,
            "remaining_seconds",
            _finite("remaining_seconds", self.remaining_seconds, minimum=0.0),
        )
        object.__setattr__(
            self, "total_capacity", _nonnegative_int("total_capacity", self.total_capacity)
        )
        object.__setattr__(
            self,
            "active_solver_slots",
            _nonnegative_int("active_solver_slots", self.active_solver_slots),
        )
        object.__setattr__(
            self,
            "scheduler_reserved_slots",
            _nonnegative_int("scheduler_reserved_slots", self.scheduler_reserved_slots),
        )
        object.__setattr__(self, "free_slots", _nonnegative_int("free_slots", self.free_slots))
        object.__setattr__(self, "tasks", tasks)
        if sum(task.active_allocations for task in tasks) != self.active_solver_slots:
            raise ValueError("active_solver_slots must equal the sum of task active_allocations")
        if (
            self.active_solver_slots + self.scheduler_reserved_slots + self.free_slots
            != self.total_capacity
        ):
            raise ValueError(
                "active_solver_slots + scheduler_reserved_slots + free_slots must equal total_capacity"
            )
        for name in ("trace_watermark", "allocation_config_sha256"):
            value = str(getattr(self, name)).strip()
            if len(value) > MAX_IDENTIFIER_CHARS:
                raise ValueError(f"{name} must be at most {MAX_IDENTIFIER_CHARS} characters")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "allocation_parameters", _freeze_json(self.allocation_parameters))

    @property
    def state_id(self) -> str:
        """Canonical identity for the entire same-state counterfactual input."""

        canonical = {
            "schema_version": self.SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "decision_index": self.decision_index,
            "elapsed_seconds": self.elapsed_seconds,
            "remaining_seconds": self.remaining_seconds,
            "total_capacity": self.total_capacity,
            "active_solver_slots": self.active_solver_slots,
            "scheduler_reserved_slots": self.scheduler_reserved_slots,
            "free_slots": self.free_slots,
            "trace_watermark": self.trace_watermark,
            "allocation_config_sha256": self.allocation_config_sha256,
            "allocation_parameters": _thaw_json(self.allocation_parameters),
            "task_order": [task.task_id for task in self.tasks],
            "eligible_task_ids": list(self.eligible_task_ids),
            "tasks": [task.public_dict() for task in self.tasks],
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def capacity(self) -> int:
        """Compatibility alias for callers using the early core API sketch."""

        return self.total_capacity

    @property
    def eligible_tasks(self) -> tuple[TaskState, ...]:
        return tuple(sorted((task for task in self.tasks if task.eligible), key=lambda task: task.task_id))

    @property
    def eligible_task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.eligible_tasks)

    @property
    def trace_reference_ids(self) -> frozenset[str]:
        return frozenset(
            reference
            for task in self.eligible_tasks
            for reference in task.trace_reference_ids
        )

    def public_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "state_id": self.state_id,
            "decision_index": self.decision_index,
            "elapsed_seconds": self.elapsed_seconds,
            "remaining_seconds": self.remaining_seconds,
            "total_capacity": self.total_capacity,
            "active_solver_slots": self.active_solver_slots,
            "scheduler_reserved_slots": self.scheduler_reserved_slots,
            "free_slots": self.free_slots,
            "trace_watermark": self.trace_watermark,
            "allocation_config_sha256": self.allocation_config_sha256,
            "allocation_parameters": _thaw_json(self.allocation_parameters),
            "task_order": [task.task_id for task in self.tasks],
            "eligible_task_ids": list(self.eligible_task_ids),
            "tasks": [task.public_dict(include_trace=include_trace) for task in self.tasks],
        }

    as_dict = public_dict


@dataclass(frozen=True)
class TaskScoreWeights:
    """Manifest-owned coefficients for ``(vQ*Q+vDelta*Delta+vX*X-vG*G)/(1+n)``."""

    checker_quality: float = 1.0
    recent_progress: float = 1.0
    starvation: float = 1.0
    failure_no_progress: float = 1.0

    def __post_init__(self) -> None:
        for item in fields(self):
            object.__setattr__(
                self,
                item.name,
                _finite(item.name, getattr(self, item.name), minimum=0.0),
            )

    @classmethod
    def from_mapping(cls, values: Mapping[str, float] | None = None) -> TaskScoreWeights:
        values = values or {}
        allowed = {item.name for item in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("unknown task score weights: " + ", ".join(sorted(unknown)))
        return cls(**{key: float(value) for key, value in values.items()})

    def public_dict(self) -> dict[str, float]:
        return _weight_dict(self)

    as_dict = public_dict


@dataclass(frozen=True)
class TraceScoreWeights:
    """Manifest-owned coefficients for the registered ``A/V/F+/F-/D`` terms."""

    actionability: float = 1.0
    evidence_association: float = 1.0
    positive_feedback: float = 1.0
    negative_feedback: float = 1.0
    drag: float = 1.0

    def __post_init__(self) -> None:
        for item in fields(self):
            object.__setattr__(
                self,
                item.name,
                _finite(item.name, getattr(self, item.name), minimum=0.0),
            )

    @classmethod
    def from_mapping(cls, values: Mapping[str, float] | None = None) -> TraceScoreWeights:
        values = values or {}
        allowed = {item.name for item in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("unknown trace score weights: " + ", ".join(sorted(unknown)))
        return cls(**{key: float(value) for key, value in values.items()})

    def public_dict(self) -> dict[str, float]:
        return _weight_dict(self)

    as_dict = public_dict


DEFAULT_TASK_SCORE_WEIGHTS = TaskScoreWeights()
DEFAULT_TRACE_SCORE_WEIGHTS = TraceScoreWeights()


class TaskStateScorer:
    """Score only ordinary task/checker state; trace fields are never read."""

    def __init__(self, weights: TaskScoreWeights = DEFAULT_TASK_SCORE_WEIGHTS) -> None:
        if not isinstance(weights, TaskScoreWeights):
            raise TypeError("weights must be TaskScoreWeights")
        self.weights = weights

    def score_task(self, task: TaskState) -> float:
        weights = self.weights
        numerator = (
            weights.checker_quality * task.checker_quality
            + weights.recent_progress * task.recent_progress
            + weights.starvation * task.starvation
            - weights.failure_no_progress * task.failure_no_progress
        )
        return numerator / (1.0 + task.active_allocations)

    def score_snapshot(self, snapshot: AllocationStateSnapshot) -> dict[str, float]:
        return {task.task_id: self.score_task(task) for task in snapshot.eligible_tasks}


class TraceStateScorer:
    """Add normalized trace evidence to the shared task-only scorer."""

    def __init__(
        self,
        task_scorer: TaskStateScorer | None = None,
        weights: TraceScoreWeights = DEFAULT_TRACE_SCORE_WEIGHTS,
    ) -> None:
        if not isinstance(weights, TraceScoreWeights):
            raise TypeError("weights must be TraceScoreWeights")
        self.task_scorer = task_scorer or TaskStateScorer()
        self.weights = weights

    def trace_increment(self, task: TaskState) -> float:
        weights = self.weights
        trace = task.trace
        numerator = (
            weights.actionability * trace.actionability
            + weights.evidence_association * trace.evidence_association
            + weights.positive_feedback * trace.positive_feedback
            - weights.negative_feedback * trace.negative_feedback
            - weights.drag * trace.drag
        )
        return numerator / (1.0 + task.active_allocations)

    def score_task(self, task: TaskState) -> float:
        return self.task_scorer.score_task(task) + self.trace_increment(task)

    def score_snapshot(self, snapshot: AllocationStateSnapshot) -> dict[str, float]:
        return {task.task_id: self.score_task(task) for task in snapshot.eligible_tasks}

    def increments(self, snapshot: AllocationStateSnapshot) -> dict[str, float]:
        return {task.task_id: self.trace_increment(task) for task in snapshot.eligible_tasks}


def _immutable_scores(values: Mapping[str, float]) -> Mapping[str, float]:
    return MappingProxyType(
        {
            str(key): _finite(f"score[{key!r}]", value)
            for key, value in sorted(values.items())
        }
    )


@dataclass(frozen=True)
class LLMSchedulerCost:
    """One scheduler call's explicit contribution to the common cost ledger."""

    calls: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    reservation_slots: int = 1
    occupied_slot_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("calls", "input_tokens", "output_tokens", "reservation_slots"):
            object.__setattr__(self, name, _nonnegative_int(name, getattr(self, name)))
        latency = _finite("latency_seconds", self.latency_seconds, minimum=0.0)
        occupied = self.occupied_slot_seconds
        if occupied is None:
            occupied = latency * self.reservation_slots
        occupied = _finite("occupied_slot_seconds", occupied, minimum=0.0)
        object.__setattr__(self, "latency_seconds", latency)
        object.__setattr__(self, "occupied_slot_seconds", occupied)

    def public_dict(self) -> dict[str, int | float]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_seconds": self.latency_seconds,
            "reservation_slots": self.reservation_slots,
            "occupied_slot_seconds": float(self.occupied_slot_seconds or 0.0),
            "total_tokens": self.input_tokens + self.output_tokens,
            "reserved_slot_seconds": float(self.occupied_slot_seconds or 0.0),
        }

    as_dict = public_dict


@dataclass(frozen=True)
class LLMSchedulerResponse:
    """Transport-neutral result returned by a runner-owned model invoker."""

    output: str
    returncode: int = 0
    timed_out: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    reservation_slots: int = 1
    occupied_slot_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.output, str):
            raise ValueError("output must be a string")
        if isinstance(self.returncode, bool) or not isinstance(self.returncode, int):
            raise ValueError("returncode must be an integer")
        if not isinstance(self.timed_out, bool):
            raise ValueError("timed_out must be a boolean")
        LLMSchedulerCost(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_seconds=self.latency_seconds,
            reservation_slots=self.reservation_slots,
            occupied_slot_seconds=self.occupied_slot_seconds,
        )

    @property
    def cost(self) -> LLMSchedulerCost:
        return LLMSchedulerCost(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_seconds=self.latency_seconds,
            reservation_slots=self.reservation_slots,
            occupied_slot_seconds=self.occupied_slot_seconds,
        )


@dataclass(frozen=True)
class AllocationDecision:
    """Immutable and JSON-friendly result of one pure allocation decision."""

    SCHEMA_VERSION: ClassVar[str] = "contextswarm_allocation_core_decision_v1"

    decision_id: str
    state_id: str
    decision_index: int
    policy: str
    selected_task_id: str
    reason: str
    scores: Mapping[str, float] = field(default_factory=dict)
    task_scores: Mapping[str, float] = field(default_factory=dict)
    trace_increments: Mapping[str, float] = field(default_factory=dict)
    trace_reference_ids: tuple[str, ...] = ()
    fallback: bool = False
    fallback_reason: str = ""
    scheduler_cost: LLMSchedulerCost | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scores", _immutable_scores(self.scores))
        object.__setattr__(self, "task_scores", _immutable_scores(self.task_scores))
        object.__setattr__(self, "trace_increments", _immutable_scores(self.trace_increments))
        object.__setattr__(self, "trace_reference_ids", tuple(self.trace_reference_ids))

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "state_id": self.state_id,
            "decision_index": self.decision_index,
            "policy": self.policy,
            "selected_task_id": self.selected_task_id,
            "reason": self.reason,
            "scores": dict(self.scores),
            "task_scores": dict(self.task_scores),
            "trace_increments": dict(self.trace_increments),
            "trace_reference_ids": list(self.trace_reference_ids),
            "fallback": self.fallback,
            "fallback_reason": self.fallback_reason,
            "scheduler_cost": self.scheduler_cost.public_dict() if self.scheduler_cost else None,
        }

    as_dict = public_dict


def _highest_score(scores: Mapping[str, float]) -> str:
    return min(scores, key=lambda task_id: (-scores[task_id], task_id)) if scores else ""


class UniformRefillAllocationPolicy:
    """Refill the eligible task with the fewest current active leases."""

    name = POLICY_UNIFORM_REFILL

    def choose(self, snapshot: AllocationStateSnapshot) -> AllocationDecision:
        active = {task.task_id: float(task.active_allocations) for task in snapshot.eligible_tasks}
        selected = min(active, key=lambda task_id: (active[task_id], task_id)) if active else ""
        return AllocationDecision(
            decision_id=snapshot.decision_id,
            state_id=snapshot.state_id,
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason="fewest active allocations; task-ID tie break" if selected else "no eligible task",
            scores={task_id: -count for task_id, count in active.items()},
        )


class TaskStateAllocationPolicy:
    name = POLICY_TASK_STATE

    def __init__(self, scorer: TaskStateScorer | None = None) -> None:
        self.scorer = scorer or TaskStateScorer()

    def choose(self, snapshot: AllocationStateSnapshot) -> AllocationDecision:
        scores = self.scorer.score_snapshot(snapshot)
        selected = _highest_score(scores)
        return AllocationDecision(
            decision_id=snapshot.decision_id,
            state_id=snapshot.state_id,
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason="highest task-state utility; task-ID tie break" if selected else "no eligible task",
            scores=scores,
            task_scores=scores,
        )


class TraceStateAllocationPolicy:
    name = POLICY_TRACE_STATE

    def __init__(self, scorer: TraceStateScorer | None = None) -> None:
        self.scorer = scorer or TraceStateScorer()

    def choose(self, snapshot: AllocationStateSnapshot) -> AllocationDecision:
        task_scores = self.scorer.task_scorer.score_snapshot(snapshot)
        increments = self.scorer.increments(snapshot)
        scores = {task_id: task_scores[task_id] + increments[task_id] for task_id in task_scores}
        selected = _highest_score(scores)
        return AllocationDecision(
            decision_id=snapshot.decision_id,
            state_id=snapshot.state_id,
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason="highest task-plus-trace utility; task-ID tie break" if selected else "no eligible task",
            scores=scores,
            task_scores=task_scores,
            trace_increments=increments,
        )


def parse_llm_scheduler_output(
    raw_output: str,
    snapshot: AllocationStateSnapshot,
) -> tuple[str, str, tuple[str, ...]]:
    """Parse the exact, non-Markdown scheduler wire shape or raise ValueError."""

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("scheduler JSON contains duplicate keys")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw_output.strip(),
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {value}")),
        )
    except (AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("scheduler output must be exactly one JSON object") from exc
    required = {"decision_id", "task_id", "reason", "trace_reference_ids"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("scheduler JSON must contain exactly decision_id, task_id, reason, trace_reference_ids")
    if payload["decision_id"] != snapshot.decision_id:
        raise ValueError("scheduler decision_id is stale or mismatched")
    task_id = payload["task_id"]
    if not isinstance(task_id, str) or task_id not in snapshot.eligible_task_ids:
        raise ValueError("scheduler task_id is not eligible")
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1_000:
        raise ValueError("scheduler reason must be non-empty and at most 1000 characters")
    references = payload["trace_reference_ids"]
    if (
        not isinstance(references, list)
        or len(references) > 20
        or any(not isinstance(reference, str) for reference in references)
        or len(set(references)) != len(references)
        or not set(references).issubset(
            next(task for task in snapshot.tasks if task.task_id == task_id).trace_reference_ids
        )
    ):
        raise ValueError("scheduler trace_reference_ids are invalid")
    return task_id, reason.strip(), tuple(references)


LLMSchedulerInvoker = Callable[[AllocationStateSnapshot, str], LLMSchedulerResponse]


class ReadOnlyLLMSchedulerPolicy:
    """Model-selected task with strict output validation and deterministic fallback."""

    name = POLICY_LLM_SCHEDULER

    def __init__(
        self,
        invoke: LLMSchedulerInvoker,
        fallback_policy: TaskStateAllocationPolicy | None = None,
    ) -> None:
        if not callable(invoke):
            raise TypeError("invoke must be callable")
        self._invoke = invoke
        self._fallback = fallback_policy or TaskStateAllocationPolicy()

    @staticmethod
    def prompt(snapshot: AllocationStateSnapshot) -> str:
        state = json.dumps(snapshot.public_dict(), ensure_ascii=False, sort_keys=True)
        return (
            "You are a read-only allocation scheduler. Decide only from SNAPSHOT; do not call "
            "tools or change state. Return exactly one JSON object with keys decision_id, task_id, "
            "reason, trace_reference_ids. task_id must be eligible; trace references must appear in "
            "the snapshot. No Markdown or extra keys.\nSNAPSHOT:\n" + state
        )

    def choose(self, snapshot: AllocationStateSnapshot) -> AllocationDecision:
        response = self._invoke(snapshot, self.prompt(snapshot))
        if not isinstance(response, LLMSchedulerResponse):
            raise TypeError("LLM scheduler invoker must return LLMSchedulerResponse")
        error = ""
        selected = ""
        reason = ""
        references: tuple[str, ...] = ()
        if response.returncode != 0:
            error = f"scheduler returned {response.returncode}"
        elif response.timed_out:
            error = "scheduler timed out"
        else:
            try:
                selected, reason, references = parse_llm_scheduler_output(response.output, snapshot)
            except ValueError as exc:
                error = str(exc)
        if error:
            fallback = self._fallback.choose(snapshot)
            selected = fallback.selected_task_id
            reason = "scheduler decision rejected; deterministic task-state fallback"
        task_scores = self._fallback.scorer.score_snapshot(snapshot)
        trace_increments = {task.task_id: 0.0 for task in snapshot.eligible_tasks}
        scores = dict(task_scores)
        return AllocationDecision(
            decision_id=snapshot.decision_id,
            state_id=snapshot.state_id,
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason=reason,
            scores=scores,
            task_scores=task_scores,
            trace_increments=trace_increments,
            trace_reference_ids=references,
            fallback=bool(error),
            fallback_reason=error,
            scheduler_cost=response.cost,
        )


ALLOCATION_POLICY_REGISTRY: Mapping[str, type[Any]] = MappingProxyType(
    {
        POLICY_UNIFORM_REFILL: UniformRefillAllocationPolicy,
        POLICY_TASK_STATE: TaskStateAllocationPolicy,
        POLICY_TRACE_STATE: TraceStateAllocationPolicy,
        POLICY_LLM_SCHEDULER: ReadOnlyLLMSchedulerPolicy,
    }
)


def create_allocation_policy(
    policy: str,
    *,
    task_weights: TaskScoreWeights = DEFAULT_TASK_SCORE_WEIGHTS,
    trace_weights: TraceScoreWeights = DEFAULT_TRACE_SCORE_WEIGHTS,
    llm_invoker: LLMSchedulerInvoker | None = None,
) -> Any:
    """Construct one registered policy from explicit manifest-owned settings."""

    name = str(policy).strip().lower()
    if name == POLICY_UNIFORM_REFILL:
        return UniformRefillAllocationPolicy()
    task_scorer = TaskStateScorer(task_weights)
    if name == POLICY_TASK_STATE:
        return TaskStateAllocationPolicy(task_scorer)
    if name == POLICY_TRACE_STATE:
        return TraceStateAllocationPolicy(TraceStateScorer(task_scorer, trace_weights))
    if name == POLICY_LLM_SCHEDULER:
        if llm_invoker is None:
            raise ValueError("llm_scheduler requires llm_invoker")
        return ReadOnlyLLMSchedulerPolicy(llm_invoker, TaskStateAllocationPolicy(task_scorer))
    raise ValueError(f"unknown allocation policy: {policy}")


__all__ = [
    "ALLOCATION_POLICY_REGISTRY",
    "DEFAULT_TASK_SCORE_WEIGHTS",
    "DEFAULT_TRACE_SCORE_WEIGHTS",
    "POLICY_LLM_SCHEDULER",
    "POLICY_TASK_STATE",
    "POLICY_TRACE_STATE",
    "POLICY_UNIFORM_REFILL",
    "AllocationDecision",
    "AllocationStateSnapshot",
    "LLMSchedulerCost",
    "LLMSchedulerInvoker",
    "LLMSchedulerResponse",
    "ReadOnlyLLMSchedulerPolicy",
    "TaskScoreWeights",
    "TaskState",
    "TaskStateAllocationPolicy",
    "TaskStateScorer",
    "TraceFeatures",
    "TraceScoreWeights",
    "TraceStateAllocationPolicy",
    "TraceStateScorer",
    "UniformRefillAllocationPolicy",
    "create_allocation_policy",
    "parse_llm_scheduler_output",
]
