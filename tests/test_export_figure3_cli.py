from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from contextswarm_mini.selection_store import SelectionStore


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_figure3.py"


def _run(root: Path, *, run_id: str, selector: str = "random", contract: str = "c" * 64) -> Path:
    path = root / run_id
    path.mkdir(parents=True)
    task_order = ["p1", "p2"]
    selection_id = f"{selector}-config"
    (path / "run_meta.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "seed": 7,
                "model": "test-model",
                "time_limit_seconds": 10,
                "max_parallel": 2,
                "judge_kind": "mock",
                "lean_env_id": "env",
                "lean_verification_profile": "strict",
                "lean_max_concurrent_evaluations": 1,
                "pi_timeout_seconds": 5,
                "effective_runtime_limits": {"memory": 1},
                "runtime_provenance": {"mock": True},
                "selection": {
                    "enabled": True,
                    "selector_name": selector,
                    "selector_version": "v1",
                    "selection_config_id": selection_id,
                    "direct_messages": False,
                    "candidate_transfer": False,
                },
                "figure3": {
                    "schema_version": "contextswarm_figure3_contract_v1",
                    "comparison_contract_id": contract,
                    "task_order": task_order,
                    "paired_seed": 7,
                    "selector_name": selector,
                    "selector_version": "v1",
                    "selection_config_id": selection_id,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (path / "final.json").write_text(
        json.dumps(
            {
                "status": "COMPLETED",
                "score": 1,
                "max_score": 2,
                "horizon_seconds": 10,
                "verdicts": {"p1": {"score": 1}, "p2": {"score": 0}},
                "agents": [],
                "allocation_scheduler_agents": [],
                "allocation": {"policy": "uniform", "agent_calls": 1},
                "judge_result_cache": {"enabled": False},
                "selection": {
                    "enabled": True,
                    "comparison_contract_id": contract,
                    "selection_config_id": selection_id,
                    "selector_name": selector,
                    "selector_version": "v1",
                },
                "score_time": {
                    "normalized_score_time_auc": 0.4,
                    "time_to_k_proofs_seconds": {"1": 2, "2": None},
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (path / "scoreboard_history.jsonl").write_text(
        json.dumps(
            {
                "task_id": "p1",
                "episode": 1,
                "score": 1,
                "horizon_elapsed_seconds": 2,
                "status": "PROVED",
                "source": "final_evaluation",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # A completed selection arm has a durable store and runner-bound closeout
    # export, even if this small fixture has no recommendations yet.
    store = SelectionStore(path / "selection.sqlite3")
    exported = store.export_jsonl(path / "selection_events.jsonl")
    (path / "selection_runtime.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "selection_config_id": selection_id,
                "selector_name": selector,
                "selector_version": "v1",
                "comparison_contract_id": contract,
                "trace_search": {"status": "available"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (path / "selection_summary.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "status": "closed",
                "selection_config_id": selection_id,
                "selector_name": selector,
                "selector_version": "v1",
                "comparison_contract_id": contract,
                "store_summary": exported["summary"],
                "artifact": {
                    "schema": exported["schema"],
                    "path": "selection_events.jsonl",
                    "sha256": exported["sha256"],
                    "record_count": exported["record_count"],
                    "record_type_counts": exported["record_type_counts"],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class Figure3ExportCliTests(unittest.TestCase):
    def test_export_reads_runner_contract_and_writes_auditable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = _run(Path(temporary), run_id="seed-7")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "export", str(run)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((run / "figure3_summary.json").read_text())
            self.assertEqual(summary["contract"]["task_order"], ["p1", "p2"])
            self.assertEqual(summary["metadata"]["selector_name"], "random")
            self.assertEqual(summary["metrics"]["accepted_score_history"][0]["task_id"], "p1")
            self.assertEqual(summary["artifacts"]["selection_events"]["record_count"], 0)
            self.assertTrue((run / "selection_events.jsonl").exists())

    def test_compare_emits_paired_differences_and_deterministic_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left = _run(root / "left", run_id="seed-7", selector="random")
            right = _run(root / "right", run_id="seed-7", selector="nustigmergy")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "compare",
                    str(left.parent),
                    str(right.parent),
                    "--bootstrap-replicates",
                    "100",
                    "--bootstrap-seed",
                    "3",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["pairs"][0]["paired_seed"], 7)
            self.assertEqual(report["bootstrap"]["replicates"], 100)
            self.assertEqual(report["bootstrap"]["n_pairs"], 1)

    def test_missing_runner_contract_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "run"
            run.mkdir()
            (run / "run_meta.json").write_text("{}")
            (run / "final.json").write_text("{}")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "export", str(run)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("no runner-owned Figure 3 contract", result.stderr)

    def test_conflicting_contracts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = _run(Path(temporary), run_id="seed-7")
            separate = json.loads((run / "run_meta.json").read_text())["figure3"]
            separate["comparison_contract_id"] = "d" * 64
            (run / "figure3_contract.json").write_text(json.dumps(separate))
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "export", str(run)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("contracts disagree", result.stderr)

    def test_tampered_runner_selection_artifact_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = _run(Path(temporary), run_id="seed-7")
            (run / "selection_events.jsonl").write_text("tampered\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "export", str(run)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("digest mismatch", result.stderr)

    def test_incomplete_selection_closeout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = _run(Path(temporary), run_id="seed-7")
            (run / "selection_summary.json").unlink()
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "export", str(run)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("missing selection_summary.json", result.stderr)


if __name__ == "__main__":
    unittest.main()
