"""Opt-in, low-cardinality resource profiling for ContextSwarm runs.

Profiling is deliberately an observational side channel.  It is disabled by
default, writes no file when disabled, and every diagnostic operation is
fail-open.  The event stream contains bounded identifiers and scalar resource
measurements only; prompts, candidates, provider responses, credentials and
host paths are never serialized.

The implementation intentionally uses Linux ``/proc`` and cgroup files when
available, while degrading to run-level timing on other platforms.  A small
background sampler is started only after :meth:`RunProfiler.start` is called
on an enabled profiler.  Callers may also use ``sample_now`` at lifecycle
boundaries for a deterministic final sample.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterator, Mapping

try:  # ``resource`` is present on the supported Unix runners.
    import resource as _resource
except ImportError:  # pragma: no cover - Windows fallback
    _resource = None  # type: ignore[assignment]


PROFILE_SCHEMA_VERSION = "contextswarm_profile_event_v1"
PROFILE_FILENAME = "profiling.jsonl"
_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSY = frozenset({"0", "false", "no", "off", "disabled", ""})
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PATH_RE = re.compile(r"(?:^|\s)(?:/|[A-Za-z]:[\\/])")
_URL_RE = re.compile(r"(?i)\b(?:https?|tcp|unix)://")
_SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+|authorization\s*[:=]|api[_-]?key\s*[:=]|"
    r"access[_-]?token\s*[:=]|secret\s*[:=]|password\s*[:=])"
)

# Text is allow-listed.  The values are labels, not a general-purpose JSON
# escape hatch; unknown keys (including ``prompt`` and ``candidate``) vanish.
_TEXT_FIELDS = frozenset(
    {
        "phase",
        "operation",
        "stage",
        "status",
        "reason",
        "outcome",
        "source",
        "mode",
        "kind",
        "event_type",
        "error_kind",
        "selector",
        "selector_name",
        "policy",
        "purpose",
        "close_reason",
        "queue_state",
        "retry_kind",
        "settlement_state",
        "transport",
        "role",
        "process_state",
        "sample_kind",
        "component",
        "fallback_reason",
        "disposition",
        "source_kind",
        "communication",
    }
)

# Resource, queue and lifecycle counters.  Keep this list explicit so a new
# caller cannot accidentally add a large nested payload to the JSONL stream.
_SCALAR_FIELDS = frozenset(
    {
        "accepted",
        "active",
        "active_handlers",
        "active_slots",
        "active_solver_slots",
        "admitted",
        "agent_result_valid",
        "agent_run_horizon_reached",
        "attempt",
        "attempt_count",
        "artifact_bytes",
        "bytes",
        "candidate_count",
        "cache_reused",
        "cache_hit",
        "cache_read_tokens",
        "cache_write_tokens",
        "call_index",
        "cancelled",
        "candidates",
        "closeout_executor_limit",
        "commit_seconds",
        "communication_enabled",
        "complete",
        "completed",
        "context_switches",
        "cpu_system_seconds",
        "cpu_throttled_seconds",
        "cpu_user_seconds",
        "db_bytes",
        "decision_index",
        "delivered_tokens",
        "disk_free_bytes",
        "drained",
        "dropped_fields",
        "elapsed_seconds",
        "episode",
        "error_count",
        "events",
        "fd_count",
        "gate_wait_seconds",
        "fifo_depth",
        "flush_seconds",
        "heartbeat_seq",
        "input_tokens",
        "isolated",
        "items",
        "lock_wait_seconds",
        "max_concurrent",
        "max_restarts",
        "max_parallel",
        "max_workers",
        "memory_bytes",
        "memory_current_bytes",
        "memory_events_count",
        "memory_peak_bytes",
        "monotonic_elapsed_seconds",
        "oom_kill_count",
        "output_tokens",
        "pages_scanned",
        "pid",
        "pool_depth",
        "process_alive",
        "process_count",
        "process_tree_count",
        "profile_bytes",
        "proved",
        "pss_bytes",
        "probe_calls",
        "queue_depth",
        "queued_count",
        "ranked_count",
        "receipt_count",
        "records",
        "recoverable_invocation_error",
        "returncode",
        "retryable",
        "rows",
        "rows_scanned",
        "rss_bytes",
        "running_count",
        "score",
        "selected_count",
        "selection_enabled",
        "selection_candidate_count",
        "selection_ranked_count",
        "selection_persisted_count",
        "settled",
        "snapshot_count",
        "snapshot_hit",
        "snapshot_pages",
        "stderr_buffer_bytes",
        "stdout_buffer_bytes",
        "system_cpu_seconds",
        "sqlite_bytes",
        "task_count",
        "thread_count",
        "total_tokens",
        "transaction_seconds",
        "timeout_seconds",
        "text_chars",
        "timed_out",
        "tokenize_count",
        "total_cpu_seconds",
        "total_memory_bytes",
        "wait_seconds",
        "wall_seconds",
        "worker_count",
        "wal_bytes",
        "workspace_bytes",
    }
)

# Opaque correlation handles are useful for joining a receipt to the runner's
# audit without admitting arbitrary text.  Values that look like paths/URLs or
# exceed the identifier grammar are replaced by a short hash in
# ``_safe_identifier``.
_IDENTIFIER_FIELDS = frozenset(
    {
        "call_id",
        "judge_job_id",
        "scheduler_call_id",
    }
)

_HASH_FIELDS = frozenset(
    {
        "candidate_sha256",
        "comparison_contract_id",
        "config_sha256",
        "request_key_sha256",
        "selector_config_id",
        "snapshot_sha256",
        "source_revision",
        "task_contract_sha256",
        "trace_watermark_sha256",
    }
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _finite_number(value: Any) -> int | float | bool | None:
    # Booleans are meaningful lifecycle counters (accepted, timed_out, etc.)
    # and must not be mistaken for integers and silently dropped.
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 10**15 else None
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) and abs(value) <= 10**15 else None
    return None


def _safe_identifier(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > 256:
        return None
    if _ID_RE.fullmatch(text) or _SHA_RE.fullmatch(text):
        return text
    # Preserve correlation without exposing a path, URL or token-like value.
    return "opaque:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _safe_text(value: Any) -> str | None:
    text = str(value or "").replace("\x00", "").strip()
    # Labels should stay small.  A long natural-language value is much more
    # likely to be a prompt/error payload than a useful profiling dimension.
    if not text or len(text.encode("utf-8")) > 160:
        return None
    if _URL_RE.search(text) or _PATH_RE.search(text) or _SECRET_RE.search(text):
        return None
    if "\n" in text or "\r" in text:
        return None
    return text


def _safe_hash(value: Any) -> str | None:
    text = str(value or "").strip()
    return text.lower() if _SHA_RE.fullmatch(text) else None


def _safe_field(key: str, value: Any) -> Any:
    if key in _HASH_FIELDS:
        return _safe_hash(value)
    if key in _IDENTIFIER_FIELDS:
        return _safe_identifier(value)
    if key in _TEXT_FIELDS:
        return _safe_text(value)
    if key in _SCALAR_FIELDS:
        return _finite_number(value)
    return None


def _normalise_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return False


def _interval_from_env(value: Any, default: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    # Avoid profiling becoming a high-frequency workload of its own.
    return min(60.0, max(0.1, number))


def _path_inside(root: Path, candidate: Path) -> Path | None:
    try:
        resolved_root = root.expanduser().resolve()
        resolved_candidate = candidate.expanduser().resolve(strict=False)
        resolved_candidate.relative_to(resolved_root)
        return resolved_candidate
    except (OSError, RuntimeError, ValueError):
        return None


@dataclass(frozen=True)
class ProfilerSettings:
    """Resolved opt-in profiler settings."""

    enabled: bool = False
    path: Path | None = None
    heartbeat_interval_seconds: float = 1.0

    @classmethod
    def from_environment(
        cls,
        output_dir: Path,
        *,
        enabled: bool | None = None,
    ) -> "ProfilerSettings":
        if enabled is None:
            raw_enabled: Any = os.environ.get("CONTEXTSWARM_PROFILE")
            if raw_enabled is None:
                raw_enabled = os.environ.get("CONTEXTSWARM_RESOURCE_PROFILING")
            if raw_enabled is None:
                raw_enabled = os.environ.get("CONTEXTSWARM_PROFILING")
            active = _normalise_enabled(raw_enabled)
        else:
            active = bool(enabled)
        interval_value = os.environ.get(
            "CONTEXTSWARM_PROFILE_HEARTBEAT_SECONDS",
            os.environ.get("CONTEXTSWARM_PROFILE_INTERVAL_SECONDS", "1"),
        )
        interval = _interval_from_env(interval_value)
        root = Path(output_dir)
        configured = os.environ.get("CONTEXTSWARM_PROFILE_PATH", "").strip()
        path: Path | None = None
        if configured:
            candidate = Path(configured)
            path = _path_inside(root, candidate if candidate.is_absolute() else root / candidate)
        if path is None:
            path = root / PROFILE_FILENAME
        return cls(enabled=active, path=path if active else None, heartbeat_interval_seconds=interval)


class _NullSpan:
    """Allocation-free disabled span object."""

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        return None


def _cpu_times() -> tuple[float, float]:
    if _resource is None:
        return 0.0, 0.0
    try:
        usage = _resource.getrusage(_resource.RUSAGE_SELF)
        return float(usage.ru_utime), float(usage.ru_stime)
    except (AttributeError, OSError, ValueError):
        return 0.0, 0.0


class _Span:
    def __init__(
        self,
        profiler: "RunProfiler",
        name: str,
        *,
        task_id: Any = None,
        actor_id: Any = None,
        fields: Mapping[str, Any],
    ) -> None:
        self.profiler = profiler
        self.name = name
        self.task_id = task_id
        self.actor_id = actor_id
        self.fields = dict(fields)
        self.started = time.monotonic()
        self.cpu_user_started, self.cpu_system_started = _cpu_times()

    def _emit(self, event: str, **fields: Any) -> None:
        # Keep correlation identities on the dedicated ``emit`` parameters so
        # they pass identifier sanitisation rather than being treated as
        # arbitrary payload fields.  This is what lets a selection/Judge span
        # be joined back to one task/agent without admitting prompt content.
        self.profiler.emit(
            event,
            task_id=self.task_id,
            actor_id=self.actor_id,
            **fields,
        )

    def __enter__(self) -> "_Span":
        self._emit(self.name + ".start", **self.fields)
        return self

    def __exit__(self, exc_type: Any, exc: Any, _tb: Any) -> None:
        ended = time.monotonic()
        cpu_user, cpu_system = _cpu_times()
        payload = dict(self.fields)
        payload.update(
            {
                "wall_seconds": ended - self.started,
                "cpu_user_seconds": max(0.0, cpu_user - self.cpu_user_started),
                "cpu_system_seconds": max(0.0, cpu_system - self.cpu_system_started),
                "status": "error" if exc_type is not None else "ok",
            }
        )
        if exc_type is not None:
            payload["error_kind"] = getattr(exc_type, "__name__", "error")
        self._emit(self.name + ".end", **payload)


class RunProfiler:
    """Thread-safe JSONL sink plus optional process/cgroup sampler."""

    def __init__(
        self,
        output_dir: Path,
        *,
        enabled: bool = False,
        path: Path | None = None,
        heartbeat_interval_seconds: float = 1.0,
        run_id: Any = None,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser().resolve()
        requested_path = path if path is not None else self.output_dir / PROFILE_FILENAME
        safe_path = _path_inside(self.output_dir, Path(requested_path))
        self.path = safe_path or self.output_dir / PROFILE_FILENAME
        self.enabled = bool(enabled) and safe_path is not None
        self.heartbeat_interval_seconds = _interval_from_env(heartbeat_interval_seconds)
        self.run_id = _safe_identifier(run_id)
        self._lock = threading.RLock()
        self._handle: Any | None = None
        self._closed = False
        self._started = False
        self._started_monotonic = time.monotonic()
        self._sequence = 0
        self._sample_sequence = 0
        self._last_sample = 0.0
        self._artifact_snapshot_at = 0.0
        self._artifact_metrics: dict[str, int] = {}
        self._root_pid = os.getpid()
        self._processes: dict[int, dict[str, Any]] = {}
        self._sampler_stop = threading.Event()
        self._sampler_wakeup = threading.Event()
        self._sampler_thread: threading.Thread | None = None

    @classmethod
    def from_environment(
        cls,
        output_dir: Path,
        *,
        enabled: bool | None = None,
        run_id: Any = None,
    ) -> "RunProfiler":
        settings = ProfilerSettings.from_environment(output_dir, enabled=enabled)
        return cls(
            output_dir,
            enabled=settings.enabled,
            path=settings.path,
            heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
            run_id=run_id,
        )

    def start(self, *, run_id: Any = None, root_pid: int | None = None) -> "RunProfiler":
        """Start low-frequency sampling; idempotent and safe to call late."""

        if not self.enabled:
            return self
        try:
            with self._lock:
                if self._closed:
                    return self
                if run_id is not None:
                    self.run_id = _safe_identifier(run_id)
                if root_pid is not None and isinstance(root_pid, int) and root_pid > 0:
                    self._root_pid = root_pid
                if not self._started:
                    self._started = True
                    self._started_monotonic = time.monotonic()
                    self._processes.setdefault(
                        self._root_pid,
                        {"task_id": None, "actor_id": None, "role": "runner"},
                    )
                    self.emit("profile.start", phase="profiling", role="runner")
                    self._sampler_thread = threading.Thread(
                        target=self._sampler_loop,
                        name="contextswarm-profiler",
                        daemon=True,
                    )
                    self._sampler_thread.start()
        except Exception:
            return self
        return self

    def _open(self) -> Any | None:
        if not self.enabled or self._closed:
            return None
        if self._handle is not None:
            return self._handle
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            if self.path.is_symlink():
                return None
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            self._handle = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        except OSError:
            self._handle = None
        return self._handle

    def emit(
        self,
        event: str,
        *,
        run_id: Any = None,
        task_id: Any = None,
        actor_id: Any = None,
        **fields: Any,
    ) -> None:
        """Write one sanitized profiling event, swallowing all sink errors."""

        if not self.enabled:
            return
        try:
            name = str(event or "").strip().casefold()
            if not _EVENT_RE.fullmatch(name):
                return
            row: dict[str, Any] = {
                "schema_version": PROFILE_SCHEMA_VERSION,
                "sequence": 0,
                "at": _utc_now(),
                "monotonic_ns": time.monotonic_ns(),
                "event": name,
            }
            for key, value in (
                ("run_id", self.run_id if run_id is None else run_id),
                ("task_id", task_id),
                ("actor_id", actor_id),
            ):
                safe = _safe_identifier(value)
                if safe is not None:
                    row[key] = safe
            dropped = 0
            for raw_key, value in fields.items():
                key = str(raw_key).strip()
                safe_value = _safe_field(key, value)
                if safe_value is None:
                    dropped += 1
                    continue
                row[key] = safe_value
            if dropped:
                row["dropped_fields"] = dropped
            with self._lock:
                handle = self._open()
                if handle is None:
                    return
                self._sequence += 1
                row["sequence"] = self._sequence
                handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")
        except (OSError, ValueError, TypeError, OverflowError, RuntimeError):
            return

    def observe_logger_event(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Translate runner lifecycle events into bounded profile rows."""

        if not self.enabled:
            return
        try:
            payload = payload or {}
            event = str(event_type or "").strip().casefold()
            mapped = {
                "run_started": "run.start",
                "run_finished": "run.end",
                "run_error": "run.error",
                "dry_run_finished": "run.dry_end",
                "horizon_started": "horizon.start",
                "horizon_closed": "horizon.end",
                "agent_assigned": "allocation.assignment",
                "agent_finished": "agent.result",
                "agent_refill_scheduled": "agent.refill.scheduled",
                "agent_refill_started": "agent.refill.start",
                "agent_refill_succeeded": "agent.refill.end",
                "agent_refill_exhausted": "agent.refill.exhausted",
                "evaluation_backpressure_wait": "judge.queue.wait",
                "evaluation_backpressure_expired": "judge.queue.expired",
                "evaluation_finished": "judge.receipt",
                "allocation_decision": "allocation.decision",
                "allocation_scheduler_finished": "scheduler.invocation.end",
                "selection_runtime_initialized": "selection.runtime.start",
                "selection_runtime_closed": "selection.runtime.end",
                "closeout_started": "closeout.start",
                "closeout_evaluation_finished": "closeout.evaluation.end",
                "closeout_finished": "closeout.end",
                "judge_broker_closed": "drain.end",
                "broker_drain_timeout": "drain.timeout",
                "broker_close_error": "drain.error",
                "remote_settlement_unconfirmed": "judge.settlement.pending",
            }.get(event)
            if mapped is None:
                if event.startswith(("trace_", "cps_", "artifact_", "scheduler_")):
                    mapped = event.replace("_", ".")
                elif event in {"scoreboard_record", "preflight_failed"}:
                    mapped = event.replace("_", ".")
                else:
                    return
            identities = {
                key: payload.get(key)
                for key in ("run_id", "task_id", "agent_id", "actor_id")
                if key in payload
            }
            if "agent_id" in identities and "actor_id" not in identities:
                identities["actor_id"] = identities.pop("agent_id")
            fields = {key: value for key, value in payload.items() if key not in identities}
            self.emit(mapped, **identities, **fields)
            if event in {
                "run_finished",
                "run_error",
                "dry_run_finished",
                # No later runner lifecycle event is guaranteed after a
                # preflight admission failure; close here so the sampler
                # thread and file descriptor cannot survive the failed run.
                "preflight_failed",
            }:
                self.close()
        except Exception:
            return

    def observe_pi_event(
        self,
        event_type: str,
        *,
        task_id: Any = None,
        actor_id: Any = None,
        episode: Any = None,
        **fields: Any,
    ) -> None:
        """Record model/tool lifecycle events without RPC content."""

        event = str(event_type or "").strip().casefold()
        mapped = {
            "message_start": "model.request.start",
            "message_end": "model.request.end",
            "tool_execution_start": "tool.start",
            "tool_execution_end": "tool.end",
            "tool_call": "tool.start",
            "tool_result": "tool.end",
            "agent_end": "agent.rpc.end",
            "agent_settled": "agent.rpc.settled",
        }.get(event)
        if mapped is None:
            return
        self.emit(mapped, task_id=task_id, actor_id=actor_id, episode=episode, **fields)

    def heartbeat(
        self,
        *,
        task_id: Any = None,
        actor_id: Any = None,
        episode: Any = None,
        **fields: Any,
    ) -> None:
        self.emit("agent.heartbeat", task_id=task_id, actor_id=actor_id, episode=episode, **fields)
        self.sample_now()

    def register_process(
        self,
        pid: int,
        *,
        task_id: Any = None,
        actor_id: Any = None,
        role: str = "solver",
    ) -> None:
        if not self.enabled:
            return
        try:
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                return
            with self._lock:
                self._processes[pid] = {"task_id": task_id, "actor_id": actor_id, "role": role}
            self.emit("resource.process.register", task_id=task_id, actor_id=actor_id, pid=pid, role=role)
        except Exception:
            return

    def unregister_process(self, pid: int, *, status: str = "exited") -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                metadata = self._processes.pop(pid, None)
            if metadata is not None:
                self.emit(
                    "resource.process.unregister",
                    task_id=metadata.get("task_id"),
                    actor_id=metadata.get("actor_id"),
                    pid=pid,
                    role=metadata.get("role", "solver"),
                    status=status,
                )
        except Exception:
            return

    register = register_process
    unregister = unregister_process

    @staticmethod
    def _children(pid: int) -> tuple[int, ...]:
        try:
            raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
            return tuple(int(token) for token in raw.split() if token.isdigit() and int(token) > 0)
        except (OSError, ValueError):
            return ()

    @classmethod
    def _tree(cls, root: int) -> tuple[int, ...]:
        seen: set[int] = set()
        queue = [root]
        while queue:
            pid = queue.pop(0)
            if pid in seen or pid <= 0:
                continue
            seen.add(pid)
            queue.extend(cls._children(pid))
        return tuple(sorted(seen))

    @staticmethod
    def _proc_snapshot(pid: int) -> dict[str, Any] | None:
        """Read bounded metrics for one Linux process."""

        if os.name != "posix":
            return None
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            closing = stat_line.rfind(")")
            if closing < 0:
                return None
            fields = stat_line[closing + 2 :].split()
            if len(fields) < 22:
                return None
            hz = float(os.sysconf("SC_CLK_TCK"))
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            result: dict[str, Any] = {
                "pid": pid,
                "process_state": fields[0],
                "cpu_user_seconds": max(0.0, int(fields[11]) / hz),
                "cpu_system_seconds": max(0.0, int(fields[12]) / hz),
                "thread_count": max(0, int(fields[17])),
                "rss_bytes": max(0, int(fields[21]) * page_size),
                "pss_bytes": 0,
                "context_switches": 0,
                "fd_count": 0,
            }
            try:
                rollup = Path(f"/proc/{pid}/smaps_rollup").read_text(encoding="ascii")
                for line in rollup.splitlines():
                    if line.startswith("Pss:"):
                        result["pss_bytes"] = max(0, int(line.split()[1]) * 1024)
                        break
            except (OSError, ValueError, IndexError):
                result["pss_bytes"] = result["rss_bytes"]
            try:
                status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
                voluntary = nonvoluntary = 0
                for line in status.splitlines():
                    if line.startswith("voluntary_ctxt_switches:"):
                        voluntary = int(line.split()[-1])
                    elif line.startswith("nonvoluntary_ctxt_switches:"):
                        nonvoluntary = int(line.split()[-1])
                result["context_switches"] = max(0, voluntary + nonvoluntary)
            except (OSError, ValueError, IndexError):
                pass
            try:
                result["fd_count"] = len(os.listdir(f"/proc/{pid}/fd"))
            except OSError:
                pass
            return result
        except (OSError, ValueError, IndexError, OverflowError):
            return None

    @staticmethod
    def _cgroup_candidates(cgroup_text: str | None = None) -> tuple[Path, ...]:
        """Return the current cgroup first, with the hierarchy root as fallback.

        The cgroup path is used only for local file lookup and is never returned
        in an event.  ``0::`` is the cgroup-v2 controller line; its relative
        scope must win over the hierarchy root because the two can have very
        different memory counters in a shared host.
        """

        base = Path("/sys/fs/cgroup")
        if os.name != "posix":
            return ()
        if cgroup_text is None:
            try:
                cgroup_text = Path("/proc/self/cgroup").read_text(encoding="ascii")
            except OSError:
                cgroup_text = ""
        candidates: list[Path] = []
        for line in str(cgroup_text).splitlines():
            if not line.startswith("0::"):
                continue
            relative = line[3:].strip().lstrip("/")
            if relative and ".." not in Path(relative).parts:
                scoped = base / relative
                try:
                    scoped.relative_to(base)
                except ValueError:
                    pass
                else:
                    candidates.append(scoped)
            break
        if base not in candidates:
            candidates.append(base)
        return tuple(candidates)

    @staticmethod
    def _cgroup_snapshot() -> dict[str, Any]:
        """Read cgroup v2 counters without exposing the cgroup path."""

        if os.name != "posix":
            return {}
        candidates = RunProfiler._cgroup_candidates()
        result: dict[str, Any] = {}
        for root in candidates:
            try:
                if not root.is_dir():
                    continue
                for filename, key in (("memory.current", "memory_current_bytes"), ("memory.peak", "memory_peak_bytes")):
                    if key not in result:
                        try:
                            result[key] = max(0, int((root / filename).read_text().strip()))
                        except (OSError, ValueError):
                            pass
                try:
                    memory_events = (root / "memory.events").read_text(encoding="ascii")
                    total = 0
                    for line in memory_events.splitlines():
                        parts = line.split()
                        if len(parts) == 2 and parts[1].isdigit():
                            total += int(parts[1])
                            if parts[0] == "oom_kill":
                                result["oom_kill_count"] = int(parts[1])
                    result["memory_events_count"] = total
                except (OSError, ValueError):
                    pass
                try:
                    cpu_stat = (root / "cpu.stat").read_text(encoding="ascii")
                    for line in cpu_stat.splitlines():
                        parts = line.split()
                        if len(parts) == 2 and parts[0] == "throttled_usec" and parts[1].isdigit():
                            result["cpu_throttled_seconds"] = int(parts[1]) / 1_000_000.0
                except (OSError, ValueError):
                    pass
                if result:
                    return result
            except OSError:
                continue
        return result

    def _artifact_snapshot(self) -> dict[str, int]:
        result = {"artifact_bytes": 0, "sqlite_bytes": 0, "wal_bytes": 0, "profile_bytes": 0, "disk_free_bytes": 0}
        try:
            for root, dirs, files in os.walk(self.output_dir):
                dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
                for name in files:
                    path = Path(root) / name
                    try:
                        size = max(0, int(path.stat().st_size))
                    except OSError:
                        continue
                    result["artifact_bytes"] += size
                    lowered = name.casefold()
                    if lowered.endswith(".sqlite3") or lowered.endswith(".sqlite"):
                        result["sqlite_bytes"] += size
                    elif lowered.endswith("-wal") or lowered.endswith("-shm"):
                        result["wal_bytes"] += size
                    if path == self.path:
                        result["profile_bytes"] += size
            statvfs = os.statvfs(self.output_dir)
            result["disk_free_bytes"] = max(0, int(statvfs.f_bavail * statvfs.f_frsize))
        except (OSError, ValueError, OverflowError):
            pass
        return result

    def sample_now(self, *, force: bool = False) -> dict[str, Any]:
        """Take and emit one aggregate process/cgroup sample."""

        if not self.enabled:
            return {}
        try:
            now = time.monotonic()
            with self._lock:
                if self._closed:
                    return {}
                if not force and self._last_sample and now - self._last_sample < self.heartbeat_interval_seconds:
                    return {}
                self._last_sample = now
                registered = dict(self._processes)
            roots = tuple(registered) or (self._root_pid,)
            all_pids: set[int] = set()
            root_pids: dict[int, tuple[int, ...]] = {}
            for root in roots:
                tree = self._tree(root)
                root_pids[root] = tree
                all_pids.update(tree)
            snapshots = {pid: snap for pid in all_pids if (snap := self._proc_snapshot(pid)) is not None}
            aggregate: dict[str, Any] = {
                "process_count": len(snapshots),
                "process_tree_count": len(all_pids),
                "thread_count": sum(int(item.get("thread_count", 0)) for item in snapshots.values()),
                "fd_count": sum(int(item.get("fd_count", 0)) for item in snapshots.values()),
                "rss_bytes": sum(int(item.get("rss_bytes", 0)) for item in snapshots.values()),
                "pss_bytes": sum(int(item.get("pss_bytes", 0)) for item in snapshots.values()),
                "cpu_user_seconds": round(sum(float(item.get("cpu_user_seconds", 0.0)) for item in snapshots.values()), 6),
                "cpu_system_seconds": round(sum(float(item.get("cpu_system_seconds", 0.0)) for item in snapshots.values()), 6),
                "context_switches": sum(int(item.get("context_switches", 0)) for item in snapshots.values()),
                "monotonic_elapsed_seconds": now - self._started_monotonic,
            }
            aggregate.update(self._cgroup_snapshot())
            # Directory-wide stat calls are materially more expensive than
            # the process counters (especially after a high-concurrency run
            # has produced hundreds of worker artifacts).  Keep resource
            # samples frequent, but refresh artifact/WAL totals at a bounded
            # lower rate and always force a fresh snapshot for closeout.
            artifact_interval = max(5.0, self.heartbeat_interval_seconds * 5.0)
            with self._lock:
                artifact_metrics = dict(self._artifact_metrics)
                refresh_artifacts = (
                    force
                    or not artifact_metrics
                    or now - self._artifact_snapshot_at >= artifact_interval
                )
            if refresh_artifacts:
                fresh_artifacts = self._artifact_snapshot()
                with self._lock:
                    self._artifact_metrics = dict(fresh_artifacts)
                    self._artifact_snapshot_at = now
                    artifact_metrics = dict(fresh_artifacts)
            aggregate.update(artifact_metrics)
            # Multiple agent threads can request a forced boundary sample at
            # nearly the same time.  Allocate the sequence number under the
            # same lock used by ``emit`` so the JSONL stream remains strictly
            # monotonic even under concurrent closeout callbacks.
            with self._lock:
                self._sample_sequence += 1
                sample_sequence = self._sample_sequence
            aggregate["snapshot_count"] = sample_sequence
            self.emit("resource.sample", role="run", sample_kind="aggregate", **aggregate)
            for root, tree in root_pids.items():
                metadata = registered.get(root, {"task_id": None, "actor_id": None, "role": "runner"})
                members = [snapshots[pid] for pid in tree if pid in snapshots]
                if not members:
                    continue
                self.emit(
                    "resource.process",
                    task_id=metadata.get("task_id"),
                    actor_id=metadata.get("actor_id"),
                    pid=root,
                    role=metadata.get("role", "solver"),
                    process_count=len(members),
                    thread_count=sum(int(item.get("thread_count", 0)) for item in members),
                    fd_count=sum(int(item.get("fd_count", 0)) for item in members),
                    rss_bytes=sum(int(item.get("rss_bytes", 0)) for item in members),
                    pss_bytes=sum(int(item.get("pss_bytes", 0)) for item in members),
                    cpu_user_seconds=round(sum(float(item.get("cpu_user_seconds", 0.0)) for item in members), 6),
                    cpu_system_seconds=round(sum(float(item.get("cpu_system_seconds", 0.0)) for item in members), 6),
                    context_switches=sum(int(item.get("context_switches", 0)) for item in members),
                    process_alive=bool(members),
                )
            return aggregate
        except Exception:
            return {}

    sample = sample_now
    snapshot = sample_now
    process_snapshot = sample_now

    def _sampler_loop(self) -> None:
        while not self._sampler_stop.wait(self.heartbeat_interval_seconds):
            self.sample_now()

    @contextmanager
    def span(
        self,
        name: str,
        *,
        task_id: Any = None,
        actor_id: Any = None,
        episode: Any = None,
        **fields: Any,
    ) -> Iterator[Any]:
        if not self.enabled:
            yield _NullSpan()
            return
        normalized = str(name or "").strip().casefold()
        if not _EVENT_RE.fullmatch(normalized):
            yield _NullSpan()
            return
        span_fields = {"episode": episode, **fields}
        with _Span(
            self,
            normalized,
            task_id=task_id,
            actor_id=actor_id,
            fields=span_fields,
        ) as span:
            yield span

    def close(self) -> None:
        """Stop sampling, flush a final sample, and close the sink."""

        if not self.enabled:
            return
        thread: threading.Thread | None = None
        try:
            with self._lock:
                if self._closed:
                    return
                self._sampler_stop.set()
                self._sampler_wakeup.set()
                thread = self._sampler_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(1.0, self.heartbeat_interval_seconds * 2.0))
            self.sample_now(force=True)
            self.emit(
                "profile.end",
                phase="profiling",
                role="runner",
                elapsed_seconds=time.monotonic() - self._started_monotonic,
                snapshot_count=self._sample_sequence,
            )
        except Exception:
            pass
        finally:
            with self._lock:
                self._closed = True
                handle = self._handle
                self._handle = None
                if handle is not None:
                    try:
                        handle.flush()
                        handle.close()
                    except OSError:
                        pass


__all__ = ["PROFILE_FILENAME", "PROFILE_SCHEMA_VERSION", "ProfilerSettings", "RunProfiler"]
