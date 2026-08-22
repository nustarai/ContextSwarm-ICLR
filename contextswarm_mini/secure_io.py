"""Race-resistant reads and immutable content-addressed candidate snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import secrets
import stat
import threading


DEFAULT_MAX_CANDIDATE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class CandidateSnapshot:
    task_id: str
    path: Path
    sha256: str
    size_bytes: int
    captured_at_monotonic: float
    payload: bytes = field(repr=False, compare=False)


def _directory_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    return flags | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def read_regular_bytes(
    path: Path,
    *,
    trusted_root: Path,
    max_bytes: int = DEFAULT_MAX_CANDIDATE_BYTES,
) -> bytes:
    """Read one confined regular file without following any path symlink."""

    root = trusted_root.absolute()
    candidate = path if path.is_absolute() else root / path
    candidate = candidate.absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise OSError("candidate is outside its trusted root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("unsafe candidate pathname")

    descriptor = os.open(root, _directory_flags())
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        leaf = os.open(relative.parts[-1], flags, dir_fd=descriptor)
        try:
            before = os.fstat(leaf)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise OSError("candidate must be a single-link regular file")
            if before.st_size < 0 or before.st_size > max_bytes:
                raise OSError(f"candidate exceeds the {max_bytes}-byte limit")
            chunks = bytearray()
            while len(chunks) <= max_bytes:
                chunk = os.read(leaf, min(1024 * 1024, max_bytes + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            after = os.fstat(leaf)
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if (
                identity_before != identity_after
                or len(chunks) != before.st_size
                or len(chunks) > max_bytes
            ):
                raise OSError("candidate changed while it was being captured")
            return bytes(chunks)
        finally:
            os.close(leaf)
    finally:
        os.close(descriptor)


class SnapshotStore:
    """Private immutable storage which workers never receive as a public path."""

    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_CANDIDATE_BYTES):
        self.root = root
        self.max_bytes = max(1, int(max_bytes))
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def capture(
        self,
        *,
        task_id: str,
        source: Path,
        trusted_root: Path,
        captured_at_monotonic: float,
    ) -> CandidateSnapshot:
        payload = read_regular_bytes(source, trusted_root=trusted_root, max_bytes=self.max_bytes)
        return self.capture_bytes(
            task_id=task_id,
            payload=payload,
            captured_at_monotonic=captured_at_monotonic,
        )

    def capture_bytes(
        self,
        *,
        task_id: str,
        payload: bytes,
        captured_at_monotonic: float,
    ) -> CandidateSnapshot:
        if len(payload) > self.max_bytes:
            raise OSError(f"candidate exceeds the {self.max_bytes}-byte limit")
        digest = hashlib.sha256(payload).hexdigest()
        task_digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
        directory = self.root / task_digest / digest
        path = directory / "result.lean"
        with self._lock:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
            if path.exists():
                existing = read_regular_bytes(path, trusted_root=self.root, max_bytes=self.max_bytes)
                if existing != payload:
                    raise OSError("content-addressed candidate snapshot is corrupt")
            else:
                temporary = directory / f".result-{os.getpid()}-{secrets.token_hex(12)}.tmp"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(temporary, flags, 0o600)
                    try:
                        offset = 0
                        while offset < len(payload):
                            written = os.write(descriptor, payload[offset:])
                            if written <= 0:
                                raise OSError("candidate snapshot write made no progress")
                            offset += written
                        os.fchmod(descriptor, 0o400)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    os.replace(temporary, path)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                directory_fd = os.open(directory, _directory_flags())
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            verified = read_regular_bytes(path, trusted_root=self.root, max_bytes=self.max_bytes)
            if hashlib.sha256(verified).hexdigest() != digest:
                raise OSError("candidate snapshot verification failed")
        return CandidateSnapshot(
            task_id=task_id,
            path=path,
            sha256=digest,
            size_bytes=len(payload),
            captured_at_monotonic=float(captured_at_monotonic),
            payload=payload,
        )

    def load(
        self,
        *,
        task_id: str,
        sha256: str,
        captured_at_monotonic: float = 0.0,
    ) -> CandidateSnapshot:
        digest = str(sha256 or "").strip().lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise OSError("invalid candidate snapshot digest")
        task_digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
        path = self.root / task_digest / digest / "result.lean"
        payload = read_regular_bytes(path, trusted_root=self.root, max_bytes=self.max_bytes)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise OSError("candidate snapshot digest mismatch")
        return CandidateSnapshot(
            task_id=task_id,
            path=path,
            sha256=digest,
            size_bytes=len(payload),
            captured_at_monotonic=float(captured_at_monotonic),
            payload=payload,
        )


__all__ = [
    "CandidateSnapshot",
    "DEFAULT_MAX_CANDIDATE_BYTES",
    "SnapshotStore",
    "read_regular_bytes",
]
