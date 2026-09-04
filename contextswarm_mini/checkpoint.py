"""Durable, explicitly unverified handoff snapshots for interrupted agents.

The checkpoint path is deliberately separate from the Judge/score path.  A
checkpoint is useful recovery evidence, but it is never a candidate verdict
and must not be promoted or credited without a fresh authoritative Judge
receipt.  The writer keeps immutable per-attempt snapshots and an atomic
``latest.json`` pointer so a later assignment can resume from a complete
candidate even when the Pi process was killed while editing its workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from typing import Any, Mapping

from .artifacts import append_jsonl, atomic_write_bytes, atomic_write_json
from .evaluator import sanitize_worker_identifier, sanitize_worker_text


CHECKPOINT_SCHEMA_VERSION = "contextswarm_checkpoint_v1"
_DEFAULT_MAX_CANDIDATE_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_SUMMARY_CHARS = 6_000
_MAX_CONTEXT_ITEMS = 20
_CANDIDATE_FILENAMES = frozenset({"result.lean", "result.cpp"})


def _private_dir(path: Path) -> None:
    """Create or validate an owner-only checkpoint directory."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError("checkpoint directory must be a regular directory")
    # Existing run roots may have been created with a permissive umask.  A
    # checkpoint is a private artifact, so tighten only the new subtree.
    os.chmod(path, 0o700)


