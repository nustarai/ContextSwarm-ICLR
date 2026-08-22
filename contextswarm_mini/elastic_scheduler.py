"""Small, communication-free scheduler for elastic per-task workers.

The CPS runner uses this event-driven building block: callers claim a worker,
report that worker as finished, and ask for the next worker. There is no
background thread and no transport-specific code here.

``max_parallel`` is a total slot budget.  ``initial_agents`` is the desired
number of workers per task before spare slots are used for elastic retries.
When a worker finishes without solving its task, its slot is available again;
when a task is solved, that task will never receive a new lease.  Existing
leases remain active until their owners report back (or the integration calls
``cancel_task`` explicitly).  No new lease is issued after ``horizon`` seconds
have elapsed.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Callable, Iterable, Mapping


@dataclass(frozen=True)
class AgentAssignment:
    """A worker lease returned by :meth:`ElasticScheduler.next_assignment`."""

    task_id: str
    agent_id: str
    generation: int
    admitted_at: float


# This name is convenient for clients which model a claimed worker as a lease.
AgentLease = AgentAssignment


@dataclass
class _TaskState:
    task_id: str
    initial_agents: int
    solved: bool = False
    retired_reason: str | None = None
    next_generation: int = 1
    initial_admitted: int = 0


class ElasticScheduler:
    """Schedule bounded, elastic workers across a set of tasks.

    The scheduler is intentionally synchronous.  A typical integration looks
    like this::

        scheduler = ElasticScheduler(["p1", "p2"], max_parallel=4,
                                     initial_agents=2, horizon=60)
        while (lease := scheduler.next_assignment()) is not None:
            submit(lease, on_done=lambda ok: scheduler.finish(lease.agent_id,
                                                                solved=ok))

    ``finish`` may be called from worker threads; scheduling and state
    transitions are protected by a small lock.  ``clock`` is injectable to
    make horizon behavior deterministic in tests.
    """

    def __init__(
        self,
        tasks: Iterable[object] | Mapping[object, int] | None = None,
        max_parallel: int = 1,
        initial_agents: int | Mapping[object, int] = 1,
        horizon: float | None = 3600.0,
        *,
        assignment_policy: str = "least_active",
        clock: Callable[[], float] = time.monotonic,
        task_ids: Iterable[object] | None = None,
        initial_agents_per_task: int | Mapping[object, int] | None = None,
    ) -> None:
        if tasks is not None and task_ids is not None:
            raise TypeError("provide tasks or task_ids, not both")
        if task_ids is not None:
            tasks = task_ids
        if initial_agents_per_task is not None:
            if not (isinstance(initial_agents, int) and not isinstance(initial_agents, bool) and initial_agents == 1):
                raise TypeError("provide initial_agents or initial_agents_per_task, not both")
            initial_agents = initial_agents_per_task
        self.max_parallel = self._positive_int(max_parallel, "max_parallel")
        self.horizon = self._horizon_value(horizon)
        if assignment_policy not in {"least_active", "round_robin"}:
            raise ValueError("assignment_policy must be least_active or round_robin")
        self.assignment_policy = assignment_policy
        if isinstance(initial_agents, Mapping):
            self._initial_agents_default = 1
        else:
            self._initial_agents_default = self._nonnegative_int(initial_agents, "initial_agents")
        self._clock = clock
        self._started_at = float(clock())
        self._lock = threading.RLock()
        self._tasks: dict[str, _TaskState] = {}
        self._active: dict[str, AgentAssignment] = {}
        self._active_by_task: dict[str, dict[str, AgentAssignment]] = {}
        self._cursor = 0
        self._initial_cursor = 0
        self._history: list[dict[str, object]] = []

        mapping = tasks if isinstance(tasks, Mapping) else None
        source = mapping.keys() if mapping is not None else (tasks or ())
        for raw_task in source:
            task_id = self._task_id(raw_task)
            if task_id in self._tasks:
                raise ValueError(f"duplicate task id: {task_id}")
            if mapping is not None:
                configured = mapping[raw_task]
            elif isinstance(initial_agents, Mapping):
                configured = initial_agents.get(raw_task, initial_agents.get(task_id, 1))
            else:
                configured = initial_agents
            count = self._nonnegative_int(configured, f"initial_agents[{task_id}]")
            self._tasks[task_id] = _TaskState(task_id, count)
            self._active_by_task[task_id] = {}

    @staticmethod
    def _task_id(task: object) -> str:
        value = getattr(task, "slug", task)
        text = str(value).strip()
        if not text:
            raise ValueError("task ids must be non-empty")
        return text

    @staticmethod
    def _positive_int(value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _nonnegative_int(value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    @staticmethod
    def _horizon_value(value: float | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("horizon must be a non-negative number")
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("horizon must be a non-negative number") from exc
        if math.isnan(result) or result < 0:
            raise ValueError("horizon must be a non-negative number")
        return result

    @property
    def elapsed(self) -> float:
        return max(0.0, float(self._clock()) - self._started_at)

    @property
    def horizon_reached(self) -> bool:
        return self._horizon_reached(float(self._clock()))

    def _horizon_reached(self, now: float) -> bool:
        return self.horizon is not None and now - self._started_at >= self.horizon

    @property
    def active_slots(self) -> int:
        with self._lock:
            return len(self._active)

    @property
    def remaining_slots(self) -> int:
        return max(0, self.max_parallel - self.active_slots)

    @property
    def solved_tasks(self) -> frozenset[str]:
        with self._lock:
            return frozenset(task_id for task_id, state in self._tasks.items() if state.solved)

    @property
    def unsolved_tasks(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(task_id for task_id, state in self._tasks.items() if not state.solved)

    @property
    def has_pending_initial(self) -> bool:
        """Whether an unsolved task has not yet received its initial quota."""
        with self._lock:
            return any(
                not state.solved and state.initial_admitted < state.initial_agents
                for state in self._tasks.values()
            )

    @property
    def done(self) -> bool:
        with self._lock:
            return not self._active and (
                not self._tasks
                or all(
                    state.solved or state.retired_reason is not None
                    for state in self._tasks.values()
                )
                or self._horizon_reached(float(self._clock()))
            )

    def add_task(self, task: object, initial_agents: int | None = None) -> str:
        """Add a task before it is solved; returns its normalized id."""
        task_id = self._task_id(task)
        count = self._nonnegative_int(
            self._default_initial_agents if initial_agents is None else initial_agents,
            f"initial_agents[{task_id}]",
        )
        with self._lock:
            if task_id in self._tasks:
                raise ValueError(f"duplicate task id: {task_id}")
            self._tasks[task_id] = _TaskState(task_id, count)
            self._active_by_task[task_id] = {}
        return task_id

    @property
    def _default_initial_agents(self) -> int:
        # ``initial_agents`` is scalar for dynamically-added tasks.  Mapping
        # input has already been resolved for constructor tasks.
        return self._initial_agents_default

    def _select_task(self) -> _TaskState | None:
        states = list(self._tasks.values())
        if not states:
            return None
        # First satisfy per-task initial quotas.  This makes a partially
        # admitted task recover its initial workers before spare retries.
        for offset in range(len(states)):
            state = states[(self._initial_cursor + offset) % len(states)]
            if (
                not state.solved
                and state.retired_reason is None
                and state.initial_admitted < state.initial_agents
            ):
                self._initial_cursor = (self._initial_cursor + offset + 1) % len(states)
                return state
        for offset in range(len(states)):
            state = states[(self._cursor + offset) % len(states)]
            if (
                not state.solved
                and state.retired_reason is None
                and len(self._active_by_task[state.task_id]) < state.initial_agents
            ):
                self._cursor = (self._cursor + offset + 1) % len(states)
                return state
        unsolved = [
            state
            for state in states
            if not state.solved and state.retired_reason is None
        ]
        if not unsolved:
            return None
        if self.assignment_policy == "least_active":
            # Stable tie-breaking keeps the distribution fair while ensuring
            # a task which just lost a worker is replenished first.
            self._cursor %= len(states)
            order = {
                state.task_id: (index - self._cursor) % len(states)
                for index, state in enumerate(states)
            }
            state = min(
                unsolved,
                key=lambda item: (
                    len(self._active_by_task[item.task_id]),
                    item.next_generation,
                    order[item.task_id],
                ),
            )
            self._cursor = (states.index(state) + 1) % len(states)
            return state
        # Once quotas are met, distribute retries round-robin for fairness.
        self._cursor %= len(unsolved)
        state = unsolved[self._cursor]
        self._cursor = (self._cursor + 1) % len(unsolved)
        return state

    def next_assignment(self, *, now: float | None = None) -> AgentAssignment | None:
        """Claim one available slot, or return ``None`` at capacity/horizon."""
        with self._lock:
            admitted_at = float(self._clock() if now is None else now)
            if len(self._active) >= self.max_parallel or self._horizon_reached(admitted_at):
                return None
            state = self._select_task()
            if state is None:
                return None
            return self._admit_locked(state, admitted_at)

    def next_assignment_for(
        self,
        task_id: object,
        *,
        now: float | None = None,
    ) -> AgentAssignment | None:
        """Claim one slot for an explicitly selected eligible task.

        This is the narrow integration point for CPS allocation policies.  It
        preserves the same capacity, solved-task, and horizon checks as
        :meth:`next_assignment`; the caller controls only the task choice.
        """
        normalized = self._task_id(task_id)
        with self._lock:
            admitted_at = float(self._clock() if now is None else now)
            if len(self._active) >= self.max_parallel or self._horizon_reached(admitted_at):
                return None
            state = self._tasks.get(normalized)
            if state is None:
                raise KeyError(f"unknown task id: {normalized}")
            if state.solved or state.retired_reason is not None:
                return None
            return self._admit_locked(state, admitted_at)

    def _admit_locked(self, state: _TaskState, admitted_at: float) -> AgentAssignment:
        generation = state.next_generation
        state.next_generation += 1
        if state.initial_admitted < state.initial_agents:
            state.initial_admitted += 1
        assignment = AgentAssignment(
            task_id=state.task_id,
            agent_id=f"agent-{state.task_id}-{generation}",
            generation=generation,
            admitted_at=admitted_at,
        )
        self._active[assignment.agent_id] = assignment
        self._active_by_task[state.task_id][assignment.agent_id] = assignment
        self._history.append({"event": "agent_admitted", **assignment.__dict__})
        return assignment

    # Short aliases keep call sites readable and make the object easy to wrap.
    claim = next_assignment
    acquire = next_assignment
    next_agent = next_assignment
    admit = next_assignment

    def finish(
        self,
        agent: str | AgentAssignment,
        *,
        solved: bool = False,
        retire_reason: str | None = None,
        now: float | None = None,
    ) -> AgentAssignment | None:
        """Release a worker and optionally solve or retire its task.

        Unknown or already released workers are ignored and return ``None``.
        ``retire_reason`` stops new leases without claiming that the task was
        solved and without cancelling any other active lease.
        A finish after the horizon is still accepted so callers can close
        their bookkeeping cleanly.
        """
        if solved and retire_reason is not None:
            raise ValueError("finish cannot solve and retire a task simultaneously")
        normalized_retire_reason = (
            str(retire_reason).strip() if retire_reason is not None else None
        )
        if retire_reason is not None and not normalized_retire_reason:
            raise ValueError("retire_reason must be non-empty")
        agent_id = agent.agent_id if isinstance(agent, AgentAssignment) else str(agent)
        with self._lock:
            assignment = self._active.pop(agent_id, None)
            if assignment is None:
                return None
            self._active_by_task[assignment.task_id].pop(agent_id, None)
            self._history.append(
                {
                    "event": "agent_finished",
                    "task_id": assignment.task_id,
                    "agent_id": assignment.agent_id,
                    "generation": assignment.generation,
                    "solved": bool(solved),
                    "retire_reason": normalized_retire_reason,
                    "finished_at": float(self._clock() if now is None else now),
                }
            )
            if solved:
                self._solve_locked(assignment.task_id)
            elif normalized_retire_reason is not None:
                self._retire_locked(
                    assignment.task_id,
                    normalized_retire_reason,
                )
            return assignment

    release = finish
    complete = finish

    def task_solved(self, task_id: object) -> bool:
        """Mark a task solved without cancelling its active workers."""
        normalized = self._task_id(task_id)
        with self._lock:
            return self._solve_locked(normalized)

    def _solve_locked(self, task_id: str) -> bool:
        state = self._tasks.get(task_id)
        if state is None:
            raise KeyError(f"unknown task id: {task_id}")
        if state.solved:
            return False
        # Solving stops future admissions but deliberately does not cancel
        # workers that are still running.  Their slots must remain accounted
        # for until ``finish`` or an explicit ``cancel_task`` call.
        state.solved = True
        state.retired_reason = None
        self._history.append({"event": "task_solved", "task_id": task_id})
        return True

    def retire_task(self, task_id: object, *, reason: str) -> bool:
        """Stop new leases for an unsolved task without cancelling active ones."""

        normalized = self._task_id(task_id)
        normalized_reason = str(reason).strip()
        if not normalized_reason:
            raise ValueError("reason must be non-empty")
        with self._lock:
            return self._retire_locked(normalized, normalized_reason)

    def _retire_locked(self, task_id: str, reason: str) -> bool:
        state = self._tasks.get(task_id)
        if state is None:
            raise KeyError(f"unknown task id: {task_id}")
        if state.solved or state.retired_reason is not None:
            return False
        state.retired_reason = reason
        self._history.append(
            {"event": "task_retired", "task_id": task_id, "reason": reason}
        )
        return True

    def cancel_task(self, task_id: object) -> tuple[AgentAssignment, ...]:
        """Explicitly retire active leases for a task.

        Integrations should call this only when they have actually cancelled
        the corresponding worker processes.  It is separate from
        :meth:`task_solved` so a solved task cannot silently oversubscribe the
        global slot budget.
        """
        normalized = self._task_id(task_id)
        with self._lock:
            if normalized not in self._tasks:
                raise KeyError(f"unknown task id: {normalized}")
            retired = tuple(self._active_by_task[normalized].values())
            for assignment in retired:
                self._active.pop(assignment.agent_id, None)
            self._active_by_task[normalized].clear()
            self._history.append(
                {
                    "event": "task_cancelled",
                    "task_id": normalized,
                    "agent_ids": [assignment.agent_id for assignment in retired],
                }
            )
            return retired

    cancel = cancel_task

    def active(self, task_id: object | None = None) -> tuple[AgentAssignment, ...]:
        """Return a stable snapshot of active leases."""
        with self._lock:
            if task_id is None:
                return tuple(self._active.values())
            normalized = self._task_id(task_id)
            if normalized not in self._tasks:
                raise KeyError(f"unknown task id: {normalized}")
            return tuple(self._active_by_task[normalized].values())

    def snapshot(self) -> dict[str, object]:
        """Return JSON-friendly state useful for logs and tests."""
        with self._lock:
            by_task = {
                task_id: {
                    "solved": state.solved,
                    "retired": state.retired_reason is not None,
                    "retired_reason": state.retired_reason,
                    "initial_agents": state.initial_agents,
                    "initial_admitted": state.initial_admitted,
                    "active_agents": len(self._active_by_task[task_id]),
                    "next_generation": state.next_generation,
                }
                for task_id, state in self._tasks.items()
            }
            return {
                "max_parallel": self.max_parallel,
                "assignment_policy": self.assignment_policy,
                "active_slots": len(self._active),
                "remaining_slots": self.max_parallel - len(self._active),
                "horizon": self.horizon,
                "elapsed": self.elapsed,
                "horizon_reached": self._horizon_reached(float(self._clock())),
                "tasks": by_task,
            }

    def history(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(dict(event) for event in self._history)


# A descriptive alias for integrations which call this a dynamic scheduler.
DynamicScheduler = ElasticScheduler


__all__ = ["AgentAssignment", "AgentLease", "ElasticScheduler", "DynamicScheduler"]
