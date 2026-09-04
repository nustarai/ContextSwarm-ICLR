from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest

from contextswarm_mini.config import load_config
from contextswarm_mini.cps import CPSStore
from contextswarm_mini.models import Task
from contextswarm_mini.pi_agent import PiAgent
from contextswarm_mini.prompts import build_task_prompt
from contextswarm_mini.runner import _activity_feedback_settings, _peer_activity_context


ROOT = Path(__file__).resolve().parents[1]


def _task() -> Task:
    return Task(
        slug="sample",
        root=Path("benchmarks/sample"),
        problem_text="Find a proof.",
        baseline_code="theorem sample : True := by sorry",
        metadata={},
    )


class ActivityFeedbackTests(unittest.TestCase):
    def test_activity_mode_keeps_descriptions_and_allows_same_opaque_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CPSStore(Path(temporary) / "cps.sqlite3")
            store.register_actor("sample", "actor-a", 1, now=100)
            store.register_actor("sample", "actor-b", 1, now=100)
            store.register_actor("sample", "actor-c", 1, now=100)

            legacy = store.claim_route(
                "sample", "actor-a", 1, "same-handle", "Test a parity invariant", now=101
            )
            self.assertTrue(legacy["acquired"])
            self.assertEqual(legacy["claim"]["route_key_semantics"], "unique")

            activity = store.claim_route(
                "sample",
                "actor-b",
                1,
                "same-handle",
                "Check the complementary odd case",
                enforce_route_uniqueness=False,
                now=102,
            )
            self.assertTrue(activity["acquired"])
            self.assertTrue(activity["claim"]["is_primary"])
            self.assertEqual(activity["claim"]["route_key_semantics"], "opaque")
            self.assertEqual(
                activity["claim"]["activity_description"],
                "Check the complementary odd case",
            )

            active = store.list_active_routes("sample", now=102)
            self.assertEqual(len(active), 2)
            self.assertEqual(
                {row["summary"] for row in active},
                {"Test a parity invariant", "Check the complementary odd case"},
            )

            # The legacy mode remains available and still reports the first
            # primary as a conflict; activity mode is an explicit treatment
            # choice, not a global weakening of the CPS store contract.
            conflict = store.claim_route(
                "sample", "actor-c", 1, "same-handle", "retry", now=103
            )
            self.assertEqual(conflict["status"], "conflict")

    def test_semantics_aware_index_preserves_legacy_uniqueness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cps.sqlite3"
            store = CPSStore(path)
            store.register_actor("sample", "legacy-a", 1, now=100)
            store.register_actor("sample", "legacy-b", 1, now=100)
            store.register_actor("sample", "activity", 1, now=100)

            first = store.claim_route(
                "sample", "legacy-a", 1, "same-handle", "legacy", now=101
            )
            self.assertTrue(first["acquired"])
            blocked = store.claim_route(
                "sample", "legacy-b", 1, "same-handle", "legacy retry", now=101
            )
            self.assertEqual(blocked["status"], "conflict")

            # An opaque activity row with the same textual handle is not
            # treated as a legacy uniqueness owner, and the index still
            # still rejects a second ``unique`` primary if a caller later uses
            # the compatibility path.
            activity = store.claim_route(
                "sample",
                "activity",
                1,
                "same-handle",
                "activity description",
                enforce_route_uniqueness=False,
                now=102,
            )
            self.assertTrue(activity["acquired"])
            with sqlite3.connect(path) as db:
                index = db.execute(
                    "SELECT sql FROM sqlite_master WHERE name='route_claims_unique_primary'"
                ).fetchone()
            self.assertIsNotNone(index)
            assert index is not None
            self.assertIn("route_key_semantics", index[0])

    def test_activity_mode_allows_concurrent_same_handle_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CPSStore(Path(temporary) / "cps.sqlite3")
            actors = [f"activity-{index}" for index in range(6)]
            for actor in actors:
                store.register_actor("sample", actor, 1, now=100)
            barrier = threading.Barrier(len(actors))

            def claim(actor: str) -> dict[str, object]:
                barrier.wait(timeout=5)
                return store.claim_route(
                    "sample",
                    actor,
                    1,
                    "same-opaque-handle",
                    f"Explore a distinct subcase for {actor}",
                    enforce_route_uniqueness=False,
                    now=101,
                )

            with ThreadPoolExecutor(max_workers=len(actors)) as pool:
                results = list(pool.map(claim, actors))
            self.assertEqual(sum(bool(result["acquired"]) for result in results), len(actors))
            self.assertTrue(all(result["primary"] for result in results))
            active = store.list_active_routes("sample", now=101)
            self.assertEqual(len(active), len(actors))
            self.assertEqual(
                {row["summary"] for row in active},
                {f"Explore a distinct subcase for {actor}" for actor in actors},
            )

    def test_peer_snapshot_is_bounded_task_scoped_and_omits_route_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CPSStore(Path(temporary) / "cps.sqlite3")
            store.register_actor("sample", "actor-a", 1)
            store.register_actor("sample", "actor-b", 1)
            store.register_actor("other", "other-actor", 1)
            store.claim_route(
                "sample",
                "actor-a",
                1,
                "technical-handle-a",
                "Explore a valuation bound",
                enforce_route_uniqueness=False,
            )
            store.claim_route(
                "sample",
                "actor-b",
                1,
                "technical-handle-b",
                "Explore a modular obstruction",
                enforce_route_uniqueness=False,
            )
            store.claim_route(
                "other",
                "other-actor",
                1,
                "other-handle",
                "Must not leak across tasks",
                enforce_route_uniqueness=False,
            )

            snapshot = _peer_activity_context(
                store, task_id="sample", actor_id="actor-b"
            )
            self.assertIn("Explore a valuation bound", snapshot)
            self.assertNotIn("Explore a modular obstruction", snapshot)
            self.assertNotIn("technical-handle-a", snapshot)
            self.assertNotIn("Must not leak across tasks", snapshot)

    def test_prompt_labels_peer_activity_as_advisory_not_a_uniqueness_filter(self) -> None:
        prompt = build_task_prompt(
            _task(),
            task_workspace="tasks/sample",
            agent_id="actor-b",
            episode=1,
            communication_enabled=True,
            route_claim_required=True,
            activity_feedback_enabled=True,
            concurrent_activity="- actor-a (episode 1, active): Explore a valuation bound",
        )
        self.assertIn("Concurrent peer activity (ephemeral, advisory, and task-scoped)", prompt)
        self.assertIn("Explore a valuation bound", prompt)
        self.assertIn("not a uniqueness filter", prompt)
        self.assertIn("Use your own judgment about whether to avoid or repeat", prompt)

    def test_pi_surface_and_environment_carry_activity_treatment(self) -> None:
        config = load_config("configs/route_claim_smoke.toml", ROOT)
        self.assertTrue(_activity_feedback_settings(config, True))
        agent = PiAgent(config)
        tools = set(agent.solver_tools())
        self.assertTrue({"cps_active_routes", "cps_claim_route"} <= tools)
        command = agent.command()
        system_prompt = command[command.index("--system-prompt") + 1]
        self.assertIn("Peer-activity feedback is enabled", system_prompt)
        with tempfile.TemporaryDirectory() as temporary:
            environment = agent.environment(
                task_id="sample",
                actor_id="actor-b",
                episode=1,
                workdir=Path(temporary),
            )
        self.assertEqual(environment["CONTEXTSWARM_CPS_ACTIVITY_FEEDBACK_ENABLED"], "1")

    def test_node_surface_can_be_enabled_by_activity_bit_alone(self) -> None:
        script = """
const { default: register } = await import(process.argv[1]);
const tools = {};
register({ registerTool(definition) { tools[definition.name] = definition; }, on() {} });
console.log(JSON.stringify(Object.keys(tools).filter((name) => name.startsWith("cps_")).sort()));
"""
        environment = os.environ | {
            "CONTEXTSWARM_CPS_ACTIVITY_FEEDBACK_ENABLED": "1",
            "CONTEXTSWARM_CPS_ROUTE_CLAIM_REQUIRED": "0",
            "CONTEXTSWARM_CPS_ROUTE_CLAIMS_ENABLED": "0",
        }
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "--eval",
                script,
                str(ROOT / "contextswarm_mini/pi_solver_tools.mjs"),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        names = set(json.loads(result.stdout))
        self.assertTrue({"cps_active_routes", "cps_claim_route"} <= names)


if __name__ == "__main__":
    unittest.main()
