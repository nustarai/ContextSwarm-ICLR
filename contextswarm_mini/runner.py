"""Experiment supervisor for Mono, Parallel, and CPS protocols."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
from queue import Empty, Queue
from pathlib import Path
import shutil
import threading
import time
import traceback
import uuid
from typing import Any, Iterable, Mapping

from .artifacts import append_jsonl, atomic_write_bytes, atomic_write_json, atomic_write_text
from .config import ConfigError, ExperimentConfig
from .cps import CPSStore, CommunicationPolicy, make_policy
from .evaluator import LeanEvaluator, MockEvaluator
from .evaluator_broker import EvaluatorBroker
from .elastic_scheduler import AgentAssignment, ElasticScheduler
from .formal_tools import PUBLIC_FILES_FILENAME, stage_worker_tools
from .models import AgentResult, Task, Verdict
from .pi_agent import PiAgent
from .preflight import PreflightError, run_preflight
from .prompts import build_mono_prompt, build_task_prompt
from .secure_io import read_regular_bytes


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
        append_jsonl(self.output_dir / "events.jsonl", row, lock=self.lock)

    def scoreboard(self, verdict: Verdict, *, episode: int, agent_id: str) -> None:
        row = {
            "at": utc_now(),
            "task_id": verdict.task_id,
            "episode": episode,
            "agent_id": agent_id,
            **verdict.as_dict(),
        }
        append_jsonl(self.output_dir / "scoreboard_history.jsonl", row, lock=self.lock)

    def agent_evaluation(self, verdict: Verdict, *, episode: int, agent_id: str) -> None:
        """Diagnostic lane only; this file is never the official scoreboard."""

        row = {
            "at": utc_now(),
            "task_id": verdict.task_id,
            "episode": episode,
            "agent_id": agent_id,
            "authority": "agent_local_diagnostic",
            **verdict.as_dict(),
        }
        append_jsonl(self.output_dir / "agent_evaluation_history.jsonl", row, lock=self.lock)


@dataclass
class _ElasticTaskState:
    """Run-local state for multiple agents collaborating on one task."""

    task: Task
    task_root: Path
    lock: threading.RLock = field(default_factory=threading.RLock)
    attempts: int = 0
    solved: bool = False
    best_verdict: Verdict | None = None
    best_candidate: Path | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class _FrozenCandidate:
    task_id: str
    path: Path
    sha256: str | None
    error: str | None = None


def _verdict_priority(verdict: Verdict | None) -> tuple[int, float]:
    if verdict is None:
        return (-1, -1.0)
    status_rank = {
        "PROVED": 4,
        "COMPILES_WITH_SORRY": 2,
        "VERIFY_FAIL": 1,
        "CHEATING": 1,
        "UNEVALUATED_CANDIDATE": 0,
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
    return (status_rank.get(verdict.status, 0), float(verdict.score))


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
        "planned_agent_sessions": sessions,
        "backend": "nurouter_pi" if config.aisw_enabled else "pi",
        "model": config.model,
        "thinking": config.thinking,
        "lean_server_url": config.public_dict().get("lean_server_url", ""),
        "lean_env_id": config.lean_env_id,
        "lean_timeout_seconds": config.lean_timeout_seconds,
        "lean_max_lifecycle_seconds": config.lean_max_lifecycle_seconds,
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
    atomic_write_json(run_dir / "run_meta.json", manifest_snapshot)
    logger.event("run_started", run_id=run_id, **plan(config, tasks))
    if dry_run:
        atomic_write_json(run_dir / "dry_run.json", plan(config, tasks))
        logger.event("dry_run_finished")
        _write_final(
            run_dir,
            config,
            tasks,
            {},
            [],
            status="DRY_RUN",
            cps_summary=None,
        )
        return run_dir

    if not mock_agent:
        try:
            run_preflight(config, run_dir)
        except PreflightError as exc:
            logger.event("preflight_failed", error=str(exc))
            _write_final(
                run_dir,
                config,
                tasks,
                {},
                [],
                status="PREFLIGHT_FAILED",
                cps_summary=None,
            )
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
            max_lifecycle_seconds=config.lean_max_lifecycle_seconds,
            verification_profile=config.lean_verification_profile,
            judge_mode=config.lean_judge_mode,
        )
    )
    pi_agent = PiAgent(config, trace_path=run_dir / "pi_events.jsonl")
    run_deadline = time.monotonic() + config.time_limit_seconds
    broker = EvaluatorBroker(
        config,
        tasks,
        run_dir,
        evaluator,
        solver_deadline_monotonic=run_deadline,
    )
    broker.start()
    agent_results: list[AgentResult] = []
    verdicts: dict[str, Verdict] = {}
    try:
        if config.mode == "mono":
            mono_result = _run_mono(
                config,
                tasks,
                run_dir,
                logger,
                pi_agent,
                broker,
                mock_agent=mock_agent,
                deadline=run_deadline,
            )
            agent_results.append(mono_result)
        elif config.uses_cps:
            results = _run_elastic_cps(
                config,
                tasks,
                run_dir,
                logger,
                broker,
                pi_agent,
                policy,
                mock_agent=mock_agent,
                deadline=run_deadline,
            )
            agent_results.extend(results)
        else:
            results = _run_task_workers(
                config,
                tasks,
                run_dir,
                logger,
                pi_agent,
                policy,
                broker,
                mock_agent=mock_agent,
                deadline=run_deadline,
            )
            agent_results.extend(results)

        logger.event(
            "horizon_closed",
            reason="deadline_elapsed" if time.monotonic() >= run_deadline else "solver_completed",
        )
        frozen = _freeze_closeout_candidates(config, tasks, run_dir, logger, broker)
        broker.begin_closeout()
        verdicts = _run_closeout(
            config,
            tasks,
            frozen,
            logger,
            broker,
        )
    except Exception as exc:  # preserve closeout artifacts for interrupted cells
        logger.event("run_error", error=str(exc), traceback=traceback.format_exc()[-4_000:])
        _write_final(
            run_dir,
            config,
            tasks,
            verdicts,
            agent_results,
            status="ERROR",
            cps_summary=store.summary() if store else None,
        )
        raise
    finally:
        broker.close()
    degraded_statuses = {
        "BUDGET_EXHAUSTED",
        "CANCELLED",
        "EVALUATOR_ERROR",
        "EVALUATOR_TIMEOUT",
        "EXECUTION_TIMEOUT",
        "INFRASTRUCTURE_ERROR",
        "MISSING_CANDIDATE",
        "NETWORK_ERROR",
        "OFFICIAL_VERDICT_MISSING",
        "OUT_OF_HORIZON",
        "REJECTED_OVERLOADED",
        "RESOURCE_LIMIT",
    }
    if len(verdicts) != len(tasks):
        status = "INCOMPLETE"
    else:
        status = (
            "COMPLETED"
            if all(verdict.status not in degraded_statuses for verdict in verdicts.values())
            else "DEGRADED"
        )
    if store is not None:
        store.export_events(run_dir / "communication_trace.jsonl")
    _write_final(
        run_dir,
        config,
        tasks,
        verdicts,
        agent_results,
        status=status,
        cps_summary=store.summary() if store else None,
    )
    logger.event("run_finished", status=status, score=sum(v.score for v in verdicts.values()))
    return run_dir


def _run_mono(
    config: ExperimentConfig,
    tasks: list[Task],
    run_dir: Path,
    logger: RunLogger,
    pi_agent: PiAgent,
    broker: EvaluatorBroker,
    *,
    mock_agent: bool,
    deadline: float,
) -> AgentResult:
    worker_dir = run_dir / "workers" / "mono"
    worker_dir.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        _stage_task(
            task,
            worker_dir / "tasks" / task.slug,
            broker=broker,
            actor_id="mono",
            context_piece_enabled=False,
        )
    _write_mono_bundle(
        worker_dir,
        tasks,
        max_candidate_bytes=config.formal_tools_max_candidate_bytes,
    )
    atomic_write_text(
        worker_dir / PUBLIC_FILES_FILENAME,
        (
            "# Mono public bundle\n\n"
            "This one baseline session intentionally sees all selected task directories.\n"
            "The aggregate `result.json` is readable and is regenerated by the runner.\n"
            "Each `tasks/<slug>/PUBLIC_FILES.md` defines that task's public files and formal tools.\n"
            "Write only `tasks/<slug>/result.lean`; no CPS surface is present.\n"
        ),
        mode=0o444,
    )
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
    _write_mono_bundle(
        worker_dir,
        tasks,
        max_candidate_bytes=config.formal_tools_max_candidate_bytes,
    )
    return result


def _run_elastic_cps(
    config: ExperimentConfig,
    tasks: list[Task],
    run_dir: Path,
    logger: RunLogger,
    broker: EvaluatorBroker,
    pi_agent: PiAgent,
    policy: CommunicationPolicy,
    *,
    mock_agent: bool,
    deadline: float,
) -> list[AgentResult]:
    """Run CPS with an elastic, task-aware agent pool.

    A task receives ``initial_agents_per_task`` leases initially.  When a
    lease finishes, the scheduler assigns the freed slot to an unfinished
    task, preferring tasks below their initial quota and then using the
    scheduler's fair round-robin retry order.  Every attempt has its own
    workspace; a task-local best candidate is copied into the next attempt so
    agents can combine CPS messages with concrete code rather than racing on a
    single ``result.lean`` file.
    """

    states: dict[str, _ElasticTaskState] = {}
    for task in tasks:
        task_root = run_dir / "workers" / task.slug
        state = _ElasticTaskState(task=task, task_root=task_root)
        best_dir = task_root / "best"
        best_dir.mkdir(parents=True, exist_ok=True)
        best_path = best_dir / "result.lean"
        if not best_path.exists():
            atomic_write_text(best_path, task.baseline_code, mode=0o600)
        state.best_candidate = best_path
        states[task.slug] = state

    scheduler = ElasticScheduler(
        [task.slug for task in tasks],
        max_parallel=config.max_parallel,
        initial_agents=config.initial_agents_per_task,
        horizon=max(0.0, deadline - time.monotonic()),
        assignment_policy=config.assignment_policy,
    )
    assignments_path = run_dir / "elastic_assignments.jsonl"
    roster_path = run_dir / "actors.json"
    roster_lock = threading.RLock()
    roster: list[dict[str, Any]] = []
    atomic_write_text(roster_path, "[]\n")
    horizon_epoch_ms = int((time.time() + max(0.0, deadline - time.monotonic())) * 1_000)
    jobs: Queue[AgentAssignment] = Queue()
    results: list[AgentResult] = []
    results_lock = threading.RLock()
    evaluation_backlog_limit = max(
        2,
        config.max_parallel + config.lean_max_concurrent_evaluations,
    )
    evaluation_backlog_gate = threading.BoundedSemaphore(evaluation_backlog_limit)

    def record_assignment(assignment: AgentAssignment) -> None:
        row = {
            "at": utc_now(),
            "event": "agent_assigned",
            "task_id": assignment.task_id,
            "agent_id": assignment.agent_id,
            "generation": assignment.generation,
            "admitted_at": assignment.admitted_at,
        }
        append_jsonl(assignments_path, row, lock=roster_lock)
        with roster_lock:
            roster.append(
                {
                    "actor_id": assignment.agent_id,
                    "task_id": assignment.task_id,
                    "episode": assignment.generation,
                }
            )
            atomic_write_text(
                roster_path,
                json.dumps(roster, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        logger.event(
            "agent_assigned",
            task_id=assignment.task_id,
            agent_id=assignment.agent_id,
            episode=assignment.generation,
            active_slots=scheduler.active_slots,
        )

    def claim_next() -> AgentAssignment | None:
        """Claim a lease, respecting an optional per-task attempt cap."""
        while time.monotonic() < deadline:
            assignment = scheduler.next_assignment()
            if assignment is None:
                return None
            state = states[assignment.task_id]
            with state.lock:
                exhausted = (
                    config.max_attempts_per_task > 0
                    and state.attempts >= config.max_attempts_per_task
                )
                if not exhausted:
                    state.attempts += 1
            if exhausted:
                # The lease was only a probe for a task which reached its
                # configured attempt budget.  Retire it immediately and stop
                # future admissions for this task; no process was launched
                # for this assignment.  Budget exhaustion is not a proof and
                # must not cancel already-running attempts.
                scheduler.finish(
                    assignment,
                    retire_reason="attempt_budget_exhausted",
                )
                logger.event(
                    "task_attempt_budget_exhausted",
                    task_id=assignment.task_id,
                    max_attempts=config.max_attempts_per_task,
                )
                continue
            record_assignment(assignment)
            return assignment
        return None

    def prepare_workspace(state: _ElasticTaskState, assignment: AgentAssignment) -> tuple[Path, Path]:
        workdir = state.task_root / "agents" / assignment.agent_id
        _stage_task(
            state.task,
            workdir,
            broker=broker,
            actor_id=assignment.agent_id,
            context_piece_enabled=policy.enabled,
        )
        assert state.best_candidate is not None
        with state.lock:
            shutil.copy2(state.best_candidate, workdir / "result.lean")
            (workdir / "result.lean").chmod(0o600)
        return workdir, state.best_candidate

    def execute_assignment(assignment: AgentAssignment) -> tuple[AgentResult, Path | None]:
        state = states[assignment.task_id]
        workdir, best_path = prepare_workspace(state, assignment)
        actor = assignment.agent_id
        task = state.task
        if state.solved:
            result = _mock_result(actor, task.slug, assignment.generation)
            return result, None

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
            "Other agents on this task may have left a stronger candidate at "
            "../../best/result.lean. "
            "Read it before editing result.lean. Keep your candidate in result.lean; "
            "the runner will merge the strongest verified candidate."
        )
        db_path = str(policy.store.path) if policy.store is not None else ""
        env = {
            "CONTEXTSWARM_CPS_DB": db_path,
            "CONTEXTSWARM_ASSIGNMENT_FILE": str(assignments_path),
            "CONTEXTSWARM_BEST_CANDIDATE_FILE": str(best_path),
            "CONTEXTSWARM_TASK_ROOT": str(state.task_root),
            "CONTEXTSWARM_HORIZON_EPOCH_MS": str(horizon_epoch_ms),
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
            return result, None
        return result, workdir / "result.lean"

    def evaluate_snapshot(assignment: AgentAssignment, candidate: Path) -> None:
        """Validate an immutable attempt without holding a solver lease."""

        state = states[assignment.task_id]
        task = state.task
        actor = assignment.agent_id
        try:
            verdict = _evaluate_candidate(
                broker,
                task,
                candidate,
                deadline,
                actor_id=actor,
                episode=assignment.generation,
            )
        except Exception as exc:
            verdict = Verdict(
                task.slug,
                "EVALUATOR_ERROR",
                0.0,
                0.0,
                error=str(exc),
            )
        eligible = time.monotonic() <= deadline and verdict.status != "OUT_OF_HORIZON"
        logger.agent_evaluation(verdict, episode=assignment.generation, agent_id=actor)
        logger.event(
            "evaluation_finished",
            **verdict.as_dict(),
            agent_id=actor,
            episode=assignment.generation,
            phase="solver",
            eligible_for_handoff=eligible,
        )
        if not eligible:
            return

        candidate_is_usable = verdict.status in {
            "PROVED",
            "COMPILES_WITH_SORRY",
            "VERIFY_FAIL",
            "MOCK_SKIPPED",
        }
        with state.lock:
            if candidate_is_usable and _verdict_priority(verdict) >= _verdict_priority(state.best_verdict):
                state.best_verdict = verdict
                assert state.best_candidate is not None
                try:
                    broker.materialize_snapshot(
                        task.slug,
                        str(verdict.response.get("candidate_sha256") or ""),
                        state.best_candidate,
                    )
                except OSError:
                    pass

        # Closeout and late evaluator receipts are deliberately feedback-free.
        if policy.enabled and time.monotonic() < deadline:
            feedback = verdict.error or str(
                verdict.response.get("error_message")
                or verdict.response.get("reason")
                or verdict.status
            )
            assert state.best_candidate is not None
            policy.publish(
                task.slug,
                actor,
                kind="validation_result",
                title=f"attempt {assignment.generation}: {verdict.status}",
                body=(
                    f"Evaluator status={verdict.status}; score={verdict.score}. "
                    f"Feedback: {feedback[:1200]} Shared candidate: "
                    f"workers/{task.slug}/best/result.lean"
                ),
                deadline_epoch_ms=horizon_epoch_ms,
            )

    def preserve_unevaluated_candidate(
        assignment: AgentAssignment,
        candidate: Path,
    ) -> None:
        """Keep an agent's completed bytes even when optional feedback closes."""

        state = states[assignment.task_id]
        try:
            payload = read_regular_bytes(
                candidate,
                trusted_root=candidate.parent,
                max_bytes=config.formal_tools_max_candidate_bytes,
            )
        except OSError as exc:
            logger.event(
                "candidate_preservation_failed",
                task_id=assignment.task_id,
                agent_id=assignment.agent_id,
                episode=assignment.generation,
                error=str(exc),
            )
            return
        digest = hashlib.sha256(payload).hexdigest()
        fallback = Verdict(
            assignment.task_id,
            "UNEVALUATED_CANDIDATE",
            0.0,
            0.0,
            {
                "candidate_sha256": digest,
                "selection_authority": "solver_completion_order",
            },
        )
        preserved = False
        with state.lock:
            if _verdict_priority(fallback) >= _verdict_priority(state.best_verdict):
                assert state.best_candidate is not None
                atomic_write_bytes(state.best_candidate, payload, mode=0o600)
                state.best_verdict = fallback
                preserved = True
        logger.event(
            "candidate_preserved",
            task_id=assignment.task_id,
            agent_id=assignment.agent_id,
            episode=assignment.generation,
            candidate_sha256=digest,
            selected=preserved,
            evaluator_feedback_required=False,
        )

    # Fill the initial pool.  A queue worker will immediately request a new
    # assignment after each completion, so no task is permanently tied to a
    # Python thread.
    initial_assignments: list[AgentAssignment] = []
    for _ in range(config.max_parallel):
        assignment = claim_next()
        if assignment is None:
            break
        initial_assignments.append(assignment)
        jobs.put(assignment)

    worker_count = max(1, min(config.max_parallel, len(initial_assignments)))

    evaluation_executor = ThreadPoolExecutor(
        max_workers=config.lean_max_concurrent_evaluations,
        thread_name_prefix="cps-evaluator",
    )

    def bounded_evaluate_snapshot(
        assignment: AgentAssignment,
        candidate: Path,
    ) -> None:
        try:
            evaluate_snapshot(assignment, candidate)
        except Exception as exc:
            logger.event(
                "evaluator_worker_error",
                task_id=assignment.task_id,
                agent_id=assignment.agent_id,
                episode=assignment.generation,
                error=str(exc),
                traceback=traceback.format_exc()[-2_000:],
            )
        finally:
            evaluation_backlog_gate.release()

    def worker_loop() -> None:
        while True:
            try:
                assignment = jobs.get(timeout=0.2)
            except Empty:
                if time.monotonic() >= deadline or scheduler.done:
                    return
                continue
            try:
                result, snapshot = execute_assignment(assignment)
                with results_lock:
                    results.append(result)
                # A Pi slot represents solver capacity only.  Release it
                # before the independent Judge queue starts or waits.
                scheduler.finish(assignment, solved=False)
                if snapshot is not None:
                    preserve_unevaluated_candidate(assignment, snapshot)
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
                        admitted = evaluation_backlog_gate.acquire(timeout=remaining)
                    if admitted:
                        try:
                            evaluation_executor.submit(
                                bounded_evaluate_snapshot,
                                assignment,
                                snapshot,
                            )
                        except Exception:
                            evaluation_backlog_gate.release()
                            raise
                    else:
                        logger.event(
                            "evaluation_backpressure_expired",
                            task_id=assignment.task_id,
                            agent_id=assignment.agent_id,
                            episode=assignment.generation,
                            backlog_limit=evaluation_backlog_limit,
                        )
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

    try:
        if initial_assignments:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(worker_loop) for _ in range(worker_count)]
                for future in futures:
                    future.result()
    finally:
        # Evaluations already admitted during the solver phase settle or are
        # cancelled before the candidate freeze. They cannot write feedback
        # after the horizon; executor shutdown is the bounded join and avoids
        # retaining every completed Future for the duration of a long run.
        evaluation_executor.shutdown(wait=True, cancel_futures=False)

    atomic_write_json(run_dir / "elastic_scheduler_state.json", scheduler.snapshot())
    results.sort(key=lambda item: (item.task_id, item.episode, item.agent_id))
    return results


