from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import tempfile
import unittest

from contextswarm_mini.cps import CPSStore
from contextswarm_mini.selection_store import CANONICAL_FEEDBACK_KINDS
from contextswarm_mini.selection_runtime import SelectionRuntime
from contextswarm_mini.selection_store import SelectionStore


class _Config:
    selector_name = "recency"
    selector_version = "test"
    policy_params = {}
    visibility = "project_shared"
    trace_slot_limit = 2
    context_token_budget = 4096
    tokenizer = "utf8_bytes_ceil_div4_v1"
    seed = 3
    tie_break = "trace_id_asc"


FEEDBACK_VALUES = {
    "useful": 1.0,
    "not_useful": -1.0,
    "misleading": -1.0,
    "stale": -0.75,
    "unsafe": -1.0,
    "duplicate": -0.5,
    "diagnostic_useful": 0.75,
    "needs_refinement": -0.25,
    "not_used": 0.0,
    "route_attempted": 0.0,
    "route_improving": 1.0,
}


@dataclass(frozen=True)
class _NuConfig:
    selector_name: str = "nustigmergy"
    selector_version: str = "test"
    policy_params: dict = field(default_factory=lambda: {
        "feedback_values": dict(FEEDBACK_VALUES),
        "weights": {"relevance": 1.0, "evidence": 1.0, "interaction": 1.0,
                    "structure": 1.0, "state": 1.0},
        "kappa": 1.0,
        "quota": 2,
        "score_precision": 8,
        "exploration": 0.0,
        "tie_break": "trace_id_asc",
    })
    visibility: str = "project_shared"
    trace_slot_limit: int = 2
    context_token_budget: int = 20_000
    tokenizer: str = "utf8_bytes_ceil_div4_v1"
    seed: int = 3
    tie_break: str = "trace_id_asc"


