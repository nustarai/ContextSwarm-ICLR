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
import math
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
    }
)
_NON_WORKER_SOURCES = frozenset(
    {"checker", "evaluator", "judge", "runner", "verifier"}
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


@dataclass(frozen=True)
class TraceProjectionLimits:
    """Manifest-owned projection bounds and normalization saturations."""

    max_tasks: int = 256
    max_records: int = 4096
    max_records_per_task: int = 256
    actionability_saturation: int = 4
    association_saturation: int = 4
    drag_saturation: int = 4
    duplicate_weight: float = 1.0
    refutation_weight: float = 1.0
    stale_weight: float = 1.0
    lineage_stagnation_weight: float = 1.0

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
        )

    @property
    def canonical_identity(self) -> tuple[str, ...]:
        """Stable event identity used to suppress receipt/event replay."""

        if self.record_id:
            return ("id", self.record_id)
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
        )


@dataclass(frozen=True)
class TraceProjectionRecordBatch:
    """Bounded source response with an inclusive high watermark."""

    records: tuple[TraceProjectionRecord | Mapping[str, Any], ...]
    watermark: int

    def __post_init__(self) -> None:
        if self.watermark < 0:
            raise ValueError("watermark must not be negative")


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

    def __post_init__(self) -> None:
        task_ids = tuple(item.task_id for item in self.projections)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("projection task_ids must be unique")
        if min(self.watermark, self.records_seen, self.records_used) < 0:
            raise ValueError("projection batch counts must not be negative")
        if self.records_used > self.records_seen:
            raise ValueError("records_used must not exceed records_seen")

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
    exposures: set[tuple[str, str, str]]
    positive: set[tuple[str, str, str]]
    negative: set[tuple[str, str, str]]
    duplicate: set[str]
    refutation: set[str]
    stale: set[str]
    stagnation: set[str]
    source_outcome_ids: set[str]

    @classmethod
    def empty(cls) -> "_TaskAccumulator":
        return cls(set(), set(), set(), set(), set(), set(), set(), set(), set(), set())


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
    ) -> TraceAllocationProjectionBatch:
        ordered = _bounded_task_ids(task_ids, self.limits.max_tasks)
        batch = source.read_allocation_projection_records(
            ordered,
            after_watermark=after_watermark,
            limit=self.limits.max_records,
        )
        return self.project_records(
            ordered,
            batch.records,
            after_watermark=after_watermark,
            source_watermark=batch.watermark,
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
            if not truncated:
                observed_watermark = max(observed_watermark, source_watermark)

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
            acc.exposures.add((record.exposure_id, record.worker_id, lineage or evidence))
            return True
        if kind in _POSITIVE_FEEDBACK_KINDS:
            if not record.exposure_id or not record.worker_id or not (record.effective and record.terminal):
                return False
            key = (record.exposure_id, record.worker_id, lineage or evidence)
            if not any(item[:2] == key[:2] for item in acc.exposures):
                return False
            acc.positive.add(key)
            if record.source_outcome_id:
                acc.source_outcome_ids.add(record.source_outcome_id)
            return True
        if kind in _NEGATIVE_FEEDBACK_KINDS:
            if not record.exposure_id or not record.worker_id or not (record.effective and record.terminal):
                return False
            key = (record.exposure_id, record.worker_id, lineage or evidence)
            if not any(item[:2] == key[:2] for item in acc.exposures):
                return False
            acc.negative.add(key)
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
