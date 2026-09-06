from __future__ import annotations

import unittest

from contextswarm_mini.route_dedup import (
    find_route_overlaps,
    normalized_route_tokens,
    route_similarity,
)


class RouteDedupTests(unittest.TestCase):
    def test_normalization_is_bounded_and_stems_common_forms(self) -> None:
        tokens = normalized_route_tokens("Use inductively bounded recurrences")
        self.assertEqual(tokens, frozenset({"induction", "bound", "recurrence"}))

    def test_short_or_generic_summaries_remain_unknown(self) -> None:
        self.assertEqual(route_similarity("try proof", "try proof"), (0.0, ()))
        self.assertEqual(
            find_route_overlaps(
                "try proof",
                [{"claim_id": "c", "actor_id": "a", "summary": "try proof"}],
                threshold=0.5,
            ),
            [],
        )

    def test_overlap_reports_same_route_for_high_confidence_restatement(self) -> None:
        overlaps = find_route_overlaps(
            "induction on n with bounded recurrence for the sum",
            [
                {
                    "claim_id": "claim-1",
                    "actor_id": "peer-1",
                    "summary": "use inductive bounded recurrence for the sums",
                }
            ],
            threshold=0.6,
        )
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0].relation, "same_route")
        self.assertGreaterEqual(overlaps[0].score, 0.82)
        self.assertIn("recurrence", overlaps[0].shared_tokens)


if __name__ == "__main__":
    unittest.main()
