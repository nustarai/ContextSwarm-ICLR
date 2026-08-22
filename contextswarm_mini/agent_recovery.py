"""Bounded recovery for solver process/session failures.

The Pi runtime already retries provider requests inside a live session.  This
module owns the narrower outer boundary: when that whole RPC process/session
exits abnormally, restart the same logical actor against its persisted session
and workspace, without extending the experiment horizon.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from typing import Any, Callable

from .models import AgentResult


RecoveryEventSink = Callable[[str, dict[str, Any]], None]
RecoveryInvocation = Callable[[int], AgentResult]


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _exception_label(exc: BaseException) -> str:
    """Return a bounded exception class label without exposing its message."""

    raw = type(exc).__name__ or "Exception"
    label = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in raw
    )
    return label[:80] or "Exception"


def _event_is_set(event: Any | None) -> bool:
    if event is None:
        return False
    try:
        return bool(event.is_set())
    except Exception:
        # A broken cancellation adapter must not turn a solver failure into an
        # unbounded retry loop.  Treat it as cancellation (fail closed).
        return True


def _exception_result(
    exc: Exception,
    *,
    task_id: str,
    actor_id: str,
    episode: int,
    deadline_monotonic: float,
    cancel_event: Any | None,
) -> tuple[AgentResult, str]:
    """Convert an invocation exception into a safe logical failed attempt."""

    label = _exception_label(exc)
    now = time.monotonic()
    timed_out = isinstance(exc, TimeoutError)
    cancelled = _event_is_set(cancel_event)
    horizon_reached = now >= float(deadline_monotonic)
    return (
        AgentResult(
            agent_id=actor_id,
            task_id=task_id,
            episode=episode,
            returncode=1,
            started_at=_utc_now(),
            finished_at=_utc_now(),
            error_tail=f"Pi solver invocation raised {label}",
            timed_out=timed_out,
            cancelled=cancelled,
            run_horizon_reached=horizon_reached,
        ),
        label,
    )


def _guard_result(
    *,
    task_id: str,
    actor_id: str,
    episode: int,
    cancelled: bool = False,
) -> AgentResult:
    """Build a terminal result when admission races closeout/cancellation.

    The guard runs before calling the solver adapter.  Returning a normal
    ``AgentResult`` keeps runner closeout/accounting uniform while ensuring
    that a replacement slot never launches a Pi process after the fixed
    horizon (or after the runner has revoked the slot).
    """

    now = _utc_now()
    return AgentResult(
        agent_id=actor_id,
        task_id=task_id,
        episode=episode,
        returncode=130 if cancelled else 124,
        started_at=now,
        finished_at=now,
        error_tail=(
            "Pi solver invocation cancelled before start"
            if cancelled
            else "Pi solver horizon elapsed before start"
        ),
        timed_out=not cancelled,
        cancelled=cancelled,
        run_horizon_reached=not cancelled,
    )


def _failure_category(result: AgentResult) -> str:
    """Classify a bounded Pi failure without copying diagnostic text."""

    text = f"{result.error_tail}\n{result.output_tail}".lower()
    if result.timed_out or "timeout" in text or "timed out" in text:
        return "timeout"
    if any(
        token in text
        for token in ("coordinator", "websocket", "network", "connection", "transport")
    ):
        return "transport"
    if result.returncode == 137 or "out of memory" in text or "oom" in text:
        return "resource"
    if "provider" in text or "oauth" in text or "429" in text or "5xx" in text:
        return "provider"
    return "process"


def recovery_settings(config: Any) -> tuple[int, float]:
    """Return the manifest-owned outer restart count and backoff in seconds."""

    if not bool(getattr(config, "pi_recovery_enabled", True)):
        return 0, 0.0
    return (
        max(0, int(getattr(config, "pi_recovery_max_restarts", 1))),
        max(0, int(getattr(config, "pi_recovery_base_delay_ms", 1_000)))
        / 1_000.0,
    )


def is_recoverable_agent_failure(
    result: AgentResult,
    *,
    deadline_monotonic: float,
    now_monotonic: float | None = None,
    cancel_event: Any | None = None,
) -> bool:
    """Return whether an outer solver restart is safe and still in budget.

    Candidate quality is deliberately absent from this classifier.  PE, WA,
    verification failures, and other Judge verdicts are candidate-attempt
    outcomes, not process/session failures.  A timeout may be recoverable only
    when it was an inner Pi timeout and the fixed run deadline still has time;
    reaching the run horizon itself is terminal.
    """

    now = time.monotonic() if now_monotonic is None else float(now_monotonic)
    return bool(
        result.returncode != 0
        and not result.cancelled
        and not result.run_horizon_reached
        and now < float(deadline_monotonic)
        and not _event_is_set(cancel_event)
    )


def run_with_recovery(
    invoke: RecoveryInvocation,
    *,
    task_id: str,
    actor_id: str,
    episode: int,
    deadline_monotonic: float,
    cancel_event: Any | None = None,
    max_restarts: int = 1,
    base_delay_seconds: float = 1.0,
    on_event: RecoveryEventSink | None = None,
) -> AgentResult:
    """Run one logical solver actor and recover abnormal session exits.

    ``invoke`` receives the zero-based recovery attempt.  Callers must keep
    actor/task/episode, workspace, prompt, and deadline fixed across calls.
    Pi derives its session identity from actor/episode and therefore resumes
    the same persisted conversation; the mutable candidate workspace is also
    retained.  If an invocation raises an ordinary ``Exception``, it is
    converted to a bounded failed attempt and follows the same retry policy;
    ``BaseException`` subclasses such as ``KeyboardInterrupt`` still escape.
    Backoff time counts against ``deadline_monotonic``.
    """

    if isinstance(max_restarts, bool) or int(max_restarts) < 0:
        raise ValueError("max_restarts must be a non-negative integer")
    restart_limit = int(max_restarts)
    delay_base = float(base_delay_seconds)
    if not math.isfinite(delay_base) or delay_base < 0:
        raise ValueError("base_delay_seconds must be a finite non-negative number")
    if not math.isfinite(float(deadline_monotonic)):
        raise ValueError("deadline_monotonic must be finite")

    def emit(event: str, **payload: Any) -> None:
        if on_event is not None:
            on_event(
                event,
                {
                    "task_id": task_id,
                    "agent_id": actor_id,
                    "episode": episode,
                    "resume_scope": "same_session_and_workspace",
                    **payload,
                },
            )

    recovery_attempt = 0
    while True:
        # A replacement can be queued while the prior attempt is draining or
        # while a broker callback is revoking the slot.  Check the fixed
        # lifecycle boundary before invoking the adapter as well as after it
        # returns; otherwise a zero-delay refill could launch a fresh Pi
        # process after normal arm closeout has already begun.
        cancelled_before_start = _event_is_set(cancel_event)
        horizon_before_start = time.monotonic() >= float(deadline_monotonic)
        if cancelled_before_start or horizon_before_start:
            result = _guard_result(
                task_id=task_id,
                actor_id=actor_id,
                episode=episode,
                cancelled=cancelled_before_start,
            )
            emit(
                "agent_recovery_exhausted",
                recovery_attempt=recovery_attempt,
                max_restarts=restart_limit,
                returncode=result.returncode,
                recoverable=False,
                reason=(
                    "cancelled_before_start"
                    if cancelled_before_start
                    else "horizon_elapsed_before_start"
                ),
            )
            return result
        if recovery_attempt:
            emit(
                "agent_recovery_started",
                recovery_attempt=recovery_attempt,
                max_restarts=restart_limit,
            )
        invocation_exception_type: str | None = None
        try:
            result = invoke(recovery_attempt)
        except Exception as exc:
            # PiAgent normally converts transport/process errors into an
            # AgentResult.  Keep the generic boundary defensive for adapters
            # or test/runtime shims that raise instead: retry the logical
            # actor, but never copy an exception message into artifacts.
            result, invocation_exception_type = _exception_result(
                exc,
                task_id=task_id,
                actor_id=actor_id,
                episode=episode,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )
        if not isinstance(result, AgentResult):
            raise TypeError("solver recovery invocation must return AgentResult")
        # PiAgent uses the same deadline for its bounded RPC wait.  A drain
        # after that wait can return just after the monotonic boundary, so the
        # caller must not mistake this ordinary arm closeout for a recoverable
        # inner timeout.  Mark it before classification and before emitting
        # the failure event; the AgentResult object is intentionally mutable.
        if (
            result.returncode != 0
            and not result.run_horizon_reached
            and time.monotonic() >= float(deadline_monotonic)
        ):
            result.run_horizon_reached = True
        if result.returncode == 0:
            if recovery_attempt:
                emit(
                    "agent_recovery_succeeded",
                    recovery_attempt=recovery_attempt,
                    max_restarts=restart_limit,
                    returncode=result.returncode,
                    timed_out=result.timed_out,
                )
            return result

        recoverable = is_recoverable_agent_failure(
            result,
            deadline_monotonic=deadline_monotonic,
            cancel_event=cancel_event,
        )
        emit(
            "agent_recovery_failure_observed",
            recovery_attempt=recovery_attempt,
            max_restarts=restart_limit,
            returncode=result.returncode,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            run_horizon_reached=result.run_horizon_reached,
            recoverable=recoverable,
            failure_category=_failure_category(result),
            failure_source=(
                "invoke_exception" if invocation_exception_type else "agent_result"
            ),
            exception_type=invocation_exception_type,
        )
        if not recoverable or recovery_attempt >= restart_limit:
            emit(
                "agent_recovery_exhausted",
                recovery_attempt=recovery_attempt,
                max_restarts=restart_limit,
                returncode=result.returncode,
                recoverable=recoverable,
                reason=(
                    "restart_limit"
                    if recoverable
                    else "cancelled_or_horizon_or_nonfailure"
                ),
            )
            return result

        delay = delay_base * (2**recovery_attempt)
        remaining = float(deadline_monotonic) - time.monotonic()
        if remaining <= delay:
            result.run_horizon_reached = True
            emit(
                "agent_recovery_exhausted",
                recovery_attempt=recovery_attempt,
                max_restarts=restart_limit,
                returncode=result.returncode,
                recoverable=False,
                reason="insufficient_horizon_for_backoff",
            )
            return result
        next_attempt = recovery_attempt + 1
        emit(
            "agent_recovery_scheduled",
            recovery_attempt=next_attempt,
            max_restarts=restart_limit,
            delay_seconds=delay,
        )
        if delay > 0 and cancel_event is not None:
            if bool(cancel_event.wait(delay)):
                emit(
                    "agent_recovery_exhausted",
                    recovery_attempt=recovery_attempt,
                    max_restarts=restart_limit,
                    returncode=result.returncode,
                    recoverable=False,
                    reason="cancelled_during_backoff",
                )
                return result
        elif delay > 0:
            time.sleep(delay)

        # The wait/sleep itself is part of the fixed horizon.  Check both
        # runner cancellation and the monotonic deadline again immediately
        # before relaunching; otherwise a zero-delay retry (or scheduling
        # overhead after a short backoff) could start a new Pi process after
        # the arm has already entered normal closeout.
        if _event_is_set(cancel_event):
            emit(
                "agent_recovery_exhausted",
                recovery_attempt=recovery_attempt,
                max_restarts=restart_limit,
                returncode=result.returncode,
                recoverable=False,
                reason="cancelled_before_relaunch",
            )
            return result
        if time.monotonic() >= float(deadline_monotonic):
            result.run_horizon_reached = True
            emit(
                "agent_recovery_exhausted",
                recovery_attempt=recovery_attempt,
                max_restarts=restart_limit,
                returncode=result.returncode,
                recoverable=False,
                reason="horizon_elapsed_before_relaunch",
            )
            return result
        recovery_attempt = next_attempt
