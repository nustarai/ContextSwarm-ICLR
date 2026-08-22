"""Worker-facing formal tool shims and the trusted declaration-index reader."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from typing import Any, Iterable, Mapping

from .artifacts import atomic_write_json, atomic_write_text


TOOL_CAPABILITY_FILENAME = ".contextswarm_tool_capability.json"
TOOL_CLIENT_FILENAME = "_contextswarm_tool_client.py"
EVALUATE_FILENAME = "evaluate.py"
FORMAL_QUERY_FILENAME = "formal_query"
PUBLIC_FILES_FILENAME = "PUBLIC_FILES.md"

_INDEX_SCHEMA = "decl_index_v1"
_PRIVATE_TEXT = re.compile(
    r"https?://\S+|/(?:home|tmp|workspace|opt|mnt|var|root|scratch)/[^\s,;:)\]}\"']*|"
    r"(?i:\bBearer\s+[A-Za-z0-9._~+\-/=]+)",
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class ToolCapability:
    socket_path: str
    token: str
    task_id: str
    surface_version: str


@dataclass(frozen=True)
class DeclarationIndexInfo:
    configured: bool
    available: bool
    compatible: bool
    sha256: str | None
    mathlib_revision: str | None
    lean_toolchain: str | None
    schema: str | None
    declaration_count: int | None
    error: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "available": self.available,
            "compatible": self.compatible,
            "sha256": self.sha256,
            "mathlib_revision": self.mathlib_revision,
            "lean_toolchain": self.lean_toolchain,
            "schema": self.schema,
            "declaration_count": self.declaration_count,
            "error": self.error,
        }


@dataclass(frozen=True)
class _DeclRecord:
    name: str
    kind: str
    file: str
    line: int
    head: str
    snippet: str


class DeclarationIndex:
    """Read-only, revision-bound Mathlib declaration search."""

    def __init__(
        self,
        path: Path | None,
        *,
        expected_sha256: str = "",
        expected_revision: str = "",
    ) -> None:
        self.path = path
        self.expected_sha256 = expected_sha256.strip().lower()
        self.expected_revision = expected_revision.strip()
        self._lock = threading.RLock()
        self._records: tuple[_DeclRecord, ...] | None = None
        self.info = inspect_declaration_index(
            path,
            expected_sha256=self.expected_sha256,
            expected_revision=self.expected_revision,
        )

    def search(
        self,
        query: str,
        *,
        limit: int,
        guarded_names: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        if not self.info.compatible or self.path is None:
            return []
        records = self._load_records()
        guarded = {name for name in guarded_names if name}
        guarded_files = {record.file for record in records if record.name in guarded}
        raw_query = query.strip().lower()
        query_tokens = {match.group(0).lower() for match in _TOKEN_RE.finditer(query)}
        scored: list[tuple[float, _DeclRecord]] = []
        for record in records:
            if record.file in guarded_files or _unsafe_index_path(record.file):
                continue
            name = record.name.lower()
            snippet = record.snippet.lower()
            filename = record.file.lower()
            score = 0.0
            if raw_query and raw_query in name:
                score += 8.0
            for token in query_tokens:
                if token in name:
                    score += 3.0
                if token in snippet:
                    score += 1.5
                if token in filename:
                    score += 0.5
            if not query_tokens:
                score = 1.0
            if record.name.startswith("_"):
                score -= 2.0
            if score > 0:
                scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [
            {
                "name": record.name,
                "kind": record.kind,
                "file": record.file,
                "line": record.line,
                "statement_snippet": sanitize_public_text(record.snippet, limit=600),
                "score_bucket": "high" if score >= 8 else "medium" if score >= 3 else "low",
                "recommended_check": f"./formal_query check {record.name}",
            }
            for score, record in scored[: max(1, min(int(limit), 24))]
        ]

    def _load_records(self) -> tuple[_DeclRecord, ...]:
        with self._lock:
            if self._records is not None:
                return self._records
            assert self.path is not None
            connection = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True)
            try:
                rows = connection.execute(
                    "SELECT name, kind, file, line, head, snippet FROM decls"
                ).fetchall()
            finally:
                connection.close()
            self._records = tuple(
                _DeclRecord(
                    name=str(row[0]),
                    kind=str(row[1]),
                    file=str(row[2]),
                    line=int(row[3]),
                    head=str(row[4]),
                    snippet=str(row[5]),
                )
                for row in rows
            )
            return self._records


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_declaration_index(
    path: Path | None,
    *,
    expected_sha256: str = "",
    expected_revision: str = "",
) -> DeclarationIndexInfo:
    if path is None:
        return DeclarationIndexInfo(False, False, False, None, None, None, None, None, "not configured")
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise OSError("declaration index must be a regular non-symlink file")
        digest = _sha256(path)
        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            meta = {str(key): str(value) for key, value in connection.execute("SELECT key, value FROM meta")}
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(decls)")
                if len(row) > 1
            }
            row = connection.execute("SELECT COUNT(*) FROM decls").fetchone()
        finally:
            connection.close()
        schema = meta.get("schema")
        revision = meta.get("mathlib_revision")
        toolchain = meta.get("lean_toolchain")
        declaration_count = int(row[0]) if row else None
        compatible = schema == _INDEX_SCHEMA and {
            "name",
            "kind",
            "file",
            "line",
            "head",
            "snippet",
        }.issubset(columns)
        if expected_sha256:
            compatible = compatible and digest == expected_sha256.lower()
        if expected_revision:
            compatible = compatible and revision == expected_revision
        error = None if compatible else "index schema, revision, or SHA-256 contract mismatch"
        return DeclarationIndexInfo(
            True,
            True,
            compatible,
            digest,
            revision,
            toolchain,
            schema,
            declaration_count,
            error,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return DeclarationIndexInfo(True, False, False, None, None, None, None, None, type(exc).__name__)


def sanitize_public_text(value: str, *, limit: int = 2_048) -> str:
    return _PRIVATE_TEXT.sub("[redacted]", str(value or ""))[: max(1, int(limit))]


def tool_surface_provenance(surface_version: str, *, guard_path: Path | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "surface_version": surface_version,
        "broker_client_sha256": hashlib.sha256(_CLIENT_SCRIPT.encode("utf-8")).hexdigest(),
        "evaluate_shim_sha256": hashlib.sha256(_EVALUATE_SCRIPT.encode("utf-8")).hexdigest(),
        "formal_query_shim_sha256": hashlib.sha256(_FORMAL_QUERY_SCRIPT.encode("utf-8")).hexdigest(),
    }
    if guard_path is not None and guard_path.is_file():
        try:
            payload["pi_worker_guard_sha256"] = _sha256(guard_path)
        except OSError:
            payload["pi_worker_guard_sha256"] = "unavailable"
    return payload


def _unsafe_index_path(value: str) -> bool:
    path = str(value or "").replace("\\", "/")
    lowered = path.lower()
    return path.startswith("/") or ".." in Path(path).parts or any(
        marker in lowered for marker in ("solution", "answer", "private", "workspace", "scratch")
    )


def stage_worker_tools(
    destination: Path,
    *,
    capability: ToolCapability,
    baseline_names: Iterable[str],
    context_piece_enabled: bool,
) -> None:
    """Stage identical manifest-selected shims for Mono, Parallel, and CPS."""

    destination.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        destination / TOOL_CAPABILITY_FILENAME,
        {
            "schema_version": "contextswarm_mini_tool_capability_v1",
            "socket_path": capability.socket_path,
            "token": capability.token,
            "task_id": capability.task_id,
            "surface_version": capability.surface_version,
        },
        mode=0o400,
    )
    atomic_write_text(destination / TOOL_CLIENT_FILENAME, _CLIENT_SCRIPT, mode=0o500)
    atomic_write_text(destination / EVALUATE_FILENAME, _EVALUATE_SCRIPT, mode=0o500)
    atomic_write_text(destination / FORMAL_QUERY_FILENAME, _FORMAL_QUERY_SCRIPT, mode=0o500)
    atomic_write_text(
        destination / PUBLIC_FILES_FILENAME,
        public_files_manifest(
            baseline_names=baseline_names,
            context_piece_enabled=context_piece_enabled,
        ),
        mode=0o444,
    )


def public_files_manifest(
    *,
    baseline_names: Iterable[str],
    context_piece_enabled: bool,
) -> str:
    files = [
        "problem.md",
        "metadata.json",
        PUBLIC_FILES_FILENAME,
        "result.lean",
        "scratch/",
        *(f"baseline/{name}" for name in sorted(set(baseline_names))),
        EVALUATE_FILENAME,
        FORMAL_QUERY_FILENAME,
    ]
    if context_piece_enabled:
        files.append("context_piece")
    lines = [
        "# Public Formal Worker Files",
        "",
        "This is the complete public task surface. Read these paths directly; broad root scans are blocked.",
        "",
        "## Files",
        "",
        *(f"- `{name}`" for name in files),
        "",
        "## Formal capabilities",
        "",
        "- `python3 evaluate.py` checks the current `result.lean` and returns bounded Lean diagnostics. Its result is agent-local feedback, never the official score.",
        "- `./formal_query --help` describes bounded `search`, `decl`, `check`, `type`, `axioms`, and `deps` queries.",
        "- `deps` returns index-related candidate premises, not a dependency graph. Verify names with `check`.",
        "- The final score comes only from the feedback-free outer evaluation of an immutable candidate snapshot.",
        "",
        "The executable helper sources and capability metadata are private boundaries; execute them but do not inspect them.",
        "",
    ]
    return "\n".join(lines)


_CLIENT_SCRIPT = r'''# Run-local broker client. This file is an executable boundary, not public context.
from __future__ import annotations
import json
from pathlib import Path
import socket

_MAX_RESPONSE_BYTES = 1024 * 1024

def request(script_file: str, payload: dict) -> dict:
    root = Path(script_file).resolve().parent
    capability_path = root / ".contextswarm_tool_capability.json"
    capability = json.loads(capability_path.read_text(encoding="utf-8"))
    message = {
        "schema_version": "contextswarm_mini_broker_request_v1",
        "token": capability["token"],
        "task_id": capability["task_id"],
        **payload,
    }
    encoded = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(encoded) > 128 * 1024:
        raise RuntimeError("formal tool request is too large")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(480)
    try:
        connection.connect(capability["socket_path"])
        connection.sendall(encoded)
        chunks = bytearray()
        while b"\n" not in chunks:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > _MAX_RESPONSE_BYTES:
                raise RuntimeError("formal tool response is too large")
    finally:
        connection.close()
    raw = bytes(chunks).split(b"\n", 1)[0]
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict):
        raise RuntimeError("formal tool broker returned a non-object")
    return response
'''


_EVALUATE_SCRIPT = r'''#!/usr/bin/env python3
from __future__ import annotations
import json
import sys
from _contextswarm_tool_client import request

def main() -> int:
    try:
        response = request(__file__, {"op": "evaluate_local"})
    except Exception as error:
        print(json.dumps({"status": "EVALUATOR_ERROR", "message": type(error).__name__}, indent=2))
        return 2
    print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if response.get("status") not in {"EVALUATOR_ERROR", "OUT_OF_HORIZON", "BUDGET_EXHAUSTED"} else 1

if __name__ == "__main__":
    raise SystemExit(main())
'''


_FORMAL_QUERY_SCRIPT = r'''#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
from _contextswarm_tool_client import request

def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bounded advisory Lean API scout; never official proof evidence.",
        epilog=(
            "Examples:\n"
            "  ./formal_query search finite sum inequality\n"
            "  ./formal_query decl Finset sum_le_sum\n"
            "  ./formal_query check Nat.coprime_comm\n"
            "  ./formal_query type Finset.sum_le_sum\n"
            "  ./formal_query check --snippet 'example (a b : Nat) : a + b = b + a := by omega'\n"
            "  ./formal_query axioms MyHelperLemma\n"
            "All results are advisory; outer closeout independently evaluates frozen bytes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text, default_limit in (
        ("search", "search public files and the revision-matched declaration index", 8),
        ("decl", "find Mathlib declaration names", 12),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("query", nargs="+")
        command.add_argument("--limit", type=int, default=default_limit)
    check = commands.add_parser("check", help="check names or elaborate a bounded snippet/tactic portfolio")
    check.add_argument("query", nargs="*", default=[])
    check.add_argument("--snippet")
    check.add_argument("--tactics")
    check.add_argument("--tactic", action="append", default=[])
    check.add_argument("--timeout", type=int, default=30)
    for name, help_text in (
        ("type", "#check an expression"),
        ("axioms", "#print axioms for a helper, including current result.lean context"),
        ("deps", "return index-related candidate premises (not a dependency graph)"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("query", nargs="+")
        command.add_argument("--timeout", type=int, default=30)
    return result

def main() -> int:
    args = parser().parse_args()
    payload = {
        "op": "formal_query",
        "command": args.command,
        "query": list(getattr(args, "query", []) or []),
        "limit": int(getattr(args, "limit", 12)),
        "snippet": getattr(args, "snippet", None),
        "tactics": getattr(args, "tactics", None),
        "tactic": list(getattr(args, "tactic", []) or []),
        "timeout": int(getattr(args, "timeout", 30)),
    }
    try:
        response = request(__file__, payload)
    except Exception as error:
        response = {"status": "scout_failed", "message": type(error).__name__, "advisory_only": True}
    print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''


__all__ = [
    "DeclarationIndex",
    "DeclarationIndexInfo",
    "EVALUATE_FILENAME",
    "FORMAL_QUERY_FILENAME",
    "PUBLIC_FILES_FILENAME",
    "TOOL_CAPABILITY_FILENAME",
    "ToolCapability",
    "inspect_declaration_index",
    "public_files_manifest",
    "sanitize_public_text",
    "stage_worker_tools",
    "tool_surface_provenance",
]
