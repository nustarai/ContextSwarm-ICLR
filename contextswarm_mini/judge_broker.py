"""Run-local capability broker for solver Judge probes and CPS operations.

The solver process receives only an unguessable loopback URL.  The broker owns
the real evaluator, task/baseline binding, candidate path, CPS identity, global
admission semaphore, and experiment deadline.  Worker-supplied payloads cannot
override any of those values.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import datetime as dt
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterator, Mapping

from .cps import CPSStore
from .evaluator import (
    LEAN_PROBE_RESPONSE_PROFILE,
    safe_worker_response,
    sanitize_worker_identifier,
    sanitize_worker_text,
)
from .formal_tools import FormalToolPolicy, sanitize_public_text
from .models import Task, Verdict
from .secure_io import DEFAULT_MAX_CANDIDATE_BYTES, read_regular_bytes
from .selection_store import CANONICAL_FEEDBACK_KINDS, SelectionStore


_MAX_REQUEST_BYTES = 32 * 1024
# Keep a per-session guard, but leave enough room for a bounded CPS solver to
# work through a hard task.  The old value (32) was routinely reached by
# normal formal agents before the one-hour horizon, turning an otherwise
# valid candidate-attempt stream into a fail-closed infrastructure error.
# 128 remains finite and, together with the one-second admission interval and
# the fixed horizon, bounds one session to a small, auditable share of Judge
# capacity.  This is a broker-wide runtime guard and is identical for every
# allocation arm; it is not a policy-specific tuning knob.
_MAX_PROBE_CALLS_PER_SESSION = 128
_MIN_PROBE_INTERVAL_SECONDS = 1.0
_PROBE_ADMISSION_TIMEOUT_SECONDS: float | None = None
# Closeout is outside the solver horizon.  A five-second drain was sufficient
# for a single canary, but it races legitimate Judge handlers when several
# formal arms revoke their sessions together: queued cancellation/receipt
# reconciliation can take tens of seconds even after the remote service is
# healthy.  Keep this bounded (and fail closed if it is genuinely stuck), but
# leave enough time for the fixed Judge lifecycle to settle.  The default is
# extended below when the evaluator exposes its deferred-settlement horizon;
# the extra margin covers the final poll/callback handoff.
_BROKER_DRAIN_TIMEOUT_SECONDS = 120.0
_BROKER_DRAIN_SETTLEMENT_MARGIN_SECONDS = 60.0
_RUNNER_ONLY_CPS_KINDS = frozenset({"validation_result"})
_ROUTE_CLAIM_OPERATIONS = frozenset(
    {
        "cps_active_routes",
        "cps_claim_route",
        "cps_update_route",
        "cps_release_route",
    }
)
# Route discovery and the first claim are deliberately the only CPS calls
# which can run before the first terminal Judge checkpoint.  Keeping this
# allowlist explicit prevents a future route operation from accidentally
# widening the historical pre-Judge communication surface.
_PRE_JUDGE_ROUTE_OPERATIONS = frozenset(
    {"cps_active_routes", "cps_claim_route"}
)
_DEFAULT_ROUTE_CLAIM_TTL_SECONDS = 900.0
_MAX_ROUTE_CLAIM_TTL_SECONDS = 86_400.0
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
# Runner/CPS adapters may expose a small lifecycle vocabulary while an actor is
# live. Keep it explicit at the trust boundary: an arbitrary non-terminal
# string (for example ``garbage`` from a broken adapter) must not mint route
# admission merely because it is not in the terminal set.
_ACTOR_LIVE_STATUSES = frozenset(
    {
        "active",
        "admitted",
        "running",
    }
)
_ROUTE_VISIBLE_STATUSES = frozenset({"active", "blocked"})
_ROUTE_TERMINAL_STATUSES = frozenset(
    {
        "done",
        "released",
        "expired",
        "cancelled",
        "canceled",
        "closed",
        "finished",
    }
)
_ROUTE_UPDATE_STATUSES = frozenset({"active", "blocked", "done", "released"})
_ROUTE_RELEASE_STATUSES = frozenset({"done", "released"})
_ALLOWED_CANDIDATE_FILENAMES = frozenset({"result.lean", "result.cpp"})
_LEAN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_'.₀-₉ⁿ¹²³@]*$")
_IMPORT_LINE = re.compile(r"(?m)^\s*import\s+[^\n]+\s*$")
_DEFAULT_TACTICS = (
    "exact?",
    "simp",
    "simp_all",
    "omega",
    "norm_num",
    "positivity",
    "linarith",
    "ring",
    "aesop",
    "decide",
)
_FORMAL_NONCACHEABLE_STATUSES = frozenset(
    {
        "BUDGET_EXHAUSTED",
        "BROKER_ERROR",
        "EVALUATOR_ERROR",
        "EVALUATOR_TIMEOUT",
        "INFRASTRUCTURE_ERROR",
        "NETWORK_ERROR",
        "OUT_OF_HORIZON",
        "REMOTE_SETTLEMENT_UNCONFIRMED",
        "REJECTED_OVERLOADED",
        "TASK_CANCELLED",
    }
)
# CPS workers must first receive a real terminal Judge/local-contract result.
# These statuses are candidate-attempt feedback, including bounded resource or
# execution failures; they do not confer score authority. Admission, transport,
# provenance, and other control failures do not satisfy this ordering contract.
_JUDGE_CHECKPOINT_TERMINAL_STATUSES = frozenset(
    {
        "PROVED",
        "COMPILES_WITH_SORRY",
        "VERIFY_FAIL",
        "LOCAL_REJECTED",
        "CHEATING",
        "RESOURCE_LIMIT",
        "EXECUTION_TIMEOUT",
        # Coding Judge candidate-attempt outcomes.  These unlock CPS only
        # after the same candidate/task/job provenance checks as formal
        # terminal feedback below; a status label by itself is insufficient.
        "WA",
        "PE",
        "CE",
        "MLE",
        "TLE",
        "RE",
    }
)
_CHECKPOINT_VALUE_UNSET = object()


def _default_drain_timeout_seconds(evaluator: Any) -> float:
    """Choose a closeout deadline that covers deferred Judge settlement.

    ``LeanEvaluator`` may retain a semaphore permit while a known cancelled
    job is reconciled by a background watcher.  The broker must not give up
    before that watcher has reached its own bounded deadline: doing so turns a
    recoverable cancellation into a spurious ``JudgeBrokerDrainError``.  Keep
    the historical 120-second floor for adapters without this optional
    surface, and add a fixed handoff margin for evaluators that expose it.
    """

    try:
        watcher_timeout = float(
            getattr(evaluator, "deferred_settlement_timeout_seconds", 0.0)
        )
    except (TypeError, ValueError, OverflowError):
        watcher_timeout = 0.0
    if not math.isfinite(watcher_timeout) or watcher_timeout <= 0:
        watcher_timeout = 0.0
    return max(
        _BROKER_DRAIN_TIMEOUT_SECONDS,
        watcher_timeout + _BROKER_DRAIN_SETTLEMENT_MARGIN_SECONDS,
    )


class JudgeBrokerDrainError(RuntimeError):
    """The broker could not finish handler audit and FIFO drain in time."""

    def __init__(self, state: Mapping[str, Any]):
        super().__init__("Judge broker closeout did not drain before its deadline.")
        self.state = {
            "drained": False,
            "active_handlers": max(0, int(state.get("active_handlers", 0))),
            "fifo_depth": max(0, int(state.get("fifo_depth", 0))),
            "remote_unsettled_jobs": max(
                0, int(state.get("remote_unsettled_jobs", 0))
            ),
        }
        pending = _nonnegative_count(state.get("pending_settlement_watchers", 0))
        if pending:
            self.state["pending_settlement_watchers"] = pending


@dataclass
class _CandidateBinding:
    task: Task
    path: Path
    expected_task_contract_sha256: str


@dataclass(frozen=True)
class CandidateSnapshot:
    """Exact UTF-8 candidate bytes frozen before Judge admission."""

    source: str
    sha256: str


@dataclass
class _SessionClaim:
    broker: Any
    actor_id: str
    workdir: Path
    candidates: dict[str, _CandidateBinding]
    deadline_monotonic: float
    deadline_epoch_ms: int
    # Logical solver episode (not a process attempt/restart counter).  Keeping
    # it on the capability claim lets every broker-side Judge/receipt event
    # join the runner's task/actor/episode attribution tuple.
    episode: int = 0
    cps_store: CPSStore | None = None
    communication: str = "none"
    direct_messages_allowed: bool = True
    selection_store: SelectionStore | None = None
    selection_enabled: bool = False
    selection_search: Callable[[Any, str, int], Mapping[str, Any]] | None = None
    roster_path: Path | None = None
    # Route claims are an optional, run-local capability.  The fields live on
    # the session claim (rather than in ambient process state) so recovery or
    # a second solver process cannot widen the capability accidentally.
    route_claims_enabled: bool = False
    route_claim_required: bool = False
    route_claim_ttl_seconds: float = _DEFAULT_ROUTE_CLAIM_TTL_SECONDS
    route_claim_bypass_reason: str | None = None
    # The activity-feedback treatment keeps route leases for lifecycle and
    # write gating, but explicitly disables textual route-key uniqueness.  The
    # Agent's bounded summary is then the peer-visible direction report.
    activity_feedback_enabled: bool = False
    external_dedup_mode: str = "off"
    external_dedup_similarity_threshold: float = 0.78
    external_dedup_min_shared_tokens: int = 3
    route_claim_ids: set[str] = field(default_factory=set, repr=False)
    route_claim_satisfied: bool = False
    on_authoritative_verdict: (
        Callable[[Task, Verdict, CandidateSnapshot], None] | None
    ) = None
    cancel_event: threading.Event | None = None
    revoked_event: threading.Event = field(default_factory=threading.Event, repr=False)
    probe_calls: int = 0
    last_probe_started: float = 0.0
    probe_active: bool = False
    judge_checkpoint_reached: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    cps_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class _ClaimCancelEvent:
    """Event-compatible view over session revocation and task cancellation."""

    def __init__(self, claim: _SessionClaim):
        self.claim = claim

    def is_set(self) -> bool:
        return _claim_cancelled(self.claim)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                delay = min(remaining, 0.02)
            else:
                delay = 0.02
            self.claim.revoked_event.wait(timeout=delay)
        return True

    def cancellation_reason(self) -> str | None:
        if self.claim.revoked_event.is_set():
            return "broker_revoked"
        event = self.claim.cancel_event
        getter = getattr(event, "cancellation_reason", None)
        if callable(getter):
            try:
                reason = getter()
            except Exception:
                reason = None
            if isinstance(reason, str) and reason:
                return reason
        return None


class _BrokerHTTPServer(ThreadingHTTPServer):
    # JudgeBroker performs its own observable, deadline-bounded drain.  Daemon
    # request threads ensure a broken evaluator cannot wedge process exit after
    # that deadline has already been reported as a fatal closeout error.
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def process_request(self, request: Any, client_address: Any) -> None:
        self._broker._handler_started()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._broker._handler_finished()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            # BaseHTTPRequestHandler returns only after the operation response;
            # judge_check appends its audit row before writing that response.
            self._broker._handler_finished()


class JudgeBroker:
    """A loopback-only, token-bound broker shared by all solver sessions."""

    def __init__(
        self,
        evaluator: Any,
        evaluator_gate: threading.BoundedSemaphore,
        *,
        audit_path: Path,
        max_probe_calls_per_session: int = _MAX_PROBE_CALLS_PER_SESSION,
        min_probe_interval_seconds: float = _MIN_PROBE_INTERVAL_SECONDS,
        probe_admission_timeout_seconds: float | None = _PROBE_ADMISSION_TIMEOUT_SECONDS,
        drain_timeout_seconds: float | None = None,
        formal_policy: FormalToolPolicy | None = None,
        formal_audit_path: Path | None = None,
        direct_messages_allowed: bool | None = None,
        selection_store: SelectionStore | None = None,
        selection_enabled: bool = False,
        selection_search: Callable[[Any, str, int], Mapping[str, Any]] | None = None,
        profiler: Any | None = None,
        route_claims_enabled: bool = False,
        route_claim_required: bool | None = None,
        route_claim_ttl_seconds: float = _DEFAULT_ROUTE_CLAIM_TTL_SECONDS,
        activity_feedback_enabled: bool = False,
        external_dedup_mode: str = "off",
        external_dedup_similarity_threshold: float = 0.78,
        external_dedup_min_shared_tokens: int = 3,
        # Compatibility spellings used by early route-claim callers.  Keep
        # these aliases at the broker boundary so one branch can be tested
        # against a sibling branch whose session vocabulary has not landed
        # yet; all internal state uses ``route_claims_enabled``.
        route_claim_enabled: bool | None = None,
        active_roster_enabled: bool | None = None,
    ):
        self.evaluator = evaluator
        self.evaluator_gate = evaluator_gate
        self.audit_path = Path(audit_path)
        self.max_probe_calls_per_session = max(1, int(max_probe_calls_per_session))
        self.min_probe_interval_seconds = max(0.0, float(min_probe_interval_seconds))
        self.probe_admission_timeout_seconds = (
            None
            if probe_admission_timeout_seconds is None
            else max(0.01, float(probe_admission_timeout_seconds))
        )
        if drain_timeout_seconds is None:
            normalized_drain_timeout = _default_drain_timeout_seconds(evaluator)
        else:
            normalized_drain_timeout = float(drain_timeout_seconds)
        if not math.isfinite(normalized_drain_timeout) or normalized_drain_timeout <= 0:
            raise ValueError("broker drain timeout must be finite and positive")
        self.drain_timeout_seconds = normalized_drain_timeout
        self.formal_policy = formal_policy
        if direct_messages_allowed is not None and not isinstance(
            direct_messages_allowed, bool
        ):
            raise ValueError("direct_messages_allowed must be a boolean or None")
        # ``None`` preserves the historical broker surface: every CPS-enabled
        # session can use both shared and direct operations.  Callers can set a
        # broker-wide default while a session remains able to narrow it.
        self.direct_messages_allowed = direct_messages_allowed
        if not isinstance(selection_enabled, bool):
            raise ValueError("selection_enabled must be a boolean")
        if selection_enabled and selection_store is None:
            raise ValueError("selection_enabled requires a selection store")
        self.selection_store = selection_store
        self.selection_enabled = selection_enabled
        if selection_search is not None and not callable(selection_search):
            raise ValueError("selection_search must be callable or None")
        self.selection_search = selection_search
        # Optional diagnostic sink.  It is deliberately duck-typed so the
        # broker remains usable by narrow test/evaluator adapters without
        # importing or depending on the profiling implementation.
        self.profiler = profiler
        try:
            self._profiling_enabled = bool(
                profiler is not None and getattr(profiler, "enabled", False)
            )
        except BaseException:
            self._profiling_enabled = False
        if not isinstance(route_claims_enabled, bool):
            raise ValueError("route_claims_enabled must be a boolean")
        alias_enabled = route_claim_enabled
        if alias_enabled is not None and active_roster_enabled is not None:
            if alias_enabled != active_roster_enabled:
                raise ValueError(
                    "route_claim_enabled and active_roster_enabled contradict each other"
                )
        if alias_enabled is None:
            alias_enabled = active_roster_enabled
        if alias_enabled is not None:
            if not isinstance(alias_enabled, bool):
                raise ValueError("route_claim_enabled must be a boolean or None")
            # The constructor has one canonical boolean with a historical
            # alias.  A true alias is an explicit opt-in for this broker
            # instance; session-scoped aliases below are not allowed to widen
            # the resulting broker capability.
            route_claims_enabled = route_claims_enabled or alias_enabled
        if route_claim_required is not None and not isinstance(
            route_claim_required, bool
        ):
            raise ValueError("route_claim_required must be a boolean or None")
        self.route_claims_enabled = bool(route_claims_enabled or route_claim_required)
        self.route_claim_required = bool(route_claim_required)
        self.route_claim_ttl_seconds = _normalize_route_claim_ttl(
            route_claim_ttl_seconds
        )
        if not isinstance(activity_feedback_enabled, bool):
            raise ValueError("activity_feedback_enabled must be a boolean")
        self.activity_feedback_enabled = activity_feedback_enabled
        normalized_dedup_mode = str(external_dedup_mode or "off").strip().lower()
        if normalized_dedup_mode not in {"off", "advisory", "enforce"}:
            raise ValueError("external_dedup_mode must be off, advisory, or enforce")
        try:
            dedup_threshold = float(external_dedup_similarity_threshold)
        except (TypeError, ValueError) as exc:
            raise ValueError("external_dedup_similarity_threshold must be finite") from exc
        if not math.isfinite(dedup_threshold) or not 0.0 < dedup_threshold <= 1.0:
            raise ValueError("external_dedup_similarity_threshold must be in (0, 1]")
        if isinstance(external_dedup_min_shared_tokens, bool) or not isinstance(
            external_dedup_min_shared_tokens, int
        ) or not 1 <= external_dedup_min_shared_tokens <= 32:
            raise ValueError("external_dedup_min_shared_tokens must be an integer in [1, 32]")
        if normalized_dedup_mode != "off" and not self.route_claims_enabled:
            raise ValueError("external dedup requires route claims")
        self.external_dedup_mode = normalized_dedup_mode
        self.external_dedup_similarity_threshold = dedup_threshold
        self.external_dedup_min_shared_tokens = external_dedup_min_shared_tokens
        self.formal_audit_path = Path(
            formal_audit_path
            if formal_audit_path is not None
            else self.audit_path.with_name("formal_tool_calls.jsonl")
        )
        self._formal_lock = threading.RLock()
        self._formal_counts: dict[tuple[str, str], int] = {}
        self._formal_serials: dict[tuple[str, str], int] = {}
        self._formal_evaluate_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._formal_query_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._claims: dict[str, _SessionClaim] = {}
        self._claims_lock = threading.RLock()
        self._audit_lock = threading.Lock()
        self._admission_condition = threading.Condition()
        self._admission_queue: deque[object] = deque()
        self._handler_condition = threading.Condition()
        self._active_handlers = 0
        self._remote_unsettled_jobs = 0
        self._server: _BrokerHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _profile_event(
        self,
        event: str,
        *,
        claim: _SessionClaim | None = None,
        task_id: str | None = None,
        episode: int | None = None,
        **fields: Any,
    ) -> None:
        if not self._profiling_enabled:
            return
        profiler = self.profiler
        actor_id = claim.actor_id if claim is not None else None
        if episode is None and claim is not None:
            episode = claim.episode
        try:
            profiler.emit(
                event,
                task_id=task_id,
                actor_id=actor_id,
                episode=episode,
                **fields,
            )
        except BaseException:
            # A diagnostic sink must never change broker admission, settlement,
            # or cancellation behavior.
            return

    def _profile_settlement_state(self, event: str, *, state: Mapping[str, Any] | None = None) -> None:
        """Emit a bounded evaluator watcher snapshot for drain diagnosis."""

        if not self._profiling_enabled:
            return
        values: dict[str, Any] = {}
        snapshot = getattr(self.evaluator, "settlement_snapshot", None)
        if callable(snapshot):
            try:
                candidate = snapshot()
                if isinstance(candidate, Mapping):
                    values.update(candidate)
            except BaseException:
                pass
        if state:
            for key in (
                "active_handlers",
                "fifo_depth",
                "remote_unsettled_jobs",
                "pending_settlement_watchers",
            ):
                if key in state:
                    values[key] = state[key]
        self._profile_event(event, **values)

    def start(self) -> "JudgeBroker":
        if self._server is not None:
            return self
        broker = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ContextSwarmBroker/1"
            sys_version = ""

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                broker._handle_http(self)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                broker._send_json(
                    self,
                    405,
                    {"ok": False, "status": "METHOD_NOT_ALLOWED"},
                )

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.touch(exist_ok=True)
        if self.formal_policy is not None and self.formal_policy.enabled:
            self.formal_audit_path.parent.mkdir(parents=True, exist_ok=True)
            self.formal_audit_path.touch(exist_ok=True)
        self._server = _BrokerHTTPServer(("127.0.0.1", 0), Handler)
        self._server._broker = self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="contextswarm-judge-broker",
            daemon=True,
        )
        self._thread.start()
        self._profile_event("judge.broker.start")
        return self

    def close(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Revoke capabilities and drain audited handlers within one deadline."""

        timeout = (
            self.drain_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("broker close timeout must be finite and positive")
        self._profile_event(
            "drain.start",
            timeout_seconds=timeout,
            active_handlers=self.active_handlers,
            fifo_depth=self.fifo_depth,
            pending_settlement_watchers=_nonnegative_count(
                getattr(self.evaluator, "pending_settlement_watchers", 0)
            ),
            remote_unsettled_jobs=self.remote_unsettled_jobs,
        )
        self._profile_settlement_state("drain.sample")
        deadline = time.monotonic() + timeout
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        with self._claims_lock:
            claims = list(self._claims.values())
            for claim in claims:
                claim.revoked_event.set()
            self._claims.clear()
        with self._admission_condition:
            self._admission_condition.notify_all()
        if server is not None:
            server.shutdown()
        state = self._wait_for_drain(deadline)
        if server is not None:
            server.server_close()
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if not state["drained"]:
            self._profile_event("drain.timeout", **state)
            raise JudgeBrokerDrainError(state)
        # A successful close is stronger than a point-in-time observation:
        # serve_forever is stopped, claims are revoked, and no handler or FIFO
        # waiter remains that could re-populate either count.
        final_state = self.drain_state()
        if any(final_state.values()):
            self._profile_event("drain.timeout", **final_state)
            raise JudgeBrokerDrainError(final_state)
        self._profile_event("drain.end", **final_state, drained=True)
        return {"drained": True, **final_state}

    def drain_state(self) -> dict[str, int]:
        """Return the closeout counters consumed by runner health checks."""

        with self._handler_condition:
            active_handlers = self._active_handlers
            broker_unsettled_jobs = self._remote_unsettled_jobs
        evaluator_unsettled_jobs = _nonnegative_count(
            getattr(self.evaluator, "remote_unsettled_jobs", 0)
        )
        pending_watchers = _nonnegative_count(
            getattr(self.evaluator, "pending_settlement_watchers", 0)
        )
        with self._admission_condition:
            fifo_depth = len(self._admission_queue)
        state = {
            "active_handlers": max(0, int(active_handlers)),
            "fifo_depth": max(0, int(fifo_depth)),
            "remote_unsettled_jobs": (
                max(0, int(broker_unsettled_jobs)) + evaluator_unsettled_jobs
            ),
        }
        if pending_watchers:
            state["pending_settlement_watchers"] = pending_watchers
        return state

    @property
    def active_handlers(self) -> int:
        return self.drain_state()["active_handlers"]

    @property
    def fifo_depth(self) -> int:
        return self.drain_state()["fifo_depth"]

    @property
    def remote_unsettled_jobs(self) -> int:
        return self.drain_state()["remote_unsettled_jobs"]

    def _handler_started(self) -> None:
        with self._handler_condition:
            self._active_handlers += 1

    def _handler_finished(self) -> None:
        with self._handler_condition:
            self._active_handlers -= 1
            self._handler_condition.notify_all()

    def _mark_remote_unsettled(self) -> None:
        """Latch one unconfirmed remote job without retaining its identity."""

        # LeanEvaluator latches every unconfirmed cancellation, including
        # direct runner evaluations outside a broker handler.  Narrow test or
        # alternate evaluator adapters may expose only the safe response; use
        # the broker-local counter as their fail-closed fallback.
        if _nonnegative_count(
            getattr(self.evaluator, "remote_unsettled_jobs", 0)
        ) > 0:
            return
        with self._handler_condition:
            self._remote_unsettled_jobs += 1
            self._handler_condition.notify_all()

    def _remote_settlement_unconfirmed(self) -> bool:
        """Return whether any run-local Judge work lacks terminal proof."""

        with self._handler_condition:
            broker_unsettled_jobs = self._remote_unsettled_jobs
        return broker_unsettled_jobs > 0 or _nonnegative_count(
            getattr(self.evaluator, "remote_unsettled_jobs", 0)
        ) > 0

    def _wait_for_drain(self, deadline_monotonic: float) -> dict[str, Any]:
        last_sample_at = 0.0
        last_sample_state: tuple[int, int, int, int] | None = None
        while True:
            state = self.drain_state()
            settlement = getattr(self.evaluator, "settlement_snapshot", None)
            watcher_state: Mapping[str, Any] = {}
            now = 0.0
            if self._profiling_enabled:
                now = time.monotonic()
            if self._profiling_enabled and callable(settlement):
                try:
                    candidate = settlement()
                    if isinstance(candidate, Mapping):
                        watcher_state = candidate
                except BaseException:
                    watcher_state = {}
            sample_key = (
                int(state.get("active_handlers", 0)),
                int(state.get("fifo_depth", 0)),
                int(state.get("remote_unsettled_jobs", 0)),
                int(watcher_state.get("pending_settlement_watchers", 0) or 0),
            )
            if self._profiling_enabled and (
                now - last_sample_at >= 1.0
                or sample_key != last_sample_state
            ):
                sample_fields = dict(state)
                sample_fields.update(dict(watcher_state))
                self._profile_event("drain.sample", **sample_fields)
                last_sample_at = now
                last_sample_state = sample_key
            if (
                state["active_handlers"] == 0
                and state["fifo_depth"] == 0
                and _nonnegative_count(
                    getattr(self.evaluator, "pending_settlement_watchers", 0)
                ) == 0
            ):
                return {
                    "drained": state["remote_unsettled_jobs"] == 0,
                    **state,
                }
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                # A handler can finish on the same tick that the deadline is
                # reached.  The state sampled above is then stale, and
                # returning an unconditional failure turns an already quiet
                # broker into a spurious closeout error.  Re-sample once at
                # the boundary and accept the close if every accounting
                # domain is actually empty.
                final_state = self.drain_state()
                pending_watchers = _nonnegative_count(
                    getattr(self.evaluator, "pending_settlement_watchers", 0)
                )
                drained = (
                    final_state["active_handlers"] == 0
                    and final_state["fifo_depth"] == 0
                    and pending_watchers == 0
                    and final_state["remote_unsettled_jobs"] == 0
                )
                if pending_watchers:
                    final_state["pending_settlement_watchers"] = pending_watchers
                return {"drained": drained, **final_state}
            with self._handler_condition:
                self._handler_condition.wait(timeout=min(remaining, 0.05))

    def public_policy(self) -> dict[str, Any]:
        policy: dict[str, Any] = {
            "transport": "runner_owned_loopback_capability",
            "response_profile": LEAN_PROBE_RESPONSE_PROFILE,
            "max_probe_calls_per_session": self.max_probe_calls_per_session,
            "min_probe_interval_seconds": self.min_probe_interval_seconds,
            "probe_admission_deadline": "session_remaining_horizon",
            "probe_admission_timeout_seconds": self.probe_admission_timeout_seconds,
            "candidate_selection": "runner_bound_task_candidate_filename",
            "allowed_candidate_filenames": sorted(_ALLOWED_CANDIDATE_FILENAMES),
            "candidate_submission": "immutable_source_snapshot",
            "shares_final_evaluator_gate": True,
            "submitted_job_cancellation": "delete_on_probe_cancel_or_deadline",
            "closeout_requires_active_handlers": 0,
            "closeout_requires_fifo_depth": 0,
            "closeout_requires_remote_unsettled_jobs": 0,
            "drain_timeout_seconds": self.drain_timeout_seconds,
            "direct_messages_allowed": (
                self.direct_messages_allowed
                if self.direct_messages_allowed is not None
                else "legacy"
            ),
            "selection_feedback": {
                "enabled": self.selection_enabled,
                "origin": "worker_explicit",
                "feedback_kinds": sorted(CANONICAL_FEEDBACK_KINDS),
            },
            "selection_search": self.selection_search is not None,
            "active_roster": {
                "enabled": self.route_claims_enabled,
                "source": "cps_runner_owned" if self.route_claims_enabled else "legacy_projection",
            },
            "route_claims": {
                "enabled": self.route_claims_enabled,
                "required": self.route_claim_required,
                "ttl_seconds": self.route_claim_ttl_seconds,
                "activity_feedback_enabled": self.activity_feedback_enabled,
                "route_key_semantics": (
                    "opaque" if self.activity_feedback_enabled else "unique"
                ),
                "pre_judge_operations": sorted(_PRE_JUDGE_ROUTE_OPERATIONS),
                "post_judge_operations": sorted(
                    _ROUTE_CLAIM_OPERATIONS - _PRE_JUDGE_ROUTE_OPERATIONS
                ),
                "failure_mode": "fail_open_with_explicit_bypass_reason",
                "external_dedup": {
                    "mode": self.external_dedup_mode,
                    "similarity_threshold": self.external_dedup_similarity_threshold,
                    "min_shared_tokens": self.external_dedup_min_shared_tokens,
                    "decision_owner": "runner_controller",
                    "unknown_action": "continue",
                },
            },
        }
        formal = self.formal_policy
        policy["formal_tools"] = (
            {
                "enabled": True,
                "surface_version": formal.surface_version,
                "transport": "same_session_loopback_capability",
                "evaluate_authority": "diagnostic_only",
                "quota_scope": "task_across_all_sessions",
                "evaluate_calls_per_task": formal.evaluate_calls_per_task,
                "evaluate_backend_jobs_per_task": formal.evaluate_backend_jobs_per_task,
                "query_calls_per_task": formal.query_calls_per_task,
                "query_backend_probes_per_task": formal.query_backend_probes_per_task,
                "max_candidate_bytes": formal.max_candidate_bytes,
                "declaration_index": formal.declaration_index.info.public_dict(),
            }
            if formal is not None and formal.enabled
            else {"enabled": False}
        )
        return policy

    def formal_summary(self) -> dict[str, Any]:
        """Return public run-global counters after broker capabilities are silent."""

        with self._formal_lock:
            counts: dict[str, dict[str, int]] = {}
            for (task_id, counter), value in sorted(self._formal_counts.items()):
                counts.setdefault(task_id, {})[counter] = max(0, int(value))
        return {
            "enabled": bool(self.formal_policy and self.formal_policy.enabled),
            "quota_scope": "task_across_all_sessions",
            "tasks": counts,
        }

    @contextmanager
    def session(
        self,
        *,
        actor_id: str,
        episode: int = 0,
        workdir: Path,
        candidates: Mapping[str, tuple[Task, Path]],
        deadline_monotonic: float,
        cps_store: CPSStore | None = None,
        communication: str = "none",
        direct_messages_allowed: bool | None = None,
        selection_store: SelectionStore | None = None,
        selection_enabled: bool | None = None,
        selection_search: Callable[[Any, str, int], Mapping[str, Any]] | None = None,
        roster_path: Path | None = None,
        route_claims_enabled: bool | None = None,
        route_claim_required: bool | None = None,
        route_claim_ttl_seconds: float | None = None,
        activity_feedback_enabled: bool | None = None,
        external_dedup_mode: str | None = None,
        external_dedup_similarity_threshold: float | None = None,
        external_dedup_min_shared_tokens: int | None = None,
        route_claim_bypass_reason: str | None = None,
        route_claim_enabled: bool | None = None,
        active_roster_enabled: bool | None = None,
        on_authoritative_verdict: (
            Callable[[Task, Verdict, CandidateSnapshot], None] | None
        ) = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[dict[str, str]]:
        """Issue and later revoke a capability bound to exact candidates."""

        if self._server is None:
            raise RuntimeError("Judge broker has not been started")
        if isinstance(episode, bool) or not isinstance(episode, int) or episode < 0:
            raise ValueError("broker session episode must be a non-negative integer")
        resolved_workdir = Path(workdir).resolve()
        bindings: dict[str, _CandidateBinding] = {}
        expected_contract = getattr(
            self.evaluator,
            "expected_task_contract_sha256",
            None,
        )
        if not callable(expected_contract):
            raise ValueError(
                "evaluator must expose expected_task_contract_sha256(task)"
            )
        for task_id, (task, candidate_path) in candidates.items():
            candidate_filename = str(task.candidate_filename)
            if candidate_filename not in _ALLOWED_CANDIDATE_FILENAMES:
                raise ValueError("task declares an unsupported candidate filename")
            candidate = Path(candidate_path).resolve()
            if candidate.name != candidate_filename or not _is_relative_to(
                candidate, resolved_workdir
            ):
                raise ValueError(
                    "broker candidate must match the task-declared filename "
                    "inside the assigned workdir"
                )
            if str(task_id) != task.slug:
                raise ValueError("broker task key must match the bound task slug")
            contract_sha256 = _safe_hash(expected_contract(task))
            if contract_sha256 is None:
                raise ValueError("evaluator returned an invalid task-contract identity")
            bindings[task.slug] = _CandidateBinding(
                task=task,
                path=candidate,
                expected_task_contract_sha256=contract_sha256,
            )
        if not bindings:
            raise ValueError("broker session requires at least one task candidate")

        requested_communication = str(communication or "none").strip().lower()
        normalized_communication = requested_communication
        if normalized_communication == "simple":
            normalized_communication = "blackboard"
        if normalized_communication not in {"none", "blackboard", "direct", "hybrid"}:
            raise ValueError("unsupported broker communication policy")
        if normalized_communication != "none" and (cps_store is None or len(bindings) != 1):
            raise ValueError("CPS broker sessions require one task and a CPS store")
        if direct_messages_allowed is not None and not isinstance(
            direct_messages_allowed, bool
        ):
            raise ValueError("direct_messages_allowed must be a boolean or None")
        # Either scope may narrow the capability, but a session cannot widen a
        # broker-wide denial.  With neither scope specified this remains True,
        # exactly preserving the pre-gate CPS behavior.
        effective_direct_messages_allowed = not (
            self.direct_messages_allowed is False
            or direct_messages_allowed is False
        )
        if selection_enabled is not None and not isinstance(selection_enabled, bool):
            raise ValueError("selection_enabled must be a boolean or None")
        effective_selection_enabled = (
            self.selection_enabled
            if selection_enabled is None
            else selection_enabled
        )
        effective_selection_store = (
            selection_store if selection_store is not None else self.selection_store
        )
        if selection_search is not None and not callable(selection_search):
            raise ValueError("selection_search must be callable or None")
        effective_selection_search = (
            selection_search if selection_search is not None else self.selection_search
        )
        if effective_selection_enabled and effective_selection_store is None:
            raise ValueError("selection_enabled requires a selection store")
        # Session scopes may narrow a broker-wide route capability, but may not
        # widen it (or turn off a manifest-required gate).  Aliases are accepted
        # only at this boundary; the worker never gets to choose a broader route
        # surface through payload fields.
        route_alias = route_claim_enabled
        if route_alias is not None and active_roster_enabled is not None:
            if route_alias != active_roster_enabled:
                raise ValueError(
                    "route_claim_enabled and active_roster_enabled contradict each other"
                )
        if route_alias is None:
            route_alias = active_roster_enabled
        if route_alias is not None and not isinstance(route_alias, bool):
            raise ValueError("route_claim_enabled must be a boolean or None")
        if route_claims_enabled is not None and not isinstance(
            route_claims_enabled, bool
        ):
            raise ValueError("route_claims_enabled must be a boolean or None")
        requested_route_enabled = route_claims_enabled
        if route_alias is not None:
            if (
                requested_route_enabled is not None
                and requested_route_enabled != route_alias
            ):
                raise ValueError(
                    "route_claims_enabled and route_claim_enabled contradict each other"
                )
            requested_route_enabled = route_alias
        if requested_route_enabled is True and not self.route_claims_enabled:
            raise ValueError("session cannot widen the broker route capability")
        if requested_route_enabled is False and self.route_claim_required:
            raise ValueError("session cannot disable the required route gate")
        effective_route_enabled = self.route_claims_enabled
        if requested_route_enabled is False:
            effective_route_enabled = False
        if route_claim_required is not None and not isinstance(
            route_claim_required, bool
        ):
            raise ValueError("route_claim_required must be a boolean or None")
        if route_claim_required is True and not self.route_claims_enabled:
            raise ValueError("session cannot widen the broker route capability")
        if route_claim_required is False and self.route_claim_required:
            raise ValueError("session cannot disable the required route gate")
        if route_alias is False and self.route_claim_required:
            raise ValueError("session cannot disable the required route gate")
        # A session may request a stricter gate when the broker already exposes
        # route coordination, but it can never relax a manifest-required gate.
        effective_route_required = self.route_claim_required or (
            route_claim_required is True
        )
        if effective_route_required:
            effective_route_enabled = True
        if effective_route_enabled and (
            cps_store is None
            or len(bindings) != 1
            or normalized_communication == "none"
        ):
            # Route coordination is a real CPS treatment surface.  A direct
            # caller must not manufacture a session whose first claim is
            # guaranteed to fail open merely because it omitted the store or
            # communication capability; normal manifests already enforce the
            # same invariant in config.py.
            raise ValueError(
                "route-enabled broker sessions require one task and a CPS store "
                "with a communication surface"
            )
        if route_claim_bypass_reason is not None:
            normalized_bypass_reason = _bounded_route_bypass_reason(
                route_claim_bypass_reason
            )
        else:
            normalized_bypass_reason = None
        if route_claim_ttl_seconds is None:
            effective_route_ttl = self.route_claim_ttl_seconds
        else:
            effective_route_ttl = _normalize_route_claim_ttl(route_claim_ttl_seconds)
        if activity_feedback_enabled is not None and not isinstance(
            activity_feedback_enabled, bool
        ):
            raise ValueError("activity_feedback_enabled must be a boolean or None")
        effective_activity_feedback = (
            self.activity_feedback_enabled
            if activity_feedback_enabled is None
            else activity_feedback_enabled
        )
        if effective_activity_feedback and not effective_route_enabled:
            raise ValueError(
                "activity feedback requires an active route capability"
            )
        effective_dedup_mode = self.external_dedup_mode if external_dedup_mode is None else str(external_dedup_mode).strip().lower()
        if effective_dedup_mode not in {"off", "advisory", "enforce"}:
            raise ValueError("external_dedup_mode must be off, advisory, or enforce")
        if effective_dedup_mode != "off" and not effective_route_enabled:
            raise ValueError("external dedup requires an active route capability")
        effective_dedup_threshold = (
            self.external_dedup_similarity_threshold
            if external_dedup_similarity_threshold is None
            else float(external_dedup_similarity_threshold)
        )
        if not math.isfinite(effective_dedup_threshold) or not 0.0 < effective_dedup_threshold <= 1.0:
            raise ValueError("external_dedup_similarity_threshold must be in (0, 1]")
        effective_dedup_min_shared = (
            self.external_dedup_min_shared_tokens
            if external_dedup_min_shared_tokens is None
            else external_dedup_min_shared_tokens
        )
        if isinstance(effective_dedup_min_shared, bool) or not isinstance(effective_dedup_min_shared, int) or not 1 <= effective_dedup_min_shared <= 32:
            raise ValueError("external_dedup_min_shared_tokens must be an integer in [1, 32]")
        if isinstance(episode, bool) or not isinstance(episode, int) or episode < 0:
            raise ValueError("episode must be a non-negative integer")
        if effective_selection_enabled:
            # Selection experiments compare ranking only.  Accepting the
            # direct/hybrid surfaces here would reintroduce a second treatment
            # path even if the caller forgot to register the matching tool
            # gate.  Check the requested spelling so the legacy ``simple``
            # alias cannot silently enter a registered selection arm either.
            if requested_communication != "blackboard":
                raise ValueError(
                    "selection-enabled broker sessions require "
                    "communication = blackboard"
                )
            # Server-side authorization is authoritative.  A stale or
            # contradictory caller flag must never widen a selection arm's
            # direct-message capability.
            effective_direct_messages_allowed = False

        normalized_deadline = float(deadline_monotonic)
        if not math.isfinite(normalized_deadline):
            raise ValueError("broker session deadline must be finite")
        remaining_seconds = max(0.0, normalized_deadline - time.monotonic())
        deadline_epoch_ms = int((time.time() + remaining_seconds) * 1_000)
        token = secrets.token_urlsafe(32)
        claim = _SessionClaim(
            broker=self,
            actor_id=str(actor_id),
            episode=episode,
            workdir=resolved_workdir,
            candidates=bindings,
            deadline_monotonic=normalized_deadline,
            deadline_epoch_ms=deadline_epoch_ms,
            cps_store=cps_store,
            communication=normalized_communication,
            direct_messages_allowed=effective_direct_messages_allowed,
            selection_store=effective_selection_store,
            selection_enabled=effective_selection_enabled,
            selection_search=effective_selection_search,
            roster_path=Path(roster_path).resolve() if roster_path is not None else None,
            route_claims_enabled=bool(effective_route_enabled),
            route_claim_required=bool(effective_route_required),
            route_claim_ttl_seconds=effective_route_ttl,
            route_claim_bypass_reason=normalized_bypass_reason,
            activity_feedback_enabled=bool(effective_activity_feedback),
            external_dedup_mode=effective_dedup_mode,
            external_dedup_similarity_threshold=effective_dedup_threshold,
            external_dedup_min_shared_tokens=effective_dedup_min_shared,
            on_authoritative_verdict=on_authoritative_verdict,
            cancel_event=cancel_event,
        )
        with self._claims_lock:
            self._claims[token] = claim
        self._profile_event(
            "judge.session.start",
            claim=claim,
            candidate_count=len(bindings),
            communication=normalized_communication,
            selection_enabled=effective_selection_enabled,
        )
        host, port = self._server.server_address[:2]
        try:
            yield {
                "CONTEXTSWARM_JUDGE_URL": f"http://{host}:{port}/{token}",
                "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": str(deadline_epoch_ms),
            }
        finally:
            self._profile_event(
                "judge.session.end",
                claim=claim,
                candidate_count=len(bindings),
                probe_calls=claim.probe_calls,
            )
            claim.revoked_event.set()
            with self._claims_lock:
                self._claims.pop(token, None)
            with self._admission_condition:
                self._admission_condition.notify_all()
            # A handler may already have resolved the claim before it was
            # removed from the token map.  Drain its serialized CPS operation
            # so this context cannot return while a stale capability can still
            # commit runner-owned communication state.
            with claim.cps_lock:
                pass

    def _handle_http(self, handler: BaseHTTPRequestHandler) -> None:
        parts = [part for part in handler.path.split("?")[0].split("/") if part]
        if len(parts) != 2:
            self._send_json(handler, 404, {"ok": False, "status": "NOT_FOUND"})
            return
        token, operation = parts
        with self._claims_lock:
            claim = self._claims.get(token)
        if claim is None:
            self._send_json(handler, 403, {"ok": False, "status": "INVALID_CAPABILITY"})
            return
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > _MAX_REQUEST_BYTES:
            self._send_json(handler, 413, {"ok": False, "status": "INVALID_REQUEST"})
            return
        try:
            raw = handler.rfile.read(length)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, json.JSONDecodeError):
            self._send_json(handler, 400, {"ok": False, "status": "INVALID_JSON"})
            return
        if not isinstance(payload, Mapping):
            self._send_json(handler, 400, {"ok": False, "status": "INVALID_REQUEST"})
            return

        try:
            if operation == "judge_check":
                result = self._judge_check(claim, payload)
            elif operation == "evaluate_local":
                result = self._evaluate_local(claim, payload)
            elif operation == "formal_query":
                result = self._formal_query(claim, payload)
            elif operation.startswith("cps_"):
                result = self._cps_operation(claim, operation, payload)
            else:
                self._send_json(handler, 404, {"ok": False, "status": "NOT_FOUND"})
                return
        except Exception:
            # This is the process boundary for a solver capability.  Keep the
            # failure stable and auditable without reflecting exception text,
            # which may contain a host path or private transport detail.
            result = _control_result(
                "BROKER_ERROR",
                "The controlled experiment broker failed this capability call.",
                retryable=False,
            )
            if operation == "judge_check":
                audit_task = (
                    next(iter(claim.candidates))
                    if len(claim.candidates) == 1
                    else "__invalid__"
                )
                self._audit(claim, audit_task, result, accepted=False)
            elif operation in {"evaluate_local", "formal_query"}:
                self._formal_audit(
                    claim,
                    operation,
                    "__invalid__",
                    result,
                    accepted=False,
                )
        self._send_json(handler, 200, result)

    def _judge_check(
        self, claim: _SessionClaim, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        # Start the capability clock before validation so even a rejected
        # request gets a terminal ``judge.receipt`` row.  This is intentionally
        # separate from the evaluator execution clock below: validation,
        # admission, execution and audit are distinct stages in the profile.
        request_started = time.monotonic() if self._profiling_enabled else 0.0
        if set(payload) - {"task_id"}:
            result = _control_result(
                "INVALID_REQUEST",
                "judge_check accepts only the runner-bound task selection.",
                retryable=False,
            )
            audit_task = (
                next(iter(claim.candidates))
                if len(claim.candidates) == 1
                else "__invalid__"
            )
            return self._finish_judge_check(
                claim,
                audit_task,
                result,
                accepted=False,
                started=request_started,
                gate_wait_started=request_started,
            )
        try:
            task_id = _select_task_id(claim, payload.get("task_id"))
        except ValueError as exc:
            result = _control_result(
                "INVALID_TASK_SELECTION",
                _safe_error(exc),
                retryable=False,
            )
            return self._finish_judge_check(
                claim,
                "__invalid__",
                result,
                accepted=False,
                started=request_started,
                gate_wait_started=request_started,
            )
        now = time.monotonic()
        with claim.lock:
            if claim.probe_active:
                result = _control_result(
                    "SESSION_PROBE_IN_FLIGHT",
                    "Only one judge_check may be in flight for this solver session.",
                    retryable=True,
                )
                return self._finish_judge_check(
                    claim,
                    task_id,
                    result,
                    accepted=False,
                    started=request_started,
                    gate_wait_started=request_started,
                )
            if claim.probe_calls >= self.max_probe_calls_per_session:
                result = _control_result(
                    "SESSION_PROBE_BUDGET_EXHAUSTED",
                    "The controlled Judge-call budget for this solver session is exhausted.",
                    retryable=False,
                )
                return self._finish_judge_check(
                    claim,
                    task_id,
                    result,
                    accepted=False,
                    started=request_started,
                    gate_wait_started=request_started,
                )
            cooldown = self.min_probe_interval_seconds - (now - claim.last_probe_started)
            if claim.last_probe_started and cooldown > 0:
                result = _control_result(
                    "SESSION_PROBE_COOLDOWN",
                    "Wait before submitting another candidate to judge_check.",
                    retryable=True,
                    retry_after_seconds=round(cooldown, 3),
                )
                return self._finish_judge_check(
                    claim,
                    task_id,
                    result,
                    accepted=False,
                    started=request_started,
                    gate_wait_started=request_started,
                )
            if now >= claim.deadline_monotonic:
                result = _control_result(
                    "OUT_OF_HORIZON",
                    "The experiment horizon has elapsed.",
                    retryable=False,
                )
                return self._finish_judge_check(
                    claim,
                    task_id,
                    result,
                    accepted=False,
                    started=request_started,
                    gate_wait_started=request_started,
                )
            if _claim_cancelled(claim):
                result = _control_result(
                    "TASK_CANCELLED",
                    "This solver task no longer accepts Judge work.",
                    retryable=False,
                )
                return self._finish_judge_check(
                    claim,
                    task_id,
                    result,
                    accepted=False,
                    started=request_started,
                    gate_wait_started=request_started,
                )
            if self._remote_settlement_unconfirmed():
                result = _remote_settlement_control_result()
                return self._finish_judge_check(
                    claim,
                    task_id,
                    result,
                    accepted=False,
                    started=request_started,
                    gate_wait_started=request_started,
                )
            claim.probe_active = True

        started = time.monotonic()
        gate_wait_started = started
        snapshot_seconds = 0.0
        evaluator_seconds = 0.0
        audit_seconds = 0.0
        acquired = False
        retain_evaluator_gate = False
        accepted = False
        call_index: int | None = None
        result: dict[str, Any]
        binding = claim.candidates[task_id]
        snapshot: CandidateSnapshot | None = None
        authoritative_verdict: Verdict | None = None
        evaluator_unsettled_before = 0
        evaluator_call_started = False
        # Keep the evaluator's raw provenance beside the sanitized public
        # result.  In particular, ``sanitize_worker_identifier`` deliberately
        # maps malformed values to ``None``; that must not turn a malformed
        # remote id into a valid LOCAL_REJECTED checkpoint.
        raw_judge_job_id: Any = _CHECKPOINT_VALUE_UNSET
        try:
            # Freeze exactly what this capability call submits before waiting
            # for the shared Judge gate.  The worker may continue editing its
            # candidate file while queued, but neither the request nor its audit
            # hash can then drift to a different file state.
            snapshot_started = (
                time.monotonic() if self._profiling_enabled else 0.0
            )
            if self._profiling_enabled:
                # Candidate freezing is a first-class broker stage.  Emit its
                # start before touching the filesystem so audit tooling can
                # always pair the terminal event, including snapshot errors.
                self._profile_event(
                    "judge.snapshot.start",
                    claim=claim,
                    task_id=task_id,
                    operation="candidate_snapshot",
                    phase="snapshot",
                )
            try:
                snapshot = _candidate_snapshot(
                    binding.path,
                    trusted_root=claim.workdir,
                    max_bytes=(
                        self.formal_policy.max_candidate_bytes
                        if self.formal_policy is not None
                        else DEFAULT_MAX_CANDIDATE_BYTES
                    ),
                )
            except (OSError, UnicodeError):
                result = _control_result(
                    "CANDIDATE_SNAPSHOT_ERROR",
                    "The runner could not freeze the task candidate for Judge submission.",
                    retryable=True,
                )
                return self._finish_judge_check(
                    claim,
                    task_id,
                    result,
                    accepted=False,
                    started=started,
                    gate_wait_started=gate_wait_started,
                    clear_probe_active=True,
                )
            finally:
                if self._profiling_enabled:
                    snapshot_seconds = max(
                        0.0, time.monotonic() - snapshot_started
                    )
                    self._profile_event(
                        "judge.snapshot.end",
                        claim=claim,
                        task_id=task_id,
                        operation="candidate_snapshot",
                        phase="snapshot",
                        elapsed_seconds=snapshot_seconds,
                        status="ok" if snapshot is not None else "error",
                    )

            self._profile_event(
                "judge.submitted",
                claim=claim,
                task_id=task_id,
                candidate_sha256=snapshot.sha256,
            )

            admission_deadline = claim.deadline_monotonic
            capped_admission = False
            if self.probe_admission_timeout_seconds is not None:
                admission_deadline = min(
                    admission_deadline,
                    gate_wait_started + self.probe_admission_timeout_seconds,
                )
                capped_admission = admission_deadline < claim.deadline_monotonic
            try:
                acquired = self._acquire_evaluator_gate(
                    admission_deadline,
                    claim=claim,
                    task_id=task_id,
                )
            except Exception:
                result = _control_result(
                    "JUDGE_ADMISSION_ERROR",
                    "The controlled Judge admission queue failed.",
                    retryable=True,
                )
                return self._finish_judge_check(
                    claim,
                    task_id,
                    result,
                    accepted=False,
                    started=started,
                    gate_wait_started=gate_wait_started,
                    candidate_sha256=snapshot.sha256,
                    clear_probe_active=True,
                )
            gate_wait = time.monotonic() - gate_wait_started
            if not acquired:
                if self._remote_settlement_unconfirmed():
                    result = _remote_settlement_control_result()
                elif _claim_cancelled(claim):
                    result = _control_result(
                        "TASK_CANCELLED",
                        "This solver task was cancelled before Judge admission.",
                        retryable=False,
                    )
                elif capped_admission and time.monotonic() < claim.deadline_monotonic:
                    result = _control_result(
                        "JUDGE_ADMISSION_TIMEOUT",
                        "The controlled Judge remained busy until the admission deadline.",
                        retryable=True,
                        retry_after_seconds=max(
                            1.0, self.min_probe_interval_seconds
                        ),
                    )
                else:
                    result = _control_result(
                        "OUT_OF_HORIZON",
                        "The experiment horizon elapsed while waiting for Judge admission.",
                        retryable=False,
                    )
            elif self._remote_settlement_unconfirmed():
                result = _remote_settlement_control_result()
            elif _claim_cancelled(claim):
                result = _control_result(
                    "TASK_CANCELLED",
                    "This solver task was cancelled during Judge admission.",
                    retryable=False,
                )
            elif time.monotonic() >= claim.deadline_monotonic:
                result = _control_result(
                    "OUT_OF_HORIZON",
                    "The experiment horizon elapsed during Judge admission.",
                    retryable=False,
                )
            else:
                admitted_at = time.monotonic()
                with claim.lock:
                    # The session permits only one in-flight probe, but keep
                    # the budget check adjacent to the actual admission so the
                    # quota accounting contract remains self-contained.
                    if claim.probe_calls >= self.max_probe_calls_per_session:
                        result = _control_result(
                            "SESSION_PROBE_BUDGET_EXHAUSTED",
                            "The controlled Judge-call budget for this solver session is exhausted.",
                            retryable=False,
                        )
                    else:
                        claim.probe_calls += 1
                        claim.last_probe_started = admitted_at
                        call_index = claim.probe_calls
                        accepted = True
                if not accepted:
                    return self._finish_judge_check(
                        claim,
                        task_id,
                        result,
                        accepted=False,
                        started=started,
                        gate_wait_started=gate_wait_started,
                        gate_wait_seconds=gate_wait,
                        candidate_sha256=snapshot.sha256,
                        clear_probe_active=True,
                    )
                probe_source = getattr(self.evaluator, "probe_source", None)
                probe = getattr(self.evaluator, "probe", None)
                if callable(probe_source):
                    evaluator_call = probe_source
                    candidate_argument: Any = snapshot.source
                elif callable(probe):
                    evaluator_call = probe
                    candidate_argument = binding.path
                else:
                    evaluator_call = self.evaluator.evaluate
                    candidate_argument = binding.path
                evaluator_unsettled_before = _nonnegative_count(
                    getattr(self.evaluator, "remote_unsettled_jobs", 0)
                )
                evaluator_kwargs: dict[str, Any] = {
                    "deadline_monotonic": claim.deadline_monotonic,
                }
                # A known peer-cancelled job may outlive the foreground grace
                # period.  Keep this gate permit retained and release it only
                # after the evaluator's bounded watcher receives a terminal
                # receipt.  Evaluators without this optional hook retain the
                # legacy fail-closed behavior.
                if _accepts_settlement_callback(evaluator_call):
                    evaluator_kwargs["settlement_callback"] = self._release_evaluator_gate
                if _accepts_cancel_event(evaluator_call):
                    evaluator_kwargs["cancel_event"] = _ClaimCancelEvent(claim)
                evaluator_call_started = True
                evaluator_started_at = time.monotonic()
                self._profile_event(
                    "judge.execute.start",
                    claim=claim,
                    task_id=task_id,
                    call_index=call_index,
                )
                try:
                    verdict: Verdict = evaluator_call(
                        binding.task,
                        candidate_argument,
                        **evaluator_kwargs,
                    )
                finally:
                    evaluator_seconds = max(
                        0.0, time.monotonic() - evaluator_started_at
                    )
                    self._profile_event(
                        "judge.execute.end",
                        claim=claim,
                        task_id=task_id,
                        call_index=call_index,
                        elapsed_seconds=evaluator_seconds,
                    )
                raw_judge_job_id = verdict.judge_job_id
                verdict_status = _safe_verdict_status(verdict.status)
                safe_job_id = sanitize_worker_identifier(verdict.judge_job_id)
                safe_response = safe_worker_response(verdict.response)
                evaluator_unsettled_after = _nonnegative_count(
                    getattr(self.evaluator, "remote_unsettled_jobs", 0)
                )
                call_unsettled = (
                    evaluator_unsettled_after > 0
                    or _has_unsettled_remote_work(
                        verdict_status,
                        safe_response,
                    )
                )
                deferred_remote = _has_deferred_remote_work(
                    verdict_status, safe_response
                )
                if call_unsettled or deferred_remote:
                    # Both unresolved classes retain the permit.  A deferred
                    # known-job cancellation is released by its watcher;
                    # an unknown job remains permanently fail-closed.
                    retain_evaluator_gate = True
                if call_unsettled and not deferred_remote:
                    # The evaluator has attempted cancellation but cannot
                    # prove the remote job terminal.  Permanently consume this
                    # process-local permit and latch a non-sensitive count;
                    # releasing it could exceed the experiment's Judge
                    # concurrency contract while the remote job still runs.
                    retain_evaluator_gate = True
                    self._mark_remote_unsettled()
                result = {
                    "ok": verdict_status
                    not in {
                        "EVALUATOR_ERROR",
                        "EVALUATOR_TIMEOUT",
                        "REJECTED_OVERLOADED",
                        "NETWORK_ERROR",
                        "CANCELLED",
                        "TASK_CANCELLED",
                    },
                    "accepted": True,
                    "call_index": call_index,
                    "task_id": task_id,
                    "status": verdict_status,
                    "proved": _safe_score(verdict.score) >= 1.0,
                    "score": _safe_score(verdict.score),
                    "elapsed_seconds": round(
                        max(0.0, _safe_finite_float(verdict.elapsed_seconds)), 6
                    ),
                    "response": safe_response,
                    "error": (
                        sanitize_worker_text(verdict.error) if verdict.error else None
                    ),
                    "candidate_sha256": snapshot.sha256,
                    "task_contract_sha256": _safe_hash(
                        verdict.task_contract_sha256
                    ),
                    "judge_job_id": safe_job_id,
                    "cache_reused": verdict.cache_reused is True,
                    "retryable": verdict_status
                    in {
                        "EVALUATOR_ERROR",
                        "EVALUATOR_TIMEOUT",
                        "REJECTED_OVERLOADED",
                        "NETWORK_ERROR",
                    },
                }
                if call_unsettled and not deferred_remote:
                    result.update(
                        {
                            "ok": False,
                            "status": "REMOTE_SETTLEMENT_UNCONFIRMED",
                            "proved": False,
                            "score": 0.0,
                            "retryable": False,
                        }
                    )
                    safe_response["remote_settlement_unconfirmed"] = True
                elif deferred_remote:
                    result.update(
                        {
                            "ok": False,
                            "status": "TASK_CANCELLED",
                            "proved": False,
                            "score": 0.0,
                            "retryable": False,
                        }
                    )
                    safe_response["settlement_deferred"] = True
                proof_claimed = verdict_status == "PROVED" or _safe_score(verdict.score) >= 1.0
                if call_unsettled or deferred_remote:
                    authoritative_verdict = None
                elif proof_claimed and time.monotonic() >= claim.deadline_monotonic:
                    result.update(
                        {
                            "ok": False,
                            "status": "OUT_OF_HORIZON",
                            "proved": False,
                            "score": 0.0,
                            "error": None,
                            "retryable": False,
                        }
                    )
                elif proof_claimed and _is_authoritative_proof(
                    task=binding.task,
                    verdict=verdict,
                    snapshot=snapshot,
                    expected_task_contract_sha256=(
                        binding.expected_task_contract_sha256
                    ),
                    allow_mock_provenance=(
                        getattr(self.evaluator, "is_mock_evaluator", False) is True
                    ),
                ):
                    authoritative_verdict = Verdict(
                        task_id=binding.task.slug,
                        status="PROVED",
                        score=_safe_score(verdict.score),
                        elapsed_seconds=max(
                            0.0, _safe_finite_float(verdict.elapsed_seconds)
                        ),
                        response=safe_response,
                        error=(
                            sanitize_worker_text(verdict.error)
                            if verdict.error
                            else None
                        ),
                        candidate_sha256=snapshot.sha256,
                        task_contract_sha256=_safe_hash(
                            verdict.task_contract_sha256
                        ),
                        judge_job_id=safe_job_id,
                        cache_reused=verdict.cache_reused is True,
                    )
                elif proof_claimed:
                    result.update(
                        {
                            "ok": False,
                            "status": "PROVENANCE_INVALID",
                            "proved": False,
                            "score": 0.0,
                            "response": {},
                            "error": "The Judge result was not bound to the submitted candidate and task contract.",
                            "retryable": False,
                        }
                    )
        except Exception as exc:  # keep one failed probe from killing the broker thread
            evaluator_unsettled_after = _nonnegative_count(
                getattr(self.evaluator, "remote_unsettled_jobs", 0)
            )
            if (
                evaluator_call_started
                and evaluator_unsettled_after > evaluator_unsettled_before
            ):
                retain_evaluator_gate = True
                self._mark_remote_unsettled()
                result = _remote_settlement_control_result()
                result.update(
                    {
                        "accepted": accepted,
                        "call_index": call_index,
                        "task_id": task_id,
                    }
                )
            else:
                result = {
                    "ok": False,
                    "accepted": accepted,
                    "call_index": call_index,
                    "task_id": task_id,
                    "status": "EVALUATOR_ERROR",
                    "proved": False,
                    "score": 0.0,
                    "response": {},
                    "error": sanitize_worker_text(exc),
                    "retryable": True,
                }
        finally:
            if acquired and not retain_evaluator_gate:
                self._release_evaluator_gate()
            with claim.lock:
                claim.probe_active = False

        # The callback runs only after the evaluator gate and session lock are
        # released.  A proof is not exposed to the solver unless the runner
        # has first durably admitted that exact frozen snapshot.
        callback = claim.on_authoritative_verdict
        if (
            callback is not None
            and authoritative_verdict is not None
            and snapshot is not None
        ):
            try:
                callback(binding.task, authoritative_verdict, snapshot)
            except Exception:
                result = {
                    "ok": False,
                    "accepted": accepted,
                    "call_index": call_index,
                    "task_id": task_id,
                    "status": "BROKER_ERROR",
                    "proved": False,
                    "score": 0.0,
                    "response": {},
                    "error": "The runner could not admit the authoritative Judge result.",
                    "retryable": False,
                }

        audit_gate_wait_seconds = (
            time.monotonic() - gate_wait_started
            if not acquired
            else gate_wait
        )
        audit_elapsed_seconds = time.monotonic() - started
        if self._profiling_enabled:
            audit_started = time.monotonic()
            try:
                self._audit(
                    claim,
                    task_id,
                    result,
                    accepted=accepted,
                    call_index=call_index,
                    gate_wait_seconds=audit_gate_wait_seconds,
                    elapsed_seconds=audit_elapsed_seconds,
                    candidate_sha256=(
                        snapshot.sha256 if snapshot is not None else None
                    ),
                    task_contract_sha256=result.get("task_contract_sha256"),
                    judge_job_id=result.get("judge_job_id"),
                    cache_reused=result.get("cache_reused") is True,
                )
            finally:
                audit_seconds = max(0.0, time.monotonic() - audit_started)
                self._profile_event(
                    "judge.audit.end",
                    claim=claim,
                    task_id=task_id,
                    elapsed_seconds=audit_seconds,
                    accepted=accepted,
                )
        else:
            self._audit(
                claim,
                task_id,
                result,
                accepted=accepted,
                call_index=call_index,
                gate_wait_seconds=audit_gate_wait_seconds,
                elapsed_seconds=audit_elapsed_seconds,
                candidate_sha256=snapshot.sha256 if snapshot is not None else None,
                task_contract_sha256=result.get("task_contract_sha256"),
                judge_job_id=result.get("judge_job_id"),
                cache_reused=result.get("cache_reused") is True,
            )
        # The normal evaluator path reaches this point after the durable audit;
        # expose the same bounded receipt profile as pre-admission failures.
        # ``result`` is already sanitized above, and no response/error payload
        # is forwarded to the profiling sink.
        self._profile_event(
            "judge.receipt",
            claim=claim,
            task_id=task_id,
            accepted=accepted,
            status=result.get("status"),
            proved=result.get("proved") is True,
            judge_job_id=result.get("judge_job_id"),
            score=result.get("score"),
            retryable=result.get("retryable") is True,
            gate_wait_seconds=audit_gate_wait_seconds,
            elapsed_seconds=audit_elapsed_seconds,
            snapshot_seconds=snapshot_seconds,
            evaluator_seconds=evaluator_seconds,
            audit_seconds=audit_seconds,
            cache_reused=result.get("cache_reused") is True,
            candidate_sha256=snapshot.sha256 if snapshot is not None else None,
        )
        if _valid_judge_checkpoint(
            result,
            raw_judge_job_id=raw_judge_job_id,
            expected_task_contract_sha256=binding.expected_task_contract_sha256,
        ):
            with claim.lock:
                claim.judge_checkpoint_reached = True
        return result

    def _acquire_evaluator_gate(
        self,
        deadline_monotonic: float,
        *,
        claim: _SessionClaim,
        task_id: str | None = None,
    ) -> bool:
        """Acquire the shared evaluator gate FIFO among broker callers."""

        waiter = object()
        acquired = False
        queued_at = time.monotonic()
        with self._admission_condition:
            self._admission_queue.append(waiter)
            self._profile_event(
                "judge.queued",
                claim=claim,
                task_id=task_id,
                queue_depth=len(self._admission_queue),
            )
        try:
            while True:
                if self._remote_settlement_unconfirmed():
                    return False
                if _claim_cancelled(claim):
                    return False
                with self._admission_condition:
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        return False
                    if not self._admission_queue or self._admission_queue[0] is not waiter:
                        self._admission_condition.wait(timeout=min(remaining, 0.1))
                        continue
                # Only the FIFO head competes for the underlying semaphore.
                # Final evaluator calls share that semaphore and may release it
                # without notifying this condition, so block on the semaphore
                # itself once this waiter reaches the head.
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    return False
                acquired = self.evaluator_gate.acquire(timeout=min(remaining, 0.1))
                if acquired:
                    if self._remote_settlement_unconfirmed():
                        self._release_evaluator_gate()
                        acquired = False
                        return False
                    self._profile_event(
                        "judge.running",
                        claim=claim,
                        task_id=task_id,
                        queue_depth=len(self._admission_queue),
                        wait_seconds=time.monotonic() - queued_at,
                    )
                    return True
        finally:
            with self._admission_condition:
                try:
                    self._admission_queue.remove(waiter)
                except ValueError:
                    pass
                self._admission_condition.notify_all()

    def _release_evaluator_gate(self) -> None:
        self.evaluator_gate.release()
        with self._admission_condition:
            self._admission_condition.notify_all()
        with self._handler_condition:
            self._handler_condition.notify_all()

    def _finish_judge_check(
        self,
        claim: _SessionClaim,
        task_id: str,
        result: Mapping[str, Any],
        *,
        accepted: bool,
        started: float,
        gate_wait_started: float,
        gate_wait_seconds: float | None = None,
        candidate_sha256: str | None = None,
        clear_probe_active: bool = False,
    ) -> dict[str, Any]:
        """Close and audit a pre-admission control result."""

        # The initial validation/quota branches call this helper while holding
        # ``claim.lock``.  They do not own the active probe marker (and must
        # leave a concurrent request's marker untouched), so only callers that
        # have set ``probe_active`` for this request ask us to clear it.  This
        # explicit ownership bit avoids re-entering the non-reentrant lock and
        # preserves the in-flight guard under concurrent capability calls.
        if clear_probe_active:
            with claim.lock:
                claim.probe_active = False
        normalized = dict(result)
        normalized["accepted"] = accepted
        if not self._profiling_enabled:
            # Keep the disabled path equivalent to the historical audit call:
            # no profiling clocks, receipt construction, or sink interaction.
            self._audit(
                claim,
                task_id,
                normalized,
                accepted=accepted,
                gate_wait_seconds=(
                    time.monotonic() - gate_wait_started
                    if gate_wait_seconds is None
                    else gate_wait_seconds
                ),
                elapsed_seconds=time.monotonic() - started,
                candidate_sha256=candidate_sha256,
            )
            return normalized

        effective_gate_wait = (
            time.monotonic() - gate_wait_started
            if gate_wait_seconds is None
            else gate_wait_seconds
        )
        audit_started = time.monotonic()
        try:
            self._audit(
                claim,
                task_id,
                normalized,
                accepted=accepted,
                gate_wait_seconds=effective_gate_wait,
                elapsed_seconds=time.monotonic() - started,
                candidate_sha256=candidate_sha256,
            )
        finally:
            self._profile_event(
                "judge.audit.end",
                claim=claim,
                task_id=task_id,
                elapsed_seconds=max(0.0, time.monotonic() - audit_started),
                accepted=accepted,
            )
        self._profile_event(
            "judge.receipt",
            claim=claim,
            task_id=task_id,
            accepted=accepted,
            status=normalized.get("status"),
            proved=normalized.get("proved") is True,
            judge_job_id=normalized.get("judge_job_id"),
            gate_wait_seconds=effective_gate_wait,
            elapsed_seconds=time.monotonic() - started,
            candidate_sha256=candidate_sha256,
        )
        return normalized

    def _evaluate_local(
        self,
        claim: _SessionClaim,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Evaluate exact runner-bound bytes without creating proof authority."""

        started = time.monotonic()
        if self.formal_policy is None or not self.formal_policy.enabled:
            return _control_result(
                "FORMAL_TOOLS_DISABLED",
                "The manifest does not enable the bounded formal tool surface.",
                retryable=False,
            )
        if set(payload) - {"task_id"}:
            result = _control_result(
                "INVALID_REQUEST",
                "evaluate_local accepts only the runner-bound task selection.",
                retryable=False,
            )
            self._formal_audit(claim, "evaluate_local", "__invalid__", result, accepted=False)
            return result
        try:
            task_id = _select_task_id(claim, payload.get("task_id"))
        except ValueError as exc:
            result = _control_result(
                "INVALID_TASK_SELECTION",
                _safe_error(exc),
                retryable=False,
            )
            self._formal_audit(claim, "evaluate_local", "__invalid__", result, accepted=False)
            return result
        call_number = self._formal_increment(task_id, "evaluate_calls")
        if call_number > self.formal_policy.evaluate_calls_per_task:
            result = {
                **_control_result(
                    "BUDGET_EXHAUSTED",
                    "The task-global evaluate.py call budget is exhausted.",
                    retryable=False,
                ),
                "call_number": call_number,
                "advisory_only": True,
                "official_score_eligible": False,
            }
            self._formal_audit(claim, "evaluate_local", task_id, result, accepted=False)
            return result
        capability_failure = _formal_capability_failure(claim)
        if capability_failure is not None:
            result = {
                **capability_failure,
                "call_number": call_number,
                "advisory_only": True,
                "official_score_eligible": False,
            }
            self._formal_audit(claim, "evaluate_local", task_id, result, accepted=False)
            return result

        binding = claim.candidates[task_id]
        try:
            source_bytes = read_regular_bytes(
                binding.path,
                trusted_root=claim.workdir,
                max_bytes=self.formal_policy.max_candidate_bytes,
            )
            source = source_bytes.decode("utf-8")
        except (OSError, UnicodeError):
            result = {
                **_control_result(
                    "CANDIDATE_SNAPSHOT_ERROR",
                    "The runner could not freeze the task candidate for diagnostic evaluation.",
                    retryable=True,
                ),
                "call_number": call_number,
                "advisory_only": True,
                "official_score_eligible": False,
            }
            self._formal_audit(claim, "evaluate_local", task_id, result, accepted=False)
            return result
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        cache_key = (
            task_id,
            binding.expected_task_contract_sha256,
            source_sha256,
        )
        with self._formal_lock:
            cached = self._formal_evaluate_cache.get(cache_key)
            cached_result = _json_clone(cached) if cached is not None else None
        if cached_result is not None:
            cached_result.update(
                {
                    "call_number": call_number,
                    "cache_hit": True,
                    "backend_job_number": None,
                    "accepted": True,
                }
            )
            self._formal_audit(
                claim,
                "evaluate_local",
                task_id,
                cached_result,
                accepted=True,
                candidate_sha256=source_sha256,
                elapsed_seconds=time.monotonic() - started,
            )
            return cached_result

        backend_number = self._formal_reserve(
            task_id,
            "evaluate_backend_jobs",
            self.formal_policy.evaluate_backend_jobs_per_task,
        )
        if backend_number is None:
            result = {
                **_control_result(
                    "BUDGET_EXHAUSTED",
                    "The task-global evaluate.py backend-job budget is exhausted.",
                    retryable=False,
                ),
                "call_number": call_number,
                "candidate_sha256": source_sha256,
                "advisory_only": True,
                "official_score_eligible": False,
            }
            self._formal_audit(claim, "evaluate_local", task_id, result, accepted=False)
            return result

        gate_started = time.monotonic()
        acquired = False
        retain_evaluator_gate = False
        evaluator_call_started = False
        evaluator_unsettled_before = 0
        verdict: Verdict | None = None
        try:
            acquired = self._acquire_evaluator_gate(
                claim.deadline_monotonic,
                claim=claim,
                task_id=task_id,
            )
            if not acquired:
                self._formal_release(task_id, "evaluate_backend_jobs", backend_number)
                backend_number = None
                result = _formal_gate_failure(claim, self._remote_settlement_unconfirmed())
            else:
                probe_source = getattr(self.evaluator, "probe_source", None)
                if not callable(probe_source):
                    self._formal_release(task_id, "evaluate_backend_jobs", backend_number)
                    backend_number = None
                    result = _control_result(
                        "EVALUATOR_ERROR",
                        "The evaluator lacks immutable diagnostic-source support.",
                        retryable=False,
                    )
                else:
                    options: dict[str, Any] = {
                        "deadline_monotonic": claim.deadline_monotonic,
                    }
                    if _accepts_settlement_callback(probe_source):
                        options["settlement_callback"] = self._release_evaluator_gate
                    if _accepts_cancel_event(probe_source):
                        options["cancel_event"] = _ClaimCancelEvent(claim)
                    evaluator_unsettled_before = _nonnegative_count(
                        getattr(self.evaluator, "remote_unsettled_jobs", 0)
                    )
                    evaluator_call_started = True
                    verdict = probe_source(binding.task, source, **options)
                    evaluator_unsettled_after = _nonnegative_count(
                        getattr(self.evaluator, "remote_unsettled_jobs", 0)
                    )
                    call_unsettled = (
                        evaluator_unsettled_after > evaluator_unsettled_before
                        or _has_unsettled_remote_work(
                            _safe_verdict_status(verdict.status),
                            verdict.response,
                        )
                    )
                    deferred_remote = _has_deferred_remote_work(
                        _safe_verdict_status(verdict.status), verdict.response
                    )
                    if call_unsettled or deferred_remote:
                        retain_evaluator_gate = True
                    if call_unsettled and not deferred_remote:
                        self._mark_remote_unsettled()
                        result = _remote_settlement_control_result()
                    else:
                        result = _formal_worker_verdict(verdict)
        except Exception:
            evaluator_unsettled_after = _nonnegative_count(
                getattr(self.evaluator, "remote_unsettled_jobs", 0)
            )
            if (
                evaluator_call_started
                and evaluator_unsettled_after > evaluator_unsettled_before
            ):
                retain_evaluator_gate = True
                self._mark_remote_unsettled()
                result = _remote_settlement_control_result()
            else:
                result = _control_result(
                    "EVALUATOR_ERROR",
                    "The controlled diagnostic evaluation failed.",
                    retryable=False,
                )
        finally:
            if acquired and not retain_evaluator_gate:
                self._release_evaluator_gate()

        if not retain_evaluator_gate and verdict is not None and (
            verdict.cache_reused or _verdict_proves_no_backend_job(verdict)
        ) and backend_number is not None:
            self._formal_release(task_id, "evaluate_backend_jobs", backend_number)
            backend_number = None
        result.update(
            {
                "accepted": verdict is not None,
                "call_number": call_number,
                "backend_job_number": backend_number,
                "candidate_sha256": source_sha256,
                "cache_hit": bool(
                    verdict and verdict.cache_reused and not retain_evaluator_gate
                ),
                "advisory_only": True,
                "official_score_eligible": False,
                "note": (
                    "Agent-local feedback never selects a candidate or writes the score; "
                    "outer closeout submits frozen bytes independently."
                ),
            }
        )
        if (
            not retain_evaluator_gate
            and
            verdict is not None
            and _safe_verdict_status(verdict.status) not in _FORMAL_NONCACHEABLE_STATUSES
        ):
            with self._formal_lock:
                self._formal_evaluate_cache[cache_key] = _json_clone(result)
        self._formal_audit(
            claim,
            "evaluate_local",
            task_id,
            result,
            accepted=verdict is not None,
            candidate_sha256=source_sha256,
            gate_wait_seconds=max(0.0, time.monotonic() - gate_started),
            elapsed_seconds=time.monotonic() - started,
            backend_number=backend_number,
        )
        return result

    def _formal_query(
        self,
        claim: _SessionClaim,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Serve one bounded search/elaboration query under task-global quotas."""

        started = time.monotonic()
        formal = self.formal_policy
        if formal is None or not formal.enabled:
            return _control_result(
                "FORMAL_TOOLS_DISABLED",
                "The manifest does not enable the bounded formal tool surface.",
                retryable=False,
            )
        allowed_fields = {
            "task_id",
            "command",
            "query",
            "limit",
            "snippet",
            "tactics",
            "tactic",
            "timeout",
        }
        if set(payload) - allowed_fields:
            result = _control_result(
                "INVALID_REQUEST",
                "formal_query received unsupported fields.",
                retryable=False,
            )
            self._formal_audit(claim, "formal_query", "__invalid__", result, accepted=False)
            return result
        try:
            task_id = _select_task_id(claim, payload.get("task_id"))
        except ValueError as exc:
            result = _control_result(
                "INVALID_TASK_SELECTION",
                _safe_error(exc),
                retryable=False,
            )
            self._formal_audit(claim, "formal_query", "__invalid__", result, accepted=False)
            return result
        command = str(payload.get("command") or "").strip().lower()
        if command not in {"search", "decl", "check", "type", "axioms", "deps"}:
            result = _control_result(
                "INVALID_REQUEST",
                "Unsupported formal_query command.",
                retryable=False,
            )
            self._formal_audit(claim, "formal_query", task_id, result, accepted=False)
            return result
        call_number = self._formal_increment(task_id, "query_calls")
        if call_number > formal.query_calls_per_task:
            result = {
                "ok": False,
                "status": "scout_call_budget_exhausted",
                "call_number": call_number,
                "advisory_only": True,
                "final_verify_required": True,
            }
            self._formal_audit(claim, "formal_query", task_id, result, accepted=False)
            return result
        capability_failure = _formal_capability_failure(claim)
        if capability_failure is not None:
            result = {
                **capability_failure,
                "call_number": call_number,
                "advisory_only": True,
                "final_verify_required": True,
            }
            self._formal_audit(claim, "formal_query", task_id, result, accepted=False)
            return result

        raw_query = payload.get("query")
        query_values = raw_query if isinstance(raw_query, list) else []
        query = [sanitize_public_text(str(value), limit=400) for value in query_values[:12]]
        query_text = " ".join(query).strip()
        limit = _bounded_limit(payload.get("limit"), default=12, maximum=24)
        timeout = _bounded_limit(payload.get("timeout"), default=30, maximum=120)
        binding = claim.candidates[task_id]
        guarded = {binding.task.theorem_name, binding.task.problem_id, binding.task.slug}
        backend_probes = 0
        cache_hits = 0

        if command == "search":
            public = self._formal_search_public(binding.path.parent, query, limit=limit)
            matches = formal.declaration_index.search(
                query_text,
                limit=limit,
                guarded_names=guarded,
            )
            result: dict[str, Any] = {
                "status": "ok" if formal.declaration_index.info.compatible else "index_unavailable",
                "query_kind": "search",
                "public_results": public,
                "mathlib_matches": matches,
                "search_corpus_revision": formal.declaration_index.info.mathlib_revision,
                "index_contract": formal.declaration_index.info.public_dict(),
                "hint": "Index names are advisory; verify a name with ./formal_query check <name>.",
            }
        elif command == "decl":
            matches = formal.declaration_index.search(
                query_text,
                limit=limit,
                guarded_names=guarded,
            )
            result = {
                "status": "searched" if formal.declaration_index.info.compatible else "index_unavailable",
                "query_kind": "decl",
                "matches": matches,
                "result_count": len(matches),
                "search_corpus_revision": formal.declaration_index.info.mathlib_revision,
                "index_contract": formal.declaration_index.info.public_dict(),
                "hint": "Verify candidate names with ./formal_query check <name>.",
            }
        elif command == "deps":
            exact = formal.declaration_index.search(
                query_text,
                limit=6,
                guarded_names=guarded,
            )
            related = formal.declaration_index.search(
                query_text.replace(".", " ").replace("_", " "),
                limit=10,
                guarded_names=guarded,
            )
            result = {
                "status": "searched" if formal.declaration_index.info.compatible else "index_unavailable",
                "query_kind": "deps",
                "query": query_text,
                "exact_matches": exact,
                "related_declarations": related,
                "semantics": "index_related_premises_not_dependency_graph",
                "hint": "Verify related declarations individually with check.",
            }
        else:
            result, backend_probes, cache_hits = self._formal_kernel_query(
                claim,
                binding,
                command,
                query,
                payload,
                timeout=timeout,
                guarded=guarded,
            )
        result.update(
            {
                "advisory_only": True,
                "final_verify_required": True,
                "call_number": call_number,
                "backend_probe_count": backend_probes,
                "cache_hit_count": cache_hits,
                "surface_version": formal.surface_version,
            }
        )
        self._formal_audit(
            claim,
            "formal_query",
            task_id,
            result,
            accepted=True,
            elapsed_seconds=time.monotonic() - started,
            backend_number=backend_probes,
            command=command,
        )
        return result

    def _formal_kernel_query(
        self,
        claim: _SessionClaim,
        binding: _CandidateBinding,
        command: str,
        query: list[str],
        request: Mapping[str, Any],
        *,
        timeout: int,
        guarded: set[str],
    ) -> tuple[dict[str, Any], int, int]:
        task = binding.task
        query_text = " ".join(query).strip()
        snippet = request.get("snippet")
        tactics = request.get("tactics")
        if any(name and _contains_guarded(query_text, name) for name in guarded):
            return {"status": "guarded_declaration_refused", "query_kind": command}, 0, 0
        imports = "\n".join(
            match.group(0).strip() for match in _IMPORT_LINE.finditer(task.baseline_code)
        )
        if command == "check" and isinstance(snippet, str) and snippet.strip():
            code = snippet.strip()[:8_000]
            if any(name and _contains_guarded(code, name) for name in guarded):
                return {"status": "guarded_declaration_refused", "query_kind": "check_snippet"}, 0, 0
            probe, consumed, cache_hit = self._formal_kernel_probe(
                claim,
                binding,
                f"{imports}\n\n{code}\n",
                timeout=timeout,
            )
            return (
                {
                    **probe,
                    "query_kind": "check_snippet",
                    "contains_sorry": bool(
                        re.search(r"(?<![A-Za-z0-9_])sorry(?![A-Za-z0-9_])", code)
                    ),
                },
                int(consumed),
                int(cache_hit),
            )
        if command == "check" and isinstance(tactics, str) and tactics.strip():
            header = tactics.strip()[:4_000]
            if any(name and _contains_guarded(header, name) for name in guarded):
                return {"status": "guarded_declaration_refused", "query_kind": "check_tactics"}, 0, 0
            raw_tactics = request.get("tactic")
            portfolio = (
                [str(value).strip()[:500] for value in raw_tactics[:12] if str(value).strip()]
                if isinstance(raw_tactics, list)
                else []
            ) or list(_DEFAULT_TACTICS)
            attempts: list[dict[str, Any]] = []
            closing: list[str] = []
            remote_unsettled = False
            probes = 0
            hits = 0
            for tactic in portfolio:
                if re.search(
                    r"(?<![A-Za-z0-9_])(?:sorry|admit)(?![A-Za-z0-9_])",
                    tactic,
                ):
                    attempts.append(
                        {
                            "tactic": tactic,
                            "outcome": "placeholder_refused",
                            "diagnostics": [],
                            "cache_hit": False,
                        }
                    )
                    continue
                probe, consumed, cache_hit = self._formal_kernel_probe(
                    claim,
                    binding,
                    f"{imports}\n\n{header} := by\n  {tactic}\n",
                    timeout=timeout,
                )
                probes += int(consumed)
                hits += int(cache_hit)
                closed = (
                    probe.get("status") == "elaborated"
                    and probe.get("is_valid_no_sorry") is True
                )
                attempts.append(
                    {
                        "tactic": tactic,
                        "outcome": "closed" if closed else str(probe.get("status") or "failed"),
                        "diagnostics": list(probe.get("diagnostics") or [])[:4],
                        "elapsed_ms": probe.get("elapsed_ms"),
                        "cache_hit": cache_hit,
                    }
                )
                if closed:
                    closing.append(tactic)
                    break
                if probe.get("status") == "probe_budget_exhausted":
                    break
                if probe.get("status") in {
                    "REMOTE_SETTLEMENT_UNCONFIRMED",
                    "probe_remote_settlement_unconfirmed",
                }:
                    remote_unsettled = True
                    break
            return (
                {
                    "status": (
                        "REMOTE_SETTLEMENT_UNCONFIRMED"
                        if remote_unsettled
                        else "closed" if closing else "not_closed"
                    ),
                    "retryable": False if remote_unsettled else None,
                    "query_kind": "check_tactics",
                    "closing_tactics": closing,
                    "attempts": attempts,
                    "note": "Each uncached tactic attempt consumes one task-global backend-probe unit.",
                },
                probes,
                hits,
            )
        if command == "axioms":
            name = query[0].strip() if query else ""
            if not _LEAN_NAME.fullmatch(name):
                return {"status": "invalid_query", "query_kind": "axioms"}, 0, 0
            try:
                candidate = read_regular_bytes(
                    binding.path,
                    trusted_root=claim.workdir,
                    max_bytes=self.formal_policy.max_candidate_bytes,
                ).decode("utf-8")
            except (OSError, UnicodeError):
                return {"status": "candidate_unavailable", "query_kind": "axioms"}, 0, 0
            probe, consumed, cache_hit = self._formal_kernel_probe(
                claim,
                binding,
                f"{candidate}\n\n#print axioms {name}\n",
                timeout=timeout,
            )
            probe.update(
                {
                    "query_kind": "axioms",
                    "query": name,
                    "candidate_context_included": True,
                }
            )
            return probe, int(consumed), int(cache_hit)
        if not query_text:
            return {"status": "empty_query", "query_kind": command}, 0, 0
        if command == "check" and all(_LEAN_NAME.fullmatch(name) for name in query[:8]):
            code = "\n".join(f"#check {name}" for name in query[:8])
        else:
            code = f"#check {query_text}"
        probe, consumed, cache_hit = self._formal_kernel_probe(
            claim,
            binding,
            f"{imports}\n\n{code}\n",
            timeout=timeout,
        )
        probe.update({"query_kind": command, "query": query_text})
        return probe, int(consumed), int(cache_hit)

    def _formal_kernel_probe(
        self,
        claim: _SessionClaim,
        binding: _CandidateBinding,
        source: str,
        *,
        timeout: int,
    ) -> tuple[dict[str, Any], bool, bool]:
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        cache_key = (
            binding.task.slug,
            binding.expected_task_contract_sha256,
            digest,
        )
        with self._formal_lock:
            cached = self._formal_query_cache.get(cache_key)
            cached_result = _json_clone(cached) if cached is not None else None
        if cached_result is not None:
            cached_result["cache_hit"] = True
            return cached_result, False, True
        backend_number = self._formal_reserve(
            binding.task.slug,
            "query_backend_probes",
            self.formal_policy.query_backend_probes_per_task,
        )
        if backend_number is None:
            return {"status": "probe_budget_exhausted", "cache_hit": False}, False, False

        deadline = min(
            claim.deadline_monotonic,
            time.monotonic() + max(10, int(timeout)) + 30.0,
        )
        acquired = False
        retain_evaluator_gate = False
        evaluator_call_started = False
        evaluator_unsettled_before = 0
        verdict: Verdict | None = None
        try:
            acquired = self._acquire_evaluator_gate(
                deadline,
                claim=claim,
                task_id=binding.task.slug,
            )
            if not acquired:
                self._formal_release(
                    binding.task.slug,
                    "query_backend_probes",
                    backend_number,
                )
                return {
                    "status": (
                        "REMOTE_SETTLEMENT_UNCONFIRMED"
                        if self._remote_settlement_unconfirmed()
                        else "probe_admission_closed"
                    ),
                    "probe_status": (
                        "probe_remote_settlement_unconfirmed"
                        if self._remote_settlement_unconfirmed()
                        else "probe_admission_closed"
                    ),
                    "retryable": False,
                    "cache_hit": False,
                }, False, False
            probe_source = getattr(self.evaluator, "probe_source", None)
            if not callable(probe_source):
                self._formal_release(
                    binding.task.slug,
                    "query_backend_probes",
                    backend_number,
                )
                return {"status": "probe_transport_error", "cache_hit": False}, False, False
            metadata = dict(binding.task.metadata)
            metadata["theorem_name"] = ""
            import_contract = "\n".join(
                match.group(0).strip() for match in _IMPORT_LINE.finditer(source)
            )
            probe_task = Task(
                slug=binding.task.slug,
                root=binding.task.root,
                problem_text="",
                baseline_code=f"{import_contract}\n" if import_contract else "",
                metadata=metadata,
            )
            options: dict[str, Any] = {"deadline_monotonic": deadline}
            if _accepts_settlement_callback(probe_source):
                options["settlement_callback"] = self._release_evaluator_gate
            if _accepts_cancel_event(probe_source):
                options["cancel_event"] = _ClaimCancelEvent(claim)
            evaluator_unsettled_before = _nonnegative_count(
                getattr(self.evaluator, "remote_unsettled_jobs", 0)
            )
            evaluator_call_started = True
            verdict = probe_source(probe_task, source, **options)
            evaluator_unsettled_after = _nonnegative_count(
                getattr(self.evaluator, "remote_unsettled_jobs", 0)
            )
            call_unsettled = (
                evaluator_unsettled_after > evaluator_unsettled_before
                or _has_unsettled_remote_work(
                    _safe_verdict_status(verdict.status),
                    verdict.response,
                )
            )
            deferred_remote = _has_deferred_remote_work(
                _safe_verdict_status(verdict.status), verdict.response
            )
            if call_unsettled or deferred_remote:
                retain_evaluator_gate = True
            if call_unsettled and not deferred_remote:
                self._mark_remote_unsettled()
                result = {
                    "status": "REMOTE_SETTLEMENT_UNCONFIRMED",
                    "probe_status": "probe_remote_settlement_unconfirmed",
                    "retryable": False,
                    "cache_hit": False,
                }
            else:
                result = _formal_probe_result(verdict)
        except Exception:
            evaluator_unsettled_after = _nonnegative_count(
                getattr(self.evaluator, "remote_unsettled_jobs", 0)
            )
            if (
                evaluator_call_started
                and evaluator_unsettled_after > evaluator_unsettled_before
            ):
                retain_evaluator_gate = True
                self._mark_remote_unsettled()
                result = {
                    "status": "REMOTE_SETTLEMENT_UNCONFIRMED",
                    "probe_status": "probe_remote_settlement_unconfirmed",
                    "retryable": False,
                    "cache_hit": False,
                }
            else:
                result = {"status": "probe_transport_error", "cache_hit": False}
        finally:
            if acquired and not retain_evaluator_gate:
                self._release_evaluator_gate()

        consumed = True
        if not retain_evaluator_gate and verdict is not None and (
            verdict.cache_reused or _verdict_proves_no_backend_job(verdict)
        ):
            self._formal_release(
                binding.task.slug,
                "query_backend_probes",
                backend_number,
            )
            consumed = False
        result.update(
            {
                "cache_hit": bool(
                    verdict and verdict.cache_reused and not retain_evaluator_gate
                ),
                "probe_number": backend_number if consumed else None,
            }
        )
        if (
            not retain_evaluator_gate
            and
            verdict is not None
            and _safe_verdict_status(verdict.status) not in _FORMAL_NONCACHEABLE_STATUSES
        ):
            with self._formal_lock:
                self._formal_query_cache[cache_key] = _json_clone(result)
        return result, consumed, bool(verdict and verdict.cache_reused)

    def _formal_search_public(
        self,
        workspace: Path,
        terms: list[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        candidates = [workspace / "problem.md", workspace / "result.lean"]
        candidates.extend(sorted((workspace / "baseline").glob("*.lean")))
        lowered_terms = [term.lower() for term in terms if term]
        rows: list[dict[str, Any]] = []
        for path in candidates:
            try:
                text = read_regular_bytes(
                    path,
                    trusted_root=workspace,
                    max_bytes=self.formal_policy.max_candidate_bytes,
                ).decode("utf-8", errors="replace")
            except OSError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                lowered = line.lower()
                if lowered_terms and not all(term in lowered for term in lowered_terms):
                    continue
                rows.append(
                    {
                        "file": str(path.relative_to(workspace)),
                        "line": line_number,
                        "text": sanitize_public_text(line.strip(), limit=260),
                    }
                )
                if len(rows) >= limit:
                    return rows
        return rows

    def _formal_increment(self, task_id: str, counter: str) -> int:
        with self._formal_lock:
            key = (task_id, counter)
            value = self._formal_counts.get(key, 0) + 1
            self._formal_counts[key] = value
            return value

    def _formal_reserve(self, task_id: str, counter: str, limit: int) -> int | None:
        with self._formal_lock:
            key = (task_id, counter)
            current = self._formal_counts.get(key, 0)
            if current >= max(0, int(limit)):
                return None
            serial = self._formal_serials.get(key, 0) + 1
            self._formal_serials[key] = serial
            self._formal_counts[key] = current + 1
            return serial

    def _formal_release(self, task_id: str, counter: str, serial: int) -> None:
        del serial
        with self._formal_lock:
            key = (task_id, counter)
            self._formal_counts[key] = max(0, self._formal_counts.get(key, 0) - 1)

    def _formal_audit(
        self,
        claim: _SessionClaim,
        operation: str,
        task_id: str,
        result: Mapping[str, Any],
        *,
        accepted: bool,
        candidate_sha256: str | None = None,
        gate_wait_seconds: float = 0.0,
        elapsed_seconds: float = 0.0,
        backend_number: int | None = None,
        command: str | None = None,
    ) -> None:
        if self.formal_policy is None or not self.formal_policy.enabled:
            return
        row = {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": operation,
            "actor_id": claim.actor_id,
            "episode": claim.episode,
            "task_id": task_id,
            "command": command,
            "accepted": bool(accepted),
            "status": str(result.get("status") or "UNKNOWN")[:120],
            "call_number": result.get("call_number"),
            "backend_number": backend_number,
            "backend_probe_count": result.get("backend_probe_count"),
            "cache_hit": result.get("cache_hit") is True,
            "cache_hit_count": result.get("cache_hit_count"),
            "candidate_sha256": _safe_hash(candidate_sha256),
            "gate_wait_seconds": round(max(0.0, gate_wait_seconds), 6),
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 6),
            "advisory_only": True,
            "official_score_eligible": False,
        }
        with self._audit_lock:
            with self.formal_audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def _cps_operation(
        self,
        claim: _SessionClaim,
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        # Serialize each session's CPS calls with capability revocation.  The
        # store rechecks the same cancellation signal after acquiring its
        # SQLite write lock, closing the cancellation-during-lock-wait race.
        with claim.cps_lock:
            failure = _cps_capability_failure(claim)
            if failure is not None:
                return failure
            if operation in _ROUTE_CLAIM_OPERATIONS and not claim.route_claims_enabled:
                return _control_result(
                    "CPS_CAPABILITY_DENIED",
                    "This solver session has no active-route capability.",
                    retryable=False,
                )
            with claim.lock:
                checkpoint_reached = claim.judge_checkpoint_reached
            # A fail-open latch does not widen the pre-Judge surface.  Only
            # discovery and the first claim may run before the checkpoint;
            # update/release must still return the ordinary gate response even
            # after an earlier route-store outage set a bypass marker.
            if (
                operation in _ROUTE_CLAIM_OPERATIONS
                and not checkpoint_reached
                and operation not in _PRE_JUDGE_ROUTE_OPERATIONS
            ):
                return _control_result(
                    "JUDGE_CHECK_REQUIRED",
                    "Complete a terminal judge_check before using CPS communication.",
                    retryable=False,
                )
            if (
                operation in _ROUTE_CLAIM_OPERATIONS
                and claim.route_claim_bypass_reason is not None
            ):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason=claim.route_claim_bypass_reason,
                )
            # Mono/Parallel sessions do not carry a CPS store or communication
            # capability. Preserve their historical CPS_UNAVAILABLE response
            # rather than imposing a checkpoint requirement on an endpoint
            # their solver cannot access.
            if claim.cps_store is None:
                if operation in _ROUTE_CLAIM_OPERATIONS:
                    return self._route_claim_bypass(
                        claim,
                        operation,
                        reason="unavailable",
                    )
                return self._cps_operation_locked(claim, operation, payload)
            if claim.communication == "none" and operation not in _ROUTE_CLAIM_OPERATIONS:
                return self._cps_operation_locked(claim, operation, payload)
            if operation in {"cps_actors", "cps_send", "cps_inbox", "cps_ack"} and not (
                claim.direct_messages_allowed
            ):
                return _control_result(
                    "CPS_CAPABILITY_DENIED",
                    "This solver session has no direct-message capability.",
                    retryable=False,
                )
            if operation == "cps_feedback" and not claim.selection_enabled:
                return _control_result(
                    "CPS_CAPABILITY_DENIED",
                    "This solver session has no selection-feedback capability.",
                    retryable=False,
                )
            # Discovery and the first atomic claim intentionally form the
            # pre-Judge coordination window.  Every historical CPS operation
            # (including route update/release) remains behind the checkpoint.
            if not checkpoint_reached and operation not in _PRE_JUDGE_ROUTE_OPERATIONS:
                return _control_result(
                    "JUDGE_CHECK_REQUIRED",
                    "Complete a terminal judge_check before using CPS communication.",
                    retryable=False,
                )
            try:
                result = self._cps_operation_locked(claim, operation, payload)
                # A compatibility adapter may ignore the runner deadline while
                # waiting on its own I/O. Re-check after the call so a response
                # that arrived after cancellation/horizon cannot be treated as
                # a live route lease (or a successful CPS write).
                failure = _cps_capability_failure(claim)
                if failure is not None:
                    return failure
                return result
            except ValueError as exc:
                # Strict route payload validation is a handled protocol
                # negative, not evidence that the CPS store is unavailable.
                # Keeping this fail-closed prevents a forged loopback POST
                # from obtaining the explicit fail-open write path.
                if operation in _ROUTE_CLAIM_OPERATIONS:
                    return _control_result(
                        "INVALID_REQUEST",
                        _safe_error(exc),
                        retryable=False,
                    )
                raise
            except RuntimeError:
                # CPSStore intentionally raises a plain RuntimeError when the
                # horizon or cancellation guard closes after a lock wait.  Map
                # only a now-observable capability closure to a stable worker
                # response; unrelated store failures remain BROKER_ERROR.
                failure = _cps_capability_failure(claim)
                if failure is not None:
                    return failure
                raise

    def _route_claim_bypass(
        self,
        claim: _SessionClaim,
        operation: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Return an explicit fail-open result for unavailable route state.

        The solver may continue its candidate path when a diagnostic route
        store is unavailable, but the response must never look like a claim
        succeeded.  The bounded reason is also persisted as a CPS event when
        the store still exposes the event surface.
        """

        bounded_reason = _bounded_route_bypass_reason(reason)
        # A fail-open response explicitly says that the broker can no longer
        # vouch for the local lease set.  Drop those ids before returning so a
        # later in-process check cannot accidentally report a stale claim as
        # authoritative (the solver still proceeds because the bypass marker
        # is explicit).
        with claim.lock:
            claim.route_claim_bypass_reason = bounded_reason
            claim.route_claim_ids.clear()
            claim.route_claim_satisfied = False
        self._record_route_claim_bypass(claim, operation, bounded_reason)
        result: dict[str, Any] = {
            "ok": True,
            "accepted": False,
            "bypassed": True,
            "status": "ROUTE_CLAIM_BYPASS",
            "route_claim_bypass_reason": bounded_reason,
            "message": (
                "Active-route coordination is unavailable; continue the candidate "
                "path without treating this as a completed claim."
            ),
        }
        if operation == "cps_active_routes":
            result["routes"] = []
        elif operation == "cps_actors":
            result["actors"] = []
        elif operation == "cps_claim_route":
            result.update({"claimed": False, "acquired": False, "claim": None})
        return result

    @staticmethod
    def _clear_route_claim_state(
        claim: _SessionClaim,
        claim_id: str | None = None,
    ) -> None:
        """Forget locally tracked leases after a handled terminal/identity negative."""

        with claim.lock:
            if claim_id is None:
                claim.route_claim_ids.clear()
            else:
                claim.route_claim_ids.discard(claim_id)
            claim.route_claim_satisfied = bool(claim.route_claim_ids)

    @staticmethod
    def _record_route_claim_bypass(
        claim: _SessionClaim,
        operation: str,
        reason: str,
    ) -> None:
        store = claim.cps_store
        recorder = getattr(store, "record_event", None) if store is not None else None
        if not callable(recorder):
            return
        try:
            _call_store_method(
                recorder,
                event_type="route_claim_bypass",
                task_id=next(iter(claim.candidates)),
                actor_id=claim.actor_id,
                payload={
                    "operation": operation,
                    "route_claim_bypass_reason": reason,
                },
                deadline_epoch_ms=claim.deadline_epoch_ms,
                cancel_guard=lambda: _claim_cancelled(claim),
            )
        except Exception:
            # The original store failure is already represented in the public
            # response; never turn best-effort audit into a solver failure.
            return

    def _route_actor_admission(
        self,
        claim: _SessionClaim,
        task_id: str,
        *,
        operation: str,
    ) -> dict[str, Any] | None:
        """Verify that this route-capable session belongs to a live admission.

        The broker token is normally issued immediately after runner admission,
        but checking the runner-owned roster at every route operation protects
        direct test/adaptor callers too.  A missing or malformed roster is an
        unavailable coordination dependency, so it takes the explicit
        fail-open path rather than becoming an implicit empty roster.
        """

        store = claim.cps_store
        if store is None:
            return self._route_claim_bypass(
                claim,
                operation,
                reason="unavailable",
            )
        try:
            roster_method = _first_callable(
                store,
                "list_active_actors",
                "active_actors",
                "cps_active_actors",
            )
        except _RouteClaimStoreUnavailable:
            return self._route_claim_bypass(
                claim,
                operation,
                reason="unavailable",
            )
        try:
            active_rows = _call_store_method(
                roster_method,
                task_id=task_id,
                include_closing=True,
                limit=500,
                deadline_epoch_ms=claim.deadline_epoch_ms,
                cancel_guard=lambda: _claim_cancelled(claim),
            )
        except ValueError as exc:
            # A malformed request/adapter argument must remain a handled,
            # fail-closed protocol response. Do not turn it into a route
            # outage bypass, because the Pi gate treats that marker as
            # permission to continue without a lease.
            return _control_result(
                "INVALID_REQUEST",
                _safe_error(exc),
                retryable=False,
            )
        except Exception:
            return self._route_claim_bypass(
                claim,
                operation,
                reason="unavailable",
            )
        if not _is_row_collection(
            active_rows,
            keys=("actors", "items"),
            required_fields=("task_id", "actor_id"),
        ):
            return self._route_claim_bypass(
                claim,
                operation,
                reason="unavailable",
            )
        safe_active_rows = _safe_actor_rows(active_rows)
        # The roster query is task-scoped, but a compatibility adapter may
        # ignore that argument and return a mixed projection.  Do not expose
        # or admit against such a projection: once the treatment is enabled,
        # task identity is part of the runner-owned capability boundary.
        raw_active_rows = (
            list(active_rows)
            if isinstance(active_rows, (list, tuple))
            else next(
                (
                    list(active_rows.get(key))
                    for key in ("actors", "items")
                    if isinstance(active_rows, Mapping)
                    and isinstance(active_rows.get(key), (list, tuple))
                ),
                None,
            )
        )
        if (
            raw_active_rows is None
            or len(safe_active_rows) != len(raw_active_rows)
            or not _actor_rows_match_task(safe_active_rows, task_id=task_id)
            or any(
                not isinstance(item.get("status"), str)
                or not str(item.get("status") or "").strip()
                or str(item.get("status") or "").strip().lower()
                not in (_ACTOR_LIVE_STATUSES | _ACTOR_TERMINAL_STATUSES | {"closing"})
                for item in safe_active_rows
            )
        ):
            return self._route_claim_bypass(
                claim,
                operation,
                reason="unavailable",
            )
        identity_rows = [
            item
            for item in safe_active_rows
            if str(item.get("task_id") or "") == str(task_id)
            if str(item.get("actor_id") or "") == claim.actor_id
        ]
        if not identity_rows:
            self._clear_route_claim_state(claim)
            return _control_result(
                "ACTOR_NOT_ADMITTED",
                "Register the actor at runner admission before using route coordination.",
                retryable=False,
            )
        # ``active`` is derived from a canonical non-terminal status by the
        # sanitizer.  A terminal row is a normal stale-admission result, while
        # a row with no usable status/active bit is an unavailable adapter
        # projection and must take the explicit fail-open path.  Requiring an
        # explicit true value prevents a sparse `{task_id, actor_id}` row from
        # minting admission or leaving the required write gate stuck forever.
        matching_rows = [item for item in identity_rows if item.get("active") is True]
        if not matching_rows:
            malformed_identity = any(
                not isinstance(item.get("status"), str)
                or not str(item.get("status") or "").strip()
                for item in identity_rows
            )
            if malformed_identity:
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            self._clear_route_claim_state(claim)
            return _control_result(
                "ACTOR_NOT_ADMITTED",
                "Register the actor at runner admission before using route coordination.",
                retryable=False,
            )
        # There must be exactly one live admission for this actor. Multiple
        # active rows (for example, stale and current episodes returned by a
        # buggy adapter) make the capability ambiguous, so fail open rather
        # than guessing which route owner is authoritative.
        if len(matching_rows) != 1:
            return self._route_claim_bypass(
                claim,
                operation,
                reason="unavailable",
            )
        observed_episode = matching_rows[0].get("episode")
        if observed_episode != claim.episode:
            self._clear_route_claim_state(claim)
            return _control_result(
                "ACTOR_EPISODE_MISMATCH",
                "The actor was admitted for a different episode.",
                retryable=False,
            )
        return None

    def _cps_operation_locked(
        self,
        claim: _SessionClaim,
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        store = claim.cps_store
        if store is None or (
            claim.communication == "none" and operation not in _ROUTE_CLAIM_OPERATIONS
        ):
            return _control_result(
                "CPS_UNAVAILABLE",
                "This solver session has no communication capability.",
                retryable=False,
            )
        task_id = next(iter(claim.candidates))
        allowed_fields = {
            "cps_search": {"query", "limit"},
            "cps_publish": {"kind", "title", "body", "tags", "scope"},
            "cps_actors": {"query"},
            "cps_active_routes": {"limit", "query", "task_id", "include_closing"},
            "cps_claim_route": {
                "route_key",
                "summary",
                "ttl_seconds",
                "independent_verification_reason",
            },
            "cps_update_route": {
                "claim_id",
                "status",
                "summary",
                "ttl_seconds",
                "independent_verification_reason",
            },
            "cps_release_route": {"claim_id", "status", "reason"},
            "cps_send": {"recipient", "body", "scope"},
            "cps_inbox": {"limit"},
            "cps_ack": {"message_id"},
            "cps_feedback": {
                "request_key",
                "exposure_item_id",
                "trace_id",
                "feedback_kind",
                "value",
                "note",
            },
        }.get(operation)
        if allowed_fields is None:
            return _control_result(
                "UNKNOWN_CPS_OPERATION", "Unknown CPS operation.", retryable=False
            )
        if set(payload) - allowed_fields:
            return _control_result(
                "INVALID_REQUEST",
                "The CPS tool accepts only its declared runner-controlled fields.",
                retryable=False,
            )
        if operation == "cps_search":
            query = _bounded_string(payload.get("query"), 500)
            limit = _bounded_limit(payload.get("limit"), default=8, maximum=8)
            if claim.selection_enabled and claim.selection_search is not None:
                try:
                    selected = claim.selection_search(claim, query, limit)
                except Exception:
                    return _control_result(
                        "BROKER_ERROR",
                        "The controlled selection search failed this capability call.",
                        retryable=False,
                    )
                return _safe_selection_search_response(selected, limit=limit)
            return {
                "ok": True,
                "items": [
                    _safe_piece(item)
                    for item in store.search(
                        task_id=task_id,
                        query=query,
                        limit=limit,
                        include_global=claim.communication == "hybrid",
                    )
                ],
            }
        if operation == "cps_publish":
            title = _required_string(payload.get("title"), "title", 300)
            body = _required_string(payload.get("body"), "body", 8_000)
            kind = _bounded_string(payload.get("kind"), 64) or "handoff"
            if kind.casefold() in _RUNNER_ONLY_CPS_KINDS:
                return _control_result(
                    "RUNNER_ONLY_CPS_KIND",
                    "This CPS piece kind is reserved for runner-owned evaluator feedback.",
                    retryable=False,
                )
            tags_raw = payload.get("tags", [])
            if not isinstance(tags_raw, list):
                raise ValueError("tags must be an array")
            tags = [_bounded_string(item, 64) for item in tags_raw[:8]]
            scope = _scope(payload.get("scope"), claim.communication)
            item = store.create_piece(
                task_id="__global__" if scope == "global" else task_id,
                author=claim.actor_id,
                kind=kind,
                title=title,
                body=body,
                tags=[tag for tag in tags if tag],
                deadline_epoch_ms=claim.deadline_epoch_ms,
                cancel_guard=lambda: _claim_cancelled(claim),
            )
            return {"ok": True, "piece": _safe_piece(item)}
        if operation == "cps_actors":
            query = _bounded_string(payload.get("query"), 300).lower()
            if claim.route_claims_enabled:
                # The SQLite roster is authoritative once the treatment is
                # enabled.  Falling back to the historical projection here
                # would re-expose future assignments that were not admitted.
                admission_failure = self._route_actor_admission(
                    claim,
                    task_id,
                    operation=operation,
                )
                if admission_failure is not None:
                    return admission_failure
                try:
                    rows = _call_store_method(
                        _first_callable(
                            store,
                            "list_active_actors",
                            "active_actors",
                            "cps_active_actors",
                        ),
                        task_id=task_id,
                        include_closing=True,
                        deadline_epoch_ms=claim.deadline_epoch_ms,
                        cancel_guard=lambda: _claim_cancelled(claim),
                    )
                except ValueError as exc:
                    return _control_result(
                        "INVALID_REQUEST",
                        _safe_error(exc),
                        retryable=False,
                    )
                except Exception:
                    return self._route_claim_bypass(
                        claim,
                        operation,
                        reason="unavailable",
                    )
                if not _is_row_collection(
                    rows,
                    keys=("actors", "items"),
                    required_fields=("task_id", "actor_id"),
                ):
                    return self._route_claim_bypass(
                        claim,
                        operation,
                        reason="unavailable",
                    )
                actors = _safe_actor_rows(rows)
                raw_actor_rows = (
                    list(rows)
                    if isinstance(rows, (list, tuple))
                    else next(
                        (
                            list(rows.get(key))
                            for key in ("actors", "items")
                            if isinstance(rows, Mapping)
                            and isinstance(rows.get(key), (list, tuple))
                        ),
                        None,
                    )
                )
                if (
                    raw_actor_rows is None
                    or len(actors) != len(raw_actor_rows)
                    or not _actor_rows_match_task(actors, task_id=task_id)
                    or any(
                        not isinstance(item.get("status"), str)
                        or not str(item.get("status") or "").strip()
                        or str(item.get("status") or "").strip().lower()
                        not in (_ACTOR_LIVE_STATUSES | _ACTOR_TERMINAL_STATUSES | {"closing"})
                        for item in actors
                    )
                ):
                    return self._route_claim_bypass(
                        claim,
                        operation,
                        reason="unavailable",
                    )
                # ``cps_actors`` is an active-roster view, not a historical
                # audit dump.  Terminal rows may remain in a compatibility
                # adapter's projection; hide them after validating the full
                # response so a stale ``active=true`` bit cannot leak a
                # finished actor to a peer.
                actors = _live_actor_rows(actors)
            else:
                actors = _safe_roster(claim.roster_path)
            if query:
                actors = [
                    item
                    for item in actors
                    if query in json.dumps(item, ensure_ascii=False).lower()
                ]
            return {"ok": True, "actors": actors[:100]}
        if operation == "cps_active_routes":
            admission_failure = self._route_actor_admission(
                claim,
                task_id,
                operation=operation,
            )
            if admission_failure is not None:
                return admission_failure
            query = _bounded_string(payload.get("query"), 300).lower()
            limit = _bounded_limit(payload.get("limit"), default=16, maximum=100)
            requested_task = _bounded_string(payload.get("task_id"), 256)
            if requested_task and requested_task != task_id:
                return _control_result(
                    "INVALID_TASK_SELECTION",
                    "active routes are limited to the runner-bound task.",
                    retryable=False,
                )
            include_closing_raw = payload.get("include_closing", False)
            if not isinstance(include_closing_raw, bool):
                return _control_result(
                    "INVALID_REQUEST",
                    "include_closing must be a boolean.",
                    retryable=False,
                )
            try:
                rows = _call_store_method(
                    _first_callable(
                        store,
                        "list_active_routes",
                        "active_routes",
                        "list_route_claims",
                        "cps_active_routes",
                    ),
                    task_id=task_id,
                    limit=limit,
                    include_closing=include_closing_raw,
                    deadline_epoch_ms=claim.deadline_epoch_ms,
                    cancel_guard=lambda: _claim_cancelled(claim),
                )
            except ValueError as exc:
                return _control_result(
                    "INVALID_REQUEST",
                    _safe_error(exc),
                    retryable=False,
                )
            except Exception:
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            if not _is_row_collection(
                rows,
                keys=("routes", "claims", "items"),
                required_fields=("claim_id", "task_id", "actor_id", "route_key"),
            ):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            routes = _safe_route_rows(rows)
            if isinstance(rows, (list, tuple)):
                raw_route_rows = list(rows)
            elif isinstance(rows, Mapping):
                raw_route_rows = next(
                    (
                        list(rows.get(key))
                        for key in ("routes", "claims", "items")
                        if isinstance(rows.get(key), (list, tuple))
                    ),
                    None,
                )
            else:
                raw_route_rows = None
            # Do not let a malformed/legacy adapter smuggle another task's
            # claims through a task-scoped query.  Returning an explicit
            # bypass is safer than silently presenting an incomplete route
            # picture and is auditable by the runner.
            if (
                raw_route_rows is None
                or len(routes) != len(raw_route_rows)
                or not _route_rows_match_task(routes, task_id=task_id)
                or any(
                    str(item.get("status") or "").strip().lower()
                    not in (_ROUTE_VISIBLE_STATUSES | _ROUTE_TERMINAL_STATUSES)
                    for item in routes
                )
            ):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            # Terminal/expired rows are valid stale observations but do not
            # belong in an API named ``active_routes``.  Keep blocked leases
            # visible for coordination; the claim gate separately requires
            # status=active.
            routes = _live_route_rows(routes)
            if query:
                routes = [
                    item
                    for item in routes
                    if query in json.dumps(item, ensure_ascii=False).lower()
                ]
            if claim.activity_feedback_enabled:
                # Peer coordination needs the human-readable activity report,
                # not another machine-readable uniqueness signal.  Keep the
                # opaque key on the caller's own claim response (so update /
                # release remain bound), but omit peer route keys from this
                # shared listing.  This makes it impossible for a model to
                # mistake the technical handle for a semantic de-duplication
                # instruction while preserving the legacy route projection.
                routes = [
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"route_key", "route_key_semantics"}
                    }
                    for item in routes
                ]
            return {
                "ok": True,
                "accepted": True,
                "status": "OK",
                "routes": routes[:limit],
            }
        if operation == "cps_claim_route":
            route_key = sanitize_public_text(
                _required_string(payload.get("route_key"), "route_key", 512),
                limit=512,
            )
            summary = sanitize_public_text(
                _required_string(payload.get("summary"), "summary", 1_000),
                limit=1_000,
            )
            ttl_seconds = _route_ttl_from_payload(
                payload.get("ttl_seconds"),
                default=claim.route_claim_ttl_seconds,
            )
            independent_reason = sanitize_public_text(
                _bounded_string(
                    payload.get("independent_verification_reason"),
                    1_000,
                ),
                limit=1_000,
            )
            admission_failure = self._route_actor_admission(
                claim,
                task_id,
                operation=operation,
            )
            if admission_failure is not None:
                return admission_failure
            try:
                raw = _call_store_method(
                    _first_callable(store, "claim_route", "cps_claim_route"),
                    task_id=task_id,
                    actor_id=claim.actor_id,
                    episode=claim.episode,
                    route_key=route_key,
                    summary=summary,
                    ttl_seconds=ttl_seconds,
                    independent_verification_reason=independent_reason or None,
                    external_dedup_mode=claim.external_dedup_mode,
                    external_dedup_similarity_threshold=(
                        claim.external_dedup_similarity_threshold
                    ),
                    external_dedup_min_shared_tokens=claim.external_dedup_min_shared_tokens,
                    # In activity mode the route key is an opaque handle, not
                    # a semantic de-duplication key.  The CPS store still
                    # serializes the write and binds the lease to this actor /
                    # episode, so allowing another primary row is safe and
                    # auditable while preserving the legacy mode by default.
                    enforce_route_uniqueness=not claim.activity_feedback_enabled,
                    deadline_epoch_ms=claim.deadline_epoch_ms,
                    cancel_guard=lambda: _claim_cancelled(claim),
                )
            except ValueError as exc:
                return _control_result(
                    "INVALID_REQUEST",
                    _safe_error(exc),
                    retryable=False,
                )
            except Exception:
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            if not isinstance(raw, Mapping):
                # A route store that returns no envelope is an unavailable
                # coordination dependency, not a handled conflict.  Return
                # the explicit fail-open marker so the Pi write gate cannot
                # wait forever on a response that carries no claim state.
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            result = _safe_route_claim_result(raw)
            if _route_claim_result_is_malformed(result, operation=operation):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            claim_row = result.get("claim")
            status = str(result.get("status") or "").strip().lower()
            semantic_negative = _route_result_is_semantic_negative(result)
            # Even a handled negative must not echo a claim from another
            # task/actor/episode into the solver-visible response.  The
            # conflict owner belongs in the dedicated ``conflict`` field;
            # ``claim`` is always the caller's bound row (when present).
            if claim_row is not None and not _route_claim_row_matches_context(
                claim_row,
                task_id=task_id,
                actor_id=claim.actor_id,
                episode=claim.episode,
                route_key=route_key,
            ):
                # Some old adapters echo a sparse terminal marker containing
                # only ``claim_id/status``. It carries no cross-task data and
                # can remain a handled negative after dropping the row. If an
                # adapter supplies any identity-bearing fields, however, a
                # mismatch is a foreign projection and must fail open without
                # exposing it to the solver.
                identity_fields = ("task_id", "actor_id", "episode", "route_key")
                has_identity = any(field in claim_row for field in identity_fields)
                if semantic_negative and not has_identity:
                    result.pop("claim", None)
                else:
                    return self._route_claim_bypass(
                        claim,
                        operation,
                        reason="unavailable",
                    )
            claim_identity_required = bool(
                not semantic_negative
                and (
                    result.get("ok") is True
                    or result.get("acquired") is True
                    or result.get("claimed") is True
                    or (
                        isinstance(claim_row, Mapping)
                        and (
                            claim_row.get("active") is True
                            or str(claim_row.get("status") or "").strip().lower()
                            in {"active", "blocked"}
                        )
                    )
                )
            )
            if (
                claim_identity_required
                and claim_row is not None
                and not _route_claim_row_matches_context(
                    claim_row,
                    task_id=task_id,
                    actor_id=claim.actor_id,
                    episode=claim.episode,
                    route_key=route_key,
                )
            ):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            conflict_row = result.get("conflict")
            if conflict_row is not None and not _route_conflict_row_is_valid(
                conflict_row,
                task_id=task_id,
                route_key=route_key,
            ):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            if (
                conflict_row is not None
                and not _route_claim_row_matches_context(
                    conflict_row,
                    task_id=task_id,
                    route_key=route_key,
                )
                and (result.get("ok") is True or result.get("conflict") is not None)
            ):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            conflict = result.get("conflict") not in (None, False, "")
            # A positive secondary claim must identify itself as secondary;
            # conversely, a conflict owner must identify itself as the
            # primary.  Without these explicit markers an adapter can echo a
            # peer's row (or relabel a primary as an independent check) and
            # accidentally satisfy the write gate.
            if (
                claim_identity_required
                and isinstance(claim_row, Mapping)
                and status in {"active", "independent_verification"}
            ):
                claim_primary = _route_claim_primary_marker(claim_row)
                if claim_primary is None:
                    return self._route_claim_bypass(
                        claim,
                        operation,
                        reason="unavailable",
                    )
                secondary_response = status == "independent_verification"
                if (conflict or secondary_response) and claim_primary is not False:
                    return self._route_claim_bypass(
                        claim,
                        operation,
                        reason="unavailable",
                    )
                if not conflict and not secondary_response and claim_primary is not True:
                    return self._route_claim_bypass(
                        claim,
                        operation,
                        reason="unavailable",
                    )
            if (
                isinstance(conflict_row, Mapping)
                and status in {"active", "independent_verification"}
            ):
                conflict_primary = _route_claim_primary_marker(conflict_row)
                if conflict_primary is not True:
                    return self._route_claim_bypass(
                        claim,
                        operation,
                        reason="unavailable",
                    )
            # A route operation is considered acquired only when the store
            # explicitly says so.  In particular, ``ok=true`` alone is not
            # enough: older adapters use that bit for a handled conflict or
            # an accepted-but-not-acquired response.  This state is mirrored
            # by the Pi extension's write gate, so keeping the broker-side
            # latch equally strict prevents a future adapter from widening
            # the treatment by accident.
            # ``accepted`` is intentionally excluded: the sanitizer supplies
            # a compatibility default for that field when an older adapter
            # only returns ``ok=true``.  Only an explicit acquired/claimed bit
            # can satisfy the treatment gate.
            explicit_acquired = any(
                result.get(key) is True for key in ("acquired", "claimed")
            )
            bypassed = (
                result.get("bypassed") is True
                or status in {"route_claim_bypass", "route_claim_bypassed"}
            )
            # ``blocked`` is a visible lifecycle state, but it is not a
            # write-gate lease.  Only an explicitly active row can satisfy
            # the treatment; an independent verifier must receive the same
            # active status after the store accepts its reason.
            claim_is_active = (
                isinstance(claim_row, Mapping)
                and bool(claim_row.get("claim_id"))
                and str(claim_row.get("status") or "").strip().lower() == "active"
                and claim_row.get("active") is True
            )
            # Independent verification is still a successful route admission,
            # just a secondary one.  Require a positive envelope, an explicit
            # acquired/claimed bit, and an active claim row; an echoed reason
            # in an ``ok=false``/terminal response must never unlock writes.
            independent_accepted = (
                bool(independent_reason)
                and result.get("ok") is True
                and explicit_acquired
                and claim_is_active
                and not bypassed
                and (
                    result.get("independent_verification_accepted") is True
                    or status == "independent_verification"
                    or (
                        isinstance(claim_row, Mapping)
                        and claim_row.get("independent_verification_reason")
                        == independent_reason
                    )
                )
                and _route_claim_primary_marker(claim_row) is False
            )
            if isinstance(claim_row, Mapping):
                claim_id = _bounded_string(claim_row.get("claim_id"), 128)
                if claim_id and (
                    result.get("ok") is True
                    and explicit_acquired
                    and not bypassed
                    and claim_is_active
                    and status in {"active", "independent_verification"}
                    # A conflict requires broker/store evidence that the
                    # supplied reason was actually accepted.  Merely echoing
                    # the request (or returning an active row) is not enough
                    # to unlock the independent-verification path.
                    and (not conflict or independent_accepted)
                    and status not in {"blocked", "released", "done", "conflict", "route_conflict"}
                ):
                    with claim.lock:
                        claim.route_claim_ids.add(claim_id)
                        claim.route_claim_satisfied = True
            if independent_accepted:
                with claim.lock:
                    claim.route_claim_satisfied = True
                # This is broker-issued evidence, not an echo of an untrusted
                # adapter payload. The Pi extension treats it as a
                # convenience signal only after the positive checks above.
                result["independent_verification_accepted"] = True
            elif (
                not claim_is_active
                or conflict
                or status in {"blocked", "released", "done", "conflict", "route_conflict"}
            ):
                # Keep the handled response and conflict/blocked metadata
                # visible, but do not let an adapter's optimistic ``ok`` or
                # ``acquired`` bits tell the worker that it owns a writable
                # lease.  The Pi extension applies the same normalization.
                result["acquired"] = False
                result["claimed"] = False
                result["accepted"] = False
            return result
        if operation == "cps_update_route":
            admission_failure = self._route_actor_admission(
                claim,
                task_id,
                operation=operation,
            )
            if admission_failure is not None:
                return admission_failure
            claim_id = _required_string(payload.get("claim_id"), "claim_id", 128)
            status = _bounded_string(payload.get("status"), 64) or None
            if status is not None:
                status = status.lower()
                if status not in _ROUTE_UPDATE_STATUSES:
                    raise ValueError("status is not a valid route-claim update status")
            summary = (
                sanitize_public_text(
                    _bounded_string(payload.get("summary"), 1_000),
                    limit=1_000,
                )
                or None
            )
            ttl_value = payload.get("ttl_seconds")
            ttl_seconds = (
                None
                if ttl_value is None
                else _route_ttl_from_payload(ttl_value, default=claim.route_claim_ttl_seconds)
            )
            independent_reason = (
                sanitize_public_text(
                    _bounded_string(
                        payload.get("independent_verification_reason"),
                        1_000,
                    ),
                    limit=1_000,
                )
                or None
            )
            try:
                raw = _call_store_method(
                    _first_callable(
                        store,
                        "update_route_claim",
                        "update_route",
                        "cps_update_route",
                    ),
                    claim_id=claim_id,
                    actor_id=claim.actor_id,
                    task_id=task_id,
                    episode=claim.episode,
                    status=status,
                    summary=summary,
                    ttl_seconds=ttl_seconds,
                    independent_verification_reason=independent_reason,
                    deadline_epoch_ms=claim.deadline_epoch_ms,
                    cancel_guard=lambda: _claim_cancelled(claim),
                )
            except ValueError as exc:
                return _control_result(
                    "INVALID_REQUEST",
                    _safe_error(exc),
                    retryable=False,
                )
            except Exception:
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            if not isinstance(raw, Mapping):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            result = _safe_route_claim_result(raw)
            if _route_claim_result_is_malformed(result, operation=operation):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            # A handled identity/terminal negative proves that this requested
            # lease cannot be used for further writes.  Retire the local id
            # even when the adapter returns only a sparse ``not_found`` row;
            # retaining it would let a stale lease keep the treatment gate
            # open after an external finish, expiry, or ownership change.
            if _route_result_is_semantic_negative(result):
                self._clear_route_claim_state(claim, claim_id)
            returned_claim = result.get("claim")
            returned_claim_requires_binding = bool(
                not _route_result_is_semantic_negative(result)
                and (
                    result.get("ok") is True
                    or (
                        isinstance(returned_claim, Mapping)
                        and (
                            returned_claim.get("active") is True
                            or str(returned_claim.get("status") or "").strip().lower()
                            in {"active", "blocked"}
                        )
                    )
                )
            )
            if (
                returned_claim is not None
                and returned_claim_requires_binding
                and not _route_claim_row_matches_context(
                    returned_claim,
                    task_id=task_id,
                    actor_id=claim.actor_id,
                    episode=claim.episode,
                    claim_id=claim_id,
                )
            ):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            if (
                result.get("ok") is True
                and isinstance(returned_claim, Mapping)
                and _route_claim_row_matches_context(
                    returned_claim,
                    task_id=task_id,
                    actor_id=claim.actor_id,
                    episode=claim.episode,
                    claim_id=claim_id,
                )
            ):
                returned_status = str(returned_claim.get("status") or "").strip().lower()
                with claim.lock:
                    if returned_status == "active" and returned_claim.get("active") is True:
                        claim.route_claim_ids.add(claim_id)
                        claim.route_claim_satisfied = True
                    elif returned_status in _ROUTE_TERMINAL_STATUSES or returned_status == "blocked":
                        # ``blocked`` is a peer-visible coordination state, not
                        # a writable lease.  Clear it even when a compatibility
                        # adapter echoes ``active=true``; the Pi extension uses
                        # the same rule at its local gate.
                        claim.route_claim_ids.discard(claim_id)
                        claim.route_claim_satisfied = bool(claim.route_claim_ids)
                        # The update itself was accepted, but its result must
                        # not tell the solver it still holds write authority.
                        # Preserve ``ok``/``accepted`` as mutation outcome and
                        # normalize only the lease-authorization bits.
                        result["acquired"] = False
                        result["claimed"] = False
            return result
        if operation == "cps_release_route":
            admission_failure = self._route_actor_admission(
                claim,
                task_id,
                operation=operation,
            )
            if admission_failure is not None:
                return admission_failure
            claim_id = _required_string(payload.get("claim_id"), "claim_id", 128)
            status = _bounded_string(payload.get("status"), 64) or "released"
            status = status.lower()
            if status not in _ROUTE_RELEASE_STATUSES:
                raise ValueError("status is not a valid route-claim release status")
            reason = (
                sanitize_public_text(
                    _bounded_string(payload.get("reason"), 1_000),
                    limit=1_000,
                )
                or None
            )
            try:
                raw = _call_store_method(
                    _first_callable(
                        store,
                        "release_route_claim",
                        "release_route",
                        "cps_release_route",
                    ),
                    claim_id=claim_id,
                    actor_id=claim.actor_id,
                    task_id=task_id,
                    episode=claim.episode,
                    status=status,
                    reason=reason,
                    deadline_epoch_ms=claim.deadline_epoch_ms,
                    cancel_guard=lambda: _claim_cancelled(claim),
                )
            except ValueError as exc:
                return _control_result(
                    "INVALID_REQUEST",
                    _safe_error(exc),
                    retryable=False,
                )
            except Exception:
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            if not isinstance(raw, Mapping):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            result = _safe_route_claim_result(raw)
            if _route_claim_result_is_malformed(result, operation=operation):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            # Release is terminal by intent.  A bound or sparse handled
            # negative (including not_found/not_owner) must not leave the
            # session believing that its old local lease remains writable.
            if _route_result_is_semantic_negative(result):
                self._clear_route_claim_state(claim, claim_id)
            returned_claim = result.get("claim")
            returned_claim_requires_binding = bool(
                not _route_result_is_semantic_negative(result)
                and (
                    result.get("ok") is True
                    or (
                        isinstance(returned_claim, Mapping)
                        and (
                            returned_claim.get("active") is True
                            or str(returned_claim.get("status") or "").strip().lower()
                            in {"active", "blocked"}
                        )
                    )
                )
            )
            if (
                returned_claim is not None
                and returned_claim_requires_binding
                and not _route_claim_row_matches_context(
                    returned_claim,
                    task_id=task_id,
                    actor_id=claim.actor_id,
                    episode=claim.episode,
                    claim_id=claim_id,
                )
            ):
                return self._route_claim_bypass(
                    claim,
                    operation,
                    reason="unavailable",
                )
            # Only retire a locally tracked lease after the store has returned
            # the exact requested claim bound to this task/actor/episode and a
            # terminal status.  Discarding before validation would let a
            # malformed/foreign response silently alter the broker's local
            # capability accounting.
            if (
                result.get("ok") is True
                and isinstance(returned_claim, Mapping)
                and _route_claim_row_matches_context(
                    returned_claim,
                    task_id=task_id,
                    actor_id=claim.actor_id,
                    episode=claim.episode,
                    claim_id=claim_id,
                )
                and str(returned_claim.get("status") or "").strip().lower()
                in _ROUTE_TERMINAL_STATUSES
                and returned_claim.get("active") is False
            ):
                with claim.lock:
                    claim.route_claim_ids.discard(claim_id)
                    claim.route_claim_satisfied = bool(claim.route_claim_ids)
            return result
        if operation == "cps_send":
            body = _required_string(payload.get("body"), "body", 8_000)
            recipient = _bounded_string(payload.get("recipient"), 256) or None
            scope = _scope(payload.get("scope"), claim.communication)
            item = store.send_message(
                task_id="__global__" if scope == "global" else task_id,
                sender=claim.actor_id,
                recipient=recipient,
                body=body,
                deadline_epoch_ms=claim.deadline_epoch_ms,
                cancel_guard=lambda: _claim_cancelled(claim),
            )
            return {"ok": True, "message": _safe_message(item)}
        if operation == "cps_inbox":
            limit = _bounded_limit(payload.get("limit"), default=8, maximum=8)
            return {
                "ok": True,
                "messages": [
                    _safe_message(item)
                    for item in store.inbox(
                        task_id=task_id,
                        recipient=claim.actor_id,
                        limit=limit,
                        include_global=claim.communication == "hybrid",
                    )
                ],
            }
        if operation == "cps_ack":
            message_id = _required_string(payload.get("message_id"), "message_id", 64)
            visible = store.inbox(
                task_id=task_id,
                recipient=claim.actor_id,
                limit=50,
                include_global=claim.communication == "hybrid",
            )
            if not any(str(item.get("id")) == message_id for item in visible):
                return {"ok": False, "status": "MESSAGE_NOT_VISIBLE", "acked": False}
            return {
                "ok": True,
                "acked": store.ack_message(
                    message_id,
                    claim.actor_id,
                    deadline_epoch_ms=claim.deadline_epoch_ms,
                    cancel_guard=lambda: _claim_cancelled(claim),
                ),
            }
        if operation == "cps_feedback":
            selection_store = claim.selection_store
            if not claim.selection_enabled or selection_store is None:
                return _control_result(
                    "CPS_CAPABILITY_DENIED",
                    "This solver session has no selection-feedback capability.",
                    retryable=False,
                )
            request_key = _required_string(payload.get("request_key"), "request_key", 512)
            exposure_item_id = _required_string(
                payload.get("exposure_item_id"), "exposure_item_id", 512
            )
            trace_id = _required_string(payload.get("trace_id"), "trace_id", 512)
            feedback_kind = _required_string(
                payload.get("feedback_kind"), "feedback_kind", 64
            )
            if feedback_kind not in CANONICAL_FEEDBACK_KINDS:
                return _control_result(
                    "INVALID_REQUEST",
                    "feedback_kind is not part of the registered feedback contract.",
                    retryable=False,
                )
            feedback_payload: dict[str, Any] = {}
            if "value" in payload:
                value = payload.get("value")
                if not _is_bounded_feedback_value(value):
                    return _control_result(
                        "INVALID_REQUEST",
                        "value must be a bounded JSON scalar.",
                        retryable=False,
                    )
                feedback_payload["value"] = value
            if "note" in payload:
                feedback_payload["note"] = _required_string(
                    payload.get("note"), "note", 2_000
                )
            try:
                recorded = selection_store.record_feedback(
                    request_key=request_key,
                    exposure_item_id=exposure_item_id,
                    actor_id=claim.actor_id,
                    trace_id=trace_id,
                    feedback_kind=feedback_kind,
                    origin="worker_explicit",
                    terminal=True,
                    payload=feedback_payload,
                )
            except ValueError:
                return _control_result(
                    "INVALID_FEEDBACK",
                    "Feedback could not be bound to this solver's selected exposure.",
                    retryable=False,
                )
            return {
                "ok": True,
                "status": str(recorded.get("status") or "RECORDED")[:64],
                "feedback_event_id": _bounded_string(
                    recorded.get("feedback_event_id"), 512
                ),
                "effective": recorded.get("effective") is True,
                "idempotent": recorded.get("idempotent") is True,
                "conflicts_with_feedback_event_id": _bounded_string(
                    recorded.get("conflicts_with_feedback_event_id"), 512
                )
                or None,
            }
        return _control_result("UNKNOWN_CPS_OPERATION", "Unknown CPS operation.", retryable=False)

    def _audit(
        self,
        claim: _SessionClaim,
        task_id: str,
        result: Mapping[str, Any],
        *,
        accepted: bool,
        call_index: int | None = None,
        gate_wait_seconds: float = 0.0,
        elapsed_seconds: float = 0.0,
        candidate_sha256: str | None = None,
        task_contract_sha256: Any = None,
        judge_job_id: Any = None,
        cache_reused: bool = False,
    ) -> None:
        failure_fields = _audit_failure_fields(result.get("response"))
        response = result.get("response")
        safe_response = response if isinstance(response, Mapping) else {}
        row = {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event": "judge_check",
            "actor_id": claim.actor_id,
            "task_id": task_id,
            "accepted": accepted,
            "call_index": call_index,
            "status": str(result.get("status") or "UNKNOWN")[:120],
            "proved": result.get("proved") is True,
            "retryable": result.get("retryable") is True,
            "gate_wait_seconds": round(max(0.0, gate_wait_seconds), 6),
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 6),
            "candidate_sha256": candidate_sha256,
            "task_contract_sha256": _safe_hash(task_contract_sha256),
            "judge_job_id": _safe_identifier(judge_job_id),
            "cache_reused": bool(cache_reused),
            "probe_cache_reused": _nested_response_bool(
                safe_response, "probe_cache_reused"
            ),
            "remote_cache_reused": _nested_response_bool(
                safe_response, "cache_reused"
            ),
            "response_profile": LEAN_PROBE_RESPONSE_PROFILE,
            **failure_fields,
        }
        with self._audit_lock:
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def _send_json(
        handler: BaseHTTPRequestHandler, status: int, payload: Mapping[str, Any]
    ) -> None:
        raw = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(raw)


def _select_task_id(claim: _SessionClaim, raw: Any) -> str:
    requested = _bounded_string(raw, 256)
    if requested:
        if requested not in claim.candidates:
            raise ValueError("task_id is not part of this solver session")
        return requested
    if len(claim.candidates) != 1:
        raise ValueError("task_id is required for a multi-task solver session")
    return next(iter(claim.candidates))


def _claim_cancelled(claim: _SessionClaim) -> bool:
    return claim.revoked_event.is_set() or (
        claim.cancel_event is not None and claim.cancel_event.is_set()
    )


def _cps_capability_failure(claim: _SessionClaim) -> dict[str, Any] | None:
    """Return a stable fail-closed result for an expired CPS capability."""

    if (
        time.monotonic() >= claim.deadline_monotonic
        or int(time.time() * 1_000) >= claim.deadline_epoch_ms
    ):
        return _control_result(
            "OUT_OF_HORIZON",
            "The CPS communication horizon has elapsed.",
            retryable=False,
        )
    if _claim_cancelled(claim):
        return _control_result(
            "TASK_CANCELLED",
            "This solver task no longer accepts CPS work.",
            retryable=False,
        )
    return None


def _has_unsettled_remote_cancellation(response: Mapping[str, Any]) -> bool:
    cancellation = response.get("judge_cancellation")
    return (
        isinstance(cancellation, Mapping)
        and cancellation.get("attempted") is True
        and cancellation.get("settled") is not True
    )


def _nested_response_bool(response: Mapping[str, Any], name: str) -> bool:
    """Read one boolean through the bounded Judge ``response`` envelope chain."""

    current: Any = response
    for _depth in range(4):
        if not isinstance(current, Mapping):
            return False
        if current.get(name) is True:
            return True
        current = current.get("response")
    return False


def _has_unsettled_remote_work(
    verdict_status: str,
    response: Mapping[str, Any],
) -> bool:
    return (
        verdict_status == "REMOTE_SETTLEMENT_UNCONFIRMED"
        or response.get("remote_settlement_unconfirmed") is True
        or response.get("settlement_error") == "cancel_settlement_unconfirmed"
        or _has_unsettled_remote_cancellation(response)
    )


def _has_deferred_remote_work(
    verdict_status: str,
    response: Mapping[str, Any],
) -> bool:
    cancellation = response.get("judge_cancellation")
    return (
        response.get("settlement_error") == "cancel_settlement_deferred"
        or (
            isinstance(cancellation, Mapping)
            and cancellation.get("deferred") is True
        )
    )


def _accepts_settlement_callback(function: Callable[..., Any]) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "settlement_callback"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _remote_settlement_control_result() -> dict[str, Any]:
    return _control_result(
        "REMOTE_SETTLEMENT_UNCONFIRMED",
        "Remote Judge work did not provide a job-bound terminal receipt; further admission is disabled.",
        retryable=False,
    )


def _nonnegative_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _accepts_cancel_event(function: Callable[..., Any]) -> bool:
    """Preserve compatibility with narrow test/custom evaluator adapters."""

    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "cancel_event"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _scope(raw: Any, communication: str) -> str:
    scope = _bounded_string(raw, 16).lower() or "task"
    if scope not in {"task", "global"}:
        raise ValueError("scope must be task or global")
    if scope == "global" and communication != "hybrid":
        raise ValueError("global CPS scope requires hybrid communication")
    return scope


def _bounded_string(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("text fields must be strings")
    text = value.strip()
    if len(text) > maximum:
        raise ValueError(f"text field exceeds {maximum} characters")
    return text


def _required_string(value: Any, name: str, maximum: int) -> str:
    text = _bounded_string(value, maximum)
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _is_bounded_feedback_value(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str) and len(value) <= 2_000


def _safe_selection_search_response(raw: Any, *, limit: int) -> dict[str, Any]:
    """Bound the narrow ``selection_search(claim, query, limit)`` response."""

    if not isinstance(raw, Mapping):
        return _control_result(
            "BROKER_ERROR",
            "The controlled selection search returned an invalid response.",
            retryable=False,
        )
    items = raw.get("items", [])
    if not isinstance(items, (list, tuple)):
        return _control_result(
            "BROKER_ERROR",
            "The controlled selection search returned an invalid response.",
            retryable=False,
        )
    safe_items = [_bounded_json(item) for item in items[:limit]]
    result: dict[str, Any] = {"ok": True, "items": safe_items}
    for key in ("search_event_id", "exposure_id", "request_key"):
        value = raw.get(key)
        if isinstance(value, str) and len(value) <= 512:
            result[key] = value
    return result


def _bounded_json(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-safe bounded value for callback-owned selection rows."""

    if _depth >= 4:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:2_000]
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: _bounded_json(item, _depth=_depth + 1)
            for key, item in list(value.items())[:64]
            if isinstance(key, (str, int, float, bool))
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_json(item, _depth=_depth + 1) for item in value[:100]]
    return None


def _bounded_limit(value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("limit must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    return max(1, min(parsed, maximum))


def _control_result(
    status: str,
    message: str,
    *,
    retryable: bool,
    retry_after_seconds: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "accepted": False,
        "status": status,
        "message": message,
        "retryable": retryable,
    }
    if retry_after_seconds is not None:
        result["retry_after_seconds"] = retry_after_seconds
    return result


def _safe_roster(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in payload[:500]:
        if not isinstance(raw, Mapping):
            continue
        item = {
            key: raw[key]
            for key in ("actor_id", "task_id", "episode")
            if isinstance(raw.get(key), (str, int)) and not isinstance(raw.get(key), bool)
        }
        if item:
            result.append(item)
    return result


class _RouteClaimStoreUnavailable(RuntimeError):
    """Raised internally when an optional route-claim method is absent."""


def _first_callable(store: Any, *names: str) -> Callable[..., Any]:
    """Resolve the first compatible CPS method without exposing its details."""

    for name in names:
        method = getattr(store, name, None)
        if callable(method):
            return method
    joined = ", ".join(names)
    raise _RouteClaimStoreUnavailable(f"missing CPS route method: {joined}")


def _call_store_method(method: Callable[..., Any], **kwargs: Any) -> Any:
    """Call a CPS method while tolerating older narrow test doubles.

    Route claims were introduced after the original CPS API.  A sibling
    worktree or a focused harness may expose the same core method without the
    optional deadline/cancellation/limit keywords.  Filter only unsupported
    keyword names based on the signature; errors raised by the method itself
    still propagate to the caller and become an explicit fail-open bypass.
    """

    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return method(**kwargs)
    accepts_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_var_kwargs:
        return method(**kwargs)
    filtered = {
        key: value
        for key, value in kwargs.items()
        if key in parameters
    }
    return method(**filtered)


def _normalize_route_claim_ttl(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("route claim TTL must be a finite positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("route claim TTL must be a finite positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("route claim TTL must be a finite positive number")
    return min(_MAX_ROUTE_CLAIM_TTL_SECONDS, max(1.0, parsed))


def _route_ttl_from_payload(value: Any, *, default: float) -> float:
    if value is None:
        return _normalize_route_claim_ttl(default)
    # Tool schemas advertise an integer lease duration, but callers can still
    # POST directly to the loopback broker.  Keep the wire contract strict so
    # a fractional/boolean value cannot silently alter expiry semantics.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("route claim TTL must be an integer")
    return _normalize_route_claim_ttl(value)


def _bounded_route_bypass_reason(value: Any) -> str:
    text = str(value or "unavailable").strip().lower()
    # Keep this an enum-like public field.  Never reflect exception text,
    # filesystem paths, or provider details into solver-visible responses.
    if text not in {"unavailable", "error", "expired", "cancelled"}:
        return "unavailable"
    return text


def _safe_actor(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, maximum in (
        ("actor_id", 256),
        ("task_id", 256),
        ("status", 64),
        ("created_at", 64),
        ("updated_at", 64),
        ("finished_at", 64),
        ("last_heartbeat_at", 64),
    ):
        value = raw.get(key)
        if isinstance(value, str):
            result[key] = value[:maximum]
    for key in ("episode",):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
    active_present = "active" in raw
    active = raw.get("active")
    status_value = str(result.get("status") or "").strip().lower()
    # An explicitly supplied non-boolean active bit is malformed state, not an
    # omitted compatibility field.  Preserve that fact as ``None`` so the
    # admission/projection validators can fail open instead of deriving a
    # truthy value from status.  A terminal status is authoritative only when
    # the adapter omitted the active bit or supplied a real boolean; a stale
    # ``active=true`` flag must never resurrect a finished actor.
    if active_present and not isinstance(active, bool):
        result["active"] = None
    elif status_value in (_ACTOR_TERMINAL_STATUSES | {"closing"}):
        result["active"] = False
    elif status_value not in _ACTOR_LIVE_STATUSES:
        # Unknown lifecycle labels are not trusted as live admissions. Preserve
        # an explicit non-boolean marker so the route validators fail open.
        result["active"] = None
    elif isinstance(active, bool):
        result["active"] = active
    else:
        result["active"] = True
    return result


def _is_row_collection(
    raw: Any,
    *,
    keys: tuple[str, ...],
    required_fields: tuple[str, ...] = (),
) -> bool:
    """Recognize a well-shaped adapter row collection before sanitizing it.

    Empty collections are valid (there are simply no peers/routes yet), but a
    non-empty collection containing ``None``/``{}`` is an adapter failure.  Do
    this validation before the sanitizers drop malformed rows, otherwise an
    unavailable roster could look like a successful empty discovery result.
    """

    collection: Any = raw
    if isinstance(raw, Mapping):
        present = [key for key in keys if key in raw]
        # The aliases are alternatives, not mergeable sources.  If an adapter
        # returns two of them, selecting whichever happens to appear first can
        # hide a foreign row (or silently drop a live one), so fail closed.
        if len(present) > 1:
            return False
        collection = next(
            (
                raw.get(key)
                for key in keys
                if key in raw and isinstance(raw.get(key), (list, tuple))
            ),
            None,
        )
    if not isinstance(collection, (list, tuple)):
        return False
    if not required_fields or not collection:
        return all(isinstance(item, Mapping) for item in collection)
    for item in collection:
        if not isinstance(item, Mapping):
            return False
        for field in required_fields:
            value = item.get(field)
            if isinstance(value, bool) or value is None or not str(value).strip():
                return False
    return True


def _safe_actor_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        raw = raw.get("actors", raw.get("items", []))
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in (_safe_actor(row) for row in raw[:500]) if item]


def _live_actor_rows(rows: Any) -> list[dict[str, Any]]:
    """Project only admitted, nonterminal actors to solver callers."""

    if not isinstance(rows, (list, tuple)):
        return []
    return [
        row
        for row in rows
        if row.get("active") is True
        and str(row.get("status") or "").strip().lower()
        not in _ACTOR_TERMINAL_STATUSES
    ]


def _actor_rows_match_task(rows: Any, *, task_id: str) -> bool:
    """Reject a roster projection containing another task's actors."""

    if not isinstance(rows, (list, tuple)):
        return False
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        observed_task = row.get("task_id")
        if not isinstance(observed_task, str) or not observed_task.strip():
            return False
        if observed_task != str(task_id):
            return False
        observed_episode = row.get("episode")
        if isinstance(observed_episode, bool) or not isinstance(observed_episode, int):
            return False
        if not isinstance(row.get("active"), bool):
            return False
    return True


def _safe_route_claim(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, maximum in (
        ("claim_id", 128),
        ("task_id", 256),
        ("actor_id", 256),
        ("route_key", 512),
        ("summary", 1_000),
        ("status", 64),
        ("created_at", 64),
        ("updated_at", 64),
        ("expires_at", 64),
        ("released_at", 64),
        ("release_reason", 512),
        ("independent_verification_reason", 1_000),
        ("route_key_semantics", 32),
        ("activity_description", 1_000),
    ):
        value = raw.get(key)
        if isinstance(value, str):
            if key in {
                "route_key",
                "summary",
                "activity_description",
                "release_reason",
                "independent_verification_reason",
            }:
                sanitized = sanitize_public_text(value, limit=maximum)
                result[key] = (
                    " ".join(sanitized.split())
                    if key in {"summary", "activity_description"}
                    else sanitized
                )
            else:
                result[key] = value[:maximum]
    for key in ("episode",):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
    active_present = "active" in raw
    for key in (
        "is_primary",
        "primary",
        "active",
        "independent_verification",
    ):
        value = raw.get(key)
        if isinstance(value, bool):
            result[key] = value
    # Normalize the minimal row shape used by older adapters.  The canonical
    # CPS store already supplies these aliases, but deriving them here keeps
    # the public route view stable without exposing adapter-specific details.
    if "is_primary" in result:
        result.setdefault("primary", result["is_primary"])
    status_value = str(result.get("status") or "").strip().lower()
    if active_present and not isinstance(raw.get("active"), bool):
        # Do not silently derive a boolean from status when an adapter sent a
        # malformed value such as ``"false"``.  Callers reject this explicit
        # ``None`` at the trust boundary.
        result["active"] = None
    elif status_value and status_value not in {"active", "blocked"}:
        # Never trust an echoed active bit for a terminal claim.
        result["active"] = False
    elif "active" not in result and isinstance(result.get("status"), str):
        result["active"] = True
    if "independent_verification" not in result:
        result["independent_verification"] = bool(
            str(result.get("independent_verification_reason") or "").strip()
        )
    semantics = str(result.get("route_key_semantics") or "unique").strip().lower()
    result["route_key_semantics"] = (
        semantics if semantics in {"unique", "opaque"} else "unique"
    )
    result.setdefault("activity_description", result.get("summary", ""))
    return result


def _safe_route_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        raw = raw.get("routes", raw.get("claims", raw.get("items", [])))
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in (_safe_route_claim(row) for row in raw[:500]) if item]


def _live_route_rows(rows: Any) -> list[dict[str, Any]]:
    """Hide terminal/expired claims from the active-route projection.

    ``blocked`` remains visible as a coordination fact, but it is deliberately
    not a write-gate lease (the claim handler applies that stricter rule).
    """

    if not isinstance(rows, (list, tuple)):
        return []
    return [
        row
        for row in rows
        if str(row.get("status") or "").strip().lower() in _ROUTE_VISIBLE_STATUSES
        and row.get("active") is not False
    ]


def _route_claim_row_matches_context(
    row: Any,
    *,
    task_id: str,
    actor_id: str | None = None,
    episode: int | None = None,
    route_key: str | None = None,
    claim_id: str | None = None,
) -> bool:
    """Verify a broker-returned claim belongs to this runner session.

    Route claims are runner-owned capability state.  A legacy/buggy adapter
    must not be able to return a valid-looking claim from another task, actor,
    or episode and thereby unlock the Pi write gate.  The canonical schema
    always carries all three identity fields; omission is therefore treated as
    an invalid response rather than guessed from the request.
    """

    if not isinstance(row, Mapping):
        return False
    observed_task = row.get("task_id")
    observed_actor = row.get("actor_id")
    if not isinstance(observed_task, str) or not observed_task.strip():
        return False
    if not isinstance(observed_actor, str) or not observed_actor.strip():
        return False
    if observed_task != str(task_id) or (
        actor_id is not None and observed_actor != str(actor_id)
    ):
        return False
    if route_key is not None:
        observed_route = row.get("route_key")
        if not isinstance(observed_route, str) or observed_route != str(route_key):
            return False
    if claim_id is not None:
        observed_claim = row.get("claim_id")
        if not isinstance(observed_claim, str) or observed_claim != str(claim_id):
            return False
    if episode is not None:
        observed_episode = row.get("episode")
        if isinstance(observed_episode, bool) or not isinstance(observed_episode, int):
            return False
        if observed_episode != int(episode):
            return False
    return True


def _route_claim_primary_marker(row: Any) -> bool | None:
    """Return a canonical primary/secondary marker, or ``None`` if ambiguous."""

    if not isinstance(row, Mapping):
        return None
    observed: list[bool] = []
    for key in ("is_primary", "primary"):
        if key not in row:
            continue
        value = row.get(key)
        if not isinstance(value, bool):
            return None
        observed.append(value)
    if not observed or any(value != observed[0] for value in observed[1:]):
        return None
    return observed[0]


def _route_conflict_row_is_valid(
    row: Any,
    *,
    task_id: str,
    route_key: str,
) -> bool:
    """Validate the bounded primary row returned as conflict evidence."""

    if not _route_claim_row_matches_context(
        row,
        task_id=task_id,
        route_key=route_key,
    ):
        return False
    if not isinstance(row, Mapping):
        return False
    actor_id = row.get("actor_id")
    episode = row.get("episode")
    status = str(row.get("status") or "").strip().lower()
    return (
        isinstance(actor_id, str)
        and bool(actor_id.strip())
        and isinstance(episode, int)
        and not isinstance(episode, bool)
        and status in _ROUTE_VISIBLE_STATUSES
        and row.get("active") is True
        and _route_claim_primary_marker(row) is True
    )


def _route_result_has_unknown_diagnostic(result: Mapping[str, Any]) -> bool:
    """Reject unbounded adapter diagnostics at the route capability boundary."""

    known = _ROUTE_CLAIM_NEGATIVE_STATUSES | _ROUTE_CLAIM_ERROR_STATUSES
    for key in ("error", "reason"):
        value = str(result.get(key) or "").strip().lower()
        if value and value not in known:
            return True
    return False


def _safe_external_dedup_overlaps(raw: Any) -> list[dict[str, Any]] | None:
    """Validate the bounded, runner-owned overlap projection."""

    if not isinstance(raw, (list, tuple)):
        return None
    result: list[dict[str, Any]] = []
    for item in raw[:8]:
        if not isinstance(item, Mapping):
            return None
        relation = item.get("relation")
        score = item.get("score")
        shared = item.get("shared_tokens")
        claim_id = item.get("compared_claim_id")
        actor_id = item.get("compared_actor_id")
        if (
            not isinstance(relation, str)
            or relation not in {"same_route", "related"}
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0.0 <= float(score) <= 1.0
            or not isinstance(shared, (list, tuple))
            or not all(isinstance(token, str) for token in shared[:12])
            or not isinstance(claim_id, str)
            or not isinstance(actor_id, str)
        ):
            return None
        result.append(
            {
                "relation": relation,
                "score": round(float(score), 6),
                "shared_tokens": [token[:64] for token in shared[:12]],
                "compared_claim_id": claim_id[:128],
                "compared_actor_id": actor_id[:128],
            }
        )
    return result


def _route_rows_match_task(rows: Any, *, task_id: str) -> bool:
    """Reject a route projection that contains cross-task or sparse rows."""

    if not isinstance(rows, (list, tuple)):
        return False
    for row in rows:
        if not _route_claim_row_matches_context(row, task_id=task_id):
            return False
        episode = row.get("episode")
        if isinstance(episode, bool) or not isinstance(episode, int):
            return False
        if not isinstance(row.get("active"), bool):
            return False
        status = str(row.get("status") or "").strip().lower()
        if status not in (_ROUTE_VISIBLE_STATUSES | _ROUTE_TERMINAL_STATUSES):
            return False
    return True


def _safe_route_claim_result(raw: Any) -> dict[str, Any]:
    """Sanitize one CPS route response while preserving conflict semantics."""

    if not isinstance(raw, Mapping):
        return _control_result(
            "ROUTE_CLAIM_ERROR",
            "The route-claim store returned an invalid response.",
            retryable=False,
        )
    # Envelope booleans are protocol fields, not loose truthy hints.  If an
    # adapter sends e.g. ``acquired="true"`` or ``bypassed=1``, dropping the
    # malformed value and deriving a positive result from another field could
    # accidentally unlock the worker write gate.  Return a minimal, known
    # protocol error before copying any nested row (which also avoids exposing
    # a foreign adapter projection in the error response).
    boolean_fields = (
        "ok",
        "accepted",
        "acquired",
        "claimed",
        "idempotent",
        "bypassed",
        "independent_verification_accepted",
    )
    invalid_types = any(
        key in raw and not isinstance(raw.get(key), bool)
        for key in boolean_fields
    )
    for key in ("status", "error", "reason", "route_claim_bypass_reason"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            invalid_types = True
    if "switch_required" in raw and not isinstance(raw.get("switch_required"), bool):
        invalid_types = True
    if "dedup_mode" in raw and (
        not isinstance(raw.get("dedup_mode"), str)
        or raw.get("dedup_mode", "").strip().lower()
        not in {"off", "advisory", "enforce"}
    ):
        invalid_types = True
    dedup_overlaps = None
    if "dedup_overlaps" in raw:
        dedup_overlaps = _safe_external_dedup_overlaps(raw.get("dedup_overlaps"))
        if dedup_overlaps is None:
            invalid_types = True
    conflict_value = raw.get("conflict")
    # ``false`` is a compact compatibility spelling for "no conflict";
    # ``true`` is not a conflict row and must never be allowed to stand in for
    # one.  Without this check a secondary claim could pair an untyped
    # ``conflict=true`` bit with its own active row and satisfy the independent
    # verification gate without any peer-owner evidence.
    if conflict_value is True or (
        conflict_value is not None
        and not isinstance(conflict_value, (Mapping, bool))
    ):
        invalid_types = True
    for key in ("claim", "owner"):
        value = raw.get(key)
        if value is not None and not isinstance(value, Mapping):
            invalid_types = True
    if invalid_types:
        return {
            "ok": False,
            "accepted": False,
            "acquired": False,
            "claimed": False,
            "bypassed": False,
            "status": "invalid_response",
            "error": "invalid_response",
        }
    # Positive envelope bits are redundant by design, so contradictory values
    # are protocol corruption rather than a meaningful partial response.  A
    # required treatment must fail open on these shapes instead of choosing
    # whichever bit happens to be checked first by a caller.
    acquired = raw.get("acquired")
    claimed = raw.get("claimed")
    if (
        isinstance(acquired, bool)
        and isinstance(claimed, bool)
        and acquired != claimed
    ):
        return {
            "ok": False,
            "accepted": False,
            "acquired": False,
            "claimed": False,
            "bypassed": False,
            "status": "invalid_response",
            "error": "invalid_response",
        }
    positive_bits = ("acquired", "claimed", "independent_verification_accepted")
    if raw.get("ok") is False and any(raw.get(key) is True for key in positive_bits):
        return {
            "ok": False,
            "accepted": False,
            "acquired": False,
            "claimed": False,
            "bypassed": False,
            "status": "invalid_response",
            "error": "invalid_response",
        }
    if raw.get("accepted") is False and any(raw.get(key) is True for key in positive_bits):
        return {
            "ok": False,
            "accepted": False,
            "acquired": False,
            "claimed": False,
            "bypassed": False,
            "status": "invalid_response",
            "error": "invalid_response",
        }
    result: dict[str, Any] = {}
    for key in (
        "ok",
        "accepted",
        "acquired",
        "claimed",
        "conflict",
        "idempotent",
        "bypassed",
        "independent_verification_accepted",
        "switch_required",
    ):
        value = raw.get(key)
        if isinstance(value, bool):
            result[key] = value
    status = raw.get("status")
    if isinstance(status, str):
        result["status"] = status[:64]
    dedup_mode = raw.get("dedup_mode")
    if isinstance(dedup_mode, str) and dedup_mode.strip().lower() in {
        "off",
        "advisory",
        "enforce",
    }:
        result["dedup_mode"] = dedup_mode.strip().lower()
    if dedup_overlaps is not None:
        result["dedup_overlaps"] = dedup_overlaps
    raw_status = str(status or "").strip().lower()
    explicit_bypass = raw.get("bypassed") is True or raw_status in {
        "route_claim_bypass",
        "route_claim_bypassed",
    }
    raw_bypass_reason = raw.get("route_claim_bypass_reason")
    # A route adapter cannot mint a fail-open marker by merely echoing a
    # string.  Only the broker's explicit bypass envelope is allowed to carry
    # that field across the capability boundary; a stray marker is treated as
    # a malformed response below.
    stray_bypass_marker = (
        isinstance(raw_bypass_reason, str) and not explicit_bypass
    )
    for key in ("message", "reason", "error"):
        value = raw.get(key)
        if isinstance(value, str):
            result[key] = (
                sanitize_public_text(value, limit=2_000)
                if key == "message"
                else value[:2_000]
            )
    if explicit_bypass and isinstance(raw_bypass_reason, str):
        result["route_claim_bypass_reason"] = raw_bypass_reason[:2_000]
    elif stray_bypass_marker:
        result["status"] = "invalid_response"
        result["error"] = "invalid_response"
    claim = _safe_route_claim(raw.get("claim"))
    if claim:
        result["claim"] = claim
    conflict = _safe_route_claim(raw.get("conflict"))
    if conflict:
        result["conflict"] = conflict
    owner = _safe_actor(raw.get("owner"))
    if owner:
        result["owner"] = owner
    # Some CPS implementations return the claim row directly.  Preserve it
    # under ``claim`` only when it has an unmistakable claim identifier.
    if not claim and isinstance(raw.get("claim_id"), str):
        direct = _safe_route_claim(raw)
        if direct:
            result["claim"] = direct
            claim = direct
    if "status" not in result and isinstance(claim, Mapping):
        claim_status = claim.get("status")
        if isinstance(claim_status, str) and claim_status.strip():
            result["status"] = claim_status[:64]
    # Preserve a small, value-only compatibility projection at the envelope
    # root.  The Pi extension consumes these fields without knowing the
    # adapter's nested-row shape (in particular for independent verification
    # and claim-id closeout); never copy arbitrary raw keys across the broker
    # boundary.
    if isinstance(claim, Mapping):
        for key in (
            "claim_id",
            "task_id",
            "actor_id",
            "episode",
            "route_key",
            "summary",
            "independent_verification_reason",
            "route_key_semantics",
            "activity_description",
            "is_primary",
            "primary",
            "active",
            "independent_verification",
            "release_reason",
        ):
            if key not in result and key in claim:
                value = claim[key]
                if isinstance(value, (str, bool)) or (
                    isinstance(value, int) and not isinstance(value, bool)
                ):
                    result[key] = value
    if "ok" not in result:
        result["ok"] = bool(result.get("acquired") or result.get("claimed"))
    result.setdefault("accepted", result.get("ok") is True)
    # Make the fail-open distinction explicit in every sanitized envelope;
    # callers should never have to infer that an omitted field means a real
    # claim rather than a bypass.
    result.setdefault("bypassed", False)
    if explicit_bypass:
        # Normalize the fail-open envelope at the trust boundary.  A missing,
        # blank, or unknown reason is still an unavailable route dependency;
        # never let an adapter-provided string block the worker indefinitely.
        result["bypassed"] = True
        result["status"] = "route_claim_bypassed"
        result["route_claim_bypass_reason"] = _bounded_route_bypass_reason(
            raw_bypass_reason
        )
        for key in (
            "ok",
            "accepted",
            "acquired",
            "claimed",
            "independent_verification_accepted",
        ):
            result[key] = False
    # Treat a contradictory positive envelope as the semantic negative it
    # names.  A buggy adapter must not turn `ok=true, acquired=true,
    # error=not_owner` (possibly echoing a peer's active row) into a lease
    # that satisfies the worker write gate.  Keep the negative response
    # visible for deliberate caller handling; only malformed/outage shapes
    # are converted to the explicit fail-open marker by the caller.
    if _route_result_is_semantic_negative(result) or _route_result_is_error(result):
        for key in ("ok", "accepted", "acquired", "claimed", "independent_verification_accepted"):
            result[key] = False
    return result


_ROUTE_CLAIM_NEGATIVE_STATUSES = frozenset(
    {
        "conflict",
        "route_conflict",
        "not_admitted",
        "actor_finished",
        "episode_mismatch",
        "not_found",
        "claim_terminal",
        "not_owner",
        "invalid_request",
        "invalid_task_selection",
        "actor_not_admitted",
        "invalid_actor_status",
        "semantic_conflict",
        "semantic_route_conflict",
        "expired",
        "released",
        "done",
        "closed",
        "finished",
    }
)
_ROUTE_CLAIM_ERROR_STATUSES = frozenset(
    {
        "route_claim_error",
        "broker_error",
        "invalid_response",
        "malformed",
        "error",
        "failed",
        "failure",
        "unavailable",
        "timeout",
        "timed_out",
        "cancelled",
        "canceled",
    }
)


def _route_result_is_semantic_negative(result: Mapping[str, Any]) -> bool:
    """Return whether a route response is a handled negative, not an outage."""

    for key in ("status", "error", "reason"):
        value = str(result.get(key) or "").strip().lower()
        if value in _ROUTE_CLAIM_NEGATIVE_STATUSES:
            return True
    return False


def _route_result_is_error(result: Mapping[str, Any]) -> bool:
    """Return whether a route envelope names a transport/protocol failure."""

    for key in ("status", "error", "reason"):
        value = str(result.get(key) or "").strip().lower()
        if value in _ROUTE_CLAIM_ERROR_STATUSES:
            return True
    return False


def _route_claim_result_is_malformed(
    result: Mapping[str, Any],
    *,
    operation: str,
) -> bool:
    """Detect adapter responses that cannot account for route state.

    A malformed response is treated like a route-store outage and therefore
    takes the explicit fail-open path.  Handled semantic negatives (conflict,
    stale actor, not-owner, and so on) remain visible to the solver so it can
    choose another route or retry deliberately.
    """

    status = str(result.get("status") or "").strip().lower()
    if result.get("bypassed") is True or status in {
        "route_claim_bypass",
        "route_claim_bypassed",
    }:
        return False
    # ``error``/``reason`` are enum-like protocol diagnostics.  An arbitrary
    # positive string is not harmless metadata: it may be an adapter's stale
    # exception or a cross-session message, so fail open explicitly rather
    # than allowing the accompanying ``ok/acquired`` bits to unlock writes.
    if _route_result_has_unknown_diagnostic(result):
        return True
    # A known semantic rejection (including one carried only in `error` or
    # `reason`) is a handled response and must remain visible to the solver,
    # not be converted into a fail-open marker.  Transport/protocol errors are
    # the opposite: they must bypass explicitly so a required gate cannot
    # deadlock on an unaccounted adapter failure.
    if _route_result_is_semantic_negative(result):
        return False
    if _route_result_is_error(result):
        return True
    if isinstance(result.get("conflict"), Mapping):
        # A conflict row is meaningful only when the adapter labels the
        # envelope as a handled conflict (or supplies another recognized
        # semantic-negative diagnostic, handled above).  A successful
        # independent-claim envelope may also carry the peer conflict row, but
        # it must have an explicit acquisition bit and caller claim for the
        # normal positive validation below.  A bare
        # ``{"conflict": {...}}`` response is ambiguous adapter state and
        # must take the explicit fail-open path.
        positive_with_claim = (
            result.get("ok") is True
            and any(result.get(key) is True for key in ("acquired", "claimed"))
            and isinstance(result.get("claim"), Mapping)
        )
        return status not in {"conflict", "route_conflict"} and not positive_with_claim
    claim = result.get("claim")
    has_claim_id = isinstance(claim, Mapping) and bool(
        str(claim.get("claim_id") or "").strip()
    )
    claim_is_visible = (
        has_claim_id
        and claim.get("active") is True
        and str(claim.get("status") or "").strip().lower() in {"active", "blocked"}
    ) if isinstance(claim, Mapping) else False
    claim_is_active = (
        claim_is_visible
        and str(claim.get("status") or "").strip().lower() == "active"
    ) if isinstance(claim, Mapping) else False
    # A terminal claim row is a valid handled response for an idempotent
    # closeout or a stale independent-verification echo.  It must not be
    # mistaken for an outage merely because it cannot satisfy the active
    # write gate.
    if status in {"released", "done"} and has_claim_id:
        return False
    explicit_acquired = any(
        result.get(key) is True for key in ("acquired", "claimed")
    )
    if operation == "cps_claim_route":
        # Positive claim responses must carry both the explicit acquisition bit
        # and an active claim id.  ``ok=true``/``accepted=true`` alone is not
        # state.
        if explicit_acquired:
            # ``blocked`` is a valid handled lifecycle response, but it is not
            # a write-gate lease. Keep it visible to the caller so it can
            # deliberately change route, while rejecting unknown status
            # values as malformed adapter state.
            if status == "blocked":
                return not claim_is_visible
            positive_status = status in {"active", "independent_verification"}
            return not claim_is_active or not positive_status
        if result.get("ok") is False:
            # Only the narrow, enumerated semantic-negative statuses above
            # are handled responses.  An unknown negative envelope is an
            # adapter/protocol failure and must take the explicit bypass path
            # rather than leaving a required worker gate waiting forever.
            return True
        return True
    # Update/release responses may legitimately return a terminal claim or a
    # not-found status, but a bare boolean/envelope cannot prove closeout.
    allowed_state_statuses = (
        _ROUTE_VISIBLE_STATUSES
        | _ROUTE_TERMINAL_STATUSES
        | {"independent_verification"}
    )
    if status not in allowed_state_statuses:
        # Unknown positive status labels are not harmless metadata: the local
        # lease state cannot determine whether it is still writable. Force an
        # explicit fail-open marker rather than leaving the required gate in a
        # permanently ambiguous state.
        return True
    if has_claim_id or isinstance(result.get("claim_id"), str):
        return False
    if result.get("ok") is False:
        return True
    # A positive update/release must carry a claim identity (checked by the
    # caller against task/actor/episode).  A bare `ok=true` or unknown status
    # is not enough to refresh/close a local lease.
    return True


def _safe_piece(raw: Any) -> dict[str, Any]:
    """Return only bounded CPS fields that are safe for a solver response."""

    if not isinstance(raw, Mapping):
        return {}
    result = {
        key: _clipped_text(raw.get(key), maximum)
        for key, maximum in (
            ("id", 64),
            ("task_id", 256),
            ("author", 256),
            ("kind", 64),
            ("title", 300),
            ("body", 2_000),
            ("created_at", 64),
        )
    }
    tags = raw.get("tags")
    result["tags"] = (
        [_clipped_text(tag, 64) for tag in tags[:8] if isinstance(tag, str)]
        if isinstance(tags, list)
        else []
    )
    return result


def _safe_message(raw: Any) -> dict[str, Any]:
    """Return only bounded direct-message fields visible to a solver."""

    if not isinstance(raw, Mapping):
        return {}
    return {
        key: _clipped_text(raw.get(key), maximum)
        for key, maximum in (
            ("id", 64),
            ("task_id", 256),
            ("sender", 256),
            ("recipient", 256),
            ("body", 2_000),
            ("created_at", 64),
            ("acked_at", 64),
        )
    }


def _clipped_text(value: Any, maximum: int) -> str:
    return value[:maximum] if isinstance(value, str) else ""


def _safe_error(value: Any) -> str:
    return sanitize_worker_text(value)


def _safe_hash(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


def _safe_identifier(value: Any) -> str | None:
    return sanitize_worker_identifier(value)


def _safe_verdict_status(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text or len(text.encode("utf-8")) > 120:
        return "EVALUATOR_ERROR"
    if not re.fullmatch(r"[A-Z][A-Z0-9_:-]*", text):
        return "EVALUATOR_ERROR"
    return {"PASS": "PROVED", "AC": "PROVED", "PASSED": "PROVED"}.get(text, text)


def _valid_judge_checkpoint(
    result: Mapping[str, Any],
    *,
    raw_judge_job_id: Any = _CHECKPOINT_VALUE_UNSET,
    expected_task_contract_sha256: str | None = None,
) -> bool:
    """Recognize valid feedback without treating admission as verification."""

    if result.get("accepted") is not True:
        return False
    status = _safe_verdict_status(result.get("status"))
    if status not in _JUDGE_CHECKPOINT_TERMINAL_STATUSES:
        return False
    if _safe_hash(result.get("candidate_sha256")) is None:
        return False
    contract_hash = _safe_hash(result.get("task_contract_sha256"))
    if contract_hash is None:
        return False
    if expected_task_contract_sha256 is not None:
        expected_hash = _safe_hash(expected_task_contract_sha256)
        if expected_hash is None or contract_hash != expected_hash:
            return False
    if raw_judge_job_id is _CHECKPOINT_VALUE_UNSET:
        # Preserve the helper's standalone behavior for callers/tests that
        # already hold a complete result mapping.  The broker call site passes
        # the unsanitized Verdict field explicitly.
        raw_judge_job_id = result.get("judge_job_id")
    if status == "LOCAL_REJECTED":
        # LOCAL_REJECTED is a supervisor-local result and must carry no remote
        # identity at all.  Checking the raw field is intentional: an invalid
        # or empty non-None value must not disappear during sanitization and
        # accidentally satisfy the local-only contract.
        return raw_judge_job_id is None
    return sanitize_worker_identifier(raw_judge_job_id) is not None


def _safe_finite_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _safe_score(value: Any) -> float:
    return max(0.0, min(1.0, _safe_finite_float(value)))


def _audit_failure_fields(value: Any) -> dict[str, Any]:
    failure = value.get("evaluator_failure") if isinstance(value, Mapping) else None
    if not isinstance(failure, Mapping):
        return {
            "failure_category": None,
            "failure_http_status": None,
            "failure_attempts": None,
            "failure_retry_after_seconds": None,
        }
    category = sanitize_worker_identifier(failure.get("category"))
    http_status = failure.get("http_status")
    attempts = failure.get("attempts")
    retry_after = failure.get("retry_after_seconds")
    safe_retry_after: float | None = None
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        try:
            parsed_retry_after = float(retry_after)
        except (ValueError, OverflowError):
            pass
        else:
            if math.isfinite(parsed_retry_after) and parsed_retry_after >= 0:
                safe_retry_after = round(parsed_retry_after, 3)
    return {
        "failure_category": category,
        "failure_http_status": (
            http_status
            if isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and 100 <= http_status <= 599
            else None
        ),
        "failure_attempts": (
            max(0, min(attempts, 1_000_000))
            if isinstance(attempts, int) and not isinstance(attempts, bool)
            else None
        ),
        "failure_retry_after_seconds": safe_retry_after,
    }


def _is_authoritative_proof(
    *,
    task: Task,
    verdict: Verdict,
    snapshot: CandidateSnapshot,
    expected_task_contract_sha256: str,
    allow_mock_provenance: bool,
) -> bool:
    """Accept only a complete Judge proof for the exact frozen submission."""

    candidate_hash = _safe_hash(verdict.candidate_sha256)
    contract_hash = _safe_hash(verdict.task_contract_sha256)
    job_id = sanitize_worker_identifier(verdict.judge_job_id)
    mock_provenance = (
        allow_mock_provenance and verdict.response.get("mock") is True
    )
    return bool(
        verdict.task_id == task.slug
        and _safe_verdict_status(verdict.status) == "PROVED"
        and _safe_score(verdict.score) >= 1.0
        and candidate_hash == snapshot.sha256
        and contract_hash == expected_task_contract_sha256
        and (job_id or mock_provenance)
    )


def _formal_capability_failure(claim: _SessionClaim) -> dict[str, Any] | None:
    # A failed cancellation/unknown remote terminal is run-global. Do not let
    # cache-only search or declaration lookup continue while a remote job may
    # still be consuming the shared Judge slot.
    broker = getattr(claim, "broker", None)
    if broker is not None and broker._remote_settlement_unconfirmed():
        return _remote_settlement_control_result()
    if time.monotonic() >= claim.deadline_monotonic:
        return _control_result(
            "OUT_OF_HORIZON",
            "The experiment horizon has elapsed.",
            retryable=False,
        )
    if _claim_cancelled(claim):
        return _control_result(
            "TASK_CANCELLED",
            "This solver task no longer accepts formal-tool work.",
            retryable=False,
        )
    return None


def _formal_gate_failure(
    claim: _SessionClaim,
    remote_unsettled: bool,
) -> dict[str, Any]:
    if remote_unsettled:
        return _remote_settlement_control_result()
    failure = _formal_capability_failure(claim)
    if failure is not None:
        return failure
    return _control_result(
        "JUDGE_ADMISSION_TIMEOUT",
        "The controlled Judge remained busy until the formal-tool deadline.",
        retryable=True,
    )


def _json_clone(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    return json.loads(json.dumps(dict(value), ensure_ascii=False))


def _recursive_value(payload: Any, name: str, *, depth: int = 0) -> Any:
    if not isinstance(payload, Mapping) or depth > 4:
        return None
    if payload.get(name) is not None:
        return payload.get(name)
    for nested_name in ("response", "canonical_verdict", "lean_environment"):
        nested = payload.get(nested_name)
        found = _recursive_value(nested, name, depth=depth + 1)
        if found is not None:
            return found
    return None


def _formal_diagnostics(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = _recursive_value(response, "probe_diagnostics")
    items = raw.get("items") if isinstance(raw, Mapping) else raw
    if not isinstance(items, list):
        return []
    result: list[dict[str, Any]] = []
    for item in items[:24]:
        if not isinstance(item, Mapping):
            continue
        result.append(
            {
                "severity": sanitize_public_text(str(item.get("severity") or "info"), limit=32),
                "message": sanitize_public_text(
                    str(item.get("data") or item.get("message") or ""),
                    limit=1_024,
                ),
                "line": item.get("line") if isinstance(item.get("line"), int) else 0,
                "column": item.get("column") if isinstance(item.get("column"), int) else 0,
            }
        )
    return result


def _formal_worker_verdict(verdict: Verdict) -> dict[str, Any]:
    response = safe_worker_response(verdict.response)
    errors: list[str] = []
    nested_error = _recursive_value(response, "error_message")
    if isinstance(nested_error, str) and nested_error.strip():
        errors.append(sanitize_public_text(nested_error, limit=1_024))
    if verdict.error:
        errors.append(sanitize_public_text(verdict.error, limit=1_024))
    return {
        "ok": _safe_verdict_status(verdict.status) not in _FORMAL_NONCACHEABLE_STATUSES,
        "status": _safe_verdict_status(verdict.status),
        "score": 0.0,
        "diagnostics": _formal_diagnostics(response),
        "error_messages": errors[:24],
        "reason": sanitize_public_text(
            str(_recursive_value(response, "reason") or verdict.error or ""),
            limit=1_024,
        ),
        "elapsed_ms": int(max(0.0, verdict.elapsed_seconds) * 1_000),
    }


def _formal_probe_result(verdict: Verdict) -> dict[str, Any]:
    response = safe_worker_response(verdict.response)
    normalized = _safe_verdict_status(verdict.status)
    if normalized in {"PROVED", "COMPILES_WITH_SORRY", "ELABORATED"}:
        status = "elaborated"
    elif normalized in {"VERIFY_FAIL", "LOCAL_REJECTED", "ELAB_FAILED"}:
        status = "elab_failed"
    elif normalized == "REJECTED_OVERLOADED":
        status = "probe_admission_closed"
    elif normalized in {"OUT_OF_HORIZON", "TASK_CANCELLED"}:
        status = "probe_admission_closed"
    elif normalized == "REMOTE_SETTLEMENT_UNCONFIRMED":
        status = "probe_remote_settlement_unconfirmed"
    else:
        status = "probe_transport_error" if normalized == "EVALUATOR_ERROR" else normalized.lower()
    errors: list[str] = []
    nested_error = _recursive_value(response, "error_message")
    if isinstance(nested_error, str) and nested_error.strip():
        errors.append(sanitize_public_text(nested_error, limit=1_024))
    if verdict.error:
        errors.append(sanitize_public_text(verdict.error, limit=1_024))
    result: dict[str, Any] = {
        "status": status,
        "is_valid_with_sorry": _recursive_value(response, "is_valid_with_sorry") is True,
        "is_valid_no_sorry": _recursive_value(response, "is_valid_no_sorry") is True,
        "diagnostics": _formal_diagnostics(response),
        "error_messages": errors[:24],
        "elapsed_ms": int(max(0.0, verdict.elapsed_seconds) * 1_000),
    }
    for key in ("mathlib_revision", "lean_version"):
        value = _recursive_value(response, key)
        if isinstance(value, str) and value.strip():
            result[key] = sanitize_public_text(value, limit=256)
    environment = _recursive_value(response, "lean_environment")
    if isinstance(environment, Mapping):
        safe_environment: dict[str, str] = {}
        for key in ("mathlib_revision", "lean_version"):
            value = environment.get(key)
            if isinstance(value, str) and value.strip():
                safe_environment[key] = sanitize_public_text(value, limit=256)
        if safe_environment:
            result["lean_environment"] = safe_environment
    return result


def _verdict_proves_no_backend_job(verdict: Verdict) -> bool:
    if sanitize_worker_identifier(verdict.judge_job_id) is not None:
        return False
    if _has_unsettled_remote_work(_safe_verdict_status(verdict.status), verdict.response):
        return False
    status = _safe_verdict_status(verdict.status)
    if status in {"LOCAL_REJECTED", "OUT_OF_HORIZON", "TASK_CANCELLED"}:
        return True
    return status == "REJECTED_OVERLOADED" and _nested_response_bool(
        verdict.response,
        "retryable",
    )


def _contains_guarded(text: str, name: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_']){re.escape(name)}(?![A-Za-z0-9_'])",
            text,
        )
    )


def _candidate_snapshot(
    path: Path,
    *,
    trusted_root: Path,
    max_bytes: int = DEFAULT_MAX_CANDIDATE_BYTES,
) -> CandidateSnapshot:
    raw = read_regular_bytes(
        path,
        trusted_root=trusted_root,
        max_bytes=max_bytes,
    )
    return CandidateSnapshot(
        source=raw.decode("utf-8"),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


__all__ = ["CandidateSnapshot", "JudgeBroker"]
