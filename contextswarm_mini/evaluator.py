"""Small HTTP client for the ContextSwarmJudge Lean router."""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .models import Task, Verdict


class EvaluatorError(RuntimeError):
    """A transport or malformed-verdict failure."""


class EvaluatorOverloadedError(EvaluatorError):
    """A definitive pre-admission rejection which is safe to retry."""

    def __init__(
        self,
        message: str,
        response: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.response = dict(response or {})


_NONTERMINAL_STATUSES = {
    "QUEUED",
    "PENDING",
    "RUNNING",
    "IN_PROGRESS",
    "STARTED",
    "CANCEL_REQUESTED",
}

FORMAL_VERDICT_SCHEMA_VERSION = "contextswarm_formal_verdict_v1"
_ALLOWED_CANONICAL_STATUSES = {
    "PROVED",
    "CHEATING",
    "VERIFY_FAIL",
    "COMPILES_WITH_SORRY",
    "NETWORK_ERROR",
}
_RAW_FAILURE_STATUSES = {
    "FAILED",
    "TIMED_OUT",
    "ERROR",
    "CANCELLED",
    "CANCELED",
    "REJECTED_OVERLOADED",
}
_MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024


def normalize_base_url(raw: str) -> str:
    value = str(raw or "").strip().rstrip("/")
    for suffix in ("/api/lean/jobs", "/api/lean/verify", "/verify", "/healthz"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value.rstrip("/")


class LeanEvaluator:
    def __init__(
        self,
        base_url: str,
        *,
        lean_env_id: str,
        timeout_seconds: int = 300,
        verification_profile: str = "formal_proof",
        judge_mode: str = "fast",
        poll_interval_seconds: float = 1.0,
        settlement_grace_seconds: float = 30.0,
        cancel_grace_seconds: float = 5.0,
        admission_retry_seconds: float = 30.0,
        max_lifecycle_seconds: float | None = None,
        terminal_overload_retries: int = 1,
        lifecycle_observer: Callable[[str, Mapping[str, Any]], None] | None = None,
    ):
        self.base_url = normalize_base_url(base_url)
        self.lean_env_id = lean_env_id
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.verification_profile = verification_profile
        self.judge_mode = judge_mode
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self.settlement_grace_seconds = max(0.1, float(settlement_grace_seconds))
        self.cancel_grace_seconds = max(0.1, float(cancel_grace_seconds))
        self.admission_retry_seconds = max(0.1, float(admission_retry_seconds))
        lifecycle_cap = (
            max(3_600.0, (8.0 * self.timeout_seconds) + 120.0)
            if max_lifecycle_seconds is None
            else float(max_lifecycle_seconds)
        )
        if not math.isfinite(lifecycle_cap) or lifecycle_cap <= 0:
            raise ValueError("max_lifecycle_seconds must be finite and positive")
        self.max_lifecycle_seconds = lifecycle_cap
        self.terminal_overload_retries = max(0, int(terminal_overload_retries))
        self.lifecycle_observer = lifecycle_observer

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        token = str(__import__("os").environ.get("LEAN_AUTH_TOKEN") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            request_timeout = min(float(timeout_seconds or self.timeout_seconds), 30.0)
            with urlopen(request, timeout=max(0.1, request_timeout)) as response:
                encoded = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
                if len(encoded) > _MAX_HTTP_RESPONSE_BYTES:
                    raise EvaluatorError(
                        f"Lean response exceeded {_MAX_HTTP_RESPONSE_BYTES} bytes ({method} {path})"
                    )
                raw = encoded.decode("utf-8")
        except HTTPError as exc:
            status_code = exc.code
            try:
                error_payload = json.loads(exc.read(64 * 1024).decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                error_payload = None
            exc.close()
            error_code = (
                str(error_payload.get("error") or "").strip().lower()
                if isinstance(error_payload, Mapping)
                else ""
            )
            error_message = (
                str(error_payload.get("message") or "").strip().lower()
                if isinstance(error_payload, Mapping)
                else ""
            )
            error_text = f"{error_code} {error_message}"
            confirmed_overload = (
                isinstance(error_payload, Mapping)
                and (
                    error_code
                    in {
                        "admission_capacity_exceeded",
                        "permit_unavailable",
                    }
                    or "overload" in error_text
                    or (
                        "queue" in error_text
                        and any(word in error_text for word in ("full", "capacity"))
                    )
                    or (
                        "ingress" in error_text
                        and "capacity" in error_text
                    )
                )
            )
            if (
                status_code in {429, 503}
                and method == "POST"
                and path == "/api/lean/jobs"
                and confirmed_overload
            ):
                raise EvaluatorOverloadedError(
                    f"Lean admission overloaded ({method} {path})",
                    error_payload,
                ) from exc
            raise EvaluatorError(f"Lean request failed ({method} {path}): {exc}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise EvaluatorError(f"Lean request failed ({method} {path}): {exc}") from exc
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise EvaluatorError(f"Lean returned non-JSON ({method} {path})") from exc
        if not isinstance(parsed, dict):
            raise EvaluatorError(f"Lean returned a non-object payload ({method} {path})")
        return parsed

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def cancel_job(self, job_id: str) -> tuple[dict[str, Any], str | None]:
        """Public broker hook for reconciling a journaled abandoned job."""

        return self._cancel_and_reconcile(str(job_id), {})

    def probe(
        self,
        task: Task,
        source: str,
        *,
        timeout_seconds: int = 30,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        """Run one advisory Lean kernel probe through the cancellable job API."""

        started = time.monotonic()
        timeout = max(10, min(int(timeout_seconds), 120))
        if deadline_monotonic is not None and started >= deadline_monotonic:
            return {
                "status": "probe_admission_closed",
                "error_kind": "run_horizon_elapsed",
                "elapsed_ms": 0,
            }
        payload = {
            "code": source,
            "timeout": timeout,
            "max_retries": 0,
            "problem_id": task.problem_id,
            "lean_env_id": self.lean_env_id,
            "response_profile": "lean_probe_v1",
        }
        job_id: str | None = None
        response: dict[str, Any] = {}
        try:
            response = self._request(
                "POST",
                "/api/lean/jobs",
                payload,
                timeout_seconds=min(30.0, timeout + 5.0),
            )
            raw_job_id = response.get("job_id") or response.get("id")
            job_id = str(raw_job_id) if raw_job_id else None
            if job_id:
                self._observe_lifecycle("submitted", {"job_id": job_id, "kind": "probe"})
            if not job_id and not _terminal(response):
                raise EvaluatorError("Judge returned a nonterminal probe receipt without a job id")
            observed = time.monotonic()
            lifecycle = _job_lifecycle_budget_seconds(
                response,
                execution_timeout=timeout,
                maximum_lifecycle_seconds=self.max_lifecycle_seconds,
            )
            settle_by = observed + lifecycle + self.settlement_grace_seconds
            if deadline_monotonic is not None:
                settle_by = min(settle_by, deadline_monotonic)
            while job_id and not _terminal(response) and time.monotonic() < settle_by:
                remaining = settle_by - time.monotonic()
                wait_ms = max(1, min(1_000, int(remaining * 1_000)))
                response = self._request(
                    "GET",
                    f"/api/lean/jobs/{quote(job_id, safe='')}?wait_ms={wait_ms}",
                    timeout_seconds=max(0.1, remaining),
                )
                if not _terminal(response):
                    time.sleep(min(self.poll_interval_seconds, max(0.0, settle_by - time.monotonic())))
            if job_id and not _terminal(response):
                response, cancel_error = self._cancel_and_reconcile(job_id, response)
                safe = _safe_probe_response(response)
                safe.update(
                    {
                        "status": "probe_timeout",
                        "error_kind": "probe_lifecycle_timeout",
                        "elapsed_ms": int((time.monotonic() - started) * 1_000),
                        "cancel_outcome": "error" if cancel_error else "requested",
                    }
                )
                return safe
            if job_id:
                self._observe_lifecycle("settled", {"job_id": job_id, "kind": "probe"})
            safe = _safe_probe_response(response)
            safe["elapsed_ms"] = int((time.monotonic() - started) * 1_000)
            return safe
        except EvaluatorOverloadedError as exc:
            return {
                **_safe_probe_response(exc.response),
                "status": "probe_admission_closed",
                "error_kind": "judge_admission_overloaded",
                "elapsed_ms": int((time.monotonic() - started) * 1_000),
            }
        except (EvaluatorError, OSError, UnicodeError) as exc:
            if job_id and not _terminal(response):
                response, _cancel_error = self._cancel_and_reconcile(job_id, response)
            return {
                **_safe_probe_response(response),
                "status": "probe_transport_error",
                "error_kind": "probe_transport",
                "error_message": _sanitize_diagnostic_text(str(exc)),
                "elapsed_ms": int((time.monotonic() - started) * 1_000),
            }

    def _observe_lifecycle(self, event: str, payload: Mapping[str, Any]) -> None:
        observer = self.lifecycle_observer
        if observer is None:
            return
        try:
            observer(event, dict(payload))
        except Exception:
            # Telemetry must never broaden evaluator authority or change a
            # verdict. The broker separately records the final call outcome.
            pass

    def evaluate(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        started = time.monotonic()
        try:
            candidate_bytes = candidate_path.read_bytes()
        except OSError as exc:
            return Verdict(
                task.slug,
                "MISSING_CANDIDATE",
                0.0,
                time.monotonic() - started,
                error=str(exc),
            )
        return self.evaluate_bytes(
            task,
            candidate_bytes,
            deadline_monotonic=deadline_monotonic,
            started=started,
        )

    def evaluate_bytes(
        self,
        task: Task,
        candidate_bytes: bytes,
        *,
        deadline_monotonic: float | None = None,
        started: float | None = None,
    ) -> Verdict:
        """Evaluate bytes already captured by the trusted broker."""

        observed_started = time.monotonic() if started is None else float(started)
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        target_sha256 = hashlib.sha256(task.baseline_code.encode("utf-8")).hexdigest()
        try:
            candidate_bytes.decode("utf-8")
        except UnicodeError as exc:
            return Verdict(
                task.slug,
                "LOCAL_REJECTED",
                0.0,
                time.monotonic() - observed_started,
                {
                    "reason": "candidate is not valid UTF-8",
                    "candidate_sha256": candidate_sha256,
                    "target_sha256": target_sha256,
                    "binding_status": "broker_bytes_bound",
                },
                error=str(exc),
            )
        verdict = self._evaluate(
            task,
            candidate_bytes,
            candidate_sha256=candidate_sha256,
            deadline_monotonic=deadline_monotonic,
            started=observed_started,
            terminal_overload_retries=self.terminal_overload_retries,
        )
        verdict.response.update(
            {
                "candidate_sha256": candidate_sha256,
                "target_sha256": target_sha256,
                "bound_task_id": task.slug,
                "bound_problem_id": task.problem_id,
                "binding_status": "broker_bytes_bound",
            }
        )
        return verdict

    def _evaluate(
        self,
        task: Task,
        candidate_bytes: bytes,
        *,
        candidate_sha256: str,
        deadline_monotonic: float | None,
        started: float,
        terminal_overload_retries: int,
    ) -> Verdict:
        if (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        ):
            return Verdict(task.slug, "OUT_OF_HORIZON", 0.0, 0.0, {"reason": "run_horizon_elapsed"})
        job_id: str | None = None
        response: dict[str, Any] = {}
        last_poll_error: str | None = None
        try:
            code = candidate_bytes.decode("utf-8")
            target = task.baseline_code
            local_error = _local_contract_error(task, code, target)
            if local_error:
                return Verdict(
                    task_id=task.slug,
                    status="LOCAL_REJECTED",
                    score=0.0,
                    elapsed_seconds=time.monotonic() - started,
                    response={"reason": local_error},
                )
            remaining_horizon = (
                deadline_monotonic - time.monotonic()
                if deadline_monotonic is not None
                else None
            )
            if remaining_horizon is not None and remaining_horizon < 1.0:
                return Verdict(
                    task.slug,
                    "OUT_OF_HORIZON",
                    0.0,
                    time.monotonic() - started,
                    {"reason": "run_horizon_elapsed_before_submission"},
                )
            execution_timeout = self.timeout_seconds
            if remaining_horizon is not None:
                execution_timeout = min(execution_timeout, max(1, int(remaining_horizon)))
            payload = {
                "code": code,
                "target_code": target,
                "timeout": execution_timeout,
                # Preserve the established evaluator contract: one retry after
                # a backend command timeout.  Admission/transport settlement
                # remains a separate job-lifecycle concern.
                "max_retries": 1,
                "problem_id": task.problem_id,
                "lean_env_id": self.lean_env_id,
                "verification_profile": self.verification_profile,
                "judge_mode": self.judge_mode,
            }
            admission_deadline = time.monotonic() + self.admission_retry_seconds
            if deadline_monotonic is not None:
                admission_deadline = min(admission_deadline, deadline_monotonic)
            admission_attempt = 0
            last_admission_rejection: dict[str, Any] | None = None
            while True:
                remaining_admission = admission_deadline - time.monotonic()
                if remaining_admission <= 0:
                    horizon_elapsed = (
                        deadline_monotonic is not None
                        and time.monotonic() >= deadline_monotonic
                    )
                    rejection_response = _safe_response(
                        last_admission_rejection or {}
                    )
                    rejection_response.update(
                        {
                            "reason": (
                                "run_horizon_elapsed_during_admission"
                                if horizon_elapsed
                                else "judge_admission_retry_exhausted"
                            ),
                            "retryable": True,
                            "admission_attempts": admission_attempt,
                        }
                    )
                    return Verdict(
                        task.slug,
                        "OUT_OF_HORIZON" if horizon_elapsed else "REJECTED_OVERLOADED",
                        0.0,
                        time.monotonic() - started,
                        rejection_response,
                    )
                admission_attempt += 1
                submit_timeout = max(
                    0.1,
                    min(30.0, remaining_admission),
                )
                try:
                    submitted = self._request(
                        "POST",
                        "/api/lean/jobs",
                        payload,
                        timeout_seconds=submit_timeout,
                    )
                    if not _retryable_admission_rejection(submitted):
                        break
                    last_admission_rejection = submitted
                except EvaluatorOverloadedError as exc:
                    if exc.response:
                        last_admission_rejection = exc.response
                remaining_admission = admission_deadline - time.monotonic()
                if remaining_admission > 0:
                    time.sleep(
                        min(
                            remaining_admission,
                            self.poll_interval_seconds * min(4, admission_attempt),
                        )
                    )
            raw_job_id = submitted.get("job_id") or submitted.get("id")
            job_id = str(raw_job_id) if raw_job_id else None
            if job_id:
                self._observe_lifecycle("submitted", {"job_id": job_id, "kind": "evaluation"})
            response = submitted
            if not job_id and not _terminal(response):
                return Verdict(
                    task_id=task.slug,
                    status="EVALUATOR_ERROR",
                    score=0.0,
                    elapsed_seconds=time.monotonic() - started,
                    response=_safe_nonterminal_response(response),
                    error="Judge returned a nonterminal admission receipt without a job id",
                )
            if deadline_monotonic is not None:
                settlement_grace = min(5.0, self.settlement_grace_seconds)
                settlement_deadline = deadline_monotonic + settlement_grace
            else:
                settlement_grace = self.settlement_grace_seconds
                submission_observed = time.monotonic()
                lifecycle_budget = _job_lifecycle_budget_seconds(
                    response,
                    execution_timeout=execution_timeout,
                    maximum_lifecycle_seconds=self.max_lifecycle_seconds,
                )
                settlement_deadline = (
                    submission_observed + lifecycle_budget + settlement_grace
                )
            abandoned_by_client = False
            if job_id and not _terminal(response):
                while time.monotonic() < settlement_deadline:
                    remaining = settlement_deadline - time.monotonic()
                    wait_ms = max(1, min(1_000, int(remaining * 1_000)))
                    try:
                        response = self._request(
                            "GET",
                            f"/api/lean/jobs/{quote(job_id, safe='')}?wait_ms={wait_ms}",
                            timeout_seconds=max(0.1, remaining),
                        )
                    except EvaluatorError as exc:
                        last_poll_error = str(exc)
                        if time.monotonic() >= settlement_deadline:
                            break
                        time.sleep(min(self.poll_interval_seconds, max(0.0, settlement_deadline - time.monotonic())))
                        continue
                    if _terminal(response):
                        break
                    if deadline_monotonic is None:
                        # Newer Judge receipts expose the authoritative whole-
                        # job lifecycle budget.  A legacy submit response may
                        # gain it on a later poll, so only ever extend here.
                        lifecycle_budget = _job_lifecycle_budget_seconds(
                            response,
                            execution_timeout=execution_timeout,
                            maximum_lifecycle_seconds=self.max_lifecycle_seconds,
                        )
                        settlement_deadline = max(
                            settlement_deadline,
                            submission_observed + lifecycle_budget + settlement_grace,
                        )
                    time.sleep(min(self.poll_interval_seconds, max(0.0, settlement_deadline - time.monotonic())))
            if job_id and not _terminal(response):
                abandoned_by_client = True
                response, cancel_error = self._cancel_and_reconcile(job_id, response)
                if cancel_error:
                    last_poll_error = cancel_error
            if _retryable_admission_rejection(response):
                retried = self._retry_terminal_overload(
                    task,
                    candidate_bytes,
                    candidate_sha256=candidate_sha256,
                    deadline_monotonic=deadline_monotonic,
                    started=started,
                    terminal_overload_retries=terminal_overload_retries,
                )
                if retried is not None:
                    return retried
            if abandoned_by_client and _terminal(response):
                abandoned_status, proved, outcome_error = _settled_outcome(
                    response,
                    expected_candidate_sha256=candidate_sha256,
                )
                if outcome_error:
                    return Verdict(
                        task_id=task.slug,
                        status="EVALUATOR_ERROR",
                        score=0.0,
                        elapsed_seconds=time.monotonic() - started,
                        response=_safe_nonterminal_response(response),
                        error=outcome_error,
                    )
                # A completion racing with DELETE remains authoritative.  A
                # cancellation caused by our own deadline is instead a client
                # lifecycle failure, never an ordinary zero-score verdict.
                if proved or abandoned_status != "CANCELLED":
                    return Verdict(
                        task_id=task.slug,
                        status="PROVED" if proved else abandoned_status,
                        score=1.0 if proved else 0.0,
                        elapsed_seconds=time.monotonic() - started,
                        response=_safe_response(response),
                    )
                safe_response = _safe_response(response)
                safe_response["reason"] = (
                    "run_horizon_elapsed_during_evaluation"
                    if deadline_monotonic is not None
                    else "judge_lifecycle_deadline_elapsed"
                )
                return Verdict(
                    task_id=task.slug,
                    status=(
                        "OUT_OF_HORIZON"
                        if deadline_monotonic is not None
                        else "EVALUATOR_TIMEOUT"
                    ),
                    score=0.0,
                    elapsed_seconds=time.monotonic() - started,
                    response=safe_response,
                    error=(
                        None
                        if deadline_monotonic is not None
                        else "Judge job exceeded its advertised lifecycle budget"
                    ),
                )
            if not _terminal(response):
                horizon_elapsed = deadline_monotonic is not None and time.monotonic() >= deadline_monotonic
                safe_response = _safe_nonterminal_response(response)
                safe_response["reason"] = (
                    "run_horizon_elapsed_during_evaluation"
                    if horizon_elapsed
                    else "judge_settlement_timeout"
                )
                if last_poll_error:
                    safe_response["settlement_error"] = last_poll_error
                return Verdict(
                    task_id=task.slug,
                    status="OUT_OF_HORIZON" if horizon_elapsed else "EVALUATOR_TIMEOUT",
                    score=0.0,
                    elapsed_seconds=time.monotonic() - started,
                    response=safe_response,
                    error=None if horizon_elapsed else "Judge job did not reach a terminal state",
                )
            if job_id:
                self._observe_lifecycle("settled", {"job_id": job_id, "kind": "evaluation"})
            status, proved, outcome_error = _settled_outcome(
                response,
                expected_candidate_sha256=candidate_sha256,
            )
            if outcome_error:
                return Verdict(
                    task_id=task.slug,
                    status="EVALUATOR_ERROR",
                    score=0.0,
                    elapsed_seconds=time.monotonic() - started,
                    response=_safe_nonterminal_response(response),
                    error=outcome_error,
                )
            return Verdict(
                task_id=task.slug,
                status="PROVED" if proved else status,
                score=1.0 if proved else 0.0,
                elapsed_seconds=time.monotonic() - started,
                response=_safe_response(response),
            )
        except (OSError, EvaluatorError, UnicodeError) as exc:
            if job_id and not _terminal(response):
                response, cancel_error = self._cancel_and_reconcile(job_id, response)
                if _retryable_admission_rejection(response):
                    retried = self._retry_terminal_overload(
                        task,
                        candidate_bytes,
                        candidate_sha256=candidate_sha256,
                        deadline_monotonic=deadline_monotonic,
                        started=started,
                        terminal_overload_retries=terminal_overload_retries,
                    )
                    if retried is not None:
                        return retried
                reconciled_status, proved, outcome_error = _settled_outcome(
                    response,
                    expected_candidate_sha256=candidate_sha256,
                )
                if (
                    _terminal(response)
                    and reconciled_status != "CANCELLED"
                    and outcome_error is None
                ):
                    return Verdict(
                        task_id=task.slug,
                        status="PROVED" if proved else reconciled_status,
                        score=1.0 if proved else 0.0,
                        elapsed_seconds=time.monotonic() - started,
                        response=_safe_response(response),
                    )
                if cancel_error:
                    response = {**response, "settlement_error": cancel_error}
            return Verdict(
                task_id=task.slug,
                status="EVALUATOR_ERROR",
                score=0.0,
                elapsed_seconds=time.monotonic() - started,
                response=(
                    _safe_nonterminal_response(response)
                    if _verdict_status(response) in _NONTERMINAL_STATUSES
                    or not _terminal(response)
                    else _safe_response(response)
                ),
                error=str(exc),
            )

    def _retry_terminal_overload(
        self,
        task: Task,
        candidate_bytes: bytes,
        *,
        candidate_sha256: str,
        deadline_monotonic: float | None,
        started: float,
        terminal_overload_retries: int,
    ) -> Verdict | None:
        """Resubmit once a previous job is definitively terminal and retryable."""

        if terminal_overload_retries <= 0:
            return None
        if (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        ):
            return None
        verdict = self._evaluate(
            task,
            candidate_bytes,
            candidate_sha256=candidate_sha256,
            deadline_monotonic=deadline_monotonic,
            started=started,
            terminal_overload_retries=terminal_overload_retries - 1,
        )
        prior = verdict.response.get("evaluator_overload_resubmissions", 0)
        verdict.response["evaluator_overload_resubmissions"] = (
            int(prior) + 1 if isinstance(prior, int) else 1
        )
        return verdict

    def _cancel_and_reconcile(
        self,
        job_id: str,
        response: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        """Boundedly cancel an abandoned job and recover its terminal receipt."""

        current = dict(response)
        error: str | None = None
        encoded = quote(job_id, safe="")
        try:
            self._observe_lifecycle("cancel_requested", {"job_id": job_id})
            cancelled = self._request(
                "DELETE",
                f"/api/lean/jobs/{encoded}",
                timeout_seconds=min(5.0, self.cancel_grace_seconds),
            )
            if cancelled:
                current = cancelled
        except EvaluatorError as exc:
            error = str(exc)

        deadline = time.monotonic() + self.cancel_grace_seconds
        while not _terminal(current) and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            wait_ms = max(1, min(500, int(remaining * 1_000)))
            try:
                current = self._request(
                    "GET",
                    f"/api/lean/jobs/{encoded}?wait_ms={wait_ms}",
                    timeout_seconds=max(0.1, remaining),
                )
            except EvaluatorError as exc:
                error = str(exc)
                break
            if not _terminal(current):
                time.sleep(min(self.poll_interval_seconds, max(0.0, deadline - time.monotonic())))
        if _terminal(current):
            self._observe_lifecycle("settled", {"job_id": job_id, "kind": "cancel_reconcile"})
        return current, error


class MockEvaluator:
    """Offline smoke evaluator; never represents a paper score."""

    def __init__(self, *, prove_without_sorry: bool = False):
        self.prove_without_sorry = prove_without_sorry

    def health(self) -> dict[str, Any]:
        return {"ok": True, "mock": True}

    def evaluate(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        try:
            candidate_bytes = candidate_path.read_bytes()
        except OSError as exc:
            return Verdict(task.slug, "MISSING_CANDIDATE", 0.0, 0.0, error=str(exc))
        return self.evaluate_bytes(task, candidate_bytes, deadline_monotonic=deadline_monotonic)

    def evaluate_bytes(
        self,
        task: Task,
        candidate_bytes: bytes,
        *,
        deadline_monotonic: float | None = None,
        started: float | None = None,
    ) -> Verdict:
        del deadline_monotonic, started
        try:
            code = candidate_bytes.decode("utf-8")
        except UnicodeError as exc:
            return Verdict(task.slug, "LOCAL_REJECTED", 0.0, 0.0, error=str(exc))
        proved = self.prove_without_sorry and "sorry" not in code and "admit" not in code
        candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
        canonical_status = "PROVED" if proved else "VERIFY_FAIL"
        return Verdict(
            task.slug,
            "PROVED" if proved else "MOCK_SKIPPED",
            1.0 if proved else 0.0,
            0.0,
            {
                "mock": True,
                "candidate_sha256": candidate_sha256,
                "canonical_verdict": {
                    "schema_version": FORMAL_VERDICT_SCHEMA_VERSION,
                    "status": canonical_status,
                    "score": 1.0 if proved else 0.0,
                    "correct": proved,
                    "cheating": False,
                    "source_contract_status": "ok",
                    "signature_check_status": "ok",
                    "solution_hash": candidate_sha256,
                },
                "is_valid_no_sorry": proved,
            },
        )

    def probe(
        self,
        task: Task,
        source: str,
        *,
        timeout_seconds: int = 30,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        del task, timeout_seconds, deadline_monotonic
        failed = "unknown_constant" in source or "BROKEN_PROBE" in source
        return {
            "status": "elab_failed" if failed else "elaborated",
            "is_valid_with_sorry": not failed,
            "is_valid_no_sorry": not failed and "sorry" not in source,
            "diagnostics": (
                [{"severity": "error", "message": "unknown constant"}] if failed else []
            ),
            "elapsed_ms": 0,
            "mock": True,
        }


def _status(payload: Mapping[str, Any]) -> str:
    for key in ("formal_status", "verdict", "result"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    canonical = payload.get("canonical_verdict")
    if isinstance(canonical, Mapping):
        value = canonical.get("status")
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    value = payload.get("status")
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    nested = payload.get("response")
    if isinstance(nested, Mapping):
        return _status(nested)
    return "UNKNOWN"


def _nested_value(payload: Mapping[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is not None:
        return value
    nested = payload.get("response")
    if isinstance(nested, Mapping):
        return _nested_value(nested, key)
    canonical = payload.get("canonical_verdict")
    if isinstance(canonical, Mapping):
        return canonical.get(key)
    return None


def _raw_lifecycle_status(payload: Mapping[str, Any]) -> str:
    """Read the transport/job lifecycle status, including wrapped receipts."""

    value = payload.get("status")
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    nested = payload.get("response")
    if isinstance(nested, Mapping):
        return _raw_lifecycle_status(nested)
    return ""


def _retryable_admission_rejection(payload: Mapping[str, Any]) -> bool:
    """Return true only for a terminal overload that created no live work."""

    return (
        _raw_lifecycle_status(payload) == "REJECTED_OVERLOADED"
        and _nested_value(payload, "retryable") is True
        and _terminal(payload)
    )


def _job_lifecycle_budget_seconds(
    payload: Mapping[str, Any],
    *,
    execution_timeout: int,
    maximum_lifecycle_seconds: float,
) -> float:
    """Return a conservative whole-job budget from the Judge receipt.

    ``timeout`` is per backend command, not submission-to-terminal wall time.
    A formal cache miss may legally use separate queue, header, body, signature,
    and SafeVerify stages.  New Judge versions publish their computed lifecycle
    deadline; the legacy fallback reconstructs a conservative upper bound from
    the queue deadline already present in older receipts.
    """

    def milliseconds(key: str) -> float | None:
        value = _nested_value(payload, key)
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None

    timeout = max(1.0, float(execution_timeout))

    def checked(budget: float) -> float:
        result = max(timeout, budget)
        if result > maximum_lifecycle_seconds:
            raise EvaluatorError(
                "Judge lifecycle budget exceeds the client safety cap "
                f"({result:.3f}s > {maximum_lifecycle_seconds:.3f}s)"
            )
        return result

    submitted_at = milliseconds("submitted_at_ms")
    lifecycle_deadline = milliseconds("lifecycle_deadline_ms")
    if (
        submitted_at is not None
        and lifecycle_deadline is not None
        and lifecycle_deadline >= submitted_at
    ):
        return checked((lifecycle_deadline - submitted_at) / 1_000.0)

    queue_deadline = milliseconds("queue_deadline_ms")
    if (
        submitted_at is None
        or queue_deadline is None
        or queue_deadline < submitted_at
    ):
        return checked(timeout)
    queue_budget = (queue_deadline - submitted_at) / 1_000.0
    # Formal REPL cache miss: initial queue + header/body commands.  Signature
    # and SafeVerify finalization may each consume another queue/command budget.
    # This intentionally over-bounds fast/official profiles rather than
    # cancelling a valid proof before the server's own lifecycle can settle.
    # max_retries=1 permits two main verification attempts; a cold cached REPL
    # can spend one additional command on its header, then formal signature and
    # SafeVerify finalization can each spend one command.
    return checked((3.0 * queue_budget) + (5.0 * timeout) + 20.0)


def _verdict_status(payload: Mapping[str, Any]) -> str:
    """Preserve Judge lifecycle failures instead of flattening them to network."""

    raw_status = _raw_lifecycle_status(payload)
    error_kind = str(_nested_value(payload, "error_kind") or "").strip().lower()
    terminal_reason = str(_nested_value(payload, "terminal_reason") or "").strip().lower()
    if raw_status in {"CANCELLED", "CANCELED"}:
        return "CANCELLED"
    if raw_status == "REJECTED_OVERLOADED":
        return "REJECTED_OVERLOADED"
    if raw_status == "TIMED_OUT" or error_kind == "timeout" or terminal_reason == "execution_timeout":
        return "EXECUTION_TIMEOUT"
    if error_kind in {"memory_limit_exceeded", "resource_limit", "resource_exhausted"}:
        return "RESOURCE_LIMIT"
    if error_kind == "overloaded" or terminal_reason == "queue_wait_timeout":
        return "REJECTED_OVERLOADED"
    if terminal_reason in {"cancelled", "canceled"}:
        return "CANCELLED"
    canonical = _canonical_verdict(payload)
    status = (
        str(canonical.get("status") or "").strip().upper()
        if canonical is not None
        else _status(payload)
    )
    # Lifecycle state is authoritative over stale nested verdict fields. A
    # failed receipt carrying an old proof marker must never score.
    if raw_status in _RAW_FAILURE_STATUSES and status == "PROVED":
        return "EVALUATOR_ERROR"
    if status == "NETWORK_ERROR":
        return "INFRASTRUCTURE_ERROR"
    return status


def _settled_outcome(
    payload: Mapping[str, Any],
    *,
    expected_candidate_sha256: str | None = None,
) -> tuple[str, bool, str | None]:
    """Resolve only a supported Judge canonical verdict.

    Transport/lifecycle state can prove that a request failed, but it can
    never derive positive correctness. A legacy ``PASS``/``AC`` marker or a
    top-level ``success=true`` is therefore diagnostic-only.
    """

    status = _verdict_status(payload)
    if status in _NONTERMINAL_STATUSES:
        return (
            "EVALUATOR_ERROR",
            False,
            "Judge returned a contradictory terminal envelope",
        )
    raw_status = _raw_lifecycle_status(payload)
    canonical = _canonical_verdict(payload)

    # A transport receipt can authoritatively establish a negative lifecycle
    # outcome even when the backend never reached the formal-verdict stage.
    # It can never establish correctness. Preserve specific timeout/resource/
    # cancellation/overload outcomes, while treating a stale canonical PROVED
    # marker on the same failed receipt as a contradiction.
    if raw_status in _RAW_FAILURE_STATUSES:
        canonical_status = (
            str(canonical.get("status") or "").strip().upper()
            if canonical is not None
            else ""
        )
        if canonical_status == "PROVED":
            return (
                "EVALUATOR_ERROR",
                False,
                "Judge lifecycle failure contradicts a canonical proof verdict",
            )
        lifecycle_status = _verdict_status(payload)
        if lifecycle_status in {
            "CANCELLED",
            "EXECUTION_TIMEOUT",
            "INFRASTRUCTURE_ERROR",
            "REJECTED_OVERLOADED",
            "RESOURCE_LIMIT",
        }:
            return lifecycle_status, False, None
        if canonical is None or _validate_canonical_verdict(canonical) is not None:
            return (
                "EVALUATOR_ERROR",
                False,
                "Judge failed before producing an authoritative formal verdict",
            )

    if canonical is None:
        return (
            "EVALUATOR_ERROR",
            False,
            "Judge terminal receipt lacks an authoritative verdict (canonical_verdict)",
        )
    canonical_error = _validate_canonical_verdict(canonical)
    if canonical_error is not None:
        return (
            "EVALUATOR_ERROR",
            False,
            f"Judge canonical verdict is malformed: {canonical_error}",
        )
    declared_schema_value = _nested_value(payload, "formal_verdict_schema_version")
    declared_schema = str(declared_schema_value or "").strip()
    if declared_schema and declared_schema != FORMAL_VERDICT_SCHEMA_VERSION:
        return (
            "EVALUATOR_ERROR",
            False,
            "Judge formal_verdict_schema_version contradicts canonical_verdict",
        )

    canonical_status = str(canonical["status"]).strip().upper()
    if canonical_status == "PROVED":
        proved_error = _strict_proved_error(
            payload,
            canonical,
            expected_candidate_sha256=expected_candidate_sha256,
        )
        if proved_error is not None:
            return "EVALUATOR_ERROR", False, proved_error
        return "PROVED", True, None
    if status == "EVALUATOR_ERROR" and raw_status in _RAW_FAILURE_STATUSES:
        return status, False, "Judge lifecycle failure contradicts its canonical verdict"
    return (
        "INFRASTRUCTURE_ERROR" if canonical_status == "NETWORK_ERROR" else canonical_status,
        False,
        None,
    )


def _terminal(payload: Mapping[str, Any]) -> bool:
    normalized_raw_status = _raw_lifecycle_status(payload)
    if normalized_raw_status in _NONTERMINAL_STATUSES:
        return False
    if normalized_raw_status in {
        "SUCCEEDED",
        "COMPLETED",
        "FAILED",
        "TIMED_OUT",
        "ERROR",
        "CANCELLED",
        "CANCELED",
        "REJECTED_OVERLOADED",
    }:
        return True
    status = _status(payload)
    if status in _NONTERMINAL_STATUSES:
        return False
    return bool(
        payload.get("terminal")
        or payload.get("finished_at")
        or payload.get("finished_at_ms")
        or status in {
            "PROVED",
            "AC",
            "PASS",
            "PASSED",
            "SUCCEEDED",
            "FAILED",
            "ERROR",
            "WA",
            "VERIFY_FAIL",
            "COMPILES_WITH_SORRY",
            "REJECTED_OVERLOADED",
            "NETWORK_ERROR",
            "INFRASTRUCTURE_ERROR",
            "EXECUTION_TIMEOUT",
            "RESOURCE_LIMIT",
            "TIMED_OUT",
            "CANCELLED",
        }
    )


def _canonical_verdict(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    canonical = payload.get("canonical_verdict")
    if isinstance(canonical, Mapping):
        return canonical
    nested = payload.get("response")
    if isinstance(nested, Mapping):
        return _canonical_verdict(nested)
    return None


def _validate_canonical_verdict(canonical: Mapping[str, Any]) -> str | None:
    schema_version = str(canonical.get("schema_version") or "").strip()
    if schema_version != FORMAL_VERDICT_SCHEMA_VERSION:
        return (
            "canonical_verdict.schema_version is unsupported: "
            f"{schema_version or '<missing>'}"
        )
    status = str(canonical.get("status") or "").strip().upper()
    if status not in _ALLOWED_CANONICAL_STATUSES:
        return f"unsupported canonical verdict status: {status or '<missing>'}"
    correct = canonical.get("correct")
    cheating = canonical.get("cheating")
    score = canonical.get("score")
    if not isinstance(correct, bool):
        return "canonical_verdict.correct must be a boolean"
    if not isinstance(cheating, bool):
        return "canonical_verdict.cheating must be a boolean"
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
        return "canonical_verdict.score must be a finite number"
    numeric_score = float(score)
    if status == "PROVED" and (not correct or cheating or numeric_score != 1.0):
        return "canonical PROVED must be correct=true, cheating=false, score=1"
    if status == "CHEATING" and (correct or not cheating or numeric_score != 0.0):
        return "canonical CHEATING must be correct=false, cheating=true, score=0"
    if status in {"VERIFY_FAIL", "NETWORK_ERROR"} and numeric_score != 0.0:
        return f"canonical {status} must have score=0"
    if status == "COMPILES_WITH_SORRY" and not 0.0 <= numeric_score < 1.0:
        return "canonical COMPILES_WITH_SORRY must have 0<=score<1"
    if correct and status != "PROVED":
        return "canonical correct=true is only valid for PROVED"
    if cheating and status != "CHEATING":
        return "canonical cheating=true is only valid for CHEATING"
    return None


def _strict_proved_error(
    payload: Mapping[str, Any],
    canonical: Mapping[str, Any],
    *,
    expected_candidate_sha256: str | None,
) -> str | None:
    if _nested_value(payload, "is_valid_no_sorry") is not True:
        return "canonical PROVED is missing is_valid_no_sorry=true"
    source_status = str(
        canonical.get("source_contract_status")
        or _nested_value(payload, "source_contract_status")
        or ""
    ).strip().lower()
    if source_status != "ok":
        return "canonical PROVED requires source_contract_status=ok"
    signature_status = str(
        canonical.get("signature_check_status")
        or _nested_value(payload, "signature_check_status")
        or ""
    ).strip().lower()
    if signature_status != "ok":
        return "canonical PROVED requires signature_check_status=ok"
    import_status = canonical.get("import_check_status")
    if import_status is None:
        import_status = _nested_value(payload, "import_check_status")
    if import_status is not None and str(import_status).strip().lower() != "ok":
        return "canonical PROVED requires import_check_status=ok when supplied"
    safeverify_status = str(
        canonical.get("safeverify_status")
        or _nested_value(payload, "safeverify_status")
        or ""
    ).strip().lower()
    if safeverify_status in {"failed", "error", "rejected"}:
        return "canonical PROVED contradicts failed SafeVerify"
    solution_hash = canonical.get("solution_hash")
    if solution_hash is None:
        solution_hash = _nested_value(payload, "solution_hash")
    if solution_hash not in {None, ""}:
        observed = str(solution_hash).strip().lower()
        if expected_candidate_sha256 is not None and observed != expected_candidate_sha256.lower():
            return "canonical solution_hash does not match the broker-bound candidate"
    return None


def _is_proved(
    payload: Mapping[str, Any],
    *,
    expected_candidate_sha256: str | None = None,
) -> bool:
    raw_status = _raw_lifecycle_status(payload)
    if raw_status in _NONTERMINAL_STATUSES or raw_status in _RAW_FAILURE_STATUSES:
        return False
    canonical = _canonical_verdict(payload)
    return bool(
        canonical is not None
        and _validate_canonical_verdict(canonical) is None
        and str(canonical.get("status") or "").strip().upper() == "PROVED"
        and _strict_proved_error(
            payload,
            canonical,
            expected_candidate_sha256=expected_candidate_sha256,
        )
        is None
    )


def _safe_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep structured verdict metadata while avoiding full source/log dumps."""
    allowed = {
        "job_id",
        "id",
        "status",
        "formal_status",
        "verdict",
        "terminal",
        "correct",
        "accepted",
        "success",
        "is_valid_no_sorry",
        "is_valid_with_sorry",
        "formal_verdict_schema_version",
        "score",
        "cheating",
        "source_contract_status",
        "signature_check_status",
        "import_check_status",
        "safeverify_status",
        "solution_hash",
        "verdict_authority",
        "official_comparator_status",
        "error_code",
        "error_message",
        "error_kind",
        "terminal_reason",
        "retryable",
        "cancel_requested",
        "queue_wait_ms",
        "execution_ms",
        "submitted_at_ms",
        "queue_deadline_ms",
        "lifecycle_deadline_ms",
        "started_at_ms",
        "finished_at_ms",
        "finalization_pending",
        "queue_wait_seconds",
        "execution_seconds",
        "reason",
        "settlement_error",
    }
    result = {key: payload[key] for key in allowed if key in payload}
    raw_errors = payload.get("error_message")
    if isinstance(raw_errors, str):
        result["error_message"] = _sanitize_diagnostic_text(raw_errors)
    elif isinstance(raw_errors, list):
        result["error_message"] = [
            _sanitize_diagnostic_text(str(item))
            for item in raw_errors[:24]
            if str(item).strip()
        ]
    diagnostics, diagnostics_truncated = _probe_diagnostic_items(
        payload.get("probe_diagnostics")
    )
    if diagnostics:
        result["probe_diagnostics"] = _safe_diagnostics(diagnostics)
    if diagnostics_truncated:
        result["probe_diagnostics_truncated"] = True
    if isinstance(payload.get("response"), Mapping):
        result["response"] = _safe_response(payload["response"])
    canonical = payload.get("canonical_verdict")
    if isinstance(canonical, Mapping):
        result["canonical_verdict"] = {
            key: canonical[key]
            for key in (
                "schema_version",
                "status",
                "score",
                "correct",
                "cheating",
                "source_contract_status",
                "signature_check_status",
                "import_check_status",
                "safeverify_status",
                "solution_hash",
                "verdict_authority",
                "official_comparator_status",
            )
            if key in canonical
        }
    return result


_PRIVATE_DIAGNOSTIC_PATTERN = re.compile(
    r"https?://\S+|/(?:home|tmp|workspace|opt|mnt|var|root|scratch)/[^\s,;:)\]}\"']*|"
    r"(?i:\bBearer\s+[A-Za-z0-9._~+\-/=]+)",
)


def _sanitize_diagnostic_text(value: str) -> str:
    return _PRIVATE_DIAGNOSTIC_PATTERN.sub("[redacted]", str(value or ""))[:2_048]


def _safe_diagnostics(raw: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in raw[:24]:
        if not isinstance(item, Mapping):
            continue
        row: dict[str, Any] = {}
        for key in ("severity", "line", "column"):
            value = item.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                row[key] = value
        message = item.get("data") if item.get("data") is not None else item.get("message")
        if message is not None:
            row["message"] = _sanitize_diagnostic_text(str(message))
        if row:
            result.append(row)
    return result


def _probe_diagnostic_items(raw: Any) -> tuple[list[Any], bool]:
    if isinstance(raw, list):
        return raw, False
    if isinstance(raw, Mapping):
        items = raw.get("items")
        return (items if isinstance(items, list) else []), raw.get("truncated") is True
    return [], False


def _safe_probe_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project a lean_probe_v1 receipt without source, paths, or raw logs."""

    result: dict[str, Any] = {}
    raw_status = _raw_lifecycle_status(payload)
    if raw_status:
        result["job_status"] = raw_status.lower()
    for key in (
        "is_valid_no_sorry",
        "is_valid_with_sorry",
        "error_kind",
        "terminal_reason",
        "queue_wait_ms",
        "execution_ms",
        "cache_hit",
        "mathlib_revision",
        "lean_version",
    ):
        value = _nested_value(payload, key)
        if isinstance(value, (str, bool, int, float)) and not (
            isinstance(value, float) and not math.isfinite(value)
        ):
            result[key] = value
    lean_environment = _nested_value(payload, "lean_environment")
    if isinstance(lean_environment, Mapping):
        result["lean_environment"] = {
            key: lean_environment[key]
            for key in ("lean_version", "mathlib_revision", "environment_id", "image_digest")
            if isinstance(lean_environment.get(key), (str, int, bool))
        }
    raw_diagnostics = payload.get("probe_diagnostics")
    if raw_diagnostics is None:
        nested = payload.get("response")
        raw_diagnostics = (
            nested.get("probe_diagnostics") if isinstance(nested, Mapping) else None
        )
    diagnostics, diagnostics_truncated = _probe_diagnostic_items(raw_diagnostics)
    if diagnostics:
        result["diagnostics"] = _safe_diagnostics(diagnostics)
    if diagnostics_truncated:
        result["diagnostics_truncated"] = True
    raw_errors = _nested_value(payload, "error_message")
    if isinstance(raw_errors, str):
        result["error_messages"] = [_sanitize_diagnostic_text(raw_errors)]
    elif isinstance(raw_errors, list):
        result["error_messages"] = [
            _sanitize_diagnostic_text(str(item))
            for item in raw_errors[:24]
            if str(item).strip()
        ]
    has_error = any(
        str(item.get("severity") or "").strip().lower() == "error"
        for item in result.get("diagnostics", [])
        if isinstance(item, Mapping)
    )
    valid_with_sorry = result.get("is_valid_with_sorry")
    if raw_status in _NONTERMINAL_STATUSES:
        result["status"] = "probe_nonterminal"
    elif has_error or valid_with_sorry is False:
        result["status"] = "elab_failed"
    elif valid_with_sorry is True:
        result["status"] = "elaborated"
    else:
        result["status"] = "probe_receipt_invalid"
    return result


def _safe_nonterminal_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Retain diagnostics without serializing a pending state as a verdict."""

    result = _safe_response(payload)
    observations: list[str] = []

    def scrub(node: dict[str, Any]) -> None:
        for key in ("status", "formal_status", "verdict"):
            value = node.get(key)
            if isinstance(value, str) and value.strip().upper() in _NONTERMINAL_STATUSES:
                observations.append(value.strip().lower())
                node.pop(key, None)
        nested = node.get("response")
        if isinstance(nested, dict):
            scrub(nested)
        canonical = node.get("canonical_verdict")
        if isinstance(canonical, dict):
            scrub(canonical)

    scrub(result)
    if observations:
        result["last_observed_lifecycle"] = observations[0]
    return result


_LEAN_DECL_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*"
_LEAN_THEOREM_START_PATTERN = re.compile(
    rf"(?m)^\s*(?:(?:@[^\n]*|private|protected|noncomputable|partial|scoped|local)\s+)*"
    rf"(?P<kind>theorem|problem)\s+(?P<name>{_LEAN_DECL_NAME_PATTERN})\b"
)
_LEAN_TOP_LEVEL_BOUNDARY_PATTERN = re.compile(
    r"(?m)^\s*(?:#|(?:local|scoped)\s+"
    r"(?:syntax|macro|macro_rules|elab|elab_rules|notation|infix|prefix|postfix|attribute)\b|"
    r"(?:theorem|problem|lemma|def|abbrev|instance|inductive|structure|class|syntax|macro|"
    r"macro_rules|elab|elab_rules|attribute|notation|infix|prefix|postfix|axiom|postulate|"
    r"constant|opaque|initialize|builtin_initialize|set_option|open|import)\b)"
)
_LEAN_NAMESPACE_LINE_PATTERN = re.compile(
    r"^\s*(?:namespace\s+[A-Za-z0-9_'.]+|end(?:\s+[A-Za-z0-9_'.]+)?)\s*$",
    re.MULTILINE,
)
_LEAN_IMPORT_LINE_PATTERN = re.compile(r"^\s*import\s+[^\n]+\s*$", re.MULTILINE)
_STRICT_FORBIDDEN_LINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("axiom/postulate/constant declarations", re.compile(r"(?m)^\s*(?:axiom|postulate|constant)\b[^\n]*")),
    ("unsafe declarations", re.compile(r"(?m)^\s*unsafe\b[^\n]*")),
    ("partial declarations", re.compile(r"(?m)^\s*partial\b[^\n]*")),
    ("extern declarations", re.compile(r"(?m)^\s*extern\b[^\n]*")),
    ("syntax declarations", re.compile(r"(?m)^\s*syntax\b[^\n]*")),
    ("macro declarations", re.compile(r"(?m)^\s*(?:macro|macro_rules)\b[^\n]*")),
    ("elab declarations", re.compile(r"(?m)^\s*(?:elab|elab_rules)\b[^\n]*")),
    ("notation declarations", re.compile(r"(?m)^\s*(?:(?:local|scoped)\s+)?(?:notation|infix|prefix|postfix)\b[^\n]*")),
    ("attribute commands", re.compile(r"(?m)^\s*(?:(?:local|scoped)\s+)?attribute\b[^\n]*")),
    ("opaque declarations", re.compile(r"(?m)^\s*opaque\b[^\n]*")),
    ("initialize commands", re.compile(r"(?m)^\s*(?:initialize|builtin_initialize)\b[^\n]*")),
    ("set_option changes", re.compile(r"(?m)^\s*set_option\b[^\n]*")),
    ("native_decide", re.compile(rf"(?<![A-Za-z0-9_']){_LEAN_DECL_NAME_PATTERN}\.native_decide(?![A-Za-z0-9_'])|(?<![A-Za-z0-9_'])native_decide(?![A-Za-z0-9_'])")),
    ("Lean.trustCompiler", re.compile(r"(?<![A-Za-z0-9_'])Lean\.trustCompiler(?![A-Za-z0-9_'])")),
    ("Lean.ofReduceBool", re.compile(r"(?<![A-Za-z0-9_'])Lean\.ofReduceBool(?![A-Za-z0-9_'])")),
    ("PASS_PROOF marker", re.compile(r"\bPASS_PROOF\b")),
)


def _strip_comments_preserve_strings(text: str) -> str:
    result: list[str] = []
    index = 0
    block_depth = 0
    in_string = False
    escape_next = False
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if block_depth:
            if char == "/" and nxt == "-":
                block_depth += 1
                index += 2
                continue
            if char == "-" and nxt == "/":
                block_depth -= 1
                index += 2
                continue
            if char == "\n":
                result.append("\n")
            index += 1
            continue
        if in_string:
            result.append(char)
            if escape_next:
                escape_next = False
            elif char == "\\":
                escape_next = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == "-" and nxt == "-":
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        if char == "/" and nxt == "-":
            block_depth = 1
            index += 2
            continue
        if char == '"':
            in_string = True
        result.append(char)
        index += 1
    return "".join(result)


def _canonical_lean_surface(text: str) -> str:
    return re.sub(r"\s+", " ", _strip_comments_preserve_strings(text).strip())


def _qualify_lean_decl_name(scan_code: str, index: int, name: str) -> str:
    namespaces: list[str] = []
    for raw_line in scan_code[:index].splitlines():
        line = raw_line.strip()
        namespace_match = re.fullmatch(r"namespace\s+([A-Za-z0-9_'.]+)", line)
        if namespace_match:
            namespaces.append(namespace_match.group(1))
        elif re.fullmatch(r"end(?:\s+[A-Za-z0-9_'.]+)?", line) and namespaces:
            namespaces.pop()
    prefix = ".".join(namespaces)
    return name if not prefix or name.startswith(prefix + ".") else f"{prefix}.{name}"


def _lean_decl_header(block: str) -> str:
    assignment = re.search(r"\s*:=", block)
    where_match = re.search(r"\s+where\b", block)
    positions = [match.start() for match in (assignment, where_match) if match is not None]
    if positions:
        return block[: min(positions)].strip()
    return block.splitlines()[0].strip() if block.splitlines() else block.strip()


def _extract_lean_theorem_surfaces(code: str) -> list[dict[str, str]]:
    scan_code = _strip_comments_preserve_strings(code)
    matches = list(_LEAN_THEOREM_START_PATTERN.finditer(scan_code))
    surfaces: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        next_decl = matches[index + 1].start() if index + 1 < len(matches) else len(scan_code)
        boundary = _LEAN_TOP_LEVEL_BOUNDARY_PATTERN.search(scan_code, match.end())
        end = min(next_decl, boundary.start()) if boundary else next_decl
        name = match.group("name")
        surfaces.append(
            {
                "name": name,
                "full_name": _qualify_lean_decl_name(scan_code, start, name),
                "header": _canonical_lean_surface(_lean_decl_header(scan_code[start:end].strip())),
            }
        )
    return surfaces


def _strict_contract_line_set(code: str, pattern: re.Pattern[str]) -> set[str]:
    scan_code = _strip_comments_preserve_strings(code)
    return {_canonical_lean_surface(match.group(0)) for match in pattern.finditer(scan_code)}


def _local_contract_error(task: Task, code: str, target: str) -> str | None:
    """Reject obvious source drift only; never derive positive correctness."""

    if not code.strip():
        return "empty candidate"
    if _strict_contract_line_set(target, _LEAN_IMPORT_LINE_PATTERN) != _strict_contract_line_set(
        code, _LEAN_IMPORT_LINE_PATTERN
    ):
        return "imports changed; preserve the original import surface exactly"
    if _strict_contract_line_set(target, _LEAN_NAMESPACE_LINE_PATTERN) != _strict_contract_line_set(
        code, _LEAN_NAMESPACE_LINE_PATTERN
    ):
        return "namespace/end surface changed"
    for reason, pattern in _STRICT_FORBIDDEN_LINE_PATTERNS:
        added = sorted(_strict_contract_line_set(code, pattern) - _strict_contract_line_set(target, pattern))
        if added:
            return f"forbidden {reason}: {added[0]}"
    originals = _extract_lean_theorem_surfaces(target)
    candidates = {item["full_name"]: item for item in _extract_lean_theorem_surfaces(code)}
    for original in originals:
        candidate = candidates.get(original["full_name"])
        if candidate is None:
            return f"original theorem `{original['full_name']}` is missing"
        if candidate["header"] != original["header"]:
            return f"theorem `{original['full_name']}` statement/signature changed"
    if task.theorem_name and not any(
        item["name"] == task.theorem_name or item["full_name"].endswith("." + task.theorem_name)
        for item in candidates.values()
    ):
        return "target theorem name is missing"
    return None
