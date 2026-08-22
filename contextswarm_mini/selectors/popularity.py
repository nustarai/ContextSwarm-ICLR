"""Pure smoothed-popularity trace selector.

Popularity is deliberately a small, auditable policy: it consumes only the
immutable selection snapshot and computes a Beta-smoothed success rate from
*delivered* exposure feedback.  All policy parameters are required explicitly
so a formal manifest cannot silently inherit defaults.
"""
from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any


class PopularityConfigError(ValueError):
    """The popularity policy parameters are missing or invalid."""


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _params(config: Any) -> Mapping[str, Any]:
    params = _get(config, "parameters", _get(config, "policy_params", config))
    if not isinstance(params, Mapping):
        raise PopularityConfigError("parameters must be a mapping")
    return params


def _finite(params: Mapping[str, Any], key: str, *, positive: bool = False) -> float:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PopularityConfigError(f"{key} must be an explicit finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0) or (not positive and result < 0):
        raise PopularityConfigError(f"{key} must be {'positive' if positive else 'non-negative'}")
    return result


class SmoothedPopularitySelector:
    """Rank eligible candidates by ``(alpha + positive)/(alpha + beta + E)``."""

    name = "smoothed_popularity"

    def __init__(self, config: Any):
        params = _params(config)
        required = ("alpha", "beta", "positive_kinds", "negative_kinds", "score_precision", "tie_break")
        missing = [key for key in required if key not in params]
        if missing:
            raise PopularityConfigError("missing explicit parameters: " + ", ".join(missing))
        self.alpha = _finite(params, "alpha", positive=True)
        self.beta = _finite(params, "beta", positive=True)
        self.positive_kinds = self._kinds(params["positive_kinds"], "positive_kinds")
        self.negative_kinds = self._kinds(params["negative_kinds"], "negative_kinds")
        if self.positive_kinds & self.negative_kinds:
            raise PopularityConfigError("positive_kinds and negative_kinds must be disjoint")
        precision = params["score_precision"]
        if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 15:
            raise PopularityConfigError("score_precision must be an integer from 0 to 15")
        if params["tie_break"] != "trace_id_asc":
            raise PopularityConfigError("tie_break must be trace_id_asc")
        self.precision = precision

    @staticmethod
    def _kinds(value: Any, name: str) -> frozenset[str]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
            raise PopularityConfigError(f"{name} must be a collection of strings")
        result = frozenset(str(item).strip() for item in value)
        if not result or any(not item for item in result):
            raise PopularityConfigError(f"{name} must not be empty")
        return result

    def _score(self, candidate: Any) -> tuple[float, int, int, int]:
        feedback = _get(candidate, "feedback")
        counts = _get(feedback, "kind_counts", {})
        if not isinstance(counts, Mapping):
            raise PopularityConfigError("feedback.kind_counts must be a mapping")
        def count_for(kind: str) -> int:
            raw = counts.get(kind, 0)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise PopularityConfigError(
                    f"feedback.kind_counts.{kind} must be a non-negative integer"
                )
            return raw

        positive = sum(count_for(kind) for kind in self.positive_kinds)
        negative = sum(count_for(kind) for kind in self.negative_kinds)
        raw_exposure = _get(feedback, "exposure_count", 0)
        if (
            isinstance(raw_exposure, bool)
            or not isinstance(raw_exposure, int)
            or raw_exposure < 0
        ):
            raise PopularityConfigError(
                "feedback.exposure_count must be a non-negative integer"
            )
        exposure = raw_exposure
        if positive + negative > exposure:
            raise PopularityConfigError("feedback counts exceed effective delivered exposure")
        score = (self.alpha + positive) / (self.alpha + self.beta + exposure)
        return round(score, self.precision), positive, negative, exposure

    def rank(self, snapshot: Any) -> tuple[dict[str, Any], ...]:
        eligible = list(_get(snapshot, "eligible", _get(snapshot, "candidates", ())))
        rows = []
        seen: set[str] = set()
        for candidate in eligible:
            trace_id = str(_get(candidate, "trace_id", _get(candidate, "id", "")))
            if not trace_id:
                raise PopularityConfigError("every candidate needs trace_id")
            if trace_id in seen:
                raise PopularityConfigError("eligible pool requires unique trace_id values")
            seen.add(trace_id)
            score, positive, negative, exposure = self._score(candidate)
            raw_tokens = _get(candidate, "token_count", 0)
            if isinstance(raw_tokens, bool) or not isinstance(raw_tokens, int) or raw_tokens < 0:
                raise PopularityConfigError(
                    "candidate token_count must be a non-negative integer"
                )
            rows.append((candidate, trace_id, score, positive, negative, exposure, raw_tokens))
        rows.sort(key=lambda row: (-row[2], row[1]))
        return tuple({"candidate": candidate, "trace_id": trace_id, "rank": rank, "total_score": score,
                      "component_scores": {"popularity": score, "positive": float(pos),
                                            "negative": float(neg), "effective_exposure": float(exp)},
                      "tie_key": trace_id, "token_count": tokens,
                      "selected": True, "drop_reason": ""}
                     for rank, (candidate, trace_id, score, pos, neg, exp, tokens) in enumerate(rows, 1))

    __call__ = rank


def smoothed_popularity_rank(snapshot: Any, config: Any) -> tuple[dict[str, Any], ...]:
    return SmoothedPopularitySelector(config).rank(snapshot)


__all__ = ["PopularityConfigError", "SmoothedPopularitySelector", "smoothed_popularity_rank"]
