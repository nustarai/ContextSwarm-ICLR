#!/usr/bin/env python3
"""Exercise the active-roster/route-claim protocol with synthetic actors.

This is an offline protocol harness, not a benchmark workload.  It uses a
fresh CPS SQLite database and a local JudgeBroker with a non-network evaluator
stub, then writes bounded JSON metrics that can be compared with the historical
profiling runs without exposing prompts, credentials, or candidate contents.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import shutil
import sys
import threading
from typing import Any
import uuid
from urllib.request import Request, urlopen

# Allow direct execution from any working directory without relying on an
# operator-provided PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextswarm_mini.cps import CPSStore
from contextswarm_mini.judge_broker import JudgeBroker
from contextswarm_mini.models import Task, Verdict


def _post(url: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{url.rstrip('/')}/{operation}",
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:  # noqa: S310 - loopback broker only.
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise RuntimeError(f"broker returned a non-object for {operation}")
    return value


class _OfflineEvaluator:
    def expected_task_contract_sha256(self, _task: Task) -> str:
        return "a" * 64

    def probe(
        self,
        task: Task,
        _candidate: Path,
        *,
        deadline_monotonic: float | None,
    ) -> Verdict:
        del deadline_monotonic
        return Verdict(
            task.slug,
            "VERIFY_FAIL",
            0.0,
            0.0,
            task_contract_sha256="a" * 64,
            judge_job_id="offline-route-protocol",
        )


class _AggregateProfiler:
    """Capture only bounded operation/status labels for this offline smoke."""

    enabled = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[tuple[str, str | None, str | None]] = []

    def emit(self, event: str, **fields: Any) -> None:
        with self._lock:
            self._rows.append(
                (
                    str(event)[:128],
                    str(fields.get("db_operation"))[:128]
                    if fields.get("db_operation") is not None
                    else None,
                    str(fields.get("status"))[:64]
                    if fields.get("status") is not None
                    else None,
                )
            )

    def snapshot(self) -> tuple[tuple[str, str | None, str | None], ...]:
        with self._lock:
            return tuple(self._rows)


def _task(root: Path, slug: str) -> Task:
    return Task(
        slug=slug,
        root=root,
        problem_text="synthetic protocol task",
        baseline_code="import Mathlib\ntheorem synthetic : True := by sorry\n",
        metadata={"problem_id": slug, "theorem_name": "synthetic"},
    )


def _run(output: Path) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    # Every invocation gets a fresh database. Reusing a thread-id directory
    # would accumulate lifecycle events across repeated smoke runs and make
    # the reported denominators look like one long experiment.
    work_root = output.parent / f".route-protocol-{uuid.uuid4().hex}"
    work_root.mkdir(parents=True, exist_ok=True)
    db_path = work_root / "cps.sqlite3"
    profiler = _AggregateProfiler()
    store = CPSStore(db_path, profiler=profiler)
    task_id = "synthetic-task"

    # Admission-only roster visibility: future actors are absent until the
    # runner calls register_actor for them.
    store.register_actor(task_id, "actor-a", 1, now=100)
    roster_after_a = store.list_active_actors(task_id, now=100)
    store.register_actor(task_id, "actor-b", 1, now=100)
    store.register_actor(task_id, "actor-c", 1, now=100)

    barrier = threading.Barrier(3)

    def race(actor_id: str) -> dict[str, Any]:
        barrier.wait(timeout=5)
        return store.claim_route(
            task_id,
            actor_id,
            1,
            "algebra/same-route",
            f"synthetic route by {actor_id}",
            ttl_seconds=60,
            now=200,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        race_results = list(pool.map(race, ("actor-a", "actor-b", "actor-c")))
    primary_results = [
        item for item in race_results if item.get("acquired") and item.get("primary")
    ]
    conflict_results = [item for item in race_results if item.get("status") == "conflict"]

    store.register_actor(task_id, "independent-verifier", 1, now=201)
    independent = store.claim_route(
        task_id,
        "independent-verifier",
        1,
        "algebra/same-route",
        "independent synthetic re-derivation",
        independent_verification_reason="check the boundary case with a separate lemma",
        ttl_seconds=60,
        now=201,
    )

    primary_actor = str(primary_results[0]["claim"]["actor_id"])
    store.finish_actor(task_id, primary_actor, "finished", episode=1, now=202)
    routes_after_primary_finish = store.list_active_routes(task_id, now=202)
    store.finish_actor(task_id, "independent-verifier", "finished", episode=1, now=203)
    routes_after_all_finish = store.list_active_routes(task_id, now=203)

    # TTL release and primary reclaim.
    store.register_actor(task_id, "ttl-owner", 2, now=300)
    expiring = store.claim_route(
        task_id,
        "ttl-owner",
        2,
        "ttl-route",
        "short synthetic lease",
        ttl_seconds=1,
        now=300,
    )
    expired_view = store.list_active_routes(task_id, now=302)
    store.register_actor(task_id, "ttl-reclaimer", 2, now=302)
    reclaimed = store.claim_route(
        task_id,
        "ttl-reclaimer",
        2,
        "ttl-route",
        "reclaimed synthetic lease",
        ttl_seconds=60,
        now=302,
    )

    # Explicit update/release lifecycle, separate from automatic actor finish
    # and TTL expiry above.
    store.register_actor(task_id, "mutable-owner", 3, now=350)
    mutable = store.claim_route(
        task_id,
        "mutable-owner",
        3,
        "mutable-route",
        "route to update and release",
        ttl_seconds=60,
        now=350,
    )
    mutable_claim_id = str(mutable["claim_id"])
    blocked = store.update_route_claim(
        mutable_claim_id,
        "mutable-owner",
        task_id=task_id,
        episode=3,
        status="blocked",
        summary="waiting on a synthetic lemma",
        now=351,
    )
    blocked_view = store.list_active_routes(task_id, now=351)
    released = store.release_route_claim(
        mutable_claim_id,
        "mutable-owner",
        task_id=task_id,
        episode=3,
        reason="synthetic route completed",
        now=352,
    )
    released_view = store.list_active_routes(task_id, now=352)
    store.finish_actor(task_id, "mutable-owner", "finished", episode=3, now=353)

    # Broker ordering gate on a separate fresh task/actor.  No Judge request is
    # needed: the pre-checkpoint surface itself is the assertion here.
    gate_task_id = "gate-task"
    gate_actor_id = "gate-actor"
    store.register_actor(gate_task_id, gate_actor_id, 1, now=400)
    gate_root = work_root / "gate"
    gate_root.mkdir(parents=True, exist_ok=True)
    candidate = gate_root / "result.lean"
    candidate.write_text("proof\n", encoding="utf-8")
    gate_task = _task(gate_root, gate_task_id)
    broker = JudgeBroker(
        _OfflineEvaluator(),
        threading.BoundedSemaphore(1),
        audit_path=work_root / "broker-audit.jsonl",
        min_probe_interval_seconds=0,
        route_claims_enabled=True,
        route_claim_required=True,
    ).start()
    try:
        import time

        with broker.session(
            actor_id=gate_actor_id,
            episode=1,
            workdir=gate_root,
            candidates={gate_task_id: (gate_task, candidate)},
            deadline_monotonic=time.monotonic() + 10,
            cps_store=store,
            communication="blackboard",
            route_claims_enabled=True,
            route_claim_required=True,
        ) as env:
            broker_url = env["CONTEXTSWARM_JUDGE_URL"]
            gate_active = _post(broker_url, "cps_active_routes", {})
            gate_search = _post(broker_url, "cps_search", {"query": "before-judge"})
    finally:
        broker.close()

    with store._db() as db:  # noqa: SLF001 - bounded synthetic audit summary.
        event_types = Counter(row[0] for row in db.execute("SELECT event_type FROM events"))
    profile_rows = profiler.snapshot()
    write_commits = [row for row in profile_rows if row[0] == "cps.write.commit"]

    result = {
        "schema_version": "contextswarm_route_claim_protocol_smoke_v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "offline_only": True,
        "task_ids": [task_id, gate_task_id],
        "roster": {
            "after_first_admission_actor_ids": [
                row["actor_id"] for row in roster_after_a
            ],
            "future_actor_visible_before_admission": any(
                row["actor_id"] in {"actor-b", "actor-c"} for row in roster_after_a
            ),
            "race_admitted_actor_count": len(
                store.list_active_actors(task_id, now=200)
            ),
        },
        "primary_race": {
            "attempts": len(race_results),
            "primary_acquired": len(primary_results),
            "conflicts": len(conflict_results),
            "all_claim_results_accounted": len(primary_results) + len(conflict_results)
            == len(race_results),
        },
        "independent_verification": {
            "ok": bool(independent.get("ok")),
            "acquired": bool(independent.get("acquired")),
            "is_primary": bool(independent.get("claim", {}).get("is_primary")),
            "accepted_reason": bool(
                independent.get("claim", {}).get("independent_verification_reason")
            ),
        },
        "lifecycle": {
            "active_routes_after_primary_finish": len(routes_after_primary_finish),
            "active_routes_after_all_finish": len(routes_after_all_finish),
            "ttl_claim_id": expiring.get("claim_id"),
            "ttl_expired_from_active_view": not any(
                row.get("claim_id") == expiring.get("claim_id") for row in expired_view
            ),
            "ttl_reclaim_acquired": bool(reclaimed.get("acquired")),
            "ttl_reclaim_primary": bool(reclaimed.get("claim", {}).get("is_primary")),
            "blocked_update_visible": bool(blocked.get("ok"))
            and any(
                row.get("claim_id") == mutable_claim_id
                and row.get("status") == "blocked"
                for row in blocked_view
            ),
            "explicit_release_status": released.get("claim", {}).get("status"),
            "explicit_release_absent_from_active_view": not any(
                row.get("claim_id") == mutable_claim_id for row in released_view
            ),
        },
        "pre_judge_gate": {
            "active_routes_ok": gate_active.get("ok") is True
            and gate_active.get("bypassed") is not True,
            "search_status": gate_search.get("status"),
            "search_blocked_before_judge": gate_search.get("status")
            == "JUDGE_CHECK_REQUIRED",
        },
        "profiling": {
            "write_commit_counts": dict(
                sorted(
                    Counter(
                        operation or "unknown"
                        for _, operation, _ in write_commits
                    ).items()
                )
            ),
            "non_ok_write_commits": sum(
                status != "ok" for _, _, status in write_commits
            ),
        },
        "event_counts": dict(sorted(event_types.items())),
    }
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.rmtree(work_root)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = _run(args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
