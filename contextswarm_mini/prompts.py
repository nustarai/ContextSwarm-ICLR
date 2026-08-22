"""Stable worker prompts for the three paper-facing protocols."""

from __future__ import annotations

from typing import Iterable

from .models import Task


SOLVER_EXECUTION_CONTRACT = """Execution and verification contract (mandatory):
- Use only `judge_check`, the experiment-provided controlled Judge interface, for
  authoritative Lean checking. When the manifest exposes the bounded formal-helper
  surface, `python3 evaluate.py` and `./formal_query ...` are advisory diagnostics
  only; they never select a candidate or establish official success.
  `CONTEXTSWARM_JUDGE_URL` is reserved for that tool; runner-owned helpers share
  the same capability boundary. You must
  not read, print, modify, or use it to contact the Judge yourself.
- Never invoke local `lean`, `lake`, `elan`, a local verifier, proof-search service,
  or any other local proof checker. Do not install or download Lean, Mathlib,
  toolchains, packages, caches, compilers, or solver infrastructure.
- Do not run resource-heavy local computation or start background, detached, or
  parallel processes. In particular, do not fan out candidate checks or use shell
  job control, `xargs -P`, GNU Parallel, subprocess pools, or similar mechanisms.
- Never call a raw Judge or evaluator HTTP endpoint with curl, wget, Python,
  JavaScript, or another network client. Do not probe ports, services, credentials,
  process sidecars, or evaluator implementation files.
- If `judge_check` is temporarily unavailable, overloaded, or returns a retryable
  result, wait/retry only through `judge_check` within the experiment budget, or
  leave the strongest candidate in `result.lean` for the runner. Never fall back to
  a local checker, raw HTTP, or a separately installed service.
- Your allowed proof context is this assigned workspace plus shared candidates,
  CPS state, and helper tools explicitly provided by the runner in this prompt.
  Do not browse unrelated workers, sessions, host paths, or runtime internals.
"""


# This block is duplicated into the self-contained benchmark problem statements.
# Keep it centralized here, use scripts/sync_problem_work_mode.py to update all
# statements, and retain the test that rejects drift between those copies.
PROBLEM_WORK_MODE_CONTRACT = """- Follow the mandatory execution and verification contract in the worker prompt.
- Treat this as offline proof construction. The sole checking exception is
  `judge_check`, the experiment-provided controlled external Judge interface.
- If the manifest exposes `evaluate.py` or `formal_query`, use them only as bounded
  advisory diagnostics; they never establish official success or select a candidate.
- Never execute local Lean/lake/elan, install or download Lean/Mathlib/toolchains,
  run a local verifier or proof search, perform resource-heavy computation, or
  start background or parallel processes. Never call raw Judge HTTP endpoints.
- If `judge_check` is unavailable or overloaded, retry/wait only through that tool
  within budget, or leave the best `result.lean`; do not create a local fallback.
- The assigned scope includes this task directory and any shared CPS context,
  shared candidate, or helper tool explicitly named by the runner's worker prompt.
  Do not browse any other home, system, runtime, worker, or session artifacts.
- Edit `result.lean` only within the allowed proof surface described above."""


def render_problem_work_mode(*, indent: str = "        ") -> str:
    """Render the canonical block used by the self-contained task statements."""

    return "\n".join(
        f"{indent}{line}" if line else ""
        for line in PROBLEM_WORK_MODE_CONTRACT.splitlines()
    )


def _communication_instructions(enabled: bool) -> str:
    if not enabled:
        return (
            "This is a no-communication baseline. Do not read or write any shared "
            "CPS/blackboard state; work only from the files in this workspace."
        )
    return """This run exposes shared ContextSwarm state only through controlled CPS tools.
Before trying a route, use `cps_search` to find relevant shared evidence. After a
meaningful discovery, use `cps_publish` to leave a concise typed handoff. Use
`cps_inbox` to receive direct messages, `cps_send` to send one, `cps_ack` to
acknowledge one, and `cps_actors` only when recipient discovery is needed.
Do not access CPS through a local CLI, database, filesystem search, or custom
script. Never include credentials, absolute host paths, or full transcripts in a
piece or message."""


def _formal_tools_instructions(enabled: bool) -> str:
    if not enabled:
        return ""
    return """This manifest may expose a bounded formal-helper surface documented in
PUBLIC_FILES.md. Use only the exact staged helper commands named there; they are
advisory diagnostics and never official score or candidate-selection authority.
Do not inspect helper source, alter capability metadata, or use any other shell
command."""


def build_task_prompt(
    task: Task,
    *,
    task_workspace: str,
    agent_id: str,
    episode: int,
    communication_enabled: bool,
    formal_tools_enabled: bool = False,
    digest: str = "",
) -> str:
    context = digest.strip() or "(no prior shared context for this task)"
    return f"""You are worker {agent_id}, episode {episode}, in a bounded formal-proof experiment.

Task: {task.slug}
Workspace: {task_workspace}
The public statement is in problem.md. The immutable starting skeleton is in baseline/.
Write your candidate proof only to result.lean and preserve the theorem statement,
imports, namespace, and source contract. The controlled external Judge, accessed
only through `judge_check`, is the only authority for success; do not claim success
from intuition, a text scan, or a local proof process.

{SOLVER_EXECUTION_CONTRACT}

{_communication_instructions(communication_enabled)}

{_formal_tools_instructions(formal_tools_enabled)}

Relevant shared context (possibly empty):
---
{context}
---

Work in small proof-construction increments. Inspect the existing result.lean first,
make a concrete candidate, and leave the best candidate in result.lean before ending.
When feedback is useful, check one candidate at a time with `judge_check`.
"""


def build_mono_prompt(
    tasks: Iterable[Task],
    *,
    workspace: str,
    communication_enabled: bool,
    formal_tools_enabled: bool = False,
) -> str:
    task_lines = "\n".join(f"- {task.slug}: tasks/{task.slug}/" for task in tasks)
    return f"""You are the Mono baseline worker for a fixed MathOlympiadBench latest12 bundle.

One Pi session must work through the following task directories serially:
{task_lines}

For each task, read its problem.md and baseline/*.lean, then write the candidate to
tasks/<slug>/result.lean (the runner also maintains the aggregate result.json bundle).
Do not modify the source statement or baseline. The runner
evaluates every candidate after this session and counts only canonical PROVED verdicts.

{SOLVER_EXECUTION_CONTRACT}

{_communication_instructions(communication_enabled)}

{_formal_tools_instructions(formal_tools_enabled)}

Use the available wall-clock budget on concrete proof construction. Leave every
task directory with its best candidate, even if some targets remain incomplete.
"""


def build_finalization_prompt(task: Task, *, digest: str = "") -> str:
    return f"""Re-open {task.slug}/result.lean and leave the strongest candidate in place.
Review the latest evaluator feedback and any relevant shared handoff below. Do not
change the theorem contract or add proof-bypass declarations.

{SOLVER_EXECUTION_CONTRACT}

{digest or '(no shared handoff)'}
"""
