"""Minimal event-backed communication and context-piece store.

The store is intentionally boring: SQLite WAL plus JSON payloads.  This keeps
the experiment surface inspectable while allowing the communication policy to
be replaced without changing the agent runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Mapping


_WORD_RE = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]+")
_MAX_TEXT = 8_000
_ACTOR_TEXT_LIMIT = 256
_ROUTE_KEY_LIMIT = 512
_ROUTE_SUMMARY_LIMIT = 1_000
_ROUTE_REASON_LIMIT = 1_000
_DEFAULT_ROUTE_TTL_SECONDS = 900.0
_MAX_ROUTE_TTL_SECONDS = 7 * 24 * 60 * 60.0
_ACTOR_TERMINAL_STATUSES = frozenset(
    {
        "aborted",
        "cancelled",
        "canceled",
        "closed",
        "completed",
        "done",
        "error",
        "failed",
        "finished",
        "recovery_exhausted",
        "solved",
        "solved_by_peer",
        "timed_out",
        "timeout",
        "proved",
    }
)
_ACTOR_LIVE_STATUSES = frozenset(
    {
        "active",
        "admitted",
        "running",
    }
)
_ROUTE_ACTIVE_STATUSES = frozenset({"active", "blocked"})
_ROUTE_TERMINAL_STATUSES = frozenset({"released", "done"})
_ROUTE_STATUS_VALUES = _ROUTE_ACTIVE_STATUSES | _ROUTE_TERMINAL_STATUSES
_SOLVED_ACTOR_STATUSES = frozenset(
    {"completed", "done", "solved", "solved_by_peer", "proved"}
)
_UNSET = object()


def _format_epoch(epoch: float) -> str:
    """Render a UTC timestamp with stable microsecond precision.

    New lifecycle records use a precision-bearing timestamp so very short TTLs
    and concurrent claims remain deterministic.  Existing CPS events continue
    to use :func:`utc_now`, whose second precision is part of the old format.
    """

    value = float(epoch)
    whole = int(value)
    micros = int(round((value - whole) * 1_000_000))
    if micros >= 1_000_000:
        whole += 1
        micros -= 1_000_000
    if micros < 0:
        whole -= 1
        micros += 1_000_000
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + f".{micros:06d}Z"


def _clock(now: Any = None) -> tuple[str, float]:
    """Return ``(canonical UTC text, epoch seconds)`` for an optional clock.

    Tests and recovery code can pass an epoch, datetime, or ISO-8601 string;
    normal runtime calls use the wall clock.  A strict parser makes malformed
    TTL overrides fail closed instead of silently making a claim immortal.
    """

    if now is None:
        epoch = time.time()
        return _format_epoch(epoch), epoch
    if isinstance(now, bool):
        raise ValueError("now must be an epoch, datetime, or ISO-8601 timestamp")
    if isinstance(now, (int, float)):
        epoch = float(now)
        return _format_epoch(epoch), epoch
    if isinstance(now, dt.datetime):
        value = now
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        epoch = value.timestamp()
        return _format_epoch(epoch), epoch
    text = str(now).strip()
    if not text:
        raise ValueError("now must not be empty")
    try:
        value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        try:
            epoch = float(text)
        except ValueError:
            raise ValueError("now must be an epoch, datetime, or ISO-8601 timestamp") from exc
    else:
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        epoch = value.timestamp()
    return _format_epoch(epoch), epoch


def _timestamp_epoch(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return _clock(value)[1]
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _normal_status(value: Any, *, default: str, allowed: set[str] | frozenset[str] | None = None) -> str:
    text = str(value or default).strip().lower()
    if not text:
        text = default
    if allowed is not None and text not in allowed:
        raise ValueError(f"unsupported status: {value}")
    return _clip(text, 64)


def _bounded_json(value: Any, *, limit: int = 4_000) -> tuple[str, Any]:
    """Encode metadata without allowing unbounded row/event payloads."""

    candidate: Any
    if value is None:
        candidate = {}
    elif isinstance(value, Mapping):
        candidate = {
            _clip(key, 128): _clip(item, 512)
            for key, item in list(value.items())[:32]
        }
    else:
        candidate = {"value": _clip(value, 512)}
    encoded = _json(candidate)
    if len(encoded) <= limit:
        return encoded, candidate
    # Keep the stored shape valid JSON even for pathological metadata.
    fallback = {"truncated": True}
    return _json(fallback), fallback


def _identifier(value: Any, name: str, *, limit: int = _ACTOR_TEXT_LIMIT) -> str:
    text = _clip(value, limit)
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clip(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _tokens(text: str) -> set[str]:
    return {item.lower() for item in _WORD_RE.findall(text)}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _metric_int(metrics: Mapping[str, Any], key: str) -> int | None:
    value = metrics.get(key)
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _metric_float(metrics: Mapping[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else 0.0


@dataclass
class _WriteObservation:
    """Timing and queue state for one profiled CPS write transaction.

    This object is created only on the opt-in profiling path.  ``transaction``
    starts immediately before ``BEGIN IMMEDIATE`` so it includes SQLite lock
    admission; ``lock_acquired_at`` lets the final event report the distinct
    lock-hold interval (the mutation body and COMMIT, but not admission wait).
    """

    operation: str
    operation_started: float
    transaction_started: float
    queued_at: float
    wal_bytes_before: int
    db_bytes_before: int
    lock_acquired_at: float | None = None
    lock_wait_seconds: float = 0.0
    queue_residence_seconds: float = 0.0
    active: bool = False
    finalized: bool = False


class CPSStore:
    """Thread/process-safe store; each operation uses a short SQLite txn."""

    def __init__(self, path: Path, profiler: Any | None = None):
        self.path = Path(path)
        self.profiler = profiler
        try:
            self._profiling_enabled = bool(
                profiler is not None and getattr(profiler, "enabled", False)
            )
        except Exception:
            self._profiling_enabled = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # These are descriptive counters for local in-flight/contending writers
        # in this CPSStore instance, not an application queue.  SQLite's
        # ``lock_wait_seconds`` remains the cross-thread/process measurement;
        # queue_residence spans local contender registration through
        # BEGIN IMMEDIATE lock acquisition, without changing the disabled path.
        self._write_state_lock = threading.Lock()
        self._write_waiters = 0
        self._write_active = 0
        self._write_sequence = 0
        self._write_wall_total_seconds = 0.0
        self._write_lock_wait_total_seconds = 0.0
        self._write_lock_hold_total_seconds = 0.0
        self._init_schema()

    def _profile_event(self, event: str, **fields: Any) -> None:
        if not self._profiling_enabled:
            return
        profiler = self.profiler
        try:
            profiler.emit(event, **fields)
        except BaseException:
            return

    @contextmanager
    def _profile_span(self, name: str, **fields: Any):
        """Run a best-effort profiling span without changing CPS semantics.

        The profiler is an observational side channel.  A custom sink can
        fail while creating a context manager, entering it, or leaving it;
        none of those failures may turn a successful CPS operation into an
        error.  Conversely, an exception raised by the wrapped business code
        must always be re-raised, even when a sink's ``__exit__`` returns a
        truthy value (the normal context-manager suppression convention).
        """

        if not self._profiling_enabled:
            yield
            return
        profiler = self.profiler
        span = getattr(profiler, "span", None) if profiler is not None else None
        if not callable(span):
            yield
            return
        try:
            context = span(name, **fields)
        except BaseException:
            # A custom diagnostic sink is outside CPS's failure domain.
            # Continue the business operation even if context construction
            # itself raises (including a non-Exception BaseException).
            context = None
        if context is None:
            yield
            return

        try:
            context.__enter__()
        except BaseException:
            # A diagnostic sink must be fail-open.  Do not call __exit__ after
            # a failed __enter__, matching Python's with semantics; continue
            # with the business operation uninstrumented.
            yield
            return

        business_error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            business_error = exc
            raise
        finally:
            try:
                # Ignore both sink failures and the suppression return value.
                # The latter is important: a profiler must never swallow an
                # exception from CPS business logic.
                context.__exit__(
                    type(business_error) if business_error is not None else None,
                    business_error,
                    business_error.__traceback__ if business_error is not None else None,
                )
            except BaseException:
                pass

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return max(0, int(path.stat().st_size))
        except OSError:
            return 0

    def _profile_checkpoint(self, operation: str) -> None:
        """Record a passive WAL checkpoint when explicitly invoked by a run."""

        if not self._profiling_enabled:
            return
        started = time.monotonic()
        try:
            with self._db(operation=f"checkpoint:{operation}") as db:
                result = db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            busy = int(result[0]) if result is not None else 0
            frames = int(result[1]) if result is not None else 0
            checkpointed = int(result[2]) if result is not None else 0
            self._profile_event(
                "cps.sqlite.checkpoint",
                db_operation=operation,
                checkpoint_seconds=max(0.0, time.monotonic() - started),
                busy_retry_count=max(0, busy),
                rows=frames,
                output_rows=checkpointed,
                wal_bytes=self._file_size(Path(str(self.path) + "-wal")),
            )
        except Exception as exc:
            self._profile_event(
                "cps.sqlite.checkpoint",
                db_operation=operation,
                checkpoint_seconds=max(0.0, time.monotonic() - started),
                status="error",
                error_kind=type(exc).__name__,
            )

    def _connect(self, *, operation: str = "generic") -> sqlite3.Connection:
        # CPS deliberately opens operation-scoped connections.  At scale the
        # PRAGMA/connection setup can become a measurable fraction of the
        # allocator budget, so keep it distinct from SQL execution time.
        started = time.monotonic() if self._profiling_enabled else 0.0
        connection: sqlite3.Connection | None = None
        error_kind: str | None = None
        connected_ok = False
        try:
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            connected_ok = True
            return connection
        except Exception as exc:
            error_kind = type(exc).__name__
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise
        finally:
            if self._profiling_enabled:
                self._profile_event(
                    "cps.sqlite.connect",
                    db_operation=operation,
                    connect_seconds=max(0.0, time.monotonic() - started),
                    status="ok" if connected_ok else "error",
                    error_kind=error_kind,
                    db_bytes=self._file_size(self.path),
                    wal_bytes=self._file_size(Path(str(self.path) + "-wal")),
                )

    @contextmanager
    def _db(self, *, operation: str = "generic"):
        connection = self._connect(operation=operation)
        try:
            yield connection
        finally:
            connection.close()

    def _finalize_profiled_write(
        self,
        observation: _WriteObservation,
        *,
        status: str,
        reason: str | None = None,
        error_kind: str | None = None,
        body_seconds: float = 0.0,
        commit_seconds: float = 0.0,
        rows_written: int = 0,
        metrics: Mapping[str, Any] | None = None,
        finished_at: float | None = None,
        deferred_events: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        """Emit exactly one terminal event and release local writer state.

        This method is profiling-only.  It is intentionally fail-open through
        :meth:`_profile_event`, and it tolerates a connection/BEGIN failure
        where no SQLite transaction was ever acquired.  In that case the
        terminal event is ``status=skipped`` with a bounded ``reason``.
        """

        if observation.finalized:
            return
        # ``finished_at`` is captured immediately after COMMIT/ROLLBACK (or
        # the failed BEGIN), before file-size probes and sink bookkeeping.  A
        # missing value is only possible for an unexpected outer failure.
        now = finished_at if finished_at is not None else time.monotonic()
        lock_hold = (
            max(0.0, now - observation.lock_acquired_at)
            if observation.lock_acquired_at is not None
            else 0.0
        )
        transaction_seconds = max(0.0, now - observation.transaction_started)
        with self._write_state_lock:
            if observation.active:
                self._write_active = max(0, self._write_active - 1)
            else:
                self._write_waiters = max(0, self._write_waiters - 1)
            queue_waiters = self._write_waiters
            queue_active = self._write_active
            queue_depth = queue_waiters + queue_active
            self._write_sequence += 1
            write_sequence = self._write_sequence
            wall_seconds = max(0.0, now - observation.operation_started)
            self._write_wall_total_seconds += wall_seconds
            self._write_lock_wait_total_seconds += observation.lock_wait_seconds
            self._write_lock_hold_total_seconds += lock_hold
            write_wall_total_seconds = self._write_wall_total_seconds
            write_lock_wait_total_seconds = self._write_lock_wait_total_seconds
            write_lock_hold_total_seconds = self._write_lock_hold_total_seconds
        wal_after = self._file_size(Path(str(self.path) + "-wal"))
        db_after = self._file_size(self.path)
        values = metrics if isinstance(metrics, Mapping) else {}
        # Mark before handing control to the sink: a pathological sink that
        # re-enters the store must not produce a duplicate terminal event.
        observation.finalized = True
        # ``total_changes`` includes statements executed before a rollback.
        # Only a durable COMMIT may contribute to the persisted-row
        # denominator; failed/skipped transactions must report zero even when
        # their mutation body reached SQLite before the error.
        reported_rows_written = (
            max(0, int(rows_written or 0)) if status == "ok" else 0
        )
        # COMMIT/ROLLBACK (or a failed BEGIN) has already completed before
        # this finalizer is called, so SQLite no longer owns the writer lock.
        # Flush lock/transaction-local observations only now.  Keeping the
        # sink I/O out of the BEGIN..COMMIT interval prevents profiling from
        # inflating the lock-hold measurement and avoids adding contention at
        # high concurrency.  Marking ``observation.finalized`` above makes the
        # flush idempotent even if a sink re-enters the store.
        if deferred_events:
            pending = tuple(deferred_events)
            deferred_events.clear()
            for event, fields in pending:
                try:
                    self._profile_event(event, **fields)
                except BaseException:
                    # ``_profile_event`` is fail-open itself; keep this guard
                    # so a test/custom override cannot turn a durable write
                    # into a profiling exception or skip its terminal row.
                    pass
        self._profile_event(
            "cps.write.commit",
            db_operation=observation.operation,
            queue_state="finished",
            status=status,
            reason=reason,
            error_kind=error_kind,
            lock_wait_seconds=observation.lock_wait_seconds,
            lock_hold_seconds=lock_hold,
            transaction_seconds=transaction_seconds,
            queue_residence_seconds=observation.queue_residence_seconds,
            lock_queue_depth=queue_depth,
            write_waiters=queue_waiters,
            write_active=queue_active,
            body_seconds=max(0.0, body_seconds),
            commit_seconds=max(0.0, commit_seconds),
            wall_seconds=wall_seconds,
            write_sequence=write_sequence,
            write_ops_total=write_sequence,
            write_wall_total_seconds=write_wall_total_seconds,
            write_lock_wait_total_seconds=write_lock_wait_total_seconds,
            write_lock_hold_total_seconds=write_lock_hold_total_seconds,
            rows_written=reported_rows_written,
            input_bytes=_metric_int(values, "input_bytes"),
            payload_bytes=_metric_int(values, "payload_bytes"),
            request_key_sha256=values.get("request_key_sha256"),
            snapshot_sha256=values.get("snapshot_sha256"),
            pool_sha256=values.get("pool_sha256"),
            serialization_seconds=_metric_float(values, "serialization_seconds"),
            serialization_inside_lock_seconds=_metric_float(
                values, "serialization_inside_lock_seconds"
            ),
            serialization_inside_lock_bytes=_metric_int(
                values, "serialization_inside_lock_bytes"
            ),
            serialization_call_count=_metric_int(values, "serialization_call_count"),
            hash_seconds=_metric_float(values, "hash_seconds"),
            prepare_seconds=_metric_float(values, "prepare_seconds"),
            prepare_rows=_metric_int(values, "prepare_rows"),
            prepare_candidate_rows=_metric_int(values, "prepare_candidate_rows"),
            prepare_ranking_rows=_metric_int(values, "prepare_ranking_rows"),
            prepare_bytes=_metric_int(values, "prepare_bytes"),
            prepare_serialization_seconds=_metric_float(
                values, "prepare_serialization_seconds"
            ),
            prepare_hash_seconds=_metric_float(values, "prepare_hash_seconds"),
            db_bytes=db_after,
            wal_bytes=wal_after,
            db_bytes_before=observation.db_bytes_before,
            db_bytes_delta=db_after - observation.db_bytes_before,
            wal_bytes_before=observation.wal_bytes_before,
            wal_bytes_after=wal_after,
            wal_bytes_delta=wal_after - observation.wal_bytes_before,
        )

    @contextmanager
    def _write_transaction(
        self,
        operation: str,
        *,
        metrics: Mapping[str, Any] | None = None,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ):
        """Run one CPS write with an opt-in, complete timing envelope.

        The non-profiled branch intentionally retains the original sequence of
        ``_db`` → ``_begin_write`` → body → ``_commit_write`` and does not call
        ``time.monotonic`` or inspect file sizes.  The profiled branch adds
        only side-channel bookkeeping around that same SQL transaction.
        """

        if not self._profiling_enabled:
            with self._db() as db:
                self._begin_write(
                    db,
                    deadline_epoch_ms=deadline_epoch_ms,
                    cancel_guard=cancel_guard,
                )
                try:
                    yield db
                    self._commit_write(
                        db,
                        deadline_epoch_ms=deadline_epoch_ms,
                        cancel_guard=cancel_guard,
                    )
                except Exception:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
            return

        operation_started = time.monotonic()
        observation = _WriteObservation(
            operation=operation,
            operation_started=operation_started,
            # Filled immediately before BEGIN IMMEDIATE, after connection
            # setup and local contender registration.  This makes
            # transaction_seconds include SQLite lock wait but keeps
            # connection/queue residence separately attributable.
            transaction_started=operation_started,
            queued_at=operation_started,
            wal_bytes_before=self._file_size(Path(str(self.path) + "-wal")),
            db_bytes_before=self._file_size(self.path),
        )
        with self._write_state_lock:
            self._write_waiters += 1
            queue_waiters = self._write_waiters
            queue_active = self._write_active
            queue_depth = queue_waiters + queue_active
        self._profile_event(
            "cps.write.queue",
            db_operation=operation,
            queue_state="waiting",
            lock_queue_depth=queue_depth,
            write_waiters=queue_waiters,
            write_active=queue_active,
        )
        db: sqlite3.Connection | None = None
        changes_before = 0
        body_seconds = 0.0
        # Keep lock-scoped events in memory until COMMIT/ROLLBACK has released
        # SQLite's writer lock.  The queue is per-attempt and bounded by the
        # handful of lifecycle events emitted by this transaction.
        deferred_profile_events: list[tuple[str, dict[str, Any]]] = []

        def _defer_profile_event(event: str, **fields: Any) -> None:
            if self._profiling_enabled:
                deferred_profile_events.append((event, dict(fields)))

        try:
            try:
                with self._db(operation=f"write:{operation}") as db:
                    changes_before = int(getattr(db, "total_changes", 0) or 0)
                    lock_started = time.monotonic()
                    observation.transaction_started = lock_started
                    try:
                        self._begin_write(
                            db,
                            deadline_epoch_ms=deadline_epoch_ms,
                            cancel_guard=cancel_guard,
                        )
                    except BaseException as exc:
                        # BEGIN may fail before acquiring a transaction (for
                        # example SQLite busy timeout or a revoked capability).
                        # Emit both the lock error and an explicit terminal
                        # commit-skipped record so every write attempt closes.
                        _defer_profile_event(
                            "cps.write.lock",
                            db_operation=operation,
                            lock_wait_seconds=max(0.0, time.monotonic() - lock_started),
                            status="error",
                            error_kind=type(exc).__name__,
                        )
                        observation.lock_wait_seconds = max(
                            0.0, time.monotonic() - lock_started
                        )
                        begin_finished_at = time.monotonic()
                        self._finalize_profiled_write(
                            observation,
                            status="skipped",
                            reason="begin_failed",
                            error_kind=type(exc).__name__,
                            metrics=metrics,
                            finished_at=begin_finished_at,
                            deferred_events=deferred_profile_events,
                        )
                        raise
                    observation.lock_acquired_at = time.monotonic()
                    observation.lock_wait_seconds = max(
                        0.0, observation.lock_acquired_at - lock_started
                    )
                    observation.queue_residence_seconds = max(
                        0.0, observation.lock_acquired_at - observation.queued_at
                    )
                    with self._write_state_lock:
                        self._write_waiters = max(0, self._write_waiters - 1)
                        self._write_active += 1
                        queue_waiters = self._write_waiters
                        queue_active = self._write_active
                        queue_depth = queue_waiters + queue_active
                    observation.active = True
                    _defer_profile_event(
                        "cps.write.lock",
                        db_operation=operation,
                        lock_wait_seconds=observation.lock_wait_seconds,
                        queue_residence_seconds=observation.queue_residence_seconds,
                        lock_queue_depth=queue_depth,
                        write_waiters=queue_waiters,
                        write_active=queue_active,
                        status="acquired",
                    )
                    body_started = time.monotonic()
                    try:
                        yield db
                    except BaseException as exc:
                        body_seconds = max(0.0, time.monotonic() - body_started)
                        if db.in_transaction:
                            try:
                                db.execute("ROLLBACK")
                            except Exception:
                                # Preserve the original mutation exception;
                                # the terminal profile still records failure.
                                pass
                        body_finished_at = time.monotonic()
                        self._finalize_profiled_write(
                            observation,
                            status="error",
                            reason="body_failed",
                            error_kind=type(exc).__name__,
                            body_seconds=body_seconds,
                            rows_written=max(
                                0,
                                int(getattr(db, "total_changes", 0) or 0)
                                - changes_before,
                            ),
                            metrics=metrics,
                            finished_at=body_finished_at,
                            deferred_events=deferred_profile_events,
                        )
                        raise
                    body_seconds = max(0.0, time.monotonic() - body_started)
                    commit_started = time.monotonic()
                    try:
                        self._commit_write(
                            db,
                            deadline_epoch_ms=deadline_epoch_ms,
                            cancel_guard=cancel_guard,
                        )
                    except BaseException as exc:
                        commit_seconds = max(0.0, time.monotonic() - commit_started)
                        if db.in_transaction:
                            try:
                                db.execute("ROLLBACK")
                            except Exception:
                                pass
                        commit_finished_at = time.monotonic()
                        self._finalize_profiled_write(
                            observation,
                            status="error",
                            reason="commit_failed",
                            error_kind=type(exc).__name__,
                            body_seconds=body_seconds,
                            commit_seconds=commit_seconds,
                            rows_written=max(
                                0,
                                int(getattr(db, "total_changes", 0) or 0)
                                - changes_before,
                            ),
                            metrics=metrics,
                            finished_at=commit_finished_at,
                            deferred_events=deferred_profile_events,
                        )
                        raise
                    commit_seconds = max(0.0, time.monotonic() - commit_started)
                    committed_finished_at = time.monotonic()
                    self._finalize_profiled_write(
                        observation,
                        status="ok",
                        body_seconds=body_seconds,
                        commit_seconds=commit_seconds,
                        rows_written=max(
                            0,
                            int(getattr(db, "total_changes", 0) or 0) - changes_before,
                        ),
                        metrics=metrics,
                        finished_at=committed_finished_at,
                        deferred_events=deferred_profile_events,
                    )
            except BaseException as exc:
                if not observation.finalized:
                    # Failure while opening the connection (or an unexpected
                    # adapter failure before BEGIN) has no lock/commit event;
                    # close the attempt explicitly as skipped/error.
                    self._finalize_profiled_write(
                        observation,
                        status="skipped" if db is None else "error",
                        reason="connection_failed" if db is None else "transaction_failed",
                        error_kind=type(exc).__name__,
                        metrics=metrics,
                        deferred_events=deferred_profile_events,
                    )
                raise
        finally:
            if not observation.finalized:
                self._finalize_profiled_write(
                    observation,
                    status="error",
                    reason="transaction_abandoned",
                    metrics=metrics,
                    deferred_events=deferred_profile_events,
                )

    def _init_schema(self) -> None:
        with self._db(operation="init_schema" if self._profiling_enabled else "generic") as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS pieces (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS pieces_task_created
                    ON pieces(task_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS messages_inbox
                    ON messages(task_id, recipient, created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    task_id TEXT,
                    actor_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                -- ``actors`` is runner-owned lifecycle state.  The existing
                -- actors.json projection is intentionally not used as a
                -- source of truth by this store.
                CREATE TABLE IF NOT EXISTS actors (
                    task_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    episode INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    admitted_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    finished_at TEXT,
                    finish_reason TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(task_id, actor_id)
                );
                CREATE INDEX IF NOT EXISTS actors_active
                    ON actors(task_id, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS route_claims (
                    claim_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    episode INTEGER NOT NULL,
                    route_key TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT,
                    independent_verification_reason TEXT,
                    is_primary INTEGER NOT NULL DEFAULT 1,
                    release_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS route_claims_task_status
                    ON route_claims(task_id, status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS route_claims_actor_status
                    ON route_claims(task_id, actor_id, status, updated_at DESC);
                -- A secondary independent-verification claim may coexist with
                -- the primary owner, but two primary owners may not.
                CREATE UNIQUE INDEX IF NOT EXISTS route_claims_one_primary
                    ON route_claims(task_id, route_key)
                    WHERE is_primary=1 AND LOWER(TRIM(status)) IN ('active', 'blocked');
                """
            )

    @classmethod
    def _begin_write(
        cls,
        db: sqlite3.Connection,
        *,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> None:
        """Acquire the write lock, then revalidate the write capability."""

        db.execute("BEGIN IMMEDIATE")
        try:
            cls._validate_write_capability(
                db,
                deadline_epoch_ms=deadline_epoch_ms,
                cancel_guard=cancel_guard,
            )
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    @staticmethod
    def _validate_write_capability(
        db: sqlite3.Connection,
        *,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> None:
        """Validate cancellation and horizon inside the active write txn."""

        if cancel_guard is not None and cancel_guard():
            raise RuntimeError("CPS communication capability has been revoked")
        if deadline_epoch_ms is None:
            return
        now_epoch_ms = int(
            db.execute(
                "SELECT CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"
            ).fetchone()[0]
        )
        if now_epoch_ms >= int(deadline_epoch_ms):
            raise RuntimeError("CPS communication horizon has elapsed")

    @classmethod
    def _commit_write(
        cls,
        db: sqlite3.Connection,
        *,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> None:
        """Revalidate immediately before making a write transaction durable."""

        cls._validate_write_capability(
            db,
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
        )
        db.execute("COMMIT")

    @staticmethod
    def _insert_event(
        db: sqlite3.Connection,
        event_type: str,
        *,
        task_id: str | None,
        actor_id: str | None,
        payload: Mapping[str, Any] | None,
    ) -> str:
        event_id = uuid.uuid4().hex
        event_payload: Mapping[str, Any] | None = payload
        if event_type.startswith(("actor_", "route_claim")):
            # Lifecycle events are solver-visible audit metadata. Keep them
            # bounded even when a caller supplied maximum-size route fields.
            _encoded, decoded = _bounded_json(payload)
            del _encoded
            event_payload = decoded
        db.execute(
            "INSERT INTO events(event_id,event_type,task_id,actor_id,payload,created_at) VALUES(?,?,?,?,?,?)",
            (event_id, event_type, task_id, actor_id, _json(dict(event_payload or {})), utc_now()),
        )
        return event_id

    def record_event(
        self,
        event_type: str,
        *,
        task_id: str | None = None,
        actor_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> str:
        with self._write_transaction(
            "record_event",
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
            metrics=(
                {"input_bytes": len(_json(dict(payload or {})).encode("utf-8"))}
                if self._profiling_enabled
                else None
            ),
        ) as db:
            event_id = self._insert_event(
                db,
                event_type,
                task_id=task_id,
                actor_id=actor_id,
                payload=payload,
            )
        return event_id

    def create_piece(
        self,
        *,
        task_id: str,
        author: str,
        kind: str,
        title: str,
        body: str,
        tags: Iterable[str] = (),
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        piece_id = uuid.uuid4().hex
        row = {
            "id": piece_id,
            "task_id": _clip(task_id, 256),
            "author": _clip(author, 256),
            "kind": _clip(kind, 64) or "note",
            "title": _clip(title, 300) or "untitled",
            "body": _clip(body),
            "tags": sorted({_clip(tag, 64) for tag in tags if _clip(tag, 64)}),
            "created_at": utc_now(),
        }
        row_payload = _json(row) if self._profiling_enabled else ""
        with self._write_transaction(
            "create_piece",
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
            metrics=(
                {"input_bytes": len(row_payload.encode("utf-8"))}
                if self._profiling_enabled
                else None
            ),
        ) as db:
            db.execute(
                "INSERT INTO pieces(id,task_id,author,kind,title,body,tags,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["task_id"],
                    row["author"],
                    row["kind"],
                    row["title"],
                    row["body"],
                    _json(row["tags"]),
                    row["created_at"],
                ),
            )
            self._insert_event(
                db,
                "piece_created",
                task_id=task_id,
                actor_id=author,
                payload=row,
            )
        return row

    def search(
        self,
        *,
        task_id: str,
        query: str = "",
        limit: int = 8,
        include_global: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        profiling = self._profiling_enabled
        started = time.monotonic() if profiling else 0.0
        query_seconds = 0.0
        fetch_seconds = 0.0
        read_scope_seconds = 0.0
        with self._db(operation="search" if profiling else "generic") as db:
            query_started = time.monotonic() if profiling else 0.0
            if include_global:
                cursor = db.execute(
                    """SELECT * FROM pieces
                       WHERE active=1 AND (task_id=? OR task_id='__global__')
                       ORDER BY created_at DESC LIMIT ?""",
                    (task_id, max(limit * 8, 32)),
                )
            else:
                cursor = db.execute(
                    "SELECT * FROM pieces WHERE active=1 AND task_id=? ORDER BY created_at DESC LIMIT ?",
                    (task_id, max(limit * 8, 32)),
                )
            if profiling:
                query_seconds = max(0.0, time.monotonic() - query_started)
                fetch_started = time.monotonic()
                rows = cursor.fetchall()
                fetch_seconds = max(0.0, time.monotonic() - fetch_started)
                read_scope_seconds = max(0.0, time.monotonic() - query_started)
            else:
                rows = cursor.fetchall()
        if profiling:
            self._profile_event(
                "cps.search.query",
                db_operation="pieces_search",
                task_count=1,
                rows_scanned=len(rows),
                input_rows=len(rows),
                input_bytes=sum(
                    len(str(row["title"] or "").encode("utf-8"))
                    + len(str(row["body"] or "").encode("utf-8"))
                    for row in rows
                ),
                query_seconds=query_seconds,
                fetch_seconds=fetch_seconds,
                read_scope_seconds=read_scope_seconds,
                read_transaction_seconds=read_scope_seconds,
                read_mode="autocommit_select",
            )
        materialize_started = time.monotonic() if profiling else 0.0
        wanted = _tokens(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = json.loads(item.get("tags") or "[]")
            except json.JSONDecodeError:
                item["tags"] = []
            haystack = " ".join(
                [str(item.get("title", "")), str(item.get("body", "")), " ".join(item["tags"])]
            )
            overlap = len(wanted & _tokens(haystack)) if wanted else 0
            # Newer pieces win ties; an explicit query match dominates recency.
            try:
                recency = int(str(item.get("id", ""))[-6:] or "0", 16) / 16_777_215
            except ValueError:
                recency = 0.0
            score = overlap * 10.0 + recency
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        result = [item for _, item in scored[:limit]]
        if profiling:
            self._profile_event(
                "cps.search.materialize",
                operation="pieces_search",
                input_rows=len(rows),
                output_rows=len(result),
                materialized_rows=len(result),
                materialized_bytes=sum(
                    len(str(item.get("title", "")).encode("utf-8"))
                    + len(str(item.get("body", "")).encode("utf-8"))
                    for item in result
                ),
                tokenize_count=len(rows) + 1,
                materialize_seconds=max(0.0, time.monotonic() - materialize_started),
                read_scope_seconds=read_scope_seconds,
                wall_seconds=max(0.0, time.monotonic() - started),
            )
        return result

    def send_message(
        self,
        *,
        task_id: str,
        sender: str,
        recipient: str | None,
        body: str,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        message = {
            "id": uuid.uuid4().hex,
            "task_id": _clip(task_id, 256),
            "sender": _clip(sender, 256),
            "recipient": _clip(recipient, 256) if recipient else None,
            "body": _clip(body),
            "created_at": utc_now(),
            "acked_at": None,
        }
        message_payload = _json(message) if self._profiling_enabled else ""
        with self._write_transaction(
            "send_message",
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
            metrics=(
                {"input_bytes": len(message_payload.encode("utf-8"))}
                if self._profiling_enabled
                else None
            ),
        ) as db:
            db.execute(
                "INSERT INTO messages(id,task_id,sender,recipient,body,created_at) VALUES(?,?,?,?,?,?)",
                (
                    message["id"],
                    message["task_id"],
                    message["sender"],
                    message["recipient"],
                    message["body"],
                    message["created_at"],
                ),
            )
            self._insert_event(
                db,
                "message_sent",
                task_id=task_id,
                actor_id=sender,
                payload=message,
            )
        return message

    def inbox(self, *, task_id: str, recipient: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        profiling = self._profiling_enabled
        started = time.monotonic() if profiling else 0.0
        query_seconds = 0.0
        fetch_seconds = 0.0
        read_scope_seconds = 0.0
        with self._db(operation="inbox" if profiling else "generic") as db:
            query_started = time.monotonic() if profiling else 0.0
            cursor = db.execute(
                """SELECT * FROM messages
                   WHERE task_id IN (?, '__global__') AND acked_at IS NULL
                     AND (recipient IS NULL OR recipient=? OR recipient='*')
                   ORDER BY created_at DESC LIMIT ?""",
                (task_id, recipient, limit),
            )
            if profiling:
                query_seconds = max(0.0, time.monotonic() - query_started)
                fetch_started = time.monotonic()
                rows = cursor.fetchall()
                fetch_seconds = max(0.0, time.monotonic() - fetch_started)
                read_scope_seconds = max(0.0, time.monotonic() - query_started)
            else:
                rows = cursor.fetchall()
        if profiling:
            self._profile_event(
                "cps.inbox.query",
                db_operation="messages_inbox",
                task_count=1,
                rows_scanned=len(rows),
                input_rows=len(rows),
                query_seconds=query_seconds,
                fetch_seconds=fetch_seconds,
                read_scope_seconds=read_scope_seconds,
                read_transaction_seconds=read_scope_seconds,
                read_mode="autocommit_select",
            )
        materialize_started = time.monotonic() if profiling else 0.0
        result = [dict(row) for row in rows]
        if profiling:
            self._profile_event(
                "cps.inbox.materialize",
                operation="messages_inbox",
                input_rows=len(rows),
                output_rows=len(result),
                materialized_rows=len(result),
                materialized_bytes=sum(
                    len(str(item.get("body", "")).encode("utf-8")) for item in result
                ),
                materialize_seconds=max(0.0, time.monotonic() - materialize_started),
                read_scope_seconds=read_scope_seconds,
                wall_seconds=max(0.0, time.monotonic() - started),
            )
        return result

    def ack_message(
        self,
        message_id: str,
        actor_id: str,
        *,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> bool:
        now = utc_now()
        with self._write_transaction(
            "ack_message",
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
            metrics=(
                {"input_bytes": len(str(message_id).encode("utf-8"))}
                if self._profiling_enabled
                else None
            ),
        ) as db:
            cursor = db.execute(
                "UPDATE messages SET acked_at=? WHERE id=? AND acked_at IS NULL",
                (now, message_id),
            )
            if cursor.rowcount:
                self._insert_event(
                    db,
                    "message_acked",
                    task_id=None,
                    actor_id=actor_id,
                    payload={"id": message_id},
                )
            rowcount = int(cursor.rowcount or 0)
        return bool(rowcount)

    def digest(
        self,
        *,
        task_id: str,
        actor_id: str,
        query: str = "",
        limit: int = 8,
        include_global: bool = False,
    ) -> dict[str, Any]:
        profiling = self._profiling_enabled
        if not profiling:
            pieces = self.search(task_id=task_id, query=query, limit=limit, include_global=include_global)
            messages = self.inbox(task_id=task_id, recipient=actor_id, limit=limit)
            return {"pieces": pieces, "messages": messages}
        started = time.monotonic()
        with self._profile_span(
            "cps.digest",
            operation="worker_context_digest",
            task_id=task_id,
            actor_id=actor_id,
        ):
            pieces = self.search(
                task_id=task_id,
                query=query,
                limit=limit,
                include_global=include_global,
            )
            messages = self.inbox(task_id=task_id, recipient=actor_id, limit=limit)
        self._profile_event(
            "cps.digest.summary",
            operation="worker_context_digest",
            task_id=task_id,
            actor_id=actor_id,
            output_rows=len(pieces) + len(messages),
            materialized_rows=len(pieces) + len(messages),
            wall_seconds=max(0.0, time.monotonic() - started),
        )
        return {"pieces": pieces, "messages": messages}

    def _progress_snapshot_impl(
        self,
        task_ids: Iterable[str],
        *,
        recent_limit: int = 3,
        body_chars: int = 1_200,
    ) -> dict[str, dict[str, Any]]:
        """Return bounded per-task CPS statistics in one read transaction.

        Allocation policies use this projection instead of receiving a database
        handle.  The scheduler therefore cannot publish pieces or accidentally
        feed its own decisions back into the communication substrate.
        """
        ordered_ids = tuple(dict.fromkeys(str(task_id) for task_id in task_ids))
        recent_limit = max(1, min(int(recent_limit), 20))
        body_chars = max(1, min(int(body_chars), _MAX_TEXT))
        result: dict[str, dict[str, Any]] = {
            task_id: {
                "piece_count": 0,
                "validation_piece_count": 0,
                "strategy_piece_count": 0,
                "duplicate_piece_count": 0,
                "latest_created_at": "",
                "recent_pieces": [],
            }
            for task_id in ordered_ids
        }
        if not ordered_ids:
            return result
        placeholders = ",".join("?" for _ in ordered_ids)
        if not self._profiling_enabled:
            # Preserve the baseline exactly: no explicit transaction, timing,
            # row/byte accounting, or extra materialization is introduced
            # when profiling is disabled.
            with self._db() as db:
                rows = db.execute(
                    f"""SELECT rowid,id,task_id,author,kind,title,body,created_at
                        FROM pieces
                        WHERE active=1 AND task_id IN ({placeholders})
                        ORDER BY rowid DESC""",
                    ordered_ids,
                ).fetchall()
            query_seconds = fetch_seconds = 0.0
            read_transaction_seconds = 0.0
            read_lock_wait_seconds = 0.0
        else:
            # The production progress path is an autocommit SELECT.  Keep the
            # profiled path on that same code path: introducing an explicit
            # BEGIN only for profiling would hold a snapshot longer and make
            # the lock/WAL comparison self-referential.  SQLite's implicit
            # read scope is measured from execute through fetchall instead.
            query_seconds = fetch_seconds = 0.0
            read_transaction_seconds = 0.0
            read_lock_wait_seconds = 0.0
            with self._db(operation="progress_snapshot") as db:
                query_started = time.monotonic()
                cursor = db.execute(
                    f"""SELECT rowid,id,task_id,author,kind,title,body,created_at
                        FROM pieces
                        WHERE active=1 AND task_id IN ({placeholders})
                        ORDER BY rowid DESC""",
                    ordered_ids,
                )
                query_seconds = max(0.0, time.monotonic() - query_started)
                fetch_started = time.monotonic()
                rows = cursor.fetchall()
                fetch_seconds = max(0.0, time.monotonic() - fetch_started)
                # With isolation_level=None SQLite releases the implicit read
                # transaction when the cursor is exhausted/closed.  This is
                # therefore the closest observable read-scope duration; any
                # lock acquisition is included in query_seconds rather than
                # fabricated as a separately measurable wait.
                read_transaction_seconds = max(
                    0.0, time.monotonic() - query_started
                )
        if self._profiling_enabled:
            input_bytes = sum(
                len(str(row["title"] or "").encode("utf-8"))
                + len(str(row["body"] or "").encode("utf-8"))
                for row in rows
            )
            self._profile_event(
                "cps.progress.query",
                operation="progress_snapshot",
                scan_mode="full_active_piece_scan",
                read_mode="autocommit_select",
                task_count=len(ordered_ids),
                rows_scanned=len(rows),
                input_rows=len(rows),
                input_bytes=input_bytes,
                query_seconds=query_seconds,
                fetch_seconds=fetch_seconds,
                read_transaction_seconds=read_transaction_seconds,
                read_scope_seconds=max(0.0, query_seconds + fetch_seconds),
                read_lock_wait_seconds=read_lock_wait_seconds,
                db_bytes=self._file_size(self.path),
                wal_bytes=self._file_size(Path(str(self.path) + "-wal")),
            )
        titles: dict[str, dict[str, int]] = {task_id: {} for task_id in ordered_ids}
        materialize_started = time.monotonic() if self._profiling_enabled else 0.0
        for raw in rows:
            item = dict(raw)
            task_id = str(item["task_id"])
            stats = result[task_id]
            stats["piece_count"] += 1
            kind = str(item.get("kind") or "")
            if kind == "validation_result" and _is_authoritative_validation_piece(item):
                stats["validation_piece_count"] += 1
            elif kind in {"proof_strategy", "strategy", "handoff", "lemma", "blocker"}:
                stats["strategy_piece_count"] += 1
            normalized_title = " ".join(str(item.get("title") or "").lower().split())
            if normalized_title and kind != "validation_result":
                titles[task_id][normalized_title] = titles[task_id].get(normalized_title, 0) + 1
            if not stats["latest_created_at"]:
                stats["latest_created_at"] = str(item.get("created_at") or "")
            if len(stats["recent_pieces"]) < recent_limit:
                body = str(item.get("body") or "")
                stats["recent_pieces"].append(
                    {
                        "piece_id": str(item.get("id") or ""),
                        "kind": kind,
                        "title": str(item.get("title") or "")[:300],
                        "body": body if len(body) <= body_chars else body[:body_chars] + "…",
                        "author": str(item.get("author") or "")[:256],
                        "created_at": str(item.get("created_at") or ""),
                    }
                )
        for task_id, counts in titles.items():
            result[task_id]["duplicate_piece_count"] = sum(
                max(0, count - 1) for count in counts.values()
            )
        if self._profiling_enabled:
            materialize_seconds = max(0.0, time.monotonic() - materialize_started)
            recent_rows = sum(
                len(item.get("recent_pieces", [])) for item in result.values()
            )
            materialized_bytes = sum(
                len(str(piece.get("title", "")).encode("utf-8"))
                + len(str(piece.get("body", "")).encode("utf-8"))
                for stats in result.values()
                for piece in stats.get("recent_pieces", [])
            )
            self._profile_event(
                "cps.progress.materialize",
                operation="progress_snapshot",
                scan_mode="full_active_piece_scan",
                input_rows=len(rows),
                output_rows=recent_rows,
                materialized_rows=recent_rows,
                materialized_bytes=materialized_bytes,
                materialize_seconds=materialize_seconds,
            )
        return result

    def progress_snapshot(
        self,
        task_ids: Iterable[str],
        *,
        recent_limit: int = 3,
        body_chars: int = 1_200,
    ) -> dict[str, dict[str, Any]]:
        """Profile the bounded CPS progress projection."""

        if not self._profiling_enabled:
            return self._progress_snapshot_impl(
                task_ids,
                recent_limit=recent_limit,
                body_chars=body_chars,
            )
        with self._profile_span("cps.progress", operation="progress_snapshot"):
            result = self._progress_snapshot_impl(
                task_ids,
                recent_limit=recent_limit,
                body_chars=body_chars,
            )
        self._profile_event(
            "cps.progress.summary",
            operation="progress_snapshot",
            task_count=len(result),
            rows_scanned=sum(int(item.get("piece_count", 0) or 0) for item in result.values()),
            output_rows=sum(
                len(item.get("recent_pieces", [])) for item in result.values()
            ),
        )
        return result

    # ------------------------------------------------------------------
    # Runner-owned actor roster and route claims
    # ------------------------------------------------------------------

    @staticmethod
    def _actor_row(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        try:
            item["metadata"] = json.loads(str(item.get("metadata") or "{}"))
        except json.JSONDecodeError:
            item["metadata"] = {}
        if not isinstance(item["metadata"], Mapping):
            item["metadata"] = {}
        item["active"] = str(item.get("status") or "").strip().lower() in _ACTOR_LIVE_STATUSES
        # Keep both names while callers migrate from the old roster projection.
        item["last_heartbeat_at"] = item.get("heartbeat_at")
        item["admission_at"] = item.get("admitted_at")
        return item

    @staticmethod
    def _claim_row(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        try:
            item["is_primary"] = bool(int(item.get("is_primary", 0)))
        except (TypeError, ValueError):
            item["is_primary"] = False
        item["primary"] = item["is_primary"]
        item["active"] = str(item.get("status") or "").strip().lower() in _ROUTE_ACTIVE_STATUSES
        item["independent_verification"] = bool(
            str(item.get("independent_verification_reason") or "").strip()
        )
        return item

    @staticmethod
    def _result_with_claim(
        claim: Mapping[str, Any] | None,
        *,
        ok: bool,
        acquired: bool,
        conflict: Mapping[str, Any] | None = None,
        error: str | None = None,
        idempotent: bool = False,
        actor_registered: bool | None = None,
    ) -> dict[str, Any]:
        """Return a stable, backwards-tolerant route operation envelope."""

        result: dict[str, Any] = {
            "ok": bool(ok),
            "acquired": bool(acquired),
            "claimed": bool(acquired),
            "idempotent": bool(idempotent),
            "primary": bool(claim and claim.get("is_primary")),
            "claim": dict(claim) if claim is not None else None,
            "conflict": dict(conflict) if conflict is not None else None,
            "existing": dict(conflict) if conflict is not None else None,
        }
        if claim is not None:
            # Expose row fields at the top level as well as under ``claim``;
            # this keeps broker adapters simple and supports early callers
            # that treated claim_route's result as the row itself.
            result.update(dict(claim))
            if not bool(claim.get("is_primary")):
                # Preserve the fact that this is an independent secondary on
                # idempotent retries, even when the original primary owner is
                # no longer returned in a conflict field.
                result["status"] = "independent_verification"
        if error:
            result["error"] = error
        if actor_registered is not None:
            result["actor_registered"] = bool(actor_registered)
        if conflict is not None:
            result["conflict_owner"] = conflict.get("actor_id")
            result["conflicting_claim_id"] = conflict.get("claim_id")
        return result

    @staticmethod
    def _validate_episode(episode: Any) -> int:
        if isinstance(episode, bool):
            raise ValueError("episode must be an integer")
        if isinstance(episode, int):
            value = episode
        elif isinstance(episode, str) and re.fullmatch(r"[0-9]+", episode.strip()):
            value = int(episode.strip())
        else:
            # Do not coerce 1.5 (or a float that happens to be integral) into
            # an episode identity.  Episode is part of the actor/claim
            # binding and must be losslessly represented as an integer.
            raise ValueError("episode must be an integer")
        if value < 0:
            raise ValueError("episode must be non-negative")
        return value

    @staticmethod
    def _ttl_seconds(value: Any) -> float:
        if value is None:
            return _DEFAULT_ROUTE_TTL_SECONDS
        if isinstance(value, bool):
            raise ValueError("ttl_seconds must be numeric")
        try:
            ttl = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("ttl_seconds must be numeric") from exc
        if ttl != ttl or ttl in (float("inf"), float("-inf")):
            raise ValueError("ttl_seconds must be finite")
        return min(max(0.0, ttl), _MAX_ROUTE_TTL_SECONDS)

    @classmethod
    def _expire_route_claims(
        cls,
        db: sqlite3.Connection,
        *,
        now_text: str,
        now_epoch: float,
        task_id: str | None = None,
        route_key: str | None = None,
    ) -> list[str]:
        """Release stale claims inside the caller's write transaction.

        Expiry is evaluated in Python rather than SQLite's date parser so both
        legacy second-precision values and new microsecond timestamps work.
        Malformed expiry values fail closed and are released as stale.
        """

        predicates = ["LOWER(TRIM(status)) IN ('active', 'blocked')"]
        parameters: list[Any] = []
        if task_id is not None:
            predicates.append("task_id=?")
            parameters.append(task_id)
        if route_key is not None:
            predicates.append("route_key=?")
            parameters.append(route_key)
        rows = db.execute(
            "SELECT * FROM route_claims WHERE " + " AND ".join(predicates),
            parameters,
        ).fetchall()
        expired: list[str] = []
        for raw in rows:
            item = dict(raw)
            expiry = _timestamp_epoch(item.get("expires_at"))
            if expiry is not None and expiry > now_epoch:
                continue
            claim_id = str(item.get("claim_id") or "")
            if not claim_id:
                continue
            cursor = db.execute(
                """UPDATE route_claims
                   SET status='released', updated_at=?, released_at=?,
                       release_reason='ttl_expired'
                   WHERE claim_id=? AND LOWER(TRIM(status)) IN ('active', 'blocked')""",
                (now_text, now_text, claim_id),
            )
            if cursor.rowcount:
                expired.append(claim_id)
                cls._insert_event(
                    db,
                    "route_claim_expired",
                    task_id=str(item.get("task_id") or ""),
                    actor_id=str(item.get("actor_id") or "") or None,
                    payload={
                        "claim_id": claim_id,
                        "task_id": str(item.get("task_id") or "")[:_ACTOR_TEXT_LIMIT],
                        "actor_id": str(item.get("actor_id") or "")[:_ACTOR_TEXT_LIMIT],
                        "route_key": str(item.get("route_key") or "")[:_ROUTE_KEY_LIMIT],
                        "status": "released",
                        "reason": "ttl_expired",
                    },
                )
        return expired

    def register_actor(
        self,
        task_id: str,
        actor_id: str,
        episode: int = 0,
        *,
        status: str = "admitted",
        metadata: Mapping[str, Any] | None = None,
        now: Any = None,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Register an actor at real runner admission.

        Registration is an upsert on ``(task_id, actor_id)``.  A retry of the
        same logical assignment therefore reopens that actor deterministically
        while preserving the original row identity and event history.
        """

        task = _identifier(task_id, "task_id")
        actor = _identifier(actor_id, "actor_id")
        episode_value = self._validate_episode(episode)
        status_value = _normal_status(status, default="admitted")
        if status_value not in (
            _ACTOR_LIVE_STATUSES | _ACTOR_TERMINAL_STATUSES | {"closing"}
        ):
            raise ValueError(f"unsupported actor status: {status}")
        now_text, _ = _clock(now)
        metadata_json, metadata_value = _bounded_json(metadata)
        with self._write_transaction(
            "actor.register",
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
        ) as db:
                existing_row = db.execute(
                    "SELECT * FROM actors WHERE task_id=? AND actor_id=?",
                    (task, actor),
                ).fetchone()
                existing = dict(existing_row) if existing_row is not None else None
                created_at = str(existing.get("created_at") or now_text) if existing else now_text
                re_registered_episode = (
                    existing is not None
                    and self._validate_episode(existing.get("episode")) != episode_value
                )
                existing_status = (
                    str(existing.get("status") or "").strip().lower()
                    if existing is not None
                    else ""
                )
                if (
                    existing is not None
                    and not re_registered_episode
                    and existing_status in (_ACTOR_TERMINAL_STATUSES | {"closing"})
                ):
                    # A terminal actor/episode is immutable.  Reusing the
                    # same identity for a fresh admission would resurrect its
                    # old capability and make stale route owners ambiguous;
                    # callers must allocate a new episode (or actor id).
                    self._insert_event(
                        db,
                        "actor_registration_rejected",
                        task_id=task,
                        actor_id=actor,
                        payload={
                            "task_id": task,
                            "actor_id": actor,
                            "episode": episode_value,
                            "reason": "actor_finished",
                            "status": existing_status,
                        },
                    )
                    rejected = self._actor_row(existing_row)
                    assert rejected is not None
                    rejected.update(
                        {
                            "ok": False,
                            "found": True,
                            "task_id": task,
                            "actor_id": actor,
                            "episode": episode_value,
                            "registered_episode": episode_value,
                            "status": "actor_finished",
                            "active": False,
                        }
                    )
                    return rejected
                released_claim_ids: list[str] = []
                if re_registered_episode:
                    # Actor ids are normally assignment-unique, but a reused
                    # id must not carry a prior episode's live route into the
                    # new admission.  Release those claims in the same write
                    # transaction before replacing the actor row.
                    old_claims = db.execute(
                        """SELECT * FROM route_claims
                           WHERE task_id=? AND actor_id=?
                             AND LOWER(TRIM(status)) IN ('active', 'blocked')""",
                        (task, actor),
                    ).fetchall()
                    released_claim_ids = [str(row["claim_id"]) for row in old_claims]
                    db.execute(
                        """UPDATE route_claims
                           SET status='released', updated_at=?, released_at=?,
                               release_reason='actor_re_registered'
                           WHERE task_id=? AND actor_id=?
                             AND LOWER(TRIM(status)) IN ('active', 'blocked')""",
                        (now_text, now_text, task, actor),
                    )
                    for old_claim in old_claims:
                        self._insert_event(
                            db,
                            "route_claim_released",
                            task_id=task,
                            actor_id=actor,
                            payload={
                                "claim_id": str(old_claim["claim_id"]),
                                "task_id": task,
                                "actor_id": actor,
                                "route_key": str(old_claim["route_key"] or "")[:_ROUTE_KEY_LIMIT],
                                "status": "released",
                                "reason": "actor_re_registered",
                            },
                        )
                db.execute(
                    """INSERT INTO actors(
                           task_id,actor_id,episode,status,admitted_at,created_at,
                           updated_at,heartbeat_at,finished_at,finish_reason,metadata)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(task_id,actor_id) DO UPDATE SET
                           episode=excluded.episode,
                           status=excluded.status,
                           admitted_at=excluded.admitted_at,
                           updated_at=excluded.updated_at,
                           heartbeat_at=excluded.heartbeat_at,
                           finished_at=NULL,
                           finish_reason=NULL,
                           metadata=excluded.metadata""",
                    (
                        task,
                        actor,
                        episode_value,
                        status_value,
                        now_text,
                        created_at,
                        now_text,
                        now_text,
                        None,
                        None,
                        metadata_json,
                    ),
                )
                row = db.execute(
                    "SELECT * FROM actors WHERE task_id=? AND actor_id=?",
                    (task, actor),
                ).fetchone()
                item = self._actor_row(row)
                assert item is not None
                self._insert_event(
                    db,
                    "actor_registered",
                    task_id=task,
                    actor_id=actor,
                    payload={
                        "task_id": task,
                        "actor_id": actor,
                        "episode": episode_value,
                        "status": status_value,
                        "admitted_at": now_text,
                        "re_registered": existing is not None,
                        "previous_episode": (
                            self._validate_episode(existing.get("episode"))
                            if existing is not None
                            else None
                        ),
                        "re_registration_released_claim_count": len(released_claim_ids),
                        "metadata": metadata_value,
                    },
                )
        return item

    def finish_actor(
        self,
        task_id: str,
        actor_id: str,
        status: str = "finished",
        *,
        episode: int | None = None,
        reason: str | None = None,
        now: Any = None,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Finish an actor and close all of its active route claims atomically."""

        task = _identifier(task_id, "task_id")
        actor = _identifier(actor_id, "actor_id")
        episode_value = (
            None if episode is None else self._validate_episode(episode)
        )
        status_value = _normal_status(status, default="finished")
        # ``finish_actor`` is a terminal operation even if an adapter passes a
        # descriptive non-terminal status by mistake.
        if status_value not in _ACTOR_TERMINAL_STATUSES:
            status_value = "finished"
        reason_value = _clip(reason, _ROUTE_REASON_LIMIT) or None
        now_text, _ = _clock(now)
        with self._write_transaction(
            "actor.finish",
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
        ) as db:
                row = db.execute(
                    "SELECT * FROM actors WHERE task_id=? AND actor_id=?",
                    (task, actor),
                ).fetchone()
                if row is None:
                    return {
                        "ok": False,
                        "found": False,
                        "task_id": task,
                        "actor_id": actor,
                        "episode": episode_value,
                        "status": "not_found",
                        "released_claim_ids": [],
                        "claims_released": 0,
                    }
                registered_episode = self._validate_episode(row["episode"])
                if episode_value is not None and registered_episode != episode_value:
                    self._insert_event(
                        db,
                        "actor_finish_rejected",
                        task_id=task,
                        actor_id=actor,
                        payload={
                            "task_id": task,
                            "actor_id": actor,
                            "reason": "episode_mismatch",
                            "registered_episode": registered_episode,
                            "requested_episode": episode_value,
                        },
                    )
                    return {
                        "ok": False,
                        "found": True,
                        "task_id": task,
                        "actor_id": actor,
                        "episode": episode_value,
                        "registered_episode": registered_episode,
                        "status": "episode_mismatch",
                        "released_claim_ids": [],
                        "claims_released": 0,
                    }
                # Runner closeout always supplies the episode.  Keep the
                # optional argument for older direct callers, but bind an
                # omitted value to the actor row's *current* episode rather
                # than closing claims from every historical episode.  This
                # prevents an unqualified late closeout from retiring a
                # separately admitted episode that reused the actor id.
                closeout_episode = (
                    registered_episode if episode_value is None else episode_value
                )
                claim_status = "done" if status_value in _SOLVED_ACTOR_STATUSES else "released"
                claim_query = """SELECT * FROM route_claims
                       WHERE task_id=? AND actor_id=? AND LOWER(TRIM(status)) IN ('active', 'blocked')"""
                claim_parameters: list[Any] = [task, actor]
                if closeout_episode is not None:
                    claim_query += " AND episode=?"
                    claim_parameters.append(closeout_episode)
                claim_rows = db.execute(claim_query, claim_parameters).fetchall()
                claim_ids = [str(item["claim_id"]) for item in claim_rows]
                update_query = """UPDATE route_claims
                       SET status=?, updated_at=?, released_at=?, release_reason=?
                       WHERE task_id=? AND actor_id=? AND LOWER(TRIM(status)) IN ('active', 'blocked')"""
                update_parameters: list[Any] = [
                    claim_status,
                    now_text,
                    now_text,
                    reason_value or "actor_finished",
                    task,
                    actor,
                ]
                if closeout_episode is not None:
                    update_query += " AND episode=?"
                    update_parameters.append(closeout_episode)
                db.execute(update_query, update_parameters)
                for claim in claim_rows:
                    self._insert_event(
                        db,
                        "route_claim_released",
                        task_id=task,
                        actor_id=actor,
                        payload={
                            "claim_id": str(claim["claim_id"]),
                            "task_id": task,
                            "actor_id": actor,
                            "route_key": str(claim["route_key"] or "")[:_ROUTE_KEY_LIMIT],
                            "status": claim_status,
                            "reason": reason_value or "actor_finished",
                        },
                    )
                db.execute(
                    """UPDATE actors
                       SET status=?, updated_at=?, heartbeat_at=?, finished_at=?, finish_reason=?
                       WHERE task_id=? AND actor_id=?""",
                    (status_value, now_text, now_text, now_text, reason_value, task, actor),
                )
                updated = self._actor_row(
                    db.execute(
                        "SELECT * FROM actors WHERE task_id=? AND actor_id=?",
                        (task, actor),
                    ).fetchone()
                )
                assert updated is not None
                self._insert_event(
                    db,
                    "actor_finished",
                    task_id=task,
                    actor_id=actor,
                    payload={
                        "task_id": task,
                        "actor_id": actor,
                        "status": status_value,
                        "episode": registered_episode,
                        "reason": reason_value,
                        "released_claim_count": len(claim_ids),
                    },
                )
        updated.update(
            {
                "ok": True,
                "found": True,
                "released_claim_ids": claim_ids,
                "claims_released": len(claim_ids),
            }
        )
        return updated

    def heartbeat_actor(
        self,
        task_id: str,
        actor_id: str,
        *,
        status: str | None = None,
        now: Any = None,
        extend_claims: bool = False,
        ttl_seconds: float | None = None,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Refresh an admitted actor heartbeat without resurrecting it."""

        task = _identifier(task_id, "task_id")
        actor = _identifier(actor_id, "actor_id")
        now_text, now_epoch = _clock(now)
        requested_status = (
            _normal_status(status, default="running") if status is not None else None
        )
        if requested_status in _ACTOR_TERMINAL_STATUSES:
            return {
                "ok": False,
                "task_id": task,
                "actor_id": actor,
                "status": "invalid_heartbeat_status",
            }
        ttl = self._ttl_seconds(ttl_seconds) if ttl_seconds is not None else None
        with self._write_transaction(
            "actor.heartbeat",
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
        ) as db:
                row = db.execute(
                    "SELECT * FROM actors WHERE task_id=? AND actor_id=?",
                    (task, actor),
                ).fetchone()
                if row is None:
                    return {
                        "ok": False,
                        "found": False,
                        "task_id": task,
                        "actor_id": actor,
                        "status": "not_found",
                    }
                current_status = str(row["status"] or "").strip().lower()
                if current_status in (_ACTOR_TERMINAL_STATUSES | {"closing"}):
                    stale = self._actor_row(row)
                    assert stale is not None
                    stale.update({"ok": False, "found": True, "error": "actor_finished"})
                    return stale
                self._expire_route_claims(
                    db,
                    now_text=now_text,
                    now_epoch=now_epoch,
                    task_id=task,
                )
                next_status = requested_status or current_status or "running"
                if next_status not in _ACTOR_LIVE_STATUSES:
                    # A legacy/broken row may carry an unknown status. Do not
                    # bless it as a live actor merely because heartbeat was
                    # requested; surface a stable rejection instead.
                    stale = self._actor_row(row)
                    assert stale is not None
                    stale.update({"ok": False, "found": True, "error": "invalid_actor_status"})
                    return stale
                db.execute(
                    """UPDATE actors
                       SET status=?, updated_at=?, heartbeat_at=?
                       WHERE task_id=? AND actor_id=?""",
                    (next_status, now_text, now_text, task, actor),
                )
                if extend_claims:
                    expiry = _format_epoch(now_epoch + (ttl if ttl is not None else _DEFAULT_ROUTE_TTL_SECONDS))
                    db.execute(
                        """UPDATE route_claims
                           SET updated_at=?, expires_at=?
                           WHERE task_id=? AND actor_id=? AND LOWER(TRIM(status)) IN ('active', 'blocked')""",
                        (now_text, expiry, task, actor),
                    )
                updated = self._actor_row(
                    db.execute(
                        "SELECT * FROM actors WHERE task_id=? AND actor_id=?",
                        (task, actor),
                    ).fetchone()
                )
                assert updated is not None
                self._insert_event(
                    db,
                    "actor_heartbeat",
                    task_id=task,
                    actor_id=actor,
                    payload={
                        "task_id": task,
                        "actor_id": actor,
                        "status": next_status,
                        "heartbeat_at": now_text,
                        "claims_extended": bool(extend_claims),
                    },
                )
        updated.update({"ok": True, "found": True})
        return updated

    # Small aliases make the lifecycle surface convenient for lightweight
    # runner adapters while retaining the explicit canonical names above.
    def register(self, task_id: str, actor_id: str, episode: int = 0, **kwargs: Any) -> dict[str, Any]:
        return self.register_actor(task_id, actor_id, episode, **kwargs)

    def finish(self, task_id: str, actor_id: str, status: str = "finished", **kwargs: Any) -> dict[str, Any]:
        return self.finish_actor(task_id, actor_id, status, **kwargs)

    def heartbeat(self, task_id: str, actor_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.heartbeat_actor(task_id, actor_id, **kwargs)

    def list_active_actors(
        self,
        task_id: str | None = None,
        *,
        include_closing: bool = True,
        limit: int = 100,
        now: Any = None,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Return only currently admitted, non-terminal actors.

        Stale route claims are expired as part of the same short transaction so
        each roster read has a deterministic route projection.
        """

        task = _clip(task_id, _ACTOR_TEXT_LIMIT) if task_id is not None else None
        if task == "":
            task = None
        limit_value = max(1, min(int(limit), 500))
        now_text, now_epoch = _clock(now)
        with self._write_transaction(
            "actor.list_active",
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
        ) as db:
                self._expire_route_claims(db, now_text=now_text, now_epoch=now_epoch, task_id=task)
                # Keep the SQL predicate derived from the same explicit live
                # status registry used by ``_actor_row``. Unknown labels are
                # not an active admission, even if a legacy row happens to
                # carry a truthy bit elsewhere in its projection.
                visible_statuses = set(_ACTOR_LIVE_STATUSES)
                if include_closing:
                    visible_statuses.add("closing")
                live_statuses = tuple(sorted(visible_statuses))
                live_placeholders = ",".join("?" for _ in live_statuses)
                predicates = [f"LOWER(TRIM(status)) IN ({live_placeholders})"]
                parameters: list[Any] = list(live_statuses)
                if task is not None:
                    predicates.append("task_id=?")
                    parameters.append(task)
                rows = db.execute(
                    "SELECT * FROM actors WHERE " + " AND ".join(predicates) + " ORDER BY admitted_at ASC, actor_id ASC LIMIT ?",
                    [*parameters, limit_value],
                ).fetchall()
                actor_rows = [self._actor_row(row) for row in rows]
                actor_rows = [row for row in actor_rows if row is not None]
                for item in actor_rows:
                    claims = db.execute(
                        """SELECT * FROM route_claims
                           WHERE task_id=? AND actor_id=? AND episode=?
                             AND LOWER(TRIM(status)) IN ('active', 'blocked')
                           ORDER BY created_at ASC""",
                        (item["task_id"], item["actor_id"], item["episode"]),
                    ).fetchall()
                    decoded_claims = [self._claim_row(claim) for claim in claims]
                    item["claims"] = [claim for claim in decoded_claims if claim is not None]
                    item["route_claims"] = list(item["claims"])
                    item["current_claim"] = item["claims"][0] if item["claims"] else None
        return actor_rows

    def active_actors(self, task_id: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_active_actors(task_id, **kwargs)

    def list_active_routes(
        self,
        task_id: str | None = None,
        *,
        actor_id: str | None = None,
        limit: int = 100,
        now: Any = None,
        include_closing: bool = False,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Return non-expired primary and independent route claims.

        ``include_closing`` is accepted for broker compatibility.  Claims do
        not have a separate ``closing`` status: once an actor starts closing,
        its claims remain visible until the atomic finish/release transaction.
        """

        del include_closing

        task = _clip(task_id, _ACTOR_TEXT_LIMIT) if task_id is not None else None
        if task == "":
            task = None
        actor = _clip(actor_id, _ACTOR_TEXT_LIMIT) if actor_id is not None else None
        if actor == "":
            actor = None
        limit_value = max(1, min(int(limit), 500))
        now_text, now_epoch = _clock(now)
        with self._write_transaction(
            "route.list_active",
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
        ) as db:
                self._expire_route_claims(db, now_text=now_text, now_epoch=now_epoch, task_id=task)
                predicates = ["LOWER(TRIM(status)) IN ('active', 'blocked')"]
                parameters: list[Any] = []
                if task is not None:
                    predicates.append("task_id=?")
                    parameters.append(task)
                if actor is not None:
                    predicates.append("actor_id=?")
                    parameters.append(actor)
                rows = db.execute(
                    "SELECT * FROM route_claims WHERE " + " AND ".join(predicates) + " ORDER BY created_at ASC, claim_id ASC LIMIT ?",
                    [*parameters, limit_value],
                ).fetchall()
                result = [self._claim_row(row) for row in rows]
        return [item for item in result if item is not None]

    # Short aliases used by broker/tool adapters.  Keep the canonical method
    # names above explicit for direct Python callers and focused tests.
    def active_routes(self, task_id: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_active_routes(task_id, **kwargs)

    def cps_active_routes(self, task_id: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_active_routes(task_id, **kwargs)

    def cps_claim_route(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Canonical CPS-tool spelling for :meth:`claim_route`."""

        return self.claim_route(*args, **kwargs)

    def list_route_claims(self, task_id: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_active_routes(task_id, **kwargs)

    def claim_route(
        self,
        task_id: str,
        actor_id: str,
        episode: int = 0,
        route_key: str | None = None,
        summary: str = "",
        *,
        short_summary: str | None = None,
        ttl_seconds: float | None = None,
        ttl: float | None = None,
        independent_verification_reason: str | None = None,
        independent_reason: str | None = None,
        now: Any = None,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Atomically claim a task route.

        Exactly one primary claim can be active for ``(task_id, route_key)``.
        A competing caller may create a non-primary claim only when it gives a
        bounded independent-verification reason.  A conflict without that
        reason is represented by a bounded response and event; no phantom row
        is inserted.
        """

        task = _identifier(task_id, "task_id")
        actor = _identifier(actor_id, "actor_id")
        episode_value = self._validate_episode(episode)
        route = _identifier(route_key, "route_key", limit=_ROUTE_KEY_LIMIT)
        if short_summary is not None:
            summary = short_summary
        summary_value = _clip(summary, _ROUTE_SUMMARY_LIMIT)
        reason_value = independent_verification_reason
        if independent_reason is not None:
            reason_value = independent_reason
        reason_value = _clip(reason_value, _ROUTE_REASON_LIMIT) or None
        if ttl_seconds is None and ttl is not None:
            ttl_seconds = ttl
        ttl_value = self._ttl_seconds(ttl_seconds)
        now_text, now_epoch = _clock(now)
        expires_at = _format_epoch(now_epoch + ttl_value)
        with self._write_transaction(
            "route.claim",
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
        ) as db:
                self._expire_route_claims(
                    db,
                    now_text=now_text,
                    now_epoch=now_epoch,
                    task_id=task,
                    route_key=route,
                )
                actor_row = db.execute(
                    "SELECT * FROM actors WHERE task_id=? AND actor_id=?",
                    (task, actor),
                ).fetchone()
                actor_registered = actor_row is not None
                if actor_row is None:
                    # Claims are runner-owned state.  Refuse a claim from a
                    # process that was not admitted; otherwise a stale or
                    # hand-crafted actor id could become visible in startup
                    # route coordination.
                    self._insert_event(
                        db,
                        "route_claim_rejected",
                        task_id=task,
                        actor_id=actor,
                        payload={
                            "task_id": task,
                            "actor_id": actor,
                            "route_key": route,
                            "reason": "actor_not_admitted",
                        },
                    )
                    result = self._result_with_claim(
                        None,
                        ok=False,
                        acquired=False,
                        error="actor_not_admitted",
                        actor_registered=False,
                    )
                    result.update(
                        {
                            "task_id": task,
                            "actor_id": actor,
                            "episode": episode_value,
                            "route_key": route,
                            "summary": summary_value,
                            "status": "not_admitted",
                        }
                    )
                    return result
                actor_status = str(actor_row["status"] or "").strip().lower()
                if actor_status in _ACTOR_TERMINAL_STATUSES:
                    result = self._result_with_claim(
                        None,
                        ok=False,
                        acquired=False,
                        error="actor_finished",
                        actor_registered=True,
                    )
                    result.update(
                        {
                            "task_id": task,
                            "actor_id": actor,
                            "episode": episode_value,
                            "route_key": route,
                            "status": "actor_finished",
                        }
                    )
                    self._insert_event(
                        db,
                        "route_claim_rejected",
                        task_id=task,
                        actor_id=actor,
                        payload={
                            "task_id": task,
                            "actor_id": actor,
                            "route_key": route,
                            "reason": "actor_finished",
                        },
                    )
                    return result
                if actor_status not in _ACTOR_LIVE_STATUSES:
                    self._insert_event(
                        db,
                        "route_claim_rejected",
                        task_id=task,
                        actor_id=actor,
                        payload={
                            "task_id": task,
                            "actor_id": actor,
                            "route_key": route,
                            "reason": "invalid_actor_status",
                        },
                    )
                    result = self._result_with_claim(
                        None,
                        ok=False,
                        acquired=False,
                        error="invalid_actor_status",
                        actor_registered=True,
                    )
                    result.update(
                        {
                            "task_id": task,
                            "actor_id": actor,
                            "episode": episode_value,
                            "route_key": route,
                            "status": "invalid_actor_status",
                        }
                    )
                    return result
                registered_episode = self._validate_episode(actor_row["episode"])
                if registered_episode != episode_value:
                    self._insert_event(
                        db,
                        "route_claim_rejected",
                        task_id=task,
                        actor_id=actor,
                        payload={
                            "task_id": task,
                            "actor_id": actor,
                            "route_key": route,
                            "reason": "episode_mismatch",
                            "registered_episode": registered_episode,
                            "requested_episode": episode_value,
                        },
                    )
                    result = self._result_with_claim(
                        None,
                        ok=False,
                        acquired=False,
                        error="episode_mismatch",
                        actor_registered=True,
                    )
                    result.update(
                        {
                            "task_id": task,
                            "actor_id": actor,
                            "episode": episode_value,
                            "route_key": route,
                            "status": "episode_mismatch",
                            "registered_episode": registered_episode,
                        }
                    )
                    return result

                # A retry of an existing claim by the same actor is idempotent.
                own = db.execute(
                    """SELECT * FROM route_claims
                       WHERE task_id=? AND actor_id=? AND episode=? AND route_key=?
                         AND LOWER(TRIM(status)) IN ('active', 'blocked')
                       ORDER BY is_primary DESC, created_at ASC LIMIT 1""",
                    (task, actor, episode_value, route),
                ).fetchone()
                if own is not None:
                    own_item = self._claim_row(own)
                    assert own_item is not None
                    # Fill a missing independent reason on an idempotent retry,
                    # but never let a retry silently change the route owner.
                    if reason_value and not own_item.get("independent_verification_reason"):
                        db.execute(
                            """UPDATE route_claims
                               SET independent_verification_reason=?, updated_at=?
                               WHERE claim_id=?""",
                            (reason_value, now_text, own_item["claim_id"]),
                        )
                        own_item["independent_verification_reason"] = reason_value
                        own_item["independent_verification"] = True
                    self._insert_event(
                        db,
                        "route_claim_reused",
                        task_id=task,
                        actor_id=actor,
                        payload={
                            "claim_id": own_item["claim_id"],
                            "task_id": task,
                            "actor_id": actor,
                            "route_key": route,
                        },
                    )
                    return self._result_with_claim(
                        own_item,
                        ok=True,
                        acquired=True,
                        idempotent=True,
                        actor_registered=actor_registered,
                    )

                primary = db.execute(
                    """SELECT * FROM route_claims
                       WHERE task_id=? AND route_key=?
                         AND is_primary=1 AND LOWER(TRIM(status)) IN ('active', 'blocked')
                       ORDER BY created_at ASC, claim_id ASC LIMIT 1""",
                    (task, route),
                ).fetchone()
                primary_item = self._claim_row(primary)
                if primary_item is not None and not reason_value:
                    # Keep conflict responses useful but bounded; callers can
                    # choose another route or retry with an explicit reason.
                    self._insert_event(
                        db,
                        "route_claim_conflict",
                        task_id=task,
                        actor_id=actor,
                        payload={
                            "task_id": task,
                            "actor_id": actor,
                            "route_key": route,
                            "owner_actor_id": str(primary_item.get("actor_id") or "")[:_ACTOR_TEXT_LIMIT],
                            "claim_id": str(primary_item.get("claim_id") or "")[:_ACTOR_TEXT_LIMIT],
                            "independent_verification_provided": False,
                        },
                    )
                    result = self._result_with_claim(
                        None,
                        ok=False,
                        acquired=False,
                        conflict=primary_item,
                        error="route_conflict",
                        actor_registered=actor_registered,
                    )
                    result.update(
                        {
                            "task_id": task,
                            "actor_id": actor,
                            "episode": episode_value,
                            "route_key": route,
                            "summary": summary_value,
                            "status": "conflict",
                        }
                    )
                    return result

                claim_id = uuid.uuid4().hex
                is_primary = 1 if primary_item is None else 0
                status_value = "active"
                db.execute(
                    """INSERT INTO route_claims(
                           claim_id,task_id,actor_id,episode,route_key,summary,status,
                           created_at,updated_at,expires_at,released_at,
                           independent_verification_reason,is_primary,release_reason)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        claim_id,
                        task,
                        actor,
                        episode_value,
                        route,
                        summary_value,
                        status_value,
                        now_text,
                        now_text,
                        expires_at,
                        None,
                        reason_value,
                        is_primary,
                        None,
                    ),
                )
                claim_item = self._claim_row(
                    db.execute(
                        "SELECT * FROM route_claims WHERE claim_id=?",
                        (claim_id,),
                    ).fetchone()
                )
                assert claim_item is not None
                self._insert_event(
                    db,
                    "route_claim_created",
                    task_id=task,
                    actor_id=actor,
                    payload={
                        "claim_id": claim_id,
                        "task_id": task,
                        "actor_id": actor,
                        "episode": episode_value,
                        "route_key": route,
                        "summary": summary_value,
                        "status": status_value,
                        "is_primary": bool(is_primary),
                        "independent_verification_reason": reason_value,
                    },
                )
        return self._result_with_claim(
            claim_item,
            ok=True,
            acquired=True,
            conflict=primary_item if primary_item is not None else None,
            actor_registered=actor_registered,
        )

    def update_route_claim(
        self,
        claim_id: str,
        actor_id: str | None = None,
        *,
        task_id: str | None = None,
        episode: int | None = None,
        status: str | None = None,
        summary: str | None = None,
        short_summary: str | None = None,
        ttl_seconds: float | None = None,
        ttl: float | None = None,
        independent_verification_reason: str | None = None,
        independent_reason: str | None = None,
        release_reason: str | None = None,
        now: Any = None,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Update a route claim owned by ``actor_id``.

        Ownership is checked in the same write transaction as the mutation.
        ``status=done``/``released`` records a terminal closeout and releases
        the primary-route uniqueness slot immediately.
        """

        claim = _identifier(claim_id, "claim_id")
        owner = _clip(actor_id, _ACTOR_TEXT_LIMIT) if actor_id is not None else None
        if owner == "":
            owner = None
        bound_task = _clip(task_id, _ACTOR_TEXT_LIMIT) if task_id is not None else None
        if bound_task == "":
            bound_task = None
        bound_episode = None if episode is None else self._validate_episode(episode)
        if status is not None:
            status_value = _normal_status(status, default="active", allowed=_ROUTE_STATUS_VALUES)
        else:
            status_value = None
        if short_summary is not None:
            summary = short_summary
        summary_value = _clip(summary, _ROUTE_SUMMARY_LIMIT) if summary is not None else None
        reason_value = independent_verification_reason
        if independent_reason is not None:
            reason_value = independent_reason
        reason_value = _clip(reason_value, _ROUTE_REASON_LIMIT) if reason_value is not None else None
        release_reason_value = _clip(release_reason, _ROUTE_REASON_LIMIT) if release_reason is not None else None
        if ttl_seconds is None and ttl is not None:
            ttl_seconds = ttl
        ttl_value = self._ttl_seconds(ttl_seconds) if ttl_seconds is not None else None
        now_text, now_epoch = _clock(now)
        transaction_operation = (
            "route.release"
            if status_value in _ROUTE_TERMINAL_STATUSES
            else "route.update"
        )
        with self._write_transaction(
            transaction_operation,
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
        ) as db:
                lookup = "SELECT * FROM route_claims WHERE claim_id=?"
                lookup_parameters: list[Any] = [claim]
                if bound_task is not None:
                    lookup += " AND task_id=?"
                    lookup_parameters.append(bound_task)
                if bound_episode is not None:
                    lookup += " AND episode=?"
                    lookup_parameters.append(bound_episode)
                row = db.execute(lookup, lookup_parameters).fetchone()
                if row is None:
                    return {"ok": False, "found": False, "claim_id": claim, "status": "not_found"}
                before = self._claim_row(row)
                assert before is not None
                # Expire this claim before evaluating an update.  A caller may
                # supply a synthetic clock in tests or recovery; never allow a
                # stale primary claim to be resurrected by ``status=active``.
                self._expire_route_claims(
                    db,
                    now_text=now_text,
                    now_epoch=now_epoch,
                    task_id=str(before.get("task_id") or ""),
                    route_key=str(before.get("route_key") or ""),
                )
                row = db.execute(lookup, lookup_parameters).fetchone()
                before = self._claim_row(row)
                assert before is not None
                if owner is not None and str(before.get("actor_id") or "") != owner:
                    self._insert_event(
                        db,
                        "route_claim_update_rejected",
                        task_id=str(before.get("task_id") or ""),
                        actor_id=owner,
                        payload={
                            "claim_id": claim,
                            "task_id": str(before.get("task_id") or "")[:_ACTOR_TEXT_LIMIT],
                            "actor_id": owner,
                            "reason": "not_owner",
                        },
                    )
                    result = self._result_with_claim(
                        before,
                        ok=False,
                        acquired=False,
                        error="not_owner",
                    )
                    result["found"] = True
                    return result
                current_status = str(before.get("status") or "active").strip().lower()
                # Terminal claims are immutable except for an idempotent repeat
                # of the same terminal update.
                if current_status in _ROUTE_TERMINAL_STATUSES:
                    same_independent_reason = (
                        reason_value is None
                        or reason_value
                        == before.get("independent_verification_reason")
                    )
                    same_release_reason = (
                        release_reason_value is None
                        or release_reason_value == before.get("release_reason")
                    )
                    if (
                        status_value not in (None, current_status)
                        or summary_value is not None
                        or ttl_value is not None
                        or not same_independent_reason
                        or not same_release_reason
                    ):
                        result = self._result_with_claim(
                            before,
                            ok=False,
                            acquired=False,
                            error="claim_terminal",
                        )
                        result["found"] = True
                        return result
                next_status = status_value or current_status
                next_summary = summary_value if summary_value is not None else str(before.get("summary") or "")
                next_reason = (
                    reason_value
                    if reason_value is not None
                    else before.get("independent_verification_reason")
                )
                next_expiry = str(before.get("expires_at") or now_text)
                if ttl_value is not None:
                    next_expiry = _format_epoch(now_epoch + ttl_value)
                released_at = before.get("released_at")
                release_reason = before.get("release_reason")
                if next_status in _ROUTE_TERMINAL_STATUSES:
                    released_at = now_text
                    release_reason = release_reason_value or release_reason or (
                        "updated" if next_status == "done" else "released"
                    )
                elif release_reason_value is not None:
                    release_reason = release_reason_value
                try:
                    db.execute(
                        """UPDATE route_claims
                           SET summary=?, status=?, updated_at=?, expires_at=?,
                               released_at=?, independent_verification_reason=?, release_reason=?
                           WHERE claim_id=?""",
                        (
                            next_summary,
                            next_status,
                            now_text,
                            next_expiry,
                            released_at,
                            next_reason,
                            release_reason,
                            claim,
                        ),
                    )
                except sqlite3.IntegrityError:
                    conflict = db.execute(
                        """SELECT * FROM route_claims
                           WHERE task_id=? AND route_key=? AND is_primary=1
                             AND LOWER(TRIM(status)) IN ('active', 'blocked')
                           ORDER BY created_at ASC, claim_id ASC LIMIT 1""",
                        (before.get("task_id"), before.get("route_key")),
                    ).fetchone()
                    conflict_item = self._claim_row(conflict)
                    self._insert_event(
                        db,
                        "route_claim_update_rejected",
                        task_id=str(before.get("task_id") or ""),
                        actor_id=str(before.get("actor_id") or "") or None,
                        payload={
                            "claim_id": claim,
                            "task_id": str(before.get("task_id") or "")[:_ACTOR_TEXT_LIMIT],
                            "actor_id": str(before.get("actor_id") or "")[:_ACTOR_TEXT_LIMIT],
                            "reason": "route_conflict",
                            "conflicting_claim_id": (
                                str(conflict_item.get("claim_id") or "")[:_ACTOR_TEXT_LIMIT]
                                if conflict_item
                                else None
                            ),
                        },
                    )
                    return self._result_with_claim(
                        before,
                        ok=False,
                        acquired=False,
                        conflict=conflict_item,
                        error="route_conflict",
                    )
                after = self._claim_row(
                    db.execute("SELECT * FROM route_claims WHERE claim_id=?", (claim,)).fetchone()
                )
                assert after is not None
                self._insert_event(
                    db,
                    "route_claim_updated",
                    task_id=str(after.get("task_id") or ""),
                    actor_id=str(after.get("actor_id") or "") or None,
                    payload={
                        "claim_id": claim,
                        "task_id": str(after.get("task_id") or "")[:_ACTOR_TEXT_LIMIT],
                        "actor_id": str(after.get("actor_id") or "")[:_ACTOR_TEXT_LIMIT],
                        "route_key": str(after.get("route_key") or "")[:_ROUTE_KEY_LIMIT],
                        "status": next_status,
                        "changed_by": owner,
                    },
                )
        result = self._result_with_claim(
            after,
            ok=True,
            # ``blocked`` remains visible in the active-route projection so
            # peers know this direction is occupied/stalled, but it is not a
            # writable lease.  Keep successful mutation (`ok`) distinct from
            # write authorization (`acquired`/`claimed`).
            acquired=(
                bool(after.get("active"))
                and str(after.get("status") or "").strip().lower() == "active"
            ),
            idempotent=bool(current_status == next_status and status is None),
        )
        result["found"] = True
        return result

    def release_route_claim(
        self,
        claim_id: str,
        actor_id: str | None = None,
        *,
        task_id: str | None = None,
        episode: int | None = None,
        status: str = "released",
        reason: str | None = None,
        now: Any = None,
        deadline_epoch_ms: int | None = None,
        cancel_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Release (or mark done) a route claim, checking optional ownership."""

        status_value = _normal_status(status, default="released", allowed=_ROUTE_TERMINAL_STATUSES)
        claim = _identifier(claim_id, "claim_id")
        owner = _clip(actor_id, _ACTOR_TEXT_LIMIT) if actor_id is not None else None
        reason_value = _clip(reason, _ROUTE_REASON_LIMIT) or None
        result = self.update_route_claim(
            claim,
            owner,
            task_id=task_id,
            episode=episode,
            status=status_value,
            release_reason=reason_value,
            now=now,
            deadline_epoch_ms=deadline_epoch_ms,
            cancel_guard=cancel_guard,
        )
        return result

    # Compatibility aliases for broker adapters and early prototypes.
    def update_route(self, claim_id: str, actor_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.update_route_claim(claim_id, actor_id, **kwargs)

    def cps_update_route(self, claim_id: str, actor_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """Canonical CPS-tool spelling for :meth:`update_route_claim`."""

        return self.update_route_claim(claim_id, actor_id, **kwargs)

    def release_route(self, claim_id: str, actor_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.release_route_claim(claim_id, actor_id, **kwargs)

    def cps_release_route(self, claim_id: str, actor_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """Canonical CPS-tool spelling for :meth:`release_route_claim`."""

        return self.release_route_claim(claim_id, actor_id, **kwargs)

    def summary(self) -> dict[str, Any]:
        with self._db(operation="summary" if self._profiling_enabled else "generic") as db:
            pieces = int(db.execute("SELECT COUNT(*) FROM pieces").fetchone()[0])
            messages = int(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            events = int(db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        return {"pieces": pieces, "messages": messages, "events": events, "db": self.path.name}

    def export_events(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._db(operation="export_events" if self._profiling_enabled else "generic") as db:
            rows = db.execute(
                "SELECT seq,event_id,event_type,task_id,actor_id,payload,created_at FROM events ORDER BY seq"
            ).fetchall()
        with destination.open("w", encoding="utf-8") as handle:
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(item.get("payload") or "{}")
                except json.JSONDecodeError:
                    item["payload"] = {}
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def render_digest(digest: Mapping[str, Any], *, max_chars: int = 6_000) -> str:
    """Render only task-relevant content into a worker prompt."""
    lines: list[str] = []
    for item in digest.get("pieces", []):
        lines.append(
            f"[piece:{item.get('kind','note')}] {item.get('title','')}\n{item.get('body','')}"
        )
    for item in digest.get("messages", []):
        lines.append(f"[message from {item.get('sender','?')}] {item.get('body','')}")
    text = "\n\n".join(lines).strip()
    return text if len(text) <= max_chars else text[:max_chars] + "\n[context truncated]"


def _is_authoritative_validation_piece(item: Mapping[str, Any]) -> bool:
    if str(item.get("author") or "") != "runner":
        return False
    try:
        payload = json.loads(str(item.get("body") or ""))
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, Mapping):
        return False
    return all(
        isinstance(payload.get(key), str)
        and re.fullmatch(r"[0-9a-f]{64}", str(payload[key]).lower()) is not None
        for key in ("candidate_sha256", "task_contract_sha256")
    )


@dataclass
class CommunicationPolicy:
    """Policy facade used by the runner; methods are no-ops for baseline mode."""

    name: str
    store: CPSStore | None

    @property
    def enabled(self) -> bool:
        return self.store is not None and self.name != "none"

    def digest(self, task_id: str, actor_id: str, query: str = "") -> str:
        if not self.enabled:
            return ""
        assert self.store is not None
        return render_digest(
            self.store.digest(
                task_id=task_id,
                actor_id=actor_id,
                query=query,
                include_global=self.name == "hybrid",
            )
        )

    def publish(
        self,
        task_id: str,
        actor_id: str,
        *,
        title: str,
        body: str,
        kind: str = "handoff",
        tags: Iterable[str] = (),
        deadline_epoch_ms: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        assert self.store is not None
        self.store.create_piece(
            task_id=task_id,
            author=actor_id,
            kind=kind,
            title=title,
            body=body,
            tags=tags,
            deadline_epoch_ms=deadline_epoch_ms,
        )

    def send(
        self,
        task_id: str,
        actor_id: str,
        body: str,
        recipient: str | None = None,
        *,
        deadline_epoch_ms: int | None = None,
    ) -> None:
        if not self.enabled or self.name == "blackboard":
            return
        assert self.store is not None
        self.store.send_message(
            task_id=task_id,
            sender=actor_id,
            recipient=recipient,
            body=body,
            deadline_epoch_ms=deadline_epoch_ms,
        )


def make_policy(name: str, store: CPSStore | None) -> CommunicationPolicy:
    normalized = str(name or "none").strip().lower()
    if normalized == "simple":
        normalized = "blackboard"
    if normalized not in {"none", "blackboard", "direct", "hybrid"}:
        raise ValueError(f"unknown communication policy: {name}")
    return CommunicationPolicy(normalized, store if normalized != "none" else None)
