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
from .models import Task, Verdict


_MAX_REQUEST_BYTES = 32 * 1024
_MAX_PROBE_CALLS_PER_SESSION = 32
_MIN_PROBE_INTERVAL_SECONDS = 1.0
_PROBE_ADMISSION_TIMEOUT_SECONDS: float | None = None
_BROKER_DRAIN_TIMEOUT_SECONDS = 5.0
_RUNNER_ONLY_CPS_KINDS = frozenset({"validation_result"})


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
    actor_id: str
    workdir: Path
    candidates: dict[str, _CandidateBinding]
    deadline_monotonic: float
    deadline_epoch_ms: int
    cps_store: CPSStore | None = None
    communication: str = "none"
    roster_path: Path | None = None
    on_authoritative_verdict: (
        Callable[[Task, Verdict, CandidateSnapshot], None] | None
    ) = None
    cancel_event: threading.Event | None = None
    revoked_event: threading.Event = field(default_factory=threading.Event, repr=False)
    probe_calls: int = 0
    last_probe_started: float = 0.0
    probe_active: bool = False
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
        drain_timeout_seconds: float = _BROKER_DRAIN_TIMEOUT_SECONDS,
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
        normalized_drain_timeout = float(drain_timeout_seconds)
        if not math.isfinite(normalized_drain_timeout) or normalized_drain_timeout <= 0:
            raise ValueError("broker drain timeout must be finite and positive")
        self.drain_timeout_seconds = normalized_drain_timeout
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
        self._server = _BrokerHTTPServer(("127.0.0.1", 0), Handler)
        self._server._broker = self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="contextswarm-judge-broker",
            daemon=True,
        )
        self._thread.start()
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
            raise JudgeBrokerDrainError(state)
        # A successful close is stronger than a point-in-time observation:
        # serve_forever is stopped, claims are revoked, and no handler or FIFO
        # waiter remains that could re-populate either count.
        final_state = self.drain_state()
        if any(final_state.values()):
            raise JudgeBrokerDrainError(final_state)
        return {"drained": True, **final_state}

    def drain_state(self) -> dict[str, int]:
        """Return the closeout counters consumed by runner health checks."""

        with self._handler_condition:
            active_handlers = self._active_handlers
            broker_unsettled_jobs = self._remote_unsettled_jobs
        evaluator_unsettled_jobs = _nonnegative_count(
            getattr(self.evaluator, "remote_unsettled_jobs", 0)
        )
        with self._admission_condition:
            fifo_depth = len(self._admission_queue)
        return {
            "active_handlers": max(0, int(active_handlers)),
            "fifo_depth": max(0, int(fifo_depth)),
            "remote_unsettled_jobs": (
                max(0, int(broker_unsettled_jobs)) + evaluator_unsettled_jobs
            ),
        }

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
        while True:
            state = self.drain_state()
            if state["active_handlers"] == 0 and state["fifo_depth"] == 0:
                return {
                    "drained": state["remote_unsettled_jobs"] == 0,
                    **state,
                }
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                return {"drained": False, **state}
            with self._handler_condition:
                self._handler_condition.wait(timeout=min(remaining, 0.05))

    def public_policy(self) -> dict[str, Any]:
        return {
            "transport": "runner_owned_loopback_capability",
            "response_profile": LEAN_PROBE_RESPONSE_PROFILE,
            "max_probe_calls_per_session": self.max_probe_calls_per_session,
            "min_probe_interval_seconds": self.min_probe_interval_seconds,
            "probe_admission_deadline": "session_remaining_horizon",
            "probe_admission_timeout_seconds": self.probe_admission_timeout_seconds,
            "candidate_selection": "runner_bound_result_lean",
            "candidate_submission": "immutable_source_snapshot",
            "shares_final_evaluator_gate": True,
            "submitted_job_cancellation": "delete_on_probe_cancel_or_deadline",
            "closeout_requires_active_handlers": 0,
            "closeout_requires_fifo_depth": 0,
            "closeout_requires_remote_unsettled_jobs": 0,
            "drain_timeout_seconds": self.drain_timeout_seconds,
        }

    @contextmanager
    def session(
        self,
        *,
        actor_id: str,
        workdir: Path,
        candidates: Mapping[str, tuple[Task, Path]],
        deadline_monotonic: float,
        cps_store: CPSStore | None = None,
        communication: str = "none",
        roster_path: Path | None = None,
        on_authoritative_verdict: (
            Callable[[Task, Verdict, CandidateSnapshot], None] | None
        ) = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[dict[str, str]]:
        """Issue and later revoke a capability bound to exact candidates."""

        if self._server is None:
            raise RuntimeError("Judge broker has not been started")
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
            candidate = Path(candidate_path).resolve()
            if candidate.name != "result.lean" or not _is_relative_to(
                candidate, resolved_workdir
            ):
                raise ValueError("broker candidates must be result.lean inside the assigned workdir")
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

        normalized_communication = str(communication or "none").strip().lower()
        if normalized_communication == "simple":
            normalized_communication = "blackboard"
        if normalized_communication not in {"none", "blackboard", "direct", "hybrid"}:
            raise ValueError("unsupported broker communication policy")
        if normalized_communication != "none" and (cps_store is None or len(bindings) != 1):
            raise ValueError("CPS broker sessions require one task and a CPS store")

        normalized_deadline = float(deadline_monotonic)
        if not math.isfinite(normalized_deadline):
            raise ValueError("broker session deadline must be finite")
        remaining_seconds = max(0.0, normalized_deadline - time.monotonic())
        deadline_epoch_ms = int((time.time() + remaining_seconds) * 1_000)
        token = secrets.token_urlsafe(32)
        claim = _SessionClaim(
            actor_id=str(actor_id),
            workdir=resolved_workdir,
            candidates=bindings,
            deadline_monotonic=normalized_deadline,
            deadline_epoch_ms=deadline_epoch_ms,
            cps_store=cps_store,
            communication=normalized_communication,
            roster_path=Path(roster_path).resolve() if roster_path is not None else None,
            on_authoritative_verdict=on_authoritative_verdict,
            cancel_event=cancel_event,
        )
        with self._claims_lock:
            self._claims[token] = claim
        host, port = self._server.server_address[:2]
        try:
            yield {
                "CONTEXTSWARM_JUDGE_URL": f"http://{host}:{port}/{token}",
                "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": str(deadline_epoch_ms),
            }
        finally:
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
        self._send_json(handler, 200, result)

    def _judge_check(
        self, claim: _SessionClaim, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
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
            self._audit(claim, audit_task, result, accepted=False)
            return result
        try:
            task_id = _select_task_id(claim, payload.get("task_id"))
        except ValueError as exc:
            result = _control_result(
                "INVALID_TASK_SELECTION",
                _safe_error(exc),
                retryable=False,
            )
            self._audit(claim, "__invalid__", result, accepted=False)
            return result
        now = time.monotonic()
        with claim.lock:
            if claim.probe_active:
                result = _control_result(
                    "SESSION_PROBE_IN_FLIGHT",
                    "Only one judge_check may be in flight for this solver session.",
                    retryable=True,
                )
                self._audit(claim, task_id, result, accepted=False)
                return result
            if claim.probe_calls >= self.max_probe_calls_per_session:
                result = _control_result(
                    "SESSION_PROBE_BUDGET_EXHAUSTED",
                    "The controlled Judge-call budget for this solver session is exhausted.",
                    retryable=False,
                )
                self._audit(claim, task_id, result, accepted=False)
                return result
            cooldown = self.min_probe_interval_seconds - (now - claim.last_probe_started)
            if claim.last_probe_started and cooldown > 0:
                result = _control_result(
                    "SESSION_PROBE_COOLDOWN",
                    "Wait before submitting another candidate to judge_check.",
                    retryable=True,
                    retry_after_seconds=round(cooldown, 3),
                )
                self._audit(claim, task_id, result, accepted=False)
                return result
            if now >= claim.deadline_monotonic:
                result = _control_result(
                    "OUT_OF_HORIZON",
                    "The experiment horizon has elapsed.",
                    retryable=False,
                )
                self._audit(claim, task_id, result, accepted=False)
                return result
            if _claim_cancelled(claim):
                result = _control_result(
                    "TASK_CANCELLED",
                    "This solver task no longer accepts Judge work.",
                    retryable=False,
                )
                self._audit(claim, task_id, result, accepted=False)
                return result
            if self._remote_settlement_unconfirmed():
                result = _remote_settlement_control_result()
                self._audit(claim, task_id, result, accepted=False)
                return result
            claim.probe_active = True

        started = time.monotonic()
        gate_wait_started = started
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
        try:
            # Freeze exactly what this capability call submits before waiting
            # for the shared Judge gate.  The worker may continue editing its
            # result.lean while queued, but neither the request nor its audit
            # hash can then drift to a different file state.
            try:
                snapshot = _candidate_snapshot(binding.path)
            except (OSError, UnicodeError):
                result = _control_result(
                    "CANDIDATE_SNAPSHOT_ERROR",
                    "The runner could not freeze result.lean for Judge submission.",
                    retryable=True,
                )
                return self._finish_judge_check(
                    claim,
                    task_id,
                    result,
                    accepted=False,
                    started=started,
                    gate_wait_started=gate_wait_started,
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
                if _accepts_cancel_event(evaluator_call):
                    evaluator_kwargs["cancel_event"] = _ClaimCancelEvent(claim)
                evaluator_call_started = True
                verdict: Verdict = evaluator_call(
                    binding.task,
                    candidate_argument,
                    **evaluator_kwargs,
                )
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
                if call_unsettled:
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
                if call_unsettled:
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
                proof_claimed = verdict_status == "PROVED" or _safe_score(verdict.score) >= 1.0
                if call_unsettled:
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

        self._audit(
            claim,
            task_id,
            result,
            accepted=accepted,
            call_index=call_index,
            gate_wait_seconds=time.monotonic() - gate_wait_started
            if not acquired
            else gate_wait,
            elapsed_seconds=time.monotonic() - started,
            candidate_sha256=snapshot.sha256 if snapshot is not None else None,
            task_contract_sha256=result.get("task_contract_sha256"),
            judge_job_id=result.get("judge_job_id"),
            cache_reused=result.get("cache_reused") is True,
        )
        return result

    def _acquire_evaluator_gate(
        self,
        deadline_monotonic: float,
        *,
        claim: _SessionClaim,
    ) -> bool:
        """Acquire the shared evaluator gate FIFO among broker callers."""

        waiter = object()
        acquired = False
        with self._admission_condition:
            self._admission_queue.append(waiter)
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
    ) -> dict[str, Any]:
        """Close and audit a pre-admission control result."""

        with claim.lock:
            claim.probe_active = False
        normalized = dict(result)
        normalized["accepted"] = accepted
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
            try:
                return self._cps_operation_locked(claim, operation, payload)
            except RuntimeError:
                # CPSStore intentionally raises a plain RuntimeError when the
                # horizon or cancellation guard closes after a lock wait.  Map
                # only a now-observable capability closure to a stable worker
                # response; unrelated store failures remain BROKER_ERROR.
                failure = _cps_capability_failure(claim)
                if failure is not None:
                    return failure
                raise

    def _cps_operation_locked(
        self,
        claim: _SessionClaim,
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        store = claim.cps_store
        if store is None or claim.communication == "none":
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
            "cps_send": {"recipient", "body", "scope"},
            "cps_inbox": {"limit"},
            "cps_ack": {"message_id"},
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
            actors = _safe_roster(claim.roster_path)
            if query:
                actors = [
                    item
                    for item in actors
                    if query in json.dumps(item, ensure_ascii=False).lower()
                ]
            return {"ok": True, "actors": actors[:100]}
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
                    )
                ],
            }
        if operation == "cps_ack":
            message_id = _required_string(payload.get("message_id"), "message_id", 64)
            visible = store.inbox(task_id=task_id, recipient=claim.actor_id, limit=50)
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


def _candidate_snapshot(path: Path) -> CandidateSnapshot:
    raw = path.read_bytes()
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
