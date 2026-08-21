"""Worker-facing CPS command line surface.

The runner places a tiny executable named ``context_piece`` in CPS workspaces;
it forwards here with the run-local store and actor identity in environment
variables.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .cps import CPSStore


def _runtime() -> tuple[CPSStore, str, str]:
    db = os.environ.get("CONTEXTSWARM_CPS_DB", "").strip()
    task = os.environ.get("CONTEXTSWARM_TASK_ID", "").strip()
    actor = os.environ.get("CONTEXTSWARM_ACTOR_ID", "").strip()
    if not db or not task or not actor:
        raise SystemExit("context_piece requires CONTEXTSWARM_CPS_DB, CONTEXTSWARM_TASK_ID, and CONTEXTSWARM_ACTOR_ID")
    return CPSStore(Path(db)), task, actor


def _actors() -> list[dict[str, object]]:
    raw = os.environ.get("CONTEXTSWARM_ACTORS_FILE", "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="context_piece")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search", help="search shared context pieces")
    search.add_argument("--query", default="")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--global", action="store_true", dest="include_global")
    create = sub.add_parser("create", help="publish a typed context piece")
    create.add_argument("--kind", default="handoff")
    create.add_argument("--title", required=True)
    create.add_argument("--body")
    create.add_argument("--body-file")
    create.add_argument("--tag", action="append", default=[])
    create.add_argument("--global", action="store_true", dest="is_global")
    message = sub.add_parser("message", help="send/read/ack direct messages")
    message_sub = message.add_subparsers(dest="message_command", required=True)
    send = message_sub.add_parser("send")
    send.add_argument("--to", default=None)
    send.add_argument("--body", required=True)
    send.add_argument("--global", action="store_true", dest="is_global")
    inbox = message_sub.add_parser("inbox")
    inbox.add_argument("--limit", type=int, default=8)
    ack = message_sub.add_parser("ack")
    ack.add_argument("message_id")
    actor = sub.add_parser("actor", help="inspect the bounded actor roster")
    actor_sub = actor.add_subparsers(dest="actor_command", required=True)
    actor_sub.add_parser("list")
    actor_search = actor_sub.add_parser("search")
    actor_search.add_argument("query")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        store, task, actor = _runtime()
        if args.command == "actor":
            roster = _actors()
            if args.actor_command == "search":
                query = args.query.lower()
                roster = [item for item in roster if query in json.dumps(item, ensure_ascii=False).lower()]
            result = roster
        elif args.command == "search":
            result = store.search(
                task_id=task,
                query=args.query,
                limit=args.limit,
                include_global=args.include_global,
            )
        elif args.command == "create":
            body = args.body
            if args.body_file:
                body = Path(args.body_file).read_text(encoding="utf-8")
            if not body:
                raise ValueError("create requires --body or --body-file")
            result = store.create_piece(
                task_id="__global__" if args.is_global else task,
                author=actor,
                kind=args.kind,
                title=args.title,
                body=body,
                tags=args.tag,
            )
        elif args.command == "message":
            if args.message_command == "send":
                result = store.send_message(
                    task_id="__global__" if args.is_global else task,
                    sender=actor,
                    recipient=args.to,
                    body=args.body,
                )
            elif args.message_command == "inbox":
                result = store.inbox(task_id=task, recipient=actor, limit=args.limit)
            else:
                result = {"acked": store.ack_message(args.message_id, actor)}
        else:  # pragma: no cover - argparse enforces this branch away.
            raise ValueError(f"unknown command {args.command}")
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
