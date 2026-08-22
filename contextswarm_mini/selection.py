"""Common, deterministic contract for Issue #38 trace selectors.

The runtime integration is intentionally kept outside this module.  Selectors
consume :class:`SelectionSnapshot` and return ranked rows; this module owns the
parts that must be byte-identical across arms (identity hashes, seed
derivation, tie handling, and token packing).  It also contains the tiny
Random/Recency policies and a lazy registry so policy modules can be developed
independently without importing CPS or the runner.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypeVar


JSONValue = Any


# Common comparison fields used by the simple policies.  Keep these values in
# one place: Random's policy parameters declare only *that* sampling is without
# replacement, while seed derivation and deterministic ties belong to the
# shared selector contract rather than to an arm-specific policy.
PAIRED_SEED_DERIVATION = "paired_seed_task_episode_ordinal_trace_v1"
TRACE_ID_ASC_TIE_BREAK = "trace_id_asc"
RECENCY_PRIMARY_SORT = "commit_seq_desc"


def _canonical(value: Any) -> Any:
    """Convert supported values into a stable JSON-compatible tree."""

    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON cannot contain non-finite numbers")
        return value
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return canonical compact JSON suitable for config/artifact hashing."""

    return json.dumps(
        _canonical(value), ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class SelectionConfig:
    """Pure-selector configuration.

    ``parameters`` is intentionally explicit.  Policy code may not silently
    add comparison-relevant numeric defaults when an official manifest is
    loaded.  The defaults below are only structural values that make fixtures
    convenient; formal config validation happens in ``config.py``.
    """

    contract_version: str = "selection-v1"
    selector_name: str = ""
    selector_version: str = ""
    parameters: Mapping[str, JSONValue] = field(default_factory=dict)
    eligible_scope: str = "project_shared"
    eligible_kinds: tuple[str, ...] = ()
    eligible_lifecycles: tuple[str, ...] = ()
    tokenizer_id: str = ""
    tokenizer_parameters: Mapping[str, JSONValue] = field(default_factory=dict)
    max_items: int = 0
    context_token_budget: int = 0
    score_precision: int = 8
    tie_break: str = TRACE_ID_ASC_TIE_BREAK
    seed_derivation: str = PAIRED_SEED_DERIVATION
    selector_config_id: str = ""
    comparison_contract_id: str = ""

    def __post_init__(self) -> None:
        if self.max_items < 0 or self.context_token_budget < 0:
            raise ValueError("selection limits must not be negative")
        if self.score_precision < 0:
            raise ValueError("score_precision must not be negative")
        if self.eligible_scope not in {"project_shared", "task_family", "task"}:
            raise ValueError("unsupported eligible_scope")

    @property
    def policy_params(self) -> Mapping[str, JSONValue]:
        """Alias matching the manifest terminology."""

        return self.parameters

    def canonical_identity(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "selector_name": self.selector_name,
            "selector_version": self.selector_version,
            "parameters": self.parameters,
            "eligible_scope": self.eligible_scope,
            "eligible_kinds": self.eligible_kinds,
            "eligible_lifecycles": self.eligible_lifecycles,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_parameters": self.tokenizer_parameters,
            "max_items": self.max_items,
            "context_token_budget": self.context_token_budget,
            "score_precision": self.score_precision,
            "tie_break": self.tie_break,
            "seed_derivation": self.seed_derivation,
        }

    @property
    def computed_config_id(self) -> str:
        return sha256_hex(self.canonical_identity())


@dataclass(frozen=True)
class SelectionRequest:
    run_id: str = ""
    request_key: str = ""
    actor_id: str = ""
    task_id: str = ""
    task_family: str = ""
    query: str = ""
    episode: int = 0
    search_ordinal: int = 0
    paired_seed: int = 0
    max_items: int = 0
    context_token_budget: int = 0
    selector_config_id: str = ""
    comparison_contract_id: str = ""

    def __post_init__(self) -> None:
        """Validate the request fields that participate in the paired contract.

        A request is part of the snapshot identity and is consequently not a
        convenient place for implicit ``int(...)`` coercions.  In particular,
        accepting ``True`` as seed ``1`` would silently produce a different
        treatment assignment, while accepting a negative ordinal would make
        replay/order checks ambiguous.  Zero remains valid for the lightweight
        pure-selector fixtures; runtime manifests impose positive packing
        limits before constructing a request.
        """

        for name in ("episode", "search_ordinal", "paired_seed",
                     "max_items", "context_token_budget"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"selection request {name} must be an integer")
            if value < 0:
                raise ValueError(f"selection request {name} must not be negative")


@dataclass(frozen=True)
class FeedbackStats:
    exposure_count: int = 0
    effective_terminal_count: int = 0
    kind_counts: Mapping[str, int] = field(default_factory=dict)
    signed_weight_sum: float = 0.0
    positive_count: int = 0
    negative_count: int = 0

    @property
    def effective_exposures(self) -> int:
        return int(self.exposure_count)


@dataclass(frozen=True)
class TraceCandidate:
    trace_id: str
    source_task_id: str = ""
    task_family: str = ""
    author_id: str = ""
    scope_key: str = ""
    visibility: str = "project_shared"
    kind: str = ""
    title: str = ""
    body: str = ""
    tags: tuple[str, ...] = ()
    created_at: str = ""
    commit_seq: int = 0
    lifecycle: str = "active"
    cluster_id: str = ""
    content_sha256: str = ""
    token_count: int = 0
    evidence: Mapping[str, JSONValue] = field(default_factory=dict)
    relations: Mapping[str, int] = field(default_factory=dict)
    feedback: FeedbackStats = field(default_factory=FeedbackStats)

    @property
    def id(self) -> str:
        return self.trace_id

    @property
    def task_id(self) -> str:
        return self.source_task_id


@dataclass(frozen=True)
class SelectionSnapshot:
    request: SelectionRequest = field(default_factory=SelectionRequest)
    snapshot_event_seq: int = 0
    eligible: tuple[TraceCandidate, ...] = ()
    eligible_pool_sha256: str = ""
    snapshot_sha256: str = ""

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.eligible, key=lambda item: str(item.trace_id)))
        if ordered != self.eligible:
            object.__setattr__(self, "eligible", ordered)
        if not self.eligible_pool_sha256:
            object.__setattr__(
                self, "eligible_pool_sha256",
                eligible_pool_sha256(ordered),
            )
        if not self.snapshot_sha256:
            object.__setattr__(
                self, "snapshot_sha256",
                snapshot_sha256(self.request, self.snapshot_event_seq, ordered),
            )

    @property
    def candidates(self) -> tuple[TraceCandidate, ...]:
        return self.eligible


