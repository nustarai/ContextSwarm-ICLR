from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from contextswarm_mini.context_piece import _parser
from contextswarm_mini.cps import CPSStore


def _count_rows(store: CPSStore, table: str) -> int:
    if table not in {"pieces", "messages", "events"}:
        raise ValueError("unexpected test table")
    with sqlite3.connect(store.path) as db:
        return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class CPSCommitCapabilityTests(unittest.TestCase):
    def test_inbox_is_task_local_unless_global_scope_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CPSStore(Path(temporary) / "cps.sqlite3")
            global_message = store.send_message(
                task_id="__global__",
                sender="global-agent",
                recipient=None,
                body="global broadcast",
            )
            self.assertEqual(
                store.inbox(task_id="task-a", recipient="worker", limit=8),
                [],
            )
            visible = store.inbox(
                task_id="task-a",
                recipient="worker",
                limit=8,
                include_global=True,
            )
            self.assertEqual([row["id"] for row in visible], [global_message["id"]])

    def test_context_piece_hides_global_flags_without_hybrid_capability(self) -> None:
        for enabled, expected in (("0", False), ("1", True)):
            with self.subTest(enabled=enabled), patch.dict(
                "os.environ", {"CONTEXTSWARM_CPS_GLOBAL_SCOPE": enabled}, clear=False
            ):
                choices = _parser()._subparsers._group_actions[0].choices
                for command in ("search", "create"):
                    self.assertEqual(
                        any(
                            "--global" in action.option_strings
                            for action in choices[command]._actions
                        ),
                        expected,
                    )

    def test_every_write_rolls_back_when_revoked_before_commit(self) -> None:
        for operation in ("record_event", "create_piece", "send_message", "ack_message"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                store = CPSStore(Path(temporary) / "cps.sqlite3")
                message_id: str | None = None
                if operation == "ack_message":
                    message_id = str(
                        store.send_message(
                            task_id="task",
                            sender="sender",
                            recipient="recipient",
                            body="before revocation",
                        )["id"]
                    )
                baseline = {
                    table: _count_rows(store, table)
                    for table in ("pieces", "messages", "events")
                }
                revoked = threading.Event()
                original_insert_event = store._insert_event  # noqa: SLF001

                def revoke_after_mutation(
                    db: sqlite3.Connection,
                    event_type: str,
                    *,
                    task_id: str | None,
                    actor_id: str | None,
                    payload: dict[str, object] | None,
                ) -> str:
                    event_id = original_insert_event(
                        db,
                        event_type,
                        task_id=task_id,
                        actor_id=actor_id,
                        payload=payload,
                    )
                    revoked.set()
                    return event_id

                with patch.object(
                    store,
                    "_insert_event",
                    side_effect=revoke_after_mutation,
                ), self.assertRaisesRegex(RuntimeError, "revoked"):
                    if operation == "record_event":
                        store.record_event(
                            "test_event",
                            task_id="task",
                            actor_id="actor",
                            cancel_guard=revoked.is_set,
                        )
                    elif operation == "create_piece":
                        store.create_piece(
                            task_id="task",
                            author="actor",
                            kind="lemma",
                            title="rolled back",
                            body="must not persist",
                            cancel_guard=revoked.is_set,
                        )
                    elif operation == "send_message":
                        store.send_message(
                            task_id="task",
                            sender="actor",
                            recipient="peer",
                            body="must not persist",
                            cancel_guard=revoked.is_set,
                        )
                    else:
                        assert message_id is not None
                        store.ack_message(
                            message_id,
                            "recipient",
                            cancel_guard=revoked.is_set,
                        )

                self.assertTrue(revoked.is_set())
                self.assertEqual(
                    {
                        table: _count_rows(store, table)
                        for table in ("pieces", "messages", "events")
                    },
                    baseline,
                )
                if message_id is not None:
                    visible = store.inbox(
                        task_id="task",
                        recipient="recipient",
                        limit=8,
                    )
                    self.assertEqual([item["id"] for item in visible], [message_id])

    def test_write_rolls_back_when_horizon_crosses_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CPSStore(Path(temporary) / "cps.sqlite3")
            deadline_epoch_ms = int(time.time() * 1_000) + 300
            original_insert_event = store._insert_event  # noqa: SLF001
            mutation_started = threading.Event()

            def wait_past_horizon(
                db: sqlite3.Connection,
                event_type: str,
                *,
                task_id: str | None,
                actor_id: str | None,
                payload: dict[str, object] | None,
            ) -> str:
                event_id = original_insert_event(
                    db,
                    event_type,
                    task_id=task_id,
                    actor_id=actor_id,
                    payload=payload,
                )
                mutation_started.set()
                remaining = deadline_epoch_ms / 1_000 - time.time()
                if remaining > 0:
                    time.sleep(remaining + 0.03)
                return event_id

            with patch.object(
                store,
                "_insert_event",
                side_effect=wait_past_horizon,
            ), self.assertRaisesRegex(RuntimeError, "horizon"):
                store.create_piece(
                    task_id="task",
                    author="actor",
                    kind="lemma",
                    title="too late",
                    body="must not persist",
                    deadline_epoch_ms=deadline_epoch_ms,
                )

            self.assertTrue(mutation_started.is_set())
            self.assertEqual(_count_rows(store, "pieces"), 0)
            self.assertEqual(_count_rows(store, "events"), 0)


if __name__ == "__main__":
    unittest.main()
