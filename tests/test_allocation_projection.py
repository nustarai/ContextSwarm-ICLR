from __future__ import annotations

from dataclasses import FrozenInstanceError
import math
import unittest

from contextswarm_mini.allocation_projection import (
    TraceAllocationProjectionBatch,
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

    def test_projection_batch_snapshot_id_does_not_coerce_numbers(self) -> None:
        with self.assertRaises(ValueError):
            TraceProjectionRecordBatch((), 0, snapshot_id=17)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TraceAllocationProjectionBatch((), 0, snapshot_id=False)  # type: ignore[arg-type]

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

    def test_drag_components_are_individually_projected_and_forwarded(self) -> None:
        adapter = TraceAllocationProjectionAdapter(
            TraceProjectionLimits(
                duplicate_saturation=2,
                refutation_saturation=4,
                staleness_saturation=4,
                lineage_stagnation_saturation=4,
            )
        )
        projection = adapter.project_records(
            ["a"],
            [
                _row(1, "duplicate", lineage_id="dup"),
                _row(2, "duplicate", lineage_id="dup-2"),
                _row(3, "refutation", lineage_id="ref"),
                _row(4, "stale", lineage_id="stale"),
                _row(5, "lineage_stagnation", lineage_id="stagnant"),
            ],
        ).for_task("a")
        self.assertEqual(projection.duplication, 1.0)
        self.assertEqual(projection.refutation, 0.25)
        self.assertEqual(projection.staleness, 0.25)
        self.assertEqual(projection.lineage_stagnation, 0.25)
        forwarded = projection.as_core_kwargs()
        self.assertEqual(
            set(forwarded),
            {
                "actionability",
                "evidence_association",
                "positive_feedback",
                "negative_feedback",
                "drag",
                "duplication",
                "refutation",
                "staleness",
                "lineage_stagnation",
            },
        )

    def test_synthetic_component_projection_is_explicit_but_zero_is_legacy_compatible(self) -> None:
        explicit = build_synthetic_trace_projection(
            {"a": {"duplication": 0.2, "staleness": 0.0}}
        ).for_task("a")
        self.assertIn("duplication", explicit.as_core_kwargs())
        zero = build_synthetic_trace_projection(["a"]).for_task("a")
        self.assertNotIn("duplication", zero.as_core_kwargs())

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

    def test_full_current_state_is_recomputed_without_delta_replay(self) -> None:
        adapter = TraceAllocationProjectionAdapter(
            TraceProjectionLimits(feedback_kappa=1.0)
        )
        rows = [
            _row(
                1,
                "piece_snapshot",
                record_id="trace-a",
                trace_id="trace-a",
                lineage_id="lineage-a",
                active=True,
                event_time=100.0,
            ),
            _row(
                2,
                "frontier",
                record_id="frontier-a",
                trace_id="trace-a",
                lineage_id="lineage-a",
                event_time=100.0,
            ),
            _row(
                3,
                "worker_exposure",
                record_id="exposure-a",
                trace_id="trace-a",
                evidence_id="trace-a",
                worker_id="worker-a",
                exposure_id="exposure-a",
            ),
        ]
        first = adapter.project_full_records(
            ["a"], rows, source_watermark="snapshot-1", reference_time=100.0
        )
        repeated = adapter.project_full_records(
            ["a"], rows, source_watermark="snapshot-1", reference_time=100.0
        )
        self.assertEqual(first, repeated)
        self.assertEqual(first.for_task("a").actionability, 1.0)
        self.assertEqual(first.for_task("a").feedback_exposure_count, 1)

        # A later complete materialization retains the old active trace even
        # when there is no newly appended event.  It must not be treated as an
        # empty (after_watermark, head] delta.
        later = adapter.project_full_records(
            ["a"], rows, source_watermark="snapshot-2", reference_time=110.0
        )
        self.assertEqual(later.for_task("a").actionability, 1.0)
        self.assertEqual(later.for_task("a").feedback_exposure_count, 1)

    def test_full_state_recency_association_feedback_and_drag_proportions(self) -> None:
        adapter = TraceAllocationProjectionAdapter(
            TraceProjectionLimits(
                recency_window_seconds=100.0,
                feedback_kappa=1.0,
                feedback_values={
                    "useful": 1.0,
                    "unsafe": -1.0,
                    "not_used": 0.0,
                },
                duplicate_weight=1.0,
                refutation_weight=1.0,
                stale_weight=1.0,
                lineage_stagnation_weight=1.0,
            )
        )
        rows = [
            # Active trace weights are exp(-0)=1 and exp(-100/100)=e^-1.
            _row(1, "piece_snapshot", record_id="t1", trace_id="t1", lineage_id="l1", active=True, event_time=100.0),
            _row(2, "frontier", record_id="f1", trace_id="t1", lineage_id="l1", event_time=100.0),
            _row(3, "evidence_link", record_id="v1", trace_id="t1", lineage_id="l1", evidence_id="e1", source_outcome_id="trace-outcome"),
            _row(4, "evidence_link", record_id="v1-copy", trace_id="t1", lineage_id="l1", evidence_id="e1", source_outcome_id="trace-outcome"),
            _row(5, "piece_snapshot", record_id="t2", trace_id="t2", lineage_id="l2", active=True, event_time=0.0),
            _row(6, "duplicate", record_id="d2", trace_id="t2", lineage_id="l2"),
            _row(7, "piece_snapshot", record_id="t3", trace_id="t3", lineage_id="l3", active=False, lifecycle="stale", event_time=100.0),
            _row(8, "stale_piece", record_id="s3", trace_id="t3", lineage_id="l3"),
            _row(9, "worker_exposure", record_id="x1", trace_id="t1", worker_id="w1", exposure_id="x1"),
            _row(10, "worker_exposure", record_id="x2", trace_id="t2", worker_id="w2", exposure_id="x2"),
            # Two terminal candidates conflict. Higher trust wins negative.
            _row(11, "useful", record_id="fb-low", trace_id="t1", worker_id="w1", exposure_id="x1", terminal=True, trust=1.0, trust_rank=1, committed_sequence=11),
            _row(12, "unsafe", record_id="fb-high", trace_id="t1", worker_id="w1", exposure_id="x1", terminal=True, trust=0.75, trust_rank=2, committed_sequence=12),
            # Exposure x2 is neutral/unanswered and stays in the denominator.
        ]
        projection = adapter.project_full_records(
            ["a"], rows, source_watermark=12, reference_time=100.0
        ).for_task("a")
        expected_actionability = 1.0 / (1.0 + math.exp(-1.0))
        self.assertAlmostEqual(projection.actionability, expected_actionability)
        self.assertAlmostEqual(projection.evidence_association, expected_actionability)
        self.assertEqual(projection.association_count, 1)
        self.assertEqual(projection.feedback_exposure_count, 2)
        self.assertEqual(projection.positive_feedback, 0.0)
        self.assertAlmostEqual(projection.negative_feedback, 0.75 / 3.0)
        self.assertEqual(projection.negative_feedback_count, 1)
        # duplicate=one of three traces, stale=one of three; the other two
        # drag components are zero, then the configured weighted mean is used.
        self.assertGreater(projection.drag_duplicate_proportion, 0.0)
        self.assertGreater(projection.drag_stale_proportion, 0.0)
        self.assertAlmostEqual(
            projection.drag,
            (
                projection.drag_duplicate_proportion
                + projection.drag_stale_proportion
            ) / 4.0,
        )

    def test_full_state_lifecycle_transition_removes_actionability(self) -> None:
        adapter = TraceAllocationProjectionAdapter()
        base = [
            _row(1, "piece_snapshot", record_id="trace", trace_id="trace", lineage_id="lineage", active=True, event_time=1.0),
            _row(2, "frontier", record_id="frontier", trace_id="trace", lineage_id="lineage", event_time=1.0),
        ]
        active = adapter.project_full_records(["a"], base, source_watermark=2).for_task("a")
        stale = adapter.project_full_records(
            ["a"],
            base + [_row(3, "stale_piece", record_id="stale", trace_id="trace", lineage_id="lineage", active=False, lifecycle="stale", event_time=2.0)],
            source_watermark=3,
        ).for_task("a")
        self.assertEqual(active.actionability, 1.0)
        self.assertEqual(stale.actionability, 0.0)
        self.assertGreater(stale.drag, 0.0)

    def test_full_state_ordinary_outcomes_and_bounds_fail_closed(self) -> None:
        adapter = TraceAllocationProjectionAdapter(
            TraceProjectionLimits(max_records=3, max_records_per_task=2)
        )
        rows = [
            _row(1, "piece_snapshot", record_id="trace", trace_id="trace", lineage_id="lineage", active=True),
            _row(2, "evidence_link", record_id="link", trace_id="trace", lineage_id="lineage", evidence_id="checker-1", source_outcome_id="checker-1"),
        ]
        projection = adapter.project_full_records(
            ["a"], rows, ordinary_outcome_ids=["checker-1"], source_watermark=2
        ).for_task("a")
        self.assertEqual(projection.evidence_association, 0.0)
        self.assertEqual(projection.source_outcome_ids, ())
        with self.assertRaises(OverflowError):
            adapter.project_full_records(
                ["a"], rows + [_row(3, "frontier", trace_id="trace", lineage_id="lineage")], source_watermark=3
            )

    def test_full_state_dedup_identity_is_task_scoped(self) -> None:
        rows = [
            _row(1, "piece_snapshot", record_id="same", task_id="a", trace_id="ta", lineage_id="la", active=True),
            _row(1, "piece_snapshot", record_id="same", task_id="b", trace_id="tb", lineage_id="lb", active=True),
            _row(2, "frontier", record_id="frontier", task_id="a", trace_id="ta", lineage_id="la"),
            _row(2, "frontier", record_id="frontier", task_id="b", trace_id="tb", lineage_id="lb"),
        ]
        batch = TraceAllocationProjectionAdapter().project_full_records(
            ["a", "b"], rows, source_watermark=2
        )
        self.assertEqual(batch.for_task("a").actionability, 1.0)
        self.assertEqual(batch.for_task("b").actionability, 1.0)

    def test_project_does_not_fast_forward_past_returned_rows(self) -> None:
        source = _Source(
            [_row(6, "frontier", lineage_id="l6")],
            watermark=9,
        )
        batch = TraceAllocationProjectionAdapter().project(
            source, ["a"], after_watermark=5
        )
        self.assertEqual(batch.watermark, 6)

    def test_project_full_records_requires_snapshot_attestation_for_source_path(self) -> None:
        # The two-field legacy batch is a delta page, even at cursor zero;
        # callers needing current-state semantics must use the explicit
        # snapshot_id/full-record API.
        source = _Source([_row(1, "frontier", lineage_id="l1")], watermark=1)
        result = TraceAllocationProjectionAdapter().project(source, ["a"])
        self.assertEqual(result.for_task("a").frontier_count, 1)

    def test_default_feedback_rate_preserves_exposure_denominator(self) -> None:
        projection = TraceAllocationProjectionAdapter().project_full_records(
            ["a"],
            [
                _row(1, "worker_exposure", record_id="x1", evidence_id="t1", worker_id="w", exposure_id="x1"),
                _row(2, "worker_exposure", record_id="x2", evidence_id="t2", worker_id="w", exposure_id="x2"),
                _row(3, "feedback_positive", record_id="f1", evidence_id="t1", worker_id="w", exposure_id="x1", effective=True, terminal=True),
            ],
            source_watermark=3,
        ).for_task("a")
        self.assertEqual(projection.feedback_exposure_count, 2)
        self.assertEqual(projection.positive_feedback, 0.5)

    def test_full_state_requires_declared_trace_or_lineage_identity(self) -> None:
        projection = TraceAllocationProjectionAdapter().project_full_records(
            ["a"],
            [_row(1, "piece_snapshot", evidence_id="shared-evidence", active=True)],
            source_watermark=1,
        ).for_task("a")
        self.assertTrue(projection.is_zero)

    def test_equal_sequence_lifecycle_order_is_deterministic(self) -> None:
        rows = [
            _row(1, "piece_snapshot", record_id="trace", trace_id="t", lineage_id="l", active=True),
            _row(2, "frontier", record_id="frontier", trace_id="t", lineage_id="l", actionable=True),
            _row(3, "lifecycle", record_id="z-active", trace_id="t", lineage_id="l", lifecycle="active", active=True),
            _row(3, "lifecycle", record_id="a-stale", trace_id="t", lineage_id="l", lifecycle="stale", active=False),
        ]
        adapter = TraceAllocationProjectionAdapter()
        forward = adapter.project_full_records(["a"], rows, source_watermark=3)
        reverse = adapter.project_full_records(["a"], list(reversed(rows)), source_watermark=3)
        self.assertEqual(forward, reverse)
        self.assertEqual(forward.for_task("a").actionability, 0.0)


if __name__ == "__main__":
    unittest.main()
