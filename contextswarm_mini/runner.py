"""Experiment supervisor for Mono, Parallel, and CPS protocols."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import datetime as dt
import json
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
)
from .config import ConfigError, ExperimentConfig
from .cps import CPSStore, CommunicationPolicy, make_policy
from .evaluator import LeanEvaluator, MockEvaluator
from .elastic_scheduler import AgentAssignment, ElasticScheduler
from .models import AgentResult, Task, Verdict
from .pi_agent import PiAgent
from .preflight import PreflightError, run_preflight
from .prompts import build_mono_prompt, build_task_prompt


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass
class RunLogger:
    output_dir: Path
    lock: threading.Lock

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def event(self, event_type: str, **payload: Any) -> None:
        row = {"at": utc_now(), "event": event_type, **payload}
        with self.lock:
            with (self.output_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def scoreboard(self, verdict: Verdict, *, episode: int, agent_id: str) -> None:
        row = {
            "at": utc_now(),
            "task_id": verdict.task_id,
            "episode": episode,
            "agent_id": agent_id,
            **verdict.as_dict(),
        }
        with self.lock:
            with (self.output_dir / "scoreboard_history.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


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


def _verdict_priority(verdict: Verdict | None) -> tuple[int, float]:
    if verdict is None:
        return (-1, -1.0)
    status_rank = {
        "PROVED": 4,
        "AC": 4,
        "PASSED": 4,
        "COMPILES_WITH_SORRY": 2,
        "VERIFY_FAIL": 1,
        "CHEATING": 1,
        "LOCAL_REJECTED": 0,
        "RUNNING": 0,
        "OUT_OF_HORIZON": 0,
    }
    return (status_rank.get(verdict.status, 0), float(verdict.score))


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


def _score_time_metrics(run_dir: Path, *, horizon_seconds: float, max_score: int) -> dict[str, Any]:
    try:
        meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
        started = dt.datetime.fromisoformat(str(meta["started_at"]).replace("Z", "+00:00"))
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
            at = dt.datetime.fromisoformat(str(row.get("at") or "").replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        elapsed = min(max(0.0, (at - started).total_seconds()), max(0.0, horizon_seconds))
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
        "lean_server_url": config.public_dict().get("lean_server_url", ""),
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
            logger.event("preflight_failed", error=str(exc))
            _write_final(run_dir, config, {}, [], status="PREFLIGHT_FAILED", cps_summary=None)
            raise

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
    pi_agent = PiAgent(config, trace_path=run_dir / "pi_events.jsonl")
    run_deadline = time.monotonic() + config.time_limit_seconds
    agent_results: list[AgentResult] = []
    verdicts: dict[str, Verdict] = {}
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
            )
            agent_results.append(mono_result)
            verdicts.update(mono_verdicts)
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
            )
            for result, verdict in results:
                agent_results.append(result)
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
            )
            for result, verdict in results:
                agent_results.append(result)
                verdicts[verdict.task_id] = verdict
    except Exception as exc:  # preserve closeout artifacts for interrupted cells
        logger.event("run_error", error=str(exc), traceback=traceback.format_exc()[-4_000:])
        _write_final(run_dir, config, verdicts, agent_results, status="ERROR", cps_summary=store.summary() if store else None)
        raise
    status = "COMPLETED" if all(verdict.status != "EVALUATOR_ERROR" for verdict in verdicts.values()) else "DEGRADED"
    if store is not None:
        store.export_events(run_dir / "communication_trace.jsonl")
    _write_final(run_dir, config, verdicts, agent_results, status=status, cps_summary=store.summary() if store else None)
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
) -> tuple[AgentResult, dict[str, Verdict]]:
    worker_dir = run_dir / "workers" / "mono"
    worker_dir.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        _stage_task(task, worker_dir / "tasks" / task.slug)
    _write_mono_bundle(worker_dir, tasks)
    prompt = build_mono_prompt(tasks, workspace=str(worker_dir), communication_enabled=False)
    if mock_agent:
        result = _mock_result("mono", "bundle", 1)
    else:
        result = pi_agent.run(
            task_id="matholympiadbench-latest12",
            actor_id="mono",
            episode=1,
            prompt=prompt,
            workdir=worker_dir,
            deadline_monotonic=deadline,
        )
    logger.event("agent_finished", **result.as_dict())
    _write_mono_bundle(worker_dir, tasks)
    verdicts: dict[str, Verdict] = {}
    for task in tasks:
        candidate = worker_dir / "tasks" / task.slug / "result.lean"
        verdict = _evaluate_candidate(evaluator, task, candidate, deadline, evaluator_gate)
        verdict = _within_horizon(verdict, deadline)
        verdicts[task.slug] = verdict
        logger.scoreboard(verdict, episode=1, agent_id="mono")
        logger.event("evaluation_finished", **verdict.as_dict(), agent_id="mono", episode=1)
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
) -> list[tuple[AgentResult, Verdict]]:
    """Run one fixed CPS computation substrate with selectable slot allocation."""

    run_started_monotonic = deadline - config.time_limit_seconds
    states: dict[str, _ElasticTaskState] = {}
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
    decision_index = 0
    initial_assignment_count = 0
    adaptive_assignments = 0

    assert policy.store is not None
    store = policy.store

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
            return result
        workdir = run_dir / "allocation_scheduler" / f"decision-{index:06d}"
        workdir.mkdir(parents=True, exist_ok=True)
        scheduler_deadline = min(
            deadline,
            time.monotonic() + config.allocation.agent_timeout_seconds,
        )
        return pi_agent.run(
            task_id="__allocation__",
            actor_id=actor_id,
            episode=index,
            prompt=prompt,
            workdir=workdir,
            deadline_monotonic=scheduler_deadline,
            isolated=True,
        )

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
                    best_status=best.status if best is not None else "NONE",
                    best_score=float(best.score) if best is not None else 0.0,
                    last_verdict_status=last_status,
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
    ) -> None:
        row = {
            "at": utc_now(),
            **decision.as_dict(snapshot=snapshot),
            "assigned_agent_id": assignment.agent_id if assignment is not None else None,
            "assigned_generation": assignment.generation if assignment is not None else None,
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
            assigned_agent_id=assignment.agent_id if assignment is not None else None,
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
        with allocation_lock:
            while time.monotonic() < deadline:
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
                decision = allocator.choose(snapshot)
                assignment = None
                execution_snapshot: TaskProgressSnapshot | None = None
                if time.monotonic() < deadline and decision.selected_task_id:
                    assignment = scheduler.next_assignment_for(decision.selected_task_id)
                if assignment is None and time.monotonic() < deadline:
                    execution_snapshot = build_snapshot(decision_index)
                    if execution_snapshot.eligible_task_ids:
                        decision = allocator.fallback(
                            execution_snapshot,
                            "selected task became ineligible before admission",
                            prior=decision,
                        )
                        if decision.selected_task_id:
                            assignment = scheduler.next_assignment_for(decision.selected_task_id)
                if assignment is None:
                    if not decision.fallback:
                        decision.fallback = True
                        decision.fallback_reason = "run horizon reached before admission"
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

    def prepare_workspace(state: _ElasticTaskState, assignment: AgentAssignment) -> tuple[Path, Path]:
        workdir = state.task_root / "agents" / assignment.agent_id
        _stage_task(state.task, workdir)
        assert state.best_candidate is not None
        with state.lock:
            shutil.copy2(state.best_candidate, workdir / "result.lean")
        return workdir, state.best_candidate

    def execute_assignment(assignment: AgentAssignment) -> tuple[AgentResult, Verdict, bool]:
        state = states[assignment.task_id]
        workdir, best_path = prepare_workspace(state, assignment)
        actor = assignment.agent_id
        task = state.task
        if state.solved:
            result = _mock_result(actor, task.slug, assignment.generation)
            verdict = Verdict(task.slug, "CANCELLED", 0.0, 0.0, {"reason": "task_already_solved"})
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
            f"Other agents on this task may have left a stronger candidate at {best_path}. "
            "Read it before editing result.lean. Keep your candidate in result.lean; "
            "the runner will merge the strongest verified candidate."
        )
        db_path = str(store.path)
        env = {
            "CONTEXTSWARM_CPS_DB": db_path,
            "CONTEXTSWARM_ASSIGNMENT_FILE": str(assignments_path),
            "CONTEXTSWARM_BEST_CANDIDATE_FILE": str(best_path),
            "CONTEXTSWARM_TASK_ROOT": str(state.task_root),
        }
        if policy.enabled:
            _write_context_piece_wrapper(workdir)
            env["CONTEXTSWARM_ACTORS_FILE"] = str(roster_path)

        if mock_agent:
            result = _mock_result(actor, task.slug, assignment.generation)
        else:
            result = pi_agent.run(
                task_id=task.slug,
                actor_id=actor,
                episode=assignment.generation,
                prompt=prompt,
                workdir=workdir,
                extra_env=env,
                deadline_monotonic=deadline,
                cancel_event=state.cancel_event,
            )
        logger.event("agent_finished", **result.as_dict())

        if result.cancelled or state.solved:
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

        logger.scoreboard(verdict, episode=assignment.generation, agent_id=actor)
        logger.event(
            "evaluation_finished",
            **verdict.as_dict(),
            agent_id=actor,
            episode=assignment.generation,
        )

        solved = verdict.score >= 1.0
        feedback = _allocation_feedback(verdict)
        with state.lock:
            prior_priority = _verdict_priority(state.best_verdict)
            candidate_is_usable = verdict.status not in {
                "LOCAL_REJECTED",
                "OUT_OF_HORIZON",
                "CANCELLED",
                "RUNNING",
            }
            improved = candidate_is_usable and _verdict_priority(verdict) > prior_priority
            state.completed_attempts += 1
            state.last_verdict_status = verdict.status
            state.last_feedback = feedback
            if improved or solved:
                state.last_progress_at = time.monotonic()
                state.consecutive_failures = 0
            elif candidate_is_usable:
                state.consecutive_failures += 1
            if candidate_is_usable and _verdict_priority(verdict) >= prior_priority:
                state.best_verdict = verdict
                try:
                    shutil.copy2(workdir / "result.lean", best_path)
                except OSError:
                    pass
            if solved:
                state.solved = True
                if config.cancel_on_proved:
                    state.cancel_event.set()
        if solved:
            scheduler.task_solved(task.slug)

        if policy.enabled:
            policy.publish(
                task.slug,
                actor,
                kind="validation_result",
                title=f"attempt {assignment.generation}: {verdict.status}",
                body=(
                    f"Evaluator status={verdict.status}; score={verdict.score}. "
                    f"Feedback: {feedback[:1200]} Shared candidate: {best_path}"
                ),
            )
        return result, verdict, solved

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
                if time.monotonic() >= deadline or scheduler.done:
                    return
                continue
            try:
                result, verdict, solved = execute_assignment(assignment)
                with results_lock:
                    results.append((result, verdict))
                scheduler.finish(assignment, solved=solved)
                replacement = claim_next()
                if replacement is not None:
                    jobs.put(replacement)
            except Exception as exc:  # keep one failed worker from wedging the pool
                logger.event(
                    "elastic_worker_error",
                    task_id=assignment.task_id,
                    agent_id=assignment.agent_id,
                    error=str(exc),
                    traceback=traceback.format_exc()[-2_000:],
                )
                scheduler.finish(assignment, solved=False)
                replacement = claim_next()
                if replacement is not None:
                    jobs.put(replacement)
            finally:
                jobs.task_done()

    if initial_assignments:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(worker_loop) for _ in range(worker_count)]
            for future in futures:
                future.result()

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
) -> list[tuple[AgentResult, Verdict]]:
    def execute(task: Task) -> tuple[AgentResult, Verdict]:
        workdir = run_dir / "workers" / task.slug
        _stage_task(task, workdir)
        db_path = str(policy.store.path) if policy.store is not None else ""
        best_result: AgentResult | None = None
        best_verdict: Verdict | None = None
        actor = f"worker-{task.slug}-e0"
        for episode in range(1, config.episodes_per_task + 1):
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
            env = {"CONTEXTSWARM_CPS_DB": db_path} if db_path else {}
            if policy.enabled:
                env["CONTEXTSWARM_ACTORS_FILE"] = str(run_dir / "actors.json")
            if policy.enabled:
                _write_context_piece_wrapper(workdir)
            if mock_agent:
                result = _mock_result(actor, task.slug, episode)
            else:
                result = pi_agent.run(
                    task_id=task.slug,
                    actor_id=actor,
                    episode=episode,
                    prompt=prompt,
                    workdir=workdir,
                    extra_env=env,
                    deadline_monotonic=deadline,
                )
            logger.event("agent_finished", **result.as_dict())
            verdict = _evaluate_candidate(
                evaluator,
                task,
                workdir / "result.lean",
                deadline,
                evaluator_gate,
            )
            verdict = _within_horizon(verdict, deadline)
            logger.scoreboard(verdict, episode=episode, agent_id=actor)
            logger.event("evaluation_finished", **verdict.as_dict(), agent_id=actor, episode=episode)
            best_result, best_verdict = result, verdict
            if policy.enabled:
                feedback = verdict.error or str(
                    verdict.response.get("error_message")
                    or verdict.response.get("reason")
                    or verdict.status
                )
                policy.publish(
                    task.slug,
                    actor,
                    kind="validation_result",
                    title=f"episode {episode}: {verdict.status}",
                    body=f"Evaluator status={verdict.status}; score={verdict.score}. Feedback: {feedback[:1200]}",
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


def _write_context_piece_wrapper(workdir: Path) -> None:
    wrapper = workdir / "context_piece"
    wrapper.write_text(
        "#!/bin/sh\nexec python3 -m contextswarm_mini.context_piece \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


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


def _write_final(
    run_dir: Path,
    config: ExperimentConfig,
    verdicts: Mapping[str, Verdict],
    agent_results: Iterable[AgentResult],
    *,
    status: str,
    cps_summary: Mapping[str, Any] | None,
) -> None:
    rows = {key: value.as_dict() for key, value in sorted(verdicts.items())}
    agent_rows = [item.as_dict() for item in agent_results]
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
        "horizon_seconds": config.time_limit_seconds,
        "agent_timeout_count": sum(1 for item in agent_rows if item.get("timed_out")),
        "verdict_status_counts": {
            status: sum(1 for item in rows.values() if item.get("status") == status)
            for status in sorted({str(item.get("status")) for item in rows.values()})
        },
        "cps": dict(cps_summary or {"enabled": False}),
        "allocation": allocation_summary,
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
