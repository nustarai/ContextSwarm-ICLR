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
    SelectionRuntimeTraceSource,
    TraceProjectionSnapshotPage,
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
    def __init__(self, rows, *, watermark: int = 1, complete: bool = True):
        self.rows = tuple(rows)
        self.watermark = watermark
        self.complete = complete
        self.calls = []

    def read_allocation_projection_records(
        self, task_ids, *, after_watermark: int, limit: int
    ) -> TraceProjectionRecordBatch:
        self.calls.append((tuple(task_ids), after_watermark, limit))
        return TraceProjectionRecordBatch(
            self.rows, self.watermark, complete=self.complete
        )


class _SnapshotStore:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def read_allocation_projection_snapshot(
        self, task_ids, *, as_of_watermark, cursor, limit
    ):
        self.calls.append((tuple(task_ids), as_of_watermark, cursor, limit))
        if not self.pages:
            raise AssertionError("unexpected snapshot page")
        page = self.pages.pop(0)
        return page


class _ContradictorySnapshotStore:
    def __init__(self):
        self.calls = []

    def read_allocation_projection_snapshot(
        self, task_ids, *, as_of_watermark, cursor, limit
    ):
        self.calls.append((tuple(task_ids), as_of_watermark, cursor, limit))
        if not cursor:
            return TraceProjectionSnapshotPage(
                records=(
                    {
                        "sequence": 1,
                        "record_id": "same-id",
                        "task_id": "task-a",
                        "kind": "frontier",
                        "lineage_id": "lineage-a",
                    },
                ),
                trace_watermark="W",
                next_cursor="c1",
                complete=False,
            )
        return TraceProjectionSnapshotPage(
            records=(
                {
                    "sequence": 1,
                    "record_id": "same-id",
                    "task_id": "task-a",
                    "kind": "frontier",
                    "lineage_id": "lineage-b",
                },
            ),
            trace_watermark="W",
            complete=True,
        )


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
        self.assertAlmostEqual(projection.positive_feedback, 0.5)
        self.assertAlmostEqual(projection.negative_feedback, 0.5)
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

        # Even when the returned page happens to end at the reported head, an
        # explicit incomplete marker must fail closed rather than becoming a
        # current-state snapshot.
        marked_incomplete = _ProtocolStore(
            [
                {
                    "sequence": 1,
                    "record_id": "partial-1",
                    "task_id": "task-a",
                    "kind": "frontier",
                    "lineage_id": "partial-lineage",
                }
            ],
            watermark=1,
            complete=False,
        )
        marked_zero = TraceProjectionBridge().read(
            ["task-a"], store=marked_incomplete
        )
        self.assertEqual(marked_zero.source, "zero")
        self.assertTrue(marked_zero.for_task("task-a").is_zero)

    def test_pinned_snapshot_pages_are_materialized_before_projection(self) -> None:
        source = _SnapshotStore(
            [
                TraceProjectionSnapshotPage(
                    records=(
                        {
                            "sequence": 1,
                            "record_id": "frontier-1",
                            "task_id": "task-a",
                            "kind": "frontier",
                            "lineage_id": "l1",
                            "evidence_id": "piece-1",
                        },
                    ),
                    trace_watermark="W",
                    next_cursor="c1",
                    complete=False,
                ),
                TraceProjectionSnapshotPage(
                    records=(
                        {
                            "sequence": 2,
                            "record_id": "frontier-2",
                            "task_id": "task-a",
                            "kind": "frontier",
                            "lineage_id": "l2",
                            "evidence_id": "piece-2",
                        },
                    ),
                    trace_watermark="W",
                    complete=True,
                ),
            ]
        )
        view = TraceProjectionBridge(
            limits=TraceProjectionLimits(max_records=2)
        ).read(["task-a"], store=source)
        self.assertEqual(view.source, "selection_store_snapshot")
        self.assertEqual(view.for_task("task-a").frontier_count, 2)
        self.assertEqual(view.watermark.startswith("snapshot:"), True)
        self.assertEqual(
            source.calls,
            [
                (("task-a",), None, "", 2),
                (("task-a",), "W", "c1", 2),
            ],
        )

    def test_full_projection_receives_pinned_snapshot_metadata(self) -> None:
        source = _SnapshotStore(
            [
                TraceProjectionSnapshotPage(
                    records=(
                        {
                            "sequence": 1,
                            "record_id": "trace-1",
                            "task_id": "task-a",
                            "kind": "piece_snapshot",
                            "trace_id": "trace-1",
                            "lineage_id": "lineage-1",
                            "active": True,
                            "event_time": 10.0,
                        },
                    ),
                    trace_watermark="opaque-W",
                    source_watermark=1,
                    snapshot_id="source-snapshot-1",
                    reference_time=12.5,
                )
            ]
        )
        bridge = TraceProjectionBridge()
        calls = []
        original = bridge.adapter.project_full_records

        def recording_project(*args, **kwargs):
            calls.append(dict(kwargs))
            return original(*args, **kwargs)

        bridge.adapter.project_full_records = recording_project  # type: ignore[method-assign]
        view = bridge.read(["task-a"], store=source)
        self.assertEqual(view.source, "selection_store_snapshot")
        self.assertEqual(view.batch.snapshot_id, "source-snapshot-1")
        self.assertEqual(calls[0]["source_watermark"], 1)
        self.assertEqual(calls[0]["snapshot_id"], "source-snapshot-1")
        self.assertEqual(calls[0]["reference_time"], 12.5)

    def test_full_state_fields_change_identity_without_hashing_payload(self) -> None:
        base = {
            "sequence": 1,
            "record_id": "trace-1",
            "task_id": "task-a",
            "kind": "piece_snapshot",
            "trace_id": "trace-1",
            "lineage_id": "lineage-1",
            "active": True,
            "event_time": 10.0,
            "payload": "PRIVATE-CPS-BODY",
        }
        first = _SnapshotStore(
            [TraceProjectionSnapshotPage(records=(base,), trace_watermark="W")]
        )
        changed = dict(base, active=False, lifecycle="stale")
        second = _SnapshotStore(
            [TraceProjectionSnapshotPage(records=(changed,), trace_watermark="W")]
        )
        first_view = TraceProjectionBridge().read(["task-a"], store=first)
        second_view = TraceProjectionBridge().read(["task-a"], store=second)
        self.assertNotEqual(first_view.watermark, second_view.watermark)
        self.assertNotIn("PRIVATE-CPS-BODY", repr(first_view))
        self.assertNotIn("PRIVATE-CPS-BODY", first_view.watermark)

    def test_snapshot_metadata_drift_and_nonfinite_reference_time_fail_closed(self) -> None:
        for pages in (
            [
                TraceProjectionSnapshotPage(
                    records=(), trace_watermark="W", next_cursor="c1", complete=False,
                    snapshot_id="S1", reference_time=1.0,
                ),
                TraceProjectionSnapshotPage(
                    records=(), trace_watermark="W", snapshot_id="S2", reference_time=1.0,
                ),
            ],
            [
                TraceProjectionSnapshotPage(
                    records=(), trace_watermark="W", next_cursor="c1", complete=False,
                    snapshot_id="S", reference_time=1.0,
                ),
                TraceProjectionSnapshotPage(
                    records=(), trace_watermark="W", snapshot_id="S", reference_time=2.0,
                ),
            ],
        ):
            view = TraceProjectionBridge().read(["task-a"], store=_SnapshotStore(pages))
            self.assertEqual(view.source, "zero")
        with self.assertRaises(ValueError):
            TraceProjectionSnapshotPage(
                records=(), trace_watermark="W", reference_time=float("nan")
            )

    def test_ordinary_outcome_is_excluded_from_projection_and_references(self) -> None:
        source = _SnapshotStore(
            [
                TraceProjectionSnapshotPage(
                    records=(
                        {
                            "sequence": 1,
                            "record_id": "trace-row",
                            "task_id": "task-a",
                            "kind": "piece_snapshot",
                            "trace_id": "trace-a",
                            "lineage_id": "lineage-a",
                            "active": True,
                        },
                        {
                            "sequence": 2,
                            "record_id": "ordinary-1",
                            "task_id": "task-a",
                            "kind": "evidence_link",
                            "trace_id": "trace-a",
                            "lineage_id": "lineage-a",
                            "evidence_id": "ordinary-1",
                            "source_outcome_id": "ordinary-1",
                        },
                    ),
                    trace_watermark="W",
                )
            ]
        )
        view = TraceProjectionBridge().read(
            ["task-a"], store=source, ordinary_outcome_ids=("ordinary-1",)
        )
        projection = view.for_task("task-a")
        self.assertEqual(projection.evidence_association, 0.0)
        self.assertEqual(projection.source_outcome_ids, ())
        self.assertEqual(view.references_for_task("task-a"), ("trace-a",))

    def test_ordinary_record_and_source_outcome_aliases_do_not_become_references(self) -> None:
        source = _SnapshotStore(
            [
                TraceProjectionSnapshotPage(
                    records=(
                        {
                            "sequence": 1,
                            "record_id": "ordinary-record",
                            "task_id": "task-a",
                            "kind": "piece_snapshot",
                            "trace_id": "trace-from-record",
                            "active": True,
                        },
                        {
                            "sequence": 2,
                            "record_id": "row-2",
                            "source_outcome_id": "ordinary-source",
                            "task_id": "task-a",
                            "kind": "piece_snapshot",
                            "trace_id": "trace-from-source-outcome",
                            "active": True,
                        },
                    ),
                    trace_watermark="W",
                )
            ]
        )
        view = TraceProjectionBridge().read(
            ["task-a"],
            store=source,
            ordinary_outcome_ids=("ordinary-record", "ordinary-source"),
        )
        self.assertEqual(view.source, "selection_store_snapshot")
        self.assertEqual(view.references_for_task("task-a"), ())

    def test_snapshot_watermark_drift_cursor_replay_and_source_head_fail_closed(self) -> None:
        drift = _SnapshotStore(
            [
                TraceProjectionSnapshotPage(
                    records=(), trace_watermark="W1", next_cursor="c1", complete=False
                ),
                TraceProjectionSnapshotPage(
                    records=(), trace_watermark="W2", complete=True
                ),
            ]
        )
        replay = _SnapshotStore(
            [
                TraceProjectionSnapshotPage(
                    records=(), trace_watermark="W", next_cursor="c1", complete=False
                ),
                TraceProjectionSnapshotPage(
                    records=(), trace_watermark="W", next_cursor="c1", complete=False
                ),
            ]
        )
        for source in (drift, replay):
            view = TraceProjectionBridge().read(["task-a"], store=source)
            self.assertEqual(view.source, "zero")
            self.assertTrue(view.for_task("task-a").is_zero)

        # Legacy integer watermark 9 with only sequence 1 is an unsafe source
        # head fast-forward and must not become an apparently complete view.
        unsafe_legacy = _ProtocolStore(
            [
                {
                    "sequence": 1,
                    "record_id": "frontier-1",
                    "task_id": "task-a",
                    "kind": "frontier",
                    "lineage_id": "l1",
                }
            ],
            watermark=9,
        )
        view = TraceProjectionBridge().read(["task-a"], store=unsafe_legacy)
        self.assertEqual(view.source, "zero")

    def test_contradictory_replay_inside_pinned_snapshot_fails_closed(self) -> None:
        source = _ContradictorySnapshotStore()
        view = TraceProjectionBridge().read(["task-a"], store=source)
        self.assertEqual(view.source, "zero")
        self.assertTrue(view.for_task("task-a").is_zero)
        self.assertTrue(view.fallback_reason.startswith("projection_unavailable:ValueError"))

    def test_selection_runtime_adapter_is_explicit_and_rejects_cursor_replay(self) -> None:
        class Runtime:
            selection_store = object()
            feedback_values = FEEDBACK_VALUES

        # Bypass SQLite setup to test the adapter's protocol boundary without
        # invoking selector search or CPS state.
        runtime_source = SelectionRuntimeTraceSource.__new__(SelectionRuntimeTraceSource)
        runtime_source._source = type(
            "CompleteSource",
            (),
            {
                "read_complete_records": lambda self, task_ids: (
                    (
                        {
                            "sequence": 1,
                            "record_id": "e1",
                            "task_id": "a",
                            "kind": "frontier",
                            "lineage_id": "l1",
                        },
                    ),
                    "W",
                )
            },
        )()
        page = runtime_source.read_allocation_projection_snapshot(
            ["a"], as_of_watermark=None, cursor="", limit=4
        )
        self.assertTrue(page.complete)
        self.assertEqual(page.trace_watermark, "W")
        with self.assertRaises(ValueError):
            runtime_source.read_allocation_projection_snapshot(
                ["a"], as_of_watermark="W", cursor="c1", limit=4
            )

    def test_bridge_accepts_bound_selection_runtime_without_selector_calls(self) -> None:
        private = "runtime-private-query"
        with tempfile.TemporaryDirectory() as temporary:
            store = _selection_db(
                Path(temporary) / "selection.sqlite3", private_marker=private
            )

            class Runtime:
                selection_store = store
                feedback_values = FEEDBACK_VALUES

            view = TraceProjectionBridge().read(
                ["task-a"],
                selection_runtime=Runtime(),
            )
            self.assertEqual(view.source, "selection_store_snapshot")
            self.assertEqual(view.for_task("task-a").positive_feedback_count, 1)
            self.assertNotIn(private, repr(view))

    def test_runtime_store_and_mapping_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = _selection_db(
                Path(temporary) / "first.sqlite3", private_marker="first"
            )
            second = _selection_db(
                Path(temporary) / "second.sqlite3", private_marker="second"
            )

            class Runtime:
                selection_store = first
                feedback_values = FEEDBACK_VALUES

            bridge = TraceProjectionBridge()
            wrong_store = bridge.read(
                ["task-a"], selection_runtime=Runtime(), store=second
            )
            self.assertEqual(wrong_store.source, "zero")

            wrong_values = dict(FEEDBACK_VALUES)
            wrong_values["useful"] = 0.0
            wrong_mapping = bridge.read(
                ["task-a"], selection_runtime=Runtime(), feedback_values=wrong_values
            )
            self.assertEqual(wrong_mapping.source, "zero")

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
