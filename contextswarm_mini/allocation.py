"""CPS-aware policies for assigning a newly available solver slot.

The elastic scheduler owns capacity, leases, and the run horizon.  The classes
in this module make only one bounded decision: select an eligible task for a
slot which has already become available.  Keeping that boundary explicit lets
the three allocation arms share every other part of the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from .models import AgentResult


_PROVED_STATUS_ALIASES = frozenset({"PROVED", "AC", "PASS", "PASSED"})


def normalize_verdict_status(status: Any) -> str:
    """Return one canonical status for policy features and best comparisons."""

    normalized = str(status or "UNKNOWN").strip().upper() or "UNKNOWN"
    if normalized in _PROVED_STATUS_ALIASES:
        return "PROVED"
    return normalized


@dataclass(frozen=True)
class EvidencePiece:
    """A bounded, read-only CPS item exposed to an allocation policy."""

    piece_id: str
    kind: str
    title: str
    body: str
    author: str
    created_at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "piece_id": self.piece_id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "author": self.author,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class TaskProgress:
    """Causal progress visible at one allocation decision."""

    task_id: str
    eligible: bool
    solved: bool
    active_agents: int
    attempts: int
    completed_attempts: int
    best_status: str
    best_score: float
    last_verdict_status: str
    last_feedback: str
    consecutive_failures: int
    seconds_since_last_assignment: float
    seconds_since_progress: float
    piece_count: int
    validation_piece_count: int
    strategy_piece_count: int
    duplicate_piece_count: int
    recent_pieces: tuple[EvidencePiece, ...] = ()

    def causal_fingerprint(self) -> tuple[Any, ...]:
        """State whose change can invalidate a decision about this task.

        Wall-clock ages are deliberately excluded: merely spending model time
        must not make an otherwise unchanged choice stale.  Other tasks are
        absent because each decision is revalidated only against its selected
        task.
        """

        pieces = tuple(
            (piece.piece_id, piece.kind, piece.title, piece.body, piece.author)
            for piece in self.recent_pieces
        )
        return (
            self.task_id,
            self.eligible,
            self.solved,
            self.active_agents,
            self.attempts,
            self.completed_attempts,
            normalize_verdict_status(self.best_status),
            float(self.best_score),
            normalize_verdict_status(self.last_verdict_status),
            self.last_feedback,
            self.consecutive_failures,
            self.piece_count,
            self.validation_piece_count,
            self.strategy_piece_count,
            self.duplicate_piece_count,
            pieces,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "eligible": self.eligible,
            "solved": self.solved,
            "active_agents": self.active_agents,
            "attempts": self.attempts,
            "completed_attempts": self.completed_attempts,
            "best_status": self.best_status,
            "best_score": self.best_score,
            "last_verdict_status": self.last_verdict_status,
            "last_feedback": self.last_feedback,
            "consecutive_failures": self.consecutive_failures,
            "seconds_since_last_assignment": round(self.seconds_since_last_assignment, 6),
            "seconds_since_progress": round(self.seconds_since_progress, 6),
            "piece_count": self.piece_count,
            "validation_piece_count": self.validation_piece_count,
            "strategy_piece_count": self.strategy_piece_count,
            "duplicate_piece_count": self.duplicate_piece_count,
            "recent_pieces": [piece.as_dict() for piece in self.recent_pieces],
        }


@dataclass(frozen=True)
class TaskProgressSnapshot:
    """The common input contract used by all three allocation arms."""

    decision_index: int
    elapsed_seconds: float
    remaining_seconds: float
    free_slots: int
    tasks: tuple[TaskProgress, ...]

    @property
    def eligible_task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.tasks if task.eligible)

    @property
    def evidence_piece_ids(self) -> frozenset[str]:
        return frozenset(
            piece.piece_id
            for task in self.tasks
            for piece in task.recent_pieces
        )

    def task_causal_fingerprint(self, task_id: str) -> tuple[Any, ...] | None:
        for task in self.tasks:
            if task.task_id == task_id:
                return task.causal_fingerprint()
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "contextswarm_task_progress_v1",
            "decision_index": self.decision_index,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "remaining_seconds": round(self.remaining_seconds, 6),
            "free_slots": self.free_slots,
            "eligible_task_ids": list(self.eligible_task_ids),
            "tasks": [task.as_dict() for task in self.tasks],
        }


@dataclass
class AllocationDecision:
    """Auditable result of one policy invocation."""

    decision_index: int
    policy: str
    selected_task_id: str
    reason: str
    requested_task_id: str = ""
    evidence_piece_ids: list[str] = field(default_factory=list)
    latency_seconds: float = 0.0
    fallback: bool = False
    fallback_reason: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    features: dict[str, dict[str, float]] = field(default_factory=dict)
    agent_returncode: int | None = None
    agent_timed_out: bool = False
    agent_cancelled: bool = False
    agent_result_valid: bool | None = None
    agent_id: str = ""
    agent_task_id: str = ""
    agent_episode: int | None = None
    agent_run_horizon_reached: bool = False
    scheduler_call_id: str = ""
    scheduler_outcome: str = ""
    invalid_output: bool = False
    recoverable_invocation_error: bool = False

    def __post_init__(self) -> None:
        if not self.requested_task_id:
            self.requested_task_id = self.selected_task_id

    def as_dict(self, *, snapshot: TaskProgressSnapshot | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "schema_version": "contextswarm_allocation_decision_v1",
            "decision_index": self.decision_index,
            "policy": self.policy,
            "requested_task_id": self.requested_task_id,
            "selected_task_id": self.selected_task_id,
            "reason": self.reason,
            "evidence_piece_ids": list(self.evidence_piece_ids),
            "latency_seconds": round(self.latency_seconds, 6),
            "fallback": self.fallback,
            "fallback_reason": self.fallback_reason,
            "scores": {key: round(value, 8) for key, value in sorted(self.scores.items())},
            "features": {
                task_id: {key: round(value, 8) for key, value in sorted(values.items())}
                for task_id, values in sorted(self.features.items())
            },
            "agent_returncode": self.agent_returncode,
            "agent_timed_out": self.agent_timed_out,
            "agent_cancelled": self.agent_cancelled,
            "agent_result_valid": self.agent_result_valid,
            "agent_id": self.agent_id,
            "agent_task_id": self.agent_task_id,
            "agent_episode": self.agent_episode,
            "agent_run_horizon_reached": self.agent_run_horizon_reached,
            "scheduler_call_id": self.scheduler_call_id,
            "scheduler_outcome": self.scheduler_outcome,
            "invalid_output": self.invalid_output,
            "recoverable_invocation_error": self.recoverable_invocation_error,
        }
        if snapshot is not None:
            row["snapshot"] = snapshot.as_dict()
        return row


class UniformAllocationPolicy:
    """Deterministic round-robin allocation which ignores CPS progress."""

    name = "uniform"

    def __init__(self, task_order: Iterable[str]):
        self._order = tuple(str(task_id) for task_id in task_order)
        self._cursor = 0
        self._decisions: list[AllocationDecision] = []

    def _select(self, eligible: Iterable[str]) -> str:
        allowed = set(eligible)
        if not allowed:
            return ""
        if not self._order:
            return sorted(allowed)[0]
        for offset in range(len(self._order)):
            index = (self._cursor + offset) % len(self._order)
            task_id = self._order[index]
            if task_id in allowed:
                self._cursor = (index + 1) % len(self._order)
                return task_id
        return sorted(allowed)[0]

    def choose(self, snapshot: TaskProgressSnapshot) -> AllocationDecision:
        started = time.monotonic()
        selected = self._select(snapshot.eligible_task_ids)
        decision = AllocationDecision(
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason="deterministic round-robin over eligible tasks",
            latency_seconds=time.monotonic() - started,
        )
        self._decisions.append(decision)
        return decision

    def fallback(
        self,
        snapshot: TaskProgressSnapshot,
        reason: str,
        *,
        prior: AllocationDecision | None = None,
    ) -> AllocationDecision:
        selected = self._select(snapshot.eligible_task_ids)
        decision = prior or AllocationDecision(
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason="deterministic round-robin fallback",
        )
        decision.selected_task_id = selected
        decision.fallback = True
        decision.fallback_reason = _combine_fallback_reasons(decision.fallback_reason, reason)
        if prior is None:
            self._decisions.append(decision)
        return decision

    def summary(self) -> dict[str, Any]:
        return _decision_summary(self.name, self._decisions)


class FormulaAllocationPolicy:
    """A frozen arithmetic priority over the common task snapshot."""

    name = "formula"

    def __init__(
        self,
        task_order: Iterable[str],
        parameters: Mapping[str, float],
    ) -> None:
        self._order = tuple(str(task_id) for task_id in task_order)
        self._cursor = 0
        self.parameters = {str(key): float(value) for key, value in parameters.items()}
        self._fallback = UniformAllocationPolicy(self._order)
        self._decisions: list[AllocationDecision] = []

    def _features(self, task: TaskProgress) -> dict[str, float]:
        p = self.parameters
        active_balance = 1.0 / (1.0 + max(0, task.active_agents))
        status_quality = {
            "PROVED": p["proved_quality"],
            "COMPILES_WITH_SORRY": p["compiles_with_sorry_quality"],
            "VERIFY_FAIL": p["verify_fail_quality"],
        }.get(normalize_verdict_status(task.best_status), p["other_status_quality"])
        progress_window = max(1.0, p["progress_window_seconds"])
        recent_progress = math.exp(-max(0.0, task.seconds_since_progress) / progress_window)
        evidence_scale = max(1.0, p["evidence_saturation"])
        cps_evidence = 1.0 - math.exp(-max(0, task.strategy_piece_count) / evidence_scale)
        starvation_window = max(1.0, p["starvation_window_seconds"])
        starvation = min(1.0, max(0.0, task.seconds_since_last_assignment) / starvation_window)
        failure_scale = max(1.0, p["failure_saturation"])
        failure = min(1.0, max(0, task.consecutive_failures) / failure_scale)
        duplication = min(
            1.0,
            max(0, task.duplicate_piece_count) / max(1, task.piece_count),
        )
        return {
            "active_balance": active_balance,
            "candidate_quality": status_quality,
            "recent_progress": recent_progress,
            "cps_evidence": cps_evidence,
            "starvation": starvation,
            "failure": failure,
            "duplication": duplication,
        }

    def _score(self, features: Mapping[str, float]) -> float:
        p = self.parameters
        return (
            p["active_balance_weight"] * features["active_balance"]
            + p["candidate_quality_weight"] * features["candidate_quality"]
            + p["recent_progress_weight"] * features["recent_progress"]
            + p["cps_evidence_weight"] * features["cps_evidence"]
            + p["starvation_weight"] * features["starvation"]
            - p["failure_penalty"] * features["failure"]
            - p["duplication_penalty"] * features["duplication"]
        )

    def _tie_rank(self, task_id: str) -> int:
        if task_id not in self._order or not self._order:
            return len(self._order)
        return (self._order.index(task_id) - self._cursor) % len(self._order)

    def choose(self, snapshot: TaskProgressSnapshot) -> AllocationDecision:
        started = time.monotonic()
        eligible = [task for task in snapshot.tasks if task.eligible]
        features = {task.task_id: self._features(task) for task in eligible}
        scores = {task_id: self._score(values) for task_id, values in features.items()}
        selected = ""
        if scores:
            selected = min(scores, key=lambda task_id: (-scores[task_id], self._tie_rank(task_id)))
            if selected in self._order:
                self._cursor = (self._order.index(selected) + 1) % len(self._order)
        decision = AllocationDecision(
            decision_index=snapshot.decision_index,
            policy=self.name,
            selected_task_id=selected,
            reason="highest frozen formula priority among eligible tasks",
            latency_seconds=time.monotonic() - started,
            scores=scores,
            features=features,
        )
        self._decisions.append(decision)
        return decision

    def fallback(
        self,
        snapshot: TaskProgressSnapshot,
        reason: str,
        *,
        prior: AllocationDecision | None = None,
    ) -> AllocationDecision:
        replacement = self._fallback.choose(snapshot)
        decision = prior or replacement
        decision.selected_task_id = replacement.selected_task_id
        decision.fallback = True
        decision.fallback_reason = _combine_fallback_reasons(decision.fallback_reason, reason)
        if prior is None:
            decision.policy = self.name
            self._decisions.append(decision)
        return decision

    def summary(self) -> dict[str, Any]:
        result = _decision_summary(self.name, self._decisions)
        result["formula_parameters"] = dict(sorted(self.parameters.items()))
        return result


AgentInvoker = Callable[[TaskProgressSnapshot, str, int], AgentResult]


class AgentAllocationPolicy:
    """Formula-free model judgment over the same bounded CPS snapshot."""

    name = "agent"

    def __init__(self, task_order: Iterable[str], invoke: AgentInvoker):
        self._fallback = UniformAllocationPolicy(task_order)
        self._invoke = invoke
        self._decisions: list[AllocationDecision] = []
        self._lock = threading.RLock()

    @staticmethod
    def prompt(snapshot: TaskProgressSnapshot) -> str:
        snapshot_json = json.dumps(snapshot.as_dict(), ensure_ascii=False, sort_keys=True)
        return f"""You are the read-only allocation scheduler for a bounded CPS computation run.

