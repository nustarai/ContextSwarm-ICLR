"""Typed, deliberately small experiment manifest loader."""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
from pathlib import Path
import tomllib
from typing import Any, Mapping
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when a manifest is incomplete or internally inconsistent."""


def _deep_merge(base: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(dict(result[key]), value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_mapping(path: Path, seen: set[Path] | None = None) -> dict[str, Any]:
    path = path.resolve()
    seen = set() if seen is None else seen
    if path in seen:
        raise ConfigError(f"manifest inheritance cycle at {path}")
    seen.add(path)
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"manifest not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    merged: dict[str, Any] = {}
    extends = payload.pop("extends", [])
    if isinstance(extends, str):
        extends = [extends]
    if not isinstance(extends, list) or not all(isinstance(item, str) for item in extends):
        raise ConfigError(f"extends must be a string or list of strings: {path}")
    for parent in extends:
        parent_path = (path.parent / parent).resolve()
        merged = _deep_merge(merged, _load_mapping(parent_path, seen.copy()))
    return _deep_merge(merged, payload)


def _as_dict(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{name}] must be a table")
    return dict(value)


def _text(value: Any, default: str = "") -> str:
    return str(default if value is None else value).strip()


def _positive_int(value: Any, name: str, default: int) -> int:
    try:
        result = int(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if result <= 0:
        raise ConfigError(f"{name} must be positive")
    return result


def _nonnegative_int(value: Any, name: str, default: int) -> int:
    try:
        result = int(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if result < 0:
        raise ConfigError(f"{name} must not be negative")
    return result


@dataclass(frozen=True)
class ExperimentConfig:
    manifest_path: Path
    repo_root: Path
    name: str
    mode: str
    communication: str
    dataset_root: Path
    problem_ids_path: Path
    output_root: Path
    max_parallel: int
    initial_agents_per_task: int
    max_attempts_per_task: int
    cancel_on_proved: bool
    assignment_policy: str
    episodes_per_task: int
    max_tasks: int
    time_limit_seconds: int
    seed: int
    model: str
    thinking: str
    fast_mode: bool
    pi_binary: str
    pi_extension: str
    pi_timeout_seconds: int
    pi_http_idle_timeout_ms: int
    pi_retry_enabled: bool
    pi_retry_max_retries: int
    pi_retry_base_delay_ms: int
    pi_provider_max_retries: int
    pi_provider_max_retry_delay_ms: int
    aisw_enabled: bool
    aisw_binary: str
    aisw_node_config: str
    aisw_coordinator_url: str
    aisw_account: str
    aisw_group: str
    aisw_lease_wait_seconds: int
    aisw_lease_retry_interval_seconds: int
    lean_server_url: str
    lean_env_id: str
    lean_timeout_seconds: int
    lean_max_lifecycle_seconds: int
    lean_max_concurrent_evaluations: int
    lean_verification_profile: str
    lean_judge_mode: str
    docker_image: str
    docker_memory_mb: int
    docker_internet: str
    docker_network: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def uses_cps(self) -> bool:
        return self.mode == "cps" and self.communication != "none"

    @property
    def resolved_output_root(self) -> Path:
        return self._resolve_path(self.output_root)

    def _resolve_path(self, value: Path | str) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
        candidate = (self.repo_root / path).resolve()
        if candidate.exists() or not (self.manifest_path.parent / path).exists():
            return candidate
        return (self.manifest_path.parent / path).resolve()

    def resolve_runtime_path(self, value: str) -> Path:
        return self._resolve_path(value)

    def public_dict(self) -> dict[str, Any]:
        """Return a JSON-safe manifest snapshot without credentials."""
        return {
            "name": self.name,
            "mode": self.mode,
            "communication": self.communication,
            "dataset_root": str(self.dataset_root),
            "problem_ids_path": str(self.problem_ids_path),
            "max_parallel": self.max_parallel,
            "initial_agents_per_task": self.initial_agents_per_task,
            "max_attempts_per_task": self.max_attempts_per_task,
            "cancel_on_proved": self.cancel_on_proved,
            "assignment_policy": self.assignment_policy,
            "episodes_per_task": self.episodes_per_task,
            "max_tasks": self.max_tasks,
            "time_limit_seconds": self.time_limit_seconds,
            "seed": self.seed,
            "model": self.model,
            "thinking": self.thinking,
            "fast_mode": self.fast_mode,
            "pi_timeout_seconds": self.pi_timeout_seconds,
            "pi_http_idle_timeout_ms": self.pi_http_idle_timeout_ms,
            "pi_retry_enabled": self.pi_retry_enabled,
            "pi_retry_max_retries": self.pi_retry_max_retries,
            "pi_retry_base_delay_ms": self.pi_retry_base_delay_ms,
            "pi_provider_max_retries": self.pi_provider_max_retries,
            "pi_provider_max_retry_delay_ms": self.pi_provider_max_retry_delay_ms,
            "pi_binary_configured": bool(self.pi_binary),
            "aisw_enabled": self.aisw_enabled,
            "aisw_binary_configured": bool(self.aisw_binary),
            "aisw_node_config_configured": bool(self.aisw_node_config),
            "aisw_coordinator_configured": bool(self.aisw_coordinator_url),
            "aisw_account_configured": bool(self.aisw_account),
            "aisw_group_configured": bool(self.aisw_group),
            "lean_server_url": _redact_endpoint(self.lean_server_url),
            "lean_env_id": self.lean_env_id,
            "lean_timeout_seconds": self.lean_timeout_seconds,
            "lean_max_lifecycle_seconds": self.lean_max_lifecycle_seconds,
            "lean_max_concurrent_evaluations": self.lean_max_concurrent_evaluations,
            "lean_verification_profile": self.lean_verification_profile,
            "lean_judge_mode": self.lean_judge_mode,
            "docker_image": self.docker_image,
            "docker_memory_mb": self.docker_memory_mb,
            "docker_internet": self.docker_internet,
            "docker_network": self.docker_network,
        }


def _resolve_manifest_arg(raw: str | Path, repo_root: Path) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    for option in (
        repo_root / candidate,
        repo_root / "configs" / candidate,
        repo_root / "configs" / f"{candidate}.toml",
    ):
        if option.is_file():
            return option.resolve()
    raise ConfigError(f"unable to resolve manifest: {raw}")


def load_config(raw: str | Path, repo_root: Path | None = None) -> ExperimentConfig:
    repo_root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    manifest_path = _resolve_manifest_arg(raw, repo_root)
    payload = _load_mapping(manifest_path)
    experiment = _as_dict(payload.get("experiment"), "experiment")
    pi = _as_dict(payload.get("pi"), "pi")
    aisw_payload = payload.get("aisw")
    if aisw_payload is None:
        aisw_payload = payload.get("nurouter")
    aisw = _as_dict(aisw_payload, "aisw")
    lean = _as_dict(payload.get("lean"), "lean")
    docker = _as_dict(payload.get("docker"), "docker")

    mode = _text(experiment.get("mode"), "cps").lower()
    if mode not in {"mono", "parallel", "cps"}:
        raise ConfigError("experiment.mode must be mono, parallel, or cps")
    communication = _text(experiment.get("communication"), "none").lower()
    allowed_communication = {"none", "blackboard", "direct", "hybrid", "simple"}
    if communication not in allowed_communication:
        raise ConfigError(f"experiment.communication must be one of {sorted(allowed_communication)}")
    if mode in {"mono", "parallel"} and communication != "none":
        raise ConfigError("mono and parallel baselines must run with communication = none")

    dataset_raw = _text(experiment.get("dataset_root"), "benchmarks/matholympiadbench")
    problem_raw = _text(experiment.get("problem_ids"), "benchmarks/matholympiadbench/problem_ids.json")
    dataset_root = Path(dataset_raw).expanduser()
    problem_ids_path = Path(problem_raw).expanduser()
    output_root = Path(_text(experiment.get("output_root"), "runs"))

    max_parallel = _positive_int(experiment.get("max_parallel"), "experiment.max_parallel", 12)
    initial_default = 2 if mode == "cps" else 1
    initial_agents = _positive_int(
        experiment.get("initial_agents_per_task"),
        "experiment.initial_agents_per_task",
        initial_default,
    )
    if mode != "cps":
        initial_agents = 1
    max_attempts = _nonnegative_int(
        experiment.get("max_attempts_per_task"),
        "experiment.max_attempts_per_task",
        0 if mode == "cps" else 1,
    )
    cancel_on_proved = bool(experiment.get("cancel_on_proved", True))
    assignment_policy = _text(experiment.get("assignment_policy"), "least_active")
    if assignment_policy not in {"least_active", "round_robin"}:
        raise ConfigError("experiment.assignment_policy must be least_active or round_robin")
    episodes = _positive_int(experiment.get("episodes_per_task"), "experiment.episodes_per_task", 1)
    max_tasks = _nonnegative_int(experiment.get("max_tasks"), "experiment.max_tasks", 0)
    horizon = _positive_int(experiment.get("time_limit_seconds"), "experiment.time_limit_seconds", 3600)
    seed = _nonnegative_int(experiment.get("seed"), "experiment.seed", 0)
    if mode == "mono":
        max_parallel = 1
        episodes = 1
    elif mode == "parallel":
        episodes = 1

    model = _text(pi.get("model"), "openai-codex/gpt-5.6-sol")
    thinking = _text(pi.get("thinking"), "max")
    pi_timeout = _positive_int(pi.get("timeout_seconds"), "pi.timeout_seconds", horizon)
    pi_http_idle_timeout_ms = _nonnegative_int(
        pi.get("http_idle_timeout_ms"),
        "pi.http_idle_timeout_ms",
        600_000,
    )
    pi_retry = _as_dict(pi.get("retry"), "pi.retry")
    pi_retry_provider = _as_dict(pi_retry.get("provider"), "pi.retry.provider")
    pi_retry_enabled = bool(pi_retry.get("enabled", True))
    pi_retry_max_retries = _nonnegative_int(
        pi_retry.get("max_retries"),
        "pi.retry.max_retries",
        10,
    )
    pi_retry_base_delay_ms = _positive_int(
        pi_retry.get("base_delay_ms"),
        "pi.retry.base_delay_ms",
        2_000,
    )
    pi_provider_max_retries = _nonnegative_int(
        pi_retry_provider.get("max_retries"),
        "pi.retry.provider.max_retries",
        0,
    )
    pi_provider_max_retry_delay_ms = _nonnegative_int(
        pi_retry_provider.get("max_retry_delay_ms"),
        "pi.retry.provider.max_retry_delay_ms",
        60_000,
    )
    fast_mode = bool(pi.get("fast_mode", True))

    aisw_enabled = bool(aisw.get("enabled", True))
    aisw_binary = _text(aisw.get("binary"), "~/.local/share/contextswarm/aisw-linux-aarch64")
    aisw_node_config = _text(aisw.get("node_config"), "~/.aisw-codex/node.toml")
    coordinator = _text(aisw.get("coordinator_url"))
    account = _text(aisw.get("account"))
    group = _text(aisw.get("group"))
    lease_wait = _positive_int(aisw.get("lease_wait_seconds"), "aisw.lease_wait_seconds", 7200)
    lease_retry = _positive_int(
        aisw.get("lease_retry_interval_seconds"),
        "aisw.lease_retry_interval_seconds",
        2,
    )

    lean_url = _text(lean.get("server_url"), "http://127.0.0.1:18000")
    lean_env = _text(lean.get("env_id"), "formal_matholympiadbench")
    lean_timeout = _positive_int(lean.get("timeout_seconds"), "lean.timeout_seconds", 300)
    lean_max_lifecycle = _positive_int(
        lean.get("max_lifecycle_seconds"),
        "lean.max_lifecycle_seconds",
        max(3_600, (8 * lean_timeout) + 120),
    )
    lean_max_evaluations = _positive_int(
        lean.get("max_concurrent_evaluations"),
        "lean.max_concurrent_evaluations",
        1,
    )
    profile = _text(lean.get("verification_profile"), "formal_proof")
    judge_mode = _text(lean.get("judge_mode"), "fast")
    docker_network = _text(docker.get("network"), "host").lower()
    if docker_network not in {"host", "bridge"}:
        raise ConfigError("docker.network must be host or bridge")

    cfg = ExperimentConfig(
        manifest_path=manifest_path,
        repo_root=repo_root,
        name=_text(experiment.get("name"), manifest_path.stem),
        mode=mode,
        communication=communication,
        dataset_root=dataset_root,
        problem_ids_path=problem_ids_path,
        output_root=output_root,
        max_parallel=max_parallel,
        initial_agents_per_task=initial_agents,
        max_attempts_per_task=max_attempts,
        cancel_on_proved=cancel_on_proved,
        assignment_policy=assignment_policy,
        episodes_per_task=episodes,
        max_tasks=max_tasks,
        time_limit_seconds=horizon,
        seed=seed,
        model=model,
        thinking=thinking,
        fast_mode=fast_mode,
        pi_binary=_text(pi.get("binary")),
        pi_extension=_text(pi.get("extension")),
        pi_timeout_seconds=pi_timeout,
        pi_http_idle_timeout_ms=pi_http_idle_timeout_ms,
        pi_retry_enabled=pi_retry_enabled,
        pi_retry_max_retries=pi_retry_max_retries,
        pi_retry_base_delay_ms=pi_retry_base_delay_ms,
        pi_provider_max_retries=pi_provider_max_retries,
        pi_provider_max_retry_delay_ms=pi_provider_max_retry_delay_ms,
        aisw_enabled=aisw_enabled,
        aisw_binary=aisw_binary,
        aisw_node_config=aisw_node_config,
        aisw_coordinator_url=coordinator,
        aisw_account=account,
        aisw_group=group,
        aisw_lease_wait_seconds=lease_wait,
        aisw_lease_retry_interval_seconds=lease_retry,
        lean_server_url=lean_url,
        lean_env_id=lean_env,
        lean_timeout_seconds=lean_timeout,
        lean_max_lifecycle_seconds=lean_max_lifecycle,
        lean_max_concurrent_evaluations=lean_max_evaluations,
        lean_verification_profile=profile,
        lean_judge_mode=judge_mode,
        docker_image=_text(docker.get("image"), "contextswarm-iclr-mini:latest"),
        docker_memory_mb=_positive_int(docker.get("memory_mb"), "docker.memory_mb", 16384),
        docker_internet=_text(docker.get("internet"), "online"),
        docker_network=docker_network,
        extra={"raw": payload},
    )
    if cfg.uses_cps and cfg.episodes_per_task < 1:
        raise ConfigError("CPS requires at least one episode per task")
    return cfg


def _redact_endpoint(raw: str) -> str:
    value = str(raw or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<configured>" if value else ""
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{host}"
    return "<configured>" if value else ""
