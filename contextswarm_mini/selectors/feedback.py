"""Feedback--diversity and Nu-stigmergy selectors.

The selectors in this module are deliberately pure.  A snapshot is a mapping
or an object with an ``eligible``/``candidates`` collection.  Candidate and
feedback records are read only; no worker, verifier, or maintenance state is
mutated.  The three Nu profiles are thin views over one scorer, so that their
only treatment difference is the interaction term and its denominator.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from typing import Any, NamedTuple


class SelectorConfigError(ValueError):
    """A required, comparison-relevant selector parameter is invalid."""


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _params(config: Any) -> Mapping[str, Any]:
    params = _get(config, "parameters", _get(config, "policy_params", config))
    if not isinstance(params, Mapping):
        raise SelectorConfigError("selector parameters must be a mapping")
    return params


def _num(params: Mapping[str, Any], key: str, default: float | None = None,
         *, lo: float | None = None) -> float:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectorConfigError(f"{key} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or (lo is not None and value < lo):
        raise SelectorConfigError(f"{key} must be a finite number >= {lo}")
    return value


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class FeatureVector(NamedTuple):
    relevance: float = 0.0
    evidence: float = 0.0
    interaction: float = 0.0
    structure: float = 0.0
    state: float = 0.0
    cluster: str = ""
    lineage: str = ""
    exposure: float = 0.0


def _feedback_items(candidate: Any) -> Sequence[Any]:
    """Return an explicit event sequence, if one is present.

    ``feedback`` is also used by the runtime for an aggregate ``FeedbackStats``
    object, so it is deliberately checked here only when it is a sequence.
    Aggregate projections are handled separately by :func:`_worker_feedback`.
    """

    for key in ("worker_feedback", "feedback", "feedback_events", "interactions"):
        value = _get(candidate, key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return value
    return ()


def _worker_feedback(
    candidate: Any,
    *,
    feedback_values: Mapping[str, float] | None = None,
) -> tuple[float, int, float]:
    """Return (signed sum, effective worker count, effective exposure count).

    Verifier and maintenance records are intentionally ignored.  ``exposure``
    is read only from effective worker feedback records (or the explicit worker
    exposure field), never from lifecycle/evaluator records.
    """
    total = count = exposures = 0.0
    events = _feedback_items(candidate)
    for item in events:
        # Verifier/evaluator and maintenance rows are intentionally excluded.
        # A few callers expose ``actor_id``/``event_class`` rather than the
        # compact fixture spelling, so accept both without treating an absent
        # actor as a non-worker event.
        event_class = str(_get(item, "event_class", "")).lower()
        if event_class and event_class not in {
            "worker_interaction", "worker", "interaction"
        }:
            continue
        actor = str(
            _get(
                item,
                "actor",
                _get(item, "actor_id", _get(item, "source", "worker")),
            )
        ).lower()
        if actor not in {"worker", "workers", "solver", "agent", "human"}:
            continue
        if _get(item, "terminal", True) is False or _get(item, "effective", True) is False:
            continue
        raw = _get(item, "value", _get(item, "score", _get(item, "signal", 0.0)))
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            continue
        total += float(raw)
        count += 1.0
        ex = _get(item, "exposure", _get(item, "weight", 1.0))
        if isinstance(ex, (int, float)) and not isinstance(ex, bool) and math.isfinite(float(ex)):
            exposures += max(0.0, float(ex))

    # Runtime snapshots normally expose ``worker_feedback`` above.  Pure
    # selector callers often provide the compact aggregate ``FeedbackStats``
    # mapping/object instead; consume its signed projection without ever
    # reading verifier or maintenance tables.  If only kind counts are
    # available, the manifest's explicit feedback-value map supplies the
    # deterministic weights.
    aggregate = _get(candidate, "feedback", None)
    if not events and aggregate is not None:
        signed = _get(aggregate, "signed_weight_sum", None)
        if isinstance(signed, (int, float)) and not isinstance(signed, bool) and math.isfinite(float(signed)):
            total = float(signed)
        elif feedback_values:
            kind_counts = _get(aggregate, "kind_counts", {})
            if isinstance(kind_counts, Mapping):
                for kind, raw_count in kind_counts.items():
                    weight = feedback_values.get(str(kind))
                    if weight is None:
                        continue
                    if isinstance(raw_count, bool) or not isinstance(raw_count, (int, float)):
                        continue
                    if not math.isfinite(float(raw_count)):
                        continue
                    total += float(weight) * max(0.0, float(raw_count))
        raw_count = _get(
            aggregate,
            "effective_terminal_count",
            _get(aggregate, "count", 0),
        )
        if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool) and math.isfinite(float(raw_count)):
            count = max(0.0, float(raw_count))
        raw_exposure = _get(
            aggregate,
            "exposure_count",
            _get(aggregate, "effective_exposures", 0),
        )
        if isinstance(raw_exposure, (int, float)) and not isinstance(raw_exposure, bool) and math.isfinite(float(raw_exposure)):
            exposures = max(0.0, float(raw_exposure))
    explicit = _get(candidate, "worker_exposure", _get(candidate, "effective_exposures", None))
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool) and math.isfinite(float(explicit)):
        exposures = max(exposures, float(explicit))
    return total, int(count), exposures


def extract_features(
    candidate: Any,
    *,
    interaction_mode: str = "nu",
    kappa: float = 1.0,
    feedback_values: Mapping[str, float] | None = None,
) -> FeatureVector:
    """Extract the common, fixed feature vector from a candidate."""
    rel = _get(candidate, "relevance", _get(candidate, "relevance_score", 0.0))
    evidence = _get(candidate, "evidence", _get(candidate, "evidence_score", 0.0))
    structure = _get(candidate, "structure", _get(candidate, "structure_score", 0.0))
    state = _get(candidate, "state", _get(candidate, "state_score", 0.0))
    def finite(v: Any) -> float:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) else 0.0
    signed, _, exposures = _worker_feedback(candidate, feedback_values=feedback_values)
    if interaction_mode == "none":
        interaction = 0.0
    elif interaction_mode == "unnormalized":
        # This arm is intentionally the no-denominator ablation.  Do not clip
        # repeated typed feedback here: doing so would make it exposure-aware
        # in disguise and would prevent the Nu-vs-unnormalized contrast from
        # being interpreted as a denominator effect.
        interaction = signed
    elif interaction_mode == "nu":
        denominator = kappa + exposures
        if denominator <= 0.0:
            raise SelectorConfigError("kappa + effective exposure must be positive")
        interaction = signed / denominator
    else:
        raise SelectorConfigError("unknown interaction mode")
    trace_id = str(_get(candidate, "trace_id", _get(candidate, "id", "")) or "")
    cluster = str(_get(candidate, "cluster", _get(candidate, "cluster_id", "")) or "")
    lineage = str(_get(candidate, "lineage", _get(candidate, "lineage_id", "")) or "")
    # Missing topology metadata is a singleton, not an invitation to infer a
    # group from text/title/body.  This keeps diversity deterministic and
    # prevents unrelated traces from being silently collapsed together.
    cluster = cluster or trace_id
    lineage = lineage or trace_id
    return FeatureVector(finite(rel), finite(evidence), interaction, finite(structure), finite(state),
                         cluster, lineage, exposures)


class _NuScorer:
    mode = "nu"
    profile = "nu_stigmergy"

    def __init__(self, config: Any):
        p = _params(config)
        self.parameters = dict(p)
        self.weights = tuple(_num(p, key, 0.0) for key in ("alpha", "beta", "gamma", "delta", "eta"))
        if "weights" in p:
            weights = p["weights"]
            if not isinstance(weights, Mapping): raise SelectorConfigError("weights must be a mapping")
            expected_weights = {"relevance", "evidence", "interaction", "structure", "state"}
            actual_weights = {str(key) for key in weights}
            if actual_weights != expected_weights:
                missing = sorted(expected_weights - actual_weights)
                unknown = sorted(actual_weights - expected_weights)
                detail = []
                if missing:
                    detail.append("missing " + ", ".join(missing))
                if unknown:
                    detail.append("unknown " + ", ".join(unknown))
                raise SelectorConfigError("weights must use the exact common schema: " + "; ".join(detail))
            self.weights = tuple(_num(weights, key, 0.0) for key in ("relevance", "evidence", "interaction", "structure", "state"))
        self.kappa = _num(p, "kappa", 1.0, lo=0.0)
        raw_quota = p.get("quota", p.get("max_items", 0))
        if raw_quota is None:
            raw_quota = 0
        if isinstance(raw_quota, bool) or not isinstance(raw_quota, int):
            raise SelectorConfigError("quota must be a non-negative integer")
        self.quota = int(raw_quota or 0)
        if self.quota < 0: raise SelectorConfigError("quota must be non-negative")
        raw_precision = p.get("score_precision", 8)
        if raw_precision is None:
            raw_precision = 8
        if isinstance(raw_precision, bool) or not isinstance(raw_precision, int) or not 0 <= raw_precision <= 15:
            raise SelectorConfigError("score_precision must be an integer from 0 to 15")
        self.precision = int(raw_precision)
        configured_mode = p.get("interaction_mode")
        if configured_mode is not None and configured_mode not in {"none", "unnormalized", "nu"}:
            raise SelectorConfigError("interaction_mode must be none, unnormalized, or nu")
        if self.profile == "feedback_diversity":
            # The issue defines this heuristic's feedback component as typed
            # unnormalized feedback.  Keep a mapping override available for
            # development fixtures, but make the default explicit.
            self.mode = str(configured_mode or "unnormalized")
        elif configured_mode is not None and configured_mode != self.mode:
            raise SelectorConfigError(
                f"{self.profile} interaction_mode must be {self.mode}"
            )
        raw_feedback_values = p.get("feedback_values", {})
        if raw_feedback_values is None:
            raw_feedback_values = {}
        if not isinstance(raw_feedback_values, Mapping):
            raise SelectorConfigError("feedback_values must be a mapping")
        self.feedback_values: dict[str, float] = {}
        for key, value in raw_feedback_values.items():
            if not isinstance(key, str) or not key:
                raise SelectorConfigError("feedback_values keys must be non-empty strings")
            self.feedback_values[key] = _num({key: value}, key)
        # Exploration is part of the frozen policy identity.  It is a pure
        # deterministic hash bonus rather than process/global RNG state, so a
        # paired repeat can be replayed byte-for-byte.  ``hash_v1`` is the
        # explicit default for old manifests which did not carry the field.
        self.exploration = _num(p, "exploration", 0.0, lo=0.0)
        self.exploration_mode = p.get("exploration_mode", "hash_v1")
        if self.exploration_mode != "hash_v1":
            raise SelectorConfigError("exploration_mode must be hash_v1")
        self.exploration_offset = _num(p, "exploration_offset", 0.0, lo=0.0)

    @staticmethod
    def _request_context(snapshot: Any) -> dict[str, Any]:
        """Extract only reproducible request fields for exploration hashing.

        Run and actor identifiers are intentionally excluded: paired arms may
        execute on different workers/runs while they must receive the same
        exploration draw.  Lightweight selector fixtures often omit a typed
        request, so the helper tolerates either mappings or objects.
        """

        request = _get(snapshot, "request", snapshot)
        fields = (
            "paired_seed", "task_id", "task_family", "episode",
            "search_ordinal", "query",
        )
        context: dict[str, Any] = {}
        for field in fields:
            value = _get(request, field, None)
            if value is not None and value != "":
                context[field] = value
        # If no paired/typed context is available, a request key is the best
        # stable identity a caller can provide.  Prefer it only as a fallback;
        # runtime request keys can contain arm-local run IDs.
        if not context:
            request_key = _get(request, "request_key", None)
            if request_key not in (None, ""):
                context["request_key"] = str(request_key)
        return context

    def _exploration_bonus(self, candidate: Any, snapshot: Any) -> float:
        if self.exploration == 0.0:
            return 0.0
        trace = str(_get(candidate, "trace_id", _get(candidate, "id", "")))
        payload = {
            "version": "exploration_hash_v1",
            "offset": self.exploration_offset,
            "context": self._request_context(snapshot),
            "trace_id": trace,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        # Use 64 bits from SHA-256 and divide by 2**64.  This yields a stable
        # value in [0, 1) without platform-dependent floating-point parsing.
        normalized = int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big") / float(1 << 64)
        return self.exploration * normalized

    def score(self, candidate: Any, *, snapshot: Any = None) -> tuple[float, FeatureVector]:
        features = extract_features(
            candidate,
            interaction_mode=self.mode,
            kappa=self.kappa,
            feedback_values=self.feedback_values,
        )
        value = sum(w * x for w, x in zip(self.weights, features[:5]))
        if snapshot is not None:
            value += self._exploration_bonus(candidate, snapshot)
        return value, features

    def rank(self, snapshot: Any) -> tuple[dict[str, Any], ...]:
        candidates = list(_get(snapshot, "eligible", _get(snapshot, "candidates", ())))
        rows = []
        seen_traces: set[str] = set()
        for seq, candidate in enumerate(candidates):
            trace = str(_get(candidate, "trace_id", _get(candidate, "id", "")))
            if not trace:
                raise SelectorConfigError("every candidate needs trace_id")
            if trace in seen_traces:
                raise SelectorConfigError("eligible pool requires unique trace_id values")
            seen_traces.add(trace)
            score, features = self.score(candidate, snapshot=snapshot)
            exploration = self._exploration_bonus(candidate, snapshot)
            rows.append((candidate, trace, score, features, seq, exploration))
        rows.sort(key=lambda r: (-round(r[2], self.precision), r[1], r[4]))
        limit = self.quota or len(rows)
        result = []
        for rank, (candidate, trace, score, features, _, exploration) in enumerate(rows[:limit], 1):
            components = {"relevance": features.relevance, "evidence": features.evidence,
                          "interaction": features.interaction, "structure": features.structure,
                          "state": features.state}
            # Keep zero-exploration output byte-compatible with the original
            # selector contract; nonzero exploration is explicitly auditable.
            if self.exploration != 0.0:
                components["exploration"] = exploration
            result.append({"candidate": candidate, "trace_id": trace, "rank": rank,
                           "total_score": round(score, self.precision),
                           "component_scores": components, "cluster": features.cluster,
                           "lineage": features.lineage, "exposure": features.exposure,
                           "tie_key": trace, "selected": True, "drop_reason": ""})
        return tuple(result)

    select = rank
    __call__ = rank


class NoInteractionSelector(_NuScorer):
    profile = "no_interaction"; mode = "none"


class UnnormalizedSelector(_NuScorer):
    profile = "unnormalized"; mode = "unnormalized"


class NuStigmergySelector(_NuScorer):
    profile = "nu_stigmergy"; mode = "nu"


class FeedbackDiversitySelector(_NuScorer):
    """Feedback score with deterministic lineage/cluster diversity reservation."""
    profile = "feedback_diversity"

    def __init__(self, config: Any):
        super().__init__(config)
        p = _params(config)
        self.diversity_weight = _num(p, "diversity_weight", 0.0, lo=0.0)

    def rank(self, snapshot: Any) -> tuple[dict[str, Any], ...]:
        # Score through the common feature extractor, then add a transparent
        # cluster novelty term.  Diversity is selected greedily over the full
        # eligible pool: ``seen_clusters`` is updated after every choice,
        # rather than being (incorrectly) evaluated against an always-empty
        # set.  The term does not alter worker feedback or lifecycle/evidence
        # features.
        candidates = list(_get(snapshot, "eligible", _get(snapshot, "candidates", ())))
        rows = []
        seen_traces: set[str] = set()
        for seq, candidate in enumerate(candidates):
            trace = str(_get(candidate, "trace_id", _get(candidate, "id", "")))
            if not trace:
                raise SelectorConfigError("every candidate needs trace_id")
            if trace in seen_traces:
                raise SelectorConfigError("eligible pool requires unique trace_id values")
            seen_traces.add(trace)
            base, features = self.score(candidate, snapshot=snapshot)
            exploration = self._exploration_bonus(candidate, snapshot)
            rows.append({
                "candidate": candidate,
                "trace_id": trace,
                "base_score": base,
                "features": features,
                "seq": seq,
                "exploration": exploration,
            })
        limit = self.quota or len(rows)
        if limit <= 0 or not rows:
            return ()

        # Greedy full-pool selection.  A row's effective score is recomputed
        # against the clusters already selected, making novelty auditable and
        # deterministic even when the input sequence changes.
        remaining = list(rows)
        selected: list[tuple[dict[str, Any], float, float]] = []
        seen_clusters: set[str] = set()
        while remaining and len(selected) < limit:
            scored: list[tuple[dict[str, Any], float, float]] = []
            for row in remaining:
                features = row["features"]
                cluster = features.cluster
                novelty = 1.0 if cluster and cluster not in seen_clusters else 0.0
                score = float(row["base_score"]) + self.diversity_weight * novelty
                scored.append((row, score, novelty))
            scored.sort(
                key=lambda item: (
                    -round(item[1], self.precision),
                    item[0]["trace_id"],
                    item[0]["seq"],
                )
            )
            chosen, score, novelty = scored[0]
            selected.append((chosen, score, novelty))
            remaining.remove(chosen)
            cluster = chosen["features"].cluster
            if cluster:
                seen_clusters.add(cluster)

        # Reserve an alternative lineage from the *full* pool.  The old
        # implementation searched only rows already inside ``rows[:quota]``,
        # so a valid alternative just beyond the quota could never enter the
        # result.  Replace the final slot deterministically and place the
        # reservation second, preserving the primary row and stable ordering.
        if len(selected) >= 2:
            primary_lineage = selected[0][0]["features"].lineage

            def is_alternative(lineage: str) -> bool:
                return bool(lineage) if not primary_lineage else bool(
                    lineage and lineage != primary_lineage
                )

            has_alternative = any(
                is_alternative(item[0]["features"].lineage)
                for item in selected[1:]
            )
            if not has_alternative:
                selected_traces = {item[0]["trace_id"] for item in selected}
                alternatives = [
                    row for row in rows
                    if row["trace_id"] not in selected_traces
                    and is_alternative(row["features"].lineage)
                ]
                if alternatives:
                    # Use the same effective-score convention, with the
                    # already-selected cluster set as the deterministic tie
                    # context.  This is a reservation only; it never mutates
                    # the candidate's feature values.
                    def alternative_key(row: dict[str, Any]) -> tuple[Any, ...]:
                        cluster = row["features"].cluster
                        novelty = 1.0 if cluster and cluster not in seen_clusters else 0.0
                        score = float(row["base_score"]) + self.diversity_weight * novelty
                        return (-round(score, self.precision), row["trace_id"], row["seq"])

                    alt = min(alternatives, key=alternative_key)
                    retained_clusters = {
                        item[0]["features"].cluster for item in selected[:-1]
                        if item[0]["features"].cluster
                    }
                    alt_cluster = alt["features"].cluster
                    alt_novelty = 1.0 if alt_cluster and alt_cluster not in retained_clusters else 0.0
                    alt_score = float(alt["base_score"]) + self.diversity_weight * alt_novelty
                    selected[-1] = (alt, alt_score, alt_novelty)
                    selected.insert(1, selected.pop())

        result: list[dict[str, Any]] = []
        for rank, (row, score, novelty) in enumerate(selected, 1):
            features = row["features"]
            components = {
                "relevance": features.relevance,
                "evidence": features.evidence,
                "interaction": features.interaction,
                "structure": features.structure,
                "state": features.state,
                "diversity": novelty,
            }
            if self.exploration != 0.0:
                components["exploration"] = row["exploration"]
            result.append({
                "candidate": row["candidate"],
                "trace_id": row["trace_id"],
                "rank": rank,
                "total_score": round(score, self.precision),
                "component_scores": components,
                "cluster": features.cluster,
                "lineage": features.lineage,
                "exposure": features.exposure,
                "tie_key": row["trace_id"],
                "selected": True,
                "drop_reason": "",
            })
        return tuple(result)


def score_candidate(candidate: Any, config: Any, *, mode: str = "nu") -> float:
    parameters = dict(_params(config))
    if mode not in {"none", "unnormalized", "nu"}:
        raise SelectorConfigError("unknown interaction mode")
    parameters.pop("interaction_mode", None)
    scorer = _NuScorer({"parameters": parameters})
    scorer.mode = mode
    return scorer.score(candidate)[0]


# Friendly functional names used by registry adapters.
no_interaction_rank = lambda snapshot, config: NoInteractionSelector(config).rank(snapshot)
unnormalized_rank = lambda snapshot, config: UnnormalizedSelector(config).rank(snapshot)
nu_stigmergy_rank = lambda snapshot, config: NuStigmergySelector(config).rank(snapshot)
feedback_diversity_rank = lambda snapshot, config: FeedbackDiversitySelector(config).rank(snapshot)

# Alternate descriptive names retained for manifest/registry adapters.
NoInteraction = NoInteractionSelector
Unnormalized = UnnormalizedSelector
NuStigmergy = NuStigmergySelector
FeedbackDiversityHeuristic = FeedbackDiversitySelector
