"""Small HTTP client for the ContextSwarmJudge Lean router."""

from __future__ import annotations

import json
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
    ):
        self.base_url = normalize_base_url(base_url)
        self.lean_env_id = lean_env_id
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.verification_profile = verification_profile
        self.judge_mode = judge_mode
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))

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
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
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

    def evaluate(
        self,
        task: Task,
        candidate_path: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> Verdict:
        started = time.monotonic()
        # A score event after the experiment horizon is invalid.  Do not send
        # an already-late candidate into a shared Judge queue just for a
        # diagnostic that can delay container closeout.
        if deadline_monotonic is not None and started >= deadline_monotonic:
            return Verdict(task.slug, "OUT_OF_HORIZON", 0.0, 0.0, {"reason": "run_horizon_elapsed"})
        request_deadline = (
            min(started + self.timeout_seconds, deadline_monotonic + 5.0)
            if deadline_monotonic is not None
            else started + self.timeout_seconds
        )
        try:
            code = candidate_path.read_text(encoding="utf-8")
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
            payload = {
                "code": code,
                "target_code": target,
                "timeout": self.timeout_seconds,
                "max_retries": 1,
                "problem_id": task.problem_id,
                "lean_env_id": self.lean_env_id,
                "verification_profile": self.verification_profile,
                "judge_mode": self.judge_mode,
            }
            submitted = self._request(
                "POST",
                "/api/lean/jobs",
                payload,
                timeout_seconds=max(0.1, request_deadline - time.monotonic()),
            )
            job_id = submitted.get("job_id") or submitted.get("id")
            response = submitted
            if job_id and not _terminal(response):
                while time.monotonic() < request_deadline:
                    response = self._request(
                        "GET",
                        f"/api/lean/jobs/{quote(str(job_id), safe='')}?wait_ms=1000",
                        timeout_seconds=max(0.1, request_deadline - time.monotonic()),
                    )
                    if _terminal(response):
                        break
                    time.sleep(self.poll_interval_seconds)
            status = _status(response)
            proved = _is_proved(response)
            return Verdict(
                task_id=task.slug,
                status="PROVED" if proved else status,
                score=1.0 if proved else 0.0,
                elapsed_seconds=time.monotonic() - started,
                response=_safe_response(response),
            )
        except (OSError, EvaluatorError, UnicodeError) as exc:
            return Verdict(
                task_id=task.slug,
                status="EVALUATOR_ERROR",
                score=0.0,
                elapsed_seconds=time.monotonic() - started,
                error=str(exc),
            )


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
        )


def _status(payload: Mapping[str, Any]) -> str:
    for key in ("formal_status", "verdict", "result", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    canonical = payload.get("canonical_verdict")
    if isinstance(canonical, Mapping):
        value = canonical.get("status")
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
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
        "queue_wait_seconds",
        "execution_seconds",
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
