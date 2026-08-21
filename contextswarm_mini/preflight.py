"""Cheap, fail-closed transport checks for real NuRouter/AISW runs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tomllib
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import ExperimentConfig
from .evaluator import LeanEvaluator
from .pi_agent import PiAgent


class PreflightError(RuntimeError):
    """A required transport is unavailable or has drifted."""


def run_preflight(config: ExperimentConfig, output_dir: Path) -> dict[str, Any]:
    """Check binary/config/Lean reachability without exposing credentials."""
    report: dict[str, Any] = {"schema_version": 1, "status": "ok", "aisw": {}, "lean": {}}
    agent = PiAgent(config)
    binary = Path(agent.binary())
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise PreflightError(f"NuRouter/AISW Pi executable is not available: {binary}")
    report["aisw"] = {
        "enabled": bool(config.aisw_enabled),
        "binary_sha256": _sha256(binary),
        "nurouter_version": os.environ.get("MINI_SWARM_NUROUTER_VERSION", "unknown"),
        "pi_binary_version": _version(binary),
    }

    if config.aisw_enabled:
        node_config = os.environ.get("MINI_SWARM_AISW_NODE_CONFIG", "").strip()
        if not node_config:
            node_config = config.aisw_node_config.strip()
        node_payload = _read_node_config(config, node_config)
        coordinator = config.aisw_coordinator_url.strip() or str(node_payload.get("coordinator_url") or "").strip()
        if not coordinator:
            raise PreflightError("AISW is enabled but no coordinator_url is configured")
        report["aisw"]["node_config_present"] = True
        report["aisw"]["coordinator_configured"] = True
        if config.fast_mode:
            policy = _runtime_policy(coordinator, str(node_payload.get("token") or ""))
            report["aisw"]["fast_mode_policy"] = policy
            if policy.get("allow_codex_fast_mode") is not True:
                raise PreflightError("NuRouter runtime policy did not explicitly allow fast mode")

    try:
        health = LeanEvaluator(config.lean_server_url, lean_env_id=config.lean_env_id).health()
        report["lean"] = _safe_health(health, config.lean_env_id)
        if report["lean"].get("requested_env_accepted") is False:
            raise PreflightError(
                f"Lean router does not advertise the requested environment: {config.lean_env_id}"
            )
        if report["lean"].get("workspace_ready") is False:
            raise PreflightError("Lean router workspace is not ready")
    except Exception as exc:
        raise PreflightError(f"Lean evaluator transport is unavailable: {exc}") from exc
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "transport_preflight.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _read_node_config(config: ExperimentConfig, raw: str) -> dict[str, Any]:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config.resolve_runtime_path(raw)
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PreflightError(f"AISW node config cannot be read: {path}") from exc
    if not isinstance(payload, dict):
        raise PreflightError("AISW node config must be a TOML table")
    return payload


def _runtime_policy(base_url: str, token: str) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/core/v1/runtime-policy"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers, method="GET"), timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise PreflightError("NuRouter runtime-policy request failed") from exc
    allowed = payload.get("allowCodexFastMode") if isinstance(payload, dict) else None
    return {
        "status": "ok" if allowed is True else "blocked",
        "allow_codex_fast_mode": allowed if isinstance(allowed, bool) else None,
    }


def _version(binary: Path) -> str:
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    line = (result.stdout or result.stderr or "").splitlines()
    return line[0][:200] if line else "unavailable"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "unavailable"
    return digest.hexdigest()


def _safe_health(payload: dict[str, Any], requested_env: str) -> dict[str, Any]:
    allowed = {
        "ok",
        "api_version",
        "service",
        "active_workers",
        "available_service_units",
        "backend_queue_depth",
        "busy_workers",
        "capacity_state",
        "workspace_ready",
        "accepted_lean_env_ids",
        "canonical_supported_lean_env_ids",
    }
    result = {key: payload[key] for key in allowed if key in payload}
    accepted = payload.get("accepted_lean_env_ids") or payload.get("supported_lean_env_ids")
    if isinstance(accepted, list):
        result["accepted_lean_env_ids"] = accepted
        result["requested_env_accepted"] = requested_env in accepted
    return result
