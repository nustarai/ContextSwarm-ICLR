from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from contextswarm_mini import judge_broker as judge_broker_module
from contextswarm_mini.cps import CPSStore
from contextswarm_mini.judge_broker import JudgeBroker
from contextswarm_mini.models import Task, Verdict
from contextswarm_mini.selection_store import SelectionStore


def _post(url: str, operation: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"{url}/{operation}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=3) as response:
        result = json.loads(response.read())
    assert isinstance(result, dict)
    return result


def _task(root: Path) -> Task:
    return Task(
        slug="task",
        root=root,
        problem_text="problem",
        baseline_code="import Mathlib\ntheorem task : True := by sorry\n",
        metadata={"problem_id": "Task", "theorem_name": "task"},
    )


class _TerminalEvaluator:
    def expected_task_contract_sha256(self, _task: Task) -> str:
        return "a" * 64

    def probe(
        self,
        task: Task,
        _candidate: Path,
        *,
        deadline_monotonic: float | None,
    ) -> Verdict:
        del deadline_monotonic
        return Verdict(
            task.slug,
            "VERIFY_FAIL",
            0.0,
            0.0,
            task_contract_sha256="a" * 64,
            judge_job_id="job-capability-test",
        )


class JudgeBrokerCapabilityTests(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
        *,
        broker_default: bool | None = None,
        selection_search: object = None,
    ) -> tuple[JudgeBroker, CPSStore, Path, Path]:
        workdir = root / "worker"
        workdir.mkdir()
        candidate = workdir / "result.lean"
        candidate.write_text("proof", encoding="utf-8")
        store = CPSStore(root / "cps.sqlite3")
        broker = JudgeBroker(
            _TerminalEvaluator(),
            threading.BoundedSemaphore(1),
            audit_path=root / "audit.jsonl",
            min_probe_interval_seconds=0,
            direct_messages_allowed=broker_default,
            selection_search=selection_search,
        ).start()
        return broker, store, workdir, candidate

    def test_explicit_denial_rejects_direct_operations_without_store_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(
                root, broker_default=False
            )
            roster = root / "roster.json"
            roster.write_text('[{"actor_id":"peer"}]', encoding="utf-8")
            try:
                with (
                    patch.object(store, "send_message", wraps=store.send_message) as send,
                    patch.object(store, "inbox", wraps=store.inbox) as inbox,
                    patch.object(store, "ack_message", wraps=store.ack_message) as ack,
                    patch.object(store, "search", wraps=store.search) as search,
                    patch.object(store, "create_piece", wraps=store.create_piece) as publish,
                    patch.object(
                        judge_broker_module,
                        "_safe_roster",
                        wraps=judge_broker_module._safe_roster,
                    ) as safe_roster,
                    broker.session(
                        actor_id="agent",
                        workdir=workdir,
                        candidates={"task": (_task(root), candidate)},
                        deadline_monotonic=time.monotonic() + 3,
                        cps_store=store,
                        communication="blackboard",
                        roster_path=roster,
                    ) as env,
                ):
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    direct_calls = (
                        ("cps_actors", {"unexpected": True}),
                        ("cps_send", {}),
                        ("cps_inbox", {"limit": "invalid"}),
                        ("cps_ack", {}),
                    )
                    # Authorization precedes checkpoint and payload handling.
                    for operation, payload in direct_calls:
                        denied = _post(url, operation, payload)
                        self.assertEqual(denied["status"], "CPS_CAPABILITY_DENIED")
                        self.assertFalse(denied["accepted"])
                        self.assertFalse(denied["retryable"])

                    checkpoint = _post(url, "judge_check", {})
                    self.assertEqual(checkpoint["status"], "VERIFY_FAIL")
                    published = _post(
                        url,
                        "cps_publish",
                        {"title": "shared", "body": "blackboard remains available"},
                    )
                    searched = _post(url, "cps_search", {"query": "blackboard"})

                self.assertTrue(published["ok"])
                self.assertEqual(len(searched["items"]), 1)
                send.assert_not_called()
                inbox.assert_not_called()
                ack.assert_not_called()
                safe_roster.assert_not_called()
                publish.assert_called_once()
                search.assert_called_once()
            finally:
                broker.close()

    def test_denial_at_either_scope_narrows_direct_capability(self) -> None:
        cases = (
            (True, False, "CPS_CAPABILITY_DENIED"),
            (False, True, "CPS_CAPABILITY_DENIED"),
            (None, True, None),
        )
        for broker_default, session_override, expected_status in cases:
            with self.subTest(
                broker_default=broker_default,
                session_override=session_override,
            ), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                broker, store, workdir, candidate = self._fixture(
                    root, broker_default=broker_default
                )
                try:
                    with broker.session(
                        actor_id="agent",
                        workdir=workdir,
                        candidates={"task": (_task(root), candidate)},
                        deadline_monotonic=time.monotonic() + 3,
                        cps_store=store,
                        communication="blackboard",
                        direct_messages_allowed=session_override,
                    ) as env:
                        url = env["CONTEXTSWARM_JUDGE_URL"]
                        self.assertEqual(
                            _post(url, "judge_check", {})["status"], "VERIFY_FAIL"
                        )
                        result = _post(url, "cps_send", {"body": "message"})
                finally:
                    broker.close()
                if expected_status is None:
                    self.assertTrue(result["ok"])
                else:
                    self.assertEqual(result["status"], expected_status)

    def test_unspecified_gate_preserves_legacy_direct_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root)
            try:
                with broker.session(
                    actor_id="agent",
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    self.assertEqual(
                        _post(url, "judge_check", {})["status"], "VERIFY_FAIL"
                    )
                    sent = _post(url, "cps_send", {"body": "legacy message"})
                    inbox = _post(url, "cps_inbox", {})
            finally:
                broker.close()
            self.assertTrue(sent["ok"])
            self.assertEqual(len(inbox["messages"]), 1)

    def test_blackboard_inbox_is_task_local_and_hybrid_keeps_global_broadcasts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root)
            store.send_message(
                task_id="__global__",
                sender="global-agent",
                recipient=None,
                body="global marker",
            )
            store.send_message(
                task_id="task",
                sender="task-agent",
                recipient=None,
                body="task marker",
            )
            try:
                with broker.session(
                    actor_id="agent",
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    self.assertEqual(_post(url, "judge_check", {})["status"], "VERIFY_FAIL")
                    blackboard = _post(url, "cps_inbox", {})
                with broker.session(
                    actor_id="agent",
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="hybrid",
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    self.assertEqual(_post(url, "judge_check", {})["status"], "VERIFY_FAIL")
                    hybrid = _post(url, "cps_inbox", {})
            finally:
                broker.close()

            self.assertEqual(
                {item["body"] for item in blackboard["messages"]},
                {"task marker"},
            )
            self.assertEqual(
                {item["body"] for item in hybrid["messages"]},
                {"task marker", "global marker"},
            )

    def test_selection_search_callback_runs_after_checkpoint_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls: list[tuple[str, str, int]] = []

            def selection_search(claim: object, query: str, limit: int) -> dict[str, object]:
                calls.append((str(getattr(claim, "actor_id")), query, limit))
                return {
                    "search_event_id": "search-event",
                    "exposure_id": "exposure",
                    "request_key": "request",
                    "items": [
                        {
                            "trace_id": "trace-1",
                            "title": "selected",
                            "body": "x" * 5000,
                            "nested": {"ok": True},
                        },
                        {"trace_id": "trace-2"},
                    ],
                }

            broker, store, workdir, candidate = self._fixture(
                root,
                selection_search=selection_search,
            )
            selection_store = SelectionStore(root / "selection.sqlite3")
            try:
                with broker.session(
                    actor_id="selection-agent",
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=store,
                    communication="blackboard",
                    selection_store=selection_store,
                    selection_enabled=True,
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    before_checkpoint = _post(url, "cps_search", {"query": "early"})
                    self.assertEqual(before_checkpoint["status"], "JUDGE_CHECK_REQUIRED")
                    self.assertEqual(calls, [])
                    self.assertEqual(
                        _post(url, "judge_check", {})["status"], "VERIFY_FAIL"
                    )
                    selected = _post(url, "cps_search", {"query": "lemma", "limit": 1})
                    denied = _post(url, "cps_send", {"body": "direct blocked"})
            finally:
                broker.close()

            self.assertEqual(calls, [("selection-agent", "lemma", 1)])
            self.assertTrue(selected["ok"])
            self.assertEqual(selected["search_event_id"], "search-event")
            self.assertEqual(len(selected["items"]), 1)
            self.assertEqual(len(selected["items"][0]["body"]), 2_000)  # type: ignore[index]
            self.assertEqual(denied["status"], "CPS_CAPABILITY_DENIED")

    def test_selection_session_rejects_every_non_blackboard_communication_surface(self) -> None:
        for communication in ("none", "direct", "hybrid", "simple"):
            with self.subTest(communication=communication), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                broker, store, workdir, candidate = self._fixture(root)
                selection_store = SelectionStore(root / "selection.sqlite3")
                try:
                    with self.assertRaisesRegex(
                        ValueError,
                        "selection-enabled broker sessions require communication = blackboard",
                    ):
                        with broker.session(
                            actor_id="selection-agent",
                            workdir=workdir,
                            candidates={"task": (_task(root), candidate)},
                            deadline_monotonic=time.monotonic() + 3,
                            cps_store=store,
                            communication=communication,
                            # A contradictory allow cannot widen the server
                            # capability even before a token is issued.
                            direct_messages_allowed=True,
                            selection_store=selection_store,
                            selection_enabled=True,
                        ):
                            self.fail("invalid selection session issued a capability")
                finally:
                    broker.close()

    def test_selection_blackboard_denies_global_publish_but_uses_shared_search_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls: list[tuple[str, str, int]] = []

            def selection_search(claim: object, query: str, limit: int) -> dict[str, object]:
                calls.append((str(getattr(claim, "actor_id")), query, limit))
                # The controlled runtime owns project-shared visibility; this
                # item deliberately originates outside the bound task.
                return {
                    "items": [
                        {
                            "trace_id": "peer-trace",
                            "task_id": "peer-task",
                            "body": "shared evidence",
                        }
                    ]
                }

            broker, store, workdir, candidate = self._fixture(
                root,
                selection_search=selection_search,
            )
            selection_store = SelectionStore(root / "selection.sqlite3")
            try:
                with (
                    patch.object(store, "search", wraps=store.search) as legacy_search,
                    patch.object(store, "create_piece", wraps=store.create_piece) as publish,
                    broker.session(
                        actor_id="selection-agent",
                        workdir=workdir,
                        candidates={"task": (_task(root), candidate)},
                        deadline_monotonic=time.monotonic() + 3,
                        cps_store=store,
                        communication="blackboard",
                        # Selection must narrow this contradictory hint.
                        direct_messages_allowed=True,
                        selection_store=selection_store,
                        selection_enabled=True,
                    ) as env,
                ):
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    self.assertEqual(
                        _post(url, "judge_check", {})["status"], "VERIFY_FAIL"
                    )
                    selected = _post(url, "cps_search", {"query": "peer", "limit": 1})
                    global_publish = _post(
                        url,
                        "cps_publish",
                        {
                            "title": "escape",
                            "body": "must remain task-scoped",
                            "scope": "global",
                        },
                    )
                    task_publish = _post(
                        url,
                        "cps_publish",
                        {"title": "local", "body": "eligible shared trace"},
                    )
                    direct = _post(url, "cps_send", {"body": "must be denied"})

                self.assertEqual(calls, [("selection-agent", "peer", 1)])
                self.assertEqual(selected["items"][0]["task_id"], "peer-task")
                # Scope parsing is inside the broker boundary; invalid global
                # scope is rejected without reflecting implementation detail.
                self.assertEqual(global_publish["status"], "BROKER_ERROR")
                self.assertTrue(task_publish["ok"])
                self.assertEqual(direct["status"], "CPS_CAPABILITY_DENIED")
                legacy_search.assert_not_called()
                publish.assert_called_once()
                self.assertEqual(publish.call_args.kwargs["task_id"], "task")
            finally:
                broker.close()

    def test_disabled_selection_denies_feedback_before_payload_or_store_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, store, workdir, candidate = self._fixture(root)
            selection_store = SelectionStore(root / "selection.sqlite3")
            try:
                with (
                    patch.object(
                        selection_store,
                        "record_feedback",
                        wraps=selection_store.record_feedback,
                    ) as record_feedback,
                    broker.session(
                        actor_id="agent",
                        workdir=workdir,
                        candidates={"task": (_task(root), candidate)},
                        deadline_monotonic=time.monotonic() + 3,
                        cps_store=store,
                        communication="blackboard",
                        selection_store=selection_store,
                        selection_enabled=False,
                    ) as env,
                ):
                    denied = _post(
                        env["CONTEXTSWARM_JUDGE_URL"],
                        "cps_feedback",
                        {"unexpected": True},
                    )
                self.assertEqual(denied["status"], "CPS_CAPABILITY_DENIED")
                record_feedback.assert_not_called()
            finally:
                broker.close()

    def test_feedback_binds_actor_origin_and_selected_exposure_server_side(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "worker"
            workdir.mkdir()
            candidate = workdir / "result.lean"
            candidate.write_text("proof", encoding="utf-8")
            cps_store = CPSStore(root / "cps.sqlite3")
            selection_store = SelectionStore(root / "selection.sqlite3")
            selector = selection_store.register_selector_config(
                selector_name="uniform_random",
                config={"seed": 7},
            )
            search = selection_store.record_search(
                request_key="search-1",
                task_id="task",
                actor_id="agent",
                selector_config_id=str(selector["selector_config_id"]),
                query={"query": "lemma"},
                comparison_identity="comparison",
                snapshot_identity="snapshot",
                pool_identity="pool",
                rankings=[
                    {
                        "trace_id": "trace-1",
                        "rank": 1,
                        "selected": True,
                        "component_scores": {"score": 1.0},
                        "payload": {},
                    }
                ],
            )
            exposure_item_id = str(search["items"][0]["exposure_item_id"])
            broker = JudgeBroker(
                _TerminalEvaluator(),
                threading.BoundedSemaphore(1),
                audit_path=root / "audit.jsonl",
                min_probe_interval_seconds=0,
                selection_store=selection_store,
                selection_enabled=True,
            ).start()
            try:
                with broker.session(
                    actor_id="agent",
                    workdir=workdir,
                    candidates={"task": (_task(root), candidate)},
                    deadline_monotonic=time.monotonic() + 3,
                    cps_store=cps_store,
                    communication="blackboard",
                ) as env:
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    self.assertEqual(
                        _post(url, "judge_check", {})["status"], "VERIFY_FAIL"
                    )
                    payload = {
                        "request_key": "feedback-1",
                        "exposure_item_id": exposure_item_id,
                        "trace_id": "trace-1",
                        "feedback_kind": "useful",
                        "value": 0.75,
                        "note": "helped close a subgoal",
                    }
                    first = _post(url, "cps_feedback", payload)
                    repeated = _post(url, "cps_feedback", payload)
                    losing = _post(
                        url,
                        "cps_feedback",
                        {
                            **payload,
                            "request_key": "feedback-2",
                            "feedback_kind": "not_useful",
                        },
                    )
            finally:
                broker.close()

            self.assertEqual(first["status"], "RECORDED")
            self.assertTrue(first["effective"])
            self.assertFalse(first["idempotent"])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(losing["status"], "ALREADY_FINAL")
            self.assertFalse(losing["effective"])
            chain = selection_store.attribution_chain(exposure_item_id)
            assert chain is not None
            feedback = chain["feedback_events"]
            self.assertEqual(feedback[0]["actor_id"], "agent")
            self.assertEqual(feedback[0]["origin"], "worker_explicit")
            self.assertEqual(
                feedback[0]["payload"],
                {"value": 0.75, "note": "helped close a subgoal"},
            )

    def test_selection_feedback_rejects_noncanonical_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker, cps_store, workdir, candidate = self._fixture(root)
            selection_store = SelectionStore(root / "selection.sqlite3")
            try:
                with (
                    patch.object(
                        selection_store,
                        "record_feedback",
                        wraps=selection_store.record_feedback,
                    ) as record_feedback,
                    broker.session(
                        actor_id="agent",
                        workdir=workdir,
                        candidates={"task": (_task(root), candidate)},
                        deadline_monotonic=time.monotonic() + 3,
                        cps_store=cps_store,
                        communication="blackboard",
                        selection_store=selection_store,
                        selection_enabled=True,
                    ) as env,
                ):
                    url = env["CONTEXTSWARM_JUDGE_URL"]
                    self.assertEqual(
                        _post(url, "judge_check", {})["status"], "VERIFY_FAIL"
                    )
                    invalid = _post(
                        url,
                        "cps_feedback",
                        {
                            "request_key": "feedback-invalid",
                            "exposure_item_id": "exposure-item",
                            "trace_id": "trace-1",
                            "feedback_kind": "free_form_label",
                        },
                    )
                self.assertEqual(invalid["status"], "INVALID_REQUEST")
                record_feedback.assert_not_called()
            finally:
                broker.close()


if __name__ == "__main__":
    unittest.main()