A solver agent has finished and one in-flight slot is available. Your choice matters
because verified score accumulated earlier is more valuable: the run optimizes
verified-score over wall-clock time while keeping the fixed agent pool productive.

Review the causal snapshot below. Consider remaining time; which tasks are eligible;
current active agents; the best candidate/verdict and evaluator feedback; recent typed
CPS pieces; failed routes or blockers; duplicated work; and whether each task is making
progress or stagnating. Use your own judgment to choose where one additional solver
agent should work. No numeric priority formula or weights are provided.

Do not call tools, inspect files, edit candidates, create context pieces, send messages,
or change any run state. Decide only from the supplied snapshot. Select exactly one ID
from eligible_task_ids. After forming the decision, return exactly one JSON object with
no Markdown or surrounding text, using this schema:
{{"task_id":"<eligible ID>","reason":"<concise explanation>","evidence_piece_ids":["<piece id>"]}}
Use an empty evidence_piece_ids list when the decision relies only on numeric/status
fields. Mention only piece IDs present in the snapshot.

SNAPSHOT:
{snapshot_json}
"""

    @staticmethod
    def _parse(result: AgentResult, snapshot: TaskProgressSnapshot) -> tuple[dict[str, Any] | None, str]:
        if result.returncode != 0:
            return None, f"scheduler agent returned {result.returncode}"
        raw = result.output_tail.strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"scheduler agent emitted invalid JSON: {exc.msg}"
        if not isinstance(payload, dict) or set(payload) != {"task_id", "reason", "evidence_piece_ids"}:
            return None, "scheduler JSON must contain exactly task_id, reason, evidence_piece_ids"
        task_id = payload.get("task_id")
        reason = payload.get("reason")
        evidence = payload.get("evidence_piece_ids")
        if not isinstance(task_id, str) or task_id not in snapshot.eligible_task_ids:
            return None, "scheduler selected a task which is not eligible"
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1_000:
            return None, "scheduler reason must be a non-empty string of at most 1000 characters"
        if (
            not isinstance(evidence, list)
            or len(evidence) > 20
            or not all(isinstance(item, str) for item in evidence)
            or not set(evidence).issubset(snapshot.evidence_piece_ids)
        ):
            return None, "scheduler evidence_piece_ids are invalid"
        return {
            "task_id": task_id,
            "reason": reason.strip(),
            "evidence_piece_ids": list(dict.fromkeys(evidence)),
        }, ""

    def choose(self, snapshot: TaskProgressSnapshot) -> AllocationDecision:
        started = time.monotonic()
        result = self._invoke(snapshot, self.prompt(snapshot), snapshot.decision_index)
        if result.run_horizon_reached:
            decision = AllocationDecision(
                decision_index=snapshot.decision_index,
                policy=self.name,
                selected_task_id="",
                requested_task_id="",
                reason="scheduler agent was truncated by the overall run horizon",
                latency_seconds=time.monotonic() - started,
                agent_returncode=result.returncode,
                agent_timed_out=result.timed_out,
                agent_cancelled=result.cancelled,
                agent_result_valid=None,
                agent_id=result.agent_id,
                agent_task_id=result.task_id,
                agent_episode=result.episode,
                agent_run_horizon_reached=True,
            )
            with self._lock:
                self._decisions.append(decision)
            return decision
        parsed, error = self._parse(result, snapshot)
        latency = time.monotonic() - started
        if parsed is None:
            with self._lock:
                fallback = self._fallback.choose(snapshot)
            decision = AllocationDecision(
                decision_index=snapshot.decision_index,
                policy=self.name,
                selected_task_id=fallback.selected_task_id,
                requested_task_id="",
                reason="scheduler agent decision rejected; deterministic round-robin fallback",
                latency_seconds=latency,
                fallback=True,
                fallback_reason=error,
                agent_returncode=result.returncode,
                agent_timed_out=result.timed_out,
                agent_cancelled=result.cancelled,
                agent_result_valid=False,
                agent_id=result.agent_id,
                agent_task_id=result.task_id,
                agent_episode=result.episode,
            )
        else:
            decision = AllocationDecision(
                decision_index=snapshot.decision_index,
                policy=self.name,
                selected_task_id=parsed["task_id"],
                reason=parsed["reason"],
                evidence_piece_ids=parsed["evidence_piece_ids"],
                latency_seconds=latency,
                agent_returncode=result.returncode,
                agent_timed_out=result.timed_out,
                agent_cancelled=result.cancelled,
                agent_result_valid=True,
                agent_id=result.agent_id,
                agent_task_id=result.task_id,
                agent_episode=result.episode,
            )
        with self._lock:
            self._decisions.append(decision)
        return decision

    def fallback(
        self,
        snapshot: TaskProgressSnapshot,
        reason: str,
        *,
        prior: AllocationDecision | None = None,
    ) -> AllocationDecision:
        with self._lock:
            replacement = self._fallback.choose(snapshot)
            decision = prior or replacement
            decision.selected_task_id = replacement.selected_task_id
            decision.fallback = True
            decision.fallback_reason = _combine_fallback_reasons(decision.fallback_reason, reason)
            if prior is None:
                decision.policy = self.name
                self._decisions.append(decision)
        return decision

    def summary(self) -> dict[str, Any]:
        with self._lock:
            decisions = list(self._decisions)
        result = _decision_summary(self.name, decisions)
        result["agent_calls"] = len(decisions)
        result["agent_timeouts"] = sum(decision.agent_timed_out for decision in decisions)
        result["agent_policy_timeouts"] = sum(
            decision.agent_timed_out and not decision.agent_run_horizon_reached
            for decision in decisions
        )
        result["agent_horizon_truncations"] = sum(
            decision.agent_run_horizon_reached for decision in decisions
        )
        result["agent_cancellations"] = sum(decision.agent_cancelled for decision in decisions)
        result["agent_invalid_outputs"] = sum(
            decision.agent_result_valid is False for decision in decisions
        )
        result["agent_nonzero_returns"] = sum(
            decision.agent_returncode not in {None, 0} for decision in decisions
        )
        return result


def _decision_summary(name: str, decisions: Iterable[AllocationDecision]) -> dict[str, Any]:
    rows = list(decisions)
    latencies = [max(0.0, decision.latency_seconds) for decision in rows]
    return {
        "schema_version": "contextswarm_allocation_summary_v1",
        "policy": name,
        "decisions": len(rows),
        "fallback_decisions": sum(decision.fallback for decision in rows),
        "total_latency_seconds": round(sum(latencies), 6),
        "mean_latency_seconds": round(sum(latencies) / len(latencies), 6) if latencies else 0.0,
        "max_latency_seconds": round(max(latencies), 6) if latencies else 0.0,
    }


def _combine_fallback_reasons(existing: str, new: str) -> str:
    parts = [str(value).strip() for value in (existing, new) if str(value).strip()]
    return "; ".join(parts)[:1_000]


__all__ = [
    "AgentAllocationPolicy",
    "AllocationDecision",
    "EvidencePiece",
    "FormulaAllocationPolicy",
    "TaskProgress",
    "TaskProgressSnapshot",
    "UniformAllocationPolicy",
    "normalize_verdict_status",
]
