from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from contextswarm_mini.config import load_config
from contextswarm_mini.cps import CPSStore, make_policy
from contextswarm_mini.evaluator import MockEvaluator
from contextswarm_mini.models import AgentResult
from contextswarm_mini.runner import (
    RunLogger,
    _build_route_task_prompt,
    _finish_runtime_actor,
    _register_runtime_actor,
    _route_claim_settings,
    _run_task_workers,
    load_tasks,
)


ROOT = Path(__file__).resolve().parents[1]


class _Broker:
    @contextmanager
    def session(self, **_kwargs):
        yield {
            "CONTEXTSWARM_JUDGE_URL": "http://127.0.0.1:1/test-token",
            "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "9999999999999",
        }


class _Pi:
    def run(self, **kwargs):
        now = "2026-01-01T00:00:00+00:00"
        return AgentResult(
            agent_id=str(kwargs["actor_id"]),
            task_id=str(kwargs["task_id"]),
            episode=int(kwargs["episode"]),
            returncode=0,
            started_at=now,
            finished_at=now,
        )


class RunnerRouteClaimTests(unittest.TestCase):
    def test_route_settings_emit_integer_wire_ttl(self):
        for raw_ttl in (12.5, True):
            with self.subTest(raw_ttl=raw_ttl):
                class Features:
                    route_claim_required = True
                    route_claim_ttl_seconds = raw_ttl

                class Config:
                    cps_features = Features()

                enabled, required, ttl = _route_claim_settings(Config())
                self.assertTrue(enabled)
                self.assertTrue(required)
                self.assertEqual(ttl, 900)

    def test_explicit_negative_lifecycle_response_is_a_bypass(self):
        class Store:
            def register_actor(self, **_kwargs):
                return {"ok": False, "status": "rejected"}

            def finish_actor(self, **_kwargs):
                return {
                    "ok": False,
                    "found": False,
                    "status": "not_found",
                    "task_id": "task",
                    "actor_id": "actor",
                    "episode": 1,
                    "released_claim_ids": [],
                    "claims_released": 0,
                }

        store = Store()
        registered = _register_runtime_actor(
            store,
            task_id="task",
            actor_id="actor",
            episode=1,
            route_claims_enabled=True,
        )
        finished = _finish_runtime_actor(
            store,
            task_id="task",
            actor_id="actor",
            episode=1,
            route_claims_enabled=True,
        )
        self.assertEqual(registered[0:2], (False, "unavailable"))
        self.assertEqual(finished[0:2], (True, None))

        stale_finish = _finish_runtime_actor(
            store,
            task_id="task",
            actor_id="actor",
            episode=2,
            route_claims_enabled=True,
        )
        self.assertEqual(stale_finish[0:2], (False, "unavailable"))

        with tempfile.TemporaryDirectory() as missing_root:
            missing_finish = _finish_runtime_actor(
                CPSStore(Path(missing_root) / "missing.sqlite3"),
                task_id="missing",
                actor_id="actor",
                episode=3,
                route_claims_enabled=True,
            )
            self.assertEqual(missing_finish[0:2], (True, None))

        class MissingStore:
            def register_actor(self, **_kwargs):
                return {"ok": False, "status": "not_found", "found": False}

        missing = _register_runtime_actor(
            MissingStore(),
            task_id="task",
            actor_id="actor",
            episode=1,
            route_claims_enabled=True,
        )
        self.assertEqual(missing[0:2], (False, "unavailable"))

    def test_malformed_finish_success_is_not_marked_closed(self):
        class BareSuccess:
            def finish_actor(self, **_kwargs):
                return {"ok": True}

        result = _finish_runtime_actor(
            BareSuccess(),
            task_id="task",
            actor_id="actor",
            episode=1,
            route_claims_enabled=True,
        )
        self.assertEqual(result[0:2], (False, "unavailable"))

    def test_bare_register_success_is_not_marked_admitted(self):
        class BareSuccess:
            def register_actor(self, **_kwargs):
                return {"ok": True}

        result = _register_runtime_actor(
            BareSuccess(),
            task_id="task",
            actor_id="actor",
            episode=2,
            route_claims_enabled=True,
        )
        self.assertEqual(result[0:2], (False, "unavailable"))

    def test_terminal_or_sparse_register_success_is_not_marked_admitted(self):
        class Terminal:
            def register_actor(self, **_kwargs):
                return {
                    "task_id": "task",
                    "actor_id": "actor",
                    "episode": 1,
                    "status": "FINISHED",
                    "active": True,
                }

        class Sparse:
            def register_actor(self, **_kwargs):
                return {
                    "task_id": "task",
                    "actor_id": "actor",
                    "episode": 1,
                }

        class NonBooleanActive:
            def register_actor(self, **_kwargs):
                return {
                    "task_id": "task",
                    "actor_id": "actor",
                    "episode": 1,
                    "status": "admitted",
                    "active": "true",
                }

        for store in (Terminal(), Sparse(), NonBooleanActive()):
            with self.subTest(store=type(store).__name__):
                result = _register_runtime_actor(
                    store,
                    task_id="task",
                    actor_id="actor",
                    episode=1,
                    route_claims_enabled=True,
                )
                self.assertEqual(result[0:2], (False, "unavailable"))

    def test_register_rejects_non_string_identity_echoes(self):
        class NumericIdentity:
            def register_actor(self, **_kwargs):
                return {
                    "task_id": 123,
                    "actor_id": 456,
                    "episode": 1,
                    "status": "admitted",
                    "active": True,
                }

        result = _register_runtime_actor(
            NumericIdentity(),
            task_id="123",
            actor_id="456",
            episode=1,
            route_claims_enabled=True,
        )
        self.assertEqual(result[0:2], (False, "unavailable"))

    def test_unknown_or_reserved_register_status_is_not_marked_admitted(self):
        class StatusStore:
            def __init__(self, status):
                self.status = status

            def register_actor(self, **_kwargs):
                return {
                    "task_id": "task",
                    "actor_id": "actor",
                    "episode": 1,
                    "status": self.status,
                    "active": True,
                }

        for status in ("garbage", "queued", "pending", " ACTIVE "):
            with self.subTest(status=status):
                result = _register_runtime_actor(
                    StatusStore(status),
                    task_id="task",
                    actor_id="actor",
                    episode=1,
                    route_claims_enabled=True,
                )
                expected = status == " ACTIVE "
                self.assertEqual(result[0], expected)
                self.assertEqual(result[1], None if expected else "unavailable")

    def test_finish_requires_terminal_bound_row_and_release_accounting(self):
        class Sparse:
            def finish_actor(self, **_kwargs):
                return {
                    "ok": True,
                    "found": True,
                    "task_id": "task",
                    "actor_id": "actor",
                    "episode": 1,
                    "status": "finished",
                }

        class Complete:
            def finish_actor(self, **_kwargs):
                return {
                    "ok": True,
                    "found": True,
                    "task_id": "task",
                    "actor_id": "actor",
                    "episode": 1,
                    "status": "finished",
                    "active": False,
                    "released_claim_ids": [],
                    "claims_released": 0,
                }

        class NonBooleanActive:
            def finish_actor(self, **_kwargs):
                return {
                    "ok": True,
                    "found": True,
                    "task_id": "task",
                    "actor_id": "actor",
                    "episode": 1,
                    "status": "finished",
                    "active": "false",
                    "released_claim_ids": [],
                    "claims_released": 0,
                }

        sparse = _finish_runtime_actor(
            Sparse(),
            task_id="task",
            actor_id="actor",
            episode=1,
            route_claims_enabled=True,
        )
        complete = _finish_runtime_actor(
            Complete(),
            task_id="task",
            actor_id="actor",
            episode=1,
            route_claims_enabled=True,
        )
        malformed_active = _finish_runtime_actor(
            NonBooleanActive(),
            task_id="task",
            actor_id="actor",
            episode=1,
            route_claims_enabled=True,
        )
        self.assertEqual(sparse[0:2], (False, "unavailable"))
        self.assertEqual(complete[0:2], (True, None))
        self.assertEqual(malformed_active[0:2], (False, "unavailable"))

    def test_finish_episode_mismatch_is_retryable(self):
        class WrongEpisode:
            def finish_actor(self, **_kwargs):
                return {
                    "ok": True,
                    "found": True,
                    "task_id": "task",
                    "actor_id": "actor",
                    "episode": 1,
                }

        result = _finish_runtime_actor(
            WrongEpisode(),
            task_id="task",
            actor_id="actor",
            episode=2,
            route_claims_enabled=True,
        )
        self.assertEqual(result[0:2], (False, "unavailable"))

    def test_treatment_prompt_does_not_inject_pre_judge_digest(self):
        config = load_config("configs/smoke.toml", ROOT)
        task = load_tasks(config)[0]
        prompt = _build_route_task_prompt(
            task,
            task_workspace="/run/task",
            agent_id="worker",
            episode=1,
            communication_enabled=True,
            formal_tools_enabled=False,
            direct_messages=True,
            selection_enabled=False,
            digest="PRIVATE PRIOR PIECE MUST NOT APPEAR",
            route_claims_enabled=True,
            route_claim_required=True,
            route_claim_ttl_seconds=60,
        )
        self.assertNotIn("PRIVATE PRIOR PIECE", prompt)

    def test_task_worker_roster_contains_only_actual_admissions_and_closes_them(self):
        base = load_config("configs/smoke.toml", ROOT)
        config = replace(base, max_tasks=1, episodes_per_task=2, max_parallel=1, time_limit_seconds=2)
        task = load_tasks(config)[0]
        calls: list[tuple[str, dict[str, object]]] = []

        def register(store, **kwargs):
            del store
            calls.append(("register", dict(kwargs)))
            return True, None, {"ok": True}

        def finish(store, **kwargs):
            del store
            calls.append(("finish", dict(kwargs)))
            return True, None, {"ok": True}

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            logger = RunLogger(run_dir)
            policy = make_policy(config.communication, CPSStore(run_dir / "cps.sqlite3"))
            with patch("contextswarm_mini.runner._route_claim_settings", return_value=(True, True, 60)), patch(
                "contextswarm_mini.runner._register_runtime_actor", side_effect=register
            ), patch("contextswarm_mini.runner._finish_runtime_actor", side_effect=finish):
                results = _run_task_workers(
                    config,
                    [task],
                    run_dir,
                    logger,
                    MockEvaluator(),
                    _Pi(),
                    policy,
                    mock_agent=False,
                    deadline=time.monotonic() + 1.5,
                    evaluator_gate=threading.BoundedSemaphore(1),
                    judge_broker=_Broker(),
                )
            self.assertEqual(len(results), 1)
            roster = json.loads((run_dir / "actors.json").read_text(encoding="utf-8"))
            self.assertEqual(
                roster,
                [
                    {"actor_id": "worker-" + task.slug + "-e1", "episode": 1, "task_id": task.slug},
                    {"actor_id": "worker-" + task.slug + "-e2", "episode": 2, "task_id": task.slug},
                ],
            )
            self.assertEqual([kind for kind, _ in calls], ["register", "finish", "register", "finish"])
            self.assertEqual(
                [item[1].get("episode") for item in calls if item[0] == "register"],
                [1, 2],
            )


if __name__ == "__main__":
    unittest.main()
