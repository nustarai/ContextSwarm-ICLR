from __future__ import annotations

import tempfile
import json
from pathlib import Path
import unittest

from contextswarm_mini.selection_artifacts import (
    ArtifactValidationError, collect_paired_metrics, export_figure3_run_summary, read_feedback, reconstruct_metrics,
    validate_attribution_joins, write_feedback, write_selector_decisions, write_exposures,
    write_relations,
)


class SelectionArtifactsTests(unittest.TestCase):
    def _rows(self):
        decisions = [{"decision_id": "d1", "selected_task_id": "p1", "policy": "formula"}]
        exposures = [{"exposure_id": "e1", "decision_id": "d1", "task_id": "p1", "started_elapsed_seconds": 1}]
        feedback = [{"feedback_id": "f1", "exposure_id": "e1", "task_id": "p1", "score": 1, "elapsed_seconds": 2}]
        relations = [{"relation_id": "r1", "decision_id": "d1", "exposure_id": "e1", "feedback_id": "f1", "task_id": "p1"}]
        return decisions, exposures, feedback, relations

    def test_jsonl_round_trip_join_and_metrics(self):
        decisions, exposures, feedback, relations = self._rows()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_selector_decisions(root / "decisions.jsonl", decisions)
            write_exposures(root / "exposures.jsonl", exposures)
            write_feedback(root / "feedback.jsonl", feedback)
            write_relations(root / "relations.jsonl", relations)
            loaded = read_feedback(root / "feedback.jsonl")
        validate_attribution_joins(decisions, exposures, loaded, relations)
        metrics = reconstruct_metrics(loaded, task_order=["p1", "p2"], horizon_seconds=10, exposures=exposures)
        self.assertEqual(metrics["final_score"], 1.0)
        self.assertEqual(metrics["time_to_k_proofs_seconds"]["1"], 2.0)
        self.assertEqual(metrics["normalized_score_time_auc"], 0.4)
        self.assertEqual(metrics["usage"]["exposure_count"], 1)

    def test_attribution_rejects_cross_task_feedback(self):
        decisions, exposures, feedback, relations = self._rows()
        feedback[0]["task_id"] = "p2"
        with self.assertRaises(ArtifactValidationError):
            validate_attribution_joins(decisions, exposures, feedback, relations)

    def test_paired_collection_fails_closed_and_calculates_difference(self):
        metadata = {"comparison_contract": "v1", "task_order": ["p1"], "paired_seed": 7,
                    "model": "m", "horizon_seconds": 10, "cps_capacity": 2,
                    "evaluator": "judge-v1", "runtime": {"limit": 1}}
        left = [{"run_id": "seed-7", "metadata": metadata,
                 "metrics": {"final_score": 1, "normalized_score_time_auc": .5}}]
        right = [{"run_id": "seed-7", "metadata": dict(metadata),
                  "metrics": {"final_score": 0, "normalized_score_time_auc": .2}}]
        report = collect_paired_metrics(left, right)
        self.assertEqual(report["pairs"][0]["differences"]["final_score"], 1.0)
        right[0]["metadata"]["model"] = "other"
        with self.assertRaisesRegex(ArtifactValidationError, "model"):
            collect_paired_metrics(left, right)

    def test_export_reconstructs_figure3_fields_from_final_and_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_meta.json").write_text(json.dumps({
                "run_id": "run-a", "seed": 7, "model": "m", "time_limit_seconds": 10,
                "max_parallel": 2, "judge_kind": "mock", "lean_env_id": "env",
                "lean_verification_profile": "strict", "lean_max_concurrent_evaluations": 1,
                "pi_timeout_seconds": 5, "effective_runtime_limits": {"memory": 1},
                "runtime_provenance": {"mock_agent": True},
            }))
            (root / "final.json").write_text(json.dumps({
                "score": 1, "horizon_seconds": 10,
                "verdicts": {"p1": {"score": 1}, "p2": {"score": 0}},
                "agents": [{"agent_id": "a"}], "allocation_scheduler_agents": [],
                "allocation": {"policy": "formula", "agent_calls": 3},
                "judge_result_cache": {"enabled": False},
                "score_time": {"normalized_score_time_auc": .4,
                               "time_to_k_proofs_seconds": {"1": 2, "2": None}},
            }))
            summary = export_figure3_run_summary(
                root, comparison_contract={"arm": "fixed-v1"}, task_order=["p1", "p2"]
            )
            persisted = json.loads((root / "figure3_summary.json").read_text())
        self.assertEqual(summary, persisted)
        self.assertEqual(summary["metrics"]["final_score"], 1.0)
        self.assertEqual(summary["metrics"]["time_to_k_proofs_seconds"]["1"], 2)
        self.assertEqual(summary["metrics"]["usage"]["allocation_policy"], "formula")

    def test_export_fails_closed_when_task_order_does_not_match_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_meta.json").write_text("{}")
            (root / "final.json").write_text(json.dumps({"verdicts": {}}))
            with self.assertRaisesRegex(ArtifactValidationError, "task_order"):
                export_figure3_run_summary(root, comparison_contract="v1", task_order=["p1"])

    def test_export_aggregates_model_usage_once_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_meta.json").write_text(json.dumps({
                "run_id": "run-u", "seed": 1, "model": "m", "time_limit_seconds": 10,
                "max_parallel": 1, "judge_kind": "mock", "lean_env_id": "env",
                "lean_verification_profile": "strict", "lean_max_concurrent_evaluations": 1,
                "pi_timeout_seconds": 5, "effective_runtime_limits": {"memory": 1},
                "runtime_provenance": {"mock_agent": True},
            }))
            (root / "final.json").write_text(json.dumps({
                "score": 0, "horizon_seconds": 10, "verdicts": {"p1": {"score": 0}},
                "agents": [], "allocation_scheduler_agents": [], "allocation": {},
                "judge_result_cache": {}, "score_time": {
                    "normalized_score_time_auc": 0, "time_to_k_proofs_seconds": {"1": None}
                },
            }))
            (root / "pi_events.jsonl").write_text("\n".join([
                json.dumps({"actor_id": "agent-1", "session_id": "s", "input_tokens": 10, "output_tokens": 2, "total_tokens": 12}),
                json.dumps({"actor_id": "agent-1", "session_id": "s", "input_tokens": 20, "output_tokens": 3, "total_tokens": 23}),
                json.dumps({"actor_id": "agent-2", "session_id": "t", "input_tokens": 4, "output_tokens": 1, "total_tokens": 5}),
            ]) + "\n")
            summary = export_figure3_run_summary(root, comparison_contract="v1", task_order=["p1"])
        usage = summary["metrics"]["usage"]
        self.assertEqual(usage["model_sessions"], 2)
        self.assertEqual(usage["model_input_tokens"], 24)
        self.assertEqual(usage["model_total_tokens"], 28)
