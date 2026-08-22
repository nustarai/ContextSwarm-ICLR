"""Minimal event-backed communication and context-piece store.

The store is intentionally boring: SQLite WAL plus JSON payloads.  This keeps
the experiment surface inspectable while allowing the communication policy to
be replaced without changing the agent runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable, Mapping


_WORD_RE = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]+")
_MAX_TEXT = 8_000


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clip(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _tokens(text: str) -> set[str]:
    return {item.lower() for item in _WORD_RE.findall(text)}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class CPSStore:
    """Thread/process-safe store; each operation uses a short SQLite txn."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS pieces (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS pieces_task_created
                    ON pieces(task_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS messages_inbox
                    ON messages(task_id, recipient, created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    task_id TEXT,
                    actor_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def record_event(
        self,
        event_type: str,
        *,
        task_id: str | None = None,
        actor_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> str:
        event_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute(
                "INSERT INTO events(event_id,event_type,task_id,actor_id,payload,created_at) VALUES(?,?,?,?,?,?)",
                (event_id, event_type, task_id, actor_id, _json(dict(payload or {})), utc_now()),
            )
        return event_id

    def create_piece(
        self,
        *,
        task_id: str,
        author: str,
        kind: str,
        title: str,
        body: str,
        tags: Iterable[str] = (),
    ) -> dict[str, Any]:
        piece_id = uuid.uuid4().hex
        row = {
            "id": piece_id,
            "task_id": _clip(task_id, 256),
            "author": _clip(author, 256),
            "kind": _clip(kind, 64) or "note",
            "title": _clip(title, 300) or "untitled",
            "body": _clip(body),
            "tags": sorted({_clip(tag, 64) for tag in tags if _clip(tag, 64)}),
            "created_at": utc_now(),
        }
        with self._db() as db:
            db.execute(
                "INSERT INTO pieces(id,task_id,author,kind,title,body,tags,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["task_id"],
                    row["author"],
                    row["kind"],
                    row["title"],
                    row["body"],
                    _json(row["tags"]),
                    row["created_at"],
                ),
            )
        self.record_event("piece_created", task_id=task_id, actor_id=author, payload=row)
        return row

    def search(
        self,
        *,
        task_id: str,
        query: str = "",
        limit: int = 8,
        include_global: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        with self._db() as db:
            if include_global:
                rows = db.execute(
                    """SELECT * FROM pieces
                       WHERE active=1 AND (task_id=? OR task_id='__global__')
                       ORDER BY created_at DESC LIMIT ?""",
                    (task_id, max(limit * 8, 32)),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM pieces WHERE active=1 AND task_id=? ORDER BY created_at DESC LIMIT ?",
                    (task_id, max(limit * 8, 32)),
                ).fetchall()
        wanted = _tokens(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = json.loads(item.get("tags") or "[]")
            except json.JSONDecodeError:
                item["tags"] = []
            haystack = " ".join(
                [str(item.get("title", "")), str(item.get("body", "")), " ".join(item["tags"])]
            )
            overlap = len(wanted & _tokens(haystack)) if wanted else 0
            # Newer pieces win ties; an explicit query match dominates recency.
            try:
                recency = int(str(item.get("id", ""))[-6:] or "0", 16) / 16_777_215
            except ValueError:
                recency = 0.0
            score = overlap * 10.0 + recency
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def send_message(
        self,
        *,
        task_id: str,
        sender: str,
        recipient: str | None,
        body: str,
    ) -> dict[str, Any]:
        message = {
            "id": uuid.uuid4().hex,
            "task_id": _clip(task_id, 256),
            "sender": _clip(sender, 256),
            "recipient": _clip(recipient, 256) if recipient else None,
            "body": _clip(body),
            "created_at": utc_now(),
            "acked_at": None,
        }
        with self._db() as db:
            db.execute(
                "INSERT INTO messages(id,task_id,sender,recipient,body,created_at) VALUES(?,?,?,?,?,?)",
                (
                    message["id"],
                    message["task_id"],
                    message["sender"],
                    message["recipient"],
                    message["body"],
                    message["created_at"],
                ),
            )
        self.record_event("message_sent", task_id=task_id, actor_id=sender, payload=message)
        return message

    def inbox(self, *, task_id: str, recipient: str, limit: int = 8) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        with self._db() as db:
            rows = db.execute(
                """SELECT * FROM messages
                   WHERE task_id IN (?, '__global__') AND acked_at IS NULL
                     AND (recipient IS NULL OR recipient=? OR recipient='*')
                   ORDER BY created_at DESC LIMIT ?""",
                (task_id, recipient, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def ack_message(self, message_id: str, actor_id: str) -> bool:
        now = utc_now()
        with self._db() as db:
            cursor = db.execute(
                "UPDATE messages SET acked_at=? WHERE id=? AND acked_at IS NULL",
                (now, message_id),
            )
        if cursor.rowcount:
            self.record_event("message_acked", actor_id=actor_id, payload={"id": message_id})
            return True
        return False

    def digest(
        self,
        *,
        task_id: str,
        actor_id: str,
        query: str = "",
        limit: int = 8,
        include_global: bool = False,
    ) -> dict[str, Any]:
        pieces = self.search(task_id=task_id, query=query, limit=limit, include_global=include_global)
        messages = self.inbox(task_id=task_id, recipient=actor_id, limit=limit)
        return {"pieces": pieces, "messages": messages}

    def progress_snapshot(
        self,
        task_ids: Iterable[str],
        *,
        recent_limit: int = 3,
        body_chars: int = 1_200,
    ) -> dict[str, dict[str, Any]]:
        """Return bounded per-task CPS statistics in one read transaction.

        Allocation policies use this projection instead of receiving a database
        handle.  The scheduler therefore cannot publish pieces or accidentally
        feed its own decisions back into the communication substrate.
        """
        ordered_ids = tuple(dict.fromkeys(str(task_id) for task_id in task_ids))
        recent_limit = max(1, min(int(recent_limit), 20))
        body_chars = max(1, min(int(body_chars), _MAX_TEXT))
        result: dict[str, dict[str, Any]] = {
            task_id: {
                "piece_count": 0,
                "validation_piece_count": 0,
                "strategy_piece_count": 0,
                "duplicate_piece_count": 0,
                "latest_created_at": "",
                "recent_pieces": [],
            }
            for task_id in ordered_ids
        }
        if not ordered_ids:
            return result
        placeholders = ",".join("?" for _ in ordered_ids)
        with self._db() as db:
            rows = db.execute(
                f"""SELECT rowid,id,task_id,author,kind,title,body,created_at
                    FROM pieces
                    WHERE active=1 AND task_id IN ({placeholders})
                    ORDER BY rowid DESC""",
                ordered_ids,
            ).fetchall()
        titles: dict[str, dict[str, int]] = {task_id: {} for task_id in ordered_ids}
        for raw in rows:
            item = dict(raw)
            task_id = str(item["task_id"])
            stats = result[task_id]
            stats["piece_count"] += 1
            kind = str(item.get("kind") or "")
            if kind == "validation_result" and _is_authoritative_validation_piece(item):
                stats["validation_piece_count"] += 1
            elif kind in {"proof_strategy", "strategy", "handoff", "lemma", "blocker"}:
                stats["strategy_piece_count"] += 1
            normalized_title = " ".join(str(item.get("title") or "").lower().split())
            if normalized_title and kind != "validation_result":
                titles[task_id][normalized_title] = titles[task_id].get(normalized_title, 0) + 1
            if not stats["latest_created_at"]:
                stats["latest_created_at"] = str(item.get("created_at") or "")
            if len(stats["recent_pieces"]) < recent_limit:
                body = str(item.get("body") or "")
                stats["recent_pieces"].append(
                    {
                        "piece_id": str(item.get("id") or ""),
                        "kind": kind,
                        "title": str(item.get("title") or "")[:300],
                        "body": body if len(body) <= body_chars else body[:body_chars] + "…",
                        "author": str(item.get("author") or "")[:256],
                        "created_at": str(item.get("created_at") or ""),
                    }
                )
        for task_id, counts in titles.items():
            result[task_id]["duplicate_piece_count"] = sum(
                max(0, count - 1) for count in counts.values()
            )
        return result

    def summary(self) -> dict[str, Any]:
        with self._db() as db:
            pieces = int(db.execute("SELECT COUNT(*) FROM pieces").fetchone()[0])
            messages = int(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            events = int(db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        return {"pieces": pieces, "messages": messages, "events": events, "db": self.path.name}

    def export_events(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            rows = db.execute(
                "SELECT seq,event_id,event_type,task_id,actor_id,payload,created_at FROM events ORDER BY seq"
            ).fetchall()
        with destination.open("w", encoding="utf-8") as handle:
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(item.get("payload") or "{}")
                except json.JSONDecodeError:
                    item["payload"] = {}
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def render_digest(digest: Mapping[str, Any], *, max_chars: int = 6_000) -> str:
    """Render only task-relevant content into a worker prompt."""
    lines: list[str] = []
    for item in digest.get("pieces", []):
        lines.append(
            f"[piece:{item.get('kind','note')}] {item.get('title','')}\n{item.get('body','')}"
        )
    for item in digest.get("messages", []):
        lines.append(f"[message from {item.get('sender','?')}] {item.get('body','')}")
    text = "\n\n".join(lines).strip()
    return text if len(text) <= max_chars else text[:max_chars] + "\n[context truncated]"


def _is_authoritative_validation_piece(item: Mapping[str, Any]) -> bool:
    if str(item.get("author") or "") != "runner":
        return False
    try:
        payload = json.loads(str(item.get("body") or ""))
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, Mapping):
        return False
    return all(
        isinstance(payload.get(key), str)
        and re.fullmatch(r"[0-9a-f]{64}", str(payload[key]).lower()) is not None
        for key in ("candidate_sha256", "task_contract_sha256")
    )


@dataclass
class CommunicationPolicy:
    """Policy facade used by the runner; methods are no-ops for baseline mode."""

    name: str
    store: CPSStore | None

    @property
    def enabled(self) -> bool:
        return self.store is not None and self.name != "none"

    def digest(self, task_id: str, actor_id: str, query: str = "") -> str:
        if not self.enabled:
            return ""
        assert self.store is not None
        return render_digest(
            self.store.digest(
                task_id=task_id,
                actor_id=actor_id,
                query=query,
                include_global=self.name == "hybrid",
            )
        )

    def publish(
        self,
        task_id: str,
        actor_id: str,
        *,
        title: str,
        body: str,
        kind: str = "handoff",
        tags: Iterable[str] = (),
    ) -> None:
        if not self.enabled:
            return
        assert self.store is not None
        self.store.create_piece(
            task_id=task_id,
            author=actor_id,
            kind=kind,
            title=title,
            body=body,
            tags=tags,
        )

    def send(self, task_id: str, actor_id: str, body: str, recipient: str | None = None) -> None:
        if not self.enabled or self.name == "blackboard":
            return
        assert self.store is not None
        self.store.send_message(task_id=task_id, sender=actor_id, recipient=recipient, body=body)


def make_policy(name: str, store: CPSStore | None) -> CommunicationPolicy:
    normalized = str(name or "none").strip().lower()
    if normalized == "simple":
        normalized = "blackboard"
    if normalized not in {"none", "blackboard", "direct", "hybrid"}:
        raise ValueError(f"unknown communication policy: {name}")
    return CommunicationPolicy(normalized, store if normalized != "none" else None)
