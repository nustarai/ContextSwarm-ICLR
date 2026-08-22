import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from contextswarm_mini.allocation_audit import (
    AllocationAuditRecord,
    append_allocation_audit,
    build_figure4_run_summary,
    build_figure4_paired_repeat,
    canonical_json_sha256,
    read_allocation_audits,
    validate_capacity_conservation,
    write_figure4_run_summary,
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
        with self.assertRaises(ValueError):
            validate_capacity_conservation(replace(row, capacity_delta_sum=1))

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

    def test_audit_identity_and_score_types_are_not_lossily_coerced(self):
        base = _record().as_dict()
        base.pop("schema_version")
        base.pop("capacity_delta_sum")
        base.pop("capacity_conserved")

        malformed = (
            ("eligible_task_ids", "abc"),  # scalar strings must not become IDs a/b/c
            ("eligible_task_ids", ["a", 2, "c"]),
            ("task_only_scores", {"a": "0.2", "b": 0.5, "c": 0.6}),
            ("state_id", None),
            ("decision_id", 17),
            ("fallback_reason", 3),
        )
        for field, value in malformed:
            with self.subTest(field=field, value=value):
                row = dict(base)
                row[field] = value
                with self.assertRaises(ValueError):
                    AllocationAuditRecord.create(**row)
        for field in ("state_id", "decision_id", "allocation_config_sha256"):
            with self.subTest(missing=field):
                row = dict(base)
                row.pop(field)
                with self.assertRaises(ValueError):
                    AllocationAuditRecord.create(**row)

    def test_audit_ids_reject_control_or_surrounding_whitespace(self):
        base = _record().as_dict()
        base.pop("schema_version")
        base.pop("capacity_delta_sum")
        base.pop("capacity_conserved")
        for field, value in (
            ("decision_id", " d-1"),
            ("trace_state_selected_task_id", "a\n"),
            ("fallback_reason", "bad\x00reason"),
        ):
            with self.subTest(field=field):
                row = dict(base)
                row[field] = value
                with self.assertRaises(ValueError):
                    AllocationAuditRecord.create(**row)

    def test_append_revalidates_directly_constructed_records(self):
        malformed = replace(_record(), task_only_scores={"a": "0.2", "b": 0.5, "c": 0.6})
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                append_allocation_audit(Path(td) / "audit.jsonl", malformed)

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
            summary = build_figure4_run_summary(run_id="r", policy="llm_scheduler", paired_seed=2, repeat=1, horizon_seconds=10, total_capacity=4, initial_allocation={"a": 1}, accepted_score_history=[{"elapsed_seconds": 2, "accepted_score": 1}], max_score=2, scheduler_cost={"calls": 1, "input_tokens": 4, "output_tokens": 2, "latency_seconds": 1.5, "capacity_reservations": 1, "reserved_slot_seconds": 1.25}, allocation_metrics={"decisions": 1, "admitted_decisions": 1}, solver_usage={"calls": 1, "input_tokens": 10, "output_tokens": 3, "slot_seconds": 5.0}, evaluator_usage={"calls": 1, "admissions": 1, "terminal_receipts": 1})
            self.assertEqual(summary["schema_version"], "contextswarm_figure4_run_summary_v1")
            self.assertEqual(summary["scheduler_cost"]["calls"], 1)
            self.assertEqual(summary["nauc"], .4)
            self.assertEqual(summary["time_to_k_seconds"], {"1": 2.0, "2": None})
            self.assertEqual(summary["scheduler_cost"]["latency_seconds"], 1.5)
            self.assertEqual(summary["scheduler_cost"]["reserved_slot_seconds"], 1.25)
            self.assertEqual(summary["capacity_usage"]["occupied_slot_seconds"], 6.25)

    def test_summary_uses_cumulative_history_and_exact_hashes(self):
        parameters = {"task_state": {"checker_quality": 1.0}}
        contract = {"dataset": "d", "label": "非 ASCII"}
        summary = build_figure4_run_summary(
            run_id="r",
            policy="trace_state",
            paired_seed=9,
            repeat=3,
            comparison_contract=contract,
            task_order=["b", "a"],
            horizon_seconds=10,
            total_capacity=2,
            initial_allocation={"b": 1, "a": 1},
            accepted_score_history=[
                {"elapsed_seconds": 2, "accepted_score": 1, "task_id": "b"},
                {"elapsed_seconds": 7, "accepted_score": 2, "task_id": "a"},
            ],
            max_score=2,
            allocation_parameters=parameters,
            allocation_config_sha256=canonical_json_sha256(parameters),
        )
        self.assertEqual(summary["task_order"], ["b", "a"])
        self.assertEqual(summary["final_accepted_score"], 2.0)
        self.assertEqual(summary["nauc"], 0.55)
        self.assertEqual(
            summary["comparison_contract_sha256"], canonical_json_sha256(contract)
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "summary.json"
            write_figure4_run_summary(path, summary)
            tampered = dict(summary)
            tampered["comparison_contract"] = {"dataset": "changed"}
            with self.assertRaisesRegex(ValueError, "contract hash mismatch"):
                write_figure4_run_summary(path, tampered)

    def test_summary_rejects_capacity_and_count_mismatches(self):
        common = dict(
            run_id="r",
            policy="llm_scheduler",
            paired_seed=1,
            repeat=1,
            task_order=["a"],
            horizon_seconds=10,
            total_capacity=1,
            initial_allocation={"a": 1},
            accepted_score_history=[],
            max_score=1,
            allocation_metrics={"decisions": 1},
        )
        with self.assertRaisesRegex(ValueError, "exceed capacity"):
            build_figure4_run_summary(
                **common,
                solver_usage={"slot_seconds": 9.5},
                scheduler_cost={"reserved_slot_seconds": 1.0},
            )
        with self.assertRaisesRegex(ValueError, "fallbacks.*exceed"):
            build_figure4_run_summary(
                **{**common, "allocation_metrics": {"decisions": 1, "fallbacks": 2}}
            )

    def test_paired_repeat_has_bootstrap_ready_contrasts(self):
        arms = {
            policy: {"policy": policy, "nauc": nauc, "final_accepted_score": 1}
            for policy, nauc in (("uniform_refill", .1), ("task_state", .2), ("trace_state", .4), ("llm_scheduler", .3))
        }
        row = build_figure4_paired_repeat(paired_repeat_id="r-1", paired_seed=4, arms=arms)
        contrast = row["registered_contrasts"]["trace_state_minus_task_state"]
        self.assertAlmostEqual(contrast["nauc"], .2)
        self.assertEqual(len(row["comparison_contract_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