def _run_task_workers(
    config: ExperimentConfig,
    tasks: list[Task],
    run_dir: Path,
    logger: RunLogger,
    pi_agent: PiAgent,
    policy: CommunicationPolicy,
    broker: EvaluatorBroker,
    *,
    mock_agent: bool,
    deadline: float,
) -> list[AgentResult]:
    def execute(task: Task) -> AgentResult:
        workdir = run_dir / "workers" / task.slug
        _stage_task(
            task,
            workdir,
            broker=broker,
            actor_id=f"worker-{task.slug}",
            context_piece_enabled=policy.enabled,
        )
        db_path = str(policy.store.path) if policy.store is not None else ""
        best_result: AgentResult | None = None
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
            best_result = result
        if best_result is None:
            best_result = _mock_result(actor, task.slug, config.episodes_per_task)
        return best_result

    results: list[AgentResult] = []
    if policy.enabled:
        actors = [
            {"actor_id": f"worker-{task.slug}-e{episode}", "task_id": task.slug, "episode": episode}
            for task in tasks
            for episode in range(1, config.episodes_per_task + 1)
        ]
        atomic_write_text(
            run_dir / "actors.json",
            json.dumps(actors, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
    with ThreadPoolExecutor(max_workers=config.max_parallel) as executor:
        futures = {executor.submit(execute, task): task.slug for task in tasks}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item.task_id)
    return results


def _stage_task(
    task: Task,
    destination: Path,
    *,
    broker: EvaluatorBroker,
    actor_id: str,
    context_piece_enabled: bool,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    atomic_write_text(destination / "problem.md", task.problem_text, mode=0o444)
    baseline_dir = destination / "baseline"
    baseline_dir.mkdir(exist_ok=True)
    baseline_source = next(iter(sorted(task.root.glob("baseline/*.lean"))))
    shutil.copy2(baseline_source, baseline_dir / baseline_source.name)
    atomic_write_json(destination / "metadata.json", task.metadata, mode=0o444)
    result = destination / "result.lean"
    if not result.exists():
        shutil.copy2(baseline_source, result)
    capability = broker.register_worker(task, destination, actor_id=actor_id)
    stage_worker_tools(
        destination,
        capability=capability,
        baseline_names=[baseline_source.name],
        context_piece_enabled=context_piece_enabled,
    )
    (destination / "scratch").mkdir(exist_ok=True, mode=0o700)
    for immutable in (
        destination / "problem.md",
        destination / "metadata.json",
        baseline_dir / baseline_source.name,
    ):
        immutable.chmod(0o444)
    result.chmod(0o600)


def _evaluate_candidate(
    broker: EvaluatorBroker,
    task: Task,
    candidate: Path,
    deadline: float,
    *,
    actor_id: str,
    episode: int,
) -> Verdict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return Verdict(task.slug, "OUT_OF_HORIZON", 0.0, 0.0, {"reason": "run_horizon_elapsed"})
    return broker.evaluate_local(
        task,
        candidate,
        trusted_root=candidate.parent,
        scope_id=f"runner:{actor_id}",
        actor_id=actor_id,
        episode=episode,
    )


def _candidate_source(config: ExperimentConfig, task: Task, run_dir: Path) -> Path:
    if config.mode == "mono":
        return run_dir / "workers" / "mono" / "tasks" / task.slug / "result.lean"
    if config.uses_cps:
        return run_dir / "workers" / task.slug / "best" / "result.lean"
    return run_dir / "workers" / task.slug / "result.lean"


def _freeze_closeout_candidates(
    config: ExperimentConfig,
    tasks: list[Task],
    run_dir: Path,
    logger: RunLogger,
    broker: EvaluatorBroker,
) -> dict[str, _FrozenCandidate]:
    """Freeze one mode-defined task candidate before feedback-free scoring."""

    root = run_dir / "closeout_candidates"
    frozen: dict[str, _FrozenCandidate] = {}
    rows: list[dict[str, Any]] = []
    for task in tasks:
        source = _candidate_source(config, task, run_dir)
        destination = root / task.slug / "result.lean"
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest: str | None = None
        error: str | None = None
        selected_source = str(source.relative_to(run_dir))
        selected_kind = "workspace_at_horizon"
        try:
            locally_proved = broker.best_local_proved(task.slug)
            if locally_proved is not None:
                payload = locally_proved.payload
                selected_source = f"broker_snapshot:{locally_proved.sha256}"
                selected_kind = "preserved_agent_local_proved_snapshot"
            else:
                payload = read_regular_bytes(
                    source,
                    trusted_root=run_dir,
                    max_bytes=config.formal_tools_max_candidate_bytes,
                )
            digest = hashlib.sha256(payload).hexdigest()
            atomic_write_bytes(destination, payload, mode=0o444)
        except OSError as exc:
            error = f"cannot freeze candidate: {exc.strerror or type(exc).__name__}"
        frozen[task.slug] = _FrozenCandidate(task.slug, destination, digest, error)
        row: dict[str, Any] = {
            "task_id": task.slug,
            "source": selected_source,
            "selection_kind": selected_kind,
            "snapshot": str(destination.relative_to(run_dir)),
            "candidate_sha256": digest,
        }
        if error:
            row["error"] = error
        rows.append(row)
    atomic_write_json(run_dir / "closeout_candidates.json", {"candidates": rows})
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
    broker: EvaluatorBroker,
    task: Task,
    candidate: _FrozenCandidate,
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
    verdict = broker.evaluate_official(
        task,
        candidate.path,
        trusted_root=candidate.path.parents[2],
    )
    verdict.response.setdefault("candidate_sha256", candidate.sha256)
    return verdict


def _run_closeout(
    config: ExperimentConfig,
    tasks: list[Task],
    frozen: Mapping[str, _FrozenCandidate],
    logger: RunLogger,
    broker: EvaluatorBroker,
) -> dict[str, Verdict]:
    """Score frozen candidates under one bounded, feedback-free contract."""

    logger.event(
        "closeout_started",
        candidate_count=len(tasks),
        max_concurrent_evaluations=config.lean_max_concurrent_evaluations,
        execution_timeout_seconds=config.lean_timeout_seconds,
        closeout_timeout_seconds=config.lean_closeout_timeout_seconds,
    )
    verdicts: dict[str, Verdict] = {}

    def evaluate(task: Task) -> Verdict:
        try:
            return _evaluate_closeout_candidate(broker, task, frozen[task.slug])
        except Exception as exc:
            return Verdict(task.slug, "EVALUATOR_ERROR", 0.0, 0.0, error=str(exc))

    worker_count = max(1, min(config.lean_max_concurrent_evaluations, len(tasks)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="closeout") as executor:
        futures = {executor.submit(evaluate, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            verdict = future.result()
            verdicts[task.slug] = verdict
            logger.scoreboard(verdict, episode=0, agent_id="closeout")
            logger.event(
                "closeout_evaluation_finished",
                **verdict.as_dict(),
                agent_id="closeout",
                episode=0,
            )
    verdicts = {task.slug: verdicts[task.slug] for task in tasks}
    logger.event("closeout_finished", score=sum(verdict.score for verdict in verdicts.values()))
    return verdicts


def _write_mono_bundle(
    worker_dir: Path,
    tasks: Iterable[Task],
    *,
    max_candidate_bytes: int,
) -> None:
    solutions: dict[str, str] = {}
    for task in tasks:
        candidate = worker_dir / "tasks" / task.slug / "result.lean"
        try:
            solutions[task.slug] = read_regular_bytes(
                candidate,
                trusted_root=worker_dir,
                max_bytes=max_candidate_bytes,
            ).decode("utf-8")
        except (OSError, UnicodeError):
            solutions[task.slug] = ""
    atomic_write_text(
        worker_dir / "result.json",
        json.dumps(
            {"schema_version": "formal_lean_single_run_bundle_v1", "solutions": solutions},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        mode=0o600,
    )


def _write_context_piece_wrapper(workdir: Path) -> None:
    wrapper = workdir / "context_piece"
    atomic_write_text(
        wrapper,
        "#!/bin/sh\nexec python3 -m contextswarm_mini.context_piece \"$@\"\n",
        mode=0o555,
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


def _write_final(
    run_dir: Path,
    config: ExperimentConfig,
    tasks: Iterable[Task],
    verdicts: Mapping[str, Verdict],
    agent_results: Iterable[AgentResult],
    *,
    status: str,
    cps_summary: Mapping[str, Any] | None,
) -> None:
    task_list = list(tasks)
    rows: dict[str, dict[str, Any]] = {}
    for task in task_list:
        verdict = verdicts.get(task.slug)
        if verdict is None:
            verdict = Verdict(
                task.slug,
                "OFFICIAL_VERDICT_MISSING",
                0.0,
                0.0,
                {"reason": "outer official evaluation did not produce a terminal verdict"},
                error="official verdict missing",
            )
        rows[task.slug] = verdict.as_dict()
    unexpected = sorted(set(verdicts) - {task.slug for task in task_list})
    if unexpected:
        status = "INCOMPLETE" if status == "COMPLETED" else status
    missing_count = sum(1 for item in rows.values() if item["status"] == "OFFICIAL_VERDICT_MISSING")
    if missing_count and status == "COMPLETED":
        status = "INCOMPLETE"
    agent_rows = [item.as_dict() for item in agent_results]
    selected_task_ids = {task.slug for task in task_list}
    official_verdict_count = sum(1 for task_id in verdicts if task_id in selected_task_ids)
    final = {
        "schema_version": "contextswarm_mini_run_v1",
        "status": status,
        "mode": config.mode,
        "communication": config.communication,
        "dataset": "matholympiadbench",
        "score": sum(item["score"] for item in rows.values()),
        "max_score": len(task_list),
        "selected_task_count": len(task_list),
        "official_verdict_count": official_verdict_count,
        "missing_official_verdict_count": missing_count,
        "verdicts": rows,
        "agents": agent_rows,
        "horizon_seconds": config.time_limit_seconds,
        "agent_timeout_count": sum(1 for item in agent_rows if item.get("timed_out")),
        "verdict_status_counts": {
            status: sum(1 for item in rows.values() if item.get("status") == status)
            for status in sorted({str(item.get("status")) for item in rows.values()})
        },
        "cps": dict(cps_summary or {"enabled": False}),
        "finished_at": utc_now(),
    }
    atomic_write_json(run_dir / "final.json", final)
