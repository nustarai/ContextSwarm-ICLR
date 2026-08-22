from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from contextswarm_mini.config import SelectionConfig, load_config
from contextswarm_mini.cps import CPSStore
from contextswarm_mini.runner import (
    RunLogger,
    _initialize_selection_runtime,
    _selection_closeout_summary,
)


ROOT = Path(__file__).resolve().parents[1]


def _selection_config() -> SelectionConfig:
    return SelectionConfig(
        enabled=True,
        selector_name="random",
        selector_version="figure3_v1",
        visibility="project_shared",
        trace_slot_limit=3,
        context_token_budget=4096,
        tokenizer="unicode_word_v1",
        seed=17,
        tie_break="trace_id_asc",
        policy_params={"sample_without_replacement": True},
        direct_messages=False,
        candidate_transfer=False,
    )


class RunnerSelectionCloseoutExportTests(unittest.TestCase):
    def _runtime(self, root: Path):
        config = replace(load_config("configs/smoke.toml", ROOT), selection=_selection_config())
        cps = CPSStore(root / "cps.sqlite3")
        cps.create_piece(
            task_id="task-a",
            author="worker-a",
            kind="note",
            title="trace",
            body="body",
        )
        runtime = _initialize_selection_runtime(
            config,
            root,
            RunLogger(root),
            cps_store=cps,
            run_id="closeout-run",
        )
        assert runtime is not None
        return config, runtime

    def test_closeout_publishes_relative_export_and_same_snapshot_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, runtime = self._runtime(root)
            summary = _selection_closeout_summary(
                config, runtime, broker_drained=True, run_dir=root
            )

            destination = root / "selection_events.jsonl"
            self.assertTrue(destination.is_file())
            artifact = summary["artifact"]
            self.assertEqual(artifact["path"], "selection_events.jsonl")
            self.assertEqual(
                artifact["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest()
            )
            rows = [json.loads(line) for line in destination.read_text().splitlines()]
            self.assertEqual(artifact["record_count"], len(rows))
            self.assertEqual(
                artifact["record_type_counts"],
                {
                    record_type: sum(row["record_type"] == record_type for row in rows)
                    for record_type in artifact["record_type_counts"]
                },
            )
            self.assertEqual(summary["store_summary"], runtime.selection_store.summary())
            self.assertEqual(summary["counts"], summary["store_summary"]["counts"])

    def test_export_failure_is_not_hidden_by_closeout_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, runtime = self._runtime(root)
            with mock.patch.object(
                runtime.selection_store,
                "export_jsonl",
                side_effect=OSError("injected export failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected export failure"):
                    _selection_closeout_summary(
                        config, runtime, broker_drained=True, run_dir=root
                    )
            self.assertFalse((root / "selection_summary.json").exists())


if __name__ == "__main__":
    unittest.main()
