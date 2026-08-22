"""Bounded trace-state projection for allocation policies.

This module deliberately does not import the selector or allocation policy
implementations.  A future selector store need only implement
``TraceProjectionSource`` (or hand its rows to ``project_records``), which
keeps the Figure 3 selector and Figure 4 allocator independently testable.

The projection contains *only* trace-derived information.  In particular,
raw Judge/verifier receipts are ignored: checker-backed outcome state belongs
to the task-state score and must not be counted again here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable


_POSITIVE_FEEDBACK_KINDS = frozenset(
    {
        "feedback_helpful",
        "feedback_positive",
        "helpful",
        "positive_feedback",
        "worker_feedback_positive",
    }
)
_NEGATIVE_FEEDBACK_KINDS = frozenset(
    {
        "feedback_harmful",
        "feedback_negative",
        "feedback_unhelpful",
        "harmful",
        "negative_feedback",
        "unhelpful",
        "worker_feedback_negative",
    }
)
_EXPOSURE_KINDS = frozenset(
    {"feedback_exposure", "piece_exposure", "worker_exposure"}
)
_FRONTIER_KINDS = frozenset(
    {"actionable_frontier", "frontier", "frontier_item", "proof_frontier"}
)
_ASSOCIATION_KINDS = frozenset(
    {"evidence_association", "evidence_link", "lineage_evidence_link"}
)
_DUPLICATE_KINDS = frozenset(
    {"duplicate", "duplicate_lineage", "duplicate_piece"}
)
_REFUTATION_KINDS = frozenset(
    {"lineage_refutation", "refutation", "refuted"}
)
_STALE_KINDS = frozenset({"stale", "stale_lineage", "stale_piece"})
_STAGNATION_KINDS = frozenset(
    {"lineage_stagnation", "stagnant", "stagnation"}
)
_TRACE_KINDS = frozenset(
    {
        "trace",
        "trace_created",
        "trace_updated",
        "context_piece",
        "piece",
        "piece_created",
        "piece_snapshot",
    }
)
_LIFECYCLE_KINDS = frozenset(
    {"lifecycle", "lifecycle_update", "trace_lifecycle", "piece_lifecycle", "status"}
)
_RAW_VERIFIER_KINDS = frozenset(
    {
        "candidate_verdict",
        "checker_receipt",
        "judge_receipt",
        "judge_result",
        "validation_result",
        "verifier_feedback",
        "verifier_receipt",
        "verifier_result",
        "evaluator_feedback",
        "evaluator_receipt",
    }
)
_NON_WORKER_SOURCES = frozenset(
    {"checker", "evaluator", "judge", "runner", "verifier"}
)
_INACTIVE_LIFECYCLES = frozenset(
    {"inactive", "refuted", "retired", "stale", "superseded"}
)


def _lifecycle_precedence(value: Any) -> int:
    """Resolve simultaneous lifecycle rows without resurrecting dead state."""

    lifecycle = _kind(value)
    if lifecycle in {"refuted", "retired", "superseded"}:
        return 3
    if lifecycle in {"stale", "inactive"}:
        return 2
    if lifecycle in {"active", "actionable", "frontier"}:
        return 1
    return 0


def _is_activity_record(record: "TraceProjectionRecord") -> bool:
    """Return whether an authoritative row refreshes lineage activity."""

    if record.kind in _STAGNATION_KINDS | _STALE_KINDS | _REFUTATION_KINDS:
        return False
    if record.lifecycle in _INACTIVE_LIFECYCLES:
        return False
    return bool(
        record.kind in _TRACE_KINDS | _FRONTIER_KINDS | _ASSOCIATION_KINDS
        or record.active is True
        or record.actionable is True
    )


def _text(value: Any, *, limit: int = 256) -> str:
    return str(value or "").strip()[:limit]


def _kind(value: Any) -> str:
    return _text(value, limit=64).lower().replace("-", "_").replace(" ", "_")


def _nonnegative_int(value: Any, *, default: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, result)


def _unit(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return min(1.0, max(0.0, number))


def _finite_or_none(value: Any) -> float | None:
    """Parse an optional finite timestamp/weight without leaking bad input."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_timestamp(value: Any) -> float | None:
    """Return seconds since epoch for common numeric/ISO timestamp aliases."""

    numeric = _finite_or_none(value)
    if numeric is not None:
        return numeric
    text = _text(value, limit=128)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _mapping_float(value: Any, *, default: float = 0.0) -> float:
    parsed = _finite_or_none(value)
    return default if parsed is None else parsed


