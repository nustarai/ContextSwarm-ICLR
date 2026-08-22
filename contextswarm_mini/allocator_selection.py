"""Prespecified, fail-closed selection of the Figure 4 allocator.

The runner deliberately does not contain a ``select one allocator`` branch.
Selection is an analysis step over the immutable paired-repeat artifacts, so
it can be audited and rerun without starting another experiment.  This module
implements that step using only the Python standard library.

The public entry point is :func:`select_allocator`.  It consumes
``contextswarm_figure4_paired_repeat_v1`` rows, validates the common contract
and all cost fields, applies the rule's hard guardrails, and ranks eligible
arms by the frozen order used by the Figure 3 selector contract:

``mean fixed-horizon nAUC`` -> ``paired-bootstrap lower endpoint`` ->
``median time-to-k`` -> canonical registry order.

Numeric guardrail values are supplied by the rule artifact.  The helper
:func:`development_rule` provides explicit *development proposal* values; it
must not be interpreted as a registered formal-manifest default.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import random
import re
from statistics import median
from typing import Any, Iterable, Mapping, Sequence


PAIRED_SCHEMA = "contextswarm_figure4_paired_repeat_v1"
SELECTION_SCHEMA = "contextswarm_allocator_selection_v1"
RULE_SCHEMA = "contextswarm_allocator_selection_rule_v1"
RULE_ID = "figure4_allocator_selection_v1"
POLICIES = ("uniform_refill", "task_state", "trace_state", "llm_scheduler")
REGISTRY_ORDER = POLICIES
_POLICY_PARAMETER_IDENTITY_FIELDS = ("policy", "allocation_policy")
DEFAULT_BOOTSTRAP_DRAWS = 10_000
DEFAULT_BOOTSTRAP_SEED = 39039
DEFAULT_TARGET_K = 6
MIN_VALIDATION_REPEATS = 8
BOOTSTRAP_METHOD = "paired_block_percentile"
BOOTSTRAP_CONFIDENCE = 0.95
BOOTSTRAP_QUANTILE = "linear"
# Comparisons in the frozen numeric guardrails use a tiny absolute tolerance
# for serialization/rounding noise.  Keep this explicit and shared by the
# per-repeat and aggregate checks.
COMPARISON_EPSILON = 1e-12
# JSON numbers are parsed as IEEE-754 doubles by many artifact consumers.
# Refuse larger integer values so an integer field cannot silently change when
# it crosses a serialization boundary (and to keep malformed artifacts from
# forcing unbounded integer arithmetic).
_MAX_EXACT_INTEGER = (1 << 53) - 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|secret|password|credential|authorization|"
    r"coordinator[_-]?url|judge[_-]?url|base[_-]?url|node\.toml|private[_-]?endpoint)",
    re.IGNORECASE,
)
_AUTH_URL = re.compile(r"https?://[^\s/@]+:[^\s/@]+@", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)
_MISSING = object()
_UNSET = object()


class AllocatorSelectionError(ValueError):
    """A malformed rule/artifact or an impossible selection."""

    def __init__(self, message: str, *, code: str = "invalid_artifact") -> None:
        super().__init__(message)
        self.code = code


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(str(key))
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number {value}")


def _loads(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError, _DuplicateKey) as exc:
        raise AllocatorSelectionError("malformed JSON artifact", code="invalid_json") from exc


def canonical_json(value: Any) -> str:
    """Return the canonical compact JSON representation used for hashes."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise AllocatorSelectionError("artifact contains non-JSON data") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AllocatorSelectionError(f"{name} must be an object")
    return value


def _text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AllocatorSelectionError(f"{name} must be a non-empty string")
    return value.strip() if not allow_empty else value


