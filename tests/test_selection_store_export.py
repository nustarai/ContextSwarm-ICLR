from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from contextswarm_mini.selection_store import (
    EXPORT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SelectionStore,
)


class SelectionStoreExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SelectionStore(self.root / "selection.sqlite3")
        self.config_a = self.store.register_selector_config(
            selector_name="recency", config={"version": "v1", "limit": 2}
        )
        self.config_b = self.store.register_selector_config(
            selector_name="bm25", config={"version": "v1", "k1": 1.2}
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _search(
        self,
        request_key: str,
        *,
        actor_id: str = "worker-a",
        trace_id: str = "trace-a",
        comparison_identity: str = "a" * 64,
    ) -> dict:
        return self.store.record_search(
            request_key=request_key,
            task_id="task",
            actor_id=actor_id,
            selector_config_id=self.config_a["selector_config_id"],
            query={"text": request_key},
            comparison_identity=comparison_identity,
            snapshot_identity={"snapshot": request_key},
            pool_identity={"pool": request_key},
            rankings=[
                {
                    "trace_id": trace_id,
                    "rank": 1,
                    "selected": True,
                    "component_scores": {"recency": 1.0},
                    "payload": {"drop_reason": ""},
                },
                {
                    "trace_id": f"{trace_id}-unselected",
                    "rank": 2,
                    "selected": False,
                },
            ],
        )

    def _populate_complete_chain(self) -> tuple[dict, dict]:
        first = self._search("search-a")
        second = self.store.record_search(
            request_key="search-b",
            task_id="task",
            actor_id="worker-b",
            selector_config_id=self.config_b["selector_config_id"],
            query={"text": "beta"},
            comparison_identity="b" * 64,
            snapshot_identity="snapshot-b",
            pool_identity="pool-b",
            rankings=[{"trace_id": "trace-b", "rank": 1, "selected": True}],
        )
        item_id = first["items"][0]["exposure_item_id"]
        common = {
            "exposure_item_id": item_id,
            "actor_id": "worker-a",
            "trace_id": "trace-a",
            "origin": "worker",
        }
        self.store.record_feedback(
            request_key="feedback-progress",
            feedback_kind="route_attempted",
            terminal=False,
            payload={"value": 0},
            **common,
        )
        self.store.record_feedback(
            request_key="feedback-winner",
            feedback_kind="useful",
            payload={"value": 1},
            **common,
        )
        self.store.record_feedback(
            request_key="feedback-conflict",
            feedback_kind="stale",
            payload={"value": -1},
            **common,
        )
        self.store.record_verifier_evidence(
            request_key="evidence-a",
            trace_id="trace-a",
            verifier_id="judge",
            status="verified",
            evidence={"receipt": "receipt-a"},
            task_id="task",
        )
        self.store.record_maintenance_event(
            request_key="maintenance-a",
            trace_id="trace-a",
            actor_id="maintainer",
            maintenance_kind="reviewed",
            payload={"reason": "audit"},
        )
        self.store.record_relation(
            request_key="relation-a",
            source_trace_id="trace-a",
            target_trace_id="trace-b",
            relation_kind="supports",
            actor_id="worker-a",
            payload={"strength": 1},
        )
        return first, second

    @staticmethod
    def _rows(path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_summary_reports_complete_counts_and_public_identities(self) -> None:
        self._populate_complete_chain()
        summary = self.store.summary()

        self.assertEqual(summary["schema_version"], SCHEMA_VERSION)
        self.assertEqual(summary["db"], "selection.sqlite3")
        self.assertEqual(
            summary["selector_config_ids"],
            sorted(
                [
                    self.config_a["selector_config_id"],
                    self.config_b["selector_config_id"],
                ]
            ),
        )
        self.assertEqual(summary["comparison_sha256s"], ["a" * 64, "b" * 64])
        self.assertEqual(
            summary["comparison_contract_ids"], summary["comparison_sha256s"]
        )
        self.assertEqual(
            summary["counts"],
            {
                "selector_configs": 2,
                "search_events": 2,
                "search_rankings": 3,
                "exposures": 2,
                "exposure_items": 2,
                "feedback_events": 3,
                "verifier_evidence": 1,
                "maintenance_events": 1,
                "trace_relations": 1,
                "terminal_feedback_events": 2,
                "nonterminal_feedback_events": 1,
                "effective_feedback_events": 1,
                "conflicting_terminal_feedback_events": 1,
            },
        )

        # Reads and exports must not weaken request-key idempotency or mutate
        # any event count.
        self._search("search-a")
        self.assertEqual(self.store.summary(), summary)

    def test_export_is_typed_decoded_deterministic_and_reconciles_to_summary(self) -> None:
        first, _second = self._populate_complete_chain()
        destination = self.root / "artifacts" / "selection_events.jsonl"
        result = self.store.export_jsonl(destination)
        rows = self._rows(destination)

        self.assertEqual(result["schema"], EXPORT_SCHEMA_VERSION)
        self.assertEqual(result["path"], str(destination))
        self.assertEqual(result["record_count"], len(rows))
        self.assertEqual(
            result["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest()
        )
        self.assertTrue(
            all(set(row) == {"schema", "record_type", "record"} for row in rows)
        )
        self.assertTrue(all(row["schema"] == EXPORT_SCHEMA_VERSION for row in rows))

        expected_order = [
            "selector_config",
            "search_event",
            "search_ranking",
            "exposure",
            "exposure_item",
            "feedback_event",
            "verifier_evidence",
            "maintenance_event",
            "trace_relation",
        ]
        positions = {name: index for index, name in enumerate(expected_order)}
        self.assertEqual(
            [positions[row["record_type"]] for row in rows],
            sorted(positions[row["record_type"]] for row in rows),
        )
        self.assertEqual(
            result["record_type_counts"],
            {
                "selector_config": 2,
                "search_event": 2,
                "search_ranking": 3,
                "exposure": 2,
                "exposure_item": 2,
                "feedback_event": 3,
                "verifier_evidence": 1,
                "maintenance_event": 1,
                "trace_relation": 1,
            },
        )
        self.assertEqual(
            result["record_count"],
            sum(result["record_type_counts"].values()),
        )

        search = next(
            row["record"]
            for row in rows
            if row["record_type"] == "search_event"
            and row["record"]["request_key"] == "search-a"
        )
        ranking = next(
            row["record"]
            for row in rows
            if row["record_type"] == "search_ranking"
            and row["record"]["selected"]
            and row["record"]["trace_id"] == "trace-a"
        )
        feedback = next(
            row["record"]
            for row in rows
            if row["record_type"] == "feedback_event"
            and row["record"]["request_key"] == "feedback-winner"
        )
        self.assertEqual(search["query"], {"text": "search-a"})
        self.assertNotIn("query_json", search)
        self.assertEqual(ranking["component_scores"], {"recency": 1.0})
        self.assertIs(ranking["selected"], True)
        self.assertEqual(feedback["payload"], {"value": 1})
        self.assertIs(feedback["terminal"], True)
        self.assertIs(feedback["effective"], True)
        self.assertEqual(
            next(
                row["record"]["exposure_item_id"]
                for row in rows
                if row["record_type"] == "exposure_item"
                and row["record"]["trace_id"] == "trace-a"
            ),
            first["items"][0]["exposure_item_id"],
        )

        second_destination = self.root / "selection_events_again.jsonl"
        repeated = self.store.export_jsonl(second_destination)
        self.assertEqual(destination.read_bytes(), second_destination.read_bytes())
        self.assertEqual(result["sha256"], repeated["sha256"])
        self.assertEqual(result["summary"], repeated["summary"])
        self.assertEqual(self.store.summary(), result["summary"])

    def test_export_uses_one_wal_snapshot_while_a_writer_commits(self) -> None:
        self._search("before", trace_id="trace-before")
        destination = self.root / "concurrent.jsonl"
        snapshot_ready = threading.Event()
        writer_done = threading.Event()
        original = SelectionStore._summary_from_db

        def pause_after_snapshot(db, *, db_name):
            result = original(db, db_name=db_name)
            snapshot_ready.set()
            if not writer_done.wait(10):
                raise TimeoutError("concurrent writer did not finish")
            return result

        with mock.patch.object(
            SelectionStore, "_summary_from_db", side_effect=pause_after_snapshot
        ):
            with ThreadPoolExecutor(max_workers=1) as pool:
                exported = pool.submit(self.store.export_jsonl, destination)
                self.assertTrue(snapshot_ready.wait(10))
                self._search("during", trace_id="trace-during")
                writer_done.set()
                result = exported.result(timeout=10)

        rows = self._rows(destination)
        exported_searches = [
            row for row in rows if row["record_type"] == "search_event"
        ]
        self.assertEqual(len(exported_searches), 1)
        self.assertEqual(result["summary"]["counts"]["search_events"], 1)
        self.assertEqual(result["record_type_counts"]["search_event"], 1)
        self.assertEqual(self.store.summary()["counts"]["search_events"], 2)

    def test_export_failure_preserves_destination_and_never_replaces_database(self) -> None:
        self._search("search")
        destination = self.root / "selection.jsonl"
        destination.write_text("existing\n", encoding="utf-8")
        with mock.patch(
            "contextswarm_mini.selection_store._decode_row",
            side_effect=RuntimeError("decode failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "decode failed"):
                self.store.export_jsonl(destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), "existing\n")
        self.assertEqual(list(self.root.glob(".selection.jsonl.*.tmp")), [])

        with self.assertRaisesRegex(ValueError, "cannot replace the SQLite store"):
            self.store.export_jsonl(self.store.path)
        self.assertEqual(self.store.summary()["counts"]["search_events"], 1)


if __name__ == "__main__":
    unittest.main()
