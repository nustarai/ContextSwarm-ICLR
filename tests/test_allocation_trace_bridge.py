from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from contextswarm_mini.allocation_projection import (
    TraceProjectionLimits,
    TraceProjectionRecordBatch,
)
from contextswarm_mini.allocation_trace_bridge import (
    SelectionStoreTraceSource,
    TraceProjectionBridge,
    feedback_values_from_config,
    policy_reads_trace,
)


FEEDBACK_VALUES = {
    "useful": 1.0,
    "not_useful": -1.0,
    "misleading": -2.0,
    "stale": -1.25,
    "unsafe": -3.0,
    "duplicate": -0.5,
    "diagnostic_useful": 0.75,
    "needs_refinement": -0.25,
    "not_used": 0.0,
    "route_attempted": 0.0,
    "route_improving": 2.0,
}


class _Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def _selection_db(path: Path, *, private_marker: str) -> _Store:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE search_events (
            search_event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            query_json TEXT NOT NULL
        );
        CREATE TABLE exposures (
            exposure_id TEXT PRIMARY KEY,
            search_event_id TEXT NOT NULL,
            actor_id TEXT NOT NULL
        );
        CREATE TABLE exposure_items (
            exposure_item_id TEXT PRIMARY KEY,
            exposure_id TEXT NOT NULL,
            trace_id TEXT NOT NULL
        );
        CREATE TABLE feedback_events (
            feedback_event_id TEXT PRIMARY KEY,
            exposure_item_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            event_class TEXT NOT NULL,
            feedback_kind TEXT NOT NULL,
            terminal INTEGER NOT NULL,
            effective INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        """
    )
    db.execute(
        "INSERT INTO search_events VALUES(?, ?, ?)",
        ("search-a", "task-a", json.dumps({"raw_query": private_marker})),
    )
    db.execute(
        "INSERT INTO exposures VALUES(?, ?, ?)",
        ("exposure-parent", "search-a", "worker-a"),
    )
    db.executemany(
        "INSERT INTO exposure_items VALUES(?, ?, ?)",
        (
            ("item-positive", "exposure-parent", "trace-positive"),
            ("item-negative", "exposure-parent", "trace-negative"),
        ),
    )
    db.executemany(
        "INSERT INTO feedback_events VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                "feedback-positive",
                "item-positive",
                "trace-positive",
                "worker-a",
                "worker_interaction",
                "useful",
                1,
                1,
                json.dumps({"note": private_marker, "value": -999}),
            ),
            (
                "feedback-negative",
                "item-negative",
                "trace-negative",
                "worker-a",
                "worker_interaction",
                "unsafe",
                1,
                1,
                json.dumps({"note": private_marker, "value": 999}),
            ),
            # Conflict/nonterminal/non-worker rows are durable audit records,
            # but none is effective allocation feedback.
            (
                "feedback-conflict",
                "item-positive",
                "trace-positive",
                "worker-a",
                "worker_interaction",
                "unsafe",
                1,
                0,
                json.dumps({"note": private_marker}),
            ),
            (
                "feedback-nonterminal",
                "item-negative",
                "trace-negative",
                "worker-a",
                "worker_interaction",
                "useful",
                0,
                0,
                json.dumps({"note": private_marker}),
            ),
            (
                "feedback-verifier",
                "item-negative",
                "trace-negative",
                "checker",
                "verifier",
                "useful",
                1,
                1,
                json.dumps({"note": private_marker}),
            ),
        ),
    )
    db.commit()
    db.close()
    return _Store(path)


class _ProtocolStore:
    def __init__(self, rows, *, watermark: int = 7):
        self.rows = tuple(rows)
        self.watermark = watermark
        self.calls = []

    def read_allocation_projection_records(
        self, task_ids, *, after_watermark: int, limit: int
    ) -> TraceProjectionRecordBatch:
        self.calls.append((tuple(task_ids), after_watermark, limit))
        return TraceProjectionRecordBatch(self.rows, self.watermark)


class AllocationTraceBridgeTests(unittest.TestCase):
    def test_only_trace_state_and_llm_scheduler_may_read_trace(self) -> None:
        self.assertFalse(policy_reads_trace("uniform_refill"))
        self.assertFalse(policy_reads_trace("task_state"))
        self.assertTrue(policy_reads_trace("trace_state"))
        self.assertTrue(policy_reads_trace("llm_scheduler"))

    def test_selection_store_full_projection_uses_item_exposures_and_frozen_polarity(self) -> None:
        private = "PRIVATE query /tmp/secret transcript"
        with tempfile.TemporaryDirectory() as temporary:
            store = _selection_db(Path(temporary) / "selection.sqlite3", private_marker=private)
            view = TraceProjectionBridge().read(
                ["task-a", "task-b"],
                store=store,
                feedback_values=FEEDBACK_VALUES,
            )

        self.assertEqual(view.source, "selection_store_sqlite_v1")
        self.assertTrue(view.complete)
        self.assertEqual(view.fallback_reason, "")
        projection = view.for_task("task-a")
        # Both selected traces share a parent exposure, but each delivered item
        # is one exposure in the feedback denominator.
        self.assertEqual(projection.feedback_exposure_count, 2)
        self.assertEqual(projection.positive_feedback_count, 1)
        self.assertEqual(projection.negative_feedback_count, 1)
        self.assertEqual(projection.positive_feedback, 0.5)
        self.assertEqual(projection.negative_feedback, 0.5)
        self.assertTrue(view.for_task("task-b").is_zero)
        self.assertEqual(
            view.references_for_task("task-a"),
            ("trace-negative", "trace-positive"),
        )
        # Raw selector inputs/payloads and even the DB path are absent from the
        # bounded object that can reach an LLM scheduler prompt.
        rendered = repr(view)
        self.assertNotIn(private, rendered)
        self.assertNotIn(temporary, rendered)

    def test_relational_source_returns_only_projection_whitelist(self) -> None:
        marker = "DO-NOT-RETURN-ME"
        with tempfile.TemporaryDirectory() as temporary:
            store = _selection_db(Path(temporary) / "selection.sqlite3", private_marker=marker)
            records, watermark = SelectionStoreTraceSource(
                store, feedback_values=FEEDBACK_VALUES
            ).read_complete_records(["task-a"])
        allowed = {
            "sequence",
            "record_id",
            "task_id",
            "kind",
            "evidence_id",
            "worker_id",
            "exposure_id",
            "source",
            "effective",
            "terminal",
        }
        self.assertTrue(all(set(record) <= allowed for record in records))
        self.assertNotIn(marker, json.dumps(records, sort_keys=True))
        self.assertRegex(watermark, r"^[0-9a-f]{64}$")

    def test_missing_mapping_overflow_and_private_errors_fail_closed(self) -> None:
        private = "operator-private-selection-path"
        with tempfile.TemporaryDirectory(prefix=private) as temporary:
            path = Path(temporary) / "selection.sqlite3"
            store = _selection_db(path, private_marker=private)
            missing = TraceProjectionBridge().read(["task-a"], store=store)
            overflow = TraceProjectionBridge(
                limits=TraceProjectionLimits(max_records=1)
            ).read(["task-a"], store=store, feedback_values=FEEDBACK_VALUES)
        for view in (missing, overflow):
            self.assertEqual(view.source, "zero")
            self.assertTrue(view.for_task("task-a").is_zero)
            self.assertNotIn(private, view.fallback_reason)
            self.assertNotIn(private, view.watermark)

    def test_store_native_protocol_precedes_sqlite_and_rejects_incomplete_page(self) -> None:
        store = _ProtocolStore(
            [
                {
                    "sequence": 1,
                    "record_id": "frontier-1",
                    "task_id": "task-a",
                    "kind": "frontier",
                    "lineage_id": "lineage-1",
                }
            ]
        )
        bridge = TraceProjectionBridge()
        view = bridge.read(["task-a"], store=store)
        self.assertEqual(view.source, "selection_store_protocol")
        self.assertEqual(view.for_task("task-a").frontier_count, 1)
        self.assertEqual(store.calls, [(('task-a',), 0, 4096)])

        incomplete = _ProtocolStore(
            [
                {"sequence": 1, "record_id": "1", "task_id": "task-a", "kind": "frontier", "lineage_id": "l1"},
                {"sequence": 2, "record_id": "2", "task_id": "task-a", "kind": "frontier", "lineage_id": "l2"},
            ],
            watermark=2,
        )
        zero = TraceProjectionBridge(
            limits=TraceProjectionLimits(max_records=1)
        ).read(["task-a"], store=incomplete)
        self.assertEqual(zero.source, "zero")
        self.assertTrue(zero.for_task("task-a").is_zero)

    def test_zero_and_synthetic_fallbacks_are_deterministic(self) -> None:
        bridge = TraceProjectionBridge()
        first = bridge.read(["a", "b"])
        second = bridge.read(["a", "b"])
        self.assertEqual(first.watermark, second.watermark)
        self.assertTrue(first.for_task("a").is_zero)

        synthetic = TraceProjectionBridge(
            synthetic_features={"a": {"actionability": 0.75}}
        ).read(["a", "b"])
        self.assertEqual(synthetic.source, "synthetic")
        self.assertEqual(synthetic.for_task("a").actionability, 0.75)
        self.assertTrue(synthetic.for_task("b").is_zero)

    def test_config_mapping_is_duck_typed_for_pre_and_post_issue38_config(self) -> None:
        class Selection:
            policy_params = {"feedback_values": FEEDBACK_VALUES}

        class TypedConfig:
            selection = Selection()

        class LegacyConfig:
            extra = {
                "raw": {
                    "selection": {
                        "policy_params": {"feedback_values": FEEDBACK_VALUES}
                    }
                }
            }

        self.assertEqual(feedback_values_from_config(TypedConfig()), FEEDBACK_VALUES)
        self.assertEqual(feedback_values_from_config(LegacyConfig()), FEEDBACK_VALUES)


if __name__ == "__main__":
    unittest.main()
