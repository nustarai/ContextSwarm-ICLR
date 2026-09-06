"""Bounded, runner-owned route overlap decisions.

This module deliberately does not ask an Agent to decide whether it is
duplicating another Agent.  It compares the peer-visible route declarations
that the runner has already admitted and returns a conservative decision.  A
short summary is only an observation signal; ``unknown`` is retained when the
signal is too short or too generic to support an intervention.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping


_WORD_RE = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]+")
_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "by", "for", "from", "in", "into", "is", "of",
        "on", "or", "the", "to", "try", "use", "using", "with", "work",
        "proof", "prove", "show", "find", "check", "testing", "test",
    }
)
_STEM_REPLACEMENTS = {
    "inductive": "induction",
    "inductively": "induction",
    "sums": "sum",
    "summing": "sum",
    "bounds": "bound",
    "bounded": "bound",
    "inequalities": "inequality",
    "lemmas": "lemma",
    "recurrences": "recurrence",
    "recursive": "recurrence",
    "factorization": "factor",
    "factorise": "factor",
}


@dataclass(frozen=True)
class RouteOverlap:
    """A bounded comparison between one route and an admitted peer route."""

    relation: str
    score: float
    shared_tokens: tuple[str, ...]
    compared_claim_id: str
    compared_actor_id: str

    def public_dict(self) -> dict[str, object]:
        return {
            "relation": self.relation,
            "score": round(self.score, 6),
            "shared_tokens": list(self.shared_tokens[:12]),
            "compared_claim_id": self.compared_claim_id[:128],
            "compared_actor_id": self.compared_actor_id[:128],
        }


def normalized_route_tokens(text: str) -> frozenset[str]:
    """Return small, privacy-safe tokens used only for route comparison."""

    tokens: set[str] = set()
    for raw in _WORD_RE.findall(str(text or "").lower()):
        token = _STEM_REPLACEMENTS.get(raw, raw)
        if len(token) < 3 or token in _STOP_WORDS:
            continue
        tokens.add(token)
    return frozenset(tokens)


def route_similarity(left: str, right: str) -> tuple[float, tuple[str, ...]]:
    """Return a conservative lexical overlap score and shared tokens.

    This is intentionally a calibration layer rather than a claim of semantic
    equivalence.  It refuses to classify short/generic summaries and combines
    Jaccard overlap with containment so a concise summary can still match a
    longer restatement.  A future semantic evaluator can replace this helper
    while retaining the same decision envelope and audit fields.
    """

    left_tokens = normalized_route_tokens(left)
    right_tokens = normalized_route_tokens(right)
    if len(left_tokens) < 3 or len(right_tokens) < 3:
        return 0.0, ()
    shared = tuple(sorted(left_tokens & right_tokens))
    if len(shared) < 3:
        return 0.0, shared
    union = left_tokens | right_tokens
    jaccard = len(shared) / len(union)
    containment = len(shared) / min(len(left_tokens), len(right_tokens))
    score = 0.6 * containment + 0.4 * jaccard
    return min(1.0, max(0.0, score)), shared


def find_route_overlaps(
    summary: str,
    peers: Iterable[Mapping[str, object]],
    *,
    threshold: float,
    min_shared_tokens: int = 3,
) -> list[RouteOverlap]:
    """Compare a candidate summary with bounded active peer projections."""

    result: list[RouteOverlap] = []
    for peer in peers:
        peer_summary = peer.get("summary") or peer.get("activity_description")
        if not isinstance(peer_summary, str):
            continue
        score, shared = route_similarity(summary, peer_summary)
        if len(shared) < max(1, int(min_shared_tokens)) or score < threshold:
            continue
        result.append(
            RouteOverlap(
                relation="same_route" if score >= max(threshold, 0.82) else "related",
                score=score,
                shared_tokens=shared,
                compared_claim_id=str(peer.get("claim_id") or ""),
                compared_actor_id=str(peer.get("actor_id") or ""),
            )
        )
    result.sort(key=lambda item: (-item.score, item.compared_claim_id))
    return result
