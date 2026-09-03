from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest

from contextswarm_mini.cps import CPSStore


class CPSRouteClaimTests(unittest.TestCase):
    def make_store(self) -> tuple[tempfile.TemporaryDirectory[str], CPSStore]:
        temporary = tempfile.TemporaryDirectory()
        return temporary, CPSStore(Path(temporary.name) / "cps.sqlite3")

    def test_schema_and_unregistered_actor_gate(self) -> None:
        temporary, store = self.make_store()
        try:
            with sqlite3.connect(store.path) as db:
                names = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertTrue({"actors", "route_claims"}.issubset(names))
            result = store.claim_route(
                "task",
                "not-admitted",
                1,
                "algebra/factor",
                "try factoring",
                now=100,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "actor_not_admitted")
            self.assertFalse(store.list_active_routes("task", now=100))
        finally:
            temporary.cleanup()

    def test_concurrent_primary_race_has_one_winner(self) -> None:
        temporary, store = self.make_store()
        try:
            for actor in ("a", "b", "c"):
                store.register_actor("task", actor, 1, now=100)
            barrier = threading.Barrier(3)

            def attempt(actor: str) -> dict[str, object]:
                barrier.wait(timeout=5)
                return store.claim_route(
                    "task",
                    actor,
                    1,
                    "same/route",
                    f"summary-{actor}",
                    now=200,
                    ttl_seconds=60,
                )

            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(attempt, ("a", "b", "c")))
            self.assertEqual(sum(bool(item.get("acquired")) for item in results), 1)
            self.assertEqual(sum(item.get("status") == "conflict" for item in results), 2)
            routes = store.list_active_routes("task", now=200)
            self.assertEqual(len(routes), 1)
            self.assertTrue(routes[0]["is_primary"])
            self.assertEqual(
                sum(
                    row[0] == "route_claim_created"
                    for row in sqlite3.connect(store.path).execute(
                        "SELECT event_type FROM events"
                    )
                ),
                1,
            )
        finally:
            temporary.cleanup()

    def test_independent_verification_is_secondary_and_visible(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "primary", 1, now=100)
            store.register_actor("task", "checker", 1, now=100)
            first = store.claim_route("task", "primary", 1, "geometry", "main", now=100)
            second = store.claim_route(
                "task",
                "checker",
                1,
                "geometry",
                "independent check",
                independent_verification_reason="verify the same route with a separate lemma",
                now=101,
            )
            self.assertTrue(first["claim"]["is_primary"])
            self.assertTrue(second["ok"])
            self.assertFalse(second["claim"]["is_primary"])
            self.assertEqual(
                second["claim"]["independent_verification_reason"],
                "verify the same route with a separate lemma",
            )
            self.assertEqual(len(store.list_active_routes("task", now=101)), 2)
        finally:
            temporary.cleanup()

    def test_finish_actor_releases_claims_and_ttl_allows_reclaim(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "a", 1, now=100)
            store.register_actor("task", "b", 1, now=100)
            first = store.claim_route("task", "a", 1, "number-theory", "bound", now=100, ttl_seconds=5)
            finished = store.finish_actor("task", "a", "solved_by_peer", reason="peer proved it", now=102)
            self.assertEqual(finished["status"], "solved_by_peer")
            self.assertEqual(finished["claims_released"], 1)
            self.assertFalse(store.list_active_routes("task", now=102))
            reopened = store.claim_route("task", "b", 1, "number-theory", "new attempt", now=103)
            self.assertTrue(reopened["acquired"])
            self.assertTrue(reopened["claim"]["is_primary"])
            store.finish_actor("task", "b", "finished", reason="test cleanup", now=104)
            self.assertFalse(store.list_active_routes("task", now=104))

            # A fresh claim expires without requiring an actor finish call.
            store.register_actor("task", "a", 2, now=200)
            expiring = store.claim_route("task", "a", 2, "ttl-route", "short", now=200, ttl_seconds=1)
            self.assertTrue(expiring["acquired"])
            self.assertFalse(store.list_active_routes("task", now=202))
            store.register_actor("task", "b", 2, now=202)
            reclaimed = store.claim_route("task", "b", 2, "ttl-route", "reclaimed", now=202)
            self.assertTrue(reclaimed["acquired"])
            with sqlite3.connect(store.path) as db:
                statuses = {
                    row[0]
                    for row in db.execute(
                        "SELECT status FROM route_claims WHERE claim_id=?", (expiring["claim_id"],)
                    )
                }
            self.assertEqual(statuses, {"released"})
        finally:
            temporary.cleanup()

    def test_update_release_heartbeat_and_bounded_events(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "a", 1, metadata={"source": "runner", "secret": "x"}, now=100)
            claim = store.claim_route("task", "a", 1, "route", "initial", now=100, ttl_seconds=10)
            claim_id = str(claim["claim_id"])
            updated = store.update_route_claim(
                claim_id=claim_id,
                actor_id="a",
                status="blocked",
                summary="waiting for a counterexample",
                now=101,
            )
            self.assertTrue(updated["ok"])
            self.assertFalse(updated["acquired"])
            self.assertFalse(updated["claimed"])
            self.assertEqual(updated["claim"]["status"], "blocked")
            self.assertTrue(updated["claim"]["active"])
            self.assertTrue(store.heartbeat_actor("task", "a", status="running", now=102)["ok"])
            released = store.release_route_claim(
                claim_id,
                "a",
                reason="route abandoned",
                now=103,
            )
            self.assertTrue(released["ok"])
            self.assertEqual(released["claim"]["status"], "released")
            self.assertEqual(released["claim"]["release_reason"], "route abandoned")
            with sqlite3.connect(store.path) as db:
                payloads = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT payload FROM events WHERE event_type LIKE 'route_claim%'"
                    )
                ]
            self.assertTrue(payloads)
            self.assertTrue(all(len(json.dumps(item, ensure_ascii=False)) <= 4_000 for item in payloads))
        finally:
            temporary.cleanup()

    def test_idempotent_retry_of_blocked_claim_is_not_a_write_lease(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "a", 1, now=100)
            claim = store.claim_route("task", "a", 1, "route", "initial", now=100)
            claim_id = str(claim["claim_id"])
            blocked = store.update_route_claim(
                claim_id=claim_id,
                actor_id="a",
                status="blocked",
                now=101,
            )
            self.assertTrue(blocked["ok"])
            self.assertFalse(blocked["acquired"])
            self.assertFalse(blocked["claimed"])

            retry = store.claim_route(
                "task",
                "a",
                1,
                "route",
                "retry while blocked",
                now=102,
            )
            self.assertTrue(retry["ok"])
            self.assertTrue(retry["idempotent"])
            self.assertFalse(retry["acquired"])
            self.assertFalse(retry["claimed"])
            self.assertEqual(retry["claim"]["status"], "blocked")
            self.assertTrue(retry["claim"]["active"])
        finally:
            temporary.cleanup()

    def test_update_and_release_can_be_bound_to_task_and_episode(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task-a", "worker", 1, now=100)
            store.register_actor("task-b", "worker", 1, now=100)
            claim = store.claim_route(
                "task-a", "worker", 1, "route", "task a", now=100
            )
            claim_id = str(claim["claim_id"])
            wrong_task = store.update_route_claim(
                claim_id,
                "worker",
                task_id="task-b",
                episode=1,
                status="blocked",
                now=101,
            )
            self.assertFalse(wrong_task["ok"])
            self.assertFalse(wrong_task["found"])
            wrong_episode = store.release_route_claim(
                claim_id,
                "worker",
                task_id="task-a",
                episode=2,
                now=101,
            )
            self.assertFalse(wrong_episode["ok"])
            self.assertFalse(wrong_episode["found"])
            current = store.update_route_claim(
                claim_id,
                "worker",
                task_id="task-a",
                episode=1,
                status="blocked",
                now=102,
            )
            self.assertTrue(current["ok"])
            self.assertEqual(current["claim"]["status"], "blocked")
        finally:
            temporary.cleanup()

    def test_proved_actor_is_not_listed_as_active(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "prover", 1, now=100)
            store.claim_route("task", "prover", 1, "proof", "completed", now=100)
            finished = store.finish_actor("task", "prover", "proved", now=101)
            self.assertFalse(finished["active"])
            self.assertFalse(store.list_active_actors("task", now=101))
            self.assertFalse(store.list_active_routes("task", now=101))
        finally:
            temporary.cleanup()

    def test_claim_rejects_stale_episode_for_reused_actor_id(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "worker", 2, now=100)
            result = store.claim_route(
                "task",
                "worker",
                1,
                "stale-route",
                "old session",
                now=101,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "episode_mismatch")
            self.assertEqual(result["registered_episode"], 2)
            self.assertFalse(store.list_active_routes("task", now=101))
        finally:
            temporary.cleanup()

    def test_reregistered_actor_releases_old_episode_and_stale_finish_cannot_close_new_claim(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "worker", 1, now=100)
            old_claim = store.claim_route(
                "task", "worker", 1, "route", "episode one", now=100
            )
            self.assertTrue(old_claim["acquired"])

            # A reused actor id is a new admission.  Its previous live claims
            # must be released before the row is reopened for episode 2.
            reopened = store.register_actor("task", "worker", 2, now=101)
            self.assertEqual(reopened["episode"], 2)
            self.assertFalse(store.list_active_routes("task", now=101))
            with sqlite3.connect(store.path) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT status, release_reason FROM route_claims WHERE claim_id=?",
                        (old_claim["claim_id"],),
                    ).fetchone(),
                    ("released", "actor_re_registered"),
                )

            new_claim = store.claim_route(
                "task", "worker", 2, "route", "episode two", now=101
            )
            self.assertTrue(new_claim["acquired"])

            # A delayed closeout from episode 1 must not release episode 2's
            # claim.  The episode guard is checked inside the same write txn.
            stale_finish = store.finish_actor(
                "task", "worker", "finished", episode=1, now=102
            )
            self.assertFalse(stale_finish["ok"])
            self.assertEqual(stale_finish["status"], "episode_mismatch")
            self.assertTrue(store.list_active_routes("task", now=102))
            current_finish = store.finish_actor(
                "task", "worker", "finished", episode=2, now=103
            )
            self.assertTrue(current_finish["ok"])
            self.assertFalse(store.list_active_routes("task", now=103))
        finally:
            temporary.cleanup()

    def test_terminal_status_registry_drives_active_roster_filter(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "worker", 1, now=100)
            # Simulate a future runner closeout label without changing the
            # query implementation.  It should remain hidden only when the
            # shared terminal registry knows about it; this test also guards
            # the current proved/finished labels through the public API.
            with sqlite3.connect(store.path) as db:
                db.execute(
                    "UPDATE actors SET status='proved', updated_at=? WHERE task_id=? AND actor_id=?",
                    ("1970-01-01T00:01:41Z", "task", "worker"),
                )
            self.assertFalse(store.list_active_actors("task", now=101))
        finally:
            temporary.cleanup()

    def test_same_episode_terminal_actor_cannot_be_resurrected(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "worker", 1, now=100)
            store.finish_actor("task", "worker", "finished", now=101)
            rejected = store.register_actor("task", "worker", 1, now=102)
            self.assertFalse(rejected["ok"])
            self.assertTrue(rejected["found"])
            self.assertEqual(rejected["status"], "actor_finished")
            self.assertFalse(rejected["active"])
            active = store.list_active_actors("task", now=102)
            self.assertFalse(active)
            with sqlite3.connect(store.path) as db:
                self.assertIsNotNone(
                    db.execute(
                        "SELECT 1 FROM events WHERE event_type='actor_registration_rejected'"
                    ).fetchone()
                )
        finally:
            temporary.cleanup()

    def test_unqualified_finish_binds_only_current_actor_episode(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "worker", 1, now=100)
            first = store.claim_route("task", "worker", 1, "route-one", "old", now=100)
            self.assertTrue(first["acquired"])
            # Re-admission under a new episode releases the old claim and
            # creates a current actor row.  An omitted episode is retained as
            # a compatibility spelling, but must bind to that current row
            # rather than scanning every historical episode.
            store.register_actor("task", "worker", 2, now=101)
            second = store.claim_route("task", "worker", 2, "route-two", "new", now=101)
            self.assertTrue(second["acquired"])
            finished = store.finish_actor("task", "worker", "finished", now=102)
            self.assertTrue(finished["ok"])
            self.assertEqual(finished["claims_released"], 1)
            self.assertFalse(store.list_active_routes("task", now=102))
            with sqlite3.connect(store.path) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT status FROM route_claims WHERE claim_id=?",
                        (first["claim_id"],),
                    ).fetchone()[0],
                    "released",
                )
        finally:
            temporary.cleanup()

    def test_episode_identity_rejects_fractional_values(self) -> None:
        temporary, store = self.make_store()
        try:
            with self.assertRaises(ValueError):
                store.register_actor("task", "worker", 1.5, now=100)
            with self.assertRaises(ValueError):
                store.register_actor("task", "worker", True, now=100)
        finally:
            temporary.cleanup()

    def test_uppercase_terminal_status_is_not_active(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "worker", 1, now=100)
            with sqlite3.connect(store.path) as db:
                db.execute(
                    "UPDATE actors SET status='FINISHED' WHERE task_id=? AND actor_id=?",
                    ("task", "worker"),
                )
            self.assertFalse(store.list_active_actors("task", now=101))
        finally:
            temporary.cleanup()

    def test_unknown_actor_status_cannot_claim_or_appear_active(self) -> None:
        temporary, store = self.make_store()
        try:
            store.register_actor("task", "worker", 1, now=100)
            # Simulate a legacy/corrupt row without bypassing the SQLite schema.
            with sqlite3.connect(store.path) as db:
                db.execute(
                    "UPDATE actors SET status='garbage' WHERE task_id=? AND actor_id=?",
                    ("task", "worker"),
                )
            self.assertFalse(store.list_active_actors("task", now=101))
            result = store.claim_route(
                "task", "worker", 1, "route", "must reject", now=101
            )
            self.assertFalse(result["acquired"])
            self.assertEqual(result["status"], "invalid_actor_status")
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
