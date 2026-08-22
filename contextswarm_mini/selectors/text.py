"""Deterministic lexical retrieval selectors.

The implementation is intentionally pure: it reads a snapshot-like object and
returns ranked rows, without touching CPS or any process/global state.  All
behaviour which can affect a comparison is required in ``parameters``.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import math
import re
import unicodedata
from typing import Any


class SelectorConfigError(ValueError):
    """The selector configuration is incomplete or ambiguous."""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _params(config: Any) -> Mapping[str, Any]:
    p = _get(config, "parameters", _get(config, "policy_params", config))
    if not isinstance(p, Mapping):
        raise SelectorConfigError("BM25+MMR parameters must be a mapping")
    return p


def _number(p: Mapping[str, Any], key: str, *, lo: float | None = None,
            hi: float | None = None) -> float:
    value = p.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise SelectorConfigError(f"{key} must be a finite number")
    value = float(value)
    if lo is not None and value < lo or hi is not None and value > hi:
        raise SelectorConfigError(f"{key} is outside its permitted range")
    return value


class BM25MMRSelector:
    """BM25 lexical ranking followed by deterministic MMR reranking.

    Required parameters are ``tokenizer_pattern``, ``lowercase``,
    ``unicode_normalization``, ``fields`` (mapping field name to positive
    weight), ``bm25_k1``, ``bm25_b``, ``bm25_idf``, ``candidate_depth``,
    ``mmr_lambda``, ``similarity`` (currently ``cosine_tfidf``),
    ``similarity_idf``, ``similarity_field_handling``, ``score_precision`` and
    ``tie_break`` (currently ``trace_id_asc``).  ``max_items`` is read from
    the request, so it is never silently changed by the policy.
    """

    def __init__(self, config: Any):
        p = _params(config)
        required = ("tokenizer_pattern", "lowercase", "unicode_normalization", "fields",
                    "bm25_k1", "bm25_b", "bm25_idf", "candidate_depth", "mmr_lambda",
                    "similarity", "similarity_idf", "similarity_field_handling",
                    "score_precision", "tie_break")
        missing = [key for key in required if key not in p]
        if missing:
            raise SelectorConfigError("missing explicit parameters: " + ", ".join(missing))
        if not isinstance(p["tokenizer_pattern"], str) or not p["tokenizer_pattern"]:
            raise SelectorConfigError("tokenizer_pattern must be a non-empty string")
        if not isinstance(p["lowercase"], bool):
            raise SelectorConfigError("lowercase must be boolean")
        if not isinstance(p["unicode_normalization"], str) or p["unicode_normalization"] not in {"none", "NFC", "NFKC", "NFD", "NFKD"}:
            raise SelectorConfigError("unsupported unicode_normalization")
        fields = p["fields"]
        if not isinstance(fields, Mapping) or not fields or any(not isinstance(k, str) or not k for k in fields):
            raise SelectorConfigError("fields must be a non-empty mapping")
        self.fields = tuple((k, _number(fields, k, lo=0.0)) for k in sorted(fields))
        if any(weight <= 0 for _, weight in self.fields):
            raise SelectorConfigError("field weights must be positive")
        self.pattern = re.compile(p["tokenizer_pattern"])
        if self.pattern.groups:
            raise SelectorConfigError("tokenizer_pattern must not contain capture groups")
        self.lowercase = p["lowercase"]
        self.normalization = p["unicode_normalization"]
        self.k1 = _number(p, "bm25_k1", lo=0.0)
        self.b = _number(p, "bm25_b", lo=0.0, hi=1.0)
        if p["bm25_idf"] != "robertson_log1p":
            raise SelectorConfigError("bm25_idf must be robertson_log1p")
        depth = p["candidate_depth"]
        if isinstance(depth, bool) or not isinstance(depth, int) or depth <= 0:
            raise SelectorConfigError("candidate_depth must be a positive integer")
        self.depth = depth
        self.mmr_lambda = _number(p, "mmr_lambda", lo=0.0, hi=1.0)
        if p["similarity"] != "cosine_tfidf":
            raise SelectorConfigError("similarity must be cosine_tfidf")
        if p["similarity_idf"] != "robertson_log1p":
            raise SelectorConfigError("similarity_idf must be robertson_log1p")
        if p["similarity_field_handling"] != "field_qualified_weighted_tf":
            raise SelectorConfigError(
                "similarity_field_handling must be field_qualified_weighted_tf"
            )
        precision = p["score_precision"]
        if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 15:
            raise SelectorConfigError("score_precision must be an integer from 0 to 15")
        self.precision = precision
        if p["tie_break"] != "trace_id_asc":
            raise SelectorConfigError("tie_break must be trace_id_asc")

    def _tokens(self, value: Any) -> list[str]:
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value)
        text = "" if value is None else str(value)
        if self.normalization != "none":
            text = unicodedata.normalize(self.normalization, text)
        if self.lowercase:
            text = text.lower()
        return self.pattern.findall(text)

    def _doc(self, candidate: Any) -> tuple[dict[str, list[str]], str]:
        result: dict[str, list[str]] = {}
        trace_id = str(_get(candidate, "trace_id", _get(candidate, "id", "")))
        if not trace_id:
            raise SelectorConfigError("every candidate needs trace_id")
        for field, _ in self.fields:
            result[field] = self._tokens(_get(candidate, field, ""))
        return result, trace_id

    @staticmethod
    def _idf(df: int, n: int) -> float:
        return math.log1p((n - df + 0.5) / (df + 0.5))

    def rank(self, snapshot: Any) -> tuple[dict[str, Any], ...]:
        """Return the configured BM25 candidate depth in deterministic MMR order.

        Candidates outside ``candidate_depth`` are deliberately not returned:
        the common packer is allowed to choose any row it receives, while the
        complete eligible pool is already retained by ``SelectionSnapshot``.
        """
        candidates = list(_get(snapshot, "eligible", _get(snapshot, "candidates", ())))
        docs = [(candidate, *self._doc(candidate)) for candidate in candidates]
        docs.sort(key=lambda row: row[2])
        query = str(_get(_get(snapshot, "request", snapshot), "query", ""))
        q_tokens = self._tokens(query)
        n = len(docs)
        # Field-local document frequencies and average lengths are frozen by this snapshot.
        stats: dict[str, tuple[dict[str, int], float]] = {}
        for field, _ in self.fields:
            lengths = [len(row[1][field]) for row in docs]
            df = Counter(token for row in docs for token in set(row[1][field]))
            stats[field] = (df, (sum(lengths) / n if n else 0.0))
        scored: list[dict[str, Any]] = []
        for candidate, token_fields, trace_id in docs:
            score = 0.0
            for field, weight in self.fields:
                tf = Counter(token_fields[field]); length = len(token_fields[field]); df, avg = stats[field]
                for token in q_tokens:
                    frequency = tf.get(token, 0)
                    if not frequency:
                        continue
                    idf = self._idf(df.get(token, 0), n)
                    norm = 1.0 - self.b + self.b * (length / avg if avg else 0.0)
                    score += weight * idf * (frequency * (self.k1 + 1.0) / (frequency + self.k1 * norm))
            scored.append({"candidate": candidate, "trace_id": trace_id, "bm25_score": score, "tokens": token_fields})
        scored.sort(key=lambda row: (-round(row["bm25_score"], self.precision), row["trace_id"]))
        pool = scored[: self.depth]
        # Combined TF-IDF vectors are used solely for diversity, with the same explicit tokenizer.
        df_all = Counter(
            f"{field}\0{token}"
            for row in pool
            for field, _ in self.fields
            for token in set(row["tokens"][field])
        )
        vectors: dict[str, dict[str, float]] = {}
        for row in pool:
            vector: Counter[str] = Counter()
            for field, weight in self.fields:
                for token, count in Counter(row["tokens"][field]).items():
                    key = f"{field}\0{token}"
                    vector[key] = weight * count * self._idf(df_all[key], len(pool))
            vectors[row["trace_id"]] = dict(vector)
        selected: list[dict[str, Any]] = []
        remaining = list(pool)
        while remaining:
            best = None
            best_key = None
            for row in remaining:
                relevance = row["bm25_score"]
                diversity = max((self._cosine(vectors[row["trace_id"]], vectors[item["trace_id"]]) for item in selected), default=0.0)
                mmr = self.mmr_lambda * relevance - (1.0 - self.mmr_lambda) * diversity
                key = (-round(mmr, self.precision), row["trace_id"])
                if best_key is None or key < best_key:
                    best, best_key = row, key
                row["mmr_score"] = mmr
                row["similarity_penalty"] = diversity
            assert best is not None
            selected.append(best)
            remaining.remove(best)
        output = []
        for rank, row in enumerate(selected, 1):
            candidate = row["candidate"]
            try:
                frozen_token_count = int(_get(candidate, "token_count", 0))
            except (TypeError, ValueError) as exc:
                raise SelectorConfigError("candidate token_count must be an integer") from exc
            if frozen_token_count < 0:
                raise SelectorConfigError("candidate token_count must not be negative")
            mmr_score = row["mmr_score"]
            penalty = row["similarity_penalty"]
            output.append({
                "candidate": candidate,
                "trace_id": row["trace_id"],
                "rank": rank,
                "total_score": round(mmr_score, self.precision),
                "component_scores": {
                    "bm25": round(row["bm25_score"], self.precision),
                    "mmr": round(mmr_score, self.precision),
                    "similarity_penalty": round(penalty, self.precision),
                },
                "tie_key": row["trace_id"],
                "token_count": frozen_token_count,
                "selected": True,
                "drop_reason": "",
            })
        return tuple(output)

    @staticmethod
    def _cosine(left: Mapping[str, float], right: Mapping[str, float]) -> float:
        denominator = math.sqrt(sum(v * v for v in left.values()) * sum(v * v for v in right.values()))
        return sum(left.get(k, 0.0) * right.get(k, 0.0) for k in left) / denominator if denominator else 0.0

    __call__ = rank


def bm25_mmr_rank(snapshot: Any, config: Any) -> tuple[dict[str, Any], ...]:
    """Functional convenience wrapper for registry adapters."""
    return BM25MMRSelector(config).rank(snapshot)
