"""Experiment supervisor for Mono, Parallel, and CPS protocols."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import math
import os
from queue import Empty, Queue
from pathlib import Path
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
from .config import ConfigError, ExperimentConfig
from .cps import CPSStore, CommunicationPolicy, make_policy
from .evaluator import LeanEvaluator, MockEvaluator, sanitize_worker_text
from .elastic_scheduler import AgentAssignment, ElasticScheduler
from .judge_broker import CandidateSnapshot, JudgeBroker, JudgeBrokerDrainError
from .models import AgentResult, Task, Verdict
from .pi_agent import PiAgent
from .preflight import PreflightError, run_preflight
from .prompts import build_mono_prompt, build_task_prompt


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


def _verdict_priority(verdict: Verdict | None) -> tuple[int, float]:
    if verdict is None:
        return (-1, -1.0)
    status_rank = {
        "PROVED": 4,
        "COMPILES_WITH_SORRY": 2,
        "VERIFY_FAIL": 1,
        "LOCAL_REJECTED": 0,
        "RUNNING": 0,
        "OUT_OF_HORIZON": 0,
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
    }
)
_NONTERMINAL_VERDICT_STATUSES = frozenset(
    {"RUNNING", "QUEUED", "PENDING", "IN_PROGRESS", "STARTED", "UNKNOWN"}
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _runtime_provenance(*, mock_agent: bool) -> dict[str, str | bool]:
    """Bind formal artifacts to the immutable image that executed them."""

    image_revision = str(
        os.environ.get("CONTEXTSWARM_IMAGE_REVISION") or ""
    ).strip()
    baked_source_commit = str(
        os.environ.get("CONTEXTSWARM_SOURCE_COMMIT") or ""
    ).strip()
    image_id = str(os.environ.get("CONTEXTSWARM_IMAGE_ID") or "").strip()
    if (
        _SOURCE_COMMIT_RE.fullmatch(image_revision)
        and _SOURCE_COMMIT_RE.fullmatch(baked_source_commit)
        and image_revision == baked_source_commit
        and _IMAGE_ID_RE.fullmatch(image_id)
    ):
        return {"source_commit": image_revision, "image_id": image_id}
    if mock_agent:
        return {
            "source_commit": "test-only-mock-source",
            "image_id": "test-only-mock-image",
            "test_only": True,
        }
    raise ConfigError(
        "formal runs require a valid immutable image revision and image ID, "
        "with the image revision matching the baked source commit"
    )


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _expected_task_contract(evaluator: Any, task: Task) -> str:
    method = getattr(evaluator, "expected_task_contract_sha256", None)
    if not callable(method):
        raise ValueError("evaluator does not expose its expected task contract")
    value = str(method(task) or "").strip().lower()
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("evaluator returned an invalid expected task contract")
    return value


def _allows_mock_provenance(evaluator: Any) -> bool:
    return getattr(evaluator, "is_mock_evaluator", False) is True


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
    compute_seconds = solver_seconds + max(0.0, policy_latency_seconds)
    compute_utilization = compute_seconds / capacity_seconds if capacity_seconds else 0.0
    return {
        "solver_agent_seconds": round(solver_seconds, 6),
        "scheduler_compute_seconds": round(max(0.0, policy_latency_seconds), 6),
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
    tasks: list[Task] = []
    for slug in ids:
        root = dataset_root / slug
        try:
            metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
            problem_text = (root / "problem.md").read_text(encoding="utf-8")
            baseline_files = sorted((root / "baseline").glob("*.lean"))
        except OSError as exc:
            raise ConfigError(f"incomplete task {slug}: {exc}") from exc
        if not baseline_files:
            raise ConfigError(f"task {slug} has no baseline/*.lean")
        if not isinstance(metadata, dict):
            raise ConfigError(f"metadata is not an object: {root / 'metadata.json'}")
        tasks.append(
            Task(
                slug=slug,
                root=root,
                problem_text=problem_text,
                baseline_code=baseline_files[0].read_text(encoding="utf-8"),
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
    runtime_provenance = _runtime_provenance(mock_agent=mock_agent)
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

    if not mock_agent:
        try:
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
    evaluator = (
        MockEvaluator(prove_without_sorry=mock_proved)
        if mock_agent
        else LeanEvaluator(
            config.lean_server_url,
            lean_env_id=config.lean_env_id,
            timeout_seconds=config.lean_timeout_seconds,
            verification_profile=config.lean_verification_profile,
            judge_mode=config.lean_judge_mode,
        )
    )
    evaluator_gate = threading.BoundedSemaphore(config.lean_max_concurrent_evaluations)
    judge_broker = JudgeBroker(
        evaluator,
        evaluator_gate,
        audit_path=run_dir / "judge_checks.jsonl",
    ).start()
    (run_dir / "judge_broker_policy.json").write_text(
        json.dumps(judge_broker.public_policy(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    pi_agent = PiAgent(config, trace_path=run_dir / "pi_events.jsonl")
    agent_results: list[AgentResult] = []
    attempt_verdicts: list[Verdict] = []
    verdicts: dict[str, Verdict] = {}
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
            verdicts.update(mono_verdicts)
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
                current = verdicts.get(verdict.task_id)
                if _verdict_priority(verdict) >= _verdict_priority(current):
                    verdicts[verdict.task_id] = verdict
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
                verdicts[verdict.task_id] = verdict
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
            observed = {"active_handlers": -1, "fifo_depth": -1}
        broker_state = {"drained": False, **observed}

    broker_closeout = {
        "schema_version": "contextswarm_judge_broker_closeout_v1",
        "drained": broker_state.get("drained") is True,
        "active_handlers": int(broker_state.get("active_handlers", -1)),
        "fifo_depth": int(broker_state.get("fifo_depth", -1)),
    }
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

    if broker_failure is None and closeout_artifact_failure is None:
        logger.event("judge_broker_closed", **broker_closeout)
    elif isinstance(broker_failure, JudgeBrokerDrainError):
        logger.event("broker_drain_timeout", **broker_closeout)
    elif broker_failure is not None:
        logger.event("broker_close_error", **broker_closeout)
    else:
        logger.event(
            "broker_closeout_artifact_error",
            **(closeout_artifact_failure_fields or {}),
        )

    terminal_failure = run_failure or broker_failure or closeout_artifact_failure
    terminal_failure_fields = (
        run_failure_fields
        or broker_failure_fields
        or closeout_artifact_failure_fields
    )
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
        _stage_task(task, worker_dir / "tasks" / task.slug)
    _write_mono_bundle(worker_dir, tasks)
    prompt = build_mono_prompt(tasks, workspace=str(worker_dir), communication_enabled=False)
    early_lock = threading.RLock()
    early_proofs: dict[str, _EarlyProofCredit] = {}
    expected_contracts = {
        task.slug: _expected_task_contract(evaluator, task) for task in tasks
    }
    allow_mock_provenance = _allows_mock_provenance(evaluator)
    callback_failure = _CallbackFailureState()

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
                verified = worker_dir / "verified" / task.slug / "result.lean"
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
        except Exception:
            callback_failure.record()
            raise

    if mock_agent:
        result = _mock_result("mono", "bundle", 1)
    else:
        candidates = {
            task.slug: (task, worker_dir / "tasks" / task.slug / "result.lean")
            for task in tasks
        }
        with judge_broker.session(
            actor_id="mono",
            workdir=worker_dir,
            candidates=candidates,
            deadline_monotonic=deadline,
            on_authoritative_verdict=admit_early_proof,
            cancel_event=callback_failure,
        ) as broker_env:
            result = pi_agent.run(
                task_id="matholympiadbench-latest12",
                actor_id="mono",
                episode=1,
                prompt=prompt,
                workdir=worker_dir,
                extra_env=broker_env,
                deadline_monotonic=deadline,
                cancel_event=callback_failure,
            )
    callback_failure.raise_if_failed()
    logger.event("agent_finished", **result.as_dict())
    verdicts: dict[str, Verdict] = {}
    for task in tasks:
        candidate = worker_dir / "tasks" / task.slug / "result.lean"
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
            verdict = _evaluate_candidate(evaluator, task, candidate, deadline, evaluator_gate)
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
        best_path = best_dir / "result.lean"
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
    roster_path = run_dir / "actors.json"
    roster_lock = threading.RLock()
    allocation_lock = threading.RLock()
    roster: list[dict[str, Any]] = []
    roster_path.write_text("[]\n", encoding="utf-8")
    jobs: Queue[AgentAssignment] = Queue()
    results: list[tuple[AgentResult, Verdict]] = []
    results_lock = threading.RLock()
    scheduler_results_lock = threading.RLock()
    decision_index = 0
    initial_assignment_count = 0
    adaptive_assignments = 0
    callback_failure = _CallbackFailureState()

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

    if config.allocation.policy == "uniform":
        allocator: Any = UniformAllocationPolicy(task_order)
    elif config.allocation.policy == "formula":
        allocator = FormulaAllocationPolicy(task_order, config.allocation.formula)
    else:
        allocator = AgentAllocationPolicy(task_order, invoke_scheduler_agent)

    def build_snapshot(index: int) -> TaskProgressSnapshot:
        now_mono = time.monotonic()
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
            if piece_age is not None:
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
                scheduler.task_solved(task_id)
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
                    if unavailable:
                        scheduler.finish(assignment, solved=True)
                        continue
                    return accept_assignment(assignment, phase="initial")

                decision_index += 1
                snapshot = build_snapshot(decision_index)
                if not snapshot.eligible_task_ids:
                    return None
                if config.allocation.policy == "agent":
                    # A released solver slot can run its own read-only scheduler
                    # call.  Release only the orchestration lock while the model
                    # reasons so simultaneous completions keep all compute slots
                    # occupied; index/snapshot and final admission remain atomic.
                    allocation_lock.release()
                    try:
                        decision = allocator.choose(snapshot)
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
                return assignment
            return None
        finally:
            allocation_lock.release()

    def prepare_workspace(state: _ElasticTaskState, assignment: AgentAssignment) -> tuple[Path, Path]:
        workdir = state.task_root / "agents" / assignment.agent_id
        _stage_task(state.task, workdir)
        assert state.best_candidate is not None
        with state.lock:
            shutil.copy2(state.best_candidate, workdir / "result.lean")
        return workdir, state.best_candidate

    def execute_assignment(assignment: AgentAssignment) -> tuple[AgentResult, Verdict, bool]:
        callback_failure.raise_if_failed()
        state = states[assignment.task_id]
        workdir, best_path = prepare_workspace(state, assignment)
        actor = assignment.agent_id
        task = state.task
        candidate_path = workdir / "result.lean"

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
                    return True

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
            )
            return result, verdict, False

        digest = policy.digest(task.slug, actor, query=task.theorem_name)
        prompt = build_task_prompt(
            task,
            task_workspace=str(workdir),
            agent_id=actor,
            episode=assignment.generation,
            communication_enabled=policy.enabled,
            digest=digest,
        )
        prompt += (
            "\n\nElastic CPS handoff:\n"
            "The runner has pre-seeded result.lean with the strongest usable candidate "
            "from earlier assignments on this task. Keep your candidate in result.lean; "
            "the runner will merge the strongest verified candidate."
        )

        if mock_agent:
            result = _mock_result(actor, task.slug, assignment.generation)
        else:
            with judge_broker.session(
                actor_id=actor,
                workdir=workdir,
                candidates={task.slug: (task, workdir / "result.lean")},
                deadline_monotonic=deadline,
                cps_store=store,
                communication=config.communication,
                roster_path=roster_path,
                on_authoritative_verdict=admit_early_proof,
                cancel_event=state.cancel_event,
            ) as broker_env:
                result = pi_agent.run(
                    task_id=task.slug,
                    actor_id=actor,
                    episode=assignment.generation,
                    prompt=prompt,
                    workdir=workdir,
                    extra_env=broker_env,
                    deadline_monotonic=deadline,
                    cancel_event=state.cancel_event,
                )
        callback_failure.raise_if_failed()
        logger.event("agent_finished", **result.as_dict())

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
                workdir / "result.lean",
                deadline,
                evaluator_gate,
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
            )
        logger.scoreboard(verdict, episode=assignment.generation, agent_id=actor)
        logger.event(
            "evaluation_finished",
            **verdict.as_dict(),
            agent_id=actor,
            episode=assignment.generation,
            source="final_evaluation",
            scoreboard_recorded=True,
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

    def worker_loop() -> None:
        while True:
            try:
                assignment = jobs.get(timeout=0.2)
            except Empty:
                if callback_failure.failed or time.monotonic() >= deadline or scheduler.done:
                    return
                continue
            try:
                result, verdict, solved = execute_assignment(assignment)
                with results_lock:
                    results.append((result, verdict))
                with allocation_lock:
                    scheduler.finish(assignment, solved=solved)
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
                with allocation_lock:
                    scheduler.finish(assignment, solved=False)
                return
            finally:
                jobs.task_done()

    if initial_assignments:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(worker_loop) for _ in range(worker_count)]
            for future in futures:
                future.result()

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

    def execute(task: Task) -> tuple[AgentResult, Verdict]:
        workdir = run_dir / "workers" / task.slug
        _stage_task(task, workdir)
        best_result: AgentResult | None = None
        best_verdict: Verdict | None = None
        actor = f"worker-{task.slug}-e0"
        early_lock = threading.RLock()
        early_credit: _EarlyProofCredit | None = None
        expected_contract = _expected_task_contract(evaluator, task)
        allow_mock_provenance = _allows_mock_provenance(evaluator)

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
                    verified = workdir / "verified" / "result.lean"
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
                digest=digest,
            )
            if mock_agent:
                result = _mock_result(actor, task.slug, episode)
            else:
                with judge_broker.session(
                    actor_id=actor,
                    workdir=workdir,
                    candidates={task.slug: (task, workdir / "result.lean")},
                    deadline_monotonic=deadline,
                    cps_store=policy.store if policy.enabled else None,
                    communication=config.communication if policy.enabled else "none",
                    roster_path=(run_dir / "actors.json") if policy.enabled else None,
                    on_authoritative_verdict=admit_early_proof,
                    cancel_event=callback_failure,
                ) as broker_env:
                    result = pi_agent.run(
                        task_id=task.slug,
                        actor_id=actor,
                        episode=episode,
                        prompt=prompt,
                        workdir=workdir,
                        extra_env=broker_env,
                        deadline_monotonic=deadline,
                        cancel_event=callback_failure,
                    )
            callback_failure.raise_if_failed()
            logger.event("agent_finished", **result.as_dict())
            with early_lock:
                credit = early_credit if early_credit and early_credit.episode == episode else None
            if credit is not None:
                _atomic_promote_source(
                    credit.candidate_source,
                    workdir / "result.lean",
                    credit.candidate_sha256,
                )
                verdict = credit.verdict
            else:
                verdict = _evaluate_candidate(
                    evaluator,
                    task,
                    workdir / "result.lean",
                    deadline,
                    evaluator_gate,
                )
                verdict = _within_horizon(verdict, deadline)
                verdict = _enforce_verdict_provenance(
                    verdict,
                    workdir / "result.lean",
                    expected_task_contract_sha256=expected_contract,
                    allow_mock_provenance=allow_mock_provenance,
                )
                logger.scoreboard(verdict, episode=episode, agent_id=actor)
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


def _stage_task(task: Task, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "problem.md").write_text(task.problem_text, encoding="utf-8")
    baseline_dir = destination / "baseline"
    baseline_dir.mkdir(exist_ok=True)
    baseline_source = next(iter(sorted(task.root.glob("baseline/*.lean"))))
    shutil.copy2(baseline_source, baseline_dir / baseline_source.name)
    (destination / "metadata.json").write_text(
        json.dumps(task.metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = destination / "result.lean"
    if not result.exists():
        shutil.copy2(baseline_source, result)


def _within_horizon(verdict: Verdict, deadline: float) -> Verdict:
    if time.monotonic() <= deadline:
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
) -> Verdict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return Verdict(task.slug, "OUT_OF_HORIZON", 0.0, 0.0, {"reason": "run_horizon_elapsed"})
    # Evaluator contention is part of the shared experiment horizon.  A fixed
    # admission timeout can reject a candidate while substantial run time is
    # still available, especially in high-concurrency cells.  Wait for the
    # gate for exactly the remaining horizon instead.
    if not gate.acquire(timeout=remaining):
        return Verdict(task.slug, "OUT_OF_HORIZON", 0.0, 0.0, {"reason": "evaluator_admission_horizon_elapsed"})
    try:
        return evaluator.evaluate(task, candidate, deadline_monotonic=deadline)
    finally:
        gate.release()


def _write_mono_bundle(worker_dir: Path, tasks: Iterable[Task]) -> None:
    solutions: dict[str, str] = {}
    for task in tasks:
        candidate = worker_dir / "tasks" / task.slug / "result.lean"
        try:
            solutions[task.slug] = candidate.read_text(encoding="utf-8")
        except OSError:
            solutions[task.slug] = ""
    (worker_dir / "result.json").write_text(
        json.dumps(
            {"schema_version": "formal_lean_single_run_bundle_v1", "solutions": solutions},
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
        if status in _INFRASTRUCTURE_VERDICT_STATUSES:
            issues.add("evaluator_infrastructure_error")
        if status in _NONTERMINAL_VERDICT_STATUSES:
            issues.add("nonterminal_verdict")
        if status == "PROVENANCE_INVALID":
            issues.add("verdict_provenance_invalid")
    if len(verdicts) != expected_task_count:
        issues.add("final_task_bundle_incomplete")

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
        str(row.get("event") or "") in {"run_error", "elastic_worker_error", "preflight_failed"}
        for row in events
    )
    if worker_errors:
        issues.add("runner_or_worker_error")

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
        "dataset": "matholympiadbench",
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
    (run_dir / "final.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
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
