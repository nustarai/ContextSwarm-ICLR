from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
from unittest import mock
import unittest

from contextswarm_mini.config import SelectionConfig, load_config
from contextswarm_mini.runner import run_experiment
from contextswarm_mini.selection_store import EXPORT_SCHEMA_VERSION, SelectionStore
import contextswarm_mini.runner as runner_module


ROOT = Path(__file__).resolve().parents[1]


def _selection_config() -> SelectionConfig:
    return SelectionConfig(
        enabled=True,
        selector_name="random",
        selector_version="mock_e2e_v1",
        visibility="project_shared",
        trace_slot_limit=2,
        context_token_budget=4096,
        tokenizer="unicode_word_v1",
        seed=17,
        tie_break="trace_id_asc",
        policy_params={"sample_without_replacement": True},
        direct_messages=False,
        candidate_transfer=False,
    )


class SelectionMockEndToEndTests(unittest.TestCase):
    def test_mock_run_closes_out_attributed_feedback_and_exports_store(self) -> None:
        """Exercise the runner-owned digest path with an offline mock solver.

        The stock mock solver intentionally publishes no CPS knowledge.  Seed
        one ordinary trace at selection-runtime initialization, then emulate a
        solver's attributed feedback immediately after the digest exposure is
        created.  All mutations still use the production CPS/runtime/store
        implementations; only the offline solver behavior is replaced.
        """

        base = load_config("configs/smoke.toml", ROOT)
        config = replace(base, selection=_selection_config())
        original_initialize = runner_module._initialize_selection_runtime
        original_mock_result = runner_module._mock_result
        runtime_holder: dict[str, object] = {}
        rendered_digests: list[str] = []

        def initialize_with_trace(*args, **kwargs):
            runtime = original_initialize(*args, **kwargs)
            self.assertIsNotNone(runtime)
            assert runtime is not None
            runtime.cps_store.create_piece(
                task_id="seed-task",
                author="seed-worker",
                kind="note",
                title="Seeded reusable trace",
                body="A bounded project-shared hint for the mock selection run.",
                tags=["mock", "e2e"],
            )
            original_digest = runtime.digest

            def digest_with_capture(**digest_kwargs):
                rendered = original_digest(**digest_kwargs)
                rendered_digests.append(rendered)
                return rendered

            runtime.digest = digest_with_capture
            runtime_holder["runtime"] = runtime
            return runtime

        def mock_result_with_feedback(actor_id: str, task_id: str, episode: int):
            runtime = runtime_holder.get("runtime")
            if runtime is not None and actor_id.startswith("agent-"):
                request_key = (
                    f"{runtime.run_id}:digest:{task_id}:{actor_id}:{episode}"
                )
                chain = runtime.selection_store.get_search_by_request_key(request_key)
                self.assertIsNotNone(chain)
                assert chain is not None
                self.assertTrue(chain["items"])
                item = chain["items"][0]
                runtime.selection_store.record_feedback(
                    request_key=f"mock-feedback:{request_key}",
                    exposure_item_id=item["exposure_item_id"],
                    actor_id=actor_id,
                    trace_id=item["trace_id"],
                    feedback_kind="useful",
                    origin="mock_e2e_solver",
                    terminal=True,
                    payload={"used": True},
                )
            return original_mock_result(actor_id, task_id, episode)

        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(
                    runner_module,
                    "_initialize_selection_runtime",
                    side_effect=initialize_with_trace,
                ),
                mock.patch.object(
                    runner_module,
                    "_mock_result",
                    side_effect=mock_result_with_feedback,
                ),
            ):
                run_dir = run_experiment(
                    config,
                    mock_agent=True,
                    output_override=Path(temporary),
                )

            final = json.loads((run_dir / "final.json").read_text(encoding="utf-8"))
            summary = json.loads(
                (run_dir / "selection_summary.json").read_text(encoding="utf-8")
            )
            runtime_metadata = json.loads(
                (run_dir / "selection_runtime.json").read_text(encoding="utf-8")
            )

            self.assertEqual(final["status"], "COMPLETED")
            self.assertEqual(final["selection"], summary)
            self.assertEqual(summary["status"], "closed")
            self.assertTrue(summary["broker_drained"])
            self.assertFalse(summary["direct_messages"])
            self.assertFalse(summary["candidate_transfer"])
            self.assertEqual(summary["counts"]["search_events"], 2)
            self.assertEqual(summary["counts"]["exposures"], 2)
            self.assertEqual(summary["counts"]["exposure_items"], 2)
            self.assertEqual(summary["counts"]["feedback_events"], 2)
            self.assertEqual(
                runtime_metadata["selection_config_id"],
                summary["selection_config_id"],
            )
            self.assertEqual(
                runtime_metadata["comparison_contract_id"],
                summary["comparison_contract_id"],
            )

            store = SelectionStore(run_dir / "selection.sqlite3")
            self.assertEqual(len(rendered_digests), 2)
            for digest in rendered_digests:
                match = re.search(
                    r"feedback_ref: trace_id=(\S+) exposure_item_id=(\S+)\]",
                    digest,
                )
                self.assertIsNotNone(match)
                assert match is not None
                trace_id, exposure_item_id = match.groups()
                chain = store.attribution_chain(exposure_item_id)
                self.assertIsNotNone(chain)
                assert chain is not None
                self.assertEqual(chain["exposure_item"]["trace_id"], trace_id)
                self.assertEqual(len(chain["feedback_events"]), 1)

            effective = store.effective_feedback()
            self.assertEqual(len(effective), 2)
            for feedback in effective:
                self.assertEqual(feedback["feedback_kind"], "useful")
                self.assertTrue(feedback["effective"])
                chain = store.attribution_chain(feedback["exposure_item_id"])
                self.assertIsNotNone(chain)
                assert chain is not None
                self.assertEqual(
                    chain["exposure_item"]["trace_id"], feedback["trace_id"]
                )
                self.assertEqual(
                    chain["search_event"]["actor_id"], feedback["actor_id"]
                )

            export_path = run_dir / "selection_attribution.probe.jsonl"
            export = store.export_jsonl(export_path)
            self.assertEqual(export["schema"], EXPORT_SCHEMA_VERSION)
            self.assertEqual(export["summary"]["counts"]["feedback_events"], 2)
            self.assertEqual(export["record_type_counts"]["feedback_event"], 2)
            records = [
                json.loads(line)
                for line in export_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), export["record_count"])
            self.assertTrue(
                all(record["schema"] == EXPORT_SCHEMA_VERSION for record in records)
            )


if __name__ == "__main__":
    unittest.main()
