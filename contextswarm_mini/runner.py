"""Experiment supervisor for Mono, Parallel, and CPS protocols."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import inspect
import json
import math
import os
from queue import Empty, Queue
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
import traceback
import uuid
from typing import Any, Iterable, Mapping

from .allocation import (
    AgentAllocationPolicy,
    AllocationDecision,
    EvidencePiece,
    FormulaAllocationPolicy,
    TaskProgress,
    TaskProgressSnapshot,
    UniformAllocationPolicy,
    normalize_verdict_status,
)
from .allocation_core import (
    AllocationStateSnapshot,
    LLMSchedulerResponse,
    TaskScoreWeights,
    TaskState,
    TraceFeatures,
    TraceScoreWeights,
    create_allocation_policy,
)
from .allocation_audit import AllocationAuditRecord, append_allocation_audit
from .config import ConfigError, ExperimentConfig
from .cps import CPSStore, CommunicationPolicy, make_policy
from .evaluator import CodingEvaluator, LeanEvaluator, MockEvaluator, sanitize_worker_text
from .elastic_scheduler import AgentAssignment, ElasticScheduler
from .formal_tools import (
    DeclarationIndex,
    FormalToolPolicy,
    ToolCapability,
    prepare_declaration_index,
    stage_worker_tools,
    tool_surface_provenance,
)
from .judge_broker import CandidateSnapshot, JudgeBroker, JudgeBrokerDrainError
from .launch_contract import LaunchContractError, verify_manifest_binding
from .models import AgentResult, Task, Verdict
from .pi_agent import PiAgent
from .preflight import PreflightError, run_preflight
from .prompts import build_mono_prompt, build_task_prompt


def _candidate_name(task: Task) -> str:
    return task.candidate_filename


def _baseline_glob(task: Task) -> str:
    return "*.cpp" if _candidate_name(task) == "result.cpp" else "*.lean"


def _candidate_path(root: Path, task: Task) -> Path:
    """Return the mutable candidate path for either benchmark language."""

    return root / _candidate_name(task)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _exception_artifact_fields(
    exc: BaseException,
    config: ExperimentConfig,
    *,
    traceback_bytes: int | None = None,
) -> dict[str, str]:
    """Return bounded exception fields with runtime capabilities removed."""

    sensitive_values = (
        config.lean_server_url,
        config.aisw_coordinator_url,
        os.environ.get("CONTEXTSWARM_JUDGE_URL"),
        os.environ.get("LEAN_AUTH_TOKEN"),
    )
    fields = {
        "error": sanitize_worker_text(
            exc,
            sensitive_values=sensitive_values,
        )
    }
    if traceback_bytes is not None:
        fields["traceback"] = sanitize_worker_text(
            traceback.format_exc(),
            traceback_bytes,
            sensitive_values=sensitive_values,
            tail=True,
        )
    return fields


@dataclass
class RunLogger:
    output_dir: Path
    lock: threading.Lock

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._horizon_started_monotonic: float | None = None

    def start_horizon(self, started_monotonic: float | None = None) -> float:
        """Bind subsequent score events to one runner-owned monotonic origin."""

        origin = time.monotonic() if started_monotonic is None else float(started_monotonic)
        with self.lock:
            if self._horizon_started_monotonic is not None:
                raise RuntimeError("run horizon has already been started")
            self._horizon_started_monotonic = origin
        return origin

    def event(self, event_type: str, **payload: Any) -> None:
        row = {"at": utc_now(), "event": event_type, **payload}
        with self.lock:
            with (self.output_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def scoreboard(
        self,
        verdict: Verdict,
        *,
        episode: int,
        agent_id: str,
        source: str = "final_evaluation",
    ) -> None:
        scored_monotonic = time.monotonic()
        origin = self._horizon_started_monotonic
        row = {
            "at": utc_now(),
            "horizon_elapsed_seconds": (
                round(max(0.0, scored_monotonic - origin), 6)
                if origin is not None
                else None
            ),
            "source": source,
            "task_id": verdict.task_id,
            "episode": episode,
            "agent_id": agent_id,
            **verdict.as_dict(),
        }
        with self.lock:
            with (self.output_dir / "scoreboard_history.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


@dataclass(frozen=True)
class _EarlyProofCredit:
    verdict: Verdict
    candidate_source: str
    candidate_sha256: str
    actor_id: str
    episode: int


class _CallbackFailureState:
    """Run-wide fail-closed latch for authoritative admission callbacks.

    Broker callbacks run on request threads.  The broker can turn an exception
    into a safe worker-facing ``BROKER_ERROR``, but the runner must still abort
    the arm rather than continue from any artifacts written before that
    exception.  Keep only a boolean latch so exception text from a transport or
    host path can never cross into run artifacts through this object.
    """

    def __init__(self) -> None:
        self._failed = threading.Event()

    @property
    def failed(self) -> bool:
        return self._failed.is_set()

    def record(self) -> None:
        self._failed.set()

    # PiAgent and JudgeBroker accept an Event-compatible cancellation object.
    # Reuse the fatal latch itself so a callback failure promptly revokes every
    # session that shares it instead of merely failing the arm after solvers
    # have consumed the rest of the horizon.
    def is_set(self) -> bool:
        return self._failed.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._failed.wait(timeout)

    def raise_if_failed(self) -> None:
        if self.failed:
            raise RuntimeError("runner worker/admission failure")


class _AnyCancelEvent:
    """Small Event-compatible OR view used across runner-owned lifecycles."""

    def __init__(self, *events: Any, reasons: tuple[str | None, ...] | None = None):
        paired = tuple(
            (event, reasons[index] if reasons is not None and index < len(reasons) else None)
            for index, event in enumerate(events)
            if event is not None
        )
        self._events = tuple(event for event, _reason in paired)
        if reasons is None:
            self._reasons = (None,) * len(self._events)
        else:
            # Filter event/reason pairs together.  Dropping a ``None`` event
            # must not shift a later event onto the wrong reason.
            self._reasons = tuple(reason for _event, reason in paired)

    def is_set(self) -> bool:
        return any(bool(event.is_set()) for event in self._events)

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
            threading.Event().wait(delay)
        return True

    def cancellation_reason(self) -> str | None:
        """Return the reason for the first active source, if one is known."""

        for event, explicit in zip(self._events, self._reasons):
            if not bool(event.is_set()):
                continue
            if explicit:
                return explicit
            nested = getattr(event, "cancellation_reason", None)
            if callable(nested):
                try:
                    reason = nested()
                except Exception:
                    reason = None
                if isinstance(reason, str) and reason:
                    return reason
            return None
        return None


class RemoteJudgeSettlementError(RuntimeError):
    """The run cannot safely admit work while a remote job is unaccounted for."""


def _evaluator_remote_settlement_event(evaluator: Any) -> Any | None:
    event = getattr(evaluator, "remote_settlement_event", None)
    if event is None or not callable(getattr(event, "is_set", None)):
        return None
    return event


def _raise_if_remote_settlement_unconfirmed(
    evaluator: Any,
    verdict: Verdict | None = None,
    *,
    on_failure: Any | None = None,
) -> None:
    if (
        _evaluator_remote_unsettled_jobs(evaluator) <= 0
        and (verdict is None or not _verdict_has_unsettled_remote_work(verdict))
    ):
        return
    if callable(on_failure):
        on_failure()
    raise RemoteJudgeSettlementError(
        "remote Judge work did not provide a job-bound terminal receipt"
    )


@dataclass
class _ElasticTaskState:
    """Run-local state for multiple agents collaborating on one task."""

    task: Task
    task_root: Path
    lock: threading.RLock = field(default_factory=threading.RLock)
    attempts: int = 0
    completed_attempts: int = 0
    solved: bool = False
    retired: bool = False
    best_verdict: Verdict | None = None
    best_candidate: Path | None = None
    last_verdict_status: str = "NONE"
    last_feedback: str = ""
    consecutive_failures: int = 0
    last_assignment_at: float = 0.0
    last_progress_at: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    early_proofs: dict[str, _EarlyProofCredit] = field(default_factory=dict)


@dataclass(frozen=True)
class _FrozenCandidate:
    """Immutable per-task input to the feedback-free closeout phase."""

    task_id: str
    path: Path
    sha256: str | None
    error: str | None = None


@dataclass(frozen=True)
class _CloseoutDecision:
    verdict: Verdict
    observed: Verdict
    prior_authority: Verdict | None
    disposition: str
    authority_mismatch: Mapping[str, Any] | None = None


_AUTHORITATIVE_PROVED_STATUSES = {"PROVED", "AC", "PASS", "PASSED"}
_RETRYABLE_CLOSEOUT_INFRA_STATUSES = {
    "EVALUATOR_ERROR",
    "EVALUATOR_TIMEOUT",
    "EXECUTION_TIMEOUT",
    "INFRASTRUCTURE_ERROR",
    "REJECTED_OVERLOADED",
    "RESOURCE_LIMIT",
}


def _normalized_sha256(value: Any) -> str | None:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    return digest


def _normalized_verdict_status(verdict: Verdict) -> str:
    return str(verdict.status or "").strip().upper()


def _response_value(response: Mapping[str, Any], key: str) -> Any:
    value = response.get(key)
    if value is not None:
        return value
    nested = response.get("response")
    if isinstance(nested, Mapping):
        return _response_value(nested, key)
    return None


def _is_authoritative_proved(verdict: Verdict) -> bool:
    return bool(
        _normalized_verdict_status(verdict) in _AUTHORITATIVE_PROVED_STATUSES
        and float(verdict.score) >= 1.0
        and _normalized_sha256(verdict.candidate_sha256) is not None
        and _normalized_sha256(verdict.task_contract_sha256) is not None
        and str(verdict.judge_job_id or "").strip()
    )


def _prior_authoritative_proof(
    evaluator: Any,
    task: Task,
    candidate: _FrozenCandidate,
    verdicts: Iterable[Verdict],
) -> tuple[Verdict | None, Mapping[str, Any] | None]:
    """Join a prior Judge proof to the frozen candidate and exact task contract."""

    candidate_sha = _normalized_sha256(candidate.sha256)
    try:
        expected_contract = _normalized_sha256(
            _expected_task_contract(evaluator, task)
        )
    except Exception:
        expected_contract = None
    authorities = [
        verdict
        for verdict in verdicts
        if verdict.task_id == task.slug and _is_authoritative_proved(verdict)
    ]
    for verdict in authorities:
        if (
            candidate_sha is not None
            and expected_contract is not None
            and _normalized_sha256(verdict.candidate_sha256) == candidate_sha
            and _normalized_sha256(verdict.task_contract_sha256) == expected_contract
        ):
            return verdict, None
    if not authorities:
        return None, None
    return None, {
        "authoritative_proof_count": len(authorities),
        "candidate_sha256_available": candidate_sha is not None,
        "task_contract_sha256_available": expected_contract is not None,
        "candidate_sha256_match": any(
            _normalized_sha256(verdict.candidate_sha256) == candidate_sha
            for verdict in authorities
        ) if candidate_sha is not None else False,
        "task_contract_sha256_match": any(
            _normalized_sha256(verdict.task_contract_sha256) == expected_contract
            for verdict in authorities
        ) if expected_contract is not None else False,
    }


def _authoritative_proof_matches(
    verdict: Verdict,
    *,
    candidate_sha256: str,
    task_contract_sha256: str,
) -> bool:
    return bool(
        _is_authoritative_proved(verdict)
        and _normalized_sha256(verdict.candidate_sha256) == candidate_sha256
        and _normalized_sha256(verdict.task_contract_sha256) == task_contract_sha256
    )


def _retryable_closeout_infrastructure_failure(verdict: Verdict) -> bool:
    return bool(
        _normalized_verdict_status(verdict) in _RETRYABLE_CLOSEOUT_INFRA_STATUSES
        and _response_value(verdict.response, "retryable") is True
    )


def _mark_closeout_infrastructure_incomplete(
    task: Task,
    candidate: _FrozenCandidate,
    prior: Verdict,
    observed: Verdict,
) -> Verdict:
    """Return a zero-score fresh-closeout failure, never a reused proof.

    A solver-phase Judge receipt can explain why the frozen candidate was
    selected, but it cannot stand in for the independent outer closeout.  Do
    not copy its ``PROVED`` status, score, or job id into the final verdict.
    """

    response = dict(observed.response)
    response["closeout_infra_incomplete"] = {
        "observed_status": _normalized_verdict_status(observed),
        "error_kind": _response_value(observed.response, "error_kind"),
        "terminal_reason": _response_value(observed.response, "terminal_reason"),
        "retryable": True,
    }
    response["prior_authoritative_proof_available"] = True
    response["fresh_closeout_confirmed"] = False
    candidate_sha = _normalized_sha256(observed.candidate_sha256)
    if candidate_sha is None:
        candidate_sha = _normalized_sha256(candidate.sha256)
    contract_sha = _normalized_sha256(observed.task_contract_sha256)
    if contract_sha is None:
        contract_sha = _normalized_sha256(prior.task_contract_sha256)
    return Verdict(
        task_id=task.slug,
        status=_normalized_verdict_status(observed) or "CLOSEOUT_INCOMPLETE",
        score=0.0,
        elapsed_seconds=observed.elapsed_seconds,
        response=response,
        error=observed.error or "fresh outer closeout did not complete",
        candidate_sha256=candidate_sha,
        task_contract_sha256=contract_sha,
        # Keep only the fresh observation's receipt; the prior solver receipt
        # is deliberately not promoted to final authority.
        judge_job_id=observed.judge_job_id,
        cache_reused=observed.cache_reused,
    )


def _preserve_authority_after_confirmation(
    prior: Verdict,
    observed: Verdict,
) -> Verdict:
    """Keep the original exact-once receipt while recording revalidation."""

    response = dict(prior.response)
    response["closeout_authority_confirmed"] = {
        "observed_status": _normalized_verdict_status(observed),
        "candidate_sha256_match": (
            _normalized_sha256(observed.candidate_sha256)
            == _normalized_sha256(prior.candidate_sha256)
        ),
        "task_contract_sha256_match": (
            _normalized_sha256(observed.task_contract_sha256)
            == _normalized_sha256(prior.task_contract_sha256)
        ),
    }
    return Verdict(
        task_id=prior.task_id,
        status="PROVED",
        score=1.0,
        elapsed_seconds=prior.elapsed_seconds,
        response=response,
        error=prior.error,
        candidate_sha256=prior.candidate_sha256,
        task_contract_sha256=prior.task_contract_sha256,
        judge_job_id=prior.judge_job_id,
        cache_reused=prior.cache_reused,
    )


def _authority_conflict_verdict(
    task: Task,
    prior: Verdict,
    observed: Verdict,
) -> Verdict:
    return Verdict(
        task_id=task.slug,
        status="AUTHORITY_CONFLICT",
        score=0.0,
        elapsed_seconds=observed.elapsed_seconds,
        response={
            "reason": "nonretryable_closeout_authority_contradiction",
            "prior_status": _normalized_verdict_status(prior),
            "observed_status": _normalized_verdict_status(observed),
            "observed_error_kind": _response_value(observed.response, "error_kind"),
            "observed_retryable": _response_value(observed.response, "retryable") is True,
        },
        error=(
            "The same candidate and task contract received a non-retryable "
            "closeout verdict after an authoritative Judge proof"
        ),
        candidate_sha256=prior.candidate_sha256,
        task_contract_sha256=prior.task_contract_sha256,
    )


def _verdict_priority(verdict: Verdict | None) -> tuple[int, float]:
    if verdict is None:
        return (-1, -1.0)
    status_rank = {
        "PROVED": 4,
        "COMPILES_WITH_SORRY": 2,
        "VERIFY_FAIL": 1,
        "LOCAL_REJECTED": 0,
        "MOCK_SKIPPED": 0,
        "RUNNING": -1,
        "OUT_OF_HORIZON": -1,
        "EVALUATOR_TIMEOUT": -1,
        "EVALUATOR_ERROR": -1,
        "INFRASTRUCTURE_ERROR": -1,
        "EXECUTION_TIMEOUT": -1,
        "RESOURCE_LIMIT": -1,
        "CANCELLED": -1,
    }
    return (status_rank.get(normalize_verdict_status(verdict.status), 0), float(verdict.score))


_AUTHORITATIVE_CANDIDATE_STATUSES = frozenset(
    {"PROVED", "COMPILES_WITH_SORRY", "VERIFY_FAIL"}
)
_INFRASTRUCTURE_VERDICT_STATUSES = frozenset(
    {
        "EVALUATOR_ERROR",
        "EVALUATOR_TIMEOUT",
        "NETWORK_ERROR",
        "REJECTED_OVERLOADED",
        "BROKER_ERROR",
        "JUDGE_ADMISSION_ERROR",
        "JUDGE_ADMISSION_TIMEOUT",
        "CANDIDATE_SNAPSHOT_ERROR",
        "SESSION_PROBE_BUDGET_EXHAUSTED",
        "INVALID_REQUEST",
        "INVALID_TASK_SELECTION",
        "PROVENANCE_INVALID",
        "REMOTE_SETTLEMENT_UNCONFIRMED",
    }
)
_NONTERMINAL_VERDICT_STATUSES = frozenset(
    {"RUNNING", "QUEUED", "PENDING", "IN_PROGRESS", "STARTED", "UNKNOWN"}
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_MANIFEST_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MANIFEST_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+\.toml")


def _is_infrastructure_verdict(verdict: Verdict) -> bool:
    status = normalize_verdict_status(verdict.status)
    return bool(
        status in _INFRASTRUCTURE_VERDICT_STATUSES
        or (
            status in {"EXECUTION_TIMEOUT", "RESOURCE_LIMIT"}
            and _response_value(verdict.response, "retryable") is True
        )
    )


def _runtime_provenance(
    config: ExperimentConfig,
    *,
    mock_agent: bool,
) -> dict[str, str | bool]:
    """Bind formal artifacts to the immutable image that executed them."""

    image_revision = str(
        os.environ.get("CONTEXTSWARM_IMAGE_REVISION") or ""
    ).strip()
    baked_source_commit = str(
        os.environ.get("CONTEXTSWARM_SOURCE_COMMIT") or ""
    ).strip()
    image_id = str(os.environ.get("CONTEXTSWARM_IMAGE_ID") or "").strip()
    manifest_path = str(
        os.environ.get("CONTEXTSWARM_MANIFEST_PATH") or ""
    ).strip()
    manifest_sha256 = str(
        os.environ.get("CONTEXTSWARM_MANIFEST_SHA256") or ""
    ).strip()
    path = PurePosixPath(manifest_path)
    manifest_bound = bool(
        _MANIFEST_PATH_RE.fullmatch(manifest_path)
        and not path.is_absolute()
        and path.parts
        and all(part not in {"", ".", ".."} for part in path.parts)
        and _MANIFEST_SHA256_RE.fullmatch(manifest_sha256)
    )
    if manifest_bound:
        try:
            canonical_manifest_path = (
                config.manifest_path.resolve()
                .relative_to(config.repo_root.resolve())
                .as_posix()
            )
        except (OSError, ValueError):
            manifest_bound = False
        else:
            manifest_bound = manifest_path == canonical_manifest_path
        if manifest_bound:
            try:
                verify_manifest_binding(
                    manifest_path,
                    config.repo_root,
                    manifest_sha256,
                )
            except (LaunchContractError, OSError, ValueError):
                manifest_bound = False
    if (
        _SOURCE_COMMIT_RE.fullmatch(image_revision)
        and _SOURCE_COMMIT_RE.fullmatch(baked_source_commit)
        and image_revision == baked_source_commit
        and _IMAGE_ID_RE.fullmatch(image_id)
        and manifest_bound
    ):
        return {
            "source_commit": image_revision,
            "image_id": image_id,
            "manifest_path": manifest_path,
            "manifest_sha256": manifest_sha256,
        }
    if mock_agent:
        return {
            "source_commit": "test-only-mock-source",
            "image_id": "test-only-mock-image",
            "test_only": True,
        }
    raise ConfigError(
        "formal runs require a valid immutable image revision and image ID, plus "
        "a valid manifest binding, with the image revision matching the baked source commit"
    )


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _expected_task_contract(evaluator: Any, task: Task) -> str:
    method = getattr(evaluator, "expected_task_contract_sha256", None)
    if not callable(method) and getattr(
        evaluator, "_contextswarm_legacy_test_mock", False
    ):
        # A few lifecycle tests replace MockEvaluator with a deliberately tiny
        # recording double.  Give only that explicitly marked, test-only
        # object a deterministic contract; real evaluators still fail closed.
        digest = hashlib.sha256()
        for value in (
            task.slug,
            task.problem_id,
            task.theorem_name,
            task.baseline_code,
            "legacy-test-mock",
        ):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()
    if not callable(method):
        raise ValueError("evaluator does not expose its expected task contract")
    value = str(method(task) or "").strip().lower()
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("evaluator returned an invalid expected task contract")
    return value


def _allows_mock_provenance(evaluator: Any) -> bool:
    return bool(
        getattr(evaluator, "is_mock_evaluator", False) is True
        or getattr(evaluator, "_contextswarm_legacy_test_mock", False)
    )


def _bind_legacy_test_mock_verdict(
    evaluator: Any,
    task: Task,
    candidate: Path,
    verdict: Verdict,
) -> Verdict:
    """Add candidate binding only for an explicitly marked mock test double."""

    if not getattr(evaluator, "_contextswarm_legacy_test_mock", False):
        return verdict
    response = dict(verdict.response)
    response["mock"] = True
    return Verdict(
        task_id=verdict.task_id,
        status=verdict.status,
        score=verdict.score,
        elapsed_seconds=verdict.elapsed_seconds,
        response=response,
        error=verdict.error,
        candidate_sha256=_file_sha256(candidate),
        task_contract_sha256=_expected_task_contract(evaluator, task),
        judge_job_id=verdict.judge_job_id,
        cache_reused=verdict.cache_reused,
    )


def _has_authoritative_provenance(
    verdict: Verdict,
    candidate: Path,
    *,
    expected_task_contract_sha256: str,
    allow_mock_provenance: bool,
) -> bool:
    candidate_hash = str(verdict.candidate_sha256 or "").lower()
    contract_hash = str(verdict.task_contract_sha256 or "").lower()
    return bool(
        normalize_verdict_status(verdict.status) in _AUTHORITATIVE_CANDIDATE_STATUSES
        and _SHA256_RE.fullmatch(candidate_hash)
        and contract_hash == expected_task_contract_sha256
        and (
            verdict.judge_job_id
            or (allow_mock_provenance and verdict.response.get("mock") is True)
        )
        and _file_sha256(candidate) == candidate_hash
    )


def _has_authoritative_snapshot_provenance(
    verdict: Verdict,
    snapshot: CandidateSnapshot,
    *,
    expected_task_contract_sha256: str,
    allow_mock_provenance: bool,
) -> bool:
    """Validate a broker-frozen candidate without rereading mutable worker state."""

    candidate_hash = str(verdict.candidate_sha256 or "").lower()
    contract_hash = str(verdict.task_contract_sha256 or "").lower()
    return bool(
        normalize_verdict_status(verdict.status) == "PROVED"
        and float(verdict.score) >= 1.0
        and _SHA256_RE.fullmatch(candidate_hash)
        and contract_hash == expected_task_contract_sha256
        and (
            verdict.judge_job_id
            or (allow_mock_provenance and verdict.response.get("mock") is True)
        )
        and candidate_hash == snapshot.sha256
    )


def _enforce_verdict_provenance(
    verdict: Verdict,
    candidate: Path,
    *,
    expected_task_contract_sha256: str,
    allow_mock_provenance: bool,
) -> Verdict:
    """Fail closed before a candidate verdict reaches score aggregation."""

    status = normalize_verdict_status(verdict.status)
    requires_provenance = (
        status in _AUTHORITATIVE_CANDIDATE_STATUSES or float(verdict.score) > 0.0
    )
    if not requires_provenance or _has_authoritative_provenance(
        verdict,
        candidate,
        expected_task_contract_sha256=expected_task_contract_sha256,
        allow_mock_provenance=allow_mock_provenance,
    ):
        return verdict
    return Verdict(
        task_id=verdict.task_id,
        status="PROVENANCE_INVALID",
        score=0.0,
        elapsed_seconds=verdict.elapsed_seconds,
        response={
            "reason": "verdict was not bound to the exact candidate and task contract",
            "original_status": verdict.status,
            "reported_candidate_sha256": verdict.candidate_sha256,
            "actual_candidate_sha256": _file_sha256(candidate),
        },
        error="authoritative or positive verdict failed candidate-bound provenance checks",
        candidate_sha256=verdict.candidate_sha256,
        task_contract_sha256=verdict.task_contract_sha256,
        judge_job_id=verdict.judge_job_id,
        cache_reused=verdict.cache_reused,
    )


def _atomic_write_candidate(raw: bytes, destination: Path, expected_sha256: str) -> str:
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError("candidate bytes do not match their authoritative Judge verdict")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return actual


def _atomic_promote_source(
    source: str,
    destination: Path,
    expected_sha256: str,
) -> str:
    return _atomic_write_candidate(source.encode("utf-8"), destination, expected_sha256)


def _atomic_promote_candidate(source: Path, destination: Path, expected_sha256: str) -> str:
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("candidate changed after its authoritative Judge verdict")
    return _atomic_write_candidate(raw, destination, expected_sha256)


def _publish_authoritative_validation(
    policy: CommunicationPolicy,
    task_id: str,
    solver_actor: str,
    *,
    label: str,
    verdict: Verdict,
    feedback: str,
    deadline_epoch_ms: int | None = None,
) -> None:
    candidate_hash = str(verdict.candidate_sha256 or "").lower()
    contract_hash = str(verdict.task_contract_sha256 or "").lower()
    if (
        not policy.enabled
        or normalize_verdict_status(verdict.status) not in _AUTHORITATIVE_CANDIDATE_STATUSES
        or not _SHA256_RE.fullmatch(candidate_hash)
        or not _SHA256_RE.fullmatch(contract_hash)
        or not (verdict.judge_job_id or verdict.response.get("mock") is True)
    ):
        return
    body = json.dumps(
        {
            "schema_version": "contextswarm_runner_validation_v1",
            "solver_actor": solver_actor,
            "status": verdict.status,
            "score": verdict.score,
            "candidate_sha256": candidate_hash,
            "task_contract_sha256": contract_hash,
            "judge_job_id": str(verdict.judge_job_id or "mock")[:256],
            "feedback": feedback[:1_200],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    policy.publish(
        task_id,
        "runner",
        kind="validation_result",
        title=f"{label}: {verdict.status}",
        body=body,
        tags=("runner_authoritative", "judge_verified"),
        deadline_epoch_ms=deadline_epoch_ms,
    )


_ENDPOINT_RE = re.compile(r"https?://[^\s\])}>\"']+")


def _allocation_feedback(verdict: Verdict) -> str:
    raw = str(
        verdict.response.get("error_message")
        or verdict.response.get("reason")
        or verdict.error
        or verdict.status
    )
    return _ENDPOINT_RE.sub("<redacted-endpoint>", raw).strip()[:1_200]


def _seconds_since_cps_timestamp(raw: str) -> float | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return max(0.0, (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())


def _allocation_runtime_metrics(
    history: Iterable[Mapping[str, object]],
    *,
    run_started_monotonic: float,
    deadline: float,
    max_parallel: int,
    policy_latency_seconds: float,
) -> dict[str, Any]:
    admitted: dict[str, tuple[str, float]] = {}
    finished: dict[str, float] = {}
    reservations: dict[str, tuple[int, float]] = {}
    reservation_seconds = 0.0
    for event in history:
        event_type = str(event.get("event") or "")
        agent_id = str(event.get("agent_id") or "")
        if event_type == "agent_admitted" and agent_id:
            admitted[agent_id] = (
                str(event.get("task_id") or ""),
                float(event.get("admitted_at") or run_started_monotonic),
            )
        elif event_type == "agent_finished" and agent_id:
            finished[agent_id] = float(event.get("finished_at") or deadline)
        elif event_type == "reservation_acquired":
            reservation_id = str(event.get("reservation_id") or "")
            if reservation_id:
                reservations[reservation_id] = (
                    int(event.get("slots") or 0),
                    float(event.get("acquired_at") or run_started_monotonic),
                )
        elif event_type == "reservation_completed":
            reservation_id = str(event.get("reservation_id") or "")
            held = reservations.pop(reservation_id, None)
            if held is not None:
                slots, started = held
                completed = float(event.get("completed_at") or deadline)
                reservation_seconds += slots * max(
                    0.0,
                    min(deadline, completed) - max(run_started_monotonic, started),
                )
    per_task: dict[str, float] = {}
    solver_seconds = 0.0
    for agent_id, (task_id, started) in admitted.items():
        bounded_start = min(deadline, max(run_started_monotonic, started))
        bounded_end = min(deadline, max(bounded_start, finished.get(agent_id, deadline)))
        duration = max(0.0, bounded_end - bounded_start)
        solver_seconds += duration
        per_task[task_id] = per_task.get(task_id, 0.0) + duration
    capacity_seconds = max(0.0, deadline - run_started_monotonic) * max_parallel
    solver_utilization = solver_seconds / capacity_seconds if capacity_seconds else 0.0
    for slots, started in reservations.values():
        reservation_seconds += slots * max(
            0.0, min(deadline, deadline) - max(run_started_monotonic, started)
        )
    # Legacy agent arms predate capacity reservations. Keep their historical
    # latency accounting; registered LLM Scheduler arms use actual reservation
    # slot-seconds and never double-count model latency.
    scheduler_compute_seconds = (
        reservation_seconds if reservation_seconds else max(0.0, policy_latency_seconds)
    )
    compute_seconds = solver_seconds + scheduler_compute_seconds
    compute_utilization = compute_seconds / capacity_seconds if capacity_seconds else 0.0
    return {
        "solver_agent_seconds": round(solver_seconds, 6),
        "scheduler_compute_seconds": round(scheduler_compute_seconds, 6),
        "scheduler_reserved_slot_seconds": round(reservation_seconds, 6),
        "capacity_seconds": round(capacity_seconds, 6),
        "solver_slot_utilization": round(min(1.0, solver_utilization), 8),
        "compute_slot_utilization": round(min(1.0, compute_utilization), 8),
        "per_task_agent_seconds": {
            task_id: round(seconds, 6) for task_id, seconds in sorted(per_task.items())
        },
    }


def _scheduler_token_usage(trace_path: Path) -> dict[str, int]:
    per_session: dict[str, dict[str, int]] = {}
    try:
        lines = trace_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(row.get("actor_id") or "").startswith("allocation-scheduler-"):
            continue
        session = str(row.get("session_id") or row.get("actor_id") or "unknown")
        usage = per_session.setdefault(session, {})
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"):
            value = row.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                usage[key] = max(usage.get(key, 0), value)
    return {
        "scheduler_model_sessions": len(per_session),
        "scheduler_input_tokens": sum(row.get("input_tokens", 0) for row in per_session.values()),
        "scheduler_output_tokens": sum(row.get("output_tokens", 0) for row in per_session.values()),
        "scheduler_cache_read_tokens": sum(row.get("cache_read_tokens", 0) for row in per_session.values()),
        "scheduler_cache_write_tokens": sum(row.get("cache_write_tokens", 0) for row in per_session.values()),
        "scheduler_total_tokens": sum(row.get("total_tokens", 0) for row in per_session.values()),
    }


_FIGURE4_POLICIES = frozenset(
    {"uniform_refill", "task_state", "trace_state", "llm_scheduler"}
)


def _core_snapshot_from_legacy(
    snapshot: TaskProgressSnapshot,
    config: ExperimentConfig,
    *,
    scheduler_reserved_slots: int = 0,
) -> AllocationStateSnapshot:
    """Project the legacy runner snapshot into the issue #39 immutable API.

    Until the Figure 3 selector bridge is installed, trace values are an
    explicit zero projection. This keeps the development arm runnable and
    makes Trace-State exactly reproduce Task-State on the same state.
    """

    tasks = tuple(
        TaskState(
            task_id=task.task_id,
            eligible=task.eligible,
            active_allocations=task.active_agents,
            checker_quality=max(0.0, min(1.0, task.best_score)),
            recent_progress=math.exp(
                -max(0.0, task.seconds_since_progress)
                / max(1.0, config.allocation.normalization["progress_window_seconds"])
            ),
            starvation=min(
                1.0,
                max(0.0, task.seconds_since_last_assignment)
                / max(1.0, config.allocation.normalization["starvation_window_seconds"]),
            ),
            failure_no_progress=min(
                1.0,
                max(0, task.consecutive_failures)
                / max(1.0, config.allocation.normalization["failure_saturation"]),
            ),
            trace=TraceFeatures(),
        )
        for task in snapshot.tasks
    )
    task_weights = config.allocation.task_state
    trace_weights = config.allocation.trace_state
    parameters = {
        "task_state": dict(task_weights),
        "trace_state": dict(trace_weights),
        "normalization": dict(config.allocation.normalization),
    }
    allocation_config_sha256 = hashlib.sha256(
        json.dumps(
            parameters,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    active_solver_slots = sum(task.active_allocations for task in tasks)
    return AllocationStateSnapshot(
        decision_id=f"decision-{snapshot.decision_index:08d}",
        decision_index=snapshot.decision_index,
        elapsed_seconds=snapshot.elapsed_seconds,
        remaining_seconds=snapshot.remaining_seconds,
        total_capacity=config.max_parallel,
        active_solver_slots=active_solver_slots,
        scheduler_reserved_slots=scheduler_reserved_slots,
        free_slots=max(
            0,
            config.max_parallel - active_solver_slots - scheduler_reserved_slots,
        ),
        tasks=tasks,
        allocation_config_sha256=allocation_config_sha256,
        allocation_parameters=parameters,
    )


def _legacy_decision_from_core(core: Any) -> AllocationDecision:
    """Keep existing runner/artifact consumers compatible with core decisions."""

    return AllocationDecision(
        decision_index=core.decision_index,
        policy=core.policy,
        selected_task_id=core.selected_task_id,
        reason=core.reason,
        evidence_piece_ids=list(core.trace_reference_ids),
        latency_seconds=(
            float(core.scheduler_cost.latency_seconds)
            if core.scheduler_cost is not None
            else 0.0
        ),
        fallback=core.fallback,
        fallback_reason=core.fallback_reason,
        scores=dict(core.scores),
        features={
            task_id: {"task_score": float(core.task_scores.get(task_id, 0.0)),
                      "trace_increment": float(core.trace_increments.get(task_id, 0.0))}
            for task_id in core.scores
        },
    )


def _runtime_limit_snapshot() -> dict[str, Any]:
    """Record effective cgroup limits without trusting launcher declarations."""

    def read_value(path: str) -> int | str | None:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if value == "max":
            return "max"
        try:
            return int(value)
        except ValueError:
            return value[:120]

    return {
        "source": "cgroup_v2",
        "memory_max_bytes": read_value("/sys/fs/cgroup/memory.max"),
        "pids_max": read_value("/sys/fs/cgroup/pids.max"),
        "cpu_max": read_value("/sys/fs/cgroup/cpu.max"),
        "process_uid": os.getuid(),
        "process_gid": os.getgid(),
    }


def _score_time_metrics(run_dir: Path, *, horizon_seconds: float, max_score: int) -> dict[str, Any]:
    try:
        meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
        started = dt.datetime.fromisoformat(
            str(meta.get("horizon_started_at") or meta["started_at"]).replace("Z", "+00:00")
        )
        if started.tzinfo is None:
            started = started.replace(tzinfo=dt.timezone.utc)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        started = dt.datetime.now(dt.timezone.utc)
    proofs: dict[str, float] = {}
    try:
        lines = (run_dir / "scoreboard_history.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
            # Frozen closeout is the fair terminal score, not an in-horizon
            # discovery.  Only realtime Judge/solver evidence contributes to
            # the score-time objective.
            if str(row.get("source") or "") == "closeout":
                continue
            if float(row.get("score") or 0.0) < 1.0:
                continue
            task_id = str(row.get("task_id") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        raw_elapsed = row.get("horizon_elapsed_seconds")
        elapsed: float
        if isinstance(raw_elapsed, (int, float)) and not isinstance(raw_elapsed, bool):
            elapsed = float(raw_elapsed)
            if not math.isfinite(elapsed):
                continue
        else:
            try:
                at = dt.datetime.fromisoformat(
                    str(row.get("at") or "").replace("Z", "+00:00")
                )
                if at.tzinfo is None:
                    at = at.replace(tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                continue
            elapsed = (at - started).total_seconds()
        elapsed = min(max(0.0, elapsed), max(0.0, horizon_seconds))
        if task_id and (task_id not in proofs or elapsed < proofs[task_id]):
            proofs[task_id] = elapsed
    ordered = sorted(proofs.values())
    horizon = max(0.0, float(horizon_seconds))
    area = 0.0
    previous = 0.0
    score = 0
    for elapsed in ordered:
        area += score * max(0.0, elapsed - previous)
        score += 1
        previous = elapsed
    area += score * max(0.0, horizon - previous)
    denominator = horizon * max_score
    return {
        "score_time_auc": round(area, 6),
        "normalized_score_time_auc": round(area / denominator, 8) if denominator else 0.0,
        "verified_proof_times_seconds": [round(value, 6) for value in ordered],
        "time_to_first_proof_seconds": round(ordered[0], 6) if ordered else None,
        "time_to_k_proofs_seconds": {
            str(index): round(value, 6) for index, value in enumerate(ordered, start=1)
        },
    }


def load_tasks(config: ExperimentConfig) -> list[Task]:
    dataset_root = config.resolve_runtime_path(str(config.dataset_root))
    ids_path = config.resolve_runtime_path(str(config.problem_ids_path))
    try:
        ids = json.loads(ids_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load problem ids: {ids_path}: {exc}") from exc
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        raise ConfigError(f"problem_ids must be a list of strings: {ids_path}")

    manifest_path = dataset_root / "manifest.json"
    try:
        bundle_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        bundle_manifest = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load benchmark manifest: {manifest_path}: {exc}") from exc
    if not isinstance(bundle_manifest, dict):
        raise ConfigError(f"benchmark manifest is not an object: {manifest_path}")

    bundle_language = str(bundle_manifest.get("language") or "").strip().lower()
    coding_bundle = config.is_coding or bundle_language in {"cpp", "c++", "c"}
    public_metadata: dict[str, Any] = {}
    public_metadata_name = str(bundle_manifest.get("public_metadata") or "").strip()
    if public_metadata_name:
        public_metadata_path = dataset_root / public_metadata_name
        try:
            loaded_public_metadata = json.loads(
                public_metadata_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"cannot load benchmark public metadata: {public_metadata_path}: {exc}"
            ) from exc
        if not isinstance(loaded_public_metadata, dict):
            raise ConfigError(
                f"benchmark public metadata is not an object: {public_metadata_path}"
            )
        public_metadata = loaded_public_metadata

    tasks: list[Task] = []
    for slug in ids:
        root = dataset_root / slug
        metadata_path = root / "metadata.json"
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"cannot load task metadata {metadata_path}: {exc}") from exc
        elif coding_bundle:
            metadata = dict(public_metadata.get(slug) or {})
        else:
            raise ConfigError(f"incomplete task {slug}: missing {metadata_path}")
        if not isinstance(metadata, dict):
            raise ConfigError(f"metadata is not an object: {metadata_path}")
        metadata = dict(metadata)
        for key in (
            "language",
            "candidate_filename",
            "dataset",
            "benchmark_revision",
            "verification_profile",
        ):
            value = bundle_manifest.get(key)
            if value is not None:
                metadata.setdefault(key, value)
        metadata.setdefault("problem_id", slug)

        problem_path = root / "problem.md"
        if problem_path.is_file():
            try:
                problem_text = problem_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot load task statement {problem_path}: {exc}") from exc
        elif coding_bundle and metadata.get("description_no_samples") is not None:
            title = str(metadata.get("name") or slug)
            body = str(metadata.get("description_no_samples") or "").rstrip()
            sample_blocks: list[str] = []
            samples = metadata.get("samples")
            if isinstance(samples, list):
                for index, sample in enumerate(samples, start=1):
                    if not isinstance(sample, Mapping):
                        continue
                    sample_input = str(sample.get("input") or "").rstrip("\n")
                    sample_output = str(sample.get("output") or "").rstrip("\n")
                    sample_blocks.append(
                        f"### Sample {index}\n\nInput:\n```text\n{sample_input}\n```\n\n"
                        f"Output:\n```text\n{sample_output}\n```"
                    )
            sample_text = "\n\n".join(sample_blocks)
            problem_text = f"# {title}\n\n{body}"
            if sample_text:
                problem_text += f"\n\n## Samples\n\n{sample_text}"
            problem_text += "\n"
        else:
            raise ConfigError(f"incomplete task {slug}: missing {problem_path}")

        baseline_files = sorted((root / "baseline").glob("*.lean")) or sorted(
            (root / "baseline").glob("*.cpp")
        )
        if baseline_files:
            baseline_source = baseline_files[0]
            try:
                baseline_code = baseline_source.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot load task baseline {baseline_source}: {exc}") from exc
            metadata.setdefault("baseline_filename", baseline_source.name)
            inferred_coding = baseline_source.suffix.lower() == ".cpp"
        elif coding_bundle:
            baseline_code = (
                "#include <bits/stdc++.h>\n"
                "using namespace std;\n\n"
                "int main() {\n"
                "    ios::sync_with_stdio(false);\n"
                "    cin.tie(nullptr);\n"
                "    return 0;\n"
                "}\n"
            )
            metadata.setdefault("baseline_filename", "baseline.cpp")
            inferred_coding = True
        else:
            raise ConfigError(f"task {slug} has no baseline source")
        metadata.setdefault("candidate_filename", "result.cpp" if inferred_coding else "result.lean")
        metadata.setdefault("language", "cpp" if inferred_coding else "lean")
        tasks.append(
            Task(
                slug=slug,
                root=root,
                problem_text=problem_text,
                baseline_code=baseline_code,
                metadata=metadata,
            )
        )
    if config.max_tasks:
        tasks = tasks[: config.max_tasks]
    if not tasks:
        raise ConfigError("manifest selected no tasks")
    return tasks


def plan(config: ExperimentConfig, tasks: Iterable[Task]) -> dict[str, Any]:
    task_list = list(tasks)
    if config.mode == "mono":
        sessions = 1
    elif config.uses_cps:
        sessions = min(config.max_parallel, len(task_list) * config.initial_agents_per_task)
    else:
        sessions = len(task_list)
    return {
        "name": config.name,
        "mode": config.mode,
        "communication": config.communication,
        "tasks": [task.slug for task in task_list],
        "task_count": len(task_list),
        "episodes_per_task": config.episodes_per_task,
        "max_parallel": config.max_parallel,
        "initial_agents_per_task": config.initial_agents_per_task,
        "max_attempts_per_task": config.max_attempts_per_task,
        "assignment_policy": config.assignment_policy,
        "allocation": config.allocation.public_dict(),
        "planned_agent_sessions": sessions,
        "backend": "nurouter_pi" if config.aisw_enabled else "pi",
        "judge_kind": config.judge_kind,
        "aisw_max_in_flight": config.aisw_max_in_flight,
        "model": config.model,
        "thinking": config.thinking,
        "lean_server_configured": bool(config.lean_server_url),
        "lean_env_id": config.lean_env_id,
        "lean_max_concurrent_evaluations": config.lean_max_concurrent_evaluations,
    }


def run_experiment(
    config: ExperimentConfig,
    *,
    dry_run: bool = False,
    mock_agent: bool = False,
    mock_proved: bool = False,
    output_override: Path | None = None,
) -> Path:
    if not mock_agent and not config.lean_server_url:
        raise ConfigError(
            "CONTEXTSWARM_JUDGE_URL must be set for a real experiment run"
        )
    runtime_provenance = _runtime_provenance(config, mock_agent=mock_agent)
    tasks = load_tasks(config)
    run_id = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = output_override or config.resolved_output_root
    run_dir = Path(output_root).resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    logger = RunLogger(run_dir)
    manifest_snapshot = config.public_dict()
    manifest_snapshot["run_id"] = run_id
    manifest_snapshot["started_at"] = utc_now()
    manifest_snapshot["repo_root"] = str(config.repo_root)
    manifest_snapshot["effective_runtime_limits"] = _runtime_limit_snapshot()
    manifest_snapshot["runtime_provenance"] = runtime_provenance
    (run_dir / "run_meta.json").write_text(
        json.dumps(manifest_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.event("run_started", run_id=run_id, **plan(config, tasks))
    if dry_run:
        (run_dir / "dry_run.json").write_text(
            json.dumps(plan(config, tasks), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        logger.event("dry_run_finished")
        _write_final(run_dir, config, {}, [], status="DRY_RUN", cps_summary=None)
        return run_dir

    declaration_index: DeclarationIndex
    if config.formal_tools_enabled:
        try:
            declaration_index = prepare_declaration_index(
                config,
                run_dir / ".private" / "formal_tools",
            )
        except OSError as exc:
            error = PreflightError(
                "formal declaration-index snapshot preparation failed"
            )
            logger.event(
                "preflight_failed",
                **_exception_artifact_fields(error, config),
            )
            _write_final(
                run_dir,
                config,
                {},
                [],
                status="PREFLIGHT_FAILED",
                cps_summary=None,
            )
            raise error from exc
    else:
        declaration_index = DeclarationIndex(None)

    if not mock_agent:
        try:
            # Keep the run-private index bound to the preflight when the
            # production function supports that keyword.  A few downstream
            # harnesses replace ``run_preflight`` with the historical
            # two-argument probe; preserving that narrow seam avoids turning a
            # diagnostic test double into a runtime failure.
            preflight_parameters = inspect.signature(run_preflight).parameters
            if "declaration_index" in preflight_parameters:
                run_preflight(
                    config,
                    run_dir,
                    declaration_index=declaration_index,
                )
            else:
                run_preflight(config, run_dir)
        except PreflightError as exc:
            logger.event(
                "preflight_failed",
                **_exception_artifact_fields(exc, config),
            )
            _write_final(run_dir, config, {}, [], status="PREFLIGHT_FAILED", cps_summary=None)
            raise

    # Preflight is an admission check, not experiment compute.  Bind both the
    # run deadline and every score event to this single post-preflight origin.
    horizon_started_monotonic = logger.start_horizon()
    manifest_snapshot["horizon_started_at"] = utc_now()
    (run_dir / "run_meta.json").write_text(
        json.dumps(manifest_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.event(
        "horizon_started",
        horizon_started_at=manifest_snapshot["horizon_started_at"],
        horizon_seconds=config.time_limit_seconds,
    )
    run_deadline = horizon_started_monotonic + config.time_limit_seconds

    store = CPSStore(run_dir / "cps.sqlite3") if config.uses_cps else None
    policy = make_policy(config.communication, store)
    if mock_agent:
        evaluator = MockEvaluator(prove_without_sorry=mock_proved)
    elif config.is_coding:
        evaluator = CodingEvaluator(
            config.lean_server_url,
            timeout_seconds=config.lean_timeout_seconds,
            max_lifecycle_seconds=config.lean_max_lifecycle_seconds,
            verification_profile=config.lean_verification_profile,
            judge_mode=config.lean_judge_mode,
        )
    else:
        evaluator = LeanEvaluator(
            config.lean_server_url,
            lean_env_id=config.lean_env_id,
            timeout_seconds=config.lean_timeout_seconds,
            max_lifecycle_seconds=config.lean_max_lifecycle_seconds,
            verification_profile=config.lean_verification_profile,
            judge_mode=config.lean_judge_mode,
        )
    if mock_agent and not callable(
        getattr(evaluator, "expected_task_contract_sha256", None)
    ):
        # Test suites may replace MockEvaluator with a minimal recording
        # double.  This marker is never set for a real experiment evaluator.
        setattr(evaluator, "_contextswarm_legacy_test_mock", True)
    evaluator_gate = threading.BoundedSemaphore(config.lean_max_concurrent_evaluations)
    formal_policy = FormalToolPolicy(
        enabled=config.formal_tools_enabled,
        surface_version=config.formal_tools_version,
        evaluate_calls_per_task=config.formal_tools_evaluate_calls_per_task,
        evaluate_backend_jobs_per_task=(
            config.formal_tools_evaluate_backend_jobs_per_task
        ),
        query_calls_per_task=config.formal_tools_query_calls_per_task,
        query_backend_probes_per_task=(
            config.formal_tools_query_backend_probes_per_task
        ),
        max_candidate_bytes=config.formal_tools_max_candidate_bytes,
        command_timeout_seconds=config.formal_tools_command_timeout_seconds,
        declaration_index=declaration_index,
    )
    judge_broker = JudgeBroker(
        evaluator,
        evaluator_gate,
        audit_path=run_dir / "judge_checks.jsonl",
        formal_policy=formal_policy,
        formal_audit_path=run_dir / "formal_tool_calls.jsonl",
    ).start()
    (run_dir / "judge_broker_policy.json").write_text(
        json.dumps(judge_broker.public_policy(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "formal_tools_contract.json").write_text(
        json.dumps(
            {
                "enabled": config.formal_tools_enabled,
                "authority": "diagnostic_only",
                "quota_scope": "task_across_all_sessions",
                "declaration_index": declaration_index.info.public_dict(),
                "surface": tool_surface_provenance(
                    config.formal_tools_version,
                    solver_extension_path=Path(__file__).with_name(
                        "pi_solver_tools.mjs"
                    ),
                ),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    pi_agent = PiAgent(config, trace_path=run_dir / "pi_events.jsonl")
    agent_results: list[AgentResult] = []
    attempt_verdicts: list[Verdict] = []
    verdicts: dict[str, Verdict] = {}
    frozen: dict[str, _FrozenCandidate] = {}
    run_failure: BaseException | None = None
    run_failure_fields: dict[str, str] | None = None
    try:
        if config.mode == "mono":
            mono_result, mono_verdicts = _run_mono(
                config,
                tasks,
                run_dir,
                logger,
                evaluator,
                pi_agent,
                mock_agent=mock_agent,
                deadline=run_deadline,
                evaluator_gate=evaluator_gate,
                judge_broker=judge_broker,
            )
            agent_results.append(mono_result)
            attempt_verdicts.extend(mono_verdicts.values())
        elif config.uses_cps:
            results = _run_elastic_cps(
                config,
                tasks,
                run_dir,
                logger,
                evaluator,
                pi_agent,
                policy,
                mock_agent=mock_agent,
                deadline=run_deadline,
                evaluator_gate=evaluator_gate,
                judge_broker=judge_broker,
                scheduler_result_sink=agent_results,
            )
            for result, verdict in results:
                agent_results.append(result)
                attempt_verdicts.append(verdict)
        else:
            results = _run_task_workers(
                config,
                tasks,
                run_dir,
                logger,
                evaluator,
                pi_agent,
                policy,
                mock_agent=mock_agent,
                deadline=run_deadline,
                evaluator_gate=evaluator_gate,
                judge_broker=judge_broker,
            )
            for result, verdict in results:
                agent_results.append(result)
                attempt_verdicts.append(verdict)
        logger.event(
            "horizon_closed",
            reason=(
                "deadline_elapsed"
                if time.monotonic() >= run_deadline
                else "solver_completed"
            ),
        )
        frozen = _freeze_closeout_candidates(config, tasks, run_dir, logger)
    except BaseException as exc:  # delay artifacts until broker capabilities are silent
        run_failure = exc
        run_failure_fields = _exception_artifact_fields(
            exc,
            config,
            traceback_bytes=4_000,
        )

    broker_failure: BaseException | None = None
    broker_failure_fields: dict[str, str] | None = None
    try:
        broker_state = judge_broker.close()
    except JudgeBrokerDrainError as exc:
        broker_failure = exc
        broker_failure_fields = _exception_artifact_fields(
            exc,
            config,
            traceback_bytes=4_000,
        )
        broker_state = dict(exc.state)
    except BaseException as exc:
        broker_failure = exc
        broker_failure_fields = _exception_artifact_fields(
            exc,
            config,
            traceback_bytes=4_000,
        )
        try:
            observed = judge_broker.drain_state()
        except Exception:
            observed = {
                "active_handlers": -1,
                "fifo_depth": -1,
                "remote_unsettled_jobs": -1,
            }
        broker_state = {"drained": False, **observed}

    formal_summary_failure: BaseException | None = None
    formal_summary_failure_fields: dict[str, str] | None = None
    try:
        (run_dir / "formal_tools_summary.json").write_text(
            json.dumps(
                judge_broker.formal_summary(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    except BaseException as exc:
        formal_summary_failure = exc
        formal_summary_failure_fields = _exception_artifact_fields(
            exc,
            config,
            traceback_bytes=4_000,
        )

    terminal_failure = run_failure or broker_failure or formal_summary_failure
    terminal_failure_fields = (
        run_failure_fields
        or broker_failure_fields
        or formal_summary_failure_fields
    )
    if terminal_failure is None:
        try:
            # The broker is already silent here: closeout cannot write CPS
            # feedback or influence any solver/allocation decision.
            verdicts = _run_closeout(
                config,
                tasks,
                frozen,
                logger,
                evaluator,
                evaluator_gate,
                reusable_verdicts=attempt_verdicts,
            )
        except BaseException as exc:
            terminal_failure = exc
            terminal_failure_fields = _exception_artifact_fields(
                exc,
                config,
                traceback_bytes=4_000,
            )

    # Closeout itself submits feedback-free Judge work after the capability
    # server is silent.  Re-observe the evaluator latch only after that phase;
    # otherwise an unknown closeout job would be hidden by a stale zero written
    # immediately after broker shutdown.
    try:
        final_broker_state = judge_broker.drain_state()
    except BaseException as exc:
        final_broker_state = {
            "active_handlers": -1,
            "fifo_depth": -1,
            "remote_unsettled_jobs": -1,
        }
        if terminal_failure is None:
            terminal_failure = exc
            terminal_failure_fields = _exception_artifact_fields(
                exc,
                config,
                traceback_bytes=4_000,
            )
    closeout_reported_remote = any(
        normalize_verdict_status(verdict.status)
        == "REMOTE_SETTLEMENT_UNCONFIRMED"
        for verdict in verdicts.values()
    )
    remote_unsettled_jobs = max(
        int(broker_state.get("remote_unsettled_jobs", -1)),
        int(final_broker_state.get("remote_unsettled_jobs", -1)),
    )
    if closeout_reported_remote and remote_unsettled_jobs == 0:
        # Compatibility fallback for narrow evaluator adapters.  Production
        # LeanEvaluator owns the exact count and always sets it before return.
        remote_unsettled_jobs = 1
    closeout_active_handlers = int(
        (broker_state if broker_failure is not None else final_broker_state).get(
            "active_handlers", -1
        )
    )
    closeout_fifo_depth = int(
        (broker_state if broker_failure is not None else final_broker_state).get(
            "fifo_depth", -1
        )
    )
    broker_closeout = {
        "schema_version": "contextswarm_judge_broker_closeout_v1",
        "drained": bool(
            broker_state.get("drained") is True
            and closeout_active_handlers == 0
            and closeout_fifo_depth == 0
            and remote_unsettled_jobs == 0
        ),
        "active_handlers": closeout_active_handlers,
        "fifo_depth": closeout_fifo_depth,
        "remote_unsettled_jobs": remote_unsettled_jobs,
    }
    if remote_unsettled_jobs > 0 and terminal_failure is None:
        terminal_failure = RemoteJudgeSettlementError(
            "remote Judge work did not provide a job-bound terminal receipt during closeout"
        )
        terminal_failure_fields = _exception_artifact_fields(
            terminal_failure,
            config,
            traceback_bytes=4_000,
        )

    closeout_artifact_failure: BaseException | None = None
    closeout_artifact_failure_fields: dict[str, str] | None = None
    try:
        (run_dir / "judge_broker_closeout.json").write_text(
            json.dumps(broker_closeout, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    except BaseException as exc:
        closeout_artifact_failure = exc
        closeout_artifact_failure_fields = _exception_artifact_fields(
            exc,
            config,
            traceback_bytes=4_000,
        )
        if terminal_failure is None:
            terminal_failure = exc
            terminal_failure_fields = closeout_artifact_failure_fields

    if closeout_artifact_failure is not None:
        logger.event(
            "broker_closeout_artifact_error",
            **(closeout_artifact_failure_fields or {}),
        )
    elif isinstance(broker_failure, JudgeBrokerDrainError) and (
        closeout_active_handlers != 0 or closeout_fifo_depth != 0
    ):
        logger.event("broker_drain_timeout", **broker_closeout)
    elif broker_failure is not None and remote_unsettled_jobs <= 0:
        logger.event("broker_close_error", **broker_closeout)
    elif remote_unsettled_jobs > 0:
        logger.event("remote_settlement_unconfirmed", **broker_closeout)
    else:
        logger.event("judge_broker_closed", **broker_closeout)
    if store is not None:
        try:
            store.export_events(run_dir / "communication_trace.jsonl")
        except BaseException as exc:
            if terminal_failure is None:
                terminal_failure = exc
                terminal_failure_fields = _exception_artifact_fields(
                    exc,
                    config,
                    traceback_bytes=4_000,
                )

    if terminal_failure is not None:
        logger.event("run_error", **(terminal_failure_fields or {}))
        health = _run_health(
            run_dir,
            config,
            verdicts,
            agent_results,
            attempt_verdicts,
            expected_task_count=len(tasks),
        )
        _write_final(
            run_dir,
            config,
            verdicts,
            agent_results,
            status="ERROR",
            cps_summary=store.summary() if store else None,
            health=health,
        )
        raise terminal_failure

    health = _run_health(
        run_dir,
        config,
        verdicts,
        agent_results,
        attempt_verdicts,
        expected_task_count=len(tasks),
    )
    status = "COMPLETED" if health["ok"] else "DEGRADED"
    _write_final(
        run_dir,
        config,
        verdicts,
        agent_results,
        status=status,
        cps_summary=store.summary() if store else None,
        health=health,
    )
    logger.event("run_finished", status=status, score=sum(v.score for v in verdicts.values()))
    return run_dir


def _run_mono(
    config: ExperimentConfig,
    tasks: list[Task],
    run_dir: Path,
    logger: RunLogger,
    evaluator: Any,
    pi_agent: PiAgent,
    *,
    mock_agent: bool,
    deadline: float,
    evaluator_gate: threading.BoundedSemaphore,
    judge_broker: JudgeBroker,
) -> tuple[AgentResult, dict[str, Verdict]]:
    worker_dir = run_dir / "workers" / "mono"
    worker_dir.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        _stage_task(task, worker_dir / "tasks" / task.slug, config=config)
    _write_mono_bundle(worker_dir, tasks)
    prompt = build_mono_prompt(
        tasks,
        workspace=str(worker_dir),
        communication_enabled=False,
        formal_tools_enabled=config.formal_tools_enabled,
    )
    early_lock = threading.RLock()
    early_proofs: dict[str, _EarlyProofCredit] = {}
    full_score_event = threading.Event()
    expected_contracts = {
        task.slug: _expected_task_contract(evaluator, task) for task in tasks
    }
    allow_mock_provenance = _allows_mock_provenance(evaluator)
    callback_failure = _CallbackFailureState()
    run_cancel_event = _AnyCancelEvent(
        callback_failure,
        _evaluator_remote_settlement_event(evaluator),
        full_score_event,
        reasons=("runner_failure", "remote_settlement_unconfirmed", "full_score"),
    )

    def admit_early_proof(
        task: Task,
        verdict: Verdict,
        snapshot: CandidateSnapshot,
    ) -> None:
        try:
            if not _has_authoritative_snapshot_provenance(
                verdict,
                snapshot,
                expected_task_contract_sha256=expected_contracts[task.slug],
                allow_mock_provenance=allow_mock_provenance,
            ):
                raise ValueError("broker proof failed runner snapshot provenance")
            with early_lock:
                if task.slug in early_proofs:
                    return
                verified = _candidate_path(worker_dir / "verified" / task.slug, task)
                _atomic_promote_source(snapshot.source, verified, snapshot.sha256)
                credit = _EarlyProofCredit(
                    verdict=verdict,
                    candidate_source=snapshot.source,
                    candidate_sha256=snapshot.sha256,
                    actor_id="mono",
                    episode=1,
                )
                # These writes are the prepare phase.  Do not expose ``credit``
                # to the runner until every fallible artifact write succeeds.
                logger.event(
                    "judge_proof_credited",
                    task_id=task.slug,
                    agent_id="mono",
                    episode=1,
                    candidate_sha256=snapshot.sha256,
                    task_contract_sha256=verdict.task_contract_sha256,
                    judge_job_id=verdict.judge_job_id,
                )
                logger.scoreboard(
                    verdict,
                    episode=1,
                    agent_id="mono",
                    source="judge_check",
                )
                early_proofs[task.slug] = credit
                if config.cancel_on_proved and len(early_proofs) == len(tasks):
                    full_score_event.set()
        except Exception:
            callback_failure.record()
            raise

    if mock_agent:
        result = _mock_result("mono", "bundle", 1)
    else:
        candidates = {
            task.slug: (task, _candidate_path(worker_dir / "tasks" / task.slug, task))
            for task in tasks
        }
        with judge_broker.session(
            actor_id="mono",
            workdir=worker_dir,
            candidates=candidates,
            deadline_monotonic=deadline,
            on_authoritative_verdict=admit_early_proof,
            cancel_event=run_cancel_event,
        ) as broker_env:
            result = pi_agent.run(
                task_id=f"{config.name}-bundle",
                actor_id="mono",
                episode=1,
                prompt=prompt,
                workdir=worker_dir,
                extra_env=broker_env,
                deadline_monotonic=deadline,
                cancel_event=run_cancel_event,
            )
    _raise_if_remote_settlement_unconfirmed(
        evaluator,
        on_failure=callback_failure.record,
    )
    callback_failure.raise_if_failed()
    logger.event("agent_finished", **result.as_dict())
    verdicts: dict[str, Verdict] = {}
    for task in tasks:
        candidate = _candidate_path(worker_dir / "tasks" / task.slug, task)
        with early_lock:
            credit = early_proofs.get(task.slug)
        if credit is not None:
            _atomic_promote_source(
                credit.candidate_source,
                candidate,
                credit.candidate_sha256,
            )
            verdict = credit.verdict
        else:
            verdict = _evaluate_candidate(
                evaluator,
                task,
                candidate,
                deadline,
                evaluator_gate,
                cancel_event=run_cancel_event,
            )
            _raise_if_remote_settlement_unconfirmed(
                evaluator,
                verdict,
                on_failure=callback_failure.record,
            )
            verdict = _within_horizon(verdict, deadline)
            verdict = _enforce_verdict_provenance(
                verdict,
                candidate,
                expected_task_contract_sha256=expected_contracts[task.slug],
                allow_mock_provenance=allow_mock_provenance,
            )
            logger.scoreboard(verdict, episode=1, agent_id="mono")
        verdicts[task.slug] = verdict
        logger.event(
            "evaluation_finished",
            **verdict.as_dict(),
            agent_id="mono",
            episode=1,
            source="judge_check" if credit is not None else "final_evaluation",
            scoreboard_recorded=credit is None,
        )
    _write_mono_bundle(worker_dir, tasks)
    return result, verdicts


def _run_elastic_cps(
    config: ExperimentConfig,
    tasks: list[Task],
    run_dir: Path,
    logger: RunLogger,
    evaluator: Any,
    pi_agent: PiAgent,
    policy: CommunicationPolicy,
    *,
    mock_agent: bool,
    deadline: float,
    evaluator_gate: threading.BoundedSemaphore,
    judge_broker: JudgeBroker,
    scheduler_result_sink: list[AgentResult],
) -> list[tuple[AgentResult, Verdict]]:
    """Run one fixed CPS computation substrate with selectable slot allocation."""

    run_started_monotonic = deadline - config.time_limit_seconds
    states: dict[str, _ElasticTaskState] = {}
    expected_contracts = {
        task.slug: _expected_task_contract(evaluator, task) for task in tasks
    }
    allow_mock_provenance = _allows_mock_provenance(evaluator)
    for task in tasks:
        task_root = run_dir / "workers" / task.slug
        state = _ElasticTaskState(
            task=task,
            task_root=task_root,
            last_assignment_at=run_started_monotonic,
            last_progress_at=run_started_monotonic,
        )
        best_dir = task_root / "best"
        best_dir.mkdir(parents=True, exist_ok=True)
        best_path = _candidate_path(best_dir, task)
        if not best_path.exists():
            best_path.write_text(task.baseline_code, encoding="utf-8")
        state.best_candidate = best_path
        states[task.slug] = state

    task_order = [task.slug for task in tasks]
    scheduler = ElasticScheduler(
        task_order,
        max_parallel=config.max_parallel,
        initial_agents=config.initial_agents_per_task,
        horizon=max(0.0, deadline - time.monotonic()),
        assignment_policy=config.assignment_policy,
    )
    assignments_path = run_dir / "elastic_assignments.jsonl"
    decisions_path = run_dir / "allocation_decisions.jsonl"
    decisions_path.write_text("", encoding="utf-8")
    audit_path = run_dir / "allocation_audit.jsonl"
    if config.allocation.policy == "trace_state":
        audit_path.write_text("", encoding="utf-8")
    roster_path = run_dir / "actors.json"
    roster_lock = threading.RLock()
    allocation_lock = threading.RLock()
    roster: list[dict[str, Any]] = []
    roster_path.write_text("[]\n", encoding="utf-8")
    horizon_epoch_ms = int(
        (time.time() + max(0.0, deadline - time.monotonic())) * 1_000
    )
    jobs: Queue[AgentAssignment] = Queue()
    results: list[tuple[AgentResult, Verdict]] = []
    results_lock = threading.RLock()
    scheduler_results_lock = threading.RLock()
    evaluation_backlog_limit = max(
        2,
        config.max_parallel + config.lean_max_concurrent_evaluations,
    )
    evaluation_backlog_gate = threading.BoundedSemaphore(
        evaluation_backlog_limit
    )
    decision_index = 0
    initial_assignment_count = 0
    adaptive_assignments = 0
    callback_failure = _CallbackFailureState()
    full_score_event = threading.Event()
    run_cancel_event = _AnyCancelEvent(
        callback_failure,
        _evaluator_remote_settlement_event(evaluator),
        full_score_event,
        reasons=("runner_failure", "remote_settlement_unconfirmed", "full_score"),
    )

    assert policy.store is not None
    store = policy.store

    def record_run_failure() -> None:
        callback_failure.record()
        # An admission or worker failure invalidates the whole arm.  Stop every
        # solver promptly; worker-loop closeout will raise the stable fatal
        # after all broker sessions have been revoked/drained.
        for task_state in states.values():
            task_state.cancel_event.set()

    def invoke_scheduler_agent(
        snapshot: TaskProgressSnapshot,
        prompt: str,
        index: int,
    ) -> AgentResult:
        actor_id = f"allocation-scheduler-{index}"
        if mock_agent:
            result = _mock_result(actor_id, "__allocation__", index)
            selected = snapshot.eligible_task_ids[0] if snapshot.eligible_task_ids else ""
            result.output_tail = json.dumps(
                {
                    "task_id": selected,
                    "reason": "mock scheduler decision",
                    "evidence_piece_ids": [],
                },
                sort_keys=True,
            )
        else:
            workdir = run_dir / "allocation_scheduler" / f"decision-{index:06d}"
            workdir.mkdir(parents=True, exist_ok=True)
            policy_deadline = time.monotonic() + config.allocation.agent_timeout_seconds
            scheduler_deadline = min(
                deadline,
                policy_deadline,
            )
            run_horizon_is_limiter = deadline <= policy_deadline
            result = pi_agent.run(
                task_id="__allocation__",
                actor_id=actor_id,
                episode=index,
                prompt=prompt,
                workdir=workdir,
                deadline_monotonic=scheduler_deadline,
                isolated=True,
                cancel_event=run_cancel_event,
            )
            result.run_horizon_reached = bool(
                result.timed_out
                and run_horizon_is_limiter
                and time.monotonic() >= deadline
            )
        result.decision_index = index
        with scheduler_results_lock:
            scheduler_result_sink.append(result)
        logger.event("allocation_scheduler_finished", **result.as_dict())
        return result

    def invoke_core_scheduler_agent(
        snapshot: AllocationStateSnapshot,
        prompt: str,
    ) -> LLMSchedulerResponse:
        started = time.monotonic()
        actor_id = f"allocation-scheduler-{snapshot.decision_index}"
        if mock_agent:
            result = _mock_result(actor_id, "__allocation__", snapshot.decision_index)
            selected = snapshot.eligible_task_ids[0] if snapshot.eligible_task_ids else ""
            result.output_tail = json.dumps(
                {
                    "decision_id": snapshot.decision_id,
                    "task_id": selected,
                    "reason": "mock scheduler decision",
                    "trace_reference_ids": [],
                },
                sort_keys=True,
            )
        else:
            workdir = run_dir / "allocation_scheduler" / f"decision-{snapshot.decision_index:06d}"
            workdir.mkdir(parents=True, exist_ok=True)
            result = pi_agent.run(
                task_id="__allocation__",
                actor_id=actor_id,
                episode=snapshot.decision_index,
                prompt=prompt,
                workdir=workdir,
                deadline_monotonic=min(
                    deadline,
                    time.monotonic() + config.allocation.agent_timeout_seconds,
                ),
                isolated=True,
                cancel_event=run_cancel_event,
            )
        result.decision_index = snapshot.decision_index
        with scheduler_results_lock:
            scheduler_result_sink.append(result)
        logger.event("allocation_scheduler_finished", **result.as_dict())
        latency = max(0.0, time.monotonic() - started)
        return LLMSchedulerResponse(
            output=result.output_tail,
            returncode=result.returncode,
            timed_out=result.timed_out,
            latency_seconds=latency,
            occupied_slot_seconds=latency,
        )

    core_decisions: dict[int, tuple[AllocationStateSnapshot, Any]] = {}

    class _CoreAllocatorAdapter:
        def __init__(self, core_policy: Any) -> None:
            self.core_policy = core_policy
            self.decisions: list[Any] = []

        def choose(self, legacy_snapshot: TaskProgressSnapshot) -> AllocationDecision:
            core_snapshot = _core_snapshot_from_legacy(legacy_snapshot, config)
            core_decision = self.core_policy.choose(core_snapshot)
            self.decisions.append(core_decision)
            core_decisions[core_decision.decision_index] = (core_snapshot, core_decision)
            return _legacy_decision_from_core(core_decision)

        def fallback(
            self,
            legacy_snapshot: TaskProgressSnapshot,
            reason: str,
            *,
            prior: AllocationDecision | None = None,
        ) -> AllocationDecision:
            replacement = self.choose(legacy_snapshot)
            decision = prior or replacement
            decision.selected_task_id = replacement.selected_task_id
            decision.fallback = True
            decision.fallback_reason = _combine_fallback_reasons(
                decision.fallback_reason, reason
            )
            return decision

        def summary(self) -> dict[str, Any]:
            latencies = [
                float(item.scheduler_cost.latency_seconds)
                for item in self.decisions
                if item.scheduler_cost is not None
            ]
            return {
                "schema_version": "contextswarm_allocation_summary_v2",
                "policy": config.allocation.policy,
                "decision_count": len(self.decisions),
                "fallback_count": sum(item.fallback for item in self.decisions),
                "agent_calls": sum(item.scheduler_cost is not None for item in self.decisions),
                "total_latency_seconds": round(sum(latencies), 6),
                "max_latency_seconds": round(max(latencies, default=0.0), 6),
                "scheduler_cost": {
                    "calls": sum(item.scheduler_cost is not None for item in self.decisions),
                    "reserved_slot_seconds": round(
                        sum(
                            float(item.scheduler_cost.occupied_slot_seconds or 0.0)
                            for item in self.decisions
                            if item.scheduler_cost is not None
                        ),
                        6,
                    ),
                },
            }

    if config.allocation.policy in _FIGURE4_POLICIES:
        core_policy = create_allocation_policy(
            config.allocation.policy,
            task_weights=TaskScoreWeights.from_mapping(config.allocation.task_state),
            trace_weights=TraceScoreWeights.from_mapping(config.allocation.trace_state),
            llm_invoker=(
                invoke_core_scheduler_agent
                if config.allocation.policy == "llm_scheduler"
                else None
            ),
        )
        allocator: Any = _CoreAllocatorAdapter(core_policy)
    elif config.allocation.policy == "uniform":
        allocator: Any = UniformAllocationPolicy(task_order)
    elif config.allocation.policy == "formula":
        allocator = FormulaAllocationPolicy(task_order, config.allocation.formula)
    else:
        allocator = AgentAllocationPolicy(task_order, invoke_scheduler_agent)

    def build_snapshot(index: int) -> TaskProgressSnapshot:
        now_mono = time.monotonic()
        if config.allocation.policy in _FIGURE4_POLICIES:
            # The ordinary Figure 4 state is built without consulting the CPS
            # store. Trace-State/LLM receive projection data through the
            # selector bridge, never through Task-State recency or counts.
            cps = {
                task_id: {
                    "latest_created_at": "",
                    "piece_count": 0,
                    "validation_piece_count": 0,
                    "strategy_piece_count": 0,
                    "duplicate_piece_count": 0,
                    "recent_pieces": [],
                }
                for task_id in task_order
            }
        else:
            cps = store.progress_snapshot(
                task_order,
                recent_limit=config.allocation.piece_limit_per_task,
                body_chars=config.allocation.piece_body_chars,
            )
        scheduler_unsolved = set(scheduler.unsolved_tasks)
        progress_rows: list[TaskProgress] = []
        for task_id in task_order:
            state = states[task_id]
            stats = cps[task_id]
            with state.lock:
                solved = state.solved
                retired = state.retired
                attempts = state.attempts
                completed_attempts = state.completed_attempts
                best = state.best_verdict
                last_status = state.last_verdict_status
                last_feedback = state.last_feedback
                failures = state.consecutive_failures
                assignment_age = max(0.0, now_mono - state.last_assignment_at)
                progress_age = max(0.0, now_mono - state.last_progress_at)
            piece_age = _seconds_since_cps_timestamp(str(stats["latest_created_at"]))
            if config.allocation.policy not in _FIGURE4_POLICIES and piece_age is not None:
                progress_age = min(progress_age, piece_age)
            capped = config.max_attempts_per_task > 0 and attempts >= config.max_attempts_per_task
            eligible = (
                task_id in scheduler_unsolved
                and not solved
                and not retired
                and not capped
            )
            pieces = tuple(EvidencePiece(**item) for item in stats["recent_pieces"])
            progress_rows.append(
                TaskProgress(
                    task_id=task_id,
                    eligible=eligible,
                    solved=solved,
                    active_agents=len(scheduler.active(task_id)),
                    attempts=attempts,
                    completed_attempts=completed_attempts,
                    best_status=(
                        normalize_verdict_status(best.status) if best is not None else "NONE"
                    ),
                    best_score=float(best.score) if best is not None else 0.0,
                    last_verdict_status=normalize_verdict_status(last_status),
                    last_feedback=last_feedback,
                    consecutive_failures=failures,
                    seconds_since_last_assignment=assignment_age,
                    seconds_since_progress=progress_age,
                    piece_count=int(stats["piece_count"]),
                    validation_piece_count=int(stats["validation_piece_count"]),
                    strategy_piece_count=int(stats["strategy_piece_count"]),
                    duplicate_piece_count=int(stats["duplicate_piece_count"]),
                    recent_pieces=pieces,
                )
            )
        return TaskProgressSnapshot(
            decision_index=index,
            elapsed_seconds=max(0.0, now_mono - run_started_monotonic),
            remaining_seconds=max(0.0, deadline - now_mono),
            free_slots=scheduler.remaining_slots,
            tasks=tuple(progress_rows),
        )

    def record_assignment(
        assignment: AgentAssignment,
        *,
        phase: str,
        decision: AllocationDecision | None = None,
    ) -> None:
        row = {
            "at": utc_now(),
            "event": "agent_assigned",
            "task_id": assignment.task_id,
            "agent_id": assignment.agent_id,
            "generation": assignment.generation,
            "admitted_at": assignment.admitted_at,
            "allocation_phase": phase,
            "allocation_policy": config.allocation.policy,
            "decision_index": decision.decision_index if decision is not None else None,
        }
        with assignments_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        with roster_lock:
            roster.append(
                {
                    "actor_id": assignment.agent_id,
                    "task_id": assignment.task_id,
                    "episode": assignment.generation,
                }
            )
            temporary = roster_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(roster, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(roster_path)
        logger.event(
            "agent_assigned",
            task_id=assignment.task_id,
            agent_id=assignment.agent_id,
            episode=assignment.generation,
            active_slots=scheduler.active_slots,
            allocation_phase=phase,
            allocation_policy=config.allocation.policy,
            decision_index=decision.decision_index if decision is not None else None,
        )

    def record_decision(
        decision: AllocationDecision,
        snapshot: TaskProgressSnapshot,
        assignment: AgentAssignment | None,
        *,
        execution_snapshot: TaskProgressSnapshot | None = None,
        disposition: str | None = None,
    ) -> None:
        if disposition is None:
            if assignment is not None:
                disposition = "assigned"
            elif time.monotonic() >= deadline:
                disposition = "not_admitted_horizon"
            else:
                disposition = "not_admitted_ineligible"
        row = {
            "at": utc_now(),
            **decision.as_dict(snapshot=snapshot),
            "assigned_agent_id": assignment.agent_id if assignment is not None else None,
            "assigned_generation": assignment.generation if assignment is not None else None,
            "disposition": disposition,
        }
        core_record = core_decisions.get(decision.decision_index)
        if core_record is not None:
            core_snapshot, core_decision = core_record
            row.update(
                {
                    "schema_version": "contextswarm_allocation_decision_v2",
                    "decision_id": core_decision.decision_id,
                    "state_id": core_decision.state_id,
                    "eligible_task_ids": list(core_snapshot.eligible_task_ids),
                    "task_only_scores": dict(core_decision.task_scores),
                    "trace_increments": dict(core_decision.trace_increments),
                    "total_scores": dict(core_decision.scores),
                    "allocation_config_sha256": core_snapshot.allocation_config_sha256,
                    "scheduler_cost": (
                        core_decision.scheduler_cost.public_dict()
                        if core_decision.scheduler_cost is not None
                        else None
                    ),
                }
            )
        if execution_snapshot is not None:
            row["execution_snapshot"] = execution_snapshot.as_dict()
        with decisions_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        logger.event(
            "allocation_decision",
            decision_index=decision.decision_index,
            allocation_policy=decision.policy,
            requested_task_id=decision.requested_task_id,
            selected_task_id=decision.selected_task_id,
            fallback=decision.fallback,
            fallback_reason=decision.fallback_reason,
            latency_seconds=decision.latency_seconds,
            agent_returncode=decision.agent_returncode,
            agent_timed_out=decision.agent_timed_out,
            agent_cancelled=decision.agent_cancelled,
            agent_result_valid=decision.agent_result_valid,
            agent_id=decision.agent_id,
            agent_task_id=decision.agent_task_id,
            agent_episode=decision.agent_episode,
            agent_run_horizon_reached=decision.agent_run_horizon_reached,
            assigned_agent_id=assignment.agent_id if assignment is not None else None,
            disposition=disposition,
        )

    def retire_exhausted_tasks() -> None:
        if config.max_attempts_per_task <= 0:
            return
        for task_id, state in states.items():
            with state.lock:
                exhausted = (
                    not state.solved
                    and not state.retired
                    and state.attempts >= config.max_attempts_per_task
                )
                if exhausted:
                    state.retired = True
            if exhausted:
                scheduler.retire_task(
                    task_id,
                    reason="attempt_budget_exhausted",
                )
                logger.event(
                    "task_attempt_budget_exhausted",
                    task_id=task_id,
                    max_attempts=config.max_attempts_per_task,
                )

    def accept_assignment(
        assignment: AgentAssignment,
        *,
        phase: str,
        decision: AllocationDecision | None = None,
    ) -> AgentAssignment:
        nonlocal initial_assignment_count
        state = states[assignment.task_id]
        with state.lock:
            state.attempts += 1
            state.last_assignment_at = assignment.admitted_at
        record_assignment(assignment, phase=phase, decision=decision)
        if phase == "initial":
            initial_assignment_count += 1
        return assignment

    def claim_next(*, initial_fill: bool = False) -> AgentAssignment | None:
        """Claim one lease; only post-initial claims invoke the treatment policy."""
        nonlocal decision_index
        nonlocal adaptive_assignments
        allocation_lock.acquire()
        try:
            while time.monotonic() < deadline:
                if _evaluator_remote_unsettled_jobs(evaluator) > 0:
                    record_run_failure()
                    return None
                if callback_failure.failed:
                    return None
                retire_exhausted_tasks()
                if initial_fill or scheduler.has_pending_initial:
                    assignment = scheduler.next_assignment()
                    if assignment is None:
                        return None
                    state = states[assignment.task_id]
                    with state.lock:
                        unavailable = state.solved or state.retired
                        solved = state.solved
                    if unavailable:
                        scheduler.finish(assignment, solved=solved)
                        continue
                    return accept_assignment(assignment, phase="initial")

                decision_index += 1
                snapshot = build_snapshot(decision_index)
                if not snapshot.eligible_task_ids:
                    return None
                scheduler_reservation = None
                if config.allocation.policy == "llm_scheduler":
                    scheduler_reservation = scheduler.acquire_reservation(
                        slots=1,
                        purpose=f"llm_scheduler_decision_{decision_index}",
                    )
                    if scheduler_reservation is None:
                        return None
                if config.allocation.policy in {"agent", "llm_scheduler"}:
                    # A released solver slot can run its own read-only scheduler
                    # call.  Release only the orchestration lock while the model
                    # reasons so simultaneous completions keep all compute slots
                    # occupied; index/snapshot and final admission remain atomic.
                    allocation_lock.release()
                    try:
                        decision = allocator.choose(snapshot)
                    except BaseException:
                        if scheduler_reservation is not None:
                            scheduler.release_reservation(
                                scheduler_reservation,
                                reason="scheduler_exception",
                            )
                        raise
                    finally:
                        allocation_lock.acquire()
                else:
                    decision = allocator.choose(snapshot)
                # Another concurrent scheduler call may have consumed the
                # selected task's final attempt while this decision reasoned.
                retire_exhausted_tasks()
                assignment = None
                execution_snapshot: TaskProgressSnapshot | None = None
                if decision.agent_run_horizon_reached:
                    if scheduler_reservation is not None:
                        scheduler.release_reservation(
                            scheduler_reservation,
                            reason="horizon_reached",
                        )
                    record_decision(
                        decision,
                        snapshot,
                        None,
                        disposition="not_admitted_horizon",
                    )
                    return None
                valid_agent_decision = (
                    config.allocation.policy == "agent"
                    and decision.agent_result_valid is True
                    and not decision.fallback
                )
                if (
                    valid_agent_decision
                    and time.monotonic() < deadline
                    and decision.selected_task_id
                ):
                    # Revalidate the selected task itself before admission.
                    # Pure passage of time and changes to other tasks do not
                    # invalidate the causal choice.  Keep the invocation's
                    # reserved index even if peer decisions advanced the
                    # global counter while this scheduler agent reasoned.
                    execution_snapshot = build_snapshot(snapshot.decision_index)
                    original_fingerprint = snapshot.task_causal_fingerprint(
                        decision.selected_task_id
                    )
                    execution_fingerprint = execution_snapshot.task_causal_fingerprint(
                        decision.selected_task_id
                    )
                    if original_fingerprint != execution_fingerprint:
                        record_decision(
                            decision,
                            snapshot,
                            None,
                            execution_snapshot=execution_snapshot,
                            disposition="not_admitted_stale",
                        )
                        if (
                            execution_snapshot.eligible_task_ids
                            and scheduler.remaining_slots > 0
                        ):
                            continue
                        return None
                    assignment = scheduler.next_assignment_for(decision.selected_task_id)
                elif time.monotonic() < deadline and decision.selected_task_id:
                    if scheduler_reservation is not None:
                        assignment = scheduler.admit_reserved(
                            scheduler_reservation,
                            decision.selected_task_id,
                        )
                    else:
                        assignment = scheduler.next_assignment_for(decision.selected_task_id)
                if assignment is None and time.monotonic() < deadline:
                    if scheduler.horizon_reached:
                        record_decision(
                            decision,
                            snapshot,
                            None,
                            execution_snapshot=execution_snapshot,
                            disposition="not_admitted_horizon",
                        )
                        return None
                    if execution_snapshot is None:
                        execution_snapshot = build_snapshot(snapshot.decision_index)
                    if valid_agent_decision:
                        record_decision(
                            decision,
                            snapshot,
                            None,
                            execution_snapshot=execution_snapshot,
                            disposition="not_admitted_stale",
                        )
                        if (
                            execution_snapshot.eligible_task_ids
                            and scheduler.remaining_slots > 0
                        ):
                            continue
                        return None
                    if not execution_snapshot.eligible_task_ids:
                        # A deterministic decision can legitimately lose its
                        # final target to a peer between snapshot and admit.
                        record_decision(
                            decision,
                            snapshot,
                            None,
                            execution_snapshot=execution_snapshot,
                            disposition="not_admitted_stale",
                        )
                        return None
                    if execution_snapshot.eligible_task_ids:
                        decision = allocator.fallback(
                            execution_snapshot,
                            "selected task became ineligible before admission",
                            prior=decision,
                        )
                        if decision.selected_task_id:
                            assignment = scheduler.next_assignment_for(decision.selected_task_id)
                if assignment is None:
                    if scheduler_reservation is not None:
                        scheduler.release_reservation(
                            scheduler_reservation,
                            reason="decision_not_admitted",
                        )
                    record_decision(
                        decision,
                        snapshot,
                        None,
                        execution_snapshot=execution_snapshot,
                    )
                    return None
                adaptive_assignments += 1
                accept_assignment(assignment, phase="adaptive", decision=decision)
                record_decision(
                    decision,
                    snapshot,
                    assignment,
                    execution_snapshot=execution_snapshot,
                )
                if config.allocation.policy == "trace_state":
                    core_record = core_decisions.get(decision.decision_index)
                    if core_record is not None:
                        core_snapshot, core_decision = core_record
                        task_counterfactual = create_allocation_policy(
                            "task_state",
                            task_weights=TaskScoreWeights.from_mapping(
                                config.allocation.task_state
                            ),
                        ).choose(core_snapshot)
                        append_allocation_audit(
                            audit_path,
                            AllocationAuditRecord.create(
                                state_id=core_snapshot.state_id,
                                decision_id=core_snapshot.decision_id,
                                eligible_task_ids=core_snapshot.eligible_task_ids,
                                allocation_config_sha256=core_snapshot.allocation_config_sha256,
                                task_only_scores=core_decision.task_scores,
                                trace_increments=core_decision.trace_increments,
                                trace_total_scores=core_decision.scores,
                                allocation_before={
                                    task.task_id: task.active_allocations
                                    for task in core_snapshot.tasks
                                },
                                trace_state_selected_task_id=core_decision.selected_task_id,
                                task_state_selected_task_id=task_counterfactual.selected_task_id,
                                admitted_task_id=assignment.task_id,
                                fallback_reason=decision.fallback_reason,
                                active_slots_before=core_snapshot.active_solver_slots,
                                active_slots_after=core_snapshot.active_solver_slots + 1,
                                free_slots_before=core_snapshot.free_slots,
                                free_slots_after=max(0, core_snapshot.free_slots - 1),
                                scheduler_reserved_slots_before=core_snapshot.scheduler_reserved_slots,
                                scheduler_reserved_slots_after=core_snapshot.scheduler_reserved_slots,
                                total_capacity=core_snapshot.total_capacity,
                            ),
                        )
                return assignment
            return None
        finally:
            allocation_lock.release()

    def prepare_workspace(state: _ElasticTaskState, assignment: AgentAssignment) -> tuple[Path, Path]:
        workdir = state.task_root / "agents" / assignment.agent_id
        _stage_task(state.task, workdir, config=config)
        assert state.best_candidate is not None
        with state.lock:
            shutil.copy2(state.best_candidate, _candidate_path(workdir, state.task))
        return workdir, state.best_candidate

    def execute_assignment(assignment: AgentAssignment) -> Any:
        """Yield once when solver capacity is releasable, then settle Judge work."""
        callback_failure.raise_if_failed()
        state = states[assignment.task_id]
        workdir, best_path = prepare_workspace(state, assignment)
        actor = assignment.agent_id
        task = state.task
        candidate_path = _candidate_path(workdir, task)

        def admit_task_proof(
            verdict: Verdict,
            *,
            feedback: str,
            source: str,
            candidate_source: str | None = None,
            complete_attempt: bool,
        ) -> bool:
            """Commit the first proof for this task after all fallible I/O.

            Judge callbacks and final evaluator results share this exact
            critical section.  Consequently only the first ``False -> True``
            solved transition can promote a proof, publish validation, append
            a positive scoreboard row, and cancel peers.
            """

            with allocation_lock:
                with state.lock:
                    if state.solved:
                        return False
                    prior_priority = _verdict_priority(state.best_verdict)
                    if candidate_source is None:
                        promoted_hash = _atomic_promote_candidate(
                            candidate_path,
                            best_path,
                            str(verdict.candidate_sha256),
                        )
                        credit = None
                    else:
                        promoted_hash = _atomic_promote_source(
                            candidate_source,
                            best_path,
                            str(verdict.candidate_sha256),
                        )
                        credit = _EarlyProofCredit(
                            verdict=verdict,
                            candidate_source=candidate_source,
                            candidate_sha256=promoted_hash,
                            actor_id=actor,
                            episode=assignment.generation,
                        )

                    # Prepare phase: every operation below may fail.  Keep the
                    # task unsolved and the credit invisible until all of them
                    # have completed.  A callback-path exception also trips the
                    # run-wide fatal latch in ``admit_early_proof``.
                    _publish_authoritative_validation(
                        policy,
                        task.slug,
                        actor,
                        label=f"attempt {assignment.generation}",
                        verdict=verdict,
                        feedback=feedback,
                        deadline_epoch_ms=horizon_epoch_ms,
                    )
                    logger.event(
                        "best_candidate_promoted",
                        task_id=task.slug,
                        agent_id=actor,
                        episode=assignment.generation,
                        source=source,
                        status=verdict.status,
                        score=verdict.score,
                        candidate_sha256=promoted_hash,
                        task_contract_sha256=verdict.task_contract_sha256,
                        judge_job_id=verdict.judge_job_id,
                        prior_priority=list(prior_priority),
                        new_priority=list(_verdict_priority(verdict)),
                    )
                    if credit is not None:
                        logger.event(
                            "judge_proof_credited",
                            task_id=task.slug,
                            agent_id=actor,
                            episode=assignment.generation,
                            candidate_sha256=promoted_hash,
                            task_contract_sha256=verdict.task_contract_sha256,
                            judge_job_id=verdict.judge_job_id,
                        )
                    logger.scoreboard(
                        verdict,
                        episode=assignment.generation,
                        agent_id=actor,
                        source=source,
                    )

                    # Commit phase: these are runner-owned in-memory state
                    # transitions only.  There must be no fallible artifact I/O
                    # after the credit becomes visible.
                    scheduler.task_solved(task.slug)
                    if credit is not None:
                        state.early_proofs[actor] = credit
                    state.best_verdict = verdict
                    state.solved = True
                    state.last_verdict_status = verdict.status
                    state.last_feedback = feedback
                    state.last_progress_at = time.monotonic()
                    state.consecutive_failures = 0
                    if complete_attempt:
                        state.completed_attempts += 1
                    if config.cancel_on_proved:
                        state.cancel_event.set()
                    if config.cancel_on_proved and all(
                        current_state.solved for current_state in states.values()
                    ):
                        full_score_event.set()
                    return True

        candidate_path = _candidate_path(workdir, task)

        def admit_early_proof(
            proved_task: Task,
            verdict: Verdict,
            snapshot: CandidateSnapshot,
        ) -> None:
            try:
                if proved_task.slug != task.slug or not _has_authoritative_snapshot_provenance(
                    verdict,
                    snapshot,
                    expected_task_contract_sha256=expected_contracts[task.slug],
                    allow_mock_provenance=allow_mock_provenance,
                ):
                    raise ValueError("broker proof failed runner snapshot provenance")
                admit_task_proof(
                    verdict,
                    feedback=_allocation_feedback(verdict),
                    source="judge_check",
                    candidate_source=snapshot.source,
                    complete_attempt=False,
                )
            except Exception:
                record_run_failure()
                raise

        if state.solved:
            with state.lock:
                state.completed_attempts += 1
            result = _mock_result(actor, task.slug, assignment.generation)
            verdict = Verdict(task.slug, "CANCELLED", 0.0, 0.0, {"reason": "task_already_solved"})
            logger.event("agent_finished", **result.as_dict())
            logger.scoreboard(verdict, episode=assignment.generation, agent_id=actor)
            logger.event(
                "evaluation_finished",
                **verdict.as_dict(),
                agent_id=actor,
                episode=assignment.generation,
                phase="solver",
                eligible_for_handoff=False,
            )
            yield result
            return result, verdict, False

        digest = policy.digest(task.slug, actor, query=task.theorem_name)
        prompt = build_task_prompt(
            task,
            task_workspace=str(workdir),
            agent_id=actor,
            episode=assignment.generation,
            communication_enabled=policy.enabled,
            formal_tools_enabled=config.formal_tools_enabled,
            digest=digest,
        )
        prompt += (
            "\n\nElastic CPS handoff:\n"
            f"The runner has pre-seeded {task.candidate_filename} with the strongest usable candidate "
            f"from earlier assignments on this task. Keep your candidate in {task.candidate_filename}; "
            "the runner will merge the strongest verified candidate."
        )

        assignment_cancel_event = _AnyCancelEvent(
            run_cancel_event,
            state.cancel_event,
            reasons=("runner_failure", "task_solved_by_peer"),
        )
        if mock_agent:
            result = _mock_result(actor, task.slug, assignment.generation)
        else:
            with judge_broker.session(
                actor_id=actor,
                workdir=workdir,
                candidates={task.slug: (task, candidate_path)},
                deadline_monotonic=deadline,
                cps_store=store,
                communication=config.communication,
                roster_path=roster_path,
                on_authoritative_verdict=admit_early_proof,
                cancel_event=assignment_cancel_event,
            ) as broker_env:
                result = pi_agent.run(
                    task_id=task.slug,
                    actor_id=actor,
                    episode=assignment.generation,
                    prompt=prompt,
                    workdir=workdir,
                    extra_env=broker_env,
                    deadline_monotonic=deadline,
                    cancel_event=assignment_cancel_event,
                )
        _raise_if_remote_settlement_unconfirmed(
            evaluator,
            on_failure=record_run_failure,
        )
        callback_failure.raise_if_failed()
        logger.event("agent_finished", **result.as_dict())

        # Everything below this point is evaluator/commit work.  The queue
        # worker advances the generator only on the bounded evaluator pool, so
        # a slow Judge never occupies the released Pi solver slot.
        yield result

        with state.lock:
            early_credit = state.early_proofs.get(actor)
        if early_credit is not None:
            verdict = early_credit.verdict
            logger.event(
                "evaluation_finished",
                **verdict.as_dict(),
                agent_id=actor,
                episode=assignment.generation,
                source="judge_check",
                scoreboard_recorded=False,
                phase="solver",
                eligible_for_handoff=True,
            )
            with allocation_lock:
                with state.lock:
                    state.completed_attempts += 1
            return result, verdict, True

        with state.lock:
            already_solved = state.solved
        if result.cancelled or already_solved:
            verdict = Verdict(
                task.slug,
                "CANCELLED",
                0.0,
                0.0,
                {"reason": "task_solved_by_peer"},
            )
        else:
            verdict = _evaluate_candidate(
                evaluator,
                task,
                candidate_path,
                deadline,
                evaluator_gate,
                cancel_event=assignment_cancel_event,
            )
            _raise_if_remote_settlement_unconfirmed(
                evaluator,
                verdict,
                on_failure=record_run_failure,
            )
            verdict = _within_horizon(verdict, deadline)
            verdict = _enforce_verdict_provenance(
                verdict,
                candidate_path,
                expected_task_contract_sha256=expected_contracts[task.slug],
                allow_mock_provenance=allow_mock_provenance,
            )

        candidate_is_usable = _has_authoritative_provenance(
            verdict,
            candidate_path,
            expected_task_contract_sha256=expected_contracts[task.slug],
            allow_mock_provenance=allow_mock_provenance,
        )
        feedback = _allocation_feedback(verdict)
        proof_candidate = verdict.score >= 1.0 and candidate_is_usable
        if proof_candidate:
            admitted = admit_task_proof(
                verdict,
                feedback=feedback,
                source="final_evaluation",
                complete_attempt=True,
            )
            if admitted:
                logger.event(
                    "evaluation_finished",
                    **verdict.as_dict(),
                    agent_id=actor,
                    episode=assignment.generation,
                    source="final_evaluation",
                    scoreboard_recorded=True,
                    phase="solver",
                    eligible_for_handoff=True,
                )
                return result, verdict, True

            # Another callback/final evaluator won the same task-level commit
            # while this candidate was being evaluated.  Preserve attempt
            # closeout without emitting a second positive score or validation.
            with state.lock:
                state.completed_attempts += 1
            verdict = Verdict(
                task.slug,
                "CANCELLED",
                0.0,
                verdict.elapsed_seconds,
                {
                    "reason": "proof_superseded_by_peer",
                    "superseded_status": verdict.status,
                },
                candidate_sha256=verdict.candidate_sha256,
                task_contract_sha256=verdict.task_contract_sha256,
                judge_job_id=verdict.judge_job_id,
                cache_reused=verdict.cache_reused,
            )
            logger.scoreboard(verdict, episode=assignment.generation, agent_id=actor)
            logger.event(
                "evaluation_finished",
                **verdict.as_dict(),
                agent_id=actor,
                episode=assignment.generation,
                source="final_evaluation",
                scoreboard_recorded=True,
                phase="solver",
                eligible_for_handoff=False,
            )
            return result, verdict, False

        superseded = False
        with allocation_lock:
            with state.lock:
                state.completed_attempts += 1
                if state.solved:
                    superseded = True
                else:
                    prior_priority = _verdict_priority(state.best_verdict)
                    improved = (
                        candidate_is_usable
                        and _verdict_priority(verdict) > prior_priority
                    )
                    state.last_verdict_status = verdict.status
                    state.last_feedback = feedback
                    if improved:
                        state.last_progress_at = time.monotonic()
                        state.consecutive_failures = 0
                        promoted_hash = _atomic_promote_candidate(
                            candidate_path,
                            best_path,
                            str(verdict.candidate_sha256),
                        )
                        state.best_verdict = verdict
                        logger.event(
                            "best_candidate_promoted",
                            task_id=task.slug,
                            agent_id=actor,
                            episode=assignment.generation,
                            source="final_evaluation",
                            status=verdict.status,
                            score=verdict.score,
                            candidate_sha256=promoted_hash,
                            task_contract_sha256=verdict.task_contract_sha256,
                            judge_job_id=verdict.judge_job_id,
                            prior_priority=list(prior_priority),
                            new_priority=list(_verdict_priority(verdict)),
                        )
                    elif candidate_is_usable:
                        state.consecutive_failures += 1

        if superseded:
            verdict = Verdict(
                task.slug,
                "CANCELLED",
                0.0,
                verdict.elapsed_seconds,
                {
                    "reason": "task_solved_by_peer",
                    "superseded_status": verdict.status,
                },
                candidate_sha256=verdict.candidate_sha256,
                task_contract_sha256=verdict.task_contract_sha256,
                judge_job_id=verdict.judge_job_id,
                cache_reused=verdict.cache_reused,
            )
        else:
            _publish_authoritative_validation(
                policy,
                task.slug,
                actor,
                label=f"attempt {assignment.generation}",
                verdict=verdict,
                feedback=feedback,
                deadline_epoch_ms=horizon_epoch_ms,
            )
        logger.scoreboard(verdict, episode=assignment.generation, agent_id=actor)
        logger.event(
            "evaluation_finished",
            **verdict.as_dict(),
            agent_id=actor,
            episode=assignment.generation,
            source="final_evaluation",
            scoreboard_recorded=True,
            phase="solver",
            eligible_for_handoff=(
                not superseded
                and time.monotonic() <= deadline
                and normalize_verdict_status(verdict.status) != "OUT_OF_HORIZON"
            ),
        )
        return result, verdict, False

    # All arms receive an identical initial pool.  Only a slot released after
    # this fill (or after an unfinished initial quota on a smaller pool) enters
    # the allocation treatment.
    initial_assignments: list[AgentAssignment] = []
    for _ in range(config.max_parallel):
        assignment = claim_next(initial_fill=True)
        if assignment is None:
            break
        initial_assignments.append(assignment)
        jobs.put(assignment)

    worker_count = max(1, min(config.max_parallel, len(initial_assignments)))

    evaluation_executor = ThreadPoolExecutor(
        max_workers=config.lean_max_concurrent_evaluations,
        thread_name_prefix="cps-evaluator",
    )

    def settle_execution(
        assignment: AgentAssignment,
        execution: Any,
        *,
        release_backlog: bool,
    ) -> None:
        try:
            try:
                next(execution)
            except StopIteration as stopped:
                settled = stopped.value
            else:
                raise RuntimeError("assignment yielded more than once")
            if not (
                isinstance(settled, tuple)
                and len(settled) == 3
                and isinstance(settled[0], AgentResult)
                and isinstance(settled[1], Verdict)
            ):
                raise RuntimeError("assignment did not produce a complete settlement")
            result, verdict, _solved = settled
            with results_lock:
                results.append((result, verdict))
        except Exception as exc:
            record_run_failure()
            logger.event(
                "evaluator_worker_error",
                task_id=assignment.task_id,
                agent_id=assignment.agent_id,
                episode=assignment.generation,
                **_exception_artifact_fields(
                    exc,
                    config,
                    traceback_bytes=2_000,
                ),
            )
        finally:
            if release_backlog:
                evaluation_backlog_gate.release()

    def worker_loop() -> None:
        while True:
            try:
                assignment = jobs.get(timeout=0.2)
            except Empty:
                if callback_failure.failed or time.monotonic() >= deadline or scheduler.done:
                    return
                continue
            lease_released = False
            execution: Any | None = None
            try:
                execution = execute_assignment(assignment)
                next(execution)
                with allocation_lock:
                    scheduler.finish(assignment, solved=False)
                lease_released = True

                remaining = max(0.0, deadline - time.monotonic())
                admitted = evaluation_backlog_gate.acquire(blocking=False)
                if not admitted:
                    logger.event(
                        "evaluation_backpressure_wait",
                        task_id=assignment.task_id,
                        agent_id=assignment.agent_id,
                        episode=assignment.generation,
                        backlog_limit=evaluation_backlog_limit,
                    )
                    wait_deadline = time.monotonic() + remaining
                    while not admitted and time.monotonic() < wait_deadline:
                        callback_failure.raise_if_failed()
                        admitted = evaluation_backlog_gate.acquire(
                            timeout=min(
                                0.2,
                                max(0.0, wait_deadline - time.monotonic()),
                            )
                        )
                if admitted:
                    try:
                        evaluation_executor.submit(
                            settle_execution,
                            assignment,
                            execution,
                            release_backlog=True,
                        )
                        execution = None
                    except Exception:
                        evaluation_backlog_gate.release()
                        execution.close()
                        raise
                else:
                    logger.event(
                        "evaluation_backpressure_expired",
                        task_id=assignment.task_id,
                        agent_id=assignment.agent_id,
                        episode=assignment.generation,
                        backlog_limit=evaluation_backlog_limit,
                    )
                    # At the horizon the generator's evaluator path resolves
                    # immediately to OUT_OF_HORIZON, preserving exact attempt
                    # closeout even when the bounded queue cannot admit it.
                    settle_execution(
                        assignment,
                        execution,
                        release_backlog=False,
                    )
                    execution = None
                replacement = claim_next()
                if replacement is not None:
                    jobs.put(replacement)
            except Exception as exc:  # fail closed; partial arms are not comparable
                record_run_failure()
                logger.event(
                    "elastic_worker_error",
                    task_id=assignment.task_id,
                    agent_id=assignment.agent_id,
                    **_exception_artifact_fields(
                        exc,
                        config,
                        traceback_bytes=2_000,
                    ),
                )
                if not lease_released:
                    with allocation_lock:
                        scheduler.finish(assignment, solved=False)
                if execution is not None:
                    execution.close()
                return
            finally:
                jobs.task_done()

    try:
        if initial_assignments:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(worker_loop) for _ in range(worker_count)]
                for future in futures:
                    future.result()
    finally:
        # Every admitted evaluator settles before candidate freezing.  This is
        # a bounded queue join, not a solver-slot join.
        evaluation_executor.shutdown(wait=True, cancel_futures=False)

    callback_failure.raise_if_failed()

    seen_tasks = {verdict.task_id for _, verdict in results}
    for task in tasks:
        state = states[task.slug]
        if task.slug in seen_tasks:
            continue
        fallback = Verdict(task.slug, "TIME_LIMIT", 0.0, 0.0, {"reason": "no_assignment_completed"})
        results.append((_mock_result(f"scheduler-{task.slug}", task.slug, state.attempts), fallback))

    scheduler_state = scheduler.snapshot()
    (run_dir / "elastic_scheduler_state.json").write_text(
        json.dumps(scheduler_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    allocation_summary = allocator.summary()
    allocation_summary.update(
        _allocation_runtime_metrics(
            scheduler.history(),
            run_started_monotonic=run_started_monotonic,
            deadline=deadline,
            max_parallel=config.max_parallel,
            policy_latency_seconds=float(allocation_summary["total_latency_seconds"]),
        )
    )
    allocation_summary.update(_scheduler_token_usage(run_dir / "pi_events.jsonl"))
    allocation_summary["initial_pool_size"] = len(initial_assignments)
    allocation_summary["initial_assignments"] = initial_assignment_count
    allocation_summary["adaptive_assignments"] = adaptive_assignments
    allocation_summary["decision_log"] = decisions_path.name
    (run_dir / "allocation_summary.json").write_text(
        json.dumps(allocation_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    results.sort(key=lambda pair: (pair[1].task_id, pair[0].episode, pair[0].agent_id))
    return results


def _run_task_workers(
    config: ExperimentConfig,
    tasks: list[Task],
    run_dir: Path,
    logger: RunLogger,
    evaluator: Any,
    pi_agent: PiAgent,
    policy: CommunicationPolicy,
    *,
    mock_agent: bool,
    deadline: float,
    evaluator_gate: threading.BoundedSemaphore,
    judge_broker: JudgeBroker,
) -> list[tuple[AgentResult, Verdict]]:
    callback_failure = _CallbackFailureState()
    full_score_event = threading.Event()
    solved_lock = threading.Lock()
    solved_tasks: set[str] = set()
    run_cancel_event = _AnyCancelEvent(
        callback_failure,
        _evaluator_remote_settlement_event(evaluator),
        full_score_event,
        reasons=("runner_failure", "remote_settlement_unconfirmed", "full_score"),
    )

    def execute(task: Task) -> tuple[AgentResult, Verdict]:
        workdir = run_dir / "workers" / task.slug
        _stage_task(task, workdir, config=config)
        best_result: AgentResult | None = None
        best_verdict: Verdict | None = None
        actor = f"worker-{task.slug}-e0"
        early_lock = threading.RLock()
        early_credit: _EarlyProofCredit | None = None
        expected_contract = _expected_task_contract(evaluator, task)
        allow_mock_provenance = _allows_mock_provenance(evaluator)
        candidate_path = _candidate_path(workdir, task)

        def admit_early_proof(
            proved_task: Task,
            verdict: Verdict,
            snapshot: CandidateSnapshot,
        ) -> None:
            nonlocal early_credit
            try:
                if proved_task.slug != task.slug or not _has_authoritative_snapshot_provenance(
                    verdict,
                    snapshot,
                    expected_task_contract_sha256=expected_contract,
                    allow_mock_provenance=allow_mock_provenance,
                ):
                    raise ValueError("broker proof failed runner snapshot provenance")
                with early_lock:
                    if early_credit is not None:
                        return
                    verified = _candidate_path(workdir / "verified", task)
                    _atomic_promote_source(snapshot.source, verified, snapshot.sha256)
                    credit = _EarlyProofCredit(
                        verdict=verdict,
                        candidate_source=snapshot.source,
                        candidate_sha256=snapshot.sha256,
                        actor_id=actor,
                        episode=episode,
                    )
                    logger.event(
                        "judge_proof_credited",
                        task_id=task.slug,
                        agent_id=actor,
                        episode=episode,
                        candidate_sha256=snapshot.sha256,
                        task_contract_sha256=verdict.task_contract_sha256,
                        judge_job_id=verdict.judge_job_id,
                    )
                    logger.scoreboard(
                        verdict,
                        episode=episode,
                        agent_id=actor,
                        source="judge_check",
                    )
                    early_credit = credit
                    if verdict.score >= 1.0 and normalize_verdict_status(verdict.status) in _AUTHORITATIVE_PROVED_STATUSES:
                        with solved_lock:
                            solved_tasks.add(task.slug)
                            if config.cancel_on_proved and len(solved_tasks) == len(tasks):
                                full_score_event.set()
            except Exception:
                callback_failure.record()
                raise

        for episode in range(1, config.episodes_per_task + 1):
            callback_failure.raise_if_failed()
            if time.monotonic() >= deadline:
                break
            actor = f"worker-{task.slug}-e{episode}"
            digest = policy.digest(task.slug, actor, query=task.theorem_name)
            prompt = build_task_prompt(
                task,
                task_workspace=str(workdir),
                agent_id=actor,
                episode=episode,
                communication_enabled=policy.enabled,
                formal_tools_enabled=config.formal_tools_enabled,
                digest=digest,
            )
            if mock_agent:
                result = _mock_result(actor, task.slug, episode)
            else:
                with judge_broker.session(
                    actor_id=actor,
                    workdir=workdir,
                    candidates={task.slug: (task, candidate_path)},
                    deadline_monotonic=deadline,
                    cps_store=policy.store if policy.enabled else None,
                    communication=config.communication if policy.enabled else "none",
                    roster_path=(run_dir / "actors.json") if policy.enabled else None,
                    on_authoritative_verdict=admit_early_proof,
                    cancel_event=run_cancel_event,
                ) as broker_env:
                    result = pi_agent.run(
                        task_id=task.slug,
                        actor_id=actor,
                        episode=episode,
                        prompt=prompt,
                        workdir=workdir,
                        extra_env=broker_env,
                        deadline_monotonic=deadline,
                        cancel_event=run_cancel_event,
                    )
            _raise_if_remote_settlement_unconfirmed(
                evaluator,
                on_failure=callback_failure.record,
            )
            callback_failure.raise_if_failed()
            logger.event("agent_finished", **result.as_dict())
            with early_lock:
                credit = early_credit if early_credit and early_credit.episode == episode else None
            if credit is not None:
                _atomic_promote_source(
                    credit.candidate_source,
                    candidate_path,
                    credit.candidate_sha256,
                )
                verdict = credit.verdict
            else:
                verdict = _evaluate_candidate(
                    evaluator,
                    task,
                    candidate_path,
                    deadline,
                    evaluator_gate,
                    cancel_event=run_cancel_event,
                )
                _raise_if_remote_settlement_unconfirmed(
                    evaluator,
                    verdict,
                    on_failure=callback_failure.record,
                )
                verdict = _within_horizon(verdict, deadline)
                verdict = _enforce_verdict_provenance(
                    verdict,
                    candidate_path,
                    expected_task_contract_sha256=expected_contract,
                    allow_mock_provenance=allow_mock_provenance,
                )
                logger.scoreboard(verdict, episode=episode, agent_id=actor)
            if verdict.score >= 1.0 and normalize_verdict_status(verdict.status) in _AUTHORITATIVE_PROVED_STATUSES:
                with solved_lock:
                    solved_tasks.add(task.slug)
                    if config.cancel_on_proved and len(solved_tasks) == len(tasks):
                        full_score_event.set()
            logger.event(
                "evaluation_finished",
                **verdict.as_dict(),
                agent_id=actor,
                episode=episode,
                source="judge_check" if credit is not None else "final_evaluation",
                scoreboard_recorded=credit is None,
            )
            best_result, best_verdict = result, verdict
            if policy.enabled and credit is None:
                feedback = verdict.error or str(
                    verdict.response.get("error_message")
                    or verdict.response.get("reason")
                    or verdict.status
                )
                _publish_authoritative_validation(
                    policy,
                    task.slug,
                    actor,
                    label=f"episode {episode}",
                    verdict=verdict,
                    feedback=feedback,
                )
            if verdict.score >= 1.0:
                break
        if best_result is None or best_verdict is None:
            best_result = _mock_result(actor, task.slug, config.episodes_per_task)
            best_verdict = Verdict(task.slug, "TIME_LIMIT", 0.0, 0.0)
        return best_result, best_verdict

    results: list[tuple[AgentResult, Verdict]] = []
    if policy.enabled:
        actors = [
            {"actor_id": f"worker-{task.slug}-e{episode}", "task_id": task.slug, "episode": episode}
            for task in tasks
            for episode in range(1, config.episodes_per_task + 1)
        ]
        (run_dir / "actors.json").write_text(
            json.dumps(actors, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    with ThreadPoolExecutor(max_workers=config.max_parallel) as executor:
        futures = {executor.submit(execute, task): task.slug for task in tasks}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda pair: pair[1].task_id)
    return results


def _stage_task(
    task: Task,
    destination: Path,
    *,
    config: ExperimentConfig,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "problem.md").write_text(task.problem_text, encoding="utf-8")
    baseline_dir = destination / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    baseline_source = baseline_dir / task.baseline_filename
    baseline_source.write_text(task.baseline_code, encoding="utf-8")
    (destination / "metadata.json").write_text(
        json.dumps(task.metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = _candidate_path(destination, task)
    if not result.exists():
        result.write_text(task.baseline_code, encoding="utf-8")
    if config.formal_tools_enabled:
        stage_worker_tools(
            destination,
            capability=ToolCapability(
                task_id=task.slug,
                surface_version=config.formal_tools_version,
            ),
            baseline_names=[baseline_source.name],
        )


def _within_horizon(verdict: Verdict, deadline: float) -> Verdict:
    if (
        normalize_verdict_status(verdict.status)
        == "REMOTE_SETTLEMENT_UNCONFIRMED"
        or time.monotonic() <= deadline
    ):
        return verdict
    return Verdict(
        task_id=verdict.task_id,
        status="OUT_OF_HORIZON",
        score=0.0,
        elapsed_seconds=verdict.elapsed_seconds,
        response={"original_status": verdict.status, **verdict.response},
        error=verdict.error,
        candidate_sha256=verdict.candidate_sha256,
        task_contract_sha256=verdict.task_contract_sha256,
        judge_job_id=verdict.judge_job_id,
        cache_reused=verdict.cache_reused,
    )


def _evaluate_candidate(
    evaluator: Any,
    task: Task,
    candidate: Path,
    deadline: float,
    gate: threading.BoundedSemaphore,
    *,
    cancel_event: Any | None = None,
) -> Verdict:
    acquired, remote_unsettled = _acquire_evaluator_gate(
        evaluator,
        gate,
        deadline_monotonic=deadline,
    )
    if remote_unsettled:
        return _remote_settlement_verdict(task)
    if not acquired:
        return Verdict(task.slug, "OUT_OF_HORIZON", 0.0, 0.0, {"reason": "run_horizon_elapsed"})
    evaluator_unsettled_before = _evaluator_remote_unsettled_jobs(evaluator)
    release_gate = True
    try:
        evaluate_kwargs: dict[str, Any] = {"deadline_monotonic": deadline}
        if cancel_event is not None and _call_accepts_cancel_event(evaluator.evaluate):
            evaluate_kwargs["cancel_event"] = cancel_event
        if _call_accepts_settlement_callback(evaluator.evaluate):
            evaluate_kwargs["settlement_callback"] = lambda: _release_gate(gate)
        verdict = evaluator.evaluate(task, candidate, **evaluate_kwargs)
        verdict = _bind_legacy_test_mock_verdict(evaluator, task, candidate, verdict)
        evaluator_unsettled_after = _evaluator_remote_unsettled_jobs(evaluator)
        call_unsettled = (
            evaluator_unsettled_after > 0
            or _verdict_has_unsettled_remote_work(verdict)
        )
        deferred_settlement = _verdict_has_deferred_remote_work(verdict)
        if deferred_settlement:
            # The evaluator retained this permit for its settlement watcher;
            # the callback, not this finally block, owns its eventual release.
            release_gate = False
        if call_unsettled:
            release_gate = False
            return _remote_settlement_verdict(task, verdict)
        if deferred_settlement:
            return verdict
        return verdict
    except BaseException:
        if _evaluator_remote_unsettled_jobs(evaluator) > evaluator_unsettled_before:
            release_gate = False
        raise
    finally:
        if release_gate:
            gate.release()


def _evaluator_remote_unsettled_jobs(evaluator: Any) -> int:
    try:
        value = getattr(evaluator, "remote_unsettled_jobs", 0)
    except Exception:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _call_accepts_cancel_event(function: Any) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "cancel_event"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _acquire_evaluator_gate(
    evaluator: Any,
    gate: threading.BoundedSemaphore,
    *,
    deadline_monotonic: float | None,
) -> tuple[bool, bool]:
    """Wait in short intervals so the global remote latch aborts admission."""

    while True:
        if _evaluator_remote_unsettled_jobs(evaluator) > 0:
            return False, True
        if deadline_monotonic is None:
            wait_seconds = 0.1
        else:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                return False, False
            wait_seconds = min(remaining, 0.1)
        if not gate.acquire(timeout=wait_seconds):
            continue
        if _evaluator_remote_unsettled_jobs(evaluator) > 0:
            gate.release()
            return False, True
        return True, False


def _verdict_has_unsettled_remote_work(verdict: Verdict) -> bool:
    response = verdict.response if isinstance(verdict.response, Mapping) else {}
    cancellation = response.get("judge_cancellation")
    return (
        normalize_verdict_status(verdict.status)
        == "REMOTE_SETTLEMENT_UNCONFIRMED"
        or response.get("remote_settlement_unconfirmed") is True
        or response.get("settlement_error") == "cancel_settlement_unconfirmed"
        or (
            isinstance(cancellation, Mapping)
            and cancellation.get("attempted") is True
            and cancellation.get("settled") is not True
            and cancellation.get("deferred") is not True
        )
    )


def _verdict_has_deferred_remote_work(verdict: Verdict) -> bool:
    response = verdict.response if isinstance(verdict.response, Mapping) else {}
    cancellation = response.get("judge_cancellation")
    return (
        response.get("settlement_error") == "cancel_settlement_deferred"
        or (
            isinstance(cancellation, Mapping)
            and cancellation.get("deferred") is True
        )
    )


def _call_accepts_settlement_callback(function: Any) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "settlement_callback"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _release_gate(gate: threading.BoundedSemaphore) -> None:
    gate.release()


def _remote_settlement_verdict(
    task: Task,
    original: Verdict | None = None,
) -> Verdict:
    response = dict(original.response) if original is not None else {}
    response["remote_settlement_unconfirmed"] = True
    response.setdefault("reason", "remote_judge_terminal_receipt_unconfirmed")
    return Verdict(
        task_id=task.slug,
        status="REMOTE_SETTLEMENT_UNCONFIRMED",
        score=0.0,
        elapsed_seconds=(original.elapsed_seconds if original is not None else 0.0),
        response=response,
        error=(original.error if original is not None else None),
        candidate_sha256=(original.candidate_sha256 if original is not None else None),
        task_contract_sha256=(
            original.task_contract_sha256 if original is not None else None
        ),
        judge_job_id=(original.judge_job_id if original is not None else None),
        cache_reused=(original.cache_reused if original is not None else False),
    )


def _candidate_source(config: ExperimentConfig, task: Task, run_dir: Path) -> Path:
    if config.mode == "mono":
        return _candidate_path(run_dir / "workers" / "mono" / "tasks" / task.slug, task)
    if config.uses_cps:
        return _candidate_path(run_dir / "workers" / task.slug / "best", task)
    return _candidate_path(run_dir / "workers" / task.slug, task)


def _freeze_closeout_candidates(
    config: ExperimentConfig,
    tasks: list[Task],
    run_dir: Path,
    logger: RunLogger,
) -> dict[str, _FrozenCandidate]:
    """Freeze one mode-defined candidate per task before final evaluation."""

    root = run_dir / "closeout_candidates"
    frozen: dict[str, _FrozenCandidate] = {}
    rows: list[dict[str, Any]] = []
    for task in tasks:
        source = _candidate_source(config, task, run_dir)
        destination = _candidate_path(root / task.slug, task)
        digest: str | None = None
        error: str | None = None
        try:
            payload = source.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            _atomic_write_candidate(payload, destination, digest)
            destination.chmod(0o444)
        except OSError as exc:
            error = f"cannot freeze candidate: {exc.strerror or type(exc).__name__}"
        frozen[task.slug] = _FrozenCandidate(task.slug, destination, digest, error)
        row: dict[str, Any] = {
            "task_id": task.slug,
            "source": str(source.relative_to(run_dir)),
            "snapshot": str(destination.relative_to(run_dir)),
            "candidate_sha256": digest,
        }
        if error is not None:
            row["error"] = error
        rows.append(row)
    index = {"candidates": rows}
    (run_dir / "closeout_candidates.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Keep an index beside the immutable files as well as at the run root so
    # a copied closeout bundle remains self-describing.
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.event(
        "candidates_frozen",
        candidate_count=len(rows),
        candidates=[
            {"task_id": row["task_id"], "candidate_sha256": row["candidate_sha256"]}
            for row in rows
        ],
    )
    return frozen


def _evaluate_closeout_candidate(
    evaluator: Any,
    task: Task,
    candidate: _FrozenCandidate,
    gate: threading.BoundedSemaphore,
) -> Verdict:
    if candidate.error or not candidate.path.is_file():
        return Verdict(
            task.slug,
            "MISSING_CANDIDATE",
            0.0,
            0.0,
            {"candidate_sha256": candidate.sha256},
            error=candidate.error or "candidate snapshot is missing",
        )
    acquired, remote_unsettled = _acquire_evaluator_gate(
        evaluator,
        gate,
        deadline_monotonic=None,
    )
    if remote_unsettled:
        return _remote_settlement_verdict(task)
    if not acquired:  # pragma: no cover - an unbounded wait has no third state
        return _remote_settlement_verdict(task)
    evaluator_unsettled_before = _evaluator_remote_unsettled_jobs(evaluator)
    release_gate = True
    try:
        # Production closeout must create an independently observable Judge
        # receipt even when the same candidate was probed during the horizon.
        # Narrow test doubles and the cache-free MockEvaluator retain the
        # legacy evaluate interface.
        evaluate_fresh = getattr(evaluator, "evaluate_fresh", None)
        evaluate = evaluate_fresh if callable(evaluate_fresh) else evaluator.evaluate
        verdict = evaluate(task, candidate.path, deadline_monotonic=None)
        verdict = _bind_legacy_test_mock_verdict(
            evaluator,
            task,
            candidate.path,
            verdict,
        )
        evaluator_unsettled_after = _evaluator_remote_unsettled_jobs(evaluator)
        call_unsettled = (
            evaluator_unsettled_after > 0
            or _verdict_has_unsettled_remote_work(verdict)
        )
        if call_unsettled:
            release_gate = False
            verdict = _remote_settlement_verdict(task, verdict)
    except BaseException:
        if _evaluator_remote_unsettled_jobs(evaluator) > evaluator_unsettled_before:
            release_gate = False
        raise
    finally:
        if release_gate:
            gate.release()
    return _enforce_verdict_provenance(
        verdict,
        candidate.path,
        expected_task_contract_sha256=_expected_task_contract(evaluator, task),
        allow_mock_provenance=_allows_mock_provenance(evaluator),
    )


def _run_closeout(
    config: ExperimentConfig,
    tasks: list[Task],
    frozen: Mapping[str, _FrozenCandidate],
    logger: RunLogger,
    evaluator: Any,
    gate: threading.BoundedSemaphore,
    *,
    reusable_verdicts: Iterable[Verdict] = (),
) -> dict[str, Verdict]:
    """Score frozen candidates under one bounded, feedback-free contract."""

    prior_verdicts = tuple(reusable_verdicts)
    logger.event(
        "closeout_started",
        candidate_count=len(tasks),
        max_concurrent_evaluations=config.lean_max_concurrent_evaluations,
        execution_timeout_seconds=config.lean_timeout_seconds,
    )
    verdicts: dict[str, Verdict] = {}
    disposition_counts: dict[str, int] = {}

    def evaluate(task: Task) -> _CloseoutDecision:
        candidate = frozen[task.slug]
        prior, mismatch = _prior_authoritative_proof(
            evaluator,
            task,
            candidate,
            prior_verdicts,
        )
        try:
            observed = _evaluate_closeout_candidate(
                evaluator,
                task,
                candidate,
                gate,
            )
        except Exception as exc:
            try:
                contract_sha256 = _expected_task_contract(evaluator, task)
            except Exception:
                contract_sha256 = None
            observed = Verdict(
                task.slug,
                "EVALUATOR_ERROR",
                0.0,
                0.0,
                {
                    "error_kind": "closeout_evaluator_exception",
                    "retryable": False,
                },
                error=sanitize_worker_text(exc),
                candidate_sha256=candidate.sha256,
                task_contract_sha256=contract_sha256,
            )
        if _verdict_has_unsettled_remote_work(observed):
            return _CloseoutDecision(
                observed,
                observed,
                prior,
                "remote_settlement_unconfirmed",
                mismatch,
            )
        if prior is None:
            return _CloseoutDecision(
                observed,
                observed,
                None,
                "evaluated",
                mismatch,
            )

        candidate_sha = _normalized_sha256(candidate.sha256)
        contract_sha = _normalized_sha256(prior.task_contract_sha256)
        assert candidate_sha is not None and contract_sha is not None
        if _authoritative_proof_matches(
            observed,
            candidate_sha256=candidate_sha,
            task_contract_sha256=contract_sha,
        ):
            return _CloseoutDecision(
                _preserve_authority_after_confirmation(prior, observed),
                observed,
                prior,
                "authority_confirmed",
            )
        if _retryable_closeout_infrastructure_failure(observed):
            return _CloseoutDecision(
                _mark_closeout_infrastructure_incomplete(
                    task,
                    candidate,
                    prior,
                    observed,
                ),
                observed,
                prior,
                "retryable_infra_unconfirmed",
            )
        return _CloseoutDecision(
            _authority_conflict_verdict(task, prior, observed),
            observed,
            prior,
            "authority_conflict",
        )

    worker_count = max(1, min(config.lean_max_concurrent_evaluations, len(tasks)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="closeout") as executor:
        futures = {executor.submit(evaluate, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            decision = future.result()
            verdict = decision.verdict
            verdicts[task.slug] = verdict
            disposition_counts[decision.disposition] = (
                disposition_counts.get(decision.disposition, 0) + 1
            )
            if decision.authority_mismatch is not None:
                logger.event(
                    "closeout_authority_mismatch",
                    task_id=task.slug,
                    candidate_sha256=frozen[task.slug].sha256,
                    **decision.authority_mismatch,
                )
            if decision.disposition == "retryable_infra_unconfirmed":
                logger.event(
                    "closeout_infra_incomplete",
                    task_id=task.slug,
                    candidate_sha256=verdict.candidate_sha256,
                    task_contract_sha256=verdict.task_contract_sha256,
                    observed_status=_normalized_verdict_status(decision.observed),
                    observed_error_kind=_response_value(
                        decision.observed.response,
                        "error_kind",
                    ),
                    observed_terminal_reason=_response_value(
                        decision.observed.response,
                        "terminal_reason",
                    ),
                    observed_retryable=True,
                    final_status=verdict.status,
                    final_score=verdict.score,
                    prior_authoritative_proof_available=True,
                    fresh_closeout_confirmed=False,
                )
            elif decision.disposition == "authority_confirmed":
                logger.event(
                    "closeout_authority_confirmed",
                    task_id=task.slug,
                    candidate_sha256=verdict.candidate_sha256,
                    task_contract_sha256=verdict.task_contract_sha256,
                    prior_judge_job_id=verdict.judge_job_id,
                    observed_judge_job_id=decision.observed.judge_job_id,
                    observed_status=_normalized_verdict_status(decision.observed),
                )
            elif decision.disposition == "authority_conflict":
                logger.event(
                    "closeout_authority_conflict",
                    task_id=task.slug,
                    candidate_sha256=verdict.candidate_sha256,
                    task_contract_sha256=verdict.task_contract_sha256,
                    prior_status=_normalized_verdict_status(
                        decision.prior_authority
                    ) if decision.prior_authority is not None else None,
                    observed_status=_normalized_verdict_status(decision.observed),
                    observed_error_kind=_response_value(
                        decision.observed.response,
                        "error_kind",
                    ),
                    observed_retryable=(
                        _response_value(decision.observed.response, "retryable")
                        is True
                    ),
                    final_status=verdict.status,
                )
            scoreboard_recorded = decision.disposition in {
                "evaluated",
                "authority_conflict",
            }
            if scoreboard_recorded:
                logger.scoreboard(
                    verdict,
                    episode=0,
                    agent_id="closeout",
                    source="closeout",
                )
            logger.event(
                "closeout_evaluation_finished",
                **verdict.as_dict(),
                agent_id="closeout",
                episode=0,
                observed_status=_normalized_verdict_status(decision.observed),
                # A prior solver proof is diagnostic linkage only.  It is never
                # reused as the final official verdict when fresh closeout is
                # incomplete.
                reused_authoritative_verdict=False,
                authoritative_proof_confirmed=(
                    decision.disposition == "authority_confirmed"
                ),
                closeout_infra_incomplete=(
                    decision.disposition == "retryable_infra_unconfirmed"
                ),
                prior_authoritative_proof_available=(
                    decision.prior_authority is not None
                    and decision.disposition == "retryable_infra_unconfirmed"
                ),
                fresh_closeout_confirmed=(
                    decision.disposition == "authority_confirmed"
                ),
                authority_conflict=(
                    decision.disposition == "authority_conflict"
                ),
                scoreboard_recorded=scoreboard_recorded,
            )
    ordered = {task.slug: verdicts[task.slug] for task in tasks}
    logger.event(
        "closeout_finished",
        score=sum(verdict.score for verdict in ordered.values()),
        # Kept for schema compatibility; no authoritative verdict is reused
        # when fresh closeout is incomplete.
        reused_authoritative_verdicts=0,
        authoritative_proofs_confirmed=disposition_counts.get(
            "authority_confirmed",
            0,
        ),
        closeout_infra_incomplete=disposition_counts.get(
            "retryable_infra_unconfirmed",
            0,
        ),
        closeout_infra_unconfirmed=disposition_counts.get(
            "retryable_infra_unconfirmed",
            0,
        ),
        authority_conflicts=disposition_counts.get("authority_conflict", 0),
        remote_settlement_unconfirmed=disposition_counts.get(
            "remote_settlement_unconfirmed",
            0,
        ),
    )
    return ordered


def _write_mono_bundle(worker_dir: Path, tasks: Iterable[Task]) -> None:
    task_list = list(tasks)
    solutions: dict[str, str] = {}
    for task in task_list:
        candidate = _candidate_path(worker_dir / "tasks" / task.slug, task)
        try:
            solutions[task.slug] = candidate.read_text(encoding="utf-8")
        except OSError:
            solutions[task.slug] = ""
    (worker_dir / "result.json").write_text(
        json.dumps(
            {
                "schema_version": (
                    "coding_cpp_single_run_bundle_v1"
                    if any(task.candidate_filename == "result.cpp" for task in task_list)
                    else "formal_lean_single_run_bundle_v1"
                ),
                "solutions": solutions,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _mock_result(agent_id: str, task_id: str, episode: int) -> AgentResult:
    now = utc_now()
    return AgentResult(
        agent_id=agent_id,
        task_id=task_id,
        episode=episode,
        returncode=0,
        started_at=now,
        finished_at=now,
        command=["<mock-agent>"],
        mocked=True,
    )


def _read_jsonl_objects(path: Path) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows, False
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return rows, False
        if not isinstance(item, dict):
            return rows, False
        rows.append(item)
    return rows, True


def _run_health(
    run_dir: Path,
    config: ExperimentConfig,
    verdicts: Mapping[str, Verdict],
    agent_results: Iterable[AgentResult],
    attempt_verdicts: Iterable[Verdict],
    *,
    expected_task_count: int,
) -> dict[str, Any]:
    """Summarize every attempt; best-per-task verdicts cannot hide failures."""

    agents = list(agent_results)
    scheduler_agents = [item for item in agents if item.task_id == "__allocation__"]
    solver_agents = [item for item in agents if item.task_id != "__allocation__"]
    attempts = list(attempt_verdicts)
    issues: set[str] = set()
    status_counts: dict[str, int] = {}
    for verdict in attempts:
        status = normalize_verdict_status(verdict.status)
        status_counts[status] = status_counts.get(status, 0) + 1
        if _is_infrastructure_verdict(verdict):
            issues.add("evaluator_infrastructure_error")
        if status in _NONTERMINAL_VERDICT_STATUSES:
            issues.add("nonterminal_verdict")
        if status == "PROVENANCE_INVALID":
            issues.add("verdict_provenance_invalid")
    if len(verdicts) != expected_task_count:
        issues.add("final_task_bundle_incomplete")
    incomplete_closeout_statuses = {
        "AUTHORITY_CONFLICT",
        "CANCELLED",
        "EVALUATOR_ERROR",
        "EVALUATOR_TIMEOUT",
        "INFRASTRUCTURE_ERROR",
        "MISSING_CANDIDATE",
        "OUT_OF_HORIZON",
        "REJECTED_OVERLOADED",
        "REMOTE_SETTLEMENT_UNCONFIRMED",
    }
    for verdict in verdicts.values():
        status = normalize_verdict_status(verdict.status)
        retryable_terminal_limit = bool(
            status in {"EXECUTION_TIMEOUT", "RESOURCE_LIMIT"}
            and _response_value(verdict.response, "retryable") is True
        )
        if (
            status in incomplete_closeout_statuses
            or status in _NONTERMINAL_VERDICT_STATUSES
            or retryable_terminal_limit
        ):
            issues.add("closeout_incomplete")
        if status == "AUTHORITY_CONFLICT":
            issues.add("closeout_authority_conflict")
        if _is_infrastructure_verdict(verdict):
            issues.add("evaluator_infrastructure_error")
        if status == "PROVENANCE_INVALID":
            issues.add("verdict_provenance_invalid")

    unexpected_process_errors = 0
    oom_or_137 = 0
    for result in solver_agents:
        tail = f"{result.error_tail}\n{result.output_tail}".lower()
        if result.returncode == 137 or "out of memory" in tail or "oom-kill" in tail:
            oom_or_137 += 1
            issues.add("solver_oom_or_exit_137")
        if result.returncode != 0 and not result.cancelled and not result.timed_out:
            unexpected_process_errors += 1
            issues.add("solver_process_error")

    scheduler_horizon_truncations = sum(
        item.run_horizon_reached for item in scheduler_agents
    )
    scheduler_policy_agents = [
        item for item in scheduler_agents if not item.run_horizon_reached
    ]
    scheduler_nonzero_returns = sum(item.returncode != 0 for item in scheduler_agents)
    scheduler_policy_nonzero_returns = sum(
        item.returncode != 0 for item in scheduler_policy_agents
    )
    scheduler_timeouts = sum(item.timed_out for item in scheduler_agents)
    scheduler_policy_timeouts = sum(item.timed_out for item in scheduler_policy_agents)
    scheduler_cancellations = sum(item.cancelled for item in scheduler_agents)
    scheduler_policy_cancellations = sum(
        item.cancelled for item in scheduler_policy_agents
    )
    scheduler_oom_or_137 = sum(
        item.returncode == 137
        or "out of memory" in f"{item.error_tail}\n{item.output_tail}".lower()
        or "oom-kill" in f"{item.error_tail}\n{item.output_tail}".lower()
        for item in scheduler_policy_agents
    )
    if config.uses_cps and config.allocation.policy == "agent":
        if scheduler_policy_nonzero_returns:
            issues.add("allocation_scheduler_process_error")
        if scheduler_policy_timeouts:
            issues.add("allocation_scheduler_timeout")
        if scheduler_policy_cancellations:
            issues.add("allocation_scheduler_cancelled")
        if scheduler_oom_or_137:
            issues.add("allocation_scheduler_oom_or_exit_137")

    events, events_valid = _read_jsonl_objects(run_dir / "events.jsonl")
    if not events_valid:
        issues.add("events_invalid_or_missing")
    worker_errors = sum(
        str(row.get("event") or "")
        in {
            "run_error",
            "elastic_worker_error",
            "evaluator_worker_error",
            "preflight_failed",
        }
        for row in events
    )
    if worker_errors:
        issues.add("runner_or_worker_error")

    lifecycle_events = (
        "horizon_closed",
        "candidates_frozen",
        "closeout_started",
        "closeout_finished",
    )
    lifecycle_observed = any(
        row.get("event") in lifecycle_events for row in events
    ) or (run_dir / "closeout_candidates.json").exists()
    if lifecycle_observed:
        lifecycle_positions: list[int] = []
        for event_name in lifecycle_events:
            positions = [
                index
                for index, row in enumerate(events)
                if row.get("event") == event_name
            ]
            if len(positions) != 1:
                issues.add("closeout_lifecycle_incomplete")
            else:
                lifecycle_positions.append(positions[0])
        if (
            len(lifecycle_positions) == len(lifecycle_events)
            and lifecycle_positions != sorted(lifecycle_positions)
        ):
            issues.add("closeout_lifecycle_incomplete")
        closeout_rows = [
            row for row in events if row.get("event") == "closeout_evaluation_finished"
        ]
        if verdicts and len(closeout_rows) != expected_task_count:
            issues.add("closeout_lifecycle_incomplete")
        try:
            closeout_index = json.loads(
                (run_dir / "closeout_candidates.json").read_text(encoding="utf-8")
            )
            indexed_candidates = closeout_index["candidates"]
            if (
                not isinstance(indexed_candidates, list)
                or len(indexed_candidates) != expected_task_count
            ):
                issues.add("closeout_candidate_index_invalid")
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            issues.add("closeout_candidate_index_invalid")

    scheduler_event_rows = [
        row for row in events if row.get("event") == "allocation_scheduler_finished"
    ]
    scheduler_result_identities = Counter(
        (
            str(item.decision_index),
            item.agent_id,
            item.task_id,
            str(item.episode),
        )
        for item in scheduler_agents
    )
    scheduler_event_identities = Counter(
        (
            str(row.get("decision_index")),
            str(row.get("agent_id") or ""),
            str(row.get("task_id") or ""),
            str(row.get("episode")),
        )
        for row in scheduler_event_rows
    )

    probe_rows, probe_valid = _read_jsonl_objects(run_dir / "judge_checks.jsonl")
    if not probe_valid:
        issues.add("judge_audit_invalid_or_missing")
    probe_infrastructure_errors = sum(
        str(row.get("status") or "").upper() in _INFRASTRUCTURE_VERDICT_STATUSES
        for row in probe_rows
    )
    if probe_infrastructure_errors:
        issues.add("judge_probe_infrastructure_error")

    assigned_count = finished_count = evaluated_count = 0
    scheduler_invalid_outputs = 0
    scheduler_fallbacks = 0
    scheduler_summary_agent_calls: int | None = None
    scheduler_active_slots: int | None = None
    if config.uses_cps:
        decisions, decisions_valid = _read_jsonl_objects(
            run_dir / "allocation_decisions.jsonl"
        )
        if not decisions_valid:
            issues.add("allocation_decision_log_invalid_or_missing")
        agent_decisions = [
            row for row in decisions if str(row.get("policy") or "") == "agent"
        ]
        scheduler_invalid_outputs = sum(
            row.get("agent_result_valid") is False for row in agent_decisions
        )
        scheduler_fallbacks = sum(bool(row.get("fallback")) for row in agent_decisions)
        scheduler_decision_identities = Counter(
            (
                str(row.get("decision_index")),
                str(row.get("agent_id") or ""),
                str(row.get("agent_task_id") or ""),
                str(row.get("agent_episode")),
            )
            for row in agent_decisions
        )
        try:
            allocation_summary = json.loads(
                (run_dir / "allocation_summary.json").read_text(encoding="utf-8")
            )
            if isinstance(allocation_summary, Mapping):
                raw_agent_calls = allocation_summary.get("agent_calls")
                if raw_agent_calls is not None:
                    scheduler_summary_agent_calls = int(raw_agent_calls)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            allocation_summary = None
        if config.allocation.policy == "agent":
            if scheduler_invalid_outputs:
                issues.add("allocation_scheduler_invalid_output")
            if scheduler_fallbacks:
                issues.add("allocation_scheduler_fallback")
            if (
                scheduler_result_identities != scheduler_event_identities
                or scheduler_result_identities != scheduler_decision_identities
                or len(scheduler_event_rows) != len(scheduler_agents)
                or scheduler_summary_agent_calls != len(scheduler_agents)
            ):
                issues.add("allocation_scheduler_closeout_mismatch")
        assignments, assignments_valid = _read_jsonl_objects(
            run_dir / "elastic_assignments.jsonl"
        )
        if not assignments_valid:
            issues.add("assignment_log_invalid_or_missing")
        assigned_keys = {
            (
                str(row.get("agent_id") or ""),
                str(row.get("task_id") or ""),
                int(row.get("generation") or 0),
            )
            for row in assignments
            if row.get("agent_id")
        }
        finished_keys = {
            (
                str(row.get("agent_id") or ""),
                str(row.get("task_id") or ""),
                int(row.get("episode") or 0),
            )
            for row in events
            if row.get("event") == "agent_finished" and row.get("agent_id")
        }
        evaluated_keys = {
            (
                str(row.get("agent_id") or ""),
                str(row.get("task_id") or ""),
                int(row.get("episode") or 0),
            )
            for row in events
            if row.get("event") == "evaluation_finished" and row.get("agent_id")
        }
        result_keys = {
            (item.agent_id, item.task_id, int(item.episode)) for item in solver_agents
        }
        assigned_count = len(assigned_keys)
        finished_count = len(finished_keys)
        evaluated_count = len(evaluated_keys)
        if not (
            assigned_keys == finished_keys == evaluated_keys == result_keys
            and len(attempts) == len(assigned_keys)
        ):
            issues.add("assignment_closeout_mismatch")
        try:
            scheduler_state = json.loads(
                (run_dir / "elastic_scheduler_state.json").read_text(encoding="utf-8")
            )
            scheduler_active_slots = int(scheduler_state["active_slots"])
            task_rows = scheduler_state.get("tasks") or {}
            if scheduler_active_slots != 0 or any(
                int(row.get("active_agents") or 0) != 0
                for row in task_rows.values()
                if isinstance(row, Mapping)
            ):
                issues.add("scheduler_not_closed")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            issues.add("scheduler_state_invalid_or_missing")

    return {
        "schema_version": "contextswarm_run_health_v1",
        "ok": not issues,
        "issues": sorted(issues),
        "attempt_count": len(attempts),
        "attempt_verdict_status_counts": dict(sorted(status_counts.items())),
        "final_task_count": len(verdicts),
        "solver_result_count": len(solver_agents),
        "solver_timeout_count": sum(item.timed_out for item in solver_agents),
        "solver_cancelled_count": sum(item.cancelled for item in solver_agents),
        "unexpected_process_error_count": unexpected_process_errors,
        "oom_or_exit_137_count": oom_or_137,
        "runner_or_worker_error_count": worker_errors,
        "allocation_scheduler_result_count": len(scheduler_agents),
        "allocation_scheduler_finished_event_count": len(scheduler_event_rows),
        "allocation_scheduler_nonzero_return_count": scheduler_nonzero_returns,
        "allocation_scheduler_timeout_count": scheduler_timeouts,
        "allocation_scheduler_cancelled_count": scheduler_cancellations,
        "allocation_scheduler_policy_timeout_count": scheduler_policy_timeouts,
        "allocation_scheduler_horizon_truncation_count": scheduler_horizon_truncations,
        "allocation_scheduler_oom_or_exit_137_count": scheduler_oom_or_137,
        "allocation_scheduler_invalid_output_count": scheduler_invalid_outputs,
        "allocation_scheduler_fallback_count": scheduler_fallbacks,
        "allocation_scheduler_summary_agent_calls": scheduler_summary_agent_calls,
        "judge_probe_count": len(probe_rows),
        "judge_probe_infrastructure_error_count": probe_infrastructure_errors,
        "assigned_count": assigned_count,
        "finished_count": finished_count,
        "evaluated_count": evaluated_count,
        "scheduler_active_slots": scheduler_active_slots,
    }


def _write_final(
    run_dir: Path,
    config: ExperimentConfig,
    verdicts: Mapping[str, Verdict],
    agent_results: Iterable[AgentResult],
    *,
    status: str,
    cps_summary: Mapping[str, Any] | None,
    health: Mapping[str, Any] | None = None,
) -> None:
    rows = {key: value.as_dict() for key, value in sorted(verdicts.items())}
    all_agent_rows = [item.as_dict() for item in agent_results]
    agent_rows = [item for item in all_agent_rows if item.get("task_id") != "__allocation__"]
    scheduler_agent_rows = [
        item for item in all_agent_rows if item.get("task_id") == "__allocation__"
    ]
    try:
        allocation_summary = json.loads(
            (run_dir / "allocation_summary.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        allocation_summary = None
    final = {
        "schema_version": "contextswarm_mini_run_v1",
        "status": status,
        "mode": config.mode,
        "communication": config.communication,
        "dataset": config.dataset_name,
        "score": sum(item["score"] for item in rows.values()),
        "max_score": len(rows),
        "verdicts": rows,
        "agents": agent_rows,
        "allocation_scheduler_agents": scheduler_agent_rows,
        "horizon_seconds": config.time_limit_seconds,
        "agent_timeout_count": sum(1 for item in agent_rows if item.get("timed_out")),
        "verdict_status_counts": {
            status: sum(1 for item in rows.values() if item.get("status") == status)
            for status in sorted({str(item.get("status")) for item in rows.values()})
        },
        "cps": dict(cps_summary or {"enabled": False}),
        "allocation": allocation_summary,
        "judge_result_cache": _judge_result_cache_evidence(run_dir, config),
        "health": dict(health or {"ok": status in {"COMPLETED", "DRY_RUN"}}),
        "score_time": _score_time_metrics(
            run_dir,
            horizon_seconds=config.time_limit_seconds,
            max_score=len(rows),
        ),
        "finished_at": utc_now(),
    }
    if config.allocation.policy in _FIGURE4_POLICIES:
        _write_figure4_summary(run_dir, config, verdicts, allocation_summary)
    (run_dir / "final.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_figure4_summary(
    run_dir: Path,
    config: ExperimentConfig,
    verdicts: Mapping[str, Verdict],
    allocation_summary: Mapping[str, Any] | None,
) -> None:
    """Emit the machine-readable per-repeat Figure 4 development artifact."""

    score_time = _score_time_metrics(
        run_dir,
        horizon_seconds=config.time_limit_seconds,
        max_score=len(verdicts),
    )
    try:
        history = [
            json.loads(line)
            for line in (run_dir / "scoreboard_history.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError):
        history = []
    accepted_history = [
        {
            "task_id": row.get("task_id"),
            "score": float(row.get("score") or 0.0),
            "horizon_elapsed_seconds": row.get("horizon_elapsed_seconds"),
        }
        for row in history
        if str(row.get("source") or "") != "closeout"
        and float(row.get("score") or 0.0) >= 1.0
    ]
    selected = getattr(config, "selection", None)
    selector_identity = selected.public_dict() if selected is not None else {
        "enabled": False,
        "selector_name": "",
        "selection_config_id": "",
    }
    contract = {
        "dataset": config.dataset_name,
        "ordered_task_ids": sorted(verdicts),
        "paired_seed": config.seed,
        "selector": selector_identity,
        "model": config.model,
        "horizon_seconds": config.time_limit_seconds,
        "total_capacity": config.max_parallel,
        "initial_agents_per_task": config.initial_agents_per_task,
        "communication": config.communication,
        "direct_messages_enabled": False,
        "candidate_transfer": True,
        "stopping_rule": "full_score_or_horizon",
    }
    contract_hash = hashlib.sha256(
        json.dumps(contract, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    summary = {
        "schema_version": "contextswarm_figure4_run_summary_v1",
        "policy": config.allocation.policy,
        "paired_repeat_id": str(config.extra.get("paired_repeat_id", "dev")),
        "paired_seed": config.seed,
        "ordered_task_ids": sorted(verdicts),
        "horizon_seconds": config.time_limit_seconds,
        "total_capacity": config.max_parallel,
        "initial_allocation": {
            task_id: config.initial_agents_per_task for task_id in sorted(verdicts)
        },
        "accepted_score_history": accepted_history,
        "final_accepted_score": sum(
            1 for verdict in verdicts.values() if float(verdict.score) >= 1.0
        ),
        "time_to_k_seconds": score_time.get("time_to_k_proofs_seconds", {}),
        "nauc": score_time.get("normalized_score_time_auc", 0.0),
        "solver_usage": {
            "solver_agent_seconds": (allocation_summary or {}).get("solver_agent_seconds", 0.0),
            "solver_slot_utilization": (allocation_summary or {}).get("solver_slot_utilization", 0.0),
        },
        "evaluator_usage": {
            "terminal_verdict_count": len(verdicts),
            "attempt_verdict_count": len(history),
        },
        "scheduler_cost": (allocation_summary or {}).get("scheduler_cost", {}),
        "allocation_metrics": {
            key: value
            for key, value in (allocation_summary or {}).items()
            if key in {"decision_count", "fallback_count", "agent_calls", "total_latency_seconds"}
        },
        "allocation_config": config.allocation.public_dict(),
        "comparison_contract": contract,
        "comparison_contract_sha256": contract_hash,
    }
    (run_dir / "figure4_run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _judge_result_cache_evidence(
    run_dir: Path,
    config: ExperimentConfig,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "required_disabled": config.lean_require_result_cache_disabled,
        "enabled": None,
    }
    try:
        preflight = json.loads(
            (run_dir / "transport_preflight.json").read_text(encoding="utf-8")
        )
        cache = preflight["lean"]["result_cache"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return evidence
    if isinstance(cache, Mapping) and isinstance(cache.get("enabled"), bool):
        evidence["enabled"] = cache["enabled"]
        for field in ("backend_ready", "requested_env_accepted"):
            if isinstance(cache.get(field), bool):
                evidence[field] = cache[field]
        backend = cache.get("backend")
        if isinstance(backend, str):
            evidence["backend"] = backend
    return evidence
