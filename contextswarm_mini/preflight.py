"""Cheap, fail-closed transport checks for real NuRouter/AISW runs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import ExperimentConfig
from .evaluator import LeanEvaluator
from .formal_tools import DeclarationIndex, tool_surface_provenance
from .models import Task
from .pi_agent import PiAgent
from .artifacts import atomic_write_json


class PreflightError(RuntimeError):
    """A required transport is unavailable or has drifted."""


def run_preflight(config: ExperimentConfig, output_dir: Path) -> dict[str, Any]:
    """Check binary/config/Lean reachability without exposing credentials."""
    report: dict[str, Any] = {
        "schema_version": 2,
        "status": "ok",
        "aisw": {},
        "lean": {},
        "formal_tools": {},
        "runtime": {
            "python_version": sys.version.split()[0],
            "docker_image": config.docker_image,
            "pi_package_version": os.environ.get("CONTEXTSWARM_MINI_PI_VERSION", "unknown"),
            "codex_package_version": os.environ.get("CONTEXTSWARM_MINI_CODEX_VERSION", "unknown"),
        },
    }
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
        lean_evaluator = LeanEvaluator(
            config.lean_server_url,
            lean_env_id=config.lean_env_id,
            timeout_seconds=config.lean_timeout_seconds,
            max_lifecycle_seconds=config.lean_max_lifecycle_seconds,
            verification_profile=config.lean_verification_profile,
            judge_mode=config.lean_judge_mode,
            terminal_overload_retries=0,
        )
        health = lean_evaluator.health()
        report["lean"] = _safe_health(health, config.lean_env_id)
        if report["lean"].get("requested_env_accepted") is False:
            raise PreflightError(
                f"Lean router does not advertise the requested environment: {config.lean_env_id}"
            )
        if report["lean"].get("workspace_ready") is False:
            raise PreflightError("Lean router workspace is not ready")
        guard_path = config.resolve_runtime_path(config.pi_guard_extension)
        if not guard_path.is_file():
            raise PreflightError("Pi worker workspace guard extension is unavailable")
        if config.formal_tools_enabled:
            index_raw = (
                os.environ.get("CONTEXTSWARM_MINI_DECL_INDEX", "").strip()
                or os.environ.get("MINI_SWARM_DECL_INDEX", "").strip()
                or config.formal_tools_decl_index.strip()
            )
            index_path = config.resolve_runtime_path(index_raw) if index_raw else None
            expected_index_sha256 = (
                os.environ.get("CONTEXTSWARM_MINI_DECL_INDEX_SHA256", "").strip().lower()
                or config.formal_tools_decl_index_sha256
            )
            expected_mathlib_revision = (
                os.environ.get("CONTEXTSWARM_MINI_MATHLIB_REVISION", "").strip()
                or config.formal_tools_mathlib_revision
            )
            declaration_index = DeclarationIndex(
                index_path,
                expected_sha256=expected_index_sha256,
                expected_revision=expected_mathlib_revision,
            )
            report["formal_tools"] = {
                **tool_surface_provenance(
                    config.formal_tools_version,
                    guard_path=guard_path,
                ),
                "declaration_index": declaration_index.info.public_dict(),
                "evaluate_calls_per_task": config.formal_tools_evaluate_calls_per_task,
                "evaluate_backend_jobs_per_task": config.formal_tools_evaluate_backend_jobs_per_task,
                "query_calls_per_task": config.formal_tools_query_calls_per_task,
                "query_backend_probes_per_task": config.formal_tools_query_backend_probes_per_task,
            }
            if config.formal_tools_require_decl_index:
                if not declaration_index.info.compatible:
                    raise PreflightError(
                        "formal declaration index is required but missing or contract-incompatible"
                    )
                if not expected_index_sha256:
                    raise PreflightError(
                        "a declaration-index SHA-256 contract is required for paper-facing runs"
                    )
            probe_task = Task(
                slug="formal-tool-preflight",
                root=output_dir,
                problem_text="",
                baseline_code="import Mathlib\n",
                metadata={"problem_id": "contextswarm_formal_tool_preflight"},
            )
            probe = lean_evaluator.probe(
                probe_task,
                "import Mathlib\n\n#check Nat.succ\n",
                timeout_seconds=min(30, config.lean_timeout_seconds),
            )
            report["formal_tools"]["kernel_probe"] = {
                key: probe[key]
                for key in (
                    "status",
                    "is_valid_with_sorry",
                    "elapsed_ms",
                    "mathlib_revision",
                    "lean_version",
                    "lean_environment",
                )
                if key in probe
            }
            if probe.get("status") != "elaborated" or probe.get("is_valid_with_sorry") is not True:
                raise PreflightError("formal_query kernel probe capability is unavailable")
            endpoint_revision = _endpoint_mathlib_revision(health, probe)
            report["formal_tools"]["endpoint_mathlib_revision"] = endpoint_revision
            if config.formal_tools_require_decl_index:
                expected_revision = expected_mathlib_revision or endpoint_revision
                if not expected_revision:
                    raise PreflightError(
                        "Mathlib revision cannot be bound: configure formal_tools.mathlib_revision "
                        "or use a Judge that reports it"
                    )
                if endpoint_revision and endpoint_revision != expected_revision:
                    raise PreflightError(
                        "configured Mathlib revision does not match the Judge endpoint"
                    )
                if declaration_index.info.mathlib_revision != expected_revision:
                    raise PreflightError("formal declaration index Mathlib revision does not match Judge")
    except Exception as exc:
        if isinstance(exc, PreflightError):
            raise
        raise PreflightError(f"Lean evaluator transport is unavailable: {exc}") from exc
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "transport_preflight.json", report)
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
        "mathlib_revision",
        "lean_version",
        "judge_version",
        "contract_version",
        "image_digest",
    }
    result = {key: payload[key] for key in allowed if key in payload}
    accepted = payload.get("accepted_lean_env_ids") or payload.get("supported_lean_env_ids")
    if isinstance(accepted, list):
        result["accepted_lean_env_ids"] = accepted
        result["requested_env_accepted"] = requested_env in accepted
    return result


def _endpoint_mathlib_revision(health: dict[str, Any], probe: dict[str, Any]) -> str:
    direct = str(health.get("mathlib_revision") or probe.get("mathlib_revision") or "").strip()
    if direct:
        return direct
    environment = probe.get("lean_environment")
    if isinstance(environment, dict):
        return str(environment.get("mathlib_revision") or "").strip()
    return ""
