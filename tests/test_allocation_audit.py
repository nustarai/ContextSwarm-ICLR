import json
from pathlib import Path
import tempfile
import unittest

from contextswarm_mini.allocation_audit import (
    AllocationAuditRecord,
    append_allocation_audit,
    build_figure4_run_summary,
    read_allocation_audits,
    validate_capacity_conservation,
)


def _record():
    return AllocationAuditRecord.create(
        state_id="a" * 64, decision_id="d-1", eligible_task_ids=["a", "b", "c"], allocation_config_sha256="c" * 64,
        task_only_scores={"a": .2, "b": .5, "c": .6}, trace_increments={"a": .55, "b": 0, "c": 0}, trace_total_scores={"a": .75, "b": .5, "c": .6},
        allocation_before={"a": 1, "b": 1, "c": 1}, trace_state_selected_task_id="a", task_state_selected_task_id="c", admitted_task_id="a",
        active_slots_before=3, active_slots_after=4, free_slots_before=1, free_slots_after=0, scheduler_reserved_slots_before=0, scheduler_reserved_slots_after=0, total_capacity=4,
    )


class AuditTests(unittest.TestCase):
    def test_same_state_vectors_and_delta(self):
        row = _record()
        self.assertEqual(row.trace_state_allocation_after, {"a": 2, "b": 1, "c": 1})
        self.assertEqual(row.task_state_allocation_after, {"a": 1, "b": 1, "c": 2})
        self.assertEqual(row.capacity_delta_sum, 0)
        self.assertTrue(validate_capacity_conservation(row))

    def test_malformed_rows_fail_closed(self):
        with self.assertRaises(ValueError):
            AllocationAuditRecord.from_dict({"schema_version": "bad"})
        with self.assertRaises(ValueError):
            bad = _record().as_dict(); bad.pop("schema_version"); bad.pop("capacity_delta_sum"); bad.pop("capacity_conserved"); bad["admitted_task_id"] = ""
            AllocationAuditRecord.create(**bad)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "audit.jsonl"; path.write_text("not-json\n")
            with self.assertRaises(ValueError): read_allocation_audits(path)
            tampered = _record().as_dict(); tampered["capacity_delta_sum"] = 1
            path.write_text(json.dumps(tampered) + "\n")
            with self.assertRaises(ValueError): read_allocation_audits(path)
            path.write_text('{"schema_version":"x","schema_version":"y"}\n')
            with self.assertRaises(ValueError): read_allocation_audits(path)

    def test_vectors_include_ineligible_tasks_but_scores_do_not(self):
        row = _record().as_dict(); row.pop("schema_version"); row.pop("capacity_delta_sum"); row.pop("capacity_conserved")
        for key in ("allocation_before", "trace_state_allocation_after", "task_state_allocation_after"):
            row[key]["ineligible"] = 0
        self.assertIn("ineligible", AllocationAuditRecord.create(**row).allocation_before)

    def test_jsonl_and_metrics_cost_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "audit.jsonl"; append_allocation_audit(path, _record())
            rows = read_allocation_audits(path, expected_config_sha256="c" * 64)
            self.assertEqual(len(rows), 1)
            summary = build_figure4_run_summary(run_id="r", policy="trace_state", paired_seed=2, repeat=1, horizon_seconds=10, total_capacity=4, initial_allocation={"a": 1}, accepted_score_history=[{"elapsed_seconds": 2, "accepted_score": 1}], max_score=2, scheduler_cost={"calls": 1, "input_tokens": 4, "output_tokens": 2, "latency_seconds": 1.5, "reserved_slot_seconds": 1.5})
            self.assertEqual(summary["schema_version"], "contextswarm_figure4_run_summary_v1")
            self.assertEqual(summary["scheduler_cost"]["calls"], 1)
            self.assertEqual(summary["nauc"], .4)


if __name__ == "__main__":
    unittest.main()