def _safe_seq(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class CheckpointRef:
    """A saved immutable checkpoint and its safe public metadata."""

    task_id: str
    sequence: int
    directory: Path
    metadata_path: Path
    candidate_path: Path | None
    record: Mapping[str, Any]

    @property
    def candidate_sha256(self) -> str | None:
        value = self.record.get("candidate", {})
        if not isinstance(value, Mapping):
            return None
        digest = value.get("sha256")
        return str(digest) if isinstance(digest, str) and digest else None

    @property
    def candidate_bytes(self) -> int | None:
        value = self.record.get("candidate", {})
        if not isinstance(value, Mapping):
            return None
        raw = value.get("bytes")
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else None

    @property
    def candidate_changed_from_baseline(self) -> bool:
        value = self.record.get("candidate", {})
        return bool(isinstance(value, Mapping) and value.get("changed_from_baseline"))

    def public_dict(self) -> dict[str, Any]:
        """Return a JSON-safe record without host paths."""

        return dict(self.record)


class CheckpointStore:
    """Thread-safe per-run checkpoint writer.

    The store intentionally accepts already bounded context rows.  It applies
    one additional text/identifier sanitization pass before writing so a
    malformed agent-authored CPS item cannot smuggle credentials or host paths
    into the durable handoff artifact.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        max_candidate_bytes: int = _DEFAULT_MAX_CANDIDATE_BYTES,
        max_summary_chars: int = _DEFAULT_MAX_SUMMARY_CHARS,
        max_context_items: int = 6,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "checkpoints"
        _private_dir(self.root)
        self.max_candidate_bytes = max(1, min(int(max_candidate_bytes), 16 * 1024 * 1024))
        self.max_summary_chars = max(256, min(int(max_summary_chars), 32_000))
        self.max_context_items = max(1, min(int(max_context_items), _MAX_CONTEXT_ITEMS))
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, task_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(task_id, threading.RLock())

    @staticmethod
    def _safe_task_id(task_id: str) -> str:
        safe = sanitize_worker_identifier(task_id)
        if safe is None:
            raise ValueError("checkpoint task id is invalid")
        return safe

    @staticmethod
    def _safe_actor_id(actor_id: str) -> str:
        safe = sanitize_worker_identifier(actor_id)
        return safe or "unknown-agent"

    @staticmethod
    def _safe_candidate_filename(candidate_filename: str) -> str:
        """Allow only the benchmark's known candidate basenames.

        A worker-controlled filename must never be allowed to introduce a
        separator, ``..`` component, or an arbitrary artifact into the
        checkpoint subtree.  The Task model already restricts this value, but
        the store is also used by test/runtime adapters and therefore checks
        the boundary independently.
        """

        value = str(candidate_filename or "")
        if value not in _CANDIDATE_FILENAMES:
            raise ValueError("checkpoint candidate filename is not supported")
        return value

    def _sanitize_context(self, context: Mapping[str, Any] | None) -> dict[str, Any]:
        context = context if isinstance(context, Mapping) else {}

        def rows(name: str) -> list[dict[str, Any]]:
            raw = context.get(name, [])
            if not isinstance(raw, (list, tuple)):
                return []
            result: list[dict[str, Any]] = []
            for item in raw[: self.max_context_items]:
                if not isinstance(item, Mapping):
                    continue
                piece_id = sanitize_worker_identifier(item.get("piece_id"))
                kind = sanitize_worker_text(item.get("kind"), 64)
                title = sanitize_worker_text(item.get("title"), 300)
                body = sanitize_worker_text(item.get("body"), max(128, self.max_summary_chars // 4))
                created_at = sanitize_worker_text(item.get("created_at"), 64)
                row: dict[str, Any] = {
                    "kind": kind,
                    "title": title,
                    "body": body,
                }
                if piece_id:
                    row["piece_id"] = piece_id
                author = sanitize_worker_identifier(item.get("author"))
                if author:
                    row["author"] = author
                recipient = sanitize_worker_identifier(item.get("recipient"))
                if recipient:
                    row["recipient"] = recipient
                if created_at:
                    row["created_at"] = created_at
                result.append(row)
            return result

        next_step = sanitize_worker_text(
            context.get("next_step"), max(256, self.max_summary_chars // 3)
        )
        if not next_step:
            next_step = (
                "Open the unverified candidate, run judge_check, and continue from "
                "the recorded evidence; do not treat the checkpoint as a proof."
            )
        return {
            "completed_work": rows("completed_work"),
            "ruled_out": rows("ruled_out"),
            "next_step": next_step,
            "source": sanitize_worker_text(context.get("source"), 128) or "runner",
        }

    def save(
        self,
        *,
        task_id: str,
        task_root: Path,
        candidate_path: Path,
        candidate_filename: str,
        baseline_sha256: str,
        actor_id: str,
        episode: int,
        recovery_attempt: int,
        result: Mapping[str, Any],
        retry_pending: bool,
        context: Mapping[str, Any] | None = None,
        feedback: str = "",
        latest_validation_status: str = "",
        latest_validation_feedback: str = "",
        best_candidate_sha256: str | None = None,
    ) -> CheckpointRef:
        """Save one bounded candidate/result snapshot.

        ``result`` is expected to contain only the scalar AgentResult fields;
        command lines and raw request/response payloads are intentionally not
        accepted.  The method raises on an artifact I/O failure; the caller's
        checkpoint callback catches that failure and records it without
        changing the solver/score lifecycle.
        """

        safe_task = self._safe_task_id(task_id)
        safe_actor = self._safe_actor_id(actor_id)
        safe_candidate_filename = self._safe_candidate_filename(candidate_filename)
        with self._lock_for(safe_task):
            task_checkpoint_root = Path(task_root) / "checkpoints"
            _private_dir(task_checkpoint_root)
            sequence_path = task_checkpoint_root / "sequence"
            # Sequence is local to the task and protected by the per-task lock.
            # Keep it as a tiny regular file, never a symlink or an append race.
            try:
                current = _safe_seq(sequence_path.read_text(encoding="ascii"))
            except (FileNotFoundError, OSError, UnicodeError):
                current = 0
            # Recover from a truncated/missing sequence marker without ever
            # overwriting an immutable snapshot directory.  This also makes a
            # crash between candidate publication and marker update safe.
            try:
                existing = [
                    _safe_seq(item.name)
                    for item in task_checkpoint_root.iterdir()
                    if item.is_dir() and not item.is_symlink() and item.name.isdigit()
                ]
            except OSError:
                existing = []
            sequence = max([current, *existing], default=0) + 1
            atomic_write_bytes(sequence_path, str(sequence).encode("ascii"), mode=0o600)

            directory = task_checkpoint_root / f"{sequence:06d}"
            _private_dir(directory)
            candidate_record: dict[str, Any] = {
                "filename": safe_candidate_filename,
                "status": "missing",
                "bytes": None,
                "sha256": None,
                "changed_from_baseline": False,
                "relative_path": None,
            }
            candidate_snapshot: Path | None = None
            try:
                metadata = candidate_path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    candidate_record["status"] = "not_regular"
                elif metadata.st_size > self.max_candidate_bytes:
                    candidate_record["status"] = "too_large"
                    candidate_record["bytes"] = int(metadata.st_size)
                else:
                    raw = candidate_path.read_bytes()
                    digest = hashlib.sha256(raw).hexdigest()
                    candidate_snapshot = directory / candidate_record["filename"]
                    atomic_write_bytes(candidate_snapshot, raw, mode=0o600)
                    candidate_record.update(
                        {
                            "status": "captured",
                            "bytes": len(raw),
                            "sha256": digest,
                            "changed_from_baseline": digest != str(baseline_sha256).lower(),
                            "relative_path": f"{sequence:06d}/{candidate_record['filename']}",
                        }
                    )
            except FileNotFoundError:
                candidate_record["status"] = "missing"
            except OSError:
                candidate_record["status"] = "read_error"

            scalar_result = {
                key: result.get(key)
                for key in (
                    "returncode",
                    "timed_out",
                    "cancelled",
                    "run_horizon_reached",
                    "events",
                    "mocked",
                )
            }
            scalar_result["returncode"] = (
                int(scalar_result["returncode"])
                if isinstance(scalar_result.get("returncode"), int)
                and not isinstance(scalar_result.get("returncode"), bool)
                else None
            )
            for key in ("timed_out", "cancelled", "run_horizon_reached", "mocked"):
                scalar_result[key] = bool(scalar_result.get(key, False))
            scalar_result["events"] = _safe_seq(scalar_result.get("events"))
            if scalar_result["timed_out"]:
                reason = "timeout"
            elif scalar_result["cancelled"]:
                reason = "cancelled"
            elif scalar_result["run_horizon_reached"]:
                reason = "horizon"
            elif scalar_result["returncode"] == 0:
                reason = "completed"
            else:
                reason = "process_failure"

            record: dict[str, Any] = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "unverified": True,
                "score_eligible": False,
                "task_id": safe_task,
                "sequence": sequence,
                "actor_id": safe_actor,
                "episode": _safe_seq(episode),
                "recovery_attempt": _safe_seq(recovery_attempt),
                "retry_pending": bool(retry_pending),
                "terminal_reason": reason,
                "result": scalar_result,
                "candidate": candidate_record,
                "feedback": sanitize_worker_text(feedback, max(128, self.max_summary_chars // 4)),
                "latest_validation": {
                    "status": sanitize_worker_text(latest_validation_status, 96)
                    or "NONE",
                    "feedback": sanitize_worker_text(
                        latest_validation_feedback,
                        max(128, self.max_summary_chars // 4),
                    ),
                    "best_candidate_sha256": (
                        str(best_candidate_sha256).lower()
                        if isinstance(best_candidate_sha256, str)
                        and len(best_candidate_sha256) == 64
                        and all(
                            character in "0123456789abcdefABCDEF"
                            for character in best_candidate_sha256
                        )
                        else None
                    ),
                },
                "output_tail": sanitize_worker_text(
                    result.get("output_tail"), max(128, self.max_summary_chars // 4)
                ),
                "error_tail": sanitize_worker_text(
                    result.get("error_tail"), max(128, self.max_summary_chars // 4), tail=True
                ),
                "context": self._sanitize_context(context),
            }
            metadata_path = directory / "checkpoint.json"
            atomic_write_json(metadata_path, record, mode=0o600)
            # ``latest.json`` is a pointer, not a mutable candidate.  Readers
            # can resolve the immutable sequence directory and verify the hash.
            atomic_write_json(
                task_checkpoint_root / "latest.json",
                {
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "task_id": safe_task,
                    "sequence": sequence,
                    "metadata": f"{sequence:06d}/checkpoint.json",
                    "candidate": candidate_record.get("relative_path"),
                    "candidate_sha256": candidate_record.get("sha256"),
                    "unverified": True,
                },
                mode=0o600,
            )
            append_jsonl(task_checkpoint_root / "index.jsonl", record, mode=0o600)
            return CheckpointRef(
                task_id=safe_task,
                sequence=sequence,
                directory=directory,
                metadata_path=metadata_path,
                candidate_path=candidate_snapshot,
                record=record,
            )

    def materialize_for_agent(
        self,
        ref: CheckpointRef,
        destination: Path,
        *,
        candidate_filename: str,
        transfer_candidate: bool,
    ) -> None:
        """Copy a checkpoint into a fresh worker workspace atomically."""

        safe_candidate_filename = self._safe_candidate_filename(candidate_filename)
        _private_dir(destination)
        metadata = destination / "checkpoint.json"
        atomic_write_json(metadata, ref.public_dict(), mode=0o600)
        if not transfer_candidate:
            return
        if ref.candidate_path is None or not ref.candidate_path.exists():
            return
        raw = ref.candidate_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != ref.candidate_sha256:
            raise ValueError("checkpoint candidate changed after publication")
        snapshot = destination / safe_candidate_filename
        atomic_write_bytes(snapshot, raw, mode=0o600)

    def publish_payload(
        self,
        ref: CheckpointRef,
        *,
        max_bytes: int = 7_500,
    ) -> dict[str, Any]:
        """Return a machine-readable CPS payload that fits the piece limit.

        CPS bodies have their own bounded text contract.  Serializing the full
        on-disk record and letting :meth:`CPSStore.create_piece` clip it would
        produce invalid JSON exactly when a context summary is richest.  Keep
        the durable disk record complete, but publish a progressively reduced
        summary whose *encoded* UTF-8/ASCII representation is guaranteed to fit
        below the CPS clipping threshold.
        """

        maximum = max(512, min(int(max_bytes), 7_500))
        record = ref.record if isinstance(ref.record, Mapping) else {}
        candidate = record.get("candidate")
        candidate = candidate if isinstance(candidate, Mapping) else {}
        validation = record.get("latest_validation")
        validation = validation if isinstance(validation, Mapping) else {}
        result = record.get("result")
        result = result if isinstance(result, Mapping) else {}
        context = record.get("context")
        context = context if isinstance(context, Mapping) else {}

        def row_list(name: str, row_limit: int, body_limit: int) -> list[dict[str, Any]]:
            raw_rows = context.get(name, [])
            if not isinstance(raw_rows, (list, tuple)):
                return []
            rows: list[dict[str, Any]] = []
            for raw in raw_rows[:row_limit]:
                if not isinstance(raw, Mapping):
                    continue
                row: dict[str, Any] = {
                    "kind": sanitize_worker_text(raw.get("kind"), 48),
                    "title": sanitize_worker_text(raw.get("title"), 180),
                    "body": sanitize_worker_text(raw.get("body"), body_limit),
                }
                piece_id = sanitize_worker_identifier(raw.get("piece_id"))
                if piece_id:
                    row["piece_id"] = piece_id
                rows.append(row)
            return rows

        def payload(row_limit: int, body_limit: int) -> dict[str, Any]:
            return {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "task_id": sanitize_worker_identifier(record.get("task_id"))
                or "unknown-task",
                "sequence": _safe_seq(record.get("sequence")),
                "actor_id": sanitize_worker_identifier(record.get("actor_id"))
                or "unknown-agent",
                "episode": _safe_seq(record.get("episode")),
                "recovery_attempt": _safe_seq(record.get("recovery_attempt")),
                "retry_pending": bool(record.get("retry_pending")),
                "terminal_reason": sanitize_worker_text(
                    record.get("terminal_reason"), 64
                ),
                "unverified": True,
                "score_eligible": False,
                "result": {
                    "returncode": result.get("returncode")
                    if isinstance(result.get("returncode"), int)
                    and not isinstance(result.get("returncode"), bool)
                    else None,
                    "timed_out": bool(result.get("timed_out")),
                    "cancelled": bool(result.get("cancelled")),
                    "run_horizon_reached": bool(result.get("run_horizon_reached")),
                },
                "candidate": {
                    "filename": sanitize_worker_text(candidate.get("filename"), 32),
                    "status": sanitize_worker_text(candidate.get("status"), 32),
                    "bytes": candidate.get("bytes")
                    if isinstance(candidate.get("bytes"), int)
                    and not isinstance(candidate.get("bytes"), bool)
                    else None,
                    "sha256": candidate.get("sha256")
                    if isinstance(candidate.get("sha256"), str)
                    and len(candidate.get("sha256")) == 64
                    else None,
                    "changed_from_baseline": bool(
                        candidate.get("changed_from_baseline")
                    ),
                },
                "latest_validation": {
                    "status": sanitize_worker_text(validation.get("status"), 64)
                    or "NONE",
                    "feedback": sanitize_worker_text(
                        validation.get("feedback"), body_limit
                    ),
                    "best_candidate_sha256": validation.get("best_candidate_sha256")
                    if isinstance(validation.get("best_candidate_sha256"), str)
                    and len(validation.get("best_candidate_sha256")) == 64
                    else None,
                },
                "context": {
                    "completed_work": row_list("completed_work", row_limit, body_limit),
                    "ruled_out": row_list("ruled_out", row_limit, body_limit),
                    "next_step": sanitize_worker_text(
                        context.get("next_step"), max(96, body_limit)
                    ),
                    "source": sanitize_worker_text(context.get("source"), 64)
                    or "runner",
                },
            }

        # Try the useful summary first, then shed rows/body text until the
        # encoded representation is safely below CPSStore's 8,000-character
        # clipping boundary.  ensure_ascii keeps the byte/character bound
        # identical even when a finding contains non-ASCII text.
        for row_limit in (
            self.max_context_items,
            min(self.max_context_items, 4),
            min(self.max_context_items, 2),
            1,
            0,
        ):
            for body_limit in (
                max(96, min(self.max_summary_chars // 4, 600)),
                400,
                200,
                96,
                0,
            ):
                candidate_payload = payload(row_limit, body_limit)
                encoded = json.dumps(
                    candidate_payload,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if len(encoded.encode("ascii")) <= maximum:
                    return candidate_payload
        # The fixed scalar fields above are comfortably below the bound.  Keep
        # this final fallback defensive in case a future schema adds a large
        # constant field; it remains valid JSON and never exposes host paths.
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "task_id": "unknown-task",
            "sequence": _safe_seq(record.get("sequence")),
            "unverified": True,
            "score_eligible": False,
            "candidate": {"status": "omitted_for_bound"},
            "context": {"completed_work": [], "ruled_out": [], "next_step": "inspect checkpoint files"},
        }


__all__ = ["CHECKPOINT_SCHEMA_VERSION", "CheckpointRef", "CheckpointStore"]
