from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from contextswarm_mini.selection_store import (
    CANONICAL_FEEDBACK_KINDS,
    RequestKeyConflictError,
    SelectionStore,
)


class SelectionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SelectionStore(Path(self.temporary.name) / "selection.sqlite3")
        self.config = self.store.register_selector_config(
            selector_name="NuStigmergy Selector", config={"smoothing": 1.0, "seed": 4}
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _search(self, request_key: str = "search-1") -> dict:
        return self.store.record_search(
            request_key=request_key,
            task_id="task",
            actor_id="worker-a",
            selector_config_id=self.config["selector_config_id"],
            query={"text": "lemma"},
            comparison_identity="fixed-comparison",
            snapshot_identity="fixed-snapshot",
            pool_identity="fixed-pool",
            rankings=[
                {"trace_id": "trace-1", "rank": 1, "selected": True, "component_scores": {"bm25": 2.0}},
                {"trace_id": "trace-2", "rank": 2, "selected": False},
            ],
        )

    def test_ids_and_request_keys_are_idempotent(self) -> None:
        first = self._search()
        retry = self._search()
        self.assertTrue(retry["idempotent"])
        self.assertEqual(first["search_event"]["search_event_id"], retry["search_event"]["search_event_id"])
        self.assertEqual(first["items"][0]["exposure_item_id"], retry["items"][0]["exposure_item_id"])
        self.assertEqual(first["rankings"][0]["search_ranking_id"], retry["rankings"][0]["search_ranking_id"])
        self.assertEqual(first["search_event"]["query"], {"text": "lemma"})
        self.assertEqual(first["rankings"][0]["component_scores"], {"bm25": 2.0})
        self.assertEqual(first["search_event"]["config_sha256"], self.config["config_sha256"])

        db = self.store._connect()
        try:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            db.close()

    def test_actor_and_trace_binding_and_separate_evidence(self) -> None:
        item = self._search()["items"][0]["exposure_item_id"]
        with self.assertRaises(ValueError):
            self.store.record_feedback(
                request_key="wrong-actor", exposure_item_id=item, actor_id="worker-b",
                trace_id="trace-1", feedback_kind="useful", origin="worker",
            )
        with self.assertRaises(ValueError):
            self.store.record_feedback(
                request_key="wrong-trace", exposure_item_id=item, actor_id="worker-a",
                trace_id="trace-2", feedback_kind="useful", origin="worker",
            )
        evidence = self.store.record_verifier_evidence(
            request_key="evidence-1", trace_id="trace-1", verifier_id="judge",
            status="VERIFY_FAIL", evidence={"message": "missing lemma"},
        )
        self.assertFalse(evidence["idempotent"])
        maintenance = self.store.record_maintenance_event(
            request_key="maintenance-1", trace_id="trace-1", actor_id="maintainer",
            maintenance_kind="superseded", payload={"reason": "newer trace"},
        )
        relation = self.store.record_relation(
            request_key="relation-1", source_trace_id="trace-1", target_trace_id="trace-3",
            relation_kind="supports", actor_id="worker-a",
        )
        self.assertFalse(maintenance["idempotent"])
        self.assertFalse(relation["idempotent"])
        self.assertEqual(self.store.effective_feedback(), [])
        chain = self.store.attribution_chain(item)
        self.assertEqual(chain["verifier_evidence"][0]["status"], "VERIFY_FAIL")
        self.assertEqual(chain["maintenance_events"][0]["maintenance_kind"], "superseded")
        self.assertEqual(chain["relations"][0]["relation_kind"], "supports")

    def test_only_one_terminal_worker_feedback_is_effective(self) -> None:
        item = self._search()["items"][0]["exposure_item_id"]

        def write(index: int) -> dict:
            return self.store.record_feedback(
                request_key=f"feedback-{index}", exposure_item_id=item, actor_id="worker-a",
                trace_id="trace-1", feedback_kind="useful" if index % 2 else "not_useful",
                origin="worker", payload={"attempt": index},
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write, range(8)))
        effective = [result for result in results if result["effective"]]
        conflicts = [result for result in results if result["status"] == "ALREADY_FINAL"]
        self.assertEqual(len(effective), 1)
        self.assertEqual(len(conflicts), 7)
        self.assertEqual(len(self.store.effective_feedback()), 1)
        self.assertTrue(all(result["event_class"] == "worker_interaction" for result in results))
        winner_id = effective[0]["feedback_event_id"]
        self.assertTrue(all(result["conflicts_with_feedback_event_id"] == winner_id for result in conflicts))
        with self.assertRaises(RequestKeyConflictError):
            self.store.record_feedback(
                request_key=effective[0]["request_key"], exposure_item_id=item, actor_id="different",
                trace_id="different", feedback_kind="unsafe", origin="different",
            )

    def test_all_canonical_feedback_kinds_are_accepted_and_nonterminal_does_not_finalize(self) -> None:
        search = self.store.record_search(
            request_key="search-all", task_id="task", actor_id="worker-a",
            selector_config_id=self.config["selector_config_id"], query="q",
            comparison_identity="c", snapshot_identity="s", pool_identity="p",
            rankings=[{"trace_id": "trace-all", "rank": 1, "selected": True}],
        )
        item = search["items"][0]["exposure_item_id"]
        for index, kind in enumerate(sorted(CANONICAL_FEEDBACK_KINDS)):
            result = self.store.record_feedback(
                request_key=f"nonterminal-{index}", exposure_item_id=item, actor_id="worker-a",
                trace_id="trace-all", feedback_kind=kind, origin="worker", terminal=False,
            )
            self.assertFalse(result["effective"])
        final = self.store.record_feedback(
            request_key="terminal", exposure_item_id=item, actor_id="worker-a",
            trace_id="trace-all", feedback_kind="useful", origin="worker", terminal=True,
        )
        self.assertTrue(final["effective"])


if __name__ == "__main__":
    unittest.main()