@dataclass(frozen=True)
class RankedTrace:
    trace_id: str
    rank: int = 0
    total_score: float = 0.0
    component_scores: Mapping[str, float] = field(default_factory=dict)
    tie_key: str = ""
    token_count: int = 0
    selected: bool = True
    drop_reason: str = ""
    candidate: Any = None


@dataclass(frozen=True)
class SelectionResult:
    search_event_id: str = ""
    exposure_id: str = ""
    request_key: str = ""
    selector_config_id: str = ""
    comparison_contract_id: str = ""
    snapshot_event_seq: int = 0
    eligible_pool_sha256: str = ""
    snapshot_sha256: str = ""
    ranked: tuple[RankedTrace, ...] = ()
    exposure_item_ids: tuple[str, ...] = ()
    delivered_tokens: int = 0
    latency_seconds: float = 0.0


def eligible_pool_sha256(candidates: Sequence[Any]) -> str:
    rows = []
    for candidate in sorted(candidates, key=lambda item: str(_get(item, "trace_id", _get(item, "id", "")))):
        rows.append({
            "trace_id": _get(candidate, "trace_id", _get(candidate, "id", "")),
            "source_task_id": _get(candidate, "source_task_id", _get(candidate, "task_id", "")),
            "scope_key": _get(candidate, "scope_key", ""),
            "visibility": _get(candidate, "visibility", ""),
            "kind": _get(candidate, "kind", ""),
            "lifecycle": _get(candidate, "lifecycle", ""),
        })
    return sha256_hex(rows)


def snapshot_sha256(request: Any, watermark: int, candidates: Sequence[Any]) -> str:
    return sha256_hex({
        "request": request,
        "watermark": int(watermark),
        "eligible": [asdict(item) if is_dataclass(item) else item for item in candidates],
    })


