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
import inspect
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence

from .allocation_projection import (
    TraceAllocationProjectionAdapter,
    TraceAllocationProjectionBatch,
    TraceProjectionRecord,
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
class TraceProjectionSnapshotPage:
    """One page of a pinned, full-current trace projection.

    ``trace_watermark`` identifies the causal snapshot, while ``next_cursor``
    is only a pagination cursor *inside that snapshot*.  They are deliberately
    separate: a cursor must never be persisted as the allocator's state
    identity, and a source head must not be used to skip records which have not
    yet been materialized.
    """

    records: tuple[Any, ...]
    trace_watermark: str
    next_cursor: str = ""
    complete: bool = True
    source_watermark: int | str | None = None
    snapshot_id: str = ""
    # Optional source-owned recency cut.  It is metadata only; it is never
    # exposed to the allocator as free-form text.  Keeping it on the page
    # lets a paged source attest that one-shot and paged materializations use
    # the same reference clock.
    reference_time: float | None = None

    def __post_init__(self) -> None:
        watermark = str(self.trace_watermark or "").strip()
        if not watermark or len(watermark) > 512:
            raise ValueError("trace_watermark must be non-empty and bounded")
        object.__setattr__(self, "trace_watermark", watermark)
        cursor = str(self.next_cursor or "").strip()
        if len(cursor) > 512:
            raise ValueError("next_cursor must be at most 512 characters")
        object.__setattr__(self, "next_cursor", cursor)
        snapshot_id = str(self.snapshot_id or "").strip()
        if len(snapshot_id) > 512:
            raise ValueError("snapshot_id must be at most 512 characters")
        object.__setattr__(self, "snapshot_id", snapshot_id)
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        if self.complete and cursor:
            raise ValueError("a complete projection page must not have next_cursor")
        if not self.complete and not cursor:
            raise ValueError("an incomplete projection page requires next_cursor")
        if self.source_watermark is not None:
            if isinstance(self.source_watermark, bool):
                raise ValueError("source_watermark must be a bounded scalar")
            if isinstance(self.source_watermark, int):
                if self.source_watermark < 0:
                    raise ValueError("source_watermark must be non-negative")
            elif isinstance(self.source_watermark, str):
                value = self.source_watermark.strip()
                if not value or len(value) > 512:
                    raise ValueError("source_watermark must be a bounded scalar")
                object.__setattr__(self, "source_watermark", value)
            else:
                raise ValueError("source_watermark must be a bounded scalar")
        if self.reference_time is not None:
            if isinstance(self.reference_time, bool):
                raise ValueError("reference_time must be finite")
            try:
                reference_time = float(self.reference_time)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("reference_time must be finite") from None
            if not math.isfinite(reference_time):
                raise ValueError("reference_time must be finite")
            object.__setattr__(self, "reference_time", reference_time)
        object.__setattr__(self, "records", tuple(self.records))


class TraceProjectionSnapshotSource(Protocol):
    """Source protocol for a complete, causally pinned projection.

    The first call uses ``as_of_watermark=None`` and an empty cursor.  Every
    subsequent call must echo the returned ``trace_watermark`` and use the
    returned cursor.  A source may paginate, but it must keep the watermark
    fixed until ``complete=True``.
    """

    def read_allocation_projection_snapshot(
        self,
        task_ids: Sequence[str],
        *,
        as_of_watermark: str | None,
        cursor: str,
        limit: int,
    ) -> TraceProjectionSnapshotPage: ...


def _snapshot_identity(
    task_ids: Sequence[str],
    source_watermark: int | str | None,
    records: Sequence[Any],
    ordinary_outcome_ids: Iterable[str],
    *,
    snapshot_id: str = "",
    reference_time: float | None = None,
    trace_watermark: str | None = None,
) -> str:
    """Hash only the bounded projection contract, never raw source metadata."""

    # Keep this list deliberately explicit.  These are the scalar fields that
    # can affect the bounded allocator projection, including the newer
    # full-current-state topology fields.  Query/payload/body fields must
    # never be copied into an identity hash or an allocation artifact.
    normalized = [_projection_signature(record) for record in records]
    normalized.sort(
        key=lambda item: json.dumps(
            item, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    )
    return _canonical_sha(
        {
            "schema": "contextswarm_trace_projection_snapshot_v3",
            "task_ids": list(task_ids),
            # Keep the causal trace watermark and the source-owned opaque
            # watermark separate.  A source watermark may change while the
            # normalized records happen to remain equal; that still denotes a
            # different pinned materialization and must invalidate stale state.
            "trace_watermark": str(
                trace_watermark if trace_watermark is not None else source_watermark
            ),
            "source_watermark": (
                None if source_watermark is None else str(source_watermark)
            ),
            "snapshot_id": str(snapshot_id or "")[:512],
            "reference_time": reference_time,
            "ordinary_outcome_ids": sorted(
                {str(value).strip() for value in ordinary_outcome_ids if str(value).strip()}
            ),
            "records": normalized,
        }
    )


_SAFE_PROJECTION_FIELDS = (
    "sequence",
    "record_id",
    "task_id",
    "kind",
    "lineage_id",
    "evidence_id",
    "worker_id",
    "source",
    "source_outcome_id",
    "exposure_id",
    "effective",
    "terminal",
    "effective_declared",
    "terminal_declared",
    "trace_id",
    "lifecycle",
    "active",
    "actionable",
    "event_time",
    "trust",
    "trust_declared",
    "trust_rank",
    "feedback_value",
    "committed_sequence",
    "run_id",
    "consumer_episode_id",
    "target_trace_id",
    "relation_kind",
)


def _safe_projection_value(value: Any) -> Any:
    """Normalize one identity scalar without retaining arbitrary source data."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 2**63 - 1:
            raise ValueError("projection identity contains an out-of-range integer")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("projection identity contains a non-finite number")
        return value
    if isinstance(value, str):
        value = value.strip()
        if len(value) > 512:
            raise ValueError("projection identity contains an overlong scalar")
        return value
    raise ValueError("projection identity contains an unsupported value")


def _projection_signature(record: Any) -> dict[str, Any]:
    """Return a canonical, bounded scalar view of a source record.

    ``TraceProjectionRecord`` grew fields as the full-state contract became
    explicit.  Extracting by attribute keeps this bridge compatible with both
    old and new adapters; mapping aliases are normalized through the record
    class first, while only the allowlisted topology scalars are retained.
    """

    if isinstance(record, Mapping):
        normalized = TraceProjectionRecord.from_mapping(record)
        result: dict[str, Any] = {}
        for field in _SAFE_PROJECTION_FIELDS:
            if hasattr(normalized, field):
                result[field] = _safe_projection_value(getattr(normalized, field))
            elif field in record:
                result[field] = _safe_projection_value(record[field])
        # Preserve newer aliases even when an older TraceProjectionRecord does
        # not know them yet.
        aliases = {
            "trace_id": ("trace_id",),
            "lifecycle": ("lifecycle", "status"),
            "active": ("active", "is_active"),
            "actionable": ("actionable", "is_actionable"),
            "event_time": ("event_time", "timestamp", "created_seconds", "created_at", "observed_at"),
            "trust": ("trust", "trust_weight", "trust_score"),
            "trust_rank": ("trust_rank", "authority_rank"),
            "feedback_value": ("feedback_value", "polarity"),
            "committed_sequence": ("committed_sequence", "commit_sequence"),
            "run_id": ("run_id", "experiment_id"),
            "consumer_episode_id": ("consumer_episode_id", "episode_id", "consumer_id"),
            "target_trace_id": ("target_trace_id", "target_piece_id"),
            "relation_kind": ("relation_kind", "relation"),
        }
        for field, keys in aliases.items():
            if field in result:
                continue
            for key in keys:
                if key in record:
                    result[field] = _safe_projection_value(record[key])
                    break
        return result
    result = {}
    for field in _SAFE_PROJECTION_FIELDS:
        if hasattr(record, field):
            result[field] = _safe_projection_value(getattr(record, field))
    if not result:
        raise TypeError("trace projection snapshot records must be records or mappings")
    return result


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
            "selection_store_snapshot",
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
    # Non-feedback selectors expose an explicit empty mapping.  That is a
    # valid frozen configuration as long as the store has no effective
    # feedback rows; ``read_complete_records`` enforces that latter condition.
    if values is None or not values:
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


class SelectionRuntimeTraceSource:
    """Explicit adapter from an Issue #38 ``SelectionRuntime`` to trace state.

    ``SelectionRuntime.search`` and ``SelectionStore.effective_feedback`` are
    selector APIs, not allocation projections.  This adapter intentionally
    unwraps only the runtime's attribution store and frozen feedback mapping,
    then delegates to the bounded full-materialization reader above.  It does
    not call the selector, inspect CPS pieces, or expose raw search results.

    SelectionStore v1 cannot replay historical snapshots.  A requested
    ``as_of_watermark`` is therefore accepted only when it still matches the
    freshly materialized content hash; otherwise the adapter fails closed.
    """

    def __init__(self, runtime: Any, *, max_records: int = 4096) -> None:
        selection_store = getattr(runtime, "selection_store", None)
        if selection_store is None:
            raise TypeError("selection runtime has no selection_store")
        values = getattr(runtime, "feedback_values", None)
        if not isinstance(values, Mapping):
            # A runtime with no configured polarity must not silently infer
            # positive/negative meaning from selector feedback-kind labels.
            values = None
        self._source = SelectionStoreTraceSource(
            selection_store,
            feedback_values=values,
            max_records=max_records,
        )

    def read_allocation_projection_snapshot(
        self,
        task_ids: Sequence[str],
        *,
        as_of_watermark: str | None,
        cursor: str,
        limit: int,
    ) -> TraceProjectionSnapshotPage:
        if cursor:
            raise ValueError("SelectionStore v1 projection is not cursor-paged")
        if limit <= 0:
            raise ValueError("projection limit must be positive")
        records, watermark = self._source.read_complete_records(task_ids)
        if len(records) > limit:
            raise OverflowError("selection projection exceeds the requested bound")
        if as_of_watermark is not None and str(as_of_watermark) != watermark:
            raise ValueError("requested selection projection snapshot is no longer available")
        return TraceProjectionSnapshotPage(
            records=records,
            trace_watermark=watermark,
            complete=True,
        )


def _record_evidence_id(record: Any) -> str:
    if isinstance(record, Mapping):
        return str(
            record.get("evidence_id")
            or record.get("piece_id")
            or record.get("context_piece_id")
            or ""
        ).strip()
    return str(getattr(record, "evidence_id", "") or "").strip()


def _record_id(record: Any) -> str:
    if isinstance(record, Mapping):
        return str(
            record.get("record_id")
            or record.get("event_id")
            or record.get("id")
            or ""
        ).strip()
    return str(getattr(record, "record_id", "") or "").strip()


def _record_source_outcome_id(record: Any) -> str:
    if isinstance(record, Mapping):
        return str(
            record.get("source_outcome_id")
            or record.get("outcome_id")
            or ""
        ).strip()
    return str(getattr(record, "source_outcome_id", "") or "").strip()


def _record_trace_id(record: Any) -> str:
    if isinstance(record, Mapping):
        return str(
            record.get("trace_id")
            or record.get("piece_id")
            or record.get("context_piece_id")
            or record.get("evidence_id")
            or ""
        ).strip()
    return str(
        getattr(record, "trace_id", "")
        or getattr(record, "evidence_id", "")
        or ""
    ).strip()


def _projection_record(record: Any) -> TraceProjectionRecord:
    """Normalize a source row without retaining its unbounded payload.

    Snapshot sources are untrusted adapters.  Normalizing at the bridge
    boundary gives duplicate/cursor validation the same bounded identity
    semantics as the projection adapter and prevents a source-specific row
    object from leaking into later artifact code.
    """

    if isinstance(record, TraceProjectionRecord):
        return record
    if isinstance(record, Mapping):
        return TraceProjectionRecord.from_mapping(record)
    raise TypeError("trace projection snapshot records must be records or mappings")


def _projection_record_identity(record: TraceProjectionRecord) -> tuple[str, ...]:
    """Return an identity scoped to task/source for replay validation."""

    signature = _projection_signature(record)
    record_id = str(signature.get("record_id") or "")
    if record_id:
        return (
            "id",
            str(signature.get("task_id") or ""),
            str(signature.get("source") or "worker"),
            record_id,
        )
    # Without a stable source ID, use only the identity-bearing topology
    # fields.  Mutable state (lifecycle, active, trust, timestamps, …) is
    # deliberately excluded so that a replay with changed state maps to the
    # same key and is rejected as contradictory below.
    return (
        "semantic",
        str(signature.get("sequence", "")),
        str(signature.get("task_id", "")),
        str(signature.get("kind", "")),
        str(signature.get("lineage_id", "")),
        str(signature.get("evidence_id", "")),
        str(signature.get("worker_id", "")),
        str(signature.get("source", "worker")),
        str(signature.get("source_outcome_id", "")),
        str(signature.get("trace_id", "")),
        str(signature.get("exposure_id", "")),
    )


def _validate_snapshot_records(records: Sequence[Any]) -> None:
    """Reject contradictory duplicate rows inside one pinned snapshot.

    Exact replay of a row is harmless and is deduplicated by the projection
    adapter.  A stable record identity carrying different fields is not
    harmless: accepting whichever page happened to arrive first makes the
    allocation state depend on pagination/retry order.  Such a source cannot
    provide a causal snapshot and must fail closed.
    """

    seen: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw in records:
        record = _projection_record(raw)
        identity = _projection_record_identity(record)
        previous = seen.get(identity)
        signature = _projection_signature(record)
        if previous is not None and previous != signature:
            raise ValueError("snapshot contains contradictory duplicate records")
        seen[identity] = signature


def _record_task_id(record: Any) -> str:
    if isinstance(record, Mapping):
        return str(record.get("task_id") or "").strip()
    return str(getattr(record, "task_id", "") or "").strip()


def _trace_references(
    task_ids: Sequence[str],
    records: Sequence[Any],
    ordinary_outcome_ids: Iterable[str] = (),
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    by_task: dict[str, set[str]] = {task_id: set() for task_id in task_ids}
    ordinary = {str(value).strip() for value in ordinary_outcome_ids if str(value).strip()}
    for record in records:
        task_id = _record_task_id(record)
        trace_id = _record_trace_id(record)
        evidence_id = _record_evidence_id(record)
        ordinary_aliases = (
            _record_id(record),
            _record_source_outcome_id(record),
            evidence_id,
            trace_id,
        )
        if (
            task_id in by_task
            and trace_id
            and ordinary.isdisjoint(value for value in ordinary_aliases if value)
        ):
            by_task[task_id].add(trace_id[:512])
    return tuple(
        (task_id, tuple(sorted(by_task[task_id])[:100]))
        for task_id in task_ids
    )


def _legacy_batch_is_complete(batch: TraceProjectionRecordBatch) -> bool:
    """Validate the old integer-watermark protocol before consuming it.

    The legacy API has no ``complete`` bit or pinned snapshot identity.  The
    only safe compatibility case is a one-shot page whose explicit sequences
    end exactly at the reported watermark.  A source head larger than the
    returned page is otherwise indistinguishable from a silently truncated
    read, so it must fail closed.
    """

    if not isinstance(batch, TraceProjectionRecordBatch):
        return False
    # ``complete`` is authoritative even on the compatibility protocol.  A
    # source must not be able to smuggle a bounded page into a full-state
    # allocator snapshot merely by reporting a watermark equal to its last
    # returned row.
    if batch.complete is not True:
        return False
    # A native source can explicitly attest a pinned full snapshot.  Its
    # sequence values need not be globally unique (siblings may share an
    # event sequence), and legacy cursor rules must not reject that valid
    # topology.  Contradictory stable IDs are still rejected by the caller.
    if str(getattr(batch, "snapshot_id", "") or "").strip():
        return True
    sequences: list[int] = []
    identities: set[tuple[str, ...]] = set()
    for item in batch.records:
        if isinstance(item, TraceProjectionRecord):
            sequence = item.sequence
            identity = item.canonical_identity
        elif isinstance(item, Mapping):
            if not any(key in item for key in ("sequence", "seq", "watermark")):
                return False
            try:
                sequence = int(item.get("sequence", item.get("seq", item.get("watermark", 0))))
            except (TypeError, ValueError, OverflowError):
                return False
            identity = ("mapping", str(item.get("record_id", item.get("event_id", ""))), str(sequence))
        else:
            return False
        if sequence <= 0 or sequence in sequences or identity in identities:
            return False
        sequences.append(sequence)
        identities.add(identity)
    return int(batch.watermark) == max(sequences, default=0)


def _project_complete_records(
    adapter: TraceAllocationProjectionAdapter,
    task_ids: Sequence[str],
    records: Sequence[Any],
    *,
    ordinary_outcome_ids: Iterable[str],
    source_watermark: int | str | None = None,
    snapshot_id: str = "",
    reference_time: float | None = None,
) -> TraceAllocationProjectionBatch:
    """Use the explicit full-state adapter when available.

    The fallback keeps this bridge source-compatible with the first projection
    implementation; once the stricter ``project_full_records`` API is present,
    pinned snapshots never pass through incremental ``after_watermark`` logic.
    """

    full_project = getattr(adapter, "project_full_records", None)
    if callable(full_project):
        kwargs: dict[str, Any] = {
            "ordinary_outcome_ids": ordinary_outcome_ids,
            "source_watermark": source_watermark,
        }
        # Keep compatibility with the original adapter while forwarding the
        # full-state attestation fields when the newer API is present.  The
        # adapter is local and trusted, but signature filtering prevents a
        # mixed-version worktree from turning harmless API drift into a
        # runtime failure.
        try:
            parameters = inspect.signature(full_project).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if "snapshot_id" in parameters or accepts_kwargs:
            kwargs["snapshot_id"] = snapshot_id
        if "reference_time" in parameters or accepts_kwargs:
            kwargs["reference_time"] = reference_time
        return full_project(task_ids, records, **kwargs)
    # The old incremental adapter only accepts integer watermarks.  A pinned
    # snapshot's opaque trace watermark must not be coerced or compared as an
    # integer; the source has already attested completeness, so omit it on the
    # compatibility path.
    legacy_source_watermark = (
        source_watermark if isinstance(source_watermark, int) and not isinstance(source_watermark, bool) else None
    )
    return adapter.project_records(
        task_ids,
        records,
        after_watermark=0,
        source_watermark=legacy_source_watermark,
        ordinary_outcome_ids=ordinary_outcome_ids,
    )


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
        selection_runtime: Any | None = None,
        feedback_values: Mapping[str, Any] | None = None,
        ordinary_outcome_ids: Iterable[str] = (),
        reference_time: float | None = None,
    ) -> AllocationTraceView:
        """Read one bounded projection at a single causal cut.

        ``selection_runtime`` is an explicit integration path for Issue #38.
        Passing the runtime (rather than reaching into it from the runner)
        makes the store/feedback binding auditable: only its
        ``selection_store`` and frozen ``feedback_values`` are consumed by
        :class:`SelectionRuntimeTraceSource`.  A separately supplied
        ``store`` must be the exact same object, and a separately supplied
        feedback mapping must agree with the runtime mapping; otherwise the
        bridge returns a deterministic zero view instead of combining state
        from different selector configurations.
        """
        ordered = _ordered_task_ids(task_ids, maximum=self.limits.max_tasks)
        if reference_time is not None:
            if isinstance(reference_time, bool):
                return self.zero(ordered, reason="invalid_reference_time")
            try:
                reference_time = float(reference_time)
            except (TypeError, ValueError, OverflowError):
                return self.zero(ordered, reason="invalid_reference_time")
            if not math.isfinite(reference_time):
                return self.zero(ordered, reason="invalid_reference_time")
        ordinary_ids = tuple(
            sorted({str(value).strip() for value in ordinary_outcome_ids if str(value).strip()})
        )
        if self.synthetic_features is not None:
            selected = {task_id: self.synthetic_features.get(task_id, {}) for task_id in ordered}
            batch = build_synthetic_trace_projection(selected)
            return AllocationTraceView(
                batch=batch,
                watermark="synthetic:" + _canonical_sha(selected),
                source="synthetic",
                complete=True,
            )
        if selection_runtime is not None:
            try:
                runtime_store = getattr(selection_runtime, "selection_store", None)
                if runtime_store is None:
                    raise TypeError("selection runtime has no selection_store")
                if store is not None and store is not runtime_store:
                    raise ValueError("selection runtime/store binding mismatch")
                runtime_values = getattr(selection_runtime, "feedback_values", None)
                if feedback_values is not None:
                    if _feedback_mapping(feedback_values) != _feedback_mapping(runtime_values):
                        raise ValueError("selection runtime feedback mapping mismatch")
                # Keep the runtime wrapper as the source so the pinned API is
                # used even when the underlying store happens to expose a
                # legacy protocol in the future.
                store = SelectionRuntimeTraceSource(
                    selection_runtime,
                    max_records=self.limits.max_records,
                )
                feedback_values = runtime_values
            except Exception as exc:
                return self.zero(ordered, reason=_bounded_reason(exc))
        if store is None:
            return self.zero(ordered)
        snapshot_protocol = getattr(store, "read_allocation_projection_snapshot", None)
        if callable(snapshot_protocol):
            try:
                records: list[Any] = []
                cursor = ""
                as_of: str | None = None
                snapshot_id: str = ""
                page_reference_time: float | None = reference_time
                source_watermark: int | str | None = None
                seen_cursors: set[str] = set()
                # A page can contain at most max_records records.  One extra
                # empty page is enough to detect a non-terminating source while
                # keeping the bridge's work bounded.
                max_pages = self.limits.max_records + 1
                for _page_index in range(max_pages):
                    page = snapshot_protocol(
                        ordered,
                        as_of_watermark=as_of,
                        cursor=cursor,
                        limit=self.limits.max_records,
                    )
                    if not isinstance(page, TraceProjectionSnapshotPage):
                        raise TypeError("snapshot source returned an invalid page")
                    if as_of is None:
                        as_of = page.trace_watermark
                    elif page.trace_watermark != as_of:
                        raise ValueError("trace watermark changed during pagination")
                    if page.snapshot_id:
                        if snapshot_id and page.snapshot_id != snapshot_id:
                            raise ValueError("snapshot identity changed during pagination")
                        snapshot_id = page.snapshot_id
                    if page.reference_time is not None:
                        if (
                            page_reference_time is not None
                            and page.reference_time != page_reference_time
                        ):
                            raise ValueError("reference time changed during pagination")
                        page_reference_time = page.reference_time
                    if page.source_watermark is not None:
                        if (
                            source_watermark is not None
                            and page.source_watermark != source_watermark
                        ):
                            raise ValueError("source watermark changed during pagination")
                        source_watermark = page.source_watermark
                    records.extend(page.records)
                    if len(records) > self.limits.max_records:
                        raise OverflowError("snapshot projection exceeds its record bound")
                    # A page may be replayed after a transient transport
                    # retry.  Exact replay is deduplicated later, but a
                    # same-ID row with changed topology would make the
                    # materialized state depend on page order; reject it.
                    _validate_snapshot_records(records)
                    if page.complete:
                        if page.next_cursor:
                            raise ValueError("complete snapshot returned a cursor")
                        assert as_of is not None
                        batch = _project_complete_records(
                            self.adapter,
                            ordered,
                            records,
                            ordinary_outcome_ids=ordinary_ids,
                            source_watermark=(
                                source_watermark
                                if source_watermark is not None
                                else (snapshot_id or as_of)
                            ),
                            snapshot_id=snapshot_id or as_of,
                            reference_time=page_reference_time,
                        )
                        if batch.truncated:
                            raise OverflowError("snapshot projection is incomplete")
                        return AllocationTraceView(
                            batch=batch,
                            watermark="snapshot:" + _snapshot_identity(
                                ordered,
                                (
                                    source_watermark
                                    if source_watermark is not None
                                    else (snapshot_id or as_of)
                                ),
                                records,
                                ordinary_ids,
                                snapshot_id=snapshot_id,
                                reference_time=page_reference_time,
                                trace_watermark=as_of,
                            ),
                            source="selection_store_snapshot",
                            complete=True,
                            trace_references=_trace_references(
                                ordered, records, ordinary_ids
                            ),
                        )
                    next_cursor = page.next_cursor
                    if not next_cursor or next_cursor in seen_cursors:
                        raise ValueError("snapshot cursor did not advance")
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                raise OverflowError("snapshot pagination exceeded its page bound")
            except Exception as exc:
                return self.zero(ordered, reason=_bounded_reason(exc))
        protocol = getattr(store, "read_allocation_projection_records", None)
        if callable(protocol):
            try:
                # A complete state is requested from origin.  Truncation is not
                # silently interpreted as zero or as the current trace state.
                raw_batch = store.read_allocation_projection_records(
                    ordered, after_watermark=0, limit=self.limits.max_records
                )
                if not _legacy_batch_is_complete(raw_batch):
                    raise ValueError("legacy projection source lacks a complete pinned snapshot")
                _validate_snapshot_records(raw_batch.records)
                batch = _project_complete_records(
                    self.adapter,
                    ordered,
                    raw_batch.records,
                    ordinary_outcome_ids=ordinary_ids,
                    source_watermark=raw_batch.watermark,
                    snapshot_id=str(getattr(raw_batch, "snapshot_id", "") or ""),
                    reference_time=reference_time,
                )
                if batch.truncated:
                    raise OverflowError("store-native projection is incomplete")
                return AllocationTraceView(
                    batch=batch,
                    watermark="legacy:" + _snapshot_identity(
                        ordered,
                        raw_batch.watermark,
                        raw_batch.records,
                        ordinary_ids,
                        snapshot_id=str(getattr(raw_batch, "snapshot_id", "") or ""),
                        reference_time=reference_time,
                        trace_watermark=str(raw_batch.watermark),
                    ),
                    source="selection_store_protocol",
                    complete=True,
                    trace_references=_trace_references(
                        ordered, raw_batch.records, ordinary_ids
                    ),
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
            batch = _project_complete_records(
                self.adapter,
                ordered,
                records,
                ordinary_outcome_ids=ordinary_ids,
                source_watermark=len(records),
                snapshot_id=watermark,
                reference_time=reference_time,
            )
            if batch.truncated:
                raise OverflowError("selection projection is incomplete")
            return AllocationTraceView(
                batch=batch,
                watermark="sqlite-v1:" + watermark,
                source="selection_store_sqlite_v1",
                complete=True,
                trace_references=_trace_references(ordered, records, ordinary_ids),
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
    "SelectionRuntimeTraceSource",
    "SelectionStoreTraceSource",
    "TraceProjectionSnapshotPage",
    "TraceProjectionSnapshotSource",
    "TraceProjectionBridge",
    "feedback_values_from_config",
    "policy_reads_trace",
]