@dataclass(frozen=True)
class TraceProjectionLimits:
    """Manifest-owned projection bounds and normalization saturations."""

    max_tasks: int = 256
    max_records: int = 4096
    max_records_per_task: int = 256
    actionability_saturation: int = 4
    association_saturation: int = 4
    drag_saturation: int = 4
    # Keep the original positional constructor contract intact.  New
    # manifest-owned knobs are appended after the four historical drag
    # weights; callers should prefer keywords for these fields.
    duplicate_weight: float = 1.0
    refutation_weight: float = 1.0
    stale_weight: float = 1.0
    lineage_stagnation_weight: float = 1.0
    recency_window_seconds: float = 600.0
    stagnation_window_seconds: float = 600.0
    feedback_kappa: float = 1.0
    feedback_trust_default: float = 1.0
    feedback_values: Mapping[str, float] | None = None
    require_feedback_mapping: bool = False

    def __post_init__(self) -> None:
        for name in (
            "max_tasks",
            "max_records",
            "max_records_per_task",
            "actionability_saturation",
            "association_saturation",
            "drag_saturation",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "duplicate_weight",
            "refutation_weight",
            "stale_weight",
            "lineage_stagnation_weight",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        for name in ("recency_window_seconds", "stagnation_window_seconds", "feedback_kappa"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        value = float(self.feedback_trust_default)
        if not math.isfinite(value) or value < 0:
            raise ValueError("feedback_trust_default must be finite and non-negative")
        # Formal runs must provide their canonical feedback-kind mapping from
        # the manifest/selected selector.  An empty mapping is deliberate: it
        # prevents outcome-dependent polarity from being guessed here.
        mapping = dict(self.feedback_values or {})
        normalized_keys: set[str] = set()
        for key, raw in mapping.items():
            normalized = _kind(key)
            if not normalized:
                raise ValueError("feedback_values keys must be non-empty")
            if normalized in normalized_keys:
                raise ValueError("feedback_values keys collide after normalization")
            normalized_keys.add(normalized)
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("feedback_values values must be finite")
        object.__setattr__(self, "recency_window_seconds", float(self.recency_window_seconds))
        object.__setattr__(self, "stagnation_window_seconds", float(self.stagnation_window_seconds))
        object.__setattr__(self, "feedback_kappa", float(self.feedback_kappa))
        object.__setattr__(self, "feedback_trust_default", float(self.feedback_trust_default))
        object.__setattr__(self, "feedback_values", {
            _kind(key): float(raw) for key, raw in mapping.items()
        })
        if not isinstance(self.require_feedback_mapping, bool):
            raise ValueError("require_feedback_mapping must be a boolean")
        object.__setattr__(
            self,
            "feedback_values",
            MappingProxyType(dict(getattr(self, "feedback_values") or {})),
        )
@dataclass(frozen=True)
class TraceProjectionRecord:
    """One normalized, selector-independent trace topology event."""

    sequence: int
    task_id: str
    kind: str
    record_id: str = ""
    lineage_id: str = ""
    evidence_id: str = ""
    worker_id: str = ""
    source: str = "worker"
    source_outcome_id: str = ""
    exposure_id: str = ""
    effective: bool = False
    terminal: bool = False
    effective_declared: bool = False
    terminal_declared: bool = False
    # Optional materialized-topology fields.  They are intentionally scalar
    # and bounded: the allocator never receives trace bodies or private paths.
    trace_id: str = ""
    lifecycle: str = ""
    active: bool | None = None
    actionable: bool | None = None
    event_time: float | None = None
    trust: float = 1.0
    trust_declared: bool = False
    trust_rank: int = 0
    feedback_value: float | None = None
    committed_sequence: int = 0
    run_id: str = ""
    consumer_episode_id: str = ""
    # Relation/topology aliases used by some source adapters.
    target_trace_id: str = ""
    relation_kind: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        if isinstance(self.committed_sequence, bool) or not isinstance(self.committed_sequence, int) or self.committed_sequence < 0:
            raise ValueError("committed_sequence must be a non-negative integer")
        if isinstance(self.trust, bool):
            raise ValueError("trust must be finite and non-negative")
        trust = float(self.trust)
        if not math.isfinite(trust) or trust < 0:
            raise ValueError("trust must be finite and non-negative")
        if not isinstance(self.trust_declared, bool):
            raise ValueError("trust_declared must be a boolean")
        if isinstance(self.event_time, bool) or (
            self.event_time is not None and not math.isfinite(float(self.event_time))
        ):
            raise ValueError("event_time must be finite")
        object.__setattr__(self, "trust", trust)

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "TraceProjectionRecord":
        """Accept common event/piece field aliases without retaining payloads."""

        return cls(
            sequence=_nonnegative_int(
                row.get("sequence", row.get("seq", row.get("watermark", 0)))
            ),
            task_id=_text(row.get("task_id")),
            kind=_kind(row.get("kind", row.get("event_kind", row.get("event_type")))),
            record_id=_text(row.get("record_id", row.get("event_id", row.get("id")))),
            lineage_id=_text(row.get("lineage_id", row.get("lineage"))),
            evidence_id=_text(
                row.get("evidence_id", row.get("piece_id", row.get("context_piece_id")))
            ),
            worker_id=_text(
                row.get("worker_id", row.get("actor_id", row.get("author")))
            ),
            source=_kind(row.get("source", row.get("producer_kind", "worker")))
            or "worker",
            source_outcome_id=_text(
                row.get("source_outcome_id", row.get("outcome_id"))
            ),
            exposure_id=_text(row.get("exposure_id", row.get("exposure"))),
            effective=_truthy(row.get("effective", row.get("is_effective", False))),
            terminal=_truthy(row.get("terminal", row.get("is_terminal", False))),
            effective_declared=any(key in row for key in ("effective", "is_effective")),
            terminal_declared=any(key in row for key in ("terminal", "is_terminal")),
            trace_id=_text(
                row.get("trace_id", row.get("target_trace_id", ""))
            ),
            lifecycle=_kind(row.get("lifecycle", row.get("status", ""))),
            active=(
                _truthy(row.get("active", row.get("is_active")))
                if any(key in row for key in ("active", "is_active"))
                else None
            ),
            actionable=(
                _truthy(row.get("actionable", row.get("is_actionable")))
                if any(key in row for key in ("actionable", "is_actionable"))
                else None
            ),
            event_time=_parse_timestamp(
                row.get(
                    "event_time",
                    row.get(
                        "timestamp",
                        row.get(
                            "created_seconds",
                            row.get("created_at", row.get("observed_at")),
                        ),
                    ),
                )
            ),
            trust=max(
                0.0,
                _mapping_float(
                    row.get("trust", row.get("trust_weight", row.get("trust_score", 1.0))),
                    default=1.0,
                ),
            ),
            trust_declared=any(
                key in row for key in ("trust", "trust_weight", "trust_score")
            ),
            trust_rank=_nonnegative_int(
                row.get("trust_rank", row.get("authority_rank", 0))
            ),
            feedback_value=(
                _finite_or_none(row.get("feedback_value", row.get("polarity")))
            ),
            committed_sequence=_nonnegative_int(
                row.get("committed_sequence", row.get("commit_sequence", 0))
            ),
            run_id=_text(row.get("run_id", row.get("experiment_id", ""))),
            consumer_episode_id=_text(
                row.get("consumer_episode_id", row.get("episode_id", row.get("consumer_id", "")))
            ),
            target_trace_id=_text(
                row.get("target_trace_id", row.get("target_piece_id", ""))
            ),
            relation_kind=_kind(row.get("relation_kind", row.get("relation", ""))),
        )

    @property
    def canonical_identity(self) -> tuple[str, ...]:
        """Stable event identity used to suppress receipt/event replay."""

        if self.record_id:
            # IDs are only globally stable when scoped by task/source.  A
            # malformed source reusing ``event-1`` for two tasks must not
            # suppress one task's event during projection.
            return ("id", self.task_id, self.source, self.record_id)
        return (
            "semantic",
            str(self.sequence),
            self.task_id,
            self.kind,
            self.lineage_id,
            self.evidence_id,
            self.worker_id,
            self.source,
            self.source_outcome_id,
            self.trace_id,
        )


@dataclass(frozen=True)
class TraceProjectionRecordBatch:
    """Bounded source response with an inclusive high watermark."""

    records: tuple[TraceProjectionRecord | Mapping[str, Any], ...]
    watermark: int
    # ``complete`` is explicit for sources which can prove a full materialized
    # state.  The historical integer protocol defaults to True for backwards
    # compatibility; adapters should set it to False when a page is partial.
    complete: bool = True
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.watermark, bool) or not isinstance(self.watermark, int):
            raise ValueError("watermark must be a non-negative integer")
        if self.watermark < 0:
            raise ValueError("watermark must not be negative")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        if len(str(self.snapshot_id or "")) > 512:
            raise ValueError("snapshot_id must be at most 512 characters")
        object.__setattr__(self, "snapshot_id", str(self.snapshot_id or "").strip())


@runtime_checkable
class TraceProjectionSource(Protocol):
    """Narrow bridge that a future Figure 3 selection store may implement."""

    def read_allocation_projection_records(
        self,
        task_ids: Sequence[str],
        *,
        after_watermark: int,
        limit: int,
    ) -> TraceProjectionRecordBatch:
        """Return records newer than ``after_watermark`` in stable order."""


@dataclass(frozen=True)
class TraceAllocationProjection:
    """Immutable five-feature trace increment for one task.

    The first five fields are the public allocation interface.  Counts are
    bounded audit diagnostics and never need to be consumed by the scorer.
    """

    task_id: str
    actionability: float = 0.0
    evidence_association: float = 0.0
    positive_feedback: float = 0.0
    negative_feedback: float = 0.0
    drag: float = 0.0
    frontier_count: int = 0
    association_count: int = 0
    feedback_exposure_count: int = 0
    positive_feedback_count: int = 0
    negative_feedback_count: int = 0
    duplicate_count: int = 0
    refutation_count: int = 0
    stale_count: int = 0
    lineage_stagnation_count: int = 0
    watermark: int = 0
    source_outcome_ids: tuple[str, ...] = ()
    zero_reason: str = ""
    # Weighted sums are retained as bounded diagnostics so artifacts can
    # reconstruct the configured smoothing/proportion calculation.
    positive_feedback_weight: float = 0.0
    negative_feedback_weight: float = 0.0
    active_trace_weight: float = 0.0
    active_lineage_weight: float = 0.0
    drag_duplicate_proportion: float = 0.0
    drag_refutation_proportion: float = 0.0
    drag_stale_proportion: float = 0.0
    drag_stagnation_proportion: float = 0.0

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id must not be empty")
        for name in (
            "actionability",
            "evidence_association",
            "positive_feedback",
            "negative_feedback",
            "drag",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")
        for name in (
            "frontier_count",
            "association_count",
            "feedback_exposure_count",
            "positive_feedback_count",
            "negative_feedback_count",
            "duplicate_count",
            "refutation_count",
            "stale_count",
            "lineage_stagnation_count",
            "watermark",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        for name in (
            "positive_feedback_weight",
            "negative_feedback_weight",
            "active_trace_weight",
            "active_lineage_weight",
            "drag_duplicate_proportion",
            "drag_refutation_proportion",
            "drag_stale_proportion",
            "drag_stagnation_proportion",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
            if name.startswith("drag_") and value > 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if len(self.source_outcome_ids) != len(set(self.source_outcome_ids)):
            raise ValueError("source_outcome_ids must be unique")

    @property
    def is_zero(self) -> bool:
        return all(value == 0.0 for value in self.as_core_kwargs().values())

    def as_core_kwargs(self) -> dict[str, float]:
        """Return exactly the fields accepted by the allocation core."""

        return {
            "actionability": self.actionability,
            "evidence_association": self.evidence_association,
            "positive_feedback": self.positive_feedback,
            "negative_feedback": self.negative_feedback,
            "drag": self.drag,
        }


@dataclass(frozen=True)
class TraceAllocationProjectionBatch:
    """Immutable task-ordered projection at one store watermark."""

    projections: tuple[TraceAllocationProjection, ...]
    watermark: int
    records_seen: int = 0
    records_used: int = 0
    truncated: bool = False
    complete: bool = True
    snapshot_id: str = ""
    zero_reason: str = ""

    def __post_init__(self) -> None:
        task_ids = tuple(item.task_id for item in self.projections)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("projection task_ids must be unique")
        if min(self.watermark, self.records_seen, self.records_used) < 0:
            raise ValueError("projection batch counts must not be negative")
        if self.records_used > self.records_seen:
            raise ValueError("records_used must not exceed records_seen")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        if len(str(self.snapshot_id or "")) > 512:
            raise ValueError("snapshot_id must be at most 512 characters")
        object.__setattr__(self, "snapshot_id", str(self.snapshot_id or "").strip())

    def for_task(self, task_id: str) -> TraceAllocationProjection:
        for projection in self.projections:
            if projection.task_id == task_id:
                return projection
        raise KeyError(task_id)

    def as_core_mapping(self) -> dict[str, dict[str, float]]:
        return {
            projection.task_id: projection.as_core_kwargs()
            for projection in self.projections
        }


@dataclass
class _TaskAccumulator:
    frontier: set[str]
    associations: set[tuple[str, str]]
    exposures: set[tuple[str, str, str, str, str]]
    feedback: dict[tuple[str, str, str, str, str], list[TraceProjectionRecord]]
    positive: set[tuple[str, str, str]]
    negative: set[tuple[str, str, str]]
    duplicate: set[str]
    refutation: set[str]
    stale: set[str]
    stagnation: set[str]
    source_outcome_ids: set[str]
    entities: dict[str, "_TraceEntity"]
    ordinary_outcome_ids: frozenset[str] = frozenset()
    explicit_state: bool = False

    @classmethod
    def empty(cls) -> "_TaskAccumulator":
        return cls(
            set(), set(), set(), {}, set(), set(), set(), set(), set(), set(), set(), {}, frozenset(), False
        )


@dataclass
class _TraceEntity:
    """Current-state materialization for one declared trace/lineage."""

    trace_id: str
    lineage_id: str
    latest_sequence: int = 0
    latest_event_time: float | None = None
    latest_state_key: tuple[Any, ...] = ()
    last_activity_time: float | None = None
    state_applied: bool = False
    active: bool | None = None
    lifecycle: str = ""
    actionable: bool = False
    evidence: set[tuple[str, str]] = None  # type: ignore[assignment]
    duplicate: bool = False
    refuted: bool = False
    stale: bool = False
    stagnation: bool = False

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = set()

    @property
    def is_active(self) -> bool:
        if self.active is False:
            return False
        return self.lifecycle not in _INACTIVE_LIFECYCLES


class TraceAllocationProjectionAdapter:
    """Convert bounded selector/store records into allocation features."""

    def __init__(self, limits: TraceProjectionLimits | None = None):
        self.limits = limits or TraceProjectionLimits()

    def project(
        self,
        source: TraceProjectionSource,
        task_ids: Iterable[str],
        *,
        after_watermark: int = 0,
        ordinary_outcome_ids: Iterable[str] = (),
    ) -> TraceAllocationProjectionBatch:
        if isinstance(after_watermark, bool) or not isinstance(after_watermark, int):
            raise ValueError("after_watermark must be a non-negative integer")
        if after_watermark < 0:
            raise ValueError("after_watermark must not be negative")
        ordered = _bounded_task_ids(task_ids, self.limits.max_tasks)
        batch = source.read_allocation_projection_records(
            ordered,
            after_watermark=after_watermark,
            limit=self.limits.max_records,
        )
        if not isinstance(batch, TraceProjectionRecordBatch):
            raise TypeError("trace projection source returned an invalid batch")
        # ``snapshot_id`` is an explicit attestation that this is a complete,
        # causally pinned current-state materialization.  Legacy two-field
        # batches remain delta pages even though their compatibility default
        # for ``complete`` is true.
        if batch.snapshot_id:
            if after_watermark != 0:
                raise ValueError("a full projection snapshot must be read from origin")
            if not batch.complete:
                raise ValueError("trace projection source returned an incomplete state")
            return self.project_full_records(
                ordered,
                batch.records,
                ordinary_outcome_ids=ordinary_outcome_ids,
                source_watermark=batch.watermark,
                snapshot_id=batch.snapshot_id,
            )
        return self.project_records(
            ordered,
            batch.records,
            after_watermark=after_watermark,
            source_watermark=batch.watermark,
            ordinary_outcome_ids=ordinary_outcome_ids,
        )

    def project_full_records(
        self,
        task_ids: Iterable[str],
        records: Iterable[TraceProjectionRecord | Mapping[str, Any]],
        *,
        ordinary_outcome_ids: Iterable[str] = (),
        source_watermark: int | str | None = None,
        snapshot_id: str = "",
        reference_time: float | None = None,
    ) -> TraceAllocationProjectionBatch:
        """Materialize a complete current trace state at one pinned cut.

        Unlike :meth:`project_records`, this method never interprets an
        ``after_watermark`` as a delta cursor.  ``records`` must be the full
        bounded state for the supplied snapshot.  A source may paginate before
        calling this method, but it must concatenate pages from the same pinned
        watermark.  This distinction prevents a later allocation decision
        from silently forgetting older active traces/exposures.

        ``source_watermark`` is retained as an opaque source identity when it
        is textual; numeric values are accepted for compatibility and are used
        only to reject records from the future.  Pagination completeness is a
        source concern: callers that cannot prove completeness should fail
        closed rather than call this method with a partial page.
        """

        ordered = _bounded_task_ids(task_ids, self.limits.max_tasks)
        ordinary_ids = frozenset(
            _text(value) for value in ordinary_outcome_ids if _text(value)
        )
        converted: list[TraceProjectionRecord] = []
        for index, item in enumerate(records, start=1):
            record = _record(item)
            # A complete materialization may contain legacy rows without a
            # sequence.  Assigning read-order sequence is safe here because
            # the caller explicitly attests that the entire snapshot is in
            # this iterable; it is not used to resume a delta page.
            if isinstance(item, Mapping) and not any(
                key in item for key in ("sequence", "seq", "watermark")
            ):
                record = replace(record, sequence=index)
            if isinstance(source_watermark, int) and not isinstance(source_watermark, bool):
                if record.sequence > source_watermark:
                    raise ValueError("record lies beyond the pinned source watermark")
            converted.append(record)
        if len(converted) > self.limits.max_records:
            raise OverflowError("full trace projection exceeds its record bound")

        # Stable ordering by sequence and identity makes same-watermark
        # materializations deterministic, including siblings sharing a seq.
        converted.sort(key=lambda item: (item.sequence, item.canonical_identity))
        accumulators = {
            task_id: replace(_TaskAccumulator.empty(), ordinary_outcome_ids=ordinary_ids)
            for task_id in ordered
        }
        per_task_seen = {task_id: 0 for task_id in ordered}
        seen_identities: set[tuple[str, ...]] = set()
        used = 0
        for record in converted:
            if record.task_id not in accumulators:
                continue
            if record.canonical_identity in seen_identities:
                continue
            seen_identities.add(record.canonical_identity)
            if per_task_seen[record.task_id] >= self.limits.max_records_per_task:
                raise OverflowError("full trace projection exceeds its per-task bound")
            per_task_seen[record.task_id] += 1
            if self._excluded_by_ordinary(record, ordinary_ids):
                continue
            if self._accumulate_full(accumulators[record.task_id], record):
                used += 1

        if reference_time is None:
            # Full-state callers should pass the allocation decision's fixed
            # as-of time.  Retain a deterministic legacy fallback for direct
            # tests/adapters, but make the choice explicit in the batch audit.
            times = [record.event_time for record in converted if record.event_time is not None]
            reference_time = max(times, default=0.0)
        if isinstance(reference_time, bool) or not math.isfinite(float(reference_time)):
            raise ValueError("reference_time must be finite")
        reference_time = float(reference_time)
        if source_watermark is None:
            numeric_watermark = max((record.sequence for record in converted), default=0)
        elif isinstance(source_watermark, int) and not isinstance(source_watermark, bool):
            numeric_watermark = int(source_watermark)
        else:
            numeric_watermark = max((record.sequence for record in converted), default=0)
        projections = tuple(
            self._finish_full(task_id, accumulators[task_id], numeric_watermark, reference_time)
            for task_id in ordered
        )
        return TraceAllocationProjectionBatch(
            projections=projections,
            watermark=numeric_watermark,
            records_seen=len(converted),
            records_used=used,
            truncated=False,
            complete=True,
            snapshot_id=str(snapshot_id or source_watermark or "").strip(),
        )

    @staticmethod
    def _excluded_by_ordinary(
        record: TraceProjectionRecord, ordinary_ids: frozenset[str]
    ) -> bool:
        """Keep checker receipts disjoint from trace-derived evidence."""

        if record.record_id in ordinary_ids or record.source_outcome_id in ordinary_ids:
            return True
        # An evidence ID can be an ordinary outcome alias in legacy stores.
        return bool(record.evidence_id and record.evidence_id in ordinary_ids)

    @staticmethod
    def _entity_key(record: TraceProjectionRecord) -> str:
        # A declared trace ID is the dedup unit.  Relation rows may identify
        # their trace as the declared target.  If only a declared lineage is
        # available, lineage is a conservative aggregate; evidence IDs are
        # deliberately not treated as trace identities.
        return record.trace_id or record.target_trace_id or (
            "lineage:" + record.lineage_id if record.lineage_id else ""
        )

    @staticmethod
    def _is_state_record(record: TraceProjectionRecord) -> bool:
        return bool(
            record.active is not None
            or record.actionable is not None
            or record.lifecycle
            or record.kind
                in (
                    _TRACE_KINDS
                    | _FRONTIER_KINDS
                    | _LIFECYCLE_KINDS
                    | _DUPLICATE_KINDS
                    | _REFUTATION_KINDS
                    | _STALE_KINDS
                    | _STAGNATION_KINDS
                )
            or record.relation_kind
            in (_DUPLICATE_KINDS | _REFUTATION_KINDS | _STALE_KINDS)
        )

    def _entity_for(
        self, acc: _TaskAccumulator, record: TraceProjectionRecord
    ) -> _TraceEntity | None:
        key = self._entity_key(record)
        if not key:
            return None
        lineage = record.lineage_id or (key if key.startswith("lineage:") else "")
        entity = acc.entities.get(key)
        if entity is None:
            entity = _TraceEntity(key, lineage)
            acc.entities[key] = entity
        entity.state_applied = False
        if not self._is_state_record(record):
            if record.event_time is not None and _is_activity_record(record):
                entity.last_activity_time = max(
                    entity.last_activity_time
                    if entity.last_activity_time is not None
                    else float("-inf"),
                    record.event_time,
                )
            return entity
        state_key = (
            int(record.sequence),
            float(record.event_time) if record.event_time is not None else float("-inf"),
            _lifecycle_precedence(record.lifecycle or record.kind),
            record.canonical_identity,
        )
        # A later materialized row supersedes scalar lifecycle state.  Equal
        # sequence collisions are resolved by event time, terminal lifecycle
        # precedence, then stable identity, never caller iteration order.
        if state_key > entity.latest_state_key:
            entity.state_applied = True
            entity.latest_state_key = state_key
            entity.latest_sequence = max(entity.latest_sequence, record.sequence)
            if record.event_time is not None:
                entity.latest_event_time = record.event_time
            if lineage:
                entity.lineage_id = lineage
            if record.active is not None:
                entity.active = record.active
            if record.lifecycle:
                entity.lifecycle = record.lifecycle
            if record.actionable is not None:
                entity.actionable = bool(record.actionable)
            # Lifecycle/drag flags are current-state properties.  A newer
            # authoritative state supersedes an older stale/refuted marker;
            # the specific status branch below sets any flags for this row.
            entity.duplicate = False
            entity.refuted = False
            entity.stale = False
            entity.stagnation = False
        if record.event_time is not None and _is_activity_record(record):
            entity.last_activity_time = max(
                entity.last_activity_time
                if entity.last_activity_time is not None
                else float("-inf"),
                record.event_time,
            )
        return entity

    def _accumulate_full(self, acc: _TaskAccumulator, record: TraceProjectionRecord) -> bool:
        """Collect one full-state record without counting raw verifier events."""

        kind = record.kind
        if kind in _RAW_VERIFIER_KINDS or record.source in _NON_WORKER_SOURCES:
            return False
        entity = self._entity_for(acc, record)
        if entity is not None:
            acc.explicit_state = acc.explicit_state or bool(
                record.trace_id or record.active is not None or record.lifecycle
            )
        lineage = record.lineage_id
        evidence = record.evidence_id
        event_key = record.record_id or "|".join(record.canonical_identity)
        if kind in _TRACE_KINDS and entity is None:
            return False
        if kind in _FRONTIER_KINDS:
            if entity is None or not lineage:
                return False
            # Explicit false wins over the kind alias; an old frontier row
            # must not resurrect a newer lifecycle/actionability update.
            if entity.state_applied and record.actionable is not False:
                entity.actionable = True
            elif entity.state_applied:
                entity.actionable = False
            return True
        if kind in _ASSOCIATION_KINDS:
            if entity is None or not lineage or not evidence:
                return False
            acc.associations.add((lineage, evidence))
            entity.evidence.add((evidence, record.source_outcome_id))
            if record.source_outcome_id:
                acc.source_outcome_ids.add(record.source_outcome_id)
            return True
        if kind in _EXPOSURE_KINDS:
            if not record.exposure_id:
                return False
            key = (
                record.run_id,
                record.consumer_episode_id,
                record.exposure_id,
                record.trace_id or evidence,
                record.worker_id,
            )
            acc.exposures.add(key)
            return True
        if (
            kind in _POSITIVE_FEEDBACK_KINDS
            or kind in _NEGATIVE_FEEDBACK_KINDS
            or kind in (self.limits.feedback_values or {})
            or record.feedback_value is not None
        ):
            if (
                self.limits.require_feedback_mapping
                and record.feedback_value is None
                and kind not in (self.limits.feedback_values or {})
            ):
                raise ValueError("feedback kind has no manifest-owned mapping")
            if not record.exposure_id or not record.terminal:
                return False
            key = (
                record.run_id,
                record.consumer_episode_id,
                record.exposure_id,
                record.trace_id or evidence,
                record.worker_id,
            )
            acc.feedback.setdefault(key, []).append(record)
            if record.source_outcome_id:
                acc.source_outcome_ids.add(record.source_outcome_id)
            return True
        if kind in _DUPLICATE_KINDS or record.relation_kind in _DUPLICATE_KINDS:
            if entity is None:
                return False
            if entity.state_applied:
                entity.duplicate = True
            return True
        if kind in _REFUTATION_KINDS or record.relation_kind in _REFUTATION_KINDS:
            if entity is None:
                return False
            if entity.state_applied:
                entity.refuted = True
                entity.active = False if record.active is None else record.active
                entity.lifecycle = "refuted"
            return True
        if kind in _STALE_KINDS or record.relation_kind in _STALE_KINDS:
            if entity is None:
                return False
            if entity.state_applied:
                entity.stale = True
                entity.active = False if record.active is None else record.active
                entity.lifecycle = "stale"
            return True
        if kind in _STAGNATION_KINDS:
            if entity is None:
                return False
            if entity.state_applied:
                entity.stagnation = True
            return True
        if kind in _LIFECYCLE_KINDS:
            # Scalar current state was applied deterministically in
            # ``_entity_for``.  Lifecycle rows are useful only when they carry
            # an explicit declared trace/lineage identity.
            return entity is not None
        # A bare trace/piece row still contributes to the current denominator;
        # no public feature changes unless its topology is explicitly known.
        return entity is not None

    def _feedback_value(self, record: TraceProjectionRecord) -> float | None:
        if record.feedback_value is not None:
            value = float(record.feedback_value)
        else:
            mapping = self.limits.feedback_values or {}
            if record.kind not in mapping:
                if self.limits.require_feedback_mapping:
                    return None
                # These aliases are emitted by the bridge only after its
                # manifest mapping has already been applied.  They are not a
                # polarity inference for arbitrary canonical store kinds.
                if record.kind in _POSITIVE_FEEDBACK_KINDS:
                    value = 1.0
                elif record.kind in _NEGATIVE_FEEDBACK_KINDS:
                    value = -1.0
                else:
                    return None
            else:
                value = float(mapping[record.kind])
        if not math.isfinite(value):
            return None
        return value

    @staticmethod
    def _feedback_winner(
        rows: Sequence[TraceProjectionRecord],
    ) -> TraceProjectionRecord | None:
        terminal = [row for row in rows if row.terminal]
        if not terminal:
            return None
        effective = [row for row in terminal if row.effective]
        if not effective and any(row.effective_declared for row in terminal):
            # A source that explicitly marks every terminal event ineffective
            # has already resolved a conflict; do not resurrect a loser by
            # applying local tie-breaking.
            return None
        # A source with an explicit winner (SelectionStore v1) has already
        # arbitrated conflicts.  If no effective bit is present, arbitrate all
        # terminal candidates using the frozen trust/sequence rule.
        candidates = effective or terminal
        return max(
            candidates,
            key=lambda row: (
                int(row.trust_rank),
                float(row.trust),
                int(row.committed_sequence or row.sequence),
                row.record_id,
            ),
        )

    def _finish_full(
        self,
        task_id: str,
        acc: _TaskAccumulator,
        watermark: int,
        reference_time: float,
    ) -> TraceAllocationProjection:
        active_entities = [entity for entity in acc.entities.values() if entity.is_active]
        all_entities = list(acc.entities.values())

        def recency(entity: _TraceEntity) -> float:
            if entity.latest_event_time is None:
                return 1.0
            age = max(0.0, reference_time - entity.latest_event_time)
            if age > self.limits.recency_window_seconds:
                return 0.0
            return math.exp(-age / self.limits.recency_window_seconds)

        active_weights = {entity.trace_id: recency(entity) for entity in active_entities}
        active_total = sum(active_weights.values())
        actionable_weight = sum(
            weight
            for entity in active_entities
            for weight in (active_weights[entity.trace_id],)
            if entity.actionable
        )
        actionability = _unit(actionable_weight / active_total) if active_total else 0.0

        # V is a lineage proportion, with one vote per lineage regardless of
        # how many copied links/receipts point at the same evidence.
        lineage_weights: dict[str, float] = {}
        lineage_has_evidence: dict[str, bool] = {}
        for entity in active_entities:
            lineage = entity.lineage_id
            if not lineage:
                continue
            weight = max(lineage_weights.get(lineage, 0.0), active_weights[entity.trace_id])
            lineage_weights[lineage] = weight
            lineage_has_evidence.setdefault(lineage, False)
            if any(
                evidence_id and evidence_id not in acc.ordinary_outcome_ids
                for evidence_id, outcome_id in entity.evidence
                if outcome_id not in acc.ordinary_outcome_ids
            ):
                lineage_has_evidence[lineage] = True
        lineage_total = sum(lineage_weights.values())
        associated_weight = sum(
            weight for lineage, weight in lineage_weights.items() if lineage_has_evidence.get(lineage)
        )
        evidence_association = _unit(associated_weight / lineage_total) if lineage_total else 0.0

        exposure_count = len(acc.exposures)
        positive_weight = negative_weight = 0.0
        positive_count = negative_count = 0
        for key, rows in acc.feedback.items():
            if key not in acc.exposures:
                continue
            winner = self._feedback_winner(rows)
            if winner is None:
                continue
            value = self._feedback_value(winner)
            if value is None or value == 0.0:
                continue
            trust = max(
                0.0,
                float(
                    winner.trust
                    if winner.trust_declared
                    else self.limits.feedback_trust_default
                ),
            )
            if value > 0:
                positive_weight += trust * value
                positive_count += 1
            else:
                negative_weight += trust * abs(value)
                negative_count += 1
            if winner.source_outcome_id:
                acc.source_outcome_ids.add(winner.source_outcome_id)
        denominator = self.limits.feedback_kappa + exposure_count
        positive = _unit(positive_weight / denominator)
        negative = _unit(negative_weight / denominator)

        denominator_entities = sum(recency(entity) for entity in all_entities)
        if denominator_entities <= 0:
            denominator_entities = float(len(all_entities))
        def proportion(predicate: Any) -> float:
            if not all_entities or denominator_entities <= 0:
                return 0.0
            return _unit(
                sum(recency(entity) for entity in all_entities if predicate(entity))
                / denominator_entities
            )
        duplicate_prop = proportion(lambda entity: entity.duplicate)
        refutation_prop = proportion(lambda entity: entity.refuted or entity.lifecycle == "refuted")
        stale_prop = proportion(lambda entity: entity.stale or entity.lifecycle == "stale")
        def is_stagnant(entity: _TraceEntity) -> bool:
            if entity.stagnation:
                return True
            # When the source provides authoritative activity timestamps, the
            # manifest window also derives current lineage stagnation.  Rows
            # with no timestamp fail closed instead of being guessed stale.
            return bool(
                entity.is_active
                and entity.last_activity_time is not None
                and reference_time - entity.last_activity_time
                >= self.limits.stagnation_window_seconds
            )

        stagnation_prop = proportion(is_stagnant)
        drag_weight_sum = (
            self.limits.duplicate_weight
            + self.limits.refutation_weight
            + self.limits.stale_weight
            + self.limits.lineage_stagnation_weight
        )
        drag = 0.0
        if drag_weight_sum > 0:
            drag = _unit(
                (
                    self.limits.duplicate_weight * duplicate_prop
                    + self.limits.refutation_weight * refutation_prop
                    + self.limits.stale_weight * stale_prop
                    + self.limits.lineage_stagnation_weight * stagnation_prop
                )
                / drag_weight_sum
            )
        source_outcome_ids = tuple(sorted(acc.source_outcome_ids))
        feature_zero = not any((actionability, evidence_association, positive, negative, drag))
        duplicate_entities = {entity.trace_id for entity in all_entities if entity.duplicate}
        refuted_entities = {
            entity.trace_id
            for entity in all_entities
            if entity.refuted or entity.lifecycle == "refuted"
        }
        stale_entities = {
            entity.trace_id
            for entity in all_entities
            if entity.stale or entity.lifecycle == "stale"
        }
        stagnation_entities = {
            entity.trace_id for entity in all_entities if is_stagnant(entity)
        }
        return TraceAllocationProjection(
            task_id=task_id,
            actionability=actionability,
            evidence_association=evidence_association,
            positive_feedback=positive,
            negative_feedback=negative,
            drag=drag,
            frontier_count=sum(1 for entity in active_entities if entity.actionable),
            association_count=len(acc.associations),
            feedback_exposure_count=exposure_count,
            positive_feedback_count=positive_count,
            negative_feedback_count=negative_count,
            duplicate_count=len(duplicate_entities),
            refutation_count=len(refuted_entities),
            stale_count=len(stale_entities),
            lineage_stagnation_count=len(stagnation_entities),
            watermark=watermark,
            source_outcome_ids=source_outcome_ids,
            zero_reason="no_trace_increment" if feature_zero else "",
            positive_feedback_weight=positive_weight,
            negative_feedback_weight=negative_weight,
            active_trace_weight=active_total,
            active_lineage_weight=lineage_total,
            drag_duplicate_proportion=duplicate_prop,
            drag_refutation_proportion=refutation_prop,
            drag_stale_proportion=stale_prop,
            drag_stagnation_proportion=stagnation_prop,
        )

    def project_records(
        self,
        task_ids: Iterable[str],
        records: Iterable[TraceProjectionRecord | Mapping[str, Any]],
        *,
        after_watermark: int = 0,
        source_watermark: int | None = None,
        ordinary_outcome_ids: Iterable[str] = (),
    ) -> TraceAllocationProjectionBatch:
        if after_watermark < 0:
            raise ValueError("after_watermark must not be negative")
        ordered = _bounded_task_ids(task_ids, self.limits.max_tasks)
        allowed = frozenset(ordered)
        ordinary_ids = frozenset(_text(value) for value in ordinary_outcome_ids if _text(value))
        converted_list: list[TraceProjectionRecord] = []
        for index, item in enumerate(records, start=1):
            record = _record(item)
            # CPS progress snapshots predate the allocation watermark field.
            # Treat such a bounded page as ordered-at-read rather than silently
            # dropping every row because its implicit sequence is zero.
            if isinstance(item, Mapping) and not any(
                key in item for key in ("sequence", "seq", "watermark")
            ):
                record = replace(record, sequence=after_watermark + index)
            converted_list.append(record)
        converted = tuple(converted_list)
        newer = tuple(item for item in converted if item.sequence > after_watermark)
        # Stable ascending processing prevents a bounded page from skipping old
        # rows when the caller resumes from the returned watermark.
        newer = tuple(sorted(newer, key=lambda item: (item.sequence, item.canonical_identity)))
        page = newer[: self.limits.max_records]
        truncated = len(newer) > len(page)
        accumulators = {task_id: _TaskAccumulator.empty() for task_id in ordered}
        per_task_seen = {task_id: 0 for task_id in ordered}
        seen_identities: set[tuple[str, ...]] = set()
        records_used = 0

        for record in page:
            if record.task_id not in allowed:
                continue
            if per_task_seen[record.task_id] >= self.limits.max_records_per_task:
                truncated = True
                continue
            per_task_seen[record.task_id] += 1
            if record.canonical_identity in seen_identities:
                continue
            seen_identities.add(record.canonical_identity)
            if (
                record.record_id in ordinary_ids
                or record.source_outcome_id in ordinary_ids
                or record.evidence_id in ordinary_ids
            ):
                continue
            if self._accumulate(accumulators[record.task_id], record):
                records_used += 1

        observed_watermark = max(
            (record.sequence for record in page), default=after_watermark
        )
        if source_watermark is not None:
            if source_watermark < after_watermark:
                raise ValueError("source watermark precedes requested watermark")
            if source_watermark < observed_watermark:
                raise ValueError("source watermark precedes returned records")
            # Do not jump past locally truncated rows.  If the page was fully
            # consumed, the source may provide a cursor beyond ignored rows.
            # A source head is not a proof that every row up to that head was
            # returned.  Never fast-forward a resumable cursor past the last
            # observed record; callers may retry the same page without replay.

        projections = tuple(
            self._finish(task_id, accumulators[task_id], observed_watermark)
            for task_id in ordered
        )
        return TraceAllocationProjectionBatch(
            projections=projections,
            watermark=observed_watermark,
            records_seen=len(page),
            records_used=records_used,
            truncated=truncated,
        )

    @staticmethod
    def _accumulate(acc: _TaskAccumulator, record: TraceProjectionRecord) -> bool:
        kind = record.kind
        if kind in _RAW_VERIFIER_KINDS or record.source in _NON_WORKER_SOURCES:
            return False
        lineage = record.lineage_id
        evidence = record.evidence_id
        event_key = record.record_id or "|".join(record.canonical_identity)
        if kind in _FRONTIER_KINDS:
            # A frontier without lineage identity cannot establish a distinct
            # lineage and is intentionally fail-closed.
            if lineage:
                acc.frontier.add(lineage)
                return True
            return False
        if kind in _ASSOCIATION_KINDS:
            if lineage and evidence:
                acc.associations.add((lineage, evidence))
                if record.source_outcome_id:
                    acc.source_outcome_ids.add(record.source_outcome_id)
                return True
            return False
        if kind in _EXPOSURE_KINDS:
            if not record.exposure_id or not record.worker_id:
                return False
            acc.exposures.add(
                (
                    record.run_id,
                    record.consumer_episode_id,
                    record.exposure_id,
                    record.worker_id,
                    lineage or evidence,
                )
            )
            return True
        if kind in _POSITIVE_FEEDBACK_KINDS:
            if not record.exposure_id or not record.worker_id or not (record.effective and record.terminal):
                return False
            key = (
                record.run_id,
                record.consumer_episode_id,
                record.exposure_id,
                record.worker_id,
                lineage or evidence,
            )
            if not any(item[:4] == key[:4] for item in acc.exposures):
                return False
            acc.positive.add((record.exposure_id, record.worker_id, lineage or evidence))
            if record.source_outcome_id:
                acc.source_outcome_ids.add(record.source_outcome_id)
            return True
        if kind in _NEGATIVE_FEEDBACK_KINDS:
            if not record.exposure_id or not record.worker_id or not (record.effective and record.terminal):
                return False
            key = (
                record.run_id,
                record.consumer_episode_id,
                record.exposure_id,
                record.worker_id,
                lineage or evidence,
            )
            if not any(item[:4] == key[:4] for item in acc.exposures):
                return False
            acc.negative.add((record.exposure_id, record.worker_id, lineage or evidence))
            if record.source_outcome_id:
                acc.source_outcome_ids.add(record.source_outcome_id)
            return True
        if kind in _DUPLICATE_KINDS:
            acc.duplicate.add(lineage or evidence or event_key)
            return True
        if kind in _REFUTATION_KINDS:
            acc.refutation.add(lineage or evidence or event_key)
            return True
        if kind in _STALE_KINDS:
            acc.stale.add(lineage or evidence or event_key)
            return True
        if kind in _STAGNATION_KINDS:
            acc.stagnation.add(lineage or event_key)
            return True
        return False

    def _finish(
        self, task_id: str, acc: _TaskAccumulator, watermark: int
    ) -> TraceAllocationProjection:
        frontier_count = len(acc.frontier)
        association_count = len(acc.associations)
        exposure_count = len(acc.exposures)
        positive_count = len(acc.positive)
        negative_count = len(acc.negative)
        duplicate_count = len(acc.duplicate)
        refutation_count = len(acc.refutation)
        stale_count = len(acc.stale)
        stagnation_count = len(acc.stagnation)
        drag_count = duplicate_count + refutation_count + stale_count + stagnation_count
        weighted_drag = (
            self.limits.duplicate_weight * duplicate_count
            + self.limits.refutation_weight * refutation_count
            + self.limits.stale_weight * stale_count
            + self.limits.lineage_stagnation_weight * stagnation_count
        )
        source_outcome_ids = tuple(sorted(acc.source_outcome_ids))
        feature_zero = not any((frontier_count, association_count, positive_count, negative_count, drag_count))
        return TraceAllocationProjection(
            task_id=task_id,
            actionability=_unit(frontier_count / self.limits.actionability_saturation),
            evidence_association=_unit(
                association_count / self.limits.association_saturation
            ),
            positive_feedback=_unit(
                positive_count / exposure_count if exposure_count else 0.0
            ),
            negative_feedback=_unit(
                negative_count / exposure_count if exposure_count else 0.0
            ),
            drag=_unit(weighted_drag / self.limits.drag_saturation),
            frontier_count=frontier_count,
            association_count=association_count,
            feedback_exposure_count=exposure_count,
            positive_feedback_count=positive_count,
            negative_feedback_count=negative_count,
            duplicate_count=duplicate_count,
            refutation_count=refutation_count,
            stale_count=stale_count,
            lineage_stagnation_count=stagnation_count,
            watermark=watermark,
            source_outcome_ids=source_outcome_ids,
            zero_reason="no_trace_increment" if feature_zero else "",
        )


def _record(
    item: TraceProjectionRecord | Mapping[str, Any],
) -> TraceProjectionRecord:
    if isinstance(item, TraceProjectionRecord):
        return item
    if isinstance(item, Mapping):
        return TraceProjectionRecord.from_mapping(item)
    raise TypeError("trace projection records must be records or mappings")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _kind(value) in {"1", "true", "yes", "effective", "terminal", "valid"}


def _bounded_task_ids(task_ids: Iterable[str], limit: int) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in task_ids:
        task_id = _text(value)
        if not task_id or task_id in seen:
            continue
        if len(ordered) >= limit:
            raise ValueError(f"trace projection exceeds the {limit}-task bound")
        seen.add(task_id)
        ordered.append(task_id)
    return tuple(ordered)


def build_synthetic_trace_projection(
    features_by_task: Mapping[str, Mapping[str, Any]] | Iterable[str],
    *,
    watermark: int = 0,
) -> TraceAllocationProjectionBatch:
    """Build a deterministic fixture without fabricating selector records.

    Passing an iterable of task IDs returns an all-zero projection.  A mapping
    may set the five public features and optional bounded diagnostic counts.
    This helper is intended for allocator tests and development-only empty
    projection runs while the selector store is unavailable.
    """

    if watermark < 0:
        raise ValueError("watermark must not be negative")
    if isinstance(features_by_task, Mapping):
        items = features_by_task.items()
    else:
        items = ((task_id, {}) for task_id in features_by_task)
    projections: list[TraceAllocationProjection] = []
    allowed = {
        "actionability",
        "evidence_association",
        "positive_feedback",
        "negative_feedback",
        "drag",
        "frontier_count",
        "association_count",
        "feedback_exposure_count",
        "positive_feedback_count",
        "negative_feedback_count",
        "duplicate_count",
        "refutation_count",
        "stale_count",
        "lineage_stagnation_count",
    }
    for task_id, raw in items:
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError("unknown synthetic projection fields: " + ", ".join(sorted(unknown)))
        unit_fields = {
            name: _unit(raw.get(name, 0.0))
            for name in (
                "actionability",
                "evidence_association",
                "positive_feedback",
                "negative_feedback",
                "drag",
            )
        }
        count_fields = {
            name: _nonnegative_int(raw.get(name, 0))
            for name in allowed
            if name.endswith("_count")
        }
        projections.append(
            TraceAllocationProjection(
                task_id=_text(task_id),
                **unit_fields,
                **count_fields,
                watermark=watermark,
                zero_reason=(
                    "synthetic_zero_projection"
                    if not any(unit_fields.values())
                    else ""
                ),
            )
        )
    return TraceAllocationProjectionBatch(tuple(projections), watermark)