def derive_seed(
    paired_seed: int,
    *,
    task_id: str = "",
    actor_episode_key: str = "",
    episode: int = 0,
    search_ordinal: int = 0,
    trace_id: str = "",
) -> str:
    """Derive the paired random-ranking key.

    The key intentionally excludes run, selector, and actor identity.  Those
    values can change when the same paired task is scheduled on a different
    worker or arm, and including them would make a purported paired repeat use
    different random draws.  ``actor_episode_key`` is retained as a deprecated
    keyword for source compatibility with early development callers; it is
    deliberately ignored by this v1 derivation.
    """

    for name, value in (("paired_seed", paired_seed), ("episode", episode),
                        ("search_ordinal", search_ordinal)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if value < 0:
            raise ValueError(f"{name} must not be negative")
    return hashlib.sha256(canonical_json_bytes({
        "version": PAIRED_SEED_DERIVATION,
        "paired_seed": paired_seed,
        "task_id": str(task_id),
        "episode": episode,
        "search_ordinal": search_ordinal,
        "trace_id": str(trace_id),
    })).hexdigest()


def stable_tiebreak_key(trace_id: str, *, score: float | None = None, descending_score: bool = True) -> tuple[Any, ...]:
    if score is None:
        return (str(trace_id),)
    value = float(score)
    return ((-value if descending_score else value), str(trace_id))


def token_count(value: Any, *, estimator: str = "utf8_bytes_ceil_div4_v1") -> int:
    if isinstance(value, TraceCandidate):
        if value.token_count > 0:
            return int(value.token_count)
        value = " ".join((value.title, value.body, " ".join(value.tags)))
    if isinstance(value, Mapping):
        if value.get("token_count") is not None:
            try:
                count = int(value["token_count"])
                if count >= 0:
                    return count
            except (TypeError, ValueError):
                pass
        value = " ".join(str(value.get(key, "")) for key in ("title", "body", "content"))
    if estimator != "utf8_bytes_ceil_div4_v1":
        raise ValueError(f"unsupported token estimator: {estimator}")
    text = str(value or "")
    return (len(text.encode("utf-8")) + 3) // 4


def _frozen_token_count(candidate: Any) -> int:
    """Read the manifest-frozen candidate token count without coercion."""

    raw = _get(candidate, "token_count", 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError("candidate token_count must be a non-negative integer")
    return raw


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def as_ranked_trace(value: Any, *, fallback_rank: int = 0) -> RankedTrace:
    if isinstance(value, RankedTrace):
        return value
    trace_id = str(_get(value, "trace_id", _get(value, "id", "")))
    if not trace_id:
        raise ValueError("ranked row requires trace_id")
    candidate = _get(value, "candidate", None)
    return RankedTrace(
        trace_id=trace_id,
        rank=int(_get(value, "rank", fallback_rank)),
        total_score=float(_get(value, "total_score", _get(value, "score", 0.0))),
        component_scores=dict(_get(value, "component_scores", _get(value, "components", {})) or {}),
        tie_key=str(_get(value, "tie_key", trace_id)),
        token_count=int(_get(value, "token_count", token_count(candidate or value))),
        selected=bool(_get(value, "selected", True)),
        drop_reason=str(_get(value, "drop_reason", "")),
        candidate=candidate,
    )


def ranked_trace_dict(value: RankedTrace | Any) -> dict[str, Any]:
    row = as_ranked_trace(value)
    result = {
        "trace_id": row.trace_id,
        "rank": row.rank,
        "total_score": row.total_score,
        "component_scores": dict(row.component_scores),
        "tie_key": row.tie_key,
        "token_count": row.token_count,
        "selected": row.selected,
        "drop_reason": row.drop_reason,
    }
    if row.candidate is not None:
        result["candidate"] = row.candidate
    return result


def pack_ranked_by_token_budget(
    ranked: Sequence[Any],
    *,
    max_items: int,
    context_token_budget: int,
    estimator: str = "utf8_bytes_ceil_div4_v1",
) -> tuple[RankedTrace, ...]:
    """Apply common hard limits while retaining every audit row.

    ``selected`` on the selector output is the policy eligibility boundary.
    The common packer may remove policy-selected rows to satisfy the shared
    item and token limits, but it must never re-admit a row the policy already
    rejected.  Policy drop reasons are consequently preserved verbatim.
    """

    if max_items < 0 or context_token_budget < 0:
        raise ValueError("packing limits must not be negative")
    rows = [as_ranked_trace(value, fallback_rank=index + 1) for index, value in enumerate(ranked)]
    output: list[RankedTrace] = []
    used = 0
    selected_count = 0
    for row in rows:
        count = row.token_count
        if count <= 0:
            count = token_count(row.candidate if row.candidate is not None else row, estimator=estimator)
        selected = False
        reason = row.drop_reason
        if not row.selected:
            # Selector-level exclusions are authoritative.  They neither
            # consume shared packing capacity nor become eligible merely
            # because earlier rows exceeded a common limit.
            pass
        elif selected_count >= max_items:
            reason = reason or "max_items"
        elif used + count > context_token_budget:
            reason = reason or "token_budget"
        else:
            selected = True
            selected_count += 1
            used += count
            reason = ""
        output.append(RankedTrace(
            trace_id=row.trace_id, rank=row.rank, total_score=row.total_score,
            component_scores=row.component_scores, tie_key=row.tie_key,
            token_count=count, selected=selected, drop_reason=reason,
            candidate=row.candidate,
        ))
    return tuple(output)


def pack_ranked(*args: Any, **kwargs: Any) -> tuple[RankedTrace, ...]:
    return pack_ranked_by_token_budget(*args, **kwargs)


pack_selection = pack_ranked_by_token_budget


def make_snapshot(
    request: SelectionRequest,
    candidates: Sequence[TraceCandidate],
    *,
    snapshot_event_seq: int = 0,
) -> SelectionSnapshot:
    """Canonicalize an already-filtered project-visible candidate pool."""

    return SelectionSnapshot(
        request=request,
        snapshot_event_seq=snapshot_event_seq,
        eligible=tuple(sorted(candidates, key=lambda item: item.trace_id)),
    )


class Selector(Protocol):
    def rank(self, snapshot: SelectionSnapshot) -> Sequence[Any]: ...


def _request_value(snapshot: Any, name: str, default: Any = None) -> Any:
    request = _get(snapshot, "request", snapshot)
    return _get(request, name, default)


def _request_counter(snapshot: Any, name: str, default: int = 0) -> int:
    """Read a non-negative integer from typed or mapping request fixtures.

    Selector tests intentionally accept lightweight mapping snapshots, so the
    dataclass-level validation on :class:`SelectionRequest` is not sufficient
    on its own.  Keeping this check at the policy boundary prevents a mapping
    value such as ``True`` or ``1.5`` from being silently coerced by ``int``.
    """

    value = _request_value(snapshot, name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"selection request {name} must be an integer")
    if value < 0:
        raise ValueError(f"selection request {name} must not be negative")
    return value


class SimpleSelectorConfigError(ValueError):
    """Random/Recency configuration is incomplete or contradictory."""


_MISSING = object()


def _config_value(config: Any, name: str) -> Any:
    if isinstance(config, Mapping):
        return config[name] if name in config else _MISSING
    return getattr(config, name, _MISSING)


def _simple_policy_params(config: Any, *, selector_name: str) -> Mapping[str, Any]:
    """Extract a simple policy's explicit parameter mapping.

    Runtime adapters use ``{"parameters": policy_params}``, the manifest
    dataclass exposes ``policy_params``, and pure-selector fixtures sometimes
    pass the mapping directly.  Empty configurations remain accepted for
    legacy/unit fixtures; once any policy parameter is supplied the complete,
    exact schema is enforced below by the policy constructor.
    """

    if config is None:
        return {}
    if isinstance(config, Mapping):
        has_parameters = "parameters" in config
        has_policy_params = "policy_params" in config
        if has_parameters and has_policy_params:
            raise SimpleSelectorConfigError(
                f"{selector_name} config must not define both parameters and policy_params"
            )
        if has_parameters:
            params = config["parameters"]
        elif has_policy_params:
            params = config["policy_params"]
        else:
            params = config
    else:
        parameters = getattr(config, "parameters", _MISSING)
        policy_params = getattr(config, "policy_params", _MISSING)
        if parameters is not _MISSING and policy_params is not _MISSING:
            if parameters != policy_params:
                raise SimpleSelectorConfigError(
                    f"{selector_name} config has contradictory parameters and policy_params"
                )
            params = parameters
        elif parameters is not _MISSING:
            params = parameters
        elif policy_params is not _MISSING:
            params = policy_params
        else:
            raise SimpleSelectorConfigError(
                f"{selector_name} parameters must be a mapping"
            )
    if not isinstance(params, Mapping):
        raise SimpleSelectorConfigError(
            f"{selector_name} parameters must be a mapping"
        )
    if any(not isinstance(key, str) or not key for key in params):
        raise SimpleSelectorConfigError(
            f"{selector_name} parameter names must be non-empty strings"
        )
    return dict(params)


def _is_formal_simple_config(config: Any) -> bool:
    """Whether ``config`` is the enabled manifest wrapper.

    Bare ``None``/``{}`` and the historical lightweight ``parameters`` adapter
    are intentionally accepted by unit fixtures.  The parsed manifest carries
    an explicit ``enabled = true`` field; that marker lets us require every
    policy parameter without breaking those legacy fixtures.
    """

    enabled = _config_value(config, "enabled")
    return enabled is True


def _require_exact_policy_schema(
    params: Mapping[str, Any],
    *,
    selector_name: str,
    required: frozenset[str],
    formal: bool = False,
) -> None:
    # Empty configs are a compatibility surface for pure selector fixtures.
    # Formal Figure 3 wrappers always supply the explicit policy field.
    if not params and not formal:
        return
    actual = frozenset(params)
    if actual != required:
        missing = sorted(required - actual)
        unknown = sorted(actual - required)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise SimpleSelectorConfigError(
            f"{selector_name} parameters must use the exact explicit schema"
            + (": " + "; ".join(details) if details else "")
        )


def _validate_common_simple_fields(
    config: Any, *, random_policy: bool, formal: bool = False
) -> None:
    tie_break = _config_value(config, "tie_break")
    if formal and tie_break is _MISSING:
        raise SimpleSelectorConfigError(
            "formal simple selector config must declare tie_break"
        )
    if tie_break is not _MISSING and tie_break != TRACE_ID_ASC_TIE_BREAK:
        raise SimpleSelectorConfigError(
            f"tie_break must be {TRACE_ID_ASC_TIE_BREAK}"
        )
    if random_policy:
        seed_derivation = _config_value(config, "seed_derivation")
        if (
            seed_derivation is not _MISSING
            and seed_derivation != PAIRED_SEED_DERIVATION
        ):
            raise SimpleSelectorConfigError(
                f"seed_derivation must be {PAIRED_SEED_DERIVATION}"
            )


class RandomSelector:
    name = "random"

    def __init__(self, config: Any = None):
        self.config = config
        self.parameters = _simple_policy_params(config, selector_name=self.name)
        _require_exact_policy_schema(
            self.parameters,
            selector_name=self.name,
            required=frozenset({"sample_without_replacement"}),
            formal=_is_formal_simple_config(config),
        )
        if self.parameters and self.parameters["sample_without_replacement"] is not True:
            raise SimpleSelectorConfigError(
                "random.sample_without_replacement must be true"
            )
        _validate_common_simple_fields(
            config, random_policy=True, formal=_is_formal_simple_config(config)
        )

    def rank(self, snapshot: Any) -> tuple[dict[str, Any], ...]:
        candidates = list(_get(snapshot, "eligible", _get(snapshot, "candidates", ())))
        paired_seed = _request_counter(snapshot, "paired_seed")
        task_id = str(_request_value(snapshot, "task_id", ""))
        episode = _request_counter(snapshot, "episode")
        ordinal = _request_counter(snapshot, "search_ordinal")
        keyed = []
        seen: set[str] = set()
        for candidate in candidates:
            trace = str(_get(candidate, "trace_id", _get(candidate, "id", "")))
            if not trace:
                raise ValueError("every candidate needs trace_id")
            if trace in seen:
                raise ValueError("random eligible pool requires unique trace_id values")
            seen.add(trace)
            # Actor/run identity is deliberately absent: paired arms may use
            # different worker IDs and must still see the same random order.
            key = derive_seed(
                paired_seed, task_id=task_id, episode=episode,
                search_ordinal=ordinal, trace_id=trace,
            )
            keyed.append((key, trace, candidate, _frozen_token_count(candidate)))
        keyed.sort(key=lambda item: (item[0], item[1]))
        return tuple({"candidate": candidate, "trace_id": trace, "rank": rank,
                      "total_score": 0.0, "component_scores": {"random_key": key},
                      "tie_key": trace, "token_count": tokens,
                      "selected": True, "drop_reason": ""}
                     for rank, (key, trace, candidate, tokens) in enumerate(keyed, 1))

    __call__ = rank


class RecencySelector:
    name = "recency"

    def __init__(self, config: Any = None):
        self.config = config
        self.parameters = _simple_policy_params(config, selector_name=self.name)
        _require_exact_policy_schema(
            self.parameters,
            selector_name=self.name,
            required=frozenset({"primary_sort"}),
            formal=_is_formal_simple_config(config),
        )
        if self.parameters and self.parameters["primary_sort"] != RECENCY_PRIMARY_SORT:
            raise SimpleSelectorConfigError(
                f"recency.primary_sort must be {RECENCY_PRIMARY_SORT}"
            )
        _validate_common_simple_fields(
            config, random_policy=False, formal=_is_formal_simple_config(config)
        )

    def rank(self, snapshot: Any) -> tuple[dict[str, Any], ...]:
        candidates = list(_get(snapshot, "eligible", _get(snapshot, "candidates", ())))
        rows = []
        seen: set[str] = set()
        for candidate in candidates:
            trace = str(_get(candidate, "trace_id", _get(candidate, "id", "")))
            if not trace:
                raise ValueError("every candidate needs trace_id")
            if trace in seen:
                raise ValueError("eligible pool requires unique trace_id values")
            seen.add(trace)
            sequence = _get(candidate, "commit_seq", 0)
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise ValueError("commit_seq must be an integer")
            if sequence < 0:
                raise ValueError("commit_seq must not be negative")
            rows.append((candidate, trace, sequence, _frozen_token_count(candidate)))
        rows.sort(key=lambda item: (-item[2], item[1]))
        return tuple({"candidate": candidate, "trace_id": trace, "rank": rank,
                      "total_score": float(sequence), "component_scores": {"commit_seq": float(sequence)},
                      "tie_key": trace, "token_count": tokens,
                      "selected": True, "drop_reason": ""}
                     for rank, (candidate, trace, sequence, tokens) in enumerate(rows, 1))

    __call__ = rank


def selector_registry() -> dict[str, type[Any]]:
    """Return the eight registered policies, importing optional modules lazily."""

    from .selectors.feedback import (
        FeedbackDiversitySelector, NoInteractionSelector, NuStigmergySelector,
        UnnormalizedSelector,
    )
    from .selectors.popularity import SmoothedPopularitySelector
    from .selectors.text import BM25MMRSelector

    return {
        "random": RandomSelector,
        "recency": RecencySelector,
        "bm25_mmr": BM25MMRSelector,
        "smoothed_popularity": SmoothedPopularitySelector,
        "feedback_diversity": FeedbackDiversitySelector,
        "no_interaction_feedback": NoInteractionSelector,
        "unnormalized_feedback": UnnormalizedSelector,
        "nustigmergy": NuStigmergySelector,
    }


def build_selector(name: str, config: Any) -> Selector:
    registry = selector_registry()
    try:
        cls = registry[str(name)]
    except KeyError as exc:
        raise ValueError(f"unknown selector: {name}") from exc
    return cls(config)


__all__ = [
    "FeedbackStats", "JSONValue", "PAIRED_SEED_DERIVATION", "RECENCY_PRIMARY_SORT",
    "RankedTrace", "RandomSelector", "RecencySelector", "SelectionConfig",
    "SelectionRequest", "SelectionResult", "SimpleSelectorConfigError",
    "SelectionSnapshot", "Selector", "TraceCandidate", "as_ranked_trace",
    "TRACE_ID_ASC_TIE_BREAK",
    "build_selector", "canonical_json", "canonical_json_bytes", "derive_seed",
    "eligible_pool_sha256", "make_snapshot", "pack_ranked", "pack_ranked_by_token_budget",
    "pack_selection", "ranked_trace_dict", "selector_registry", "sha256_hex",
    "snapshot_sha256", "stable_tiebreak_key", "token_count",
]
