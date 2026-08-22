from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from contextswarm_mini.allocation_projection import (
    TraceAllocationProjectionAdapter,
    TraceProjectionLimits,
    TraceProjectionRecordBatch,
    build_synthetic_trace_projection,
)


def _row(sequence: int, kind: str, **fields: object) -> dict[str, object]:
    return {
        "sequence": sequence,
        "record_id": fields.pop("record_id", f"event-{sequence}"),
        "task_id": fields.pop("task_id", "a"),
        "kind": kind,
        **fields,
    }


class _Source:
    def __init__(self, rows: list[dict[str, object]], watermark: int):
        self.rows = rows
        self.watermark = watermark
        self.calls: list[tuple[tuple[str, ...], int, int]] = []

    def read_allocation_projection_records(
        self, task_ids: tuple[str, ...], *, after_watermark: int, limit: int
    ) -> TraceProjectionRecordBatch:
        self.calls.append((tuple(task_ids), after_watermark, limit))
        return TraceProjectionRecordBatch(tuple(self.rows), self.watermark)


class TraceAllocationProjectionTests(unittest.TestCase):
    def test_zero_projection_is_immutable_and_has_exact_core_shape(self) -> None:
        batch = build_synthetic_trace_projection(["a", "b"], watermark=7)
        self.assertEqual(batch.watermark, 7)
        self.assertTrue(batch.for_task("a").is_zero)
        self.assertEqual(
            batch.as_core_mapping()["a"],
            {
                "actionability": 0.0,
                "evidence_association": 0.0,
                "positive_feedback": 0.0,
                "negative_feedback": 0.0,
                "drag": 0.0,
            },
        )
        with self.assertRaises(FrozenInstanceError):
            batch.for_task("a").drag = 1.0  # type: ignore[misc]

    def test_distinct_lineages_associations_and_feedback_are_normalized(self) -> None:
        adapter = TraceAllocationProjectionAdapter(
            TraceProjectionLimits(
                actionability_saturation=2,
                association_saturation=2,
                drag_saturation=4,
            )
        )
        rows = [
            _row(1, "frontier", lineage_id="l1"),
            _row(2, "frontier", lineage_id="l1"),
            _row(3, "frontier", lineage_id="l2"),
            _row(4, "evidence_link", lineage_id="l1", evidence_id="p1"),
            _row(5, "evidence_link", lineage_id="l1", evidence_id="p1"),
            _row(6, "evidence_link", lineage_id="l2", evidence_id="p1"),
            _row(7, "worker_exposure", worker_id="w1", evidence_id="p1", exposure_id="event-7"),
            _row(8, "worker_exposure", worker_id="w2", evidence_id="p1", exposure_id="event-8"),
            _row(9, "worker_exposure", worker_id="w3", evidence_id="p1", exposure_id="event-9"),
            _row(10, "feedback_helpful", worker_id="w1", evidence_id="p1", exposure_id="event-7", effective=True, terminal=True),
            _row(11, "worker_feedback_negative", worker_id="w2", evidence_id="p1", exposure_id="event-8", effective=True, terminal=True),
            _row(12, "duplicate", lineage_id="l2"),
            _row(13, "refutation", lineage_id="l3"),
            _row(14, "stale", lineage_id="l4"),
            _row(15, "lineage_stagnation", lineage_id="l5"),
        ]
        projection = adapter.project_records(["a"], rows).for_task("a")
        self.assertEqual(projection.actionability, 1.0)
        self.assertEqual(projection.frontier_count, 2)
        self.assertEqual(projection.evidence_association, 1.0)
        self.assertEqual(projection.association_count, 2)
        self.assertEqual(projection.feedback_exposure_count, 3)
        self.assertAlmostEqual(projection.positive_feedback, 1 / 3)
        self.assertAlmostEqual(projection.negative_feedback, 1 / 3)
        self.assertEqual(projection.drag, 1.0)

    def test_replayed_records_and_raw_verifier_receipts_do_not_double_count(self) -> None:
        adapter = TraceAllocationProjectionAdapter()
        helpful = _row(
            2,
            "feedback_positive",
            record_id="feedback-1",
            worker_id="w1",
            evidence_id="piece-1",
            exposure_id="exposure-1",
            effective=True,
            terminal=True,
            source_outcome_id="trace-outcome-1",
        )
        rows = [
            _row(1, "worker_exposure", record_id="exposure-1", worker_id="w1", evidence_id="piece-1", exposure_id="exposure-1"),
            helpful,
            dict(helpful),
            _row(3, "validation_result", record_id="receipt-1", source="runner"),
            _row(4, "judge_receipt", record_id="receipt-2", source="judge"),
            # Even a feedback-looking event from a verifier is task state, not
            # worker feedback in the trace increment.
            _row(
                5,
                "feedback_helpful",
                record_id="receipt-3",
                source="verifier",
                worker_id="checker",
            ),
        ]
        batch = adapter.project_records(["a"], rows)
        projection = batch.for_task("a")
        self.assertEqual(projection.feedback_exposure_count, 1)
        self.assertEqual(projection.positive_feedback_count, 1)
        self.assertEqual(projection.positive_feedback, 1.0)
        self.assertEqual(projection.negative_feedback, 0.0)
        self.assertEqual(projection.source_outcome_ids, ("trace-outcome-1",))
        self.assertEqual(batch.records_used, 2)

    def test_checker_outcome_ids_exclude_links_and_feedback_from_trace(self) -> None:
        rows = [
            _row(1, "evidence_link", lineage_id="l1", evidence_id="checker-1"),
            _row(2, "worker_exposure", worker_id="w1", evidence_id="p1", exposure_id="exp-1"),
            _row(3, "feedback_helpful", worker_id="w1", evidence_id="p1", exposure_id="exp-1", effective=True, terminal=True, source_outcome_id="checker-1"),
        ]
        projection = TraceAllocationProjectionAdapter().project_records(
            ["a"], rows, ordinary_outcome_ids=["checker-1"]
        ).for_task("a")
        self.assertEqual(projection.evidence_association, 0.0)
        self.assertEqual(projection.positive_feedback, 0.0)
        self.assertEqual(projection.source_outcome_ids, ())

    def test_watermark_is_exclusive_stable_and_does_not_skip_truncated_page(self) -> None:
        limits = TraceProjectionLimits(max_records=2)
        adapter = TraceAllocationProjectionAdapter(limits)
        source = _Source(
            [
                _row(9, "frontier", lineage_id="l9"),
                _row(6, "frontier", lineage_id="l6"),
                _row(8, "frontier", lineage_id="l8"),
                _row(5, "frontier", lineage_id="old"),
            ],
            watermark=9,
        )
        first = adapter.project(source, ["a"], after_watermark=5)
        self.assertEqual(first.watermark, 8)
        self.assertTrue(first.truncated)
        self.assertEqual(first.for_task("a").frontier_count, 2)
        self.assertEqual(source.calls, [(('a',), 5, 2)])

        second = adapter.project_records(
            ["a"], source.rows, after_watermark=first.watermark, source_watermark=9
        )
        self.assertEqual(second.watermark, 9)
        self.assertEqual(second.for_task("a").frontier_count, 1)

    def test_bounds_cap_values_records_and_per_task_input(self) -> None:
        adapter = TraceAllocationProjectionAdapter(
            TraceProjectionLimits(
                max_tasks=1,
                max_records=10,
                max_records_per_task=2,
                actionability_saturation=1,
                association_saturation=1,
                drag_saturation=1,
            )
        )
        with self.assertRaises(ValueError):
            adapter.project_records(["a", "b"], [])
        batch = adapter.project_records(
            ["a"],
            [
                _row(1, "frontier", lineage_id="l1"),
                _row(2, "frontier", lineage_id="l2"),
                _row(3, "frontier", lineage_id="l3"),
            ],
        )
        self.assertTrue(batch.truncated)
        self.assertEqual(batch.for_task("a").actionability, 1.0)
        self.assertEqual(batch.for_task("a").frontier_count, 2)

    def test_missing_feedback_and_incomplete_associations_fail_closed_to_zero(self) -> None:
        projection = TraceAllocationProjectionAdapter().project_records(
            ["a"],
            [
                _row(1, "frontier"),
                _row(2, "evidence_link", lineage_id="l1"),
                _row(3, "unknown_feedback", worker_id="w1"),
            ],
        ).for_task("a")
        self.assertTrue(projection.is_zero)

    def test_legacy_mapping_page_without_sequence_uses_read_order(self) -> None:
        projection = TraceAllocationProjectionAdapter().project_records(
            ["a"], [{"task_id": "a", "kind": "frontier", "lineage_id": "l1"}]
        ).for_task("a")
        self.assertEqual(projection.frontier_count, 1)
        self.assertEqual(projection.watermark, 1)


if __name__ == "__main__":
    unittest.main()
