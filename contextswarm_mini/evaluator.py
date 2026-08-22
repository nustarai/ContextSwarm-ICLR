"""Small HTTP client for the ContextSwarmJudge Lean router."""

from __future__ import annotations

import copy
import datetime as dt
from email.utils import parsedate_to_datetime
import hashlib
from http.client import HTTPException, InvalidURL
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .models import Task, Verdict


LEAN_PROBE_RESPONSE_PROFILE = "lean_probe_v1"
_MAX_PROBE_DIAGNOSTICS = 24
_MAX_PROBE_DATA_BYTES = 1_024
_MAX_PROBE_SEVERITY_BYTES = 32
_MAX_DIAGNOSTIC_POSITION = 2_147_483_647
_MIN_HTTP_BACKOFF_SECONDS = 0.5
_MAX_HTTP_BACKOFF_SECONDS = 30.0
_JUDGE_CANCEL_TIMEOUT_SECONDS = 2.0
_CANCEL_AWARE_LONG_POLL_MS = 250
_CANCEL_AWARE_HTTP_TIMEOUT_SECONDS = 1.0
_MAX_WORKER_ERROR_BYTES = 1_200
_MAX_WORKER_STATUS_BYTES = 120
_MAX_WORKER_IDENTIFIER_BYTES = 256
_ENDPOINT_RE = re.compile(r"https?://[^\s\])}>\"']+", re.IGNORECASE)
_CREDENTIAL_RE = re.compile(
    r"(?i)\b(authorization|bearer|access[_-]?token|api[_-]?key|token)\b"
    r"(?:\s*[:=]\s*|\s+)([^\s,;]+)"
)
_OPAQUE_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"(?:sk|tok|nur|aisw)[_-][A-Za-z0-9_-]{12,}"
    r"|eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,}){2}"
    r"|[A-Za-z0-9_-]{48,}"
    r")(?![A-Za-z0-9])"
)
_UNIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?!/)(?:[^/\s:;,\])}>]+/)+[^/\s,;\])}>]+"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])[A-Z]:\\(?:[^\\\s:;,\])}>]+\\)*[^\\\s,;\])}>]+"
)
_NON_CACHEABLE_PROBE_STATUSES = {
    "EVALUATOR_ERROR",
    "EVALUATOR_TIMEOUT",
    "NETWORK_ERROR",
    "INFRASTRUCTURE_ERROR",
    "REMOTE_SETTLEMENT_UNCONFIRMED",
    "REJECTED_OVERLOADED",
    "OUT_OF_HORIZON",
    "RUNNING",
    "QUEUED",
    "PENDING",
    "CANCELLED",
    "TASK_CANCELLED",
}


