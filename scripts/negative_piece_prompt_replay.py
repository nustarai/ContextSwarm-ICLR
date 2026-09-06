#!/usr/bin/env python3
"""Replay the prompt-only negative-knowledge treatment on a fixed CPS fixture.

This is a deterministic mechanism replay, not a model-quality or Judge-score
benchmark.  Both arms receive the same task/message stream.  The treatment
arm represents an Agent following the new prompt rule: it publishes a normal
``cps_publish`` piece for each reusable negative finding while retaining the
same direct message.  No promotion endpoint, extra schema, or reviewer is
used.  The replay measures the resulting searchability for a fresh worker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from contextswarm_mini.cps import CPSStore  # noqa: E402
from contextswarm_mini.models import Task  # noqa: E402
from contextswarm_mini.prompts import build_task_prompt  # noqa: E402


TASKS = (
    "imo2024_p1",
    "imo2024_p2",
    "imo2024_p3",
    "imo2024_p5",
    "imo2024_p6",
    "uk2024_r1_p1",
    "uk2024_r1_p2",
    "usa2024_p2",
    "imo2023_p2_v2",
    "imo2023_p3",
    "imo2023_p4",
    "imo2023_p5",
)

NEGATIVE_FINDINGS: tuple[dict[str, str], ...] = (
    {
        "task_id": "imo2024_p2",
        "route": "gcd-normalization",
        "claim": "The naive gcd normalization route is invalid.",
        "evidence": "A concrete integer assignment breaks the proposed normalization.",
        "next_action": "Avoid the route and try a direct divisibility argument.",
    },
    {
        "task_id": "usa2024_p2",
        "route": "hU-lower-bound",
        "claim": "The candidate h_U lower bound is false without its missing premise.",
        "evidence": "A small admissible case violates the claimed lower bound.",
        "next_action": "State and prove the missing premise before using the bound.",
    },
    {
        "task_id": "uk2024_r1_p1",
        "route": "rfl-decide",
        "claim": "The direct rfl/decide route does not close this theorem.",
        "evidence": "The controlled check reaches a reproducible route failure.",
        "next_action": "Use an explicit intermediate lemma instead of retrying rfl/decide.",
    },
)

_NEGATIVE_BY_TASK = {item["task_id"]: item for item in NEGATIVE_FINDINGS}


def _task(slug: str) -> Task:
    return Task(
        slug=slug,
        root=Path("benchmarks") / slug,
        problem_text="synthetic replay task",
        baseline_code="theorem replay : True := by sorry\n",
        metadata={"problem_id": slug, "theorem_name": slug},
    )


def _prompt(treatment: bool) -> str:
    return build_task_prompt(
        _task("replay-task"),
        task_workspace="tasks/replay-task",
        agent_id="worker-replay",
        episode=1,
        communication_enabled=True,
        negative_piece_prompt=treatment,
    )


def _fixture_id() -> str:
    payload = {"tasks": TASKS, "negative_findings": NEGATIVE_FINDINGS}
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _structured_body(finding: Mapping[str, str]) -> str:
    return "\n".join(
        (
            f"Claim: {finding['claim']}",
            f"Evidence/counterexample: {finding['evidence']}",
            "Scope/preconditions: the same task statement and proof contract.",
            f"Consequence/next action: {finding['next_action']}",
        )
    )


def _run_arm(root: Path, *, treatment: bool) -> dict[str, Any]:
    arm_name = "treatment" if treatment else "control"
    arm_root = root / arm_name
    arm_root.mkdir(parents=True, exist_ok=True)
    os.chmod(arm_root, 0o700)
    store = CPSStore(arm_root / "cps.sqlite3")

    negative_message_ids: dict[str, str] = {}
    for index, task_id in enumerate(TASKS):
        finding = _NEGATIVE_BY_TASK.get(task_id)
        sender = f"worker-{task_id}-source"
        recipient = f"worker-{task_id}-next"
        if finding is None:
            body = f"route=baseline-handoff; current candidate handoff {index}"
        else:
            body = (
                f"route={finding['route']}; claim={finding['claim']}; "
                f"evidence={finding['evidence']}"
            )
        message = store.send_message(
            task_id=task_id,
            sender=sender,
            recipient=recipient,
            body=body,
        )

        # Existing positive knowledge is identical in both arms.  The only
        # treatment difference is a normal shared piece for reusable negative
        # findings, sent in addition to the same direct message.
        if finding is None:
            store.create_piece(
                task_id=task_id,
                author=sender,
                kind="handoff",
                title="Baseline candidate handoff",
                body="Inspect the current candidate before changing direction.",
                tags=["replay", "positive-baseline"],
            )
            continue
        negative_message_ids[task_id] = str(message["id"])
        if treatment:
            store.create_piece(
                task_id=task_id,
                author=sender,
                kind="negative_finding",
                title=f"Do not reuse {finding['route']}",
                body=_structured_body(finding),
                tags=["negative", "replay", finding["route"]],
            )

    structured_visible = 0
    message_visible = 0
    route_revisits = 0
    negative_piece_count = 0
    for finding in NEGATIVE_FINDINGS:
        task_id = finding["task_id"]
        pieces = store.search(task_id=task_id, query=finding["route"], limit=8)
        matching = [
            piece
            for piece in pieces
            if piece.get("kind") == "negative_finding"
            and finding["route"] in f"{piece.get('title', '')} {piece.get('body', '')}"
        ]
        if matching:
            structured_visible += 1
            negative_piece_count += len(matching)
        else:
            route_revisits += 1
        inbox = store.inbox(
            task_id=task_id,
            recipient=f"worker-{task_id}-next",
            limit=8,
        )
        if any(str(item.get("id")) == negative_message_ids[task_id] for item in inbox):
            message_visible += 1

    prompt = _prompt(treatment)
    summary = store.summary()
    for path in (
        arm_root / "cps.sqlite3",
        arm_root / "cps.sqlite3-wal",
        arm_root / "cps.sqlite3-shm",
    ):
        if path.exists():
            os.chmod(path, 0o600)
    return {
        "arm": arm_name,
        "negative_piece_prompt": treatment,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_bytes": len(prompt.encode("utf-8")),
        "prompt_contains_negative_rule": "reusable negative finding" in prompt,
        "task_count": len(TASKS),
        "negative_finding_count": len(NEGATIVE_FINDINGS),
        "message_count": summary["messages"],
        "piece_count": summary["pieces"],
        "negative_piece_count": negative_piece_count,
        "negative_messages_visible_to_intended_recipient": message_visible,
        "structured_negative_visible_to_fresh_worker": structured_visible,
        "message_only_negative_count": len(NEGATIVE_FINDINGS) - structured_visible,
        "simulated_route_revisit_count": route_revisits,
    }


def run(output: Path) -> dict[str, Any]:
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)

    control = _run_arm(output, treatment=False)
    treatment = _run_arm(output, treatment=True)
    report = {
        "schema_version": "contextswarm_negative_piece_prompt_replay_v1",
        "protocol": "same_messages_and_positive_pieces_plus_prompt_treatment_negative_piece",
        "fixture_id": _fixture_id(),
        "arms": [control, treatment],
        "delta_treatment_minus_control": {
            "prompt_bytes": treatment["prompt_bytes"] - control["prompt_bytes"],
            "negative_piece_count": treatment["negative_piece_count"]
            - control["negative_piece_count"],
            "structured_negative_visible_to_fresh_worker": treatment[
                "structured_negative_visible_to_fresh_worker"
            ]
            - control["structured_negative_visible_to_fresh_worker"],
            "message_only_negative_count": treatment["message_only_negative_count"]
            - control["message_only_negative_count"],
            "simulated_route_revisit_count": treatment["simulated_route_revisit_count"]
            - control["simulated_route_revisit_count"],
        },
        "interpretation": {
            "causal_scope": "deterministic CPS data-path and prompt-contract mechanism only",
            "control": "same direct messages and positive handoffs; no negative piece",
            "treatment": "same direct messages plus normal cps_publish negative_finding pieces",
            "not_measured": "model behavior, Judge score, proof quality, or adoption",
        },
    }
    report_path = output / "replay_summary.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(report_path, 0o600)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
