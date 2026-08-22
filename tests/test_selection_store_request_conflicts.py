from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier
import unittest

from contextswarm_mini.selection_store import (
    RequestKeyConflictError,
    SelectionStore,
)


class SelectionStoreRequestConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SelectionStore(Path(self.temporary.name) / "selection.sqlite3")
        self.config = self.store.register_selector_config(
            selector_name="Nu Stigmergy", config={"seed": 7, "kappa": 1.0}
        )
        self.other_config = self.store.register_selector_config(
            selector_name="Nu Stigmergy", config={"seed": 8, "kappa": 1.0}
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _kwargs(self, request_key: str = "request-1") -> dict:
        return {
            "request_key": request_key,
            "task_id": "task-1",
            "actor_id": "worker-1",
            "selector_config_id": self.config["selector_config_id"],
            "query": {"text": "lemma", "filters": ["project"]},
            "comparison_identity": {"contract": "fixed-v1"},
            "snapshot_identity": {"watermark": 11},
            "pool_identity": {"pool": "project", "sha": "pool-11"},
            "rankings": [
                {
                    "trace_id": "trace-1",
                    "rank": 1,
                    "selected": True,
                    "component_scores": {"relevance": 2.0},
                    "payload": {"source": "cps"},
                },
                {
                    "trace_id": "trace-2",
                    "rank": 2,
                    "selected": False,
                    "component_scores": {"relevance": 1.0},
                },
            ],
        }

    def _record(self, **overrides):
        kwargs = self._kwargs(overrides.pop("request_key", "request-1"))
        kwargs.update(overrides)
        return self.store.record_search(**kwargs)

    def _feedback_kwargs(self, request_key: str = "feedback-1") -> dict:
        item = self._record(request_key="feedback-search")["items"][0]
        return {
            "request_key": request_key,
            "exposure_item_id": item["exposure_item_id"],
            "actor_id": "worker-1",
            "trace_id": "trace-1",
            "feedback_kind": "useful",
            "origin": "worker-tool",
            "terminal": True,
            "payload": {"value": 1, "note": "accepted"},
        }

    def test_same_canonical_request_retries_idempotently(self) -> None:
        first_kwargs = self._kwargs("canonical-retry")
        # Input order and mapping order are not part of the canonical identity.
        first_kwargs["rankings"] = list(reversed(first_kwargs["rankings"]))
        first_kwargs["query"] = {"filters": ["project"], "text": "lemma"}
        first = self.store.record_search(**first_kwargs)
        retry = self._record(request_key="canonical-retry")
        self.assertTrue(retry["idempotent"])
        self.assertEqual(
            first["search_event"]["search_event_id"], retry["search_event"]["search_event_id"]
        )
        self.assertEqual(first["items"], retry["items"])

    def test_changed_identity_fails_closed_and_preserves_original_chain(self) -> None:
        variants = {
            "task_id": "task-2",
            "actor_id": "worker-2",
            "selector_config_id": self.other_config["selector_config_id"],
            "query": {"text": "different"},
            "comparison_identity": {"contract": "changed"},
            "snapshot_identity": {"watermark": 12},
            "pool_identity": {"pool": "other"},
            "search_identity": {"search": "changed"},
            "rankings": [
                {
                    "trace_id": "trace-1",
                    "rank": 1,
                    "selected": True,
                    "component_scores": {"relevance": 9.0},
                    "payload": {"source": "cps"},
                },
                {
                    "trace_id": "trace-2",
                    "rank": 2,
                    "selected": False,
                    "component_scores": {"relevance": 1.0},
                },
            ],
        }
        for field, value in variants.items():
            with self.subTest(field=field):
                request_key = f"conflict-{field}"
                original = self._record(request_key=request_key)
                with self.assertRaises(RequestKeyConflictError) as raised:
                    self._record(request_key=request_key, **{field: value})
                self.assertIn("REQUEST_KEY_CONFLICT", str(raised.exception))
                expected_field = {
                    "selector_config_id": "config_identity",
                    "rankings": "rankings_identity",
                }.get(field, field)
                self.assertIn(expected_field, raised.exception.mismatched_fields)
                retained = self.store.get_search_by_request_key(request_key)
                self.assertIsNotNone(retained)
                self.assertEqual(
                    retained["search_event"]["search_event_id"],
                    original["search_event"]["search_event_id"],
                )

    def test_concurrent_different_retries_have_one_winner(self) -> None:
        barrier = Barrier(2)

        def attempt(query: str):
            kwargs = self._kwargs("concurrent-conflict")
            kwargs["query"] = {"text": query}
            barrier.wait()
            try:
                return ("ok", self.store.record_search(**kwargs))
            except RequestKeyConflictError as exc:
                return ("conflict", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("left", "right")))
        self.assertEqual([kind for kind, _ in results].count("ok"), 1)
        self.assertEqual([kind for kind, _ in results].count("conflict"), 1)
        conflict = next(value for kind, value in results if kind == "conflict")
        self.assertIn("query", conflict.mismatched_fields)
        retained = self.store.get_search_by_request_key("concurrent-conflict")
        self.assertIsNotNone(retained)
        self.assertIn(retained["search_event"]["query"]["text"], {"left", "right"})

    def test_concurrent_identical_retries_share_one_chain(self) -> None:
        barrier = Barrier(8)

        def attempt(_: int):
            barrier.wait()
            return self._record(request_key="concurrent-retry")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(not result["idempotent"] for result in results), 1)
        self.assertEqual(sum(result["idempotent"] for result in results), 7)
        self.assertEqual(
            len({result["search_event"]["search_event_id"] for result in results}), 1
        )

    def test_feedback_same_canonical_request_is_idempotent(self) -> None:
        first_kwargs = self._feedback_kwargs("feedback-canonical")
        first = self.store.record_feedback(**first_kwargs)
        retry_kwargs = dict(first_kwargs)
        retry_kwargs["payload"] = {"note": "accepted", "value": 1}
        retry = self.store.record_feedback(**retry_kwargs)
        self.assertTrue(retry["idempotent"])
        self.assertEqual(first["feedback_event_id"], retry["feedback_event_id"])
        self.assertEqual(first["payload"], retry["payload"])

    def test_feedback_changed_identity_fails_closed(self) -> None:
        original = self._feedback_kwargs("feedback-field-base")
        self.store.record_feedback(**original)
        fields = {
            "exposure_item_id": "exposure-item-other",
            "actor_id": "worker-other",
            "trace_id": "trace-other",
            "feedback_kind": "not_useful",
            "origin": "other-origin",
            "terminal": False,
            "payload": {"value": 0, "note": "changed"},
        }
        for field, value in fields.items():
            with self.subTest(field=field):
                kwargs = dict(original)
                kwargs["request_key"] = f"feedback-conflict-{field}"
                self.store.record_feedback(**kwargs)
                kwargs[field] = value
                with self.assertRaises(RequestKeyConflictError) as raised:
                    self.store.record_feedback(**kwargs)
                self.assertIn("REQUEST_KEY_CONFLICT", str(raised.exception))
                expected = "payload" if field == "payload" else field
                self.assertIn(expected, raised.exception.mismatched_fields)

    def test_feedback_concurrent_identical_retries_share_one_event(self) -> None:
        kwargs = self._feedback_kwargs("feedback-concurrent")
        barrier = Barrier(8)

        def attempt(_: int):
            barrier.wait()
            return self.store.record_feedback(**kwargs)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(not result["idempotent"] for result in results), 1)
        self.assertEqual(sum(result["idempotent"] for result in results), 7)
        self.assertEqual(
            len({result["feedback_event_id"] for result in results}), 1
        )

    def test_feedback_concurrent_different_retries_fail_closed(self) -> None:
        barrier = Barrier(2)

        def attempt(kind: str):
            kwargs = self._feedback_kwargs("feedback-concurrent-conflict")
            kwargs["feedback_kind"] = kind
            barrier.wait()
            try:
                return ("ok", self.store.record_feedback(**kwargs))
            except RequestKeyConflictError as exc:
                return ("conflict", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ("useful", "not_useful")))
        self.assertEqual([kind for kind, _ in results].count("ok"), 1)
        self.assertEqual([kind for kind, _ in results].count("conflict"), 1)
        conflict = next(value for kind, value in results if kind == "conflict")
        self.assertIn("feedback_kind", conflict.mismatched_fields)

    def test_auxiliary_event_retries_also_fail_closed(self) -> None:
        evidence = {
            "request_key": "evidence-conflict",
            "trace_id": "trace-1",
            "verifier_id": "judge",
            "status": "PASS",
            "evidence": {"receipt": "r1"},
            "task_id": "task-1",
        }
        self.store.record_verifier_evidence(**evidence)
        changed_evidence = dict(evidence, status="FAIL")
        with self.assertRaises(RequestKeyConflictError):
            self.store.record_verifier_evidence(**changed_evidence)

        maintenance = {
            "request_key": "maintenance-conflict",
            "trace_id": "trace-1",
            "actor_id": "worker-1",
            "maintenance_kind": "superseded",
            "payload": {"reason": "newer"},
        }
        self.store.record_maintenance_event(**maintenance)
        with self.assertRaises(RequestKeyConflictError):
            self.store.record_maintenance_event(
                **dict(maintenance, payload={"reason": "different"})
            )

        relation = {
            "request_key": "relation-conflict",
            "source_trace_id": "trace-1",
            "target_trace_id": "trace-2",
            "relation_kind": "supports",
            "actor_id": "worker-1",
            "payload": {"confidence": 0.8},
        }
        self.store.record_relation(**relation)
        with self.assertRaises(RequestKeyConflictError):
            self.store.record_relation(**dict(relation, relation_kind="refutes"))

    def test_auxiliary_events_same_canonical_retry(self) -> None:
        evidence = {
            "request_key": "evidence-idempotent",
            "trace_id": "trace-1",
            "verifier_id": "judge",
            "status": "PASS",
            "evidence": {"b": 2, "a": 1},
            "task_id": "task-1",
        }
        first_evidence = self.store.record_verifier_evidence(**evidence)
        retry_evidence = self.store.record_verifier_evidence(
            **dict(evidence, evidence={"a": 1, "b": 2})
        )
        self.assertTrue(retry_evidence["idempotent"])
        self.assertEqual(first_evidence["evidence_event_id"], retry_evidence["evidence_event_id"])

        maintenance = {
            "request_key": "maintenance-idempotent",
            "trace_id": "trace-1",
            "actor_id": "worker-1",
            "maintenance_kind": "reviewed",
            "payload": {"b": 2, "a": 1},
        }
        first_maintenance = self.store.record_maintenance_event(**maintenance)
        retry_maintenance = self.store.record_maintenance_event(
            **dict(maintenance, payload={"a": 1, "b": 2})
        )
        self.assertTrue(retry_maintenance["idempotent"])
        self.assertEqual(
            first_maintenance["maintenance_event_id"],
            retry_maintenance["maintenance_event_id"],
        )

        relation = {
            "request_key": "relation-idempotent",
            "source_trace_id": "trace-1",
            "target_trace_id": "trace-2",
            "relation_kind": "supports",
            "actor_id": "worker-1",
            "payload": {"b": 2, "a": 1},
        }
        first_relation = self.store.record_relation(**relation)
        retry_relation = self.store.record_relation(
            **dict(relation, payload={"a": 1, "b": 2})
        )
        self.assertTrue(retry_relation["idempotent"])
        self.assertEqual(first_relation["relation_id"], retry_relation["relation_id"])


if __name__ == "__main__":
    unittest.main()
