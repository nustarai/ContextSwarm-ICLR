"""Safe runner bridge from a selected trace store to allocation features.

The Figure 4 allocator deliberately consumes a much smaller surface than the
Figure 3 selector.  This module is the boundary: it returns only normalized
features and stable opaque identifiers.  It never returns selector queries,
ranking payloads, CPS bodies, filesystem paths, or verifier/Judge payloads.

Two store generations are supported:

* a future store-native ``read_allocation_projection_records`` protocol; and
* the current Issue #38 SQLite attribution store, which has no cross-table
  append sequence.  That schema is therefore read as one complete bounded
  materialization for each decision, never as an unsafe incremental page.

Any absent, incompatible, incomplete, or over-limit source fails closed to an
explicit all-zero projection.  A deterministic synthetic projection can be
injected by tests and development tooling without fabricating selector rows.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence

from .allocation_projection import (
    TraceAllocationProjectionAdapter,
    TraceAllocationProjectionBatch,
    TraceProjectionLimits,
    TraceProjectionRecordBatch,
    build_synthetic_trace_projection,
)


_CANONICAL_FEEDBACK_KINDS = frozenset(
    {
        "useful",
        "not_useful",
        "misleading",
        "stale",
        "unsafe",
        "duplicate",
        "diagnostic_useful",
        "needs_refinement",
        "not_used",
        "route_attempted",
        "route_improving",
    }
)
_REQUIRED_SELECTION_TABLES = frozenset(
    {
        "search_events",
        "exposures",
        "exposure_items",
        "feedback_events",
    }
)
_TRACE_POLICIES = frozenset({"trace_state", "llm_scheduler"})


class _ProjectionSource(Protocol):
    def read_allocation_projection_records(
        self,
        task_ids: Sequence[str],
        *,
        after_watermark: int,
        limit: int,
    ) -> TraceProjectionRecordBatch: ...


@dataclass(frozen=True)
class AllocationTraceView:
    """One bounded, immutable trace view for a core allocation snapshot."""

    batch: TraceAllocationProjectionBatch
    watermark: str
    source: str
    complete: bool
    fallback_reason: str = ""
    trace_references: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        if not self.watermark or len(self.watermark) > 512:
            raise ValueError("trace watermark must be non-empty and bounded")
        if self.source not in {
            "selection_store_protocol",
            "selection_store_sqlite_v1",
            "synthetic",
            "zero",
        }:
            raise ValueError("unsupported trace projection source")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        task_ids = tuple(task_id for task_id, _values in self.trace_references)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("trace reference task IDs must be unique")
        for task_id, values in self.trace_references:
            if not task_id or len(task_id) > 512:
                raise ValueError("trace reference task ID must be non-empty and bounded")
            if len(values) > 100 or len(values) != len(set(values)):
                raise ValueError("trace references must be unique and bounded")
            if any(not value or len(value) > 512 for value in values):
                raise ValueError("trace reference IDs must be non-empty and bounded")

    def for_task(self, task_id: str):
        return self.batch.for_task(task_id)

    def references_for_task(self, task_id: str) -> tuple[str, ...]:
        for current, references in self.trace_references:
            if current == task_id:
                return references
        return ()


def policy_reads_trace(policy: str) -> bool:
    """Return whether the registered allocator may consult trace state."""

    return str(policy).strip() in _TRACE_POLICIES


def _ordered_task_ids(task_ids: Iterable[str], *, maximum: int) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in task_ids:
        task_id = str(raw or "").strip()
        if not task_id or task_id in seen:
            continue
        if len(task_id) > 512:
            raise ValueError("task_id exceeds 512 characters")
        if len(result) >= maximum:
            raise ValueError(f"trace projection exceeds the {maximum}-task bound")
        seen.add(task_id)
        result.append(task_id)
    return tuple(result)


def _canonical_sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_reason(exc: BaseException) -> str:
    # Only the class is retained: exception messages can contain a private DB
    # path, SQL detail, or provider endpoint.
    return f"projection_unavailable:{type(exc).__name__}"[:128]


def _feedback_mapping(values: Mapping[str, Any] | None) -> dict[str, float]:
    if values is None:
        return {}
    keys = {str(key) for key in values}
    if keys != _CANONICAL_FEEDBACK_KINDS:
        raise ValueError("feedback_values must cover exactly the canonical feedback kinds")
    result: dict[str, float] = {}
    for kind in sorted(keys):
        value = values[kind]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"feedback_values.{kind} must be finite")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"feedback_values.{kind} must be finite")
        result[kind] = number
    return result


class SelectionStoreTraceSource:
    """Read only the public attribution topology from SelectionStore v1.

    The current store has no single sequence spanning exposure and feedback
    tables.  ``read_complete_records`` consequently pins one SQLite read
    transaction and emits the full bounded state.  It does not pretend that a
    table-local rowid or timestamp is a resumable global cursor.
    """

    def __init__(
        self,
        store: Any,
        *,
        feedback_values: Mapping[str, Any] | None,
        max_records: int = 4096,
    ) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self.store = store
        self.feedback_values = _feedback_mapping(feedback_values)
        self.max_records = int(max_records)

    @contextmanager
    def _read_db(self) -> Iterator[sqlite3.Connection]:
        path = self.store if isinstance(self.store, (str, Path)) else getattr(self.store, "path", None)
        if path is not None:
            # Prefer a separate mode=ro connection even when the object also
            # exposes SelectionStore._db().  The latter enables WAL and can
            # mutate store metadata, which would violate this bridge's
            # read-only boundary.
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA query_only=ON")
                yield db
            finally:
                db.close()
            return
        factory = getattr(self.store, "_db", None)
        if callable(factory):
            # A protocol-only test double may have no path.  Keep this fallback
            # narrow; real SelectionStore instances always have a path.
            with factory() as db:
                db.execute("PRAGMA query_only=ON")
                yield db
            return
        raise TypeError("selection store has no read-only database surface")

    @staticmethod
    def _schema_ok(db: sqlite3.Connection) -> bool:
        names = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return _REQUIRED_SELECTION_TABLES.issubset(names)

    def read_complete_records(
        self, task_ids: Sequence[str]
    ) -> tuple[tuple[dict[str, Any], ...], str]:
        ordered = tuple(task_ids)
        if not ordered:
            return (), _canonical_sha({"schema": "selection_store_v1", "records": []})
        placeholders = ",".join("?" for _ in ordered)
        with self._read_db() as db:
            if not self._schema_ok(db):
                raise ValueError("selection store schema is incompatible")
            db.execute("BEGIN")
            try:
                # Task attribution is the task whose worker received the trace:
                # item -> exposure -> search.  exposure_item_id is the exposure
                # identity because one parent exposure may deliver many items.
                exposure_rows = list(
                    db.execute(
                        f"""SELECT item.exposure_item_id, item.trace_id,
                                   exposure.actor_id, search.task_id
                              FROM exposure_items AS item
                              JOIN exposures AS exposure
                                ON exposure.exposure_id = item.exposure_id
                              JOIN search_events AS search
                                ON search.search_event_id = exposure.search_event_id
                             WHERE search.task_id IN ({placeholders})
                               AND item.trace_id <> ''
                             ORDER BY search.task_id, item.exposure_item_id""",
                        ordered,
                    )
                )
                feedback_rows = list(
                    db.execute(
                        f"""SELECT feedback.feedback_event_id,
                                   feedback.exposure_item_id,
                                   feedback.trace_id,
                                   feedback.actor_id,
                                   feedback.feedback_kind,
                                   search.task_id
                              FROM feedback_events AS feedback
                              JOIN exposure_items AS item
                                ON item.exposure_item_id = feedback.exposure_item_id
                              JOIN exposures AS exposure
                                ON exposure.exposure_id = item.exposure_id
                              JOIN search_events AS search
                                ON search.search_event_id = exposure.search_event_id
                             WHERE feedback.event_class = 'worker_interaction'
                               AND feedback.terminal = 1
                               AND feedback.effective = 1
                               AND feedback.actor_id = exposure.actor_id
                               AND feedback.trace_id = item.trace_id
                               AND feedback.feedback_kind IN ({','.join('?' for _ in _CANONICAL_FEEDBACK_KINDS)})
                               AND search.task_id IN ({placeholders})
                             ORDER BY search.task_id, feedback.feedback_event_id""",
                        tuple(sorted(_CANONICAL_FEEDBACK_KINDS)) + ordered,
                    )
                )
            finally:
                db.execute("ROLLBACK")

        total = len(exposure_rows) + len(feedback_rows)
        if total > self.max_records:
            raise OverflowError("selection projection exceeds its record bound")
        if feedback_rows and not self.feedback_values:
            # Polarity must come from the frozen selector contract.  Kind names
            # and arbitrary feedback payloads are not an acceptable substitute.
            raise ValueError("selection-store feedback projection requires feedback_values")
        records: list[dict[str, Any]] = []
        sequence = 0
        for row in exposure_rows:
            sequence += 1
            records.append(
                {
                    "sequence": sequence,
                    "record_id": str(row["exposure_item_id"]),
                    "task_id": str(row["task_id"]),
                    "kind": "worker_exposure",
                    "evidence_id": str(row["trace_id"]),
                    "worker_id": str(row["actor_id"]),
                    "exposure_id": str(row["exposure_item_id"]),
                    "source": "worker",
                }
            )
        for row in feedback_rows:
            kind = str(row["feedback_kind"])
            if kind not in self.feedback_values:
                raise ValueError("effective feedback has no registered polarity")
            value = self.feedback_values[kind]
            if value == 0.0:
                continue
            sequence += 1
            records.append(
                {
                    "sequence": sequence,
                    "record_id": str(row["feedback_event_id"]),
                    "task_id": str(row["task_id"]),
                    "kind": "feedback_positive" if value > 0 else "feedback_negative",
                    "evidence_id": str(row["trace_id"]),
                    "worker_id": str(row["actor_id"]),
                    "exposure_id": str(row["exposure_item_id"]),
                    "source": "worker",
                    "effective": True,
                    "terminal": True,
                }
            )
        # Hash only bounded public topology.  This is a full materialization ID,
        # not a cursor; paths, payloads, query text, and timestamps are absent.
        watermark = _canonical_sha(
            {"schema": "selection_store_v1_projection", "records": records}
        )
        return tuple(records), watermark


class TraceProjectionBridge:
    """Resolve one complete allocation trace view, with explicit fail-closed zero."""

    def __init__(
        self,
        *,
        limits: TraceProjectionLimits | None = None,
        synthetic_features: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.limits = limits or TraceProjectionLimits()
        self.adapter = TraceAllocationProjectionAdapter(self.limits)
        self.synthetic_features = (
            {str(key): dict(value) for key, value in synthetic_features.items()}
            if synthetic_features is not None
            else None
        )

    def zero(self, task_ids: Iterable[str], *, reason: str = "trace_store_unavailable") -> AllocationTraceView:
        ordered = _ordered_task_ids(task_ids, maximum=self.limits.max_tasks)
        batch = build_synthetic_trace_projection(ordered)
        return AllocationTraceView(
            batch=batch,
            watermark="zero:" + _canonical_sha({"tasks": ordered, "reason": reason}),
            source="zero",
            complete=True,
            fallback_reason=str(reason)[:128],
        )

    def read(
        self,
        task_ids: Iterable[str],
        *,
        store: Any | None = None,
        feedback_values: Mapping[str, Any] | None = None,
    ) -> AllocationTraceView:
        ordered = _ordered_task_ids(task_ids, maximum=self.limits.max_tasks)
        if self.synthetic_features is not None:
            selected = {task_id: self.synthetic_features.get(task_id, {}) for task_id in ordered}
            batch = build_synthetic_trace_projection(selected)
            return AllocationTraceView(
                batch=batch,
                watermark="synthetic:" + _canonical_sha(selected),
                source="synthetic",
                complete=True,
            )
        if store is None:
            return self.zero(ordered)
        protocol = getattr(store, "read_allocation_projection_records", None)
        if callable(protocol):
            try:
                # A complete state is requested from origin.  Truncation is not
                # silently interpreted as zero or as the current trace state.
                batch = self.adapter.project(store, ordered, after_watermark=0)
                if batch.truncated:
                    raise OverflowError("store-native projection is incomplete")
                return AllocationTraceView(
                    batch=batch,
                    watermark=f"protocol:{batch.watermark}",
                    source="selection_store_protocol",
                    complete=True,
                )
            except Exception as exc:
                return self.zero(ordered, reason=_bounded_reason(exc))
        try:
            source = SelectionStoreTraceSource(
                store,
                feedback_values=feedback_values,
                max_records=self.limits.max_records,
            )
            records, watermark = source.read_complete_records(ordered)
            batch = self.adapter.project_records(
                ordered,
                records,
                after_watermark=0,
                source_watermark=len(records),
            )
            if batch.truncated:
                raise OverflowError("selection projection is incomplete")
            return AllocationTraceView(
                batch=batch,
                watermark="sqlite-v1:" + watermark,
                source="selection_store_sqlite_v1",
                complete=True,
                trace_references=tuple(
                    (
                        task_id,
                        tuple(
                            sorted(
                                {
                                    str(record.get("evidence_id") or "")
                                    for record in records
                                    if record.get("task_id") == task_id
                                    and record.get("evidence_id")
                                }
                            )[:100]
                        ),
                    )
                    for task_id in ordered
                ),
            )
        except Exception as exc:
            return self.zero(ordered, reason=_bounded_reason(exc))


def feedback_values_from_config(config: Any) -> Mapping[str, Any] | None:
    """Read the frozen selector feedback mapping without importing #38 types."""

    selection = getattr(config, "selection", None)
    params = getattr(selection, "policy_params", None)
    if isinstance(params, Mapping):
        values = params.get("feedback_values")
        return values if isinstance(values, Mapping) else None
    extra = getattr(config, "extra", None)
    raw = extra.get("raw") if isinstance(extra, Mapping) else None
    selection_raw = raw.get("selection") if isinstance(raw, Mapping) else None
    policy_params = (
        selection_raw.get("policy_params") if isinstance(selection_raw, Mapping) else None
    )
    values = policy_params.get("feedback_values") if isinstance(policy_params, Mapping) else None
    return values if isinstance(values, Mapping) else None


__all__ = [
    "AllocationTraceView",
    "SelectionStoreTraceSource",
    "TraceProjectionBridge",
    "feedback_values_from_config",
    "policy_reads_trace",
]