class SelectionRuntimeTests(unittest.TestCase):
    def _stores(self, root: str) -> tuple[CPSStore, SelectionStore]:
        return CPSStore(Path(root) / "cps.sqlite3"), SelectionStore(Path(root) / "selection.sqlite3")

    def test_project_shared_search_and_digest_share_persisted_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cps = CPSStore(Path(tmp) / "cps.sqlite3")
            selection = SelectionStore(Path(tmp) / "selection.sqlite3")
            cps.create_piece(task_id="task-a", author="a", kind="handoff", title="A", body="alpha")
            cps.create_piece(task_id="task-b", author="b", kind="handoff", title="B", body="beta")
            runtime = SelectionRuntime(cps, selection, _Config(), run_id="r", paired_seed=9)
            result = runtime.search(task_id="task-a", actor_id="worker", query="", request_key="req-1")
            self.assertEqual({item["title"] for item in result["items"]}, {"A", "B"})
            digest = runtime.digest(task_id="task-a", actor_id="worker", query="", episode=1)
            self.assertIn("A", digest)
            self.assertIn("B", digest)
            self.assertEqual(len(selection.get_search_by_request_key("req-1")["items"]), 2)
            self.assertEqual(len(selection.get_search_by_request_key("r:digest:task-a:worker:1")["items"]), 2)

    def test_filters_control_kinds_and_uses_committed_rowid_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cps, selection = self._stores(tmp)
            old = cps.create_piece(task_id="task-a", author="a", kind="handoff", title="old", body="alpha")
            cps.create_piece(task_id="task-b", author="runner", kind="validation_result", title="control", body="secret")
            new = cps.create_piece(task_id="task-b", author="b", kind="strategy", title="new", body="beta")
            runtime = SelectionRuntime(cps, selection, _Config(), run_id="r")
            result = runtime.search("task-a", "worker", request_key="filtered")
            self.assertEqual([item["trace_id"] for item in result["items"]], [new["id"], old["id"]])
            self.assertEqual(result["snapshot_event_seq"], 3)
            self.assertEqual(result["shared_trace_visibility"], "project_shared")
            self.assertNotIn("secret", json.dumps(result))
            chain = selection.get_search_by_request_key("filtered")
            assert chain is not None
            self.assertEqual(len(chain["candidates"]), 2)
            self.assertEqual(
                chain["search_event"]["snapshot_watermarks"]["cps_pieces_rowid"], 3
            )

    def test_request_key_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cps, selection = self._stores(tmp)
            cps.create_piece(task_id="task", author="a", kind="handoff", title="one", body="alpha")
            runtime = SelectionRuntime(cps, selection, _Config(), run_id="r")
            runtime.search("task", "worker", query="alpha", request_key="same", search_ordinal=0)
            with self.assertRaises(ValueError):
                runtime.search("task", "worker", query="different", request_key="same", search_ordinal=0)

    def test_replay_restores_persisted_watermark_and_pool_after_cps_changes(self) -> None:
        """A request-key replay is bound to its original snapshot, not live CPS."""

        with tempfile.TemporaryDirectory() as tmp:
            cps, selection = self._stores(tmp)
            old = cps.create_piece(
                task_id="task", author="a", kind="handoff", title="old", body="old body"
            )
            runtime = SelectionRuntime(cps, selection, _Config(), run_id="r")
            first = runtime.search("task", "worker", request_key="replay-watermark")
            self.assertEqual(first["snapshot_event_seq"], old["rowid"] if "rowid" in old else 1)
            first_chain = selection.get_search_by_request_key("replay-watermark")
            self.assertIsNotNone(first_chain)
            assert first_chain is not None
            watermark = first_chain["search_event"]["snapshot_watermarks"]

            # This row must not appear in a replay of the already committed
            # request, even though it would win the recency ranking now.
            cps.create_piece(
                task_id="task", author="b", kind="handoff", title="new", body="new body"
            )
            replay = runtime.search("task", "worker", request_key="replay-watermark")
            self.assertEqual(replay["snapshot_event_seq"], first["snapshot_event_seq"])
            self.assertEqual(replay["snapshot_watermarks"], watermark)
            self.assertEqual(replay["eligible_trace_ids"], [old["id"]])
            self.assertEqual([item["trace_id"] for item in replay["items"]],
                             [item["trace_id"] for item in first["items"]])
            self.assertNotIn("new", json.dumps(replay))

    def test_empty_pool_has_no_fabricated_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cps, selection = self._stores(tmp)
            runtime = SelectionRuntime(cps, selection, _Config(), run_id="r")
            result = runtime.search("task", "worker", request_key="empty")
            self.assertEqual(result["items"], [])
            self.assertIsNone(result["exposure_id"])
            self.assertIsNone(selection.get_search_by_request_key("empty"))

    def test_digest_exposes_feedback_refs_and_feedback_weights_ignore_payload_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cps, selection = self._stores(tmp)
            piece = cps.create_piece(task_id="task", author="a", kind="handoff", title="alpha", body="A" * 7_000)
            runtime = SelectionRuntime(cps, selection, _NuConfig(), run_id="r")
            digest = runtime.digest("task", "worker", query="alpha", episode=1)
            match = re.search(r"trace_id=([^ ]+) exposure_item_id=([^]]+)", digest)
            self.assertIsNotNone(match)
            assert match is not None
            trace_id, exposure_item_id = match.groups()
            self.assertEqual(trace_id, piece["id"])
            self.assertNotIn("[context truncated]", digest)
            self.assertEqual(digest, runtime.digest("task", "worker", query="alpha", episode=1))
            feedback = selection.record_feedback(
                request_key="digest-feedback", exposure_item_id=exposure_item_id,
                actor_id="worker", trace_id=trace_id, feedback_kind="useful",
                origin="worker_explicit", payload={"value": -999},
            )
            self.assertTrue(feedback["effective"])
            stats, _ = runtime._trace_stats([trace_id])
            self.assertEqual(stats[trace_id].signed_weight_sum, 1.0)
            self.assertEqual(stats[trace_id].exposure_count, 1)

    def test_nu_runtime_projects_nonzero_common_components_and_exports_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cps, selection = self._stores(tmp)
            piece = cps.create_piece(task_id="task-a", author="a", kind="strategy", title="induction", body="proof")
            selection.record_verifier_evidence(
                request_key="ev", trace_id=piece["id"], task_id="task-a", verifier_id="judge",
                status="PROVED", evidence={"bound": True},
            )
            selection.record_relation(
                request_key="rel", source_trace_id=piece["id"], target_trace_id="other",
                relation_kind="supports", actor_id="a",
            )
            runtime = SelectionRuntime(cps, selection, _NuConfig(), run_id="r")
            result = runtime.search("task-b", "worker", query="induction", request_key="nu")
            scores = result["ranked"][0]["component_scores"]
            self.assertGreater(scores["relevance"], 0.0)
            self.assertGreater(scores["evidence"], 0.0)
            self.assertGreater(scores["structure"], 0.0)
            self.assertGreater(scores["state"], 0.0)
            summary = runtime.summary()
            self.assertEqual(summary["exposure_items"], 1)
            export = Path(tmp) / "selection.jsonl"
            runtime.export_events(export)
            exported = [json.loads(line) for line in export.read_text().splitlines()]
            self.assertEqual({row["event_type"] for row in exported}, {"search", "ranking", "exposure_item"})
            chain = selection.get_search_by_request_key("nu")
            assert chain is not None
            payload = chain["candidates"][0]["candidate_payload"]
            self.assertGreater(payload["relevance"], 0.0)
            self.assertGreater(payload["evidence_score"], 0.0)
            self.assertGreater(payload["structure_score"], 0.0)

    def test_feedback_values_are_explicit_and_cover_canonical_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cps, selection = self._stores(tmp)
            bad = _NuConfig(policy_params={**_NuConfig().policy_params, "feedback_values": {"useful": 1.0}})
            with self.assertRaisesRegex(ValueError, "cover exactly"):
                SelectionRuntime(cps, selection, bad)
            self.assertEqual(set(FEEDBACK_VALUES), set(CANONICAL_FEEDBACK_KINDS))


if __name__ == "__main__":
    unittest.main()
