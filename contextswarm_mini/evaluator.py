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
    "REJECTED_OVERLOADED",
    "OUT_OF_HORIZON",
    "RUNNING",
    "QUEUED",
    "PENDING",
    "CANCELLED",
    "TASK_CANCELLED",
}


class EvaluatorError(RuntimeError):
    """A classified transport or malformed-verdict failure.

    Only bounded, non-sensitive fields are retained so callers can audit the
    failure class without receiving the Judge endpoint, credentials, or host
    paths from an underlying HTTP exception.
    """

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
        bounded = ""
    elif tail and len(text.encode("utf-8")) > maximum:
        bounded = text.encode("utf-8")[-maximum:].decode("utf-8", errors="ignore")
    else:
        bounded, _ = _bounded_utf8_text(text, maximum)
    return bounded


def sanitize_worker_identifier(value: Any) -> str | None:
    """Return a bounded opaque identifier, rejecting path/endpoint-shaped data."""

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


def _cancel_requested(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _wait_for_cancel(
    cancel_event: threading.Event | None,
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
    ):
        self.base_url = normalize_base_url(base_url)
        self.lean_env_id = lean_env_id
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.verification_profile = verification_profile
        self.judge_mode = judge_mode
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self._probe_cache: dict[str, Verdict] = {}
        self._probe_cache_lock = threading.Lock()

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
                    # Admission retries consume the same absolute horizon.
                    # Recompute the submitted integer execution budget on each
                    # retry so the Judge job cannot retain a stale, longer
                    # timeout after a large Retry-After delay.
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
                retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
                backoff = min(
                    _MAX_HTTP_BACKOFF_SECONDS,
                    _MIN_HTTP_BACKOFF_SECONDS * (2 ** min(attempt - 1, 16)),
                )
                delay = _retry_after_seconds(
                    retry_after,
                    default=backoff,
                )
                exc.close()
                if http_status in {429, 503}:
                    # A zero/very small Retry-After must not turn many CPS
                    # workers into a tight retry storm.  Honor larger server
                    # guidance verbatim, otherwise apply bounded backoff.
                    delay = max(delay, backoff)
                    remaining = request_deadline - time.monotonic()
                    if _cancel_requested(cancel_event):
                        raise EvaluatorError(
                            "The Judge request was cancelled.",
                            category="request_cancelled",
                            http_status=http_status,
                            attempts=attempt,
                            retry_after_seconds=delay,
                        ) from None
                    if remaining > 0 and delay < remaining:
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
            except (URLError, TimeoutError, OSError, HTTPException):
                raise EvaluatorError(
                    "The Judge transport failed.",
                    category="network_error",
                    attempts=attempt,
                ) from None
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise EvaluatorError(
                "The Judge returned a non-JSON response.",
                category="malformed_response",
                attempts=attempt,
            ) from None
        if not isinstance(parsed, dict):
            raise EvaluatorError(
                "The Judge returned a non-object response.",
                category="malformed_response",
                attempts=attempt,
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
    ) -> Verdict:
        code = _read_candidate(candidate_path)
        cache_key = self._probe_cache_key(task, code) if code is not None else None
        if cache_key is not None:
            cached = self._cached_verdict(cache_key)
            if cached is not None:
                return cached
        return self._evaluate(
            task,
            candidate_path,
            deadline_monotonic=deadline_monotonic,
            response_profile=None,
            candidate_code=code,
            cancel_event=None,
        )

    def probe(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Verdict:
        """Run the canonical evaluator with bounded worker-facing diagnostics."""

        code = _read_candidate(candidate_path)
        return self._probe_source(
            task,
            candidate_path,
            code,
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
        """Probe an immutable broker-owned source snapshot.

        The broker uses this entry point so the bytes admitted to the Judge,
        the cache key, and the audit hash all describe the same candidate even
        if the worker edits ``result.lean`` while waiting for admission.
        """

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
        if _cancel_requested(cancel_event):
            return self._evaluate(
                task,
                candidate_path,
                deadline_monotonic=deadline_monotonic,
                response_profile=LEAN_PROBE_RESPONSE_PROFILE,
                candidate_code=code,
                cancel_event=cancel_event,
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
            response_profile=LEAN_PROBE_RESPONSE_PROFILE,
            candidate_code=code,
            cancel_event=cancel_event,
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
        response_profile: str | None,
        candidate_code: str | None,
        cancel_event: threading.Event | None,
    ) -> Verdict:
        started = time.monotonic()
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
        if _cancel_requested(cancel_event):
            return Verdict(
                task.slug,
                "TASK_CANCELLED",
                0.0,
                0.0,
                {"reason": "cancel_event_set"},
                **provenance,
            )
        # A score event after the experiment horizon is invalid.  Do not send
        # an already-late candidate into a shared Judge queue just for a
        # diagnostic that can delay container closeout.
        if deadline_monotonic is not None and started >= deadline_monotonic:
            return Verdict(
                task.slug,
                "OUT_OF_HORIZON",
                0.0,
                0.0,
                {"reason": "run_horizon_elapsed"},
                **provenance,
            )
        # ``timeout_seconds`` is the Judge's Lean execution budget.  Admission
        # and queue waits are separate, so the end-to-end evaluator deadline is
        # the runner horizon when one is supplied; the submitted execution
        # timeout below remains capped independently.
        request_deadline = (
            deadline_monotonic
            if deadline_monotonic is not None
            else started + self.timeout_seconds
        )
        job_id: Any = None
        cancel_endpoint: Any = None
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
            remaining_budget = request_deadline - time.monotonic()
            # The Judge contract accepts an integer timeout >= 1.  Do not
            # round a sub-second remainder up into work that can outlive the
            # runner-owned horizon.
            if remaining_budget < 1.0:
                return Verdict(
                    task.slug,
                    "OUT_OF_HORIZON",
                    0.0,
                    time.monotonic() - started,
                    {"reason": "run_horizon_elapsed_before_submission"},
                    **provenance,
                )
            judge_timeout = max(
                1,
                min(self.timeout_seconds, int(remaining_budget)),
            )
            payload = {
                "code": code,
                "target_code": target,
                "timeout": judge_timeout,
                "max_retries": 1,
                "problem_id": task.problem_id,
                "lean_env_id": self.lean_env_id,
                "verification_profile": self.verification_profile,
                "judge_mode": self.judge_mode,
            }
            if response_profile:
                payload["response_profile"] = response_profile
            request_options: dict[str, Any] = {
                "timeout_seconds": request_deadline - time.monotonic(),
            }
            if cancel_event is not None:
                request_options["cancel_event"] = cancel_event
            submitted = self._request(
                "POST",
                "/api/lean/jobs",
                payload,
                **request_options,
            )
            job_id = submitted.get("job_id") or submitted.get("id")
            cancel_endpoint = submitted.get("cancel_endpoint")
            response = submitted
            if job_id and not _terminal(response):
                while time.monotonic() < request_deadline:
                    if _cancel_requested(cancel_event):
                        cancellation = self._cancel_submitted_job(
                            job_id,
                            cancel_endpoint=cancel_endpoint,
                        )
                        return self._cancelled_verdict(
                            task,
                            started=started,
                            provenance=provenance,
                            job_id=job_id,
                            cancellation=cancellation,
                        )
                    remaining_budget = request_deadline - time.monotonic()
                    if remaining_budget <= 0:
                        break
                    wait_ms = max(1, min(1_000, int(remaining_budget * 1_000)))
                    poll_request_timeout = remaining_budget
                    if cancel_event is not None:
                        wait_ms = min(wait_ms, _CANCEL_AWARE_LONG_POLL_MS)
                        poll_request_timeout = min(
                            poll_request_timeout,
                            _CANCEL_AWARE_HTTP_TIMEOUT_SECONDS,
                        )
                    request_options = {"timeout_seconds": poll_request_timeout}
                    if cancel_event is not None:
                        request_options["cancel_event"] = cancel_event
                    response = self._request(
                        "GET",
                        f"/api/lean/jobs/{quote(str(job_id), safe='')}?wait_ms={wait_ms}",
                        **request_options,
                    )
                    if response.get("cancel_endpoint") is not None:
                        cancel_endpoint = response.get("cancel_endpoint")
                    if _terminal(response):
                        break
                    remaining_budget = request_deadline - time.monotonic()
                    if remaining_budget > 0:
                        if _wait_for_cancel(
                            cancel_event,
                            min(self.poll_interval_seconds, remaining_budget),
                        ):
                            cancellation = self._cancel_submitted_job(
                                job_id,
                                cancel_endpoint=cancel_endpoint,
                            )
                            return self._cancelled_verdict(
                                task,
                                started=started,
                                provenance=provenance,
                                job_id=job_id,
                                cancellation=cancellation,
                            )
            status = _status(response)
            safe_response = _safe_response(response)
            normalized_job_id = sanitize_worker_identifier(job_id)
            if normalized_job_id and not (
                safe_response.get("job_id") or safe_response.get("id")
            ):
                safe_response["job_id"] = normalized_job_id
            if not _terminal(response):
                cancellation = (
                    self._cancel_submitted_job(
                        job_id,
                        cancel_endpoint=cancel_endpoint,
                    )
                    if job_id
                    else None
                )
                if cancellation is not None:
                    safe_response["judge_cancellation"] = cancellation
                horizon_elapsed = (
                    deadline_monotonic is not None
                    and time.monotonic() >= deadline_monotonic
                )
                return Verdict(
                    task_id=task.slug,
                    status="OUT_OF_HORIZON" if horizon_elapsed else "EVALUATOR_TIMEOUT",
                    score=0.0,
                    elapsed_seconds=time.monotonic() - started,
                    response=safe_response,
                    error=(
                        None
                        if horizon_elapsed
                        else "Judge job did not reach a terminal verdict within the evaluation deadline"
                    ),
                    judge_job_id=normalized_job_id,
                    **provenance,
                )
            proved = _is_proved(response)
            return Verdict(
                task_id=task.slug,
                status="PROVED" if proved else status,
                score=1.0 if proved else 0.0,
                elapsed_seconds=time.monotonic() - started,
                response=safe_response,
                judge_job_id=normalized_job_id,
                **provenance,
            )
        except EvaluatorError as exc:
            cancellation = (
                self._cancel_submitted_job(
                    job_id,
                    cancel_endpoint=cancel_endpoint,
                )
                if job_id
                else None
            )
            if exc.category == "request_cancelled" or _cancel_requested(cancel_event):
                return self._cancelled_verdict(
                    task,
                    started=started,
                    provenance=provenance,
                    job_id=job_id,
                    cancellation=cancellation,
                )
            if (
                exc.category == "request_deadline_elapsed"
                and deadline_monotonic is not None
                and time.monotonic() >= deadline_monotonic
            ):
                status = "OUT_OF_HORIZON"
            else:
                status = {
                    "judge_overloaded_deadline": "REJECTED_OVERLOADED",
                    "network_error": "NETWORK_ERROR",
                    "request_deadline_elapsed": "EVALUATOR_TIMEOUT",
                }.get(exc.category, "EVALUATOR_ERROR")
            return Verdict(
                task_id=task.slug,
                status=status,
                score=0.0,
                elapsed_seconds=time.monotonic() - started,
                response={
                    "evaluator_failure": exc.public_details(),
                    **(
                        {"judge_cancellation": cancellation}
                        if cancellation is not None
                        else {}
                    ),
                },
                error=sanitize_worker_text(exc),
                judge_job_id=sanitize_worker_identifier(job_id),
                **provenance,
            )
        except (OSError, UnicodeError) as exc:
            return Verdict(
                task_id=task.slug,
                status="EVALUATOR_ERROR",
                score=0.0,
                elapsed_seconds=time.monotonic() - started,
                error=sanitize_worker_text(exc),
                judge_job_id=sanitize_worker_identifier(job_id),
                **provenance,
            )

    def _cancel_submitted_job(
        self,
        job_id: Any,
        *,
        cancel_endpoint: Any = None,
    ) -> dict[str, Any]:
        """Best-effort bounded cancellation for a known non-terminal Judge job."""

        cancel_path, invalid_category = self._cancel_request_path(
            job_id,
            cancel_endpoint=cancel_endpoint,
        )
        # A malformed or cross-origin capability supplied by a response must
        # never suppress the fixed same-origin cancellation path for an
        # otherwise valid opaque job id.
        if cancel_path is None and cancel_endpoint is not None:
            cancel_path, fallback_category = self._cancel_request_path(
                job_id,
                cancel_endpoint=None,
            )
            if cancel_path is None:
                invalid_category = fallback_category
        if cancel_path is None:
            return {
                "attempted": False,
                "succeeded": False,
                "failure_category": invalid_category,
            }
        try:
            self._request(
                "DELETE",
                cancel_path,
                timeout_seconds=_JUDGE_CANCEL_TIMEOUT_SECONDS,
            )
        except EvaluatorError as exc:
            return {
                "attempted": True,
                "succeeded": False,
                "failure_category": exc.category,
            }
        return {
            "attempted": True,
            "succeeded": True,
            "failure_category": None,
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
        digest.update(
            task_contract_sha256(
                task,
                lean_env_id=self.lean_env_id,
                verification_profile=self.verification_profile,
                judge_mode=self.judge_mode,
            ).encode("ascii")
        )
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
    ) -> Verdict:
        try:
            code = candidate_path.read_text(encoding="utf-8")
        except OSError as exc:
            return Verdict(
                task.slug,
                "MISSING_CANDIDATE",
                0.0,
                0.0,
                error=sanitize_worker_text(exc),
                task_contract_sha256=task_contract_sha256(
                    task,
                    lean_env_id="mock",
                    verification_profile="mock",
                    judge_mode="mock",
                ),
            )
        proved = self.prove_without_sorry and "sorry" not in code and "admit" not in code
        return Verdict(
            task.slug,
            "PROVED" if proved else "MOCK_SKIPPED",
            1.0 if proved else 0.0,
            0.0,
            {"mock": True},
            candidate_sha256=candidate_sha256(code),
            task_contract_sha256=task_contract_sha256(
                task,
                lean_env_id="mock",
                verification_profile="mock",
                judge_mode="mock",
            ),
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
            return Verdict(task.slug, "TASK_CANCELLED", 0.0, 0.0)
        return self.evaluate(
            task,
            candidate_path,
            deadline_monotonic=deadline_monotonic,
        )

    def probe_source(
        self,
        task: Task,
        candidate_code: str,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Verdict:
        if _cancel_requested(cancel_event):
            return Verdict(task.slug, "TASK_CANCELLED", 0.0, 0.0)
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
        )


def _status(payload: Mapping[str, Any]) -> str:
    for key in ("formal_status", "verdict", "result", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _safe_status(value)
    canonical = payload.get("canonical_verdict")
    if isinstance(canonical, Mapping):
        value = canonical.get("status")
        if isinstance(value, str) and value.strip():
            return _safe_status(value)
    nested = payload.get("response")
    if isinstance(nested, Mapping):
        return _status(nested)
    return "UNKNOWN"


def _terminal(payload: Mapping[str, Any]) -> bool:
    raw_status = payload.get("status")
    if isinstance(raw_status, str) and raw_status.strip().upper() in {
        "SUCCEEDED",
        "FAILED",
        "ERROR",
        "CANCELLED",
        "REJECTED_OVERLOADED",
    }:
        return True
    status = _status(payload)
    if status in {"QUEUED", "PENDING", "RUNNING", "IN_PROGRESS", "STARTED"}:
        return False
    return bool(
        payload.get("terminal")
        or payload.get("finished_at")
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
            "CANCELLED",
        }
    )


def _is_proved(payload: Mapping[str, Any]) -> bool:
    status = _status(payload)
    if status in {"PROVED", "AC", "PASS", "PASSED"}:
        return True
    if status in {"SUCCEEDED", "COMPLETED"}:
        for key in ("is_valid_no_sorry", "correct", "success", "accepted"):
            if payload.get(key) is True:
                return True
        nested = payload.get("response")
        if isinstance(nested, Mapping) and _is_proved(nested):
            return True
        canonical = payload.get("canonical_verdict")
        return isinstance(canonical, Mapping) and _status(canonical) in {"PROVED", "AC", "PASS", "PASSED"}
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
    for key in ("status", "formal_status", "verdict", "error_code"):
        value = payload.get(key)
        if isinstance(value, str):
            result[key] = sanitize_worker_text(value, _MAX_WORKER_STATUS_BYTES)
    if "error_message" in payload:
        result["error_message"] = sanitize_worker_text(
            payload.get("error_message"), _MAX_WORKER_ERROR_BYTES
        )
    for key in (
        "terminal",
        "correct",
        "accepted",
        "success",
        "is_valid_no_sorry",
        "is_valid_with_sorry",
    ):
        if isinstance(payload.get(key), bool):
            result[key] = payload[key]
    for key in ("queue_wait_seconds", "execution_seconds"):
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
    """Backward-compatible internal alias for the worker response sanitizer."""

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


def _bounded_utf8_text(value: Any, max_bytes: int, *, default: str = "") -> tuple[str, bool]:
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