class EvaluatorError(RuntimeError):
    """A classified, bounded transport or malformed-verdict failure."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "evaluator_error",
        http_status: int | None = None,
        attempts: int = 0,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(sanitize_worker_text(message, _MAX_WORKER_ERROR_BYTES))
        self.category = _safe_category(category)
        self.http_status = (
            int(http_status)
            if isinstance(http_status, int) and not isinstance(http_status, bool)
            else None
        )
        self.attempts = max(0, int(attempts))
        self.retry_after_seconds = _safe_nonnegative_number(retry_after_seconds)

    def public_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "category": self.category,
            "attempts": self.attempts,
        }
        if self.http_status is not None:
            details["http_status"] = self.http_status
        if self.retry_after_seconds is not None:
            details["retry_after_seconds"] = round(self.retry_after_seconds, 3)
        return details


def sanitize_worker_text(
    value: Any,
    maximum_bytes: int = _MAX_WORKER_ERROR_BYTES,
    *,
    sensitive_values: Iterable[Any] = (),
    tail: bool = False,
) -> str:
    """Bound and redact text before it can reach a solver or run artifact."""

    text = str(value or "").replace("\x00", "")
    exact_values = {
        str(item)
        for item in sensitive_values
        if item is not None and len(str(item)) >= 4
    }
    for private_value in sorted(exact_values, key=len, reverse=True):
        text = text.replace(private_value, "<redacted-secret>")
    text = _ENDPOINT_RE.sub("<redacted-endpoint>", text)
    text = _CREDENTIAL_RE.sub(
        lambda match: f"{match.group(1)}=<redacted-secret>", text
    )
    text = _OPAQUE_SECRET_RE.sub("<redacted-secret>", text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub("<redacted-path>", text)
    text = _UNIX_ABSOLUTE_PATH_RE.sub("<redacted-path>", text)
    text = text.strip()
    maximum = max(0, int(maximum_bytes))
    if maximum == 0:
        return ""
    if tail and len(text.encode("utf-8")) > maximum:
        return text.encode("utf-8")[-maximum:].decode("utf-8", errors="ignore")
    bounded, _ = _bounded_utf8_text(text, maximum)
    return bounded


def sanitize_worker_identifier(value: Any) -> str | None:
    """Return a bounded opaque identifier, rejecting sensitive shapes."""

    text = str(value or "").strip()
    if not text or len(text.encode("utf-8")) > _MAX_WORKER_IDENTIFIER_BYTES:
        return None
    if (
        _ENDPOINT_RE.search(text)
        or _CREDENTIAL_RE.search(text)
        or _OPAQUE_SECRET_RE.search(text)
    ):
        return None
    if _UNIX_ABSOLUTE_PATH_RE.search(text) or _WINDOWS_ABSOLUTE_PATH_RE.search(text):
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", text):
        return None
    return text


class EvaluatorOverloadedError(EvaluatorError):
    """A definitive pre-admission rejection which is safe to retry."""

    def __init__(
        self,
        message: str,
        *,
        response: Mapping[str, Any] | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message, **details)
        self.response = dict(response or {})


class RemoteSettlementUnconfirmedError(EvaluatorError):
    """A submission may have created work whose identity was not returned."""

    def __init__(
        self,
        message: str,
        *,
        submission_response: Mapping[str, Any] | None = None,
        **details: Any,
    ):
        super().__init__(message, **details)
        self.submission_response = dict(submission_response or {})


class _CombinedCancelEvent:
    """Event-compatible view over caller cancellation and the global latch."""

    def __init__(self, caller_event: Any, remote_event: threading.Event):
        self._caller_event = caller_event
        self._remote_event = remote_event

    def is_set(self) -> bool:
        return bool(
            self._remote_event.is_set()
            or (
                self._caller_event is not None
                and self._caller_event.is_set()
            )
        )

    def wait(self, timeout: float | None = None) -> bool:
        deadline = (
            None
            if timeout is None
            else time.perf_counter() + max(0.0, float(timeout))
        )
        while not self.is_set():
            if deadline is None:
                delay = 0.02
            else:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return False
                delay = min(0.02, remaining)
            # The process-global event wakes this wait immediately. Caller
            # events are polled at a small bounded interval because Python has
            # no native wait-any primitive for Event objects.
            self._remote_event.wait(delay)
        return True


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


def _read_candidate(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None

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
    """Hash every immutable field that changes the meaning of a verdict."""

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


def _retry_after_seconds(raw: str | None, *, default: float) -> float:
    delay = float(default)
    value = str(raw or "").strip()
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                delay = (parsed - dt.datetime.now(dt.timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = float(default)
    if not math.isfinite(delay):
        return max(0.0, float(default))
    return max(0.0, delay)


def _cancel_requested(cancel_event: Any | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _wait_for_cancel(
    cancel_event: Any | None,
    delay_seconds: float,
) -> bool:
    """Wait for backoff/poll time while allowing broker revocation to wake it."""

    delay = max(0.0, float(delay_seconds))
    if cancel_event is None:
        if delay:
            time.sleep(delay)
        return False
    return bool(cancel_event.wait(delay))

class LeanEvaluator:
    is_mock_evaluator = False

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
        self._probe_cache: dict[str, Verdict] = {}
        self._probe_cache_lock = threading.Lock()
        self._remote_unsettled_lock = threading.Lock()
        self._remote_unsettled_jobs = 0
        self._remote_settlement_event = threading.Event()

    @property
    def remote_unsettled_jobs(self) -> int:
        """Return the run-global count of remote jobs lacking terminal proof."""

        with self._remote_unsettled_lock:
            return self._remote_unsettled_jobs

    @property
    def remote_settlement_event(self) -> threading.Event:
        """Expose the one-way process latch for admission/cancel coordination."""

        return self._remote_settlement_event

    def _mark_remote_unsettled(self) -> None:
        with self._remote_unsettled_lock:
            self._remote_unsettled_jobs += 1
            self._remote_settlement_event.set()

    def _combined_cancel_event(self, cancel_event: Any | None) -> Any:
        if cancel_event is None or cancel_event is self._remote_settlement_event:
            return self._remote_settlement_event
        return _CombinedCancelEvent(cancel_event, self._remote_settlement_event)

    def _remote_submission_error(
        self,
        message: str,
        *,
        category: str,
        attempts: int,
        http_status: int | None = None,
        submission_response: Mapping[str, Any] | None = None,
    ) -> RemoteSettlementUnconfirmedError:
        """Latch exactly one submission whose remote identity is unknown."""

        self._mark_remote_unsettled()
        return RemoteSettlementUnconfirmedError(
            message,
            category=category,
            http_status=http_status,
            attempts=attempts,
            submission_response=submission_response,
        )

    def _remote_settlement_gate_verdict(
        self,
        task: Task,
        *,
        started: float,
        candidate_code: str | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> Verdict:
        """Return the stable result used to block all later admissions."""

        bound_provenance = dict(provenance or {})
        bound_provenance.setdefault(
            "candidate_sha256",
            candidate_sha256(candidate_code) if candidate_code is not None else None,
        )
        bound_provenance.setdefault(
            "task_contract_sha256", self.expected_task_contract_sha256(task)
        )
        return Verdict(
            task_id=task.slug,
            status="REMOTE_SETTLEMENT_UNCONFIRMED",
            score=0.0,
            elapsed_seconds=max(0.0, time.monotonic() - started),
            response={"reason": "remote_settlement_gate_latched"},
            **bound_provenance,
        )

    def expected_task_contract_sha256(self, task: Task) -> str:
        """Return the exact contract identity this evaluator will submit."""

        return task_contract_sha256(
            task,
            lean_env_id=self.lean_env_id,
            verification_profile=self.verification_profile,
            judge_mode=self.judge_mode,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        is_job_submission = (
            method.upper() == "POST" and path == "/api/lean/jobs"
        )
        request_payload = dict(payload) if payload is not None else None
        headers = {"Accept": "application/json"}
        if request_payload is not None:
            headers["Content-Type"] = "application/json"
        token = str(__import__("os").environ.get("LEAN_AUTH_TOKEN") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        total_timeout = (
            float(self.timeout_seconds)
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if not math.isfinite(total_timeout) or total_timeout <= 0:
            raise EvaluatorError(
                "The Judge request deadline elapsed before transport admission.",
                category="request_deadline_elapsed",
            )
        request_deadline = time.monotonic() + total_timeout
        capacity_deadline = request_deadline
        if is_job_submission:
            capacity_deadline = min(
                capacity_deadline,
                time.monotonic() + self.admission_retry_seconds,
            )
        raw = ""
        attempt = 0
        while True:
            if _cancel_requested(cancel_event):
                raise EvaluatorError(
                    "The Judge request was cancelled.",
                    category="request_cancelled",
                    attempts=attempt,
                )
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                raise EvaluatorError(
                    "The Judge request deadline elapsed.",
                    category="request_deadline_elapsed",
                    attempts=attempt,
                )
            attempt += 1
            data = None
            if request_payload is not None:
                attempt_payload = dict(request_payload)
                raw_execution_timeout = attempt_payload.get("timeout")
                if (
                    method.upper() == "POST"
                    and isinstance(raw_execution_timeout, int)
                    and not isinstance(raw_execution_timeout, bool)
                ):
                    if remaining < 1.0:
                        raise EvaluatorError(
                            "The Judge request deadline left no execution budget.",
                            category="request_deadline_elapsed",
                            attempts=attempt - 1,
                        )
                    attempt_payload["timeout"] = min(
                        raw_execution_timeout,
                        max(1, int(remaining)),
                    )
                data = json.dumps(attempt_payload, ensure_ascii=False).encode("utf-8")
            try:
                request = Request(url, data=data, headers=headers, method=method)
                request_timeout = min(remaining, 30.0)
                with urlopen(request, timeout=request_timeout) as response:
                    raw = response.read().decode("utf-8")
                break
            except HTTPError as exc:
                http_status = int(exc.code)
                retry_after = (
                    exc.headers.get("Retry-After")
                    if exc.headers is not None
                    else None
                )
                try:
                    error_payload = json.loads(exc.read(64 * 1024).decode("utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    error_payload = None
                backoff = min(
                    _MAX_HTTP_BACKOFF_SECONDS,
                    _MIN_HTTP_BACKOFF_SECONDS * (2 ** min(attempt - 1, 16)),
                )
                delay = _retry_after_seconds(retry_after, default=backoff)
                exc.close()
                confirmed_overload = (
                    is_job_submission
                    and http_status in {429, 503}
                    and isinstance(error_payload, Mapping)
                    and _confirmed_pre_admission_rejection(error_payload)
                )
                if confirmed_overload:
                    explicit_retry_delay = _retry_after_seconds(
                        retry_after,
                        default=0.0,
                    )
                    raise EvaluatorOverloadedError(
                        "Judge admission was definitively overloaded.",
                        category="judge_overloaded",
                        http_status=http_status,
                        attempts=attempt,
                        retry_after_seconds=explicit_retry_delay,
                        response=error_payload,
                    ) from None
                # Generic 429/503 responses are safe to replay for read-only
                # health/poll requests, but not for job creation: without an
                # idempotency key they may be lost responses after admission.
                # Only the explicit pre-admission receipt above may retry POST.
                retryable_capacity = (
                    http_status in {429, 503} and not is_job_submission
                )
                if retryable_capacity:
                    delay = max(delay, backoff)
                    remaining_capacity = capacity_deadline - time.monotonic()
                    if _cancel_requested(cancel_event):
                        raise EvaluatorError(
                            "The Judge request was cancelled.",
                            category="request_cancelled",
                            http_status=http_status,
                            attempts=attempt,
                            retry_after_seconds=delay,
                        ) from None
                    if remaining_capacity > 0 and delay < remaining_capacity:
                        if _wait_for_cancel(cancel_event, delay):
                            raise EvaluatorError(
                                "The Judge request was cancelled.",
                                category="request_cancelled",
                                http_status=http_status,
                                attempts=attempt,
                                retry_after_seconds=delay,
                            ) from None
                        continue
                    raise EvaluatorError(
                        "Judge capacity remained unavailable until the request deadline.",
                        category="judge_overloaded_deadline",
                        http_status=http_status,
                        attempts=attempt,
                        retry_after_seconds=delay,
                    ) from None
                if (
                    is_job_submission
                    and (http_status == 429 or http_status >= 500)
                    and not _bindable_terminal_job_receipt(error_payload)
                ):
                    raise self._remote_submission_error(
                        "The Judge submission outcome could not be settled.",
                        category="http_error",
                        http_status=http_status,
                        attempts=attempt,
                        submission_response=(
                            error_payload
                            if isinstance(error_payload, Mapping)
                            else None
                        ),
                    ) from None
                raise EvaluatorError(
                    "The Judge rejected the HTTP request.",
                    category="http_error",
                    http_status=http_status,
                    attempts=attempt,
                ) from None
            except (InvalidURL, ValueError, TypeError, OverflowError):
                raise EvaluatorError(
                    "The Judge endpoint or request configuration is invalid.",
                    category="invalid_request_configuration",
                    attempts=attempt,
                ) from None
            except UnicodeError:
                if is_job_submission:
                    raise self._remote_submission_error(
                        "The Judge submission response was malformed.",
                        category="malformed_response",
                        attempts=attempt,
                    ) from None
                raise EvaluatorError(
                    "The Judge returned a non-UTF-8 response.",
                    category="malformed_response",
                    attempts=attempt,
                ) from None
            except (URLError, TimeoutError, OSError, HTTPException):
                if is_job_submission:
                    raise self._remote_submission_error(
                        "The Judge submission transport outcome is unknown.",
                        category="network_error",
                        attempts=attempt,
                    ) from None
                raise EvaluatorError(
                    "The Judge transport failed.",
                    category="network_error",
                    attempts=attempt,
                ) from None
        if is_job_submission and not raw:
            raise self._remote_submission_error(
                "The Judge returned an empty submission response.",
                category="malformed_response",
                attempts=attempt,
            )
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            if is_job_submission:
                raise self._remote_submission_error(
                    "The Judge returned a malformed submission response.",
                    category="malformed_response",
                    attempts=attempt,
                ) from None
            raise EvaluatorError(
                "The Judge returned a non-JSON response.",
                category="malformed_response",
                attempts=attempt,
            ) from None
        if not isinstance(parsed, dict):
            if is_job_submission:
                raise self._remote_submission_error(
                    "The Judge returned a non-object submission response.",
                    category="malformed_response",
                    attempts=attempt,
                )
            raise EvaluatorError(
                "The Judge returned a non-object response.",
                category="malformed_response",
                attempts=attempt,
            )
        if (
            is_job_submission
            and not _confirmed_pre_admission_rejection(parsed)
            and _submission_job_identifier(parsed) is None
        ):
            raise self._remote_submission_error(
                "The Judge submission response lacked a bindable job id.",
                category="missing_job_identifier",
                attempts=attempt,
                submission_response=parsed,
            )
        return parsed

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def evaluate(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: Any | None = None,
    ) -> Verdict:
        started = time.monotonic()
        code = _read_candidate(candidate_path)
        if self.remote_unsettled_jobs > 0:
            return self._remote_settlement_gate_verdict(
                task,
                started=started,
                candidate_code=code,
            )
        combined_cancel_event = self._combined_cancel_event(cancel_event)
        if _cancel_requested(combined_cancel_event):
            return self._cancelled_verdict(
                task,
                started=started,
                provenance={
                    "candidate_sha256": (
                        candidate_sha256(code) if code is not None else None
                    ),
                    "task_contract_sha256": self.expected_task_contract_sha256(task),
                },
                job_id=None,
                cancellation=None,
            )
        cache_key = self._probe_cache_key(task, code) if code is not None else None
        if cache_key is not None:
            cached = self._cached_verdict(cache_key)
            if cached is not None:
                return cached
        verdict = self._evaluate(
            task,
            candidate_path,
            deadline_monotonic=deadline_monotonic,
            started=started,
            terminal_overload_retries=self.terminal_overload_retries,
            response_profile=None,
            candidate_code=code,
            cancel_event=combined_cancel_event,
        )
        verdict.cache_reused = bool(
            verdict.cache_reused
            or _nested_value(verdict.response, "cache_reused") is True
        )
        return verdict

    def probe(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Verdict:
        """Run the canonical evaluator with bounded worker-facing diagnostics."""

        return self._probe_source(
            task,
            candidate_path,
            _read_candidate(candidate_path),
            deadline_monotonic=deadline_monotonic,
            cancel_event=cancel_event,
        )

    def probe_source(
        self,
        task: Task,
        candidate_code: str,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Verdict:
        """Probe a broker-owned immutable source snapshot."""

        if not isinstance(candidate_code, str):
            raise TypeError("candidate_code must be a string")
        return self._probe_source(
            task,
            Path("<broker-candidate-snapshot>"),
            candidate_code,
            deadline_monotonic=deadline_monotonic,
            cancel_event=cancel_event,
        )

    def _probe_source(
        self,
        task: Task,
        candidate_path: Path,
        code: str | None,
        *,
        deadline_monotonic: float | None,
        cancel_event: threading.Event | None,
    ) -> Verdict:
        started = time.monotonic()
        if self.remote_unsettled_jobs > 0:
            return self._remote_settlement_gate_verdict(
                task,
                started=started,
                candidate_code=code,
            )
        combined_cancel_event = self._combined_cancel_event(cancel_event)
        cache_key = self._probe_cache_key(task, code) if code is not None else None
        if not _cancel_requested(combined_cancel_event) and cache_key is not None:
            cached = self._cached_verdict(cache_key)
            if cached is not None:
                return cached
        verdict = self._evaluate(
            task,
            candidate_path,
            deadline_monotonic=deadline_monotonic,
            started=started,
            terminal_overload_retries=self.terminal_overload_retries,
            response_profile=LEAN_PROBE_RESPONSE_PROFILE,
            candidate_code=code,
            cancel_event=combined_cancel_event,
        )
        verdict.cache_reused = bool(
            verdict.cache_reused
            or _nested_value(verdict.response, "cache_reused") is True
        )
        if cache_key is not None and verdict.status not in _NON_CACHEABLE_PROBE_STATUSES:
            with self._probe_cache_lock:
                self._probe_cache[cache_key] = copy.deepcopy(verdict)
        return verdict

    def _cached_verdict(self, cache_key: str) -> Verdict | None:
        with self._probe_cache_lock:
            cached = self._probe_cache.get(cache_key)
        if cached is None:
            return None
        response = copy.deepcopy(cached.response)
        response["probe_cache_reused"] = True
        return Verdict(
            task_id=cached.task_id,
            status=cached.status,
            score=cached.score,
            elapsed_seconds=0.0,
            response=response,
            error=cached.error,
            candidate_sha256=cached.candidate_sha256,
            task_contract_sha256=cached.task_contract_sha256,
            judge_job_id=cached.judge_job_id,
            cache_reused=True,
        )

    def _evaluate(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None,
        started: float,
        terminal_overload_retries: int,
        response_profile: str | None,
        candidate_code: str | None,
        cancel_event: threading.Event | None,
    ) -> Verdict:
        contract_sha256 = task_contract_sha256(
            task,
            lean_env_id=self.lean_env_id,
            verification_profile=self.verification_profile,
            judge_mode=self.judge_mode,
        )
        source_sha256 = (
            candidate_sha256(candidate_code) if candidate_code is not None else None
        )
        provenance = {
            "candidate_sha256": source_sha256,
            "task_contract_sha256": contract_sha256,
        }
        if self.remote_unsettled_jobs > 0:
            return self._remote_settlement_gate_verdict(
                task,
                started=started,
                candidate_code=candidate_code,
                provenance=provenance,
            )
        if _cancel_requested(cancel_event):
            return self._cancelled_verdict(
                task,
                started=started,
                provenance=provenance,
                job_id=None,
                cancellation=None,
            )
        if (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        ):
            return Verdict(
                task.slug,
                "OUT_OF_HORIZON",
                0.0,
                0.0,
                {"reason": "run_horizon_elapsed"},
                **provenance,
            )
        job_id: Any = None
        cancel_endpoint: Any = None
        response: dict[str, Any] = {}
        last_poll_error: str | None = None
        try:
            code = (
                candidate_code
                if candidate_code is not None
                else candidate_path.read_text(encoding="utf-8")
            )
            if source_sha256 is None:
                source_sha256 = candidate_sha256(code)
                provenance["candidate_sha256"] = source_sha256
            target = task.baseline_code
            local_error = _local_contract_error(task, code, target)
            if local_error:
                return Verdict(
                    task_id=task.slug,
                    status="LOCAL_REJECTED",
                    score=0.0,
                    elapsed_seconds=time.monotonic() - started,
                    response={"reason": local_error},
                    **provenance,
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
                    **provenance,
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
            if response_profile:
                payload["response_profile"] = response_profile
            admission_deadline = time.monotonic() + self.admission_retry_seconds
            if deadline_monotonic is not None:
                admission_deadline = min(admission_deadline, deadline_monotonic)
            admission_attempt = 0
            last_admission_rejection: dict[str, Any] | None = None
            while True:
                if self.remote_unsettled_jobs > 0:
                    return self._remote_settlement_gate_verdict(
                        task,
                        started=started,
                        candidate_code=code,
                        provenance=provenance,
                    )
                if _cancel_requested(cancel_event):
                    return self._cancelled_verdict(
                        task,
                        started=started,
                        provenance=provenance,
                        job_id=None,
                        cancellation=None,
                    )
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
                        **provenance,
                    )
                admission_attempt += 1
                admission_retry_delay = 0.0
                submit_timeout = (
                    max(0.1, deadline_monotonic - time.monotonic())
                    if deadline_monotonic is not None
                    else max(0.1, execution_timeout + remaining_admission)
                )
                request_options: dict[str, Any] = {
                    "timeout_seconds": submit_timeout,
                }
                if cancel_event is not None:
                    request_options["cancel_event"] = cancel_event
                try:
                    submitted = self._request(
                        "POST",
                        "/api/lean/jobs",
                        payload,
                        **request_options,
                    )
                    if not _confirmed_pre_admission_rejection(submitted):
                        break
                    last_admission_rejection = submitted
                except EvaluatorOverloadedError as exc:
                    if exc.response:
                        last_admission_rejection = exc.response
                    admission_retry_delay = float(exc.retry_after_seconds or 0.0)
                if self.remote_unsettled_jobs > 0:
                    return self._remote_settlement_gate_verdict(
                        task,
                        started=started,
                        candidate_code=code,
                        provenance=provenance,
                    )
                remaining_admission = admission_deadline - time.monotonic()
                if remaining_admission > 0:
                    cancelled = _wait_for_cancel(
                        cancel_event,
                        min(
                            remaining_admission,
                            max(
                                admission_retry_delay,
                                self.poll_interval_seconds
                                * min(4, admission_attempt),
                            ),
                        ),
                    )
                    if cancelled:
                        if self.remote_unsettled_jobs > 0:
                            return self._remote_settlement_gate_verdict(
                                task,
                                started=started,
                                candidate_code=code,
                                provenance=provenance,
                            )
                        return self._cancelled_verdict(
                            task,
                            started=started,
                            provenance=provenance,
                            job_id=None,
                            cancellation=None,
                        )
            job_id = _submission_job_identifier(submitted)
            cancel_endpoint = submitted.get("cancel_endpoint")
            response = submitted
            if not job_id:
                self._mark_remote_unsettled()
                safe_response = (
                    _safe_nonterminal_response(response)
                    if not _terminal(response)
                    else _safe_response(response)
                )
                safe_response.update(
                    {
                        "reason": "remote_settlement_unconfirmed",
                        "remote_settlement_unconfirmed": True,
                    }
                )
                return Verdict(
                    task_id=task.slug,
                    status="REMOTE_SETTLEMENT_UNCONFIRMED",
                    score=0.0,
                    elapsed_seconds=time.monotonic() - started,
                    response=safe_response,
                    error="Judge submission receipt lacked a bindable job id",
                    **provenance,
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
                    if _cancel_requested(cancel_event):
                        cancellation = self._cancel_submitted_job(
                            job_id,
                            response=response,
                            cancel_endpoint=cancel_endpoint,
                        )
                        return self._cancelled_verdict(
                            task,
                            started=started,
                            provenance=provenance,
                            job_id=job_id,
                            cancellation=cancellation,
                        )
                    remaining = settlement_deadline - time.monotonic()
                    wait_ms = max(1, min(1_000, int(remaining * 1_000)))
                    poll_timeout = max(0.1, remaining)
                    if cancel_event is not None:
                        wait_ms = min(wait_ms, _CANCEL_AWARE_LONG_POLL_MS)
                        poll_timeout = min(
                            poll_timeout,
                            _CANCEL_AWARE_HTTP_TIMEOUT_SECONDS,
                        )
                    request_options = {"timeout_seconds": poll_timeout}
                    if cancel_event is not None:
                        request_options["cancel_event"] = cancel_event
                    try:
                        response = self._request(
                            "GET",
                            f"/api/lean/jobs/{quote(job_id, safe='')}?wait_ms={wait_ms}",
                            **request_options,
                        )
                    except EvaluatorError as exc:
                        if (
                            exc.category == "request_cancelled"
                            or _cancel_requested(cancel_event)
                        ):
                            cancellation = self._cancel_submitted_job(
                                job_id,
                                response=response,
                                cancel_endpoint=cancel_endpoint,
                            )
                            return self._cancelled_verdict(
                                task,
                                started=started,
                                provenance=provenance,
                                job_id=job_id,
                                cancellation=cancellation,
                            )
                        last_poll_error = str(exc)
                        if time.monotonic() >= settlement_deadline:
                            break
                        _wait_for_cancel(
                            cancel_event,
                            min(
                                self.poll_interval_seconds,
                                max(0.0, settlement_deadline - time.monotonic()),
                            ),
                        )
                        continue
                    # A successful GET on the job-specific capability binds an
                    # otherwise id-less receipt to the submitted job.  An
                    # explicit contradictory id is never rewritten or scored;
                    # keep polling and, if necessary, reconcile the original
                    # job through the normal fail-closed cancellation path.
                    expected_job_id = sanitize_worker_identifier(job_id)
                    receipt_job_id = _submission_job_identifier(response)
                    if receipt_job_id is None and expected_job_id is not None:
                        response = dict(response)
                        response["job_id"] = expected_job_id
                    elif receipt_job_id != expected_job_id:
                        last_poll_error = "Judge poll receipt job id mismatch"
                        response = {
                            "job_id": expected_job_id,
                            "status": "RUNNING",
                            "reason": "poll_job_id_mismatch",
                        }
                        _wait_for_cancel(
                            cancel_event,
                            min(
                                self.poll_interval_seconds,
                                max(0.0, settlement_deadline - time.monotonic()),
                            ),
                        )
                        continue
                    if response.get("cancel_endpoint") is not None:
                        cancel_endpoint = response.get("cancel_endpoint")
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
                    if _wait_for_cancel(
                        cancel_event,
                        min(
                            self.poll_interval_seconds,
                            max(0.0, settlement_deadline - time.monotonic()),
                        ),
                    ):
                        cancellation = self._cancel_submitted_job(
                            job_id,
                            response=response,
                            cancel_endpoint=cancel_endpoint,
                        )
                        return self._cancelled_verdict(
                            task,
                            started=started,
                            provenance=provenance,
                            job_id=job_id,
                            cancellation=cancellation,
                        )
            if job_id and not _terminal(response):
                abandoned_by_client = True
                response, cancel_error = self._cancel_and_reconcile(
                    job_id,
                    response,
                    cancel_endpoint=cancel_endpoint,
                )
                if cancel_error:
                    last_poll_error = cancel_error
                    safe_response = _safe_nonterminal_response(response)
                    safe_response.update(
                        {
                            "reason": "remote_settlement_unconfirmed",
                            "settlement_error": cancel_error,
                            "remote_settlement_unconfirmed": True,
                        }
                    )
                    return Verdict(
                        task_id=task.slug,
                        status="REMOTE_SETTLEMENT_UNCONFIRMED",
                        score=0.0,
                        elapsed_seconds=time.monotonic() - started,
                        response=safe_response,
                        error="Judge job cancellation did not reach terminal settlement",
                        judge_job_id=sanitize_worker_identifier(job_id),
                        **provenance,
                    )
            if _retryable_admission_rejection(response):
                retried = self._retry_terminal_overload(
                    task,
                    candidate_path,
                    deadline_monotonic=deadline_monotonic,
                    started=started,
                    terminal_overload_retries=terminal_overload_retries,
                    response_profile=response_profile,
                    candidate_code=code,
                    cancel_event=cancel_event,
                )
                if retried is not None:
                    return retried
            normalized_job_id = sanitize_worker_identifier(job_id)
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
                        judge_job_id=normalized_job_id,
                        **provenance,
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
                        judge_job_id=normalized_job_id,
                        **provenance,
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
                    judge_job_id=normalized_job_id,
                    **provenance,
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
                    judge_job_id=normalized_job_id,
                    **provenance,
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
                    judge_job_id=normalized_job_id,
                    **provenance,
                )
            return Verdict(
                task_id=task.slug,
                status="PROVED" if proved else status,
                score=1.0 if proved else 0.0,
                elapsed_seconds=time.monotonic() - started,
                response=_safe_response(response),
                judge_job_id=normalized_job_id,
                **provenance,
            )
        except (OSError, EvaluatorError, UnicodeError) as exc:
            cancellation_summary: dict[str, Any] | None = None
            cancel_error: str | None = None
            if (
                isinstance(exc, EvaluatorError)
                and not isinstance(exc, RemoteSettlementUnconfirmedError)
                and (
                    exc.category == "request_cancelled"
                    or _cancel_requested(cancel_event)
                )
            ):
                if job_id:
                    cancellation_summary = self._cancel_submitted_job(
                        job_id,
                        response=response,
                        cancel_endpoint=cancel_endpoint,
                    )
                return self._cancelled_verdict(
                    task,
                    started=started,
                    provenance=provenance,
                    job_id=job_id,
                    cancellation=cancellation_summary,
                )
            if job_id and not _terminal(response):
                response, cancel_error = self._cancel_and_reconcile(
                    str(job_id),
                    response,
                    cancel_endpoint=cancel_endpoint,
                )
                if _retryable_admission_rejection(response):
                    retried = self._retry_terminal_overload(
                        task,
                        candidate_path,
                        deadline_monotonic=deadline_monotonic,
                        started=started,
                        terminal_overload_retries=terminal_overload_retries,
                        response_profile=response_profile,
                        candidate_code=(
                            code if "code" in locals() else candidate_code
                        ),
                        cancel_event=cancel_event,
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
                        judge_job_id=sanitize_worker_identifier(job_id),
                        **provenance,
                    )
                if cancel_error:
                    response = {**response, "settlement_error": cancel_error}
            if (
                isinstance(exc, RemoteSettlementUnconfirmedError)
                and exc.submission_response
            ):
                response = dict(exc.submission_response)
            safe_error_response = (
                _safe_nonterminal_response(response)
                if _verdict_status(response) in _NONTERMINAL_STATUSES
                or not _terminal(response)
                else _safe_response(response)
            )
            if isinstance(exc, EvaluatorError):
                safe_error_response["evaluator_failure"] = exc.public_details()
            if cancel_error:
                safe_error_response.update(
                    {
                        "reason": "remote_settlement_unconfirmed",
                        "settlement_error": cancel_error,
                        "remote_settlement_unconfirmed": True,
                    }
                )
                failure_status = "REMOTE_SETTLEMENT_UNCONFIRMED"
            elif isinstance(exc, RemoteSettlementUnconfirmedError):
                safe_error_response.update(
                    {
                        "reason": "remote_settlement_unconfirmed",
                        "remote_settlement_unconfirmed": True,
                    }
                )
                failure_status = "REMOTE_SETTLEMENT_UNCONFIRMED"
            elif (
                isinstance(exc, EvaluatorError)
                and exc.category == "request_deadline_elapsed"
                and deadline_monotonic is not None
                and time.monotonic() >= deadline_monotonic
            ):
                failure_status = "OUT_OF_HORIZON"
            elif isinstance(exc, EvaluatorError):
                failure_status = {
                    "judge_overloaded_deadline": "REJECTED_OVERLOADED",
                    "network_error": "NETWORK_ERROR",
                    "request_deadline_elapsed": "EVALUATOR_TIMEOUT",
                }.get(exc.category, "EVALUATOR_ERROR")
            else:
                failure_status = "EVALUATOR_ERROR"
            return Verdict(
                task_id=task.slug,
                status=failure_status,
                score=0.0,
                elapsed_seconds=time.monotonic() - started,
                response=safe_error_response,
                error=sanitize_worker_text(exc),
                judge_job_id=sanitize_worker_identifier(job_id),
                **provenance,
            )

    def _retry_terminal_overload(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None,
        started: float,
        terminal_overload_retries: int,
        response_profile: str | None,
        candidate_code: str | None,
        cancel_event: threading.Event | None,
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
            deadline_monotonic=deadline_monotonic,
            started=started,
            terminal_overload_retries=terminal_overload_retries - 1,
            response_profile=response_profile,
            candidate_code=candidate_code,
            cancel_event=cancel_event,
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
        *,
        cancel_endpoint: Any = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Boundedly cancel an abandoned job and recover its terminal receipt."""

        current, error, _attempted = self._cancel_and_reconcile_details(
            job_id,
            response,
            cancel_endpoint=cancel_endpoint,
        )
        return current, error

    def _cancel_and_reconcile_details(
        self,
        job_id: Any,
        response: Mapping[str, Any],
        *,
        cancel_endpoint: Any = None,
    ) -> tuple[dict[str, Any], str | None, bool]:
        """Cancel and confirm terminal settlement within one absolute deadline."""

        current = dict(response)
        last_nonterminal = dict(response)
        deadline = time.monotonic() + self.cancel_grace_seconds
        cancel_path, _ = self._cancel_request_path(
            job_id,
            cancel_endpoint=cancel_endpoint,
        )
        if cancel_path is None and cancel_endpoint is not None:
            cancel_path, _ = self._cancel_request_path(
                job_id,
                cancel_endpoint=None,
            )
        attempted = False
        if cancel_path is not None:
            remaining = deadline - time.monotonic()
            attempted = remaining > 0
            try:
                if attempted:
                    current = self._request(
                        "DELETE",
                        cancel_path,
                        timeout_seconds=min(
                            _JUDGE_CANCEL_TIMEOUT_SECONDS,
                            remaining,
                        ),
                    )
            except EvaluatorError:
                # A transport acknowledgement is not settlement.  Continue
                # through the same bounded reconciliation window whenever a
                # status endpoint is available.
                current = dict(response)
        if self._authoritative_terminal_receipt(current, job_id):
            return current, None, attempted
        if self._job_bound_receipt(current, job_id) and not _terminal(current):
            last_nonterminal = current

        poll_paths = self._settlement_poll_paths(
            job_id,
            current,
            response,
            cancel_endpoint=cancel_endpoint,
        )
        poll_index = 0
        while poll_paths and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            wait_ms = max(1, min(500, int(remaining * 1_000)))
            poll_path = poll_paths[poll_index % len(poll_paths)]
            separator = "&" if "?" in poll_path else "?"
            try:
                current = self._request(
                    "GET",
                    f"{poll_path}{separator}wait_ms={wait_ms}",
                    timeout_seconds=remaining,
                )
            except EvaluatorError:
                poll_index += 1
            else:
                if self._authoritative_terminal_receipt(current, job_id):
                    return current, None, attempted
                if (
                    self._job_bound_receipt(current, job_id)
                    and not _terminal(current)
                ):
                    last_nonterminal = current
            # Every unsuccessful reconcile attempt yields; malformed terminal
            # receipts and repeated transport failures must not tight-loop.
            time.sleep(
                min(
                    self.poll_interval_seconds,
                    max(0.0, deadline - time.monotonic()),
                )
            )
        self._mark_remote_unsettled()
        return last_nonterminal, "cancel_settlement_unconfirmed", attempted

    def _settlement_poll_paths(
        self,
        job_id: Any,
        *receipts: Mapping[str, Any],
        cancel_endpoint: Any = None,
    ) -> list[str]:
        """Resolve same-origin receipt capabilities in authoritative order."""

        raw_endpoints: list[Any] = []
        for receipt in receipts:
            for key in ("status_endpoint", "cancel_endpoint"):
                endpoint = _nested_value(receipt, key)
                if endpoint is not None:
                    raw_endpoints.append(endpoint)
        if cancel_endpoint is not None:
            raw_endpoints.append(cancel_endpoint)
        paths: list[str] = []
        for endpoint in raw_endpoints:
            path, _ = self._cancel_request_path(
                job_id,
                cancel_endpoint=endpoint,
            )
            if path is not None and path not in paths:
                paths.append(path)
        fallback, _ = self._cancel_request_path(job_id, cancel_endpoint=None)
        if fallback is not None and fallback not in paths:
            paths.append(fallback)
        return paths

    @staticmethod
    def _authoritative_terminal_receipt(
        response: Mapping[str, Any],
        job_id: Any,
    ) -> bool:
        """Require a terminal lifecycle receipt bound to the submitted job."""

        return _terminal(response) and LeanEvaluator._job_bound_receipt(
            response,
            job_id,
        )

    @staticmethod
    def _job_bound_receipt(
        response: Mapping[str, Any],
        job_id: Any,
    ) -> bool:
        """Reject receipts that omit or contradict the submitted job id."""

        expected_job_id = sanitize_worker_identifier(job_id)
        receipt_job_id = sanitize_worker_identifier(
            _nested_value(response, "job_id") or _nested_value(response, "id")
        )
        return (
            expected_job_id is not None
            and receipt_job_id is not None
            and receipt_job_id == expected_job_id
        )

    def _cancel_submitted_job(
        self,
        job_id: Any,
        *,
        response: Mapping[str, Any] | None = None,
        cancel_endpoint: Any = None,
    ) -> dict[str, Any]:
        """Cancel a job, reporting success only after terminal settlement."""

        normalized_job_id = sanitize_worker_identifier(job_id)
        if normalized_job_id is None:
            return {
                "attempted": False,
                "succeeded": False,
                "settled": False,
                "unconfirmed": True,
                "failure_category": "cancel_settlement_unconfirmed",
            }
        current, error, attempted = self._cancel_and_reconcile_details(
            normalized_job_id,
            response or {"job_id": normalized_job_id, "status": "running"},
            cancel_endpoint=cancel_endpoint,
        )
        settled = self._authoritative_terminal_receipt(current, normalized_job_id)
        return {
            "attempted": attempted,
            "succeeded": attempted and settled,
            "settled": settled,
            "unconfirmed": not settled,
            "failure_category": (
                None if settled else (error or "cancel_settlement_unconfirmed")
            ),
        }

    def _cancel_request_path(
        self,
        job_id: Any,
        *,
        cancel_endpoint: Any,
    ) -> tuple[str | None, str]:
        """Use a Judge-provided same-origin cancel capability when present."""

        if cancel_endpoint is not None:
            if not isinstance(cancel_endpoint, str) or not cancel_endpoint.strip():
                return None, "invalid_cancel_endpoint"
            raw_endpoint = cancel_endpoint.strip()
            try:
                endpoint_size = len(raw_endpoint.encode("utf-8"))
            except UnicodeError:
                return None, "invalid_cancel_endpoint"
            if endpoint_size > 2_048:
                return None, "invalid_cancel_endpoint"
            try:
                parsed = urlsplit(raw_endpoint)
                base = urlsplit(self.base_url)
            except ValueError:
                return None, "invalid_cancel_endpoint"
            if parsed.fragment or not parsed.path.startswith("/"):
                return None, "invalid_cancel_endpoint"
            if parsed.scheme or parsed.netloc:
                if (
                    parsed.scheme.casefold() != base.scheme.casefold()
                    or parsed.netloc.casefold() != base.netloc.casefold()
                ):
                    return None, "invalid_cancel_endpoint"
            path = parsed.path
            if parsed.query:
                path += f"?{parsed.query}"
            return path, "invalid_cancel_endpoint"

        normalized_job_id = sanitize_worker_identifier(job_id)
        if normalized_job_id is None:
            return None, "invalid_job_identifier"
        return (
            f"/api/lean/jobs/{quote(normalized_job_id, safe='')}",
            "invalid_job_identifier",
        )

    @staticmethod
    def _cancelled_verdict(
        task: Task,
        *,
        started: float,
        provenance: Mapping[str, Any],
        job_id: Any,
        cancellation: Mapping[str, Any] | None,
    ) -> Verdict:
        response: dict[str, Any] = {"reason": "cancel_event_set"}
        if cancellation is not None:
            response["judge_cancellation"] = dict(cancellation)
        return Verdict(
            task_id=task.slug,
            status="TASK_CANCELLED",
            score=0.0,
            elapsed_seconds=max(0.0, time.monotonic() - started),
            response=response,
            judge_job_id=sanitize_worker_identifier(job_id),
            candidate_sha256=provenance.get("candidate_sha256"),
            task_contract_sha256=provenance.get("task_contract_sha256"),
        )

    def _probe_cache_key(self, task: Task, code: str) -> str:
        digest = hashlib.sha256()
        digest.update(self.expected_task_contract_sha256(task).encode("ascii"))
        digest.update(b"\0")
        digest.update(candidate_sha256(code).encode("ascii"))
        return digest.hexdigest()