def _finite(value: Any, name: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AllocatorSelectionError(f"{name} must be numeric")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise AllocatorSelectionError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise AllocatorSelectionError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise AllocatorSelectionError(f"{name} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise AllocatorSelectionError(f"{name} must be at most {maximum}")
    return number


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    """Parse integer counts without lossy float conversion."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AllocatorSelectionError(f"{name} must be an integer")
    if isinstance(value, int):
        number = value
    else:
        if not math.isfinite(value) or not value.is_integer():
            raise AllocatorSelectionError(f"{name} must be an integer")
        # A float beyond this bound may already have lost integer precision.
        if abs(value) > _MAX_EXACT_INTEGER:
            raise AllocatorSelectionError(f"{name} exceeds exact integer range")
        number = int(value)
    if number < minimum:
        raise AllocatorSelectionError(f"{name} must be at least {minimum}")
    if number > _MAX_EXACT_INTEGER:
        raise AllocatorSelectionError(f"{name} exceeds exact integer range")
    return number


def _sha(value: Any, name: str) -> str:
    text = _text(value, name)
    if not _SHA256.fullmatch(text):
        raise AllocatorSelectionError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _scan_sensitive(value: Any, path: str = "$") -> list[str]:
    """Find credential/endpoint material before it can enter output hashes."""

    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if _SENSITIVE_KEY.search(str(key)):
                found.append(child_path)
            found.extend(_scan_sensitive(child, child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(_scan_sensitive(child, f"{path}[{index}]"))
    elif isinstance(value, str) and (_AUTH_URL.search(value) or _BEARER.search(value)):
        found.append(path)
    return found


def _get(mapping: Mapping[str, Any], *paths: str, default: Any = _UNSET) -> Any:
    """Read a dotted path, rejecting contradictory aliases.

    Figure 4 producers retain a few compatibility aliases (for example
    ``time_to_k`` and ``time_to_k_seconds``).  Silently preferring the first
    alias would let a tampered artifact hide a disagreement, so all present
    aliases must carry the same canonical JSON value.
    """
    found: list[tuple[str, Any]] = []
    for path in paths:
        current: Any = mapping
        for component in path.split("."):
            if not isinstance(current, Mapping) or component not in current:
                break
            current = current[component]
        else:
            found.append((path, current))
    if found:
        baseline = canonical_json(found[0][1])
        if any(canonical_json(value) != baseline for _, value in found[1:]):
            raise AllocatorSelectionError(
                f"contradictory aliases for {', '.join(path for path, _ in found)}"
            )
        return found[0][1]
    if default is not _UNSET:
        return default
    raise AllocatorSelectionError(f"missing one of {', '.join(paths)}")


def _pair_identity(value: Any, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise AllocatorSelectionError(f"{name} must be a string or non-negative integer")
    if isinstance(value, int) and value < 0:
        raise AllocatorSelectionError(f"{name} must be a string or non-negative integer")
    text = str(value).strip()
    if not text:
        raise AllocatorSelectionError(f"{name} must not be empty")
    if text.startswith("-") and text[1:].isdigit():
        raise AllocatorSelectionError(f"{name} must be a string or non-negative integer")
    return text


def _without_pair_identity(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a contract for comparison across paired repeats.

    The raw contract hash legitimately differs between repeats because it
    contains the paired identity.  Only those identity fields are rebound;
    all other fields remain part of the fairness boundary.
    """

    def strip(value: Any, *, top: bool = False) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, child in value.items():
                if top and str(key) in {"paired_repeat_id", "paired_seed", "repeat", "seed"}:
                    continue
                result[str(key)] = strip(child, top=False)
            return result
        if isinstance(value, list):
            return [strip(item, top=False) for item in value]
        return value

    return strip(contract, top=True)


def _selector_identity(contract: Mapping[str, Any]) -> Any:
    """Resolve selector identity aliases without conflating objects and names.

    Producers in the wild use both a complete selector identity object
    (``selector_identity``/``selection``) and a compatibility leaf alias
    (``selection.selector_name``).  Those are related representations, not
    values that can be compared byte-for-byte.  Complete-object aliases must
    agree with one another; leaf aliases must agree with one another and, when
    an object is present, with that object's selector name.
    """

    def read(path: str) -> Any:
        current: Any = contract
        for component in path.split("."):
            if not isinstance(current, Mapping) or component not in current:
                return _MISSING
            current = current[component]
        return current

    candidates: list[tuple[str, Any]] = []
    for path in ("selector_identity", "selector.identity", "selection"):
        value = read(path)
        if value is not _MISSING:
            candidates.append((path, value))
    selector_container = read("selector")
    # ``selector = {identity = ...}`` is a wrapper, while a mapping without an
    # identity child is itself a legacy full-identity object.
    if selector_container is not _MISSING and not (
        isinstance(selector_container, Mapping) and "identity" in selector_container
    ):
        candidates.append(("selector", selector_container))
    for path in ("selector_name", "selection.selector_name"):
        value = read(path)
        if value is not _MISSING:
            candidates.append((path, value))

    objects = [(path, value) for path, value in candidates if isinstance(value, Mapping)]
    leaves = [(path, value) for path, value in candidates if not isinstance(value, Mapping)]

    if objects:
        baseline = canonical_json(objects[0][1])
        if any(canonical_json(value) != baseline for _, value in objects[1:]):
            raise AllocatorSelectionError(
                "contradictory selector identity object aliases"
            )
        identity: Any = objects[0][1]

        def object_fields(*names: str) -> list[tuple[str, Any]]:
            values: list[tuple[str, Any]] = []
            for path, value in objects:
                for name in names:
                    if name in value:
                        values.append((f"{path}.{name}", value[name]))
            return values

        def contract_fields(*paths: str) -> list[tuple[str, Any]]:
            values: list[tuple[str, Any]] = []
            for path in paths:
                value = read(path)
                if value is not _MISSING:
                    values.append((path, value))
            return values

        def require_agreement(values: list[tuple[str, Any]], label: str) -> None:
            if not values:
                return
            baseline = canonical_json(values[0][1])
            if any(canonical_json(value) != baseline for _, value in values[1:]):
                raise AllocatorSelectionError(f"contradictory selector {label} aliases")

        name_values = object_fields("selector_name") + leaves
        require_agreement(name_values, "name")
        visibility_values = object_fields("visibility") + contract_fields(
            "selector_visibility",
            "trace_visibility",
            "selector.visibility",
            "selection.visibility",
        )
        require_agreement(visibility_values, "visibility")
        config_values = object_fields("config_sha256", "selection_config_id") + contract_fields(
            "selector_config_sha256",
            "selector.config_sha256",
            "selection.selection_config_id",
        )
        require_agreement(config_values, "configuration")
    if not candidates:
        raise AllocatorSelectionError(
            "comparison_contract.selector_identity is missing"
        )
    if leaves:
        baseline = canonical_json(leaves[0][1])
        if any(canonical_json(value) != baseline for _, value in leaves[1:]):
            raise AllocatorSelectionError("contradictory selector name aliases")
    # Preserve the historical resolution priority after validation.
    return candidates[0][1]


def _contract_summary(contract: Mapping[str, Any], repeat_id: str, paired_seed: str) -> dict[str, Any]:
    """Validate the fixed comparison contract and return canonical metadata."""

    if not contract:
        raise AllocatorSelectionError("comparison_contract must not be empty")
    selector = _selector_identity(contract)
    if isinstance(selector, Mapping):
        if not selector:
            raise AllocatorSelectionError("selector identity must not be empty")
        visibility = _get(contract, "selector_visibility", "trace_visibility", "selector.visibility", "selection.visibility", default=_MISSING)
        if visibility is _MISSING:
            visibility = _get(selector, "visibility", default="project_shared")
        selector_config = _get(contract, "selector_config_sha256", "selector.config_sha256", "selection.selection_config_id", default=_MISSING)
        if selector_config is _MISSING:
            selector_config = _get(selector, "config_sha256", "selection_config_id", default=_MISSING)
    else:
        _text(selector, "comparison_contract.selector_identity")
        visibility = _get(
            contract,
            "selector_visibility",
            "trace_visibility",
            "visibility",
            "selection.visibility",
            default="project_shared",
        )
        selector_config = _get(
            contract,
            "selector_config_sha256",
            "selector.config_sha256",
            "selection.selection_config_id",
            default=_MISSING,
        )
    if visibility != "project_shared":
        raise AllocatorSelectionError("selector visibility must be project_shared")
    # The selector identity/configuration is part of the fixed-arm boundary;
    # a missing or placeholder digest would make arms incomparable.
    selector_config = _sha(selector_config, "comparison_contract.selector_config")

    dataset = _get(contract, "dataset", "dataset_identity")
    _text(dataset, "comparison_contract.dataset")
    tasks = _get(contract, "ordered_task_ids", "tasks", "task_order")
    if not isinstance(tasks, (list, tuple)) or not tasks or not all(isinstance(item, str) and item for item in tasks):
        raise AllocatorSelectionError("comparison_contract task order must be a non-empty string list")
    if len(set(tasks)) != len(tasks):
        raise AllocatorSelectionError("comparison_contract task order contains duplicates")
    _text(_get(contract, "model"), "comparison_contract.model")
    _mapping(_get(contract, "inference_settings", "inference"), "comparison_contract.inference_settings")
    evaluator = _get(contract, "evaluator", "evaluator_contract", "evaluator_contract_sha256", "judge_contract")
    if isinstance(evaluator, Mapping):
        if not evaluator:
            raise AllocatorSelectionError("comparison_contract.evaluator must not be empty")
    else:
        _text(evaluator, "comparison_contract.evaluator")
    _mapping(_get(contract, "runtime_limits", "runtime"), "comparison_contract.runtime_limits")
    horizon = _finite(_get(contract, "horizon_seconds", "horizon"), "comparison_contract.horizon_seconds", minimum=0.0)
    if horizon <= 0:
        raise AllocatorSelectionError("comparison_contract.horizon_seconds must be positive")
    capacity = _integer(_get(contract, "total_capacity", "cps_capacity", "capacity"), "comparison_contract.total_capacity", minimum=1)
    initial = _get(contract, "initial_allocation")
    initial_map = _mapping(initial, "comparison_contract.initial_allocation")
    if set(initial_map) != set(tasks):
        raise AllocatorSelectionError("initial_allocation must cover exactly the ordered task IDs")
    initial_total = 0
    for task_id in tasks:
        initial_total += _integer(initial_map[task_id], f"initial_allocation.{task_id}")
    if initial_total > capacity:
        raise AllocatorSelectionError("comparison_contract.initial_allocation exceeds total_capacity")
    candidate_transfer = _get(contract, "candidate_transfer", "candidate_solution_transfer")
    if not isinstance(candidate_transfer, bool):
        raise AllocatorSelectionError("comparison_contract.candidate_transfer must be boolean")
    stopping = _get(contract, "stopping_rule", "stopping")
    _text(stopping, "comparison_contract.stopping_rule")
    _text(_get(contract, "communication"), "comparison_contract.communication")
    direct = _get(contract, "direct_messages_enabled", "direct_messages")
    if direct is not False:
        raise AllocatorSelectionError("direct messages must be disabled")

    contract_repeat = _get(contract, "paired_repeat_id", "repeat", default=repeat_id)
    contract_seed = _get(contract, "paired_seed", "seed", default=paired_seed)
    if _pair_identity(contract_repeat, "comparison_contract.paired_repeat_id") != repeat_id:
        raise AllocatorSelectionError("comparison contract repeat identity mismatch")
    if _pair_identity(contract_seed, "comparison_contract.paired_seed") != paired_seed:
        raise AllocatorSelectionError("comparison contract seed identity mismatch")
    return {
        "dataset": str(dataset),
        "ordered_task_ids": list(tasks),
        "selector_identity": selector,
        "selector_visibility": visibility,
        "selector_config": selector_config,
        "model": str(_get(contract, "model")),
        "inference_settings": dict(_get(contract, "inference_settings", "inference")),
        "evaluator": evaluator,
        "runtime_limits": dict(_get(contract, "runtime_limits", "runtime")),
        "horizon_seconds": horizon,
        "total_capacity": capacity,
        "initial_allocation": {str(k): int(_integer(v, f"initial_allocation.{k}")) for k, v in initial_map.items()},
        "candidate_transfer": candidate_transfer,
        "stopping_rule": str(stopping),
        "communication": str(_get(contract, "communication")),
        "direct_messages_enabled": False,
        "paired_repeat_id": repeat_id,
        "paired_seed": paired_seed,
    }


def _threshold(mapping: Mapping[str, Any], *names: str, default: Any = _MISSING) -> Any:
    found: list[tuple[str, Any]] = []
    for name in names:
        if name in mapping:
            value = mapping[name]
            if isinstance(value, Mapping):
                value = _get(value, "max", "threshold", default=_MISSING)
                if value is _MISSING:
                    raise AllocatorSelectionError(
                        f"guardrail alias {name} must contain max or threshold"
                    )
            found.append((name, value))
    if found:
        baseline = canonical_json(found[0][1])
        if any(canonical_json(value) != baseline for _, value in found[1:]):
            raise AllocatorSelectionError(
                "contradictory guardrail aliases for "
                + ", ".join(name for name, _ in found),
                code="invalid_rule",
            )
        return found[0][1]
    if default is not _MISSING:
        return default
    raise AllocatorSelectionError(f"guardrails missing one of {', '.join(names)}")


@dataclass(frozen=True)
class GuardrailConfig:
    """Numeric hard gates applied before metric ranking."""

    scheduler_occupied_capacity_fraction_max: float = 0.05
    scheduler_token_fraction_max: float = 0.05
    fallback_rate_max: float = 0.10
    total_slot_seconds_factor_max: float = 1.000000001
    max_occupied_slots: int | None = None
    require_per_block: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "GuardrailConfig":
        values = _mapping(raw or {}, "guardrails")
        occupied = _threshold(
            values,
            "scheduler_occupied_capacity_fraction_max",
            "scheduler_reserved_capacity_fraction_max",
            "scheduler_slot_seconds_fraction_max",
            "scheduler_occupied_capacity_fraction",
            default=cls.scheduler_occupied_capacity_fraction_max,
        )
        token = _threshold(
            values,
            "scheduler_token_fraction_max",
            "scheduler_tokens_fraction_max",
            "scheduler_token_share_max",
            "scheduler_token_fraction",
            default=cls.scheduler_token_fraction_max,
        )
        fallback = _threshold(values, "fallback_rate_max", "fallback_fraction_max", "fallback_fraction", default=cls.fallback_rate_max)
        slot_factor = _threshold(
            values,
            "total_slot_seconds_factor_max",
            "total_capacity_slot_seconds_factor_max",
            default=cls.total_slot_seconds_factor_max,
        )
        max_slots = _threshold(values, "max_occupied_slots", "max_occupied_capacity", default=None)
        occupied_number = _finite(occupied, "guardrails.scheduler_occupied_capacity_fraction_max", minimum=0.0, maximum=1.0)
        token_number = _finite(token, "guardrails.scheduler_token_fraction_max", minimum=0.0, maximum=1.0)
        fallback_number = _finite(fallback, "guardrails.fallback_rate_max", minimum=0.0, maximum=1.0)
        factor_number = _finite(slot_factor, "guardrails.total_slot_seconds_factor_max", minimum=1.0)
        slots_number = None if max_slots is None else _integer(max_slots, "guardrails.max_occupied_slots", minimum=1)
        require_per_block = values.get("require_per_block", True)
        if not isinstance(require_per_block, bool):
            raise AllocatorSelectionError("guardrails.require_per_block must be boolean")
        if require_per_block is not True:
            raise AllocatorSelectionError(
                "guardrails.require_per_block must be true (fail-closed rule)",
                code="invalid_rule",
            )
        return cls(occupied_number, token_number, fallback_number, factor_number, slots_number, require_per_block)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "scheduler_occupied_capacity_fraction_max": self.scheduler_occupied_capacity_fraction_max,
            "scheduler_token_fraction_max": self.scheduler_token_fraction_max,
            "fallback_rate_max": self.fallback_rate_max,
            "total_slot_seconds_factor_max": self.total_slot_seconds_factor_max,
            "require_per_block": self.require_per_block,
        }
        if self.max_occupied_slots is not None:
            result["max_occupied_slots"] = self.max_occupied_slots
        return result


def development_rule(*, validation_repeat_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """Return an explicit development/validation rule proposal.

    The thresholds and split are intentionally visible in the returned JSON.
    A formal launcher must copy/freeze them in its manifest before observing
    formal outcomes; this helper is not a hidden formal default.
    """

    ids = [str(item) for item in (validation_repeat_ids or ())]
    return {
        "schema_version": RULE_SCHEMA,
        "rule_id": RULE_ID,
        "phase": "development_validation",
        "policies": list(POLICIES),
        "metric": {
            "name": "fixed_horizon_nauc",
            "field": "nauc",
            "aggregation": "mean",
            "direction": "max",
            "require_history": True,
        },
        "validation_split": {"kind": "paired_repeat_ids", "paired_repeat_ids": ids},
        "minimum_validation_repeats": MIN_VALIDATION_REPEATS,
        "target_k": DEFAULT_TARGET_K,
        "bootstrap": {
            "method": BOOTSTRAP_METHOD,
            "draws": DEFAULT_BOOTSTRAP_DRAWS,
            "seed": DEFAULT_BOOTSTRAP_SEED,
            "confidence": BOOTSTRAP_CONFIDENCE,
            "quantile": BOOTSTRAP_QUANTILE,
        },
        "tie_break": ["mean_nauc_desc", "bootstrap_lcb95_desc", "median_time_to_k_asc", "registry_order"],
        "guardrails": GuardrailConfig().as_dict(),
        "posthoc_tuning": False,
    }


@dataclass(frozen=True)
class _Cost:
    solver_calls: int
    solver_tokens: int
    scheduler_tokens: int
    solver_slot_seconds: float
    scheduler_slot_seconds: float
    scheduler_calls: int
    scheduler_latency_seconds: float
    scheduler_reservations: int
    invalid_outputs: int
    fallback_count: int
    decisions: int
    max_occupied_slots: int | None

    @property
    def total_tokens(self) -> int:
        return self.solver_tokens + self.scheduler_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "solver_calls": self.solver_calls,
            "solver_tokens": self.solver_tokens,
            "scheduler_tokens": self.scheduler_tokens,
            "solver_slot_seconds": self.solver_slot_seconds,
            "scheduler_slot_seconds": self.scheduler_slot_seconds,
            "scheduler_calls": self.scheduler_calls,
            "scheduler_latency_seconds": self.scheduler_latency_seconds,
            "scheduler_reservations": self.scheduler_reservations,
            "invalid_outputs": self.invalid_outputs,
            "fallback_count": self.fallback_count,
            "decisions": self.decisions,
            "max_occupied_slots": self.max_occupied_slots,
        }


def _cost_number(mapping: Mapping[str, Any], names: Sequence[str], name: str, *, default: Any = _MISSING) -> float:
    values = [mapping[key] for key in names if key in mapping]
    if not values:
        if default is _MISSING:
            raise AllocatorSelectionError(f"missing {name}")
        value = default
    else:
        parsed = [_finite(value, name, minimum=0.0) for value in values]
        if any(value != parsed[0] for value in parsed[1:]):
            raise AllocatorSelectionError(f"contradictory aliases for {name}")
        return parsed[0]
    return _finite(value, name, minimum=0.0)


def _cost_int(mapping: Mapping[str, Any], names: Sequence[str], name: str, *, default: Any = _MISSING) -> int:
    values = [mapping[key] for key in names if key in mapping]
    if not values:
        if default is _MISSING:
            raise AllocatorSelectionError(f"missing {name}")
        value = default
    else:
        parsed = [_integer(value, name, minimum=0) for value in values]
        if any(value != parsed[0] for value in parsed[1:]):
            raise AllocatorSelectionError(f"contradictory aliases for {name}")
        return parsed[0]
    return _integer(value, name, minimum=0)


def _extract_cost(arm: Mapping[str, Any], policy: str) -> _Cost:
    solver = _mapping(_get(arm, "solver_usage", "solver_cost"), f"{policy}.solver_usage")
    scheduler = _mapping(_get(arm, "scheduler_cost", "llm_scheduler_cost"), f"{policy}.scheduler_cost")
    solver_calls = _cost_int(
        solver, ("calls", "solver_calls"), f"{policy}.solver_usage.calls", default=_MISSING
    )
    solver_tokens = _cost_int(
        solver,
        ("total_tokens", "tokens", "model_tokens", "solver_tokens"),
        f"{policy}.solver_usage.total_tokens",
        default=_MISSING,
    )
    solver_input = _cost_int(solver, ("input_tokens", "prompt_tokens"), f"{policy}.solver_usage.input_tokens", default=_MISSING)
    solver_output = _cost_int(solver, ("output_tokens", "completion_tokens"), f"{policy}.solver_usage.output_tokens", default=_MISSING)
    if solver_tokens != solver_input + solver_output:
        raise AllocatorSelectionError(f"{policy}.solver_usage.total_tokens must equal input+output")
    solver_slots = _cost_number(
        solver,
        ("slot_seconds", "occupied_slot_seconds", "solver_slot_seconds", "compute_slot_seconds", "solver_agent_seconds"),
        f"{policy}.solver_usage.slot_seconds",
        default=_MISSING,
    )
    scheduler_input = _cost_int(scheduler, ("input_tokens", "prompt_tokens"), f"{policy}.scheduler_cost.input_tokens", default=_MISSING)
    scheduler_output = _cost_int(scheduler, ("output_tokens", "completion_tokens"), f"{policy}.scheduler_cost.output_tokens", default=_MISSING)
    scheduler_tokens = _cost_int(
        scheduler,
        ("total_tokens", "tokens", "scheduler_tokens"),
        f"{policy}.scheduler_cost.total_tokens",
        default=_MISSING,
    )
    if scheduler_tokens != scheduler_input + scheduler_output:
        raise AllocatorSelectionError(f"{policy}.scheduler_cost.total_tokens must equal input+output")
    scheduler_slots = _cost_number(
        scheduler,
        ("occupied_capacity_slot_seconds", "reserved_slot_seconds", "occupied_slot_seconds", "slot_seconds"),
        f"{policy}.scheduler_cost.occupied_capacity_slot_seconds",
        default=_MISSING,
    )
    calls = _cost_int(scheduler, ("calls", "scheduler_calls"), f"{policy}.scheduler_cost.calls", default=_MISSING)
    latency = _cost_number(scheduler, ("latency_seconds", "scheduler_latency_seconds"), f"{policy}.scheduler_cost.latency_seconds", default=_MISSING)
    reservations = _cost_int(scheduler, ("capacity_reservations", "reservation_slots", "reservations"), f"{policy}.scheduler_cost.capacity_reservations", default=_MISSING)
    invalid = _cost_int(scheduler, ("invalid_outputs", "invalid_decisions"), f"{policy}.scheduler_cost.invalid_outputs", default=_MISSING)
    scheduler_fallback = _cost_int(
        scheduler, ("fallback_count", "fallbacks", "fallback_decisions"),
        f"{policy}.scheduler_cost.fallback_count", default=_MISSING,
    )
    scheduler_horizon = _cost_int(
        scheduler, ("horizon_truncations", "horizon_truncation_count"),
        f"{policy}.scheduler_cost.horizon_truncations", default=0,
    )
    metrics = _mapping(_get(arm, "allocation_metrics", "allocation"), f"{policy}.allocation_metrics")
    decisions = _cost_int(metrics, ("decisions", "decision_count", "allocation_decisions"), f"{policy}.allocation_metrics.decisions", default=_MISSING)
    fallback = _cost_int(metrics, ("fallbacks", "fallback_decisions", "fallback_count"), f"{policy}.allocation_metrics.fallbacks", default=_MISSING)
    metrics_invalid = _cost_int(
        metrics, ("invalid_outputs", "invalid_output_count"),
        f"{policy}.allocation_metrics.invalid_outputs", default=0,
    )
    metrics_horizon = _cost_int(
        metrics, ("horizon_truncations", "horizon_truncation_count"),
        f"{policy}.allocation_metrics.horizon_truncations", default=0,
    )
    admitted = _cost_int(
        metrics, ("admitted_decisions", "admitted_count"),
        f"{policy}.allocation_metrics.admitted_decisions", default=decisions,
    )
    reported_rate = _get(metrics, "fallback_rate", default=_MISSING)
    if reported_rate is not _MISSING:
        rate = _finite(reported_rate, f"{policy}.allocation_metrics.fallback_rate", minimum=0.0, maximum=1.0)
        if decisions == 0 or not math.isclose(rate, fallback / decisions, abs_tol=1e-12):
            raise AllocatorSelectionError(f"{policy}.allocation_metrics.fallback_rate is inconsistent")
    if fallback > decisions:
        raise AllocatorSelectionError(f"{policy}.allocation_metrics.fallbacks exceeds decisions")
    if admitted > decisions:
        raise AllocatorSelectionError(f"{policy}.allocation_metrics.admitted_decisions exceeds decisions")
    if scheduler_fallback != fallback:
        raise AllocatorSelectionError(f"{policy} scheduler and allocation fallback counts disagree")
    if invalid != metrics_invalid:
        raise AllocatorSelectionError(f"{policy} scheduler and allocation invalid-output counts disagree")
    if scheduler_horizon != metrics_horizon:
        raise AllocatorSelectionError(f"{policy} scheduler and allocation horizon-truncation counts disagree")
    max_candidates = []
    for source, names in (
        (solver, ("max_occupied_slots", "max_occupied_capacity")),
        (scheduler, ("max_occupied_slots", "max_occupied_capacity")),
        (arm, ("max_occupied_slots", "max_occupied_capacity")),
    ):
        for alias in names:
            if alias in source:
                max_candidates.append(_integer(source[alias], f"{policy}.max_occupied_slots", minimum=0))
                break
    if not max_candidates:
        raise AllocatorSelectionError(f"{policy}.max_occupied_slots is required")
    if len(set(max_candidates)) != 1:
        raise AllocatorSelectionError(f"{policy}.max_occupied_slots is contradictory")
    max_slots_raw = max_candidates[0]
    max_slots = None if max_slots_raw is None else _integer(max_slots_raw, f"{policy}.max_occupied_slots", minimum=0)
    if invalid > calls:
        raise AllocatorSelectionError(f"{policy}.scheduler_cost.invalid_outputs exceeds calls")
    if fallback > calls and calls > 0:
        raise AllocatorSelectionError(f"{policy}.allocation_metrics.fallbacks exceeds scheduler calls")
    cost = _Cost(
        solver_tokens=solver_tokens,
        solver_calls=solver_calls,
        scheduler_tokens=scheduler_tokens,
        solver_slot_seconds=solver_slots,
        scheduler_slot_seconds=scheduler_slots,
        scheduler_calls=calls,
        scheduler_latency_seconds=latency,
        scheduler_reservations=reservations,
        invalid_outputs=invalid,
        fallback_count=fallback,
        decisions=decisions,
        max_occupied_slots=max_slots,
    )
    if policy != "llm_scheduler":
        if any(value != 0 for value in (scheduler_tokens, scheduler_slots, calls, reservations, invalid, fallback, scheduler_fallback, scheduler_horizon)) or latency != 0.0:
            raise AllocatorSelectionError(f"deterministic arm {policy} has non-zero scheduler cost")
    else:
        if reservations != calls:
            raise AllocatorSelectionError("llm_scheduler capacity reservations must equal calls")
        if calls != decisions:
            raise AllocatorSelectionError("llm_scheduler calls must equal allocation decisions")
    if calls == 0 and latency != 0.0:
        raise AllocatorSelectionError(f"{policy} has latency but zero scheduler calls")
    return cost


def _history(arm: Mapping[str, Any], *, horizon: float, max_score: float) -> tuple[list[tuple[float, float]], dict[str, float | None]]:
    raw = _get(arm, "accepted_score_history", "score_history", default=_MISSING)
    if raw is _MISSING:
        raise AllocatorSelectionError("accepted_score_history is required to reconstruct nAUC")
    if not isinstance(raw, (list, tuple)):
        raise AllocatorSelectionError("accepted_score_history must be a list")
    points: list[tuple[float, float]] = []
    computed_times: dict[str, float | None] = {
        str(target): None for target in range(1, int(max_score) + 1)
    }
    previous_time = -1.0
    previous_score = 0.0
    for index, item in enumerate(raw):
        row = _mapping(item, f"accepted_score_history[{index}]")
        elapsed = _finite(_get(row, "elapsed_seconds", "horizon_elapsed_seconds", "time_seconds"), f"accepted_score_history[{index}].elapsed_seconds", minimum=0.0, maximum=horizon)
        score = _finite(_get(row, "accepted_score", "score", "value"), f"accepted_score_history[{index}].accepted_score", minimum=0.0, maximum=max_score)
        if elapsed < previous_time:
            raise AllocatorSelectionError("accepted_score_history times must be monotone")
        if score < previous_score:
            raise AllocatorSelectionError("accepted_score_history scores must be monotone")
        if elapsed == previous_time and score == previous_score and points:
            raise AllocatorSelectionError("accepted_score_history contains a duplicate no-op point")
        previous_time, previous_score = elapsed, score
        points.append((elapsed, score))
    area = 0.0
    previous_time = 0.0
    score = 0.0
    for elapsed, value in points:
        area += score * (elapsed - previous_time)
        score = value
        previous_time = elapsed
        for target in range(1, int(max_score) + 1):
            key = str(target)
            if computed_times[key] is None and score >= target:
                computed_times[key] = elapsed
    area += score * (horizon - previous_time)
    # Keep explicit nulls out of the internal map; the output normalizer adds
    # the requested target key when it is unreached.
    reported = _get(arm, "time_to_k_seconds", "time_to_k", default=_MISSING)
    if reported is _MISSING:
        raise AllocatorSelectionError("time_to_k_seconds is required")
    reported_map = _mapping(reported, "time_to_k_seconds")
    if set(reported_map) != set(computed_times):
        raise AllocatorSelectionError("time_to_k_seconds must cover every target through max_score")
    for key, expected in computed_times.items():
        value = reported_map[key]
        if value is None:
            if expected is not None:
                raise AllocatorSelectionError("reported time_to_k marks a reached target as null")
            continue
        number = _finite(value, f"time_to_k_seconds.{key}", minimum=0.0, maximum=horizon)
        if expected is None or not math.isclose(expected, number, abs_tol=1e-12):
            raise AllocatorSelectionError("reported time_to_k disagrees with score history")
    nauc = area / (horizon * max_score)
    return points, computed_times


def _recompute_nauc(points: Sequence[tuple[float, float]], horizon: float, max_score: float) -> float:
    previous = 0.0
    score = 0.0
    area = 0.0
    for elapsed, value in points:
        area += score * (elapsed - previous)
        score = value
        previous = elapsed
    area += score * (horizon - previous)
    return area / (horizon * max_score)


def _policy_neutral_parameters(parameters: Mapping[str, Any], policy: str) -> dict[str, Any]:
    """Return allocation parameters with only the arm identity removed.

    The four Figure 4 arms are allowed to carry an explicit policy identity in
    their parameter object, but every other parameter is part of the common
    allocation contract.  Runtime summaries from older producers omit that
    identity, so its presence is optional; when present, both compatibility
    spellings must agree and must name the arm selected by the enclosing key.
    """

    normalized = json.loads(canonical_json(parameters))
    identity = _get(
        normalized,
        *_POLICY_PARAMETER_IDENTITY_FIELDS,
        default=_MISSING,
    )
    if identity is not _MISSING:
        if not isinstance(identity, str) or identity != policy:
            raise AllocatorSelectionError(
                f"{policy}.allocation_parameters policy identity does not match arm",
                code="config_mismatch",
            )
        for field in _POLICY_PARAMETER_IDENTITY_FIELDS:
            normalized.pop(field, None)
    return normalized


def _normalize_arm(row: Mapping[str, Any], policy: str, *, row_horizon: float, task_count: int, require_history: bool) -> dict[str, Any]:
    arm = dict(_mapping(row, f"arms.{policy}"))
    if arm.get("policy", policy) != policy:
        raise AllocatorSelectionError(f"arm policy mismatch for {policy}")
    reported_nauc = _finite(arm.get("nauc"), f"{policy}.nauc", minimum=0.0, maximum=1.0)
    max_score = _integer(arm.get("max_score", row.get("max_score", task_count)), f"{policy}.max_score", minimum=1)
    horizon = _finite(arm.get("horizon_seconds", row_horizon), f"{policy}.horizon_seconds", minimum=0.000001)
    if not math.isclose(horizon, row_horizon, abs_tol=1e-12):
        raise AllocatorSelectionError(f"{policy}.horizon_seconds mismatch")
    points: list[tuple[float, float]] = []
    times: dict[str, float | None]
    if _get(arm, "accepted_score_history", "score_history", default=_MISSING) is not _MISSING:
        points, times = _history(arm, horizon=horizon, max_score=max_score)
        reconstructed = _recompute_nauc(points, horizon, max_score)
        if not math.isclose(reconstructed, reported_nauc, rel_tol=0.0, abs_tol=1e-9):
            raise AllocatorSelectionError(f"{policy}.nauc does not match accepted_score_history")
        final_value = points[-1][1] if points else 0.0
        reported_final = _finite(
            arm.get("final_accepted_score", final_value),
            f"{policy}.final_accepted_score",
            minimum=0.0,
            maximum=max_score,
        )
        if not math.isclose(reported_final, final_value, rel_tol=0.0, abs_tol=1e-12):
            raise AllocatorSelectionError(f"{policy}.final_accepted_score does not match accepted_score_history")
    elif require_history:
        raise AllocatorSelectionError(f"{policy}.accepted_score_history is required")
    else:
        raw_times = _mapping(_get(arm, "time_to_k_seconds", "time_to_k", default={}), f"{policy}.time_to_k_seconds")
        times = {str(key): (None if value is None else _finite(value, f"{policy}.time_to_k_seconds.{key}", minimum=0.0, maximum=horizon)) for key, value in raw_times.items()}
    # The paired schema carries an arm-level final score.  It is required even
    # when a legacy development row omits score history, and remains bounded.
    if "final_accepted_score" not in arm:
        raise AllocatorSelectionError(f"{policy}.final_accepted_score is required")
    final_score = _finite(
        arm["final_accepted_score"],
        f"{policy}.final_accepted_score",
        minimum=0.0,
        maximum=max_score,
    )
    parameters = _mapping(_get(arm, "allocation_parameters", "allocation_config"), f"{policy}.allocation_parameters")
    parameters = json.loads(canonical_json(parameters))
    config_hash = _sha(_get(arm, "allocation_config_sha256", "allocation_config_hash"), f"{policy}.allocation_config_sha256")
    if canonical_sha256(parameters) != config_hash:
        raise AllocatorSelectionError(f"{policy}.allocation_config_sha256 does not match allocation_parameters")
    neutral_parameters = _policy_neutral_parameters(parameters, policy)
    cost = _extract_cost(arm, policy)
    return {
        "policy": policy,
        "nauc": reported_nauc,
        "max_score": max_score,
        "horizon_seconds": horizon,
        "accepted_score_history": points,
        "time_to_k_seconds": times,
        "final_accepted_score": final_score,
        "allocation_parameters": parameters,
        "allocation_parameters_neutral": neutral_parameters,
        "allocation_config_sha256": config_hash,
        "cost": cost,
        "raw": arm,
    }


def _normalize_row(raw: Mapping[str, Any], *, require_history: bool) -> dict[str, Any]:
    row = dict(_mapping(raw, "paired repeat"))
    if row.get("schema_version") != PAIRED_SCHEMA:
        raise AllocatorSelectionError("unsupported paired-repeat schema")
    if _scan_sensitive(row):
        raise AllocatorSelectionError("paired artifact contains sensitive fields")
    repeat_id = _pair_identity(row.get("paired_repeat_id"), "paired_repeat_id")
    seed = _pair_identity(row.get("paired_seed"), "paired_seed")
    arms_raw = _mapping(row.get("arms"), "arms")
    if set(arms_raw) != set(POLICIES):
        raise AllocatorSelectionError(f"paired repeat must contain exactly {POLICIES}")
    contract = dict(_mapping(row.get("comparison_contract"), "comparison_contract"))
    contract_hash = _sha(row.get("comparison_contract_sha256"), "comparison_contract_sha256")
    if canonical_sha256(contract) != contract_hash:
        raise AllocatorSelectionError("comparison_contract_sha256 mismatch")
    contract_meta = _contract_summary(contract, repeat_id, seed)
    row_horizon = contract_meta["horizon_seconds"]
    row_max_score_raw = row.get("max_score", _MISSING)
    row_max_score = (
        None
        if row_max_score_raw is _MISSING
        else _integer(row_max_score_raw, "paired_repeat.max_score", minimum=1)
    )
    normalized_arms = {
        policy: _normalize_arm(arms_raw[policy], policy, row_horizon=row_horizon, task_count=len(contract_meta["ordered_task_ids"]), require_history=require_history)
        for policy in POLICIES
    }
    arm_max_scores = {arm["max_score"] for arm in normalized_arms.values()}
    if len(arm_max_scores) != 1:
        raise AllocatorSelectionError(
            "max_score must be identical across all paired arms",
            code="contract_mismatch",
        )
    common_max_score = next(iter(arm_max_scores))
    if row_max_score is not None and row_max_score != common_max_score:
        raise AllocatorSelectionError(
            "paired_repeat.max_score disagrees with arm max_score",
            code="contract_mismatch",
        )
    neutral_parameter_values = {
        policy: canonical_json(arm["allocation_parameters_neutral"])
        for policy, arm in normalized_arms.items()
    }
    if len(set(neutral_parameter_values.values())) != 1:
        raise AllocatorSelectionError(
            "allocation parameters may differ across arms only by policy identity",
            code="config_mismatch",
        )
    # If the producer included registered contrasts, verify the two canonical
    # values instead of trusting a stale precomputed difference.
    contrasts = row.get("registered_contrasts", {})
    if contrasts is not None:
        contrasts_map = _mapping(contrasts, "registered_contrasts")
        for name, left, right in (
            ("trace_state_minus_task_state", "trace_state", "task_state"),
            ("task_state_minus_uniform_refill", "task_state", "uniform_refill"),
            ("trace_state_minus_uniform_refill", "trace_state", "uniform_refill"),
            ("llm_scheduler_minus_trace_state", "llm_scheduler", "trace_state"),
        ):
            if name not in contrasts_map:
                continue
            contrast = _mapping(contrasts_map[name], f"registered_contrasts.{name}")
            for metric in ("nauc", "final_accepted_score"):
                if metric in contrast:
                    value = _finite(contrast[metric], f"registered_contrasts.{name}.{metric}", minimum=-1.0 if metric == "nauc" else None, maximum=1.0 if metric == "nauc" else None)
                    expected = normalized_arms[left]["nauc"] - normalized_arms[right]["nauc"] if metric == "nauc" else normalized_arms[left]["final_accepted_score"] - normalized_arms[right]["final_accepted_score"]
                    if not math.isclose(value, expected, abs_tol=1e-9):
                        raise AllocatorSelectionError(f"registered contrast {name}.{metric} is inconsistent")
    return {
        "schema_version": PAIRED_SCHEMA,
        "paired_repeat_id": repeat_id,
        "paired_seed": seed,
        "comparison_contract": contract,
        "comparison_contract_sha256": contract_hash,
        "contract_meta": contract_meta,
        "contract_normalized": _without_pair_identity(contract),
        "max_score": common_max_score,
        "arms": normalized_arms,
        "raw": row,
    }


def load_paired_repeats(source: str | Path | Iterable[Mapping[str, Any]], *, require_history: bool = True) -> list[dict[str, Any]]:
    """Load and validate paired-repeat rows from JSON, JSONL, or mappings."""

    if require_history is not True:
        raise AllocatorSelectionError(
            "paired-repeat validation always requires accepted score history",
            code="invalid_rule",
        )

    source_name = "<memory>"
    if isinstance(source, (str, Path)):
        path = Path(source)
        source_name = path.name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AllocatorSelectionError("cannot read paired-repeat artifact", code="io_error") from exc
        if path.suffix.lower() == ".json":
            decoded = _loads(text)
            if isinstance(decoded, Mapping):
                decoded = decoded.get("rows", decoded.get("repeats", decoded))
            if not isinstance(decoded, list):
                raise AllocatorSelectionError("JSON paired-repeat artifact must contain a list")
            raw_rows = decoded
        else:
            raw_rows = []
            for line_number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    value = _loads(line)
                except AllocatorSelectionError as exc:
                    raise AllocatorSelectionError(f"invalid paired-repeat JSONL line {line_number}", code=exc.code) from exc
                raw_rows.append(value)
    else:
        raw_rows = list(source)
    rows = [_normalize_row(_mapping(value, "paired repeat"), require_history=require_history) for value in raw_rows]
    if not rows:
        raise AllocatorSelectionError("paired-repeat artifact is empty", code="empty_artifact")
    repeat_ids = [row["paired_repeat_id"] for row in rows]
    seeds = [row["paired_seed"] for row in rows]
    if len(set(repeat_ids)) != len(repeat_ids):
        raise AllocatorSelectionError("duplicate paired_repeat_id", code="duplicate_pair")
    if len(set(seeds)) != len(seeds):
        raise AllocatorSelectionError("duplicate paired_seed", code="duplicate_pair")
    rows.sort(key=lambda item: (item["paired_repeat_id"], item["paired_seed"]))
    baseline_contract = canonical_json(rows[0]["contract_normalized"])
    for row in rows[1:]:
        if canonical_json(row["contract_normalized"]) != baseline_contract:
            raise AllocatorSelectionError("comparison contract differs across paired repeats", code="contract_mismatch")
        if row["max_score"] != rows[0]["max_score"]:
            raise AllocatorSelectionError(
                "max_score differs across paired repeats",
                code="contract_mismatch",
            )
    baseline_neutral_parameters = canonical_json(
        rows[0]["arms"][POLICIES[0]]["allocation_parameters_neutral"]
    )
    for row in rows:
        for policy in POLICIES:
            neutral_parameters = canonical_json(
                row["arms"][policy]["allocation_parameters_neutral"]
            )
            if neutral_parameters != baseline_neutral_parameters:
                raise AllocatorSelectionError(
                    "policy-neutral allocation parameters drift across paired repeats",
                    code="config_mismatch",
                )
    return rows


def parse_rule(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a rule artifact."""

    rule = dict(_mapping(raw, "selection rule"))
    if rule.get("schema_version") != RULE_SCHEMA:
        raise AllocatorSelectionError("unsupported allocator selection rule schema", code="invalid_rule")
    if _text(rule.get("rule_id"), "rule_id") != RULE_ID:
        raise AllocatorSelectionError("unexpected allocator selection rule ID", code="invalid_rule")
    phase = _text(rule.get("phase", "development_validation"), "rule.phase")
    if phase != "development_validation":
        raise AllocatorSelectionError(
            "rule.phase must be development_validation",
            code="invalid_rule",
        )
    if phase.lower() in {"posthoc", "post_hoc", "outcome_tuned"}:
        raise AllocatorSelectionError("post-hoc selection is forbidden", code="invalid_rule")
    if rule.get("posthoc_tuning", False) is not False:
        raise AllocatorSelectionError("posthoc_tuning must be false", code="invalid_rule")
    forbidden = {"formal_repeat_ids", "official_repeat_ids", "paper_claim", "outcome_tuning"}
    if forbidden.intersection(rule):
        raise AllocatorSelectionError("rule contains post-formal selection controls", code="invalid_rule")
    policies = tuple(rule.get("policies", POLICIES))
    if policies != POLICIES:
        raise AllocatorSelectionError(f"rule policy registry must be exactly {POLICIES}", code="invalid_rule")
    metric = dict(_mapping(rule.get("metric"), "rule.metric"))
    if metric.get("name") not in {"fixed_horizon_nauc", "nauc"} or metric.get("field", "nauc") != "nauc" or metric.get("aggregation") != "mean" or metric.get("direction") != "max":
        raise AllocatorSelectionError("rule metric must be mean fixed-horizon nAUC", code="invalid_rule")
    if metric.get("require_history") is not True:
        raise AllocatorSelectionError(
            "rule.metric.require_history must be true",
            code="invalid_rule",
        )
    split = dict(_mapping(rule.get("validation_split"), "rule.validation_split"))
    if split.get("kind") != "paired_repeat_ids":
        raise AllocatorSelectionError("validation split must enumerate paired repeat IDs", code="invalid_rule")
    ids = tuple(_pair_identity(item, "validation_split.paired_repeat_id") for item in split.get("paired_repeat_ids", ()))
    if not ids or len(set(ids)) != len(ids):
        raise AllocatorSelectionError("validation split must contain unique paired repeat IDs", code="invalid_rule")
    minimum = _integer(rule.get("minimum_validation_repeats", MIN_VALIDATION_REPEATS), "minimum_validation_repeats", minimum=MIN_VALIDATION_REPEATS)
    if minimum < MIN_VALIDATION_REPEATS:
        raise AllocatorSelectionError(
            f"minimum_validation_repeats must be at least {MIN_VALIDATION_REPEATS}",
            code="invalid_rule",
        )
    if len(ids) < minimum:
        raise AllocatorSelectionError("validation split is smaller than minimum_validation_repeats", code="invalid_rule")
    target_k = _integer(rule.get("target_k", DEFAULT_TARGET_K), "target_k", minimum=1)
    bootstrap = dict(_mapping(rule.get("bootstrap"), "rule.bootstrap"))
    if bootstrap.get("method") != BOOTSTRAP_METHOD:
        raise AllocatorSelectionError("unsupported bootstrap method", code="invalid_rule")
    draws = _integer(bootstrap.get("draws", DEFAULT_BOOTSTRAP_DRAWS), "bootstrap.draws", minimum=DEFAULT_BOOTSTRAP_DRAWS)
    if draws != DEFAULT_BOOTSTRAP_DRAWS:
        raise AllocatorSelectionError(
            f"bootstrap.draws must be exactly {DEFAULT_BOOTSTRAP_DRAWS}",
            code="invalid_rule",
        )
    seed = _integer(bootstrap.get("seed", DEFAULT_BOOTSTRAP_SEED), "bootstrap.seed", minimum=0)
    confidence = _finite(bootstrap.get("confidence", BOOTSTRAP_CONFIDENCE), "bootstrap.confidence", minimum=0.0, maximum=1.0)
    if confidence != BOOTSTRAP_CONFIDENCE:
        raise AllocatorSelectionError(
            f"bootstrap.confidence must be exactly {BOOTSTRAP_CONFIDENCE}",
            code="invalid_rule",
        )
    if bootstrap.get("quantile", BOOTSTRAP_QUANTILE) != BOOTSTRAP_QUANTILE:
        raise AllocatorSelectionError("bootstrap.quantile must be linear", code="invalid_rule")
    tie_break = list(rule.get("tie_break", ("mean_nauc_desc", "bootstrap_lcb95_desc", "median_time_to_k_asc", "registry_order")))
    expected_tie = ["mean_nauc_desc", "bootstrap_lcb95_desc", "median_time_to_k_asc", "registry_order"]
    if tie_break != expected_tie:
        raise AllocatorSelectionError("tie_break must use the registered deterministic order", code="invalid_rule")
    if "guardrails" not in rule:
        raise AllocatorSelectionError("rule.guardrails must explicitly freeze numeric thresholds", code="invalid_rule")
    guardrail_raw = _mapping(rule.get("guardrails"), "rule.guardrails")
    required_guardrail_aliases = (
        ("scheduler_occupied_capacity_fraction_max", "scheduler_reserved_capacity_fraction_max", "scheduler_slot_seconds_fraction_max", "scheduler_occupied_capacity_fraction"),
        ("scheduler_token_fraction_max", "scheduler_tokens_fraction_max", "scheduler_token_share_max", "scheduler_token_fraction"),
        ("fallback_rate_max", "fallback_fraction_max", "fallback_fraction"),
        ("total_slot_seconds_factor_max", "total_capacity_slot_seconds_factor_max"),
    )
    if any(not any(name in guardrail_raw for name in aliases) for aliases in required_guardrail_aliases):
        raise AllocatorSelectionError("rule.guardrails must explicitly provide all numeric thresholds", code="invalid_rule")
    guardrails = GuardrailConfig.from_mapping(guardrail_raw)
    return {
        "schema_version": RULE_SCHEMA,
        "rule_id": RULE_ID,
        "phase": phase,
        "policies": list(POLICIES),
        "metric": {
            "name": "fixed_horizon_nauc",
            "field": "nauc",
            "aggregation": "mean",
            "direction": "max",
            "require_history": True,
        },
        "validation_split": {"kind": "paired_repeat_ids", "paired_repeat_ids": list(ids)},
        "minimum_validation_repeats": minimum,
        "target_k": target_k,
        "bootstrap": {
            "method": BOOTSTRAP_METHOD,
            "draws": draws,
            "seed": seed,
            "confidence": confidence,
            "quantile": BOOTSTRAP_QUANTILE,
        },
        "tie_break": expected_tie,
        "guardrails": guardrails.as_dict(),
        "posthoc_tuning": False,
    }


def load_rule(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return parse_rule(source)
    path = Path(source)
    try:
        raw = _loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AllocatorSelectionError("cannot read selection rule", code="io_error") from exc
    return parse_rule(_mapping(raw, "selection rule"))


def _quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise AllocatorSelectionError("cannot compute bootstrap interval")
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def _bootstrap(rows: Sequence[dict[str, Any]], *, draws: int, seed: int, confidence: float) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    rng = random.Random(seed)
    n = len(rows)
    samples: dict[str, list[float]] = {policy: [] for policy in POLICIES}
    contrast_samples: dict[str, list[float]] = {"trace_state_minus_task_state": []}
    for _ in range(draws):
        indices = [rng.randrange(n) for _ in range(n)]
        for policy in POLICIES:
            samples[policy].append(math.fsum(rows[index]["arms"][policy]["nauc"] for index in indices) / n)
        contrast_samples["trace_state_minus_task_state"].append(
            math.fsum(rows[index]["arms"]["trace_state"]["nauc"] - rows[index]["arms"]["task_state"]["nauc"] for index in indices) / n
        )
    alpha = (1.0 - confidence) / 2.0
    intervals = {
        policy: {
            "lower": _quantile(values, alpha),
            "upper": _quantile(values, 1.0 - alpha),
        }
        for policy, values in samples.items()
    }
    contrast_intervals = {
        name: {"lower": _quantile(values, alpha), "upper": _quantile(values, 1.0 - alpha)}
        for name, values in contrast_samples.items()
    }
    return intervals, contrast_intervals


def _guardrail_check(policy: str, rows: Sequence[dict[str, Any]], config: GuardrailConfig) -> dict[str, Any]:
    per_repeat: list[dict[str, Any]] = []
    total_budget = 0.0
    total_scheduler_slots = 0.0
    total_solver_slots = 0.0
    total_scheduler_tokens = 0
    total_tokens = 0
    total_fallbacks = 0
    total_decisions = 0
    all_pass = True
    for row in rows:
        arm = row["arms"][policy]
        cost: _Cost = arm["cost"]
        capacity = row["contract_meta"]["total_capacity"]
        horizon = row["contract_meta"]["horizon_seconds"]
        budget = capacity * horizon
        total_budget += budget
        total_scheduler_slots += cost.scheduler_slot_seconds
        total_solver_slots += cost.solver_slot_seconds
        total_scheduler_tokens += cost.scheduler_tokens
        total_tokens += cost.total_tokens
        total_fallbacks += cost.fallback_count
        total_decisions += cost.decisions
        checks: dict[str, dict[str, Any]] = {}
        occupied_fraction = cost.scheduler_slot_seconds / budget
        checks["scheduler_occupied_capacity_fraction"] = {"observed": occupied_fraction, "threshold": config.scheduler_occupied_capacity_fraction_max, "pass": occupied_fraction <= config.scheduler_occupied_capacity_fraction_max}
        token_fraction = cost.scheduler_tokens / cost.total_tokens if cost.total_tokens else (0.0 if cost.scheduler_tokens == 0 else math.inf)
        checks["scheduler_token_fraction"] = {"observed": token_fraction, "threshold": config.scheduler_token_fraction_max, "pass": math.isfinite(token_fraction) and token_fraction <= config.scheduler_token_fraction_max}
        fallback_fraction = cost.fallback_count / cost.decisions if cost.decisions else None
        checks["fallback_rate"] = {"observed": fallback_fraction, "threshold": config.fallback_rate_max, "pass": fallback_fraction is not None and fallback_fraction <= config.fallback_rate_max}
        slot_factor = (cost.solver_slot_seconds + cost.scheduler_slot_seconds) / budget
        checks["total_slot_seconds_factor"] = {"observed": slot_factor, "threshold": config.total_slot_seconds_factor_max, "pass": slot_factor <= config.total_slot_seconds_factor_max}
        observed_slots = cost.max_occupied_slots
        max_threshold = capacity if config.max_occupied_slots is None else min(capacity, config.max_occupied_slots)
        checks["max_occupied_slots"] = {"observed": observed_slots, "threshold": max_threshold, "pass": observed_slots is not None and observed_slots <= max_threshold}
        block_pass = all(bool(item["pass"]) for item in checks.values())
        if policy != "llm_scheduler":
            block_pass = block_pass and all(value == 0 for value in (cost.scheduler_tokens, cost.scheduler_slot_seconds, cost.scheduler_calls, cost.scheduler_reservations, cost.invalid_outputs, cost.fallback_count, cost.scheduler_latency_seconds))
        elif cost.scheduler_calls == 0 and cost.decisions > 0:
            block_pass = False
        per_repeat.append({"paired_repeat_id": row["paired_repeat_id"], "paired_seed": row["paired_seed"], "capacity": capacity, "checks": checks, "pass": block_pass, "cost": cost.as_dict()})
        all_pass = all_pass and (block_pass if config.require_per_block else True)
    aggregate_budget = total_budget
    aggregate_checks: dict[str, dict[str, Any]] = {}
    occupied_fraction = total_scheduler_slots / aggregate_budget
    aggregate_checks["scheduler_occupied_capacity_fraction"] = {"observed": occupied_fraction, "threshold": config.scheduler_occupied_capacity_fraction_max, "pass": occupied_fraction <= config.scheduler_occupied_capacity_fraction_max}
    token_fraction = total_scheduler_tokens / total_tokens if total_tokens else (0.0 if total_scheduler_tokens == 0 else math.inf)
    aggregate_checks["scheduler_token_fraction"] = {"observed": token_fraction, "threshold": config.scheduler_token_fraction_max, "pass": math.isfinite(token_fraction) and token_fraction <= config.scheduler_token_fraction_max}
    fallback_fraction = total_fallbacks / total_decisions if total_decisions else None
    aggregate_checks["fallback_rate"] = {"observed": fallback_fraction, "threshold": config.fallback_rate_max, "pass": fallback_fraction is not None and fallback_fraction <= config.fallback_rate_max}
    slot_factor = (total_solver_slots + total_scheduler_slots) / aggregate_budget
    aggregate_checks["total_slot_seconds_factor"] = {"observed": slot_factor, "threshold": config.total_slot_seconds_factor_max, "pass": slot_factor <= config.total_slot_seconds_factor_max}
    observed_values = [
        (row["cost"]["max_occupied_slots"], row["capacity"])
        for row in per_repeat
        if row["cost"]["max_occupied_slots"] is not None
    ]
    observed = max((value for value, _ in observed_values), default=None)
    capacity_limit = min((capacity for _, capacity in observed_values), default=0)
    aggregate_threshold = capacity_limit if config.max_occupied_slots is None else min(capacity_limit, config.max_occupied_slots)
    aggregate_checks["max_occupied_slots"] = {"observed": observed, "threshold": aggregate_threshold, "pass": observed is not None and observed <= aggregate_threshold}
    aggregate_pass = all(bool(item["pass"]) for item in aggregate_checks.values())
    if policy != "llm_scheduler":
        aggregate_pass = aggregate_pass and all(value == 0 for value in (total_scheduler_tokens, total_scheduler_slots, sum(item["cost"]["scheduler_calls"] for item in per_repeat), sum(item["cost"]["scheduler_reservations"] for item in per_repeat), sum(item["cost"]["invalid_outputs"] for item in per_repeat), total_fallbacks, sum(item["cost"]["scheduler_latency_seconds"] for item in per_repeat)))
    eligible = all_pass and aggregate_pass
    return {"pass": eligible, "per_repeat": per_repeat, "aggregate": {"checks": aggregate_checks, "pass": aggregate_pass, "totals": {"budget_slot_seconds": aggregate_budget, "solver_slot_seconds": total_solver_slots, "scheduler_slot_seconds": total_scheduler_slots, "solver_tokens": total_tokens - total_scheduler_tokens, "scheduler_tokens": total_scheduler_tokens, "fallbacks": total_fallbacks, "decisions": total_decisions}}}


def select_allocator(source: str | Path | Iterable[Mapping[str, Any]], rule: Mapping[str, Any] | str | Path | None = None, *, source_name: str | None = None, require_history: bool | None = None) -> dict[str, Any]:
    """Select one allocator and return a machine-readable artifact.

    ``rule`` is required for a formal invocation.  When omitted, the explicit
    development proposal is used only if the caller supplies validation IDs in
    a mapping; a file/CLI invocation therefore normally passes ``--rule``.
    """

    if rule is None:
        raise AllocatorSelectionError("selection rule is required", code="invalid_rule")
    normalized_rule = load_rule(rule)
    if require_history is not None and require_history is not True:
        raise AllocatorSelectionError(
            "complete score history is mandatory for allocator selection",
            code="invalid_rule",
        )
    rows = load_paired_repeats(source, require_history=True)
    validation_ids = tuple(normalized_rule["validation_split"]["paired_repeat_ids"])
    by_id = {row["paired_repeat_id"]: row for row in rows}
    missing = [item for item in validation_ids if item not in by_id]
    if missing:
        raise AllocatorSelectionError("validation split references missing paired repeats", code="split_invalid")
    validation_rows = [by_id[item] for item in validation_ids]
    if len(validation_rows) < normalized_rule["minimum_validation_repeats"]:
        raise AllocatorSelectionError("not enough complete validation repeats", code="insufficient_validation")
    guardrail_config = GuardrailConfig.from_mapping(normalized_rule["guardrails"])
    smallest_max_score = min(
        row["arms"][policy]["max_score"]
        for row in validation_rows
        for policy in POLICIES
    )
    if normalized_rule["target_k"] > smallest_max_score:
        raise AllocatorSelectionError("target_k exceeds an arm's max_score", code="invalid_rule")
    intervals, contrast_intervals = _bootstrap(
        validation_rows,
        draws=normalized_rule["bootstrap"]["draws"],
        seed=normalized_rule["bootstrap"]["seed"],
        confidence=normalized_rule["bootstrap"]["confidence"],
    )
    arm_results: dict[str, dict[str, Any]] = {}
    eligible: list[str] = []
    target_k = normalized_rule["target_k"]
    for index, policy in enumerate(POLICIES):
        arm_rows = [row["arms"][policy] for row in validation_rows]
        guardrails = _guardrail_check(policy, validation_rows, guardrail_config)
        times: list[float] = []
        for arm in arm_rows:
            value = arm["time_to_k_seconds"].get(str(target_k))
            times.append(float("inf") if value is None else float(value))
        median_time = float(median(times)) if times else float("inf")
        mean_nauc = math.fsum(arm["nauc"] for arm in arm_rows) / len(arm_rows)
        lcb = intervals[policy]["lower"]
        rank_tuple = (-mean_nauc, -lcb, median_time if math.isfinite(median_time) else None, index)
        arm_results[policy] = {
            "policy": policy,
            "repeat_count": len(arm_rows),
            "mean_nauc": mean_nauc,
            "bootstrap_ci95": intervals[policy],
            "median_time_to_k_seconds": None if not math.isfinite(median_time) else median_time,
            "target_k": target_k,
            "allocation_config_sha256": arm_rows[0]["allocation_config_sha256"],
            "allocation_parameters": arm_rows[0]["allocation_parameters"],
            "guardrails": guardrails,
            "eligible": bool(guardrails["pass"]),
            "rank_tuple": list(rank_tuple),
        }
        if guardrails["pass"]:
            eligible.append(policy)
    def rank_key(policy: str) -> tuple[Any, ...]:
        value = arm_results[policy]["rank_tuple"]
        return (value[0], value[1], float("inf") if value[2] is None else value[2], value[3])

    ranked = sorted(eligible, key=rank_key)
    selected_policy = ranked[0] if ranked else None
    normalized_source = [{
        "paired_repeat_id": row["paired_repeat_id"],
        "paired_seed": row["paired_seed"],
        "comparison_contract": row["comparison_contract"],
        "comparison_contract_sha256": row["comparison_contract_sha256"],
        "arms": {policy: row["raw"]["arms"][policy] for policy in POLICIES},
    } for row in rows]
    source_hash = canonical_sha256(normalized_source)
    rule_hash = canonical_sha256(normalized_rule)
    selected = None
    if selected_policy is not None:
        selected = {
            "policy": selected_policy,
            "allocation_config_sha256": arm_results[selected_policy]["allocation_config_sha256"],
            "allocation_parameters": arm_results[selected_policy]["allocation_parameters"],
        }
    result: dict[str, Any] = {
        "schema_version": SELECTION_SCHEMA,
        "status": "selected" if selected_policy else "no_selection",
        "rule_id": normalized_rule["rule_id"],
        "rule_sha256": rule_hash,
        "selection_phase": normalized_rule["phase"],
        "source_artifact_basename": Path(source_name or (source if isinstance(source, (str, Path)) else "paired_repeats.jsonl")).name,
        "source_artifact_sha256": source_hash,
        "validation_split": normalized_rule["validation_split"],
        "validation_repeat_ids": list(validation_ids),
        "validation_paired_seeds": [row["paired_seed"] for row in validation_rows],
        "metric": normalized_rule["metric"],
        "target_k": target_k,
        "bootstrap": normalized_rule["bootstrap"],
        "tie_break": normalized_rule["tie_break"],
        "guardrails": normalized_rule["guardrails"],
        "arms": arm_results,
        "eligible_arms": ranked,
        "selected": selected,
        "selected_policy": selected_policy,
        "no_selection_reason": None if selected_policy else "no_arm_passed_numeric_guardrails",
        "paired_contrasts": {
            "trace_state_minus_task_state": {
                "mean": math.fsum(row["arms"]["trace_state"]["nauc"] - row["arms"]["task_state"]["nauc"] for row in validation_rows) / len(validation_rows),
                "bootstrap_ci95": contrast_intervals["trace_state_minus_task_state"],
            }
        },
        "posthoc_tuning": False,
    }
    if source_name is None and isinstance(source, (str, Path)):
        result["source_artifact_basename"] = Path(source).name
    return result


def write_selection_result(path: str | Path, result: Mapping[str, Any]) -> None:
    """Atomically write a previously generated selection result."""

    from .artifacts import atomic_write_json

    if result.get("schema_version") != SELECTION_SCHEMA:
        raise AllocatorSelectionError("invalid selection result schema")
    atomic_write_json(Path(path), dict(result))


__all__ = [
    "AllocatorSelectionError",
    "GuardrailConfig",
    "PAIRED_SCHEMA",
    "POLICIES",
    "REGISTRY_ORDER",
    "RULE_ID",
    "RULE_SCHEMA",
    "SELECTION_SCHEMA",
    "canonical_json",
    "canonical_sha256",
    "development_rule",
    "load_paired_repeats",
    "load_rule",
    "parse_rule",
    "select_allocator",
    "write_selection_result",
]
