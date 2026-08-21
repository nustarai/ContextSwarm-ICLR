"""Experiment supervisor for Mono, Parallel, and CPS protocols."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import datetime as dt
import json
from pathlib import Path
import shutil
import threading
import time
import traceback
import uuid
from typing import Any, Iterable, Mapping

from .config import ConfigError, ExperimentConfig
from .cps import CPSStore, CommunicationPolicy, make_policy
from .evaluator import LeanEvaluator, MockEvaluator
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
    sessions = len(task_list) if config.mode != "mono" else 1
    if config.mode == "cps":
        sessions *= config.episodes_per_task
    return {
        "name": config.name,
        "mode": config.mode,
        "communication": config.communication,
        "tasks": [task.slug for task in task_list],
        "task_count": len(task_list),
        "episodes_per_task": config.episodes_per_task,
        "max_parallel": config.max_parallel,
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
    if not gate.acquire(timeout=min(remaining, 5.0)):
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
        "finished_at": utc_now(),
    }
    (run_dir / "final.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