class MockEvaluator:
    """Offline smoke evaluator; never represents a paper score."""

    is_mock_evaluator = True

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
        cancel_event: Any | None = None,
    ) -> Verdict:
        del deadline_monotonic
        if _cancel_requested(cancel_event):
            return Verdict(
                task.slug,
                "TASK_CANCELLED",
                0.0,
                0.0,
                task_contract_sha256=self.expected_task_contract_sha256(task),
            )
        try:
            code = candidate_path.read_text(encoding="utf-8")
        except OSError as exc:
            return Verdict(
                task.slug,
                "MISSING_CANDIDATE",
                0.0,
                0.0,
                error=sanitize_worker_text(exc),
                task_contract_sha256=self.expected_task_contract_sha256(task),
            )
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

    def probe(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Verdict:
        if _cancel_requested(cancel_event):
            return Verdict(
                task.slug,
                "TASK_CANCELLED",
                0.0,
                0.0,
                task_contract_sha256=self.expected_task_contract_sha256(task),
            )
        return self.evaluate(
            task,
            candidate_path,
            deadline_monotonic=deadline_monotonic,
            cancel_event=cancel_event,
        )

    def probe_source(
        self,
        task: Task,
        candidate_code: str,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Verdict:
        del deadline_monotonic
        provenance = {
            "candidate_sha256": candidate_sha256(candidate_code),
            "task_contract_sha256": self.expected_task_contract_sha256(task),
        }
        if _cancel_requested(cancel_event):
            return Verdict(task.slug, "TASK_CANCELLED", 0.0, 0.0, **provenance)
        proved = (
            self.prove_without_sorry
            and "sorry" not in candidate_code
            and "admit" not in candidate_code
        )
        return Verdict(
            task.slug,
            "PROVED" if proved else "MOCK_SKIPPED",
            1.0 if proved else 0.0,
            0.0,
            {"mock": True},
            **provenance,
        )


def _status(payload: Mapping[str, Any]) -> str:
    for key in ("formal_status", "verdict", "result"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _safe_status(value)
    canonical = payload.get("canonical_verdict")
    if isinstance(canonical, Mapping):
        value = canonical.get("status")
        if isinstance(value, str) and value.strip():
            return _safe_status(value)
    value = payload.get("status")
    if isinstance(value, str) and value.strip():
        return _safe_status(value)
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
        return _safe_status(value)
    nested = payload.get("response")
    if isinstance(nested, Mapping):
        return _raw_lifecycle_status(nested)
    return ""


def _submission_job_identifier(payload: Any) -> str | None:
    """Return the opaque identity needed to settle an admitted submission."""

    if not isinstance(payload, Mapping):
        return None
    raw = _nested_value(payload, "job_id") or _nested_value(payload, "id")
    if isinstance(raw, bool):
        return None
    return sanitize_worker_identifier(raw)


def _bindable_terminal_job_receipt(payload: Any) -> bool:
    return (
        isinstance(payload, Mapping)
        and _submission_job_identifier(payload) is not None
        and _terminal(payload)
    )


def _confirmed_pre_admission_rejection(payload: Mapping[str, Any]) -> bool:
    """Recognize only receipts which prove no remote job was admitted."""

    if _retryable_admission_rejection(payload):
        return True
    if _submission_job_identifier(payload) is not None:
        return False
    error_code = str(payload.get("error") or "").strip().lower()
    error_message = str(payload.get("message") or "").strip().lower()
    error_text = f"{error_code} {error_message}".strip()
    if error_code in {
        "admission_capacity_exceeded",
        "permit_unavailable",
    }:
        return True
    return (
        payload.get("ok") is False
        and (
            "overload" in error_text
            or (
                "queue" in error_text
                and any(word in error_text for word in ("full", "capacity"))
            )
            or ("ingress" in error_text and "capacity" in error_text)
        )
    )


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


def safe_worker_response(
    payload: Mapping[str, Any] | Any,
    *,
    _depth: int = 0,
) -> dict[str, Any]:
    """Keep bounded verdict metadata while removing secrets and host details."""

    if not isinstance(payload, Mapping) or _depth > 2:
        return {}
    result: dict[str, Any] = {}
    for key in ("job_id", "id"):
        identifier = sanitize_worker_identifier(payload.get(key))
        if identifier is not None:
            result[key] = identifier
    for key in (
        "status",
        "formal_status",
        "verdict",
        "error_code",
        "error_kind",
        "terminal_reason",
    ):
        value = payload.get(key)
        if isinstance(value, str):
            result[key] = sanitize_worker_text(value, _MAX_WORKER_STATUS_BYTES)
    for key in ("error_message", "reason", "settlement_error"):
        if key in payload:
            result[key] = sanitize_worker_text(
                payload.get(key), _MAX_WORKER_ERROR_BYTES
            )
    for key in (
        "terminal",
        "correct",
        "accepted",
        "success",
        "is_valid_no_sorry",
        "is_valid_with_sorry",
        "retryable",
        "cache_reused",
        "cancel_requested",
        "finalization_pending",
        "remote_settlement_unconfirmed",
    ):
        if isinstance(payload.get(key), bool):
            result[key] = payload[key]
    for key in (
        "queue_wait_ms",
        "execution_ms",
        "submitted_at_ms",
        "queue_deadline_ms",
        "lifecycle_deadline_ms",
        "started_at_ms",
        "finished_at_ms",
        "queue_wait_seconds",
        "execution_seconds",
        "admission_attempts",
        "evaluator_overload_resubmissions",
    ):
        number = _safe_nonnegative_number(payload.get(key))
        if number is not None:
            result[key] = number
    if isinstance(payload.get("response"), Mapping):
        result["response"] = safe_worker_response(
            payload["response"], _depth=_depth + 1
        )
    if "probe_diagnostics" in payload:
        result["probe_diagnostics"] = _safe_probe_diagnostics(
            payload.get("probe_diagnostics")
        )
    failure = payload.get("evaluator_failure")
    if isinstance(failure, Mapping):
        safe_failure: dict[str, Any] = {
            "category": _safe_category(failure.get("category")),
        }
        for key in ("attempts", "http_status"):
            value = failure.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                safe_failure[key] = max(0, min(value, 1_000_000))
        retry_after = _safe_nonnegative_number(failure.get("retry_after_seconds"))
        if retry_after is not None:
            safe_failure["retry_after_seconds"] = round(retry_after, 3)
        result["evaluator_failure"] = safe_failure
    cancellation = payload.get("judge_cancellation")
    if isinstance(cancellation, Mapping):
        safe_cancellation = {
            "attempted": cancellation.get("attempted") is True,
            "succeeded": cancellation.get("succeeded") is True,
            "settled": cancellation.get("settled") is True,
            "unconfirmed": cancellation.get("unconfirmed") is True,
        }
        failure_category = cancellation.get("failure_category")
        if isinstance(failure_category, str) and failure_category.strip():
            safe_cancellation["failure_category"] = _safe_category(
                failure_category
            )
        result["judge_cancellation"] = safe_cancellation
    canonical = payload.get("canonical_verdict")
    if isinstance(canonical, Mapping):
        safe_canonical: dict[str, Any] = {}
        for key in ("status", "source_contract_status"):
            if isinstance(canonical.get(key), str):
                safe_canonical[key] = sanitize_worker_text(
                    canonical[key], _MAX_WORKER_STATUS_BYTES
                )
        score = _safe_nonnegative_number(canonical.get("score"))
        if score is not None:
            safe_canonical["score"] = score
        for key in ("correct", "cheating"):
            if isinstance(canonical.get(key), bool):
                safe_canonical[key] = canonical[key]
        result["canonical_verdict"] = safe_canonical
    return result


def _safe_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Backward-compatible internal alias for the response sanitizer."""

    return safe_worker_response(payload)


def _safe_probe_diagnostics(value: Any) -> dict[str, Any]:
    """Re-apply the public Judge probe bounds before exposing diagnostics."""

    truncated = False
    items = value
    if isinstance(value, Mapping):
        items = value.get("items")
        truncated = value.get("truncated") is True
    if not isinstance(items, list):
        return {"items": [], "truncated": truncated}

    safe_items: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        if len(safe_items) >= _MAX_PROBE_DIAGNOSTICS:
            truncated = True
            break
        severity, severity_was_truncated = _bounded_utf8_text(
            sanitize_worker_text(raw.get("severity"), _MAX_PROBE_SEVERITY_BYTES),
            _MAX_PROBE_SEVERITY_BYTES,
            default="info",
        )
        data, data_was_truncated = _bounded_utf8_text(
            sanitize_worker_text(raw.get("data"), _MAX_PROBE_DATA_BYTES),
            _MAX_PROBE_DATA_BYTES,
        )
        truncated = truncated or severity_was_truncated or data_was_truncated
        safe_items.append(
            {
                "severity": severity,
                "data": data,
                "line": _bounded_position(raw.get("line")),
                "column": _bounded_position(raw.get("column")),
            }
        )
    return {"items": safe_items, "truncated": truncated}


def _bounded_utf8_text(
    value: Any,
    max_bytes: int,
    *,
    default: str = "",
) -> tuple[str, bool]:
    text = value if isinstance(value, str) else default
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, value is not None and not isinstance(value, str)
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _bounded_position(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(0, value), _MAX_DIAGNOSTIC_POSITION)


def _safe_category(value: Any) -> str:
    text = str(value or "evaluator_error").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text):
        return "evaluator_error"
    return text


def _safe_status(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text or len(text.encode("utf-8")) > _MAX_WORKER_STATUS_BYTES:
        return "UNKNOWN"
    if not re.fullmatch(r"[A-Z][A-Z0-9_:-]*", text):
        return "UNKNOWN"
    return text


def _safe_nonnegative_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return value


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
