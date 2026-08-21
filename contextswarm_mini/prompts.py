"""Stable worker prompts for the three paper-facing protocols."""

from __future__ import annotations

from typing import Iterable

from .models import Task


def _communication_instructions(enabled: bool) -> str:
    if not enabled:
        return (
            "This is a no-communication baseline. Do not read or write any shared "
            "CPS/blackboard state; work only from the files in this workspace."
        )
    return """This run exposes the shared ContextSwarm communication CLI at ./context_piece.
Before trying a route, search with `./context_piece search --query <keywords>`.
After a meaningful discovery, publish a concise typed handoff with
`./context_piece create --kind proof_strategy --title '...' --body '...'`.
For direct-message variants you may use `./context_piece message send --to <agent> --body '...'`.
Never include credentials, absolute host paths, or full transcripts in a piece."""


def build_task_prompt(
    task: Task,
    *,
    task_workspace: str,
    agent_id: str,
    episode: int,
    communication_enabled: bool,
    digest: str = "",
) -> str:
    context = digest.strip() or "(no prior shared context for this task)"
    return f"""You are worker {agent_id}, episode {episode}, in a bounded formal-proof experiment.

Task: {task.slug}
Workspace: {task_workspace}
The public statement is in problem.md. The immutable starting skeleton is in baseline/.
Write your candidate proof only to result.lean and preserve the theorem statement,
imports, namespace, and source contract. The external Lean evaluator is the only
authority for success; do not claim success from intuition or a local text scan.

{_communication_instructions(communication_enabled)}

Relevant shared context (possibly empty):
---
{context}
---

Work in small checked increments. Inspect the existing result.lean first, make a
concrete candidate, and leave the best candidate in result.lean before ending.
"""


def build_mono_prompt(tasks: Iterable[Task], *, workspace: str, communication_enabled: bool) -> str:
    task_lines = "\n".join(f"- {task.slug}: tasks/{task.slug}/" for task in tasks)
    return f"""You are the Mono baseline worker for a fixed MathOlympiadBench latest12 bundle.

One Pi session must work through the following task directories serially:
{task_lines}

For each task, read its problem.md and baseline/*.lean, then write the candidate to
tasks/<slug>/result.lean (the runner also maintains the aggregate result.json bundle).
Do not modify the source statement or baseline. The runner
evaluates every candidate after this session and counts only canonical PROVED verdicts.

{_communication_instructions(communication_enabled)}

Use the available wall-clock budget on concrete proof construction. Leave every
task directory with its best candidate, even if some targets remain incomplete.
"""


def build_finalization_prompt(task: Task, *, digest: str = "") -> str:
    return f"""Re-open {task.slug}/result.lean and leave the strongest checked candidate in place.
Review the latest evaluator feedback and any relevant shared handoff below. Do not
change the theorem contract or add proof-bypass declarations.

{digest or '(no shared handoff)'}
"""
