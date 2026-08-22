"""Resolve and bind the manifest consumed by the container launcher."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess
import tomllib
from typing import Any

from .config import ConfigError, ExperimentConfig, load_config


class LaunchContractError(ValueError):
    """Raised when host-side launch inputs cannot exist in the frozen image."""


@dataclass(frozen=True)
class LaunchContract:
    config: ExperimentConfig
    container_manifest: str
    manifest_sha256: str


def resolve_launch_contract(raw: str | Path, repo_root: Path) -> LaunchContract:
    """Resolve one clean, container-visible manifest closure.

    Tracked manifests are supplied by the commit-bound image. Operator-local
    manifests are allowed only below ``runs/``, the sole source-tree mount in
    the hardened container. Every inherited manifest must follow the same
    rule, and the complete closure is hashed for an entrypoint recheck.
    """

    root = repo_root.resolve()
    _require_clean_tracked_tree(root)
    try:
        config = load_config(raw, root)
        sources = manifest_sources(config.manifest_path)
    except (ConfigError, OSError, ValueError) as exc:
        raise LaunchContractError(str(exc)) from exc

    tracked = _tracked_paths(root)
    relative_sources: list[tuple[Path, Path]] = []
    for source in sources:
        try:
            relative = source.relative_to(root)
        except ValueError as exc:
            raise LaunchContractError(
                "manifest inheritance must remain inside the launcher worktree"
            ) from exc
        if not relative.parts:
            raise LaunchContractError("manifest path is invalid")
        relative_text = relative.as_posix()
        if relative.parts[0] != "runs" and relative_text not in tracked:
            raise LaunchContractError(
                "manifest sources must be tracked or located below runs/"
            )
        relative_sources.append((relative, source))

    top_level = config.manifest_path.resolve().relative_to(root).as_posix()
    if "\n" in top_level or "\r" in top_level:
        raise LaunchContractError("manifest path contains a line break")
    return LaunchContract(
        config=config,
        container_manifest=top_level,
        manifest_sha256=_manifest_digest(relative_sources),
    )


def manifest_closure_sha256(raw: str | Path, repo_root: Path) -> str:
    """Hash the manifest closure as visible inside the running container."""

    root = repo_root.resolve()
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    sources = manifest_sources(path)
    relative_sources: list[tuple[Path, Path]] = []
    for source in sources:
        try:
            relative = source.relative_to(root)
        except ValueError as exc:
            raise LaunchContractError(
                "manifest inheritance escaped the container source root"
            ) from exc
        relative_sources.append((relative, source))
    return _manifest_digest(relative_sources)


def manifest_sources(raw: str | Path) -> tuple[Path, ...]:
    """Return a deterministic, cycle-checked manifest inheritance closure."""

    ordered: list[Path] = []
    visited: set[Path] = set()
    active: set[Path] = set()

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in active:
            raise LaunchContractError(f"manifest inheritance cycle at {resolved}")
        if resolved in visited:
            return
        active.add(resolved)
        try:
            payload: dict[str, Any] = tomllib.loads(
                resolved.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise LaunchContractError(f"manifest not found: {resolved}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise LaunchContractError(f"invalid TOML in {resolved}: {exc}") from exc
        extends = payload.get("extends", [])
        if isinstance(extends, str):
            extends = [extends]
        if not isinstance(extends, list) or not all(
            isinstance(item, str) for item in extends
        ):
            raise LaunchContractError(
                f"extends must be a string or list of strings: {resolved}"
            )
        for parent in extends:
            visit(resolved.parent / parent)
        active.remove(resolved)
        visited.add(resolved)
        ordered.append(resolved)

    visit(Path(raw))
    return tuple(ordered)


def _require_clean_tracked_tree(root: Path) -> None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise LaunchContractError("unable to inspect launcher worktree state")
    if result.stdout:
        raise LaunchContractError(
            "refusing to launch from a worktree with modified tracked files"
        )


def _tracked_paths(root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise LaunchContractError("unable to inspect tracked manifest paths")
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def _manifest_digest(sources: list[tuple[Path, Path]]) -> str:
    digest = hashlib.sha256()
    for relative, source in sorted(sources, key=lambda item: item[0].as_posix()):
        digest.update(relative.as_posix().encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        try:
            digest.update(source.read_bytes())
        except OSError as exc:
            raise LaunchContractError(f"unable to hash manifest: {relative}") from exc
        digest.update(b"\0")
    return digest.hexdigest()
