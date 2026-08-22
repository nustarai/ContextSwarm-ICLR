"""Small durable artifact helpers used by the run supervisor and broker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading
from typing import Any, Mapping
import uuid


def _reject_symlink_target(path: Path) -> None:
    """Refuse replacing a pre-existing symlink artifact pathname."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise OSError("artifact destination must not be a symlink")


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability after an atomic publication."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Publish bytes without exposing a partially written final pathname."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_target(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, mode)
        try:
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("atomic artifact write made no progress")
                offset += written
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # Recheck immediately before publication so an attacker cannot plant
        # a symlink after the initial validation and have it silently replaced.
        # Atomic replacement would not follow the link, but preserving the
        # hostile pathname masks tampering and violates the artifact contract.
        _reject_symlink_target(path)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, value: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, value.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> None:
    rendered = json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, rendered, mode=mode)


def append_jsonl(
    path: Path,
    payload: Mapping[str, Any],
    *,
    lock: threading.Lock | threading.RLock | None = None,
    mode: int = 0o600,
) -> None:
    """Append one bounded JSON record and durably flush it before returning."""

    row = (json.dumps(dict(payload), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)

    def write() -> None:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, mode)
        try:
            view = memoryview(row)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("JSONL append made no progress")
                offset += written
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    if lock is None:
        write()
    else:
        with lock:
            write()


__all__ = [
    "append_jsonl",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
]
