"""Cheap, fail-closed transport checks for real NuRouter/AISW runs."""

from __future__ import annotations

import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import tomllib
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .config import ExperimentConfig
from .evaluator import (
    EvaluatorError,
    LeanEvaluator,
    safe_worker_response,
    sanitize_worker_text,
)
from .formal_tools import (
    DeclarationIndex,
    effective_mathlib_revision,
    prepare_declaration_index,
)
from .models import Task, Verdict
from .pi_agent import PiAgent


class PreflightError(RuntimeError):
    """A required transport is unavailable or has drifted."""


def run_preflight(
    config: ExperimentConfig,
    output_dir: Path,
    *,
    declaration_index: DeclarationIndex | None = None,
) -> dict[str, Any]:
    """Check binary/config/Lean reachability without exposing credentials."""
    if not config.lean_server_url:
        raise PreflightError(
            "CONTEXTSWARM_JUDGE_URL must be set for a real preflight"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    if declaration_index is None:
        if config.formal_tools_enabled:
            try:
                declaration_index = prepare_declaration_index(
                    config,
                    output_dir / ".private" / "formal_tools",
                )
            except OSError:
                raise PreflightError(
                    "formal declaration-index snapshot preparation failed"
                ) from None
        else:
            declaration_index = DeclarationIndex(None)
    report: dict[str, Any] = {
        "schema_version": 2,
        "status": "ok",
        "aisw": {},
        "lean": {},
        "formal_tools": {},
    }
    agent = PiAgent(config)
    binary = Path(agent.binary())
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise PreflightError("NuRouter/AISW Pi executable is not available")
    report["aisw"] = {
        "enabled": bool(config.aisw_enabled),
        "binary_sha256": _sha256(binary),
        "nurouter_version": sanitize_worker_text(
            os.environ.get("MINI_SWARM_NUROUTER_VERSION", "unknown"), 200
        ),
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
        evaluator = LeanEvaluator(
            config.lean_server_url,
            lean_env_id=config.lean_env_id,
            timeout_seconds=config.lean_timeout_seconds,
            max_lifecycle_seconds=config.lean_max_lifecycle_seconds,
            verification_profile=config.lean_verification_profile,
            judge_mode=config.lean_judge_mode,
        )
        health = evaluator.health()
        execution_identity = _deployment_identity(health)
        report["lean"] = _safe_health(health, config.lean_env_id)
        _validate_lean_health(report["lean"])
        # The strict kernel/index contract is enabled for paper-facing
        # manifests.  Offline smoke/compatibility manifests may still probe
        # health without requiring a live Lean kernel or an operator index.
        strict_formal = bool(
            config.formal_tools_enabled and config.formal_tools_require_decl_index
        )
        kernel_verdict: Verdict | None = None
        if strict_formal:
            kernel_verdict = _kernel_probe(evaluator, output_dir)
            report["lean"]["kernel_probe"] = _safe_kernel_probe(kernel_verdict)
            _validate_kernel_probe(kernel_verdict)
        health_revision = _revision_from_payload(report["lean"])
        kernel_revision = (
            _revision_from_payload(kernel_verdict.response)
            if kernel_verdict is not None
            else ""
        )
        if health_revision and kernel_revision and health_revision != kernel_revision:
            raise PreflightError("Lean health and kernel probe revisions disagree")
        endpoint_revision = kernel_revision or health_revision
        if strict_formal and not endpoint_revision:
            raise PreflightError("Lean endpoint did not advertise a Mathlib revision")
        if endpoint_revision:
            report["lean"]["endpoint_mathlib_revision"] = endpoint_revision
        if config.formal_tools_enabled:
            configured_revision = effective_mathlib_revision(config)
            index_info = declaration_index.info
            report["formal_tools"] = {
                "enabled": True,
                "configured_mathlib_revision": configured_revision or None,
                "endpoint_mathlib_revision": endpoint_revision,
                "declaration_index": index_info.public_dict(),
            }
            if strict_formal:
                expected_sha256 = (
                    os.environ.get("CONTEXTSWARM_MINI_DECL_INDEX_SHA256", "")
                    .strip()
                    .lower()
                    or str(config.formal_tools_decl_index_sha256 or "").strip().lower()
                )
                if not configured_revision:
                    raise PreflightError(
                        "formal_tools.mathlib_revision must be configured for a real run"
                    )
                if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                    raise PreflightError(
                        "a declaration-index SHA-256 contract is required for paper-facing runs"
                    )
                if not index_info.available or not index_info.compatible:
                    raise PreflightError(
                        "formal declaration index is unavailable or incompatible"
                    )
                if not index_info.sha256 or index_info.sha256 != expected_sha256:
                    raise PreflightError(
                        "formal declaration index SHA-256 does not match its contract"
                    )
                if not index_info.mathlib_revision:
                    raise PreflightError(
                        "formal declaration index lacks Mathlib revision metadata"
                    )
                if index_info.schema != "decl_index_v1":
                    raise PreflightError("formal declaration index schema is invalid")
                if not (
                    configured_revision == endpoint_revision == index_info.mathlib_revision
                ):
                    raise PreflightError(
                        "configured, endpoint, and declaration-index Mathlib revisions disagree"
                    )
        else:
            report["formal_tools"] = {
                "enabled": False,
                "declaration_index": declaration_index.info.public_dict(),
            }
        if config.lean_require_result_cache_disabled:
            cache_health_url = os.environ.get(
                "CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL", ""
            ).strip()
            if not cache_health_url:
                raise PreflightError(
                    "CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL must be set when disabled Judge result cache is required"
                )
            # A separately configured cache-health URL may point at a
            # different Judge deployment.  Requiring a stable identity match
            # prevents a healthy cache-disabled sidecar from being mistaken
            # for the backend that actually receives proof submissions.
            identity_required = not _same_endpoint(
                config.lean_server_url,
                cache_health_url,
            )
            if identity_required and execution_identity is None:
                raise PreflightError(
                    "Lean health lacks a stable execution deployment identity"
                )
            cache_evidence = _result_cache_health(
                cache_health_url,
                config.lean_env_id,
                expected_identity=execution_identity,
                require_identity=identity_required,
            )
            report["lean"]["result_cache"] = cache_evidence
            if cache_evidence.get("enabled") is not False:
                raise PreflightError("Judge result cache is not verifiably disabled")
    except PreflightError:
        raise
    except EvaluatorError as exc:
        raise PreflightError(
            f"Lean evaluator transport is unavailable ({exc.category})"
        ) from None
    except Exception:
        raise PreflightError(
            "Lean evaluator transport is unavailable (unexpected_error)"
        ) from None
    (output_dir / "transport_preflight.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def _kernel_probe(evaluator: Any, output_dir: Path) -> Verdict:
    """Run one tiny real kernel probe, independent of any worker candidate."""

    # The formal Judge requires an authored theorem before it will emit a
    # successful formal verdict.  A bare ``#check`` is useful to a human Lean
    # session, but the Judge correctly classifies it as ``no_authored_theorem``
    # (VERIFY_FAIL) even when elaboration succeeded.  Keep the probe
    # deliberately tiny while giving the endpoint the same theorem-shaped
    # contract as a real task.
    source = (
        "import Mathlib\n"
        "theorem contextswarm_preflight_kernel : (0 : Nat) = 0 := by\n"
        "  rfl\n"
    )
    task = Task(
        slug="__contextswarm_preflight_kernel__",
        root=output_dir,
        problem_text="preflight kernel probe",
        baseline_code=source,
        metadata={
            "problem_id": "preflight",
            "theorem_name": "contextswarm_preflight_kernel",
        },
    )
    probe_source = getattr(evaluator, "probe_source", None)
    if callable(probe_source):
        return probe_source(task, source, deadline_monotonic=time.monotonic() + 120.0)
    probe = getattr(evaluator, "probe", None)
    if not callable(probe):
        raise PreflightError("Lean evaluator lacks kernel probe support")
    result = probe(task, source, deadline_monotonic=time.monotonic() + 120.0)
    if isinstance(result, Verdict):
        return result
    if isinstance(result, dict):
        return Verdict(
            task_id=task.slug,
            status=str(result.get("status") or "ELABORATED"),
            score=0.0,
            elapsed_seconds=float(result.get("elapsed_ms", 0) or 0) / 1_000.0,
            response=dict(result),
        )
    raise PreflightError("Lean kernel probe returned an invalid verdict")


def _safe_kernel_probe(verdict: Verdict) -> dict[str, Any]:
    response = safe_worker_response(verdict.response)
    result: dict[str, Any] = {
        "status": sanitize_worker_text(verdict.status, 64),
        "elapsed_seconds": round(max(0.0, float(verdict.elapsed_seconds)), 6),
        "response": response,
    }
    revision = _revision_from_payload(response)
    if revision:
        result["mathlib_revision"] = revision
    return result


def _validate_kernel_probe(verdict: Verdict) -> None:
    status = str(verdict.status or "").upper()
    if status not in {
        "PROVED",
        "COMPILES_WITH_SORRY",
        "ELABORATED",
        "SUCCEEDED",
        "COMPLETED",
    }:
        raise PreflightError("Lean kernel probe did not reach a usable terminal state")
    response = safe_worker_response(verdict.response)
    # A terminal-looking status alone is not evidence that Lean elaborated the
    # probe.  Require the explicit validity bit (the probe source contains no
    # placeholder, so either accepted validity field is sufficient for older
    # Judge response profiles).
    if not (
        response.get("is_valid_with_sorry") is True
        or response.get("is_valid_no_sorry") is True
    ):
        raise PreflightError("Lean kernel probe was not explicitly accepted by the kernel")


def _revision_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("mathlib_revision", "endpoint_mathlib_revision"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return sanitize_worker_text(value.strip(), 256)
    nested = payload.get("response")
    if isinstance(nested, dict):
        value = _revision_from_payload(nested)
        if value:
            return value
    environment = payload.get("lean_environment")
    if isinstance(environment, dict):
        value = environment.get("mathlib_revision")
        if isinstance(value, str) and value.strip():
            return sanitize_worker_text(value.strip(), 256)
    return ""


def _read_node_config(config: ExperimentConfig, raw: str) -> dict[str, Any]:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config.resolve_runtime_path(raw)
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        raise PreflightError("AISW node config cannot be read") from None
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
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        HTTPException,
        TypeError,
        ValueError,
    ):
        raise PreflightError("NuRouter runtime-policy request failed") from None
    allowed = payload.get("allowCodexFastMode") if isinstance(payload, dict) else None
    return {
        "status": "ok" if allowed is True else "blocked",
        "allow_codex_fast_mode": allowed if isinstance(allowed, bool) else None,
    }


def _result_cache_health(
    raw_url: str,
    requested_env: str,
    *,
    expected_identity: tuple[str, str] | None = None,
    require_identity: bool = False,
) -> dict[str, Any]:
    """Read cache state only from a ready backend serving ``requested_env``."""

    try:
        parsed = urlsplit(raw_url.strip())
    except ValueError:
        raise PreflightError("Judge cache-health endpoint is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PreflightError("Judge cache-health endpoint is invalid")
    path = parsed.path.rstrip("/")
    if not path.endswith("/healthz"):
        path = f"{path}/healthz" if path else "/healthz"
    url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    try:
        with urlopen(
            Request(url, headers={"Accept": "application/json"}, method="GET"),
            timeout=10,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        HTTPException,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise PreflightError("Judge cache-health request failed") from None
    cache = payload.get("result_cache") if isinstance(payload, dict) else None
    if not isinstance(cache, dict) or not isinstance(cache.get("enabled"), bool):
        raise PreflightError("Judge cache-health response lacks result_cache.enabled")
    if payload.get("ok") is not True or payload.get("workspace_ready") is not True:
        raise PreflightError("Judge cache-health backend is not ready")
    for readiness_field in (
        "safeverify_ready",
        "formal_strict_safeverify_ready",
    ):
        if readiness_field in payload and payload.get(readiness_field) is not True:
            raise PreflightError("Judge cache-health backend is not ready")
    advertised_envs: set[str] = set()
    for env_field in ("accepted_lean_env_ids", "supported_lean_env_ids"):
        raw_envs = payload.get(env_field)
        if isinstance(raw_envs, list):
            advertised_envs.update(
                value for value in raw_envs if isinstance(value, str)
            )
    if requested_env not in advertised_envs:
        raise PreflightError(
            "Judge cache-health backend does not advertise the requested environment"
        )
    observed_identity = _deployment_identity(payload)
    if require_identity and observed_identity is None:
        raise PreflightError(
            "Judge cache-health response lacks a stable deployment identity"
        )
    if expected_identity is not None and observed_identity != expected_identity:
        raise PreflightError(
            "Judge cache-health deployment identity does not match the execution Judge"
        )
    result: dict[str, Any] = {
        "enabled": cache["enabled"],
        "backend_ready": True,
        "requested_env_accepted": True,
    }
    if observed_identity is not None:
        result["deployment_identity"] = {
            "kind": observed_identity[0],
            "value": observed_identity[1],
        }
    backend = cache.get("backend")
    if isinstance(backend, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", backend):
        result["backend"] = backend
    service = payload.get("service")
    if isinstance(service, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", service):
        result["service"] = service
    api_version = payload.get("api_version")
    if isinstance(api_version, str) and re.fullmatch(
        r"[A-Za-z0-9_.-]{1,64}", api_version
    ):
        result["api_version"] = api_version
    return result


_DEPLOYMENT_ID_KEYS = (
    "deployment_id",
    "execution_pool_id",
    "router_id",
    "service_instance_id",
    "instance_id",
)
_DEPLOYMENT_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,160}")


def _deployment_identity(payload: Any) -> tuple[str, str] | None:
    """Extract one stable, non-secret Judge deployment identity."""

    if not isinstance(payload, dict):
        return None
    candidates: list[dict[str, Any]] = [payload]
    nested = payload.get("response")
    if isinstance(nested, dict):
        candidates.append(nested)
    service = payload.get("service")
    if isinstance(service, dict):
        candidates.append(service)
    for candidate in candidates:
        for key in _DEPLOYMENT_ID_KEYS:
            value = candidate.get(key)
            if not isinstance(value, str):
                continue
            normalized = value.strip()
            if _DEPLOYMENT_ID_RE.fullmatch(normalized):
                return key, normalized
    return None


def _same_endpoint(left: str, right: str) -> bool:
    """Compare endpoint scopes without recording or exposing credentials."""

    try:
        left_parts = urlsplit(str(left or "").strip())
        right_parts = urlsplit(str(right or "").strip())
    except ValueError:
        return False
    if not left_parts.scheme or not left_parts.netloc or not right_parts.scheme or not right_parts.netloc:
        return False
    left_path = left_parts.path.rstrip("/")
    right_path = right_parts.path.rstrip("/")
    if left_path.endswith("/healthz"):
        left_path = left_path[: -len("/healthz")].rstrip("/")
    if right_path.endswith("/healthz"):
        right_path = right_path[: -len("/healthz")].rstrip("/")
    return (
        left_parts.scheme.lower(),
        left_parts.netloc.lower(),
        left_path,
    ) == (
        right_parts.scheme.lower(),
        right_parts.netloc.lower(),
        right_path,
    )


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
    return sanitize_worker_text(line[0], 200) if line else "unavailable"


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
        "ready_workers",
        "available_service_units",
        "backend_queue_depth",
        "busy_workers",
        "capacity_state",
        "workspace_ready",
        "accepted_lean_env_ids",
        "canonical_supported_lean_env_ids",
        "mathlib_revision",
        "lean_version",
        "lean_environment",
    }
    result = {key: payload[key] for key in allowed if key in payload}
    identity = _deployment_identity(payload)
    if identity is not None:
        result["deployment_identity"] = {
            "kind": identity[0],
            "value": identity[1],
        }
    # The formal Judge exposes group-admission capacity at the top level for
    # observability.  When that optional protocol is disabled, its deliberate
    # ``admission_disabled``/zero-capacity projection must not be mistaken for
    # the direct /api/lean/jobs worker pool used by this runner.
    group_admission = payload.get("group_admission")
    group_disabled = (
        isinstance(group_admission, dict)
        and group_admission.get("enabled") is False
        and group_admission.get("status") == "disabled"
        and payload.get("capacity_error_kind") == "admission_disabled"
    )
    if group_disabled:
        result["group_admission_enabled"] = False
        for key in (
            "available_service_units",
            "backend_queue_depth",
            "capacity_state",
        ):
            result.pop(key, None)
    elif isinstance(group_admission, dict) and isinstance(
        group_admission.get("enabled"), bool
    ):
        result["group_admission_enabled"] = bool(group_admission["enabled"])
    accepted = payload.get("accepted_lean_env_ids")
    if isinstance(accepted, list):
        safe_accepted = [value for value in accepted if isinstance(value, str)]
        result["accepted_lean_env_ids"] = safe_accepted
        result["requested_env_accepted"] = requested_env in safe_accepted
    for key in ("mathlib_revision", "lean_version"):
        value = result.get(key)
        if isinstance(value, str):
            result[key] = sanitize_worker_text(value, 256)
    environment = result.get("lean_environment")
    if isinstance(environment, dict):
        safe_environment = {
            key: sanitize_worker_text(value, 256)
            for key, value in environment.items()
            if key in {"mathlib_revision", "lean_version"}
            and isinstance(value, str)
        }
        result["lean_environment"] = safe_environment
    return result


def _validate_lean_health(health: dict[str, Any]) -> None:
    """Require an explicitly usable Judge without rejecting legacy mocks.

    Core readiness fields are mandatory. Capacity fields were added later, so
    their absence remains compatible; once advertised, however, they must
    prove that a real submission can be admitted now.
    """

    if health.get("ok") is not True:
        raise PreflightError("Lean router health is not ready")
    if health.get("workspace_ready") is not True:
        raise PreflightError("Lean router workspace is not ready")
    if health.get("requested_env_accepted") is not True:
        raise PreflightError(
            "Lean router does not explicitly accept the requested environment"
        )
    if health.get("group_admission_enabled") is False:
        direct_ready = health.get("ready_workers", health.get("active_workers"))
        if (
            isinstance(direct_ready, bool)
            or not isinstance(direct_ready, (int, float))
            or not math.isfinite(float(direct_ready))
            or direct_ready <= 0
        ):
            raise PreflightError("Direct Lean Judge has no ready workers")
        return
    if "available_service_units" in health:
        available = health.get("available_service_units")
        if (
            isinstance(available, bool)
            or not isinstance(available, (int, float))
            or not math.isfinite(float(available))
            or available <= 0
        ):
            raise PreflightError("Lean router has no available service units")
    if (
        "capacity_state" in health
        and health.get("capacity_state") != "AVAILABLE"
    ):
        raise PreflightError("Lean router capacity is not available")
