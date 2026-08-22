"""Small HTTP client for the ContextSwarmJudge Lean router."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Mapping
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

_PROVED_STATUSES = {"PROVED", "AC", "PASS", "PASSED"}
_RAW_FAILURE_STATUSES = {
    "FAILED",
    "TIMED_OUT",
    "ERROR",
    "CANCELLED",
    "CANCELED",
    "REJECTED_OVERLOADED",
}


def normalize_base_url(raw: str) -> str:
    value = str(raw or "").strip().rstrip("/")
    for suffix in ("/api/lean/jobs", "/api/lean/verify", "/verify", "/healthz"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value.rstrip("/")


def candidate_sha256(code: str) -> str:
    """Hash the exact UTF-8 source submitted to the Judge."""

    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def task_contract_sha256(
    task: Task,
    *,
    lean_env_id: str,
    verification_profile: str,
    judge_mode: str,
) -> str:
    """Hash the immutable inputs which give a formal verdict its meaning."""

    digest = hashlib.sha256()
    for value in (
        task.slug,
        task.problem_id,
        task.theorem_name,
        task.baseline_code,
        lean_env_id,
        verification_profile,
        judge_mode,
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


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
                raw = response.read().decode("utf-8")
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

    def expected_task_contract_sha256(self, task: Task) -> str:
        """Return the contract identity used for this task's Judge request."""

        return task_contract_sha256(
            task,
            lean_env_id=self.lean_env_id,
            verification_profile=self.verification_profile,
            judge_mode=self.judge_mode,
        )

    def evaluate(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        started = time.monotonic()
        contract_sha256 = self.expected_task_contract_sha256(task)
        try:
            code = candidate_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return Verdict(
                task_id=task.slug,
                status="EVALUATOR_ERROR",
                score=0.0,
                elapsed_seconds=time.monotonic() - started,
                error=str(exc),
                task_contract_sha256=contract_sha256,
            )
        verdict = self._evaluate(
            task,
            candidate_path,
            candidate_code=code,
            deadline_monotonic=deadline_monotonic,
            started=started,
            terminal_overload_retries=self.terminal_overload_retries,
        )
        verdict.candidate_sha256 = candidate_sha256(code)
        verdict.task_contract_sha256 = contract_sha256
        raw_job_id = _nested_value(verdict.response, "job_id")
        if raw_job_id is None:
            raw_job_id = _nested_value(verdict.response, "id")
        if isinstance(raw_job_id, (str, int)) and str(raw_job_id).strip():
            verdict.judge_job_id = str(raw_job_id).strip()
        verdict.cache_reused = _nested_value(verdict.response, "cache_reused") is True
        return verdict

    def _evaluate(
        self,
        task: Task,
        candidate_path: Path,
        *,
        candidate_code: str,
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
            code = candidate_code
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
            if job_id:
                # Poll and cancellation receipts are allowed to omit the id;
                # preserve the identity from the authoritative submit receipt.
                response.setdefault("job_id", job_id)
            if _retryable_admission_rejection(response):
                retried = self._retry_terminal_overload(
                    task,
                    candidate_path,
                    candidate_code=candidate_code,
                    deadline_monotonic=deadline_monotonic,
                    started=started,
                    terminal_overload_retries=terminal_overload_retries,
                )
                if retried is not None:
                    return retried
            if abandoned_by_client and _terminal(response):
                abandoned_status, proved, outcome_error = _settled_outcome(response)
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
            status, proved, outcome_error = _settled_outcome(response)
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
            if job_id:
                response.setdefault("job_id", job_id)
            if job_id and not _terminal(response):
                response, cancel_error = self._cancel_and_reconcile(job_id, response)
                response.setdefault("job_id", job_id)
                if _retryable_admission_rejection(response):
                    retried = self._retry_terminal_overload(
                        task,
                        candidate_path,
                        candidate_code=candidate_code,
                        deadline_monotonic=deadline_monotonic,
                        started=started,
                        terminal_overload_retries=terminal_overload_retries,
                    )
                    if retried is not None:
                        return retried
                reconciled_status, proved, outcome_error = _settled_outcome(response)
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
        candidate_path: Path,
        *,
        candidate_code: str,
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
            candidate_path,
            candidate_code=candidate_code,
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
        return current, error


class MockEvaluator:
    """Offline smoke evaluator; never represents a paper score."""

    def __init__(self, *, prove_without_sorry: bool = False):
        self.prove_without_sorry = prove_without_sorry

    def health(self) -> dict[str, Any]:
        return {"ok": True, "mock": True}

    def expected_task_contract_sha256(self, task: Task) -> str:
        return task_contract_sha256(
            task,
            lean_env_id="mock",
            verification_profile="mock",
            judge_mode="mock",
        )

    def evaluate(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        try:
            code = candidate_path.read_text(encoding="utf-8")
        except OSError as exc:
            return Verdict(task.slug, "MISSING_CANDIDATE", 0.0, 0.0, error=str(exc))
        proved = self.prove_without_sorry and "sorry" not in code and "admit" not in code
        return Verdict(
            task.slug,
            "PROVED" if proved else "MOCK_SKIPPED",
            1.0 if proved else 0.0,
            0.0,
            {"mock": True},
            candidate_sha256=candidate_sha256(code),
            task_contract_sha256=self.expected_task_contract_sha256(task),
        )


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
    status = _status(payload)
    # Lifecycle state is authoritative over stale nested verdict fields.  A
    # failed receipt carrying an old PROVED marker must never score.
    if raw_status in _RAW_FAILURE_STATUSES and status in _PROVED_STATUSES:
        return "EVALUATOR_ERROR"
    if status == "NETWORK_ERROR":
        return "INFRASTRUCTURE_ERROR"
    return status


def _settled_outcome(
    payload: Mapping[str, Any],
) -> tuple[str, bool, str | None]:
    """Resolve a terminal receipt and fail closed on envelope contradictions."""

    status = _verdict_status(payload)
    if status in _NONTERMINAL_STATUSES:
        return (
            "EVALUATOR_ERROR",
            False,
            "Judge returned a contradictory terminal envelope",
        )
    proved = _is_proved(payload)
    if proved:
        return "PROVED", True, None
    if status in {"SUCCEEDED", "COMPLETED", "FAILED", "ERROR", "UNKNOWN"}:
        return (
            "EVALUATOR_ERROR",
            False,
            "Judge terminal receipt lacks an authoritative verdict",
        )
    if status == "EVALUATOR_ERROR":
        return (
            status,
            False,
            "Judge lifecycle failure contradicts a proof verdict",
        )
    return status, False, None


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


def _is_proved(payload: Mapping[str, Any]) -> bool:
    raw_status = _raw_lifecycle_status(payload)
    if raw_status in _NONTERMINAL_STATUSES or raw_status in _RAW_FAILURE_STATUSES:
        return False
    status = _status(payload)
    if status in _PROVED_STATUSES:
        return True
    if status in {"SUCCEEDED", "COMPLETED"}:
        for key in ("is_valid_no_sorry", "correct", "success", "accepted"):
            if payload.get(key) is True:
                return True
        nested = payload.get("response")
        if isinstance(nested, Mapping) and _is_proved(nested):
            return True
        canonical = payload.get("canonical_verdict")
        return isinstance(canonical, Mapping) and _status(canonical) in _PROVED_STATUSES
    return False


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
        "error_code",
        "error_message",
        "error_kind",
        "terminal_reason",
        "retryable",
        "cache_reused",
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
    if isinstance(payload.get("response"), Mapping):
        result["response"] = _safe_response(payload["response"])
    canonical = payload.get("canonical_verdict")
    if isinstance(canonical, Mapping):
        result["canonical_verdict"] = {
            key: canonical[key]
            for key in ("status", "score", "correct", "cheating", "source_contract_status")
            if key in canonical
        }
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


def _local_contract_error(task: Task, code: str, target: str) -> str | None:
    if not code.strip():
        return "empty candidate"
    # Keep `sorry` candidates eligible for diagnostic Lean feedback; only the
    # judge's `is_valid_no_sorry`/canonical verdict can award a score.
    if re.search(r"\b(?:axiom|unsafe|native_decide|trustCompiler)\b", code):
        return "candidate contains a forbidden proof-bypass construct"
    if task.theorem_name and task.theorem_name not in code:
        return "target theorem name is missing"
    imports = {line.strip() for line in target.splitlines() if line.strip().startswith("import ")}
    candidate_imports = {line.strip() for line in code.splitlines() if line.strip().startswith("import ")}
    if imports != candidate_imports:
        return "imports changed"
    return None
