from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from contextswarm_mini.selection_store import (
    RequestKeyConflictError,
    SelectionStore,
)


class SelectionStoreCandidatePoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "selection.sqlite3"
        self.store = SelectionStore(self.path)
        self.config = self.store.register_selector_config(
            selector_name="nustigmergy",
            config={"selector_version": "figure3_v1", "kappa": 1.0},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _candidate(trace_id: str, *, body: str, exposure_count: int) -> dict:
        return {
            "trace_id": trace_id,
            "source_task_id": "source-task",
            "task_family": "formal",
            "author_id": "worker-source",
            "scope_key": "project_shared",
            "visibility": "project_shared",
            "kind": "knowledge",
            "title": f"title {trace_id}",
            "body": body,
            "tags": ["lemma", trace_id],
            "created_at": "2026-08-23T00:00:00Z",
            "commit_seq": 17 if trace_id == "trace-z" else 11,
            "lifecycle": "active",
            "cluster_id": f"cluster-{trace_id}",
            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "token_count": 9,
            "evidence": {"verifier_count": 2.0},
            "relations": {"supports": 1},
            "relevance": 0.75,
            "evidence_score": 2.0,
            "structure_score": 1.0,
            "state_score": 1.0,
            "lineage_id": "source-task",
            "feedback": {
                "exposure_count": exposure_count,
                "effective_terminal_count": 1,
                "kind_counts": {"useful": 1},
                "signed_weight_sum": 1.0,
                "positive_count": 1,
                "negative_count": 0,
            },
        }

    def _kwargs(self, request_key: str = "search-with-pool") -> dict:
        candidates = [
            self._candidate("trace-z", body="zeta proof", exposure_count=3),
            self._candidate("trace-a", body="alpha proof", exposure_count=1),
        ]
        return {
            "request_key": request_key,
            "task_id": "task-1",
            "actor_id": "worker-1",
            "selector_config_id": self.config["selector_config_id"],
            "query": {"text": "lemma", "search_ordinal": 4},
            "comparison_identity": {"contract": "fixed"},
            "snapshot_identity": {"snapshot": 17},
            "pool_identity": {"eligible": ["trace-a", "trace-z"]},
            "rankings": [
                {
                    "trace_id": "trace-z",
                    "rank": 2,
                    "selected": False,
                    "component_scores": {"interaction": 0.25},
                },
                {
                    "trace_id": "trace-a",
                    "rank": 1,
                    "selected": True,
                    "component_scores": {"interaction": 0.5},
                    "payload": {"token_count": 9, "total_score": 1.5},
                },
            ],
            # Deliberately reverse the input order.  The store owns canonical
            # pool order, independent of Python/CPS read ordering.
            "eligible_candidates": candidates,
            "snapshot_watermarks": {
                "cps": {"pieces_rowid": 17},
                "selection": {
                    "exposure_item_rowid": 3,
                    "feedback_event_rowid": 2,
                },
            },
        }

    def test_pool_rows_are_stable_complete_and_restart_idempotent(self) -> None:
        first = self.store.record_search(**self._kwargs())
        self.assertFalse(first["idempotent"])
        self.assertEqual(
            [row["trace_id"] for row in first["candidates"]],
            ["trace-a", "trace-z"],
        )
        self.assertEqual(
            [row["pool_order"] for row in first["candidates"]], [1, 2]
        )
        candidate = first["candidates"][0]
        self.assertEqual(candidate["candidate_payload"]["body"], "alpha proof")
        self.assertEqual(candidate["candidate_payload"]["relevance"], 0.75)
        self.assertEqual(candidate["candidate_payload"]["lineage_id"], "source-task")
        self.assertEqual(candidate["feedback_snapshot"]["exposure_count"], 1)
        self.assertEqual(
            candidate["snapshot_watermarks"], self._kwargs()["snapshot_watermarks"]
        )
        self.assertEqual(
            first["search_event"]["snapshot_watermarks"],
            self._kwargs()["snapshot_watermarks"],
        )
        for name in (
            "eligible_candidates_sha256",
            "snapshot_watermarks_sha256",
        ):
            self.assertRegex(first["search_event"][name], r"^[0-9a-f]{64}$")

        candidate_ids = [row["search_candidate_id"] for row in first["candidates"]]
        reopened = SelectionStore(self.path)
        retry_kwargs = self._kwargs()
        retry_kwargs["eligible_candidates"] = list(
            reversed(retry_kwargs["eligible_candidates"])
        )
        retry = reopened.record_search(**retry_kwargs)
        self.assertTrue(retry["idempotent"])
        self.assertEqual(
            [row["search_candidate_id"] for row in retry["candidates"]],
            candidate_ids,
        )

    def test_summary_and_jsonl_export_include_replayable_pool(self) -> None:
        search = self.store.record_search(**self._kwargs())
        summary = self.store.summary()
        self.assertEqual(summary["counts"]["search_candidates"], 2)

        destination = self.root / "selection_events.jsonl"
        export = self.store.export_jsonl(destination)
        self.assertEqual(export["record_type_counts"]["search_candidate"], 2)
        rows = [
            json.loads(line)
            for line in destination.read_text(encoding="utf-8").splitlines()
        ]
        record_types = [row["record_type"] for row in rows]
        self.assertLess(
            record_types.index("search_event"),
            record_types.index("search_candidate"),
        )
        self.assertLess(
            record_types.index("search_candidate"),
            record_types.index("search_ranking"),
        )
        exported = [
            row["record"] for row in rows if row["record_type"] == "search_candidate"
        ]
        self.assertEqual(
            {row["search_candidate_id"] for row in exported},
            {row["search_candidate_id"] for row in search["candidates"]},
        )
        self.assertTrue(all("candidate_payload" in row for row in exported))
        self.assertTrue(all("feedback_snapshot" in row for row in exported))
        self.assertTrue(all("snapshot_watermarks" in row for row in exported))

    def test_request_key_conflicts_on_pool_payload_feedback_or_watermark(self) -> None:
        self.store.record_search(**self._kwargs())
        for field in ("body", "feedback", "watermark"):
            with self.subTest(field=field):
                changed = self._kwargs()
                if field == "body":
                    changed["eligible_candidates"][0]["body"] = "different proof"
                elif field == "feedback":
                    changed["eligible_candidates"][0]["feedback"][
                        "exposure_count"
                    ] = 99
                else:
                    changed["snapshot_watermarks"]["selection"][
                        "feedback_event_rowid"
                    ] = 99
                with self.assertRaises(RequestKeyConflictError) as raised:
                    self.store.record_search(**changed)
                expected = (
                    "snapshot_watermarks"
                    if field == "watermark"
                    else "eligible_candidates"
                )
                self.assertIn(expected, raised.exception.mismatched_fields)

    def test_pool_contract_rejects_partial_or_unreplayable_inputs(self) -> None:
        missing_watermark = self._kwargs("missing-watermark")
        missing_watermark.pop("snapshot_watermarks")
        with self.assertRaisesRegex(ValueError, "supplied together"):
            self.store.record_search(**missing_watermark)

        missing_candidate = self._kwargs("missing-candidate")
        missing_candidate["eligible_candidates"] = [
            missing_candidate["eligible_candidates"][0]
        ]
        with self.assertRaisesRegex(ValueError, "absent from eligible_candidates"):
            self.store.record_search(**missing_candidate)

        duplicate = self._kwargs("duplicate-candidate")
        duplicate["eligible_candidates"].append(
            dict(duplicate["eligible_candidates"][0])
        )
        with self.assertRaisesRegex(ValueError, "must be unique"):
            self.store.record_search(**duplicate)


if __name__ == "__main__":
    unittest.main()
