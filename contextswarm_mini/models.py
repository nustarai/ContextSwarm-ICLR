"""Small shared data objects used by the runner, CPS store, and evaluator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Task:
    slug: str
    root: Path
    problem_text: str
    baseline_code: str
    metadata: dict[str, Any]

    @property
    def problem_id(self) -> str:
        return str(self.metadata.get("problem_id") or self.slug)

    @property
    def theorem_name(self) -> str:
        return str(self.metadata.get("theorem_name") or "")


@dataclass
class AgentResult:
    agent_id: str
    task_id: str
    episode: int
    returncode: int
    started_at: str
    finished_at: str
    command: list[str] = field(default_factory=list)
    output_tail: str = ""
    error_tail: str = ""
    events: int = 0
    timed_out: bool = False
    cancelled: bool = False
    mocked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "episode": self.episode,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "command": self.command,
            "output_tail": self.output_tail,
            "error_tail": self.error_tail,
            "events": self.events,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "mocked": self.mocked,
        }


@dataclass
class Verdict:
    task_id: str
    status: str
    score: float
    elapsed_seconds: float
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    candidate_sha256: str | None = None
    task_contract_sha256: str | None = None
    judge_job_id: str | None = None
    cache_reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "score": self.score,
            "elapsed_seconds": self.elapsed_seconds,
            "response": self.response,
            "error": self.error,
            "candidate_sha256": self.candidate_sha256,
            "task_contract_sha256": self.task_contract_sha256,
            "judge_job_id": self.judge_job_id,
            "cache_reused": self.cache_reused,
        }


@dataclass
class RunState:
    run_id: str
    output_dir: Path
    started_at: str
    finished_at: str | None = None
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    agent_results: list[AgentResult] = field(default_factory=list)
