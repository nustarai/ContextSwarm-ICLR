"""Independent golden/property checks for selector policy contracts.

These tests intentionally use plain mappings, so selectors cannot rely on a
particular CPS ORM or insertion order.  They are kept separate from policy
unit tests to protect the common comparison contract.
"""
from __future__ import annotations

import unittest

from contextswarm_mini.selectors.feedback import (
    FeedbackDiversitySelector,
    NoInteractionSelector,
    NuStigmergySelector,
    UnnormalizedSelector,
)
from contextswarm_mini.selectors.popularity import SmoothedPopularitySelector
from contextswarm_mini.selectors.text import BM25MMRSelector
from contextswarm_mini.selectors.feedback import SelectorConfigError


class SelectorContractProperties(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            {"trace_id": "b", "title": "alpha proof", "body": "shared", "token_count": 2,
             "relevance": 0.5, "evidence": 0.2, "structure": 0.1, "state": 0.0,
             "cluster": "c1", "lineage": "l1", "feedback": {"kind_counts": {"useful": 1}, "exposure_count": 2},
             "worker_feedback": [{"actor": "worker", "value": 1, "exposure": 2}]},
            {"trace_id": "a", "title": "alpha lemma", "body": "different", "token_count": 4,
             "relevance": 0.5, "evidence": 0.2, "structure": 0.1, "state": 0.0,
             "cluster": "c2", "lineage": "l2", "feedback": {"kind_counts": {}, "exposure_count": 0}},
        ]
        self.snapshot = {"request": {"query": "alpha"}, "eligible": self.candidates}
        self.nu_config = {"parameters": {"alpha": 1, "beta": 1, "gamma": 1, "delta": 1, "eta": 1,
                                           "kappa": 1, "quota": 2, "score_precision": 6}}
        self.pop_config = {"parameters": {"alpha": 1, "beta": 1, "positive_kinds": ["useful"],
                                           "negative_kinds": ["not_useful"], "score_precision": 6,
                                           "tie_break": "trace_id_asc"}}
        self.text_config = {"parameters": {"tokenizer_pattern": r"[A-Za-z]+", "lowercase": True,
                                            "unicode_normalization": "NFC", "fields": {"title": 1, "body": 1},
                                            "bm25_k1": 1.2, "bm25_b": .75, "bm25_idf": "robertson_log1p",
                                            "candidate_depth": 10,
                                            "mmr_lambda": .7, "similarity": "cosine_tfidf",
                                            "similarity_idf": "robertson_log1p",
                                            "similarity_field_handling": "field_qualified_weighted_tf",
                                            "score_precision": 6, "tie_break": "trace_id_asc"}}

    def test_replay_is_byte_stable_and_input_order_independent(self):
        selectors = [NoInteractionSelector(self.nu_config), UnnormalizedSelector(self.nu_config),
                     NuStigmergySelector(self.nu_config), FeedbackDiversitySelector(self.nu_config),
                     SmoothedPopularitySelector(self.pop_config), BM25MMRSelector(self.text_config)]
        for selector in selectors:
            first = selector.rank(self.snapshot)
            second = selector.rank({"request": {"query": "alpha"}, "eligible": list(reversed(self.candidates))})
            self.assertEqual(first, second, type(selector).__name__)

    def test_ties_are_trace_id_ascending(self):
        snap = {"eligible": [{"trace_id": "z", "relevance": 1}, {"trace_id": "a", "relevance": 1}]}
        rows = NoInteractionSelector(self.nu_config).rank(snap)
        self.assertEqual([r["trace_id"] for r in rows], ["a", "z"])

    def test_nu_profiles_differ_only_in_interaction_component(self):
        outputs = [s.rank(self.snapshot)[0] for s in (NoInteractionSelector(self.nu_config),
                                                       UnnormalizedSelector(self.nu_config),
                                                       NuStigmergySelector(self.nu_config))]
        for key in ("relevance", "evidence", "structure", "state"):
            self.assertEqual(outputs[0]["component_scores"][key], outputs[1]["component_scores"][key])
            self.assertEqual(outputs[1]["component_scores"][key], outputs[2]["component_scores"][key])
        self.assertEqual(outputs[0]["component_scores"]["interaction"], 0.0)

    def test_popularity_rejects_impossible_exposure_counts(self):
        bad = {"eligible": [{"trace_id": "x", "feedback": {"kind_counts": {"useful": 2}, "exposure_count": 1}}]}
        with self.assertRaises(ValueError):
            SmoothedPopularitySelector(self.pop_config).rank(bad)

    def test_feedback_diversity_updates_seen_clusters_and_reserves_full_pool_alternative(self):
        config = {"parameters": {
            "weights": {"relevance": 1.0, "evidence": 0.0, "interaction": 0.0,
                        "structure": 0.0, "state": 0.0},
            "kappa": 1.0, "quota": 2, "score_precision": 8,
            "diversity_weight": 1.0,
        }}
        candidates = [
            {"trace_id": "a", "relevance": 10.0, "cluster": "c1", "lineage": "l1"},
            # Same cluster/lineage: after a is selected, this row is not novel.
            {"trace_id": "b", "relevance": 9.5, "cluster": "c1", "lineage": "l1"},
            # This row starts outside the base top-2, but is selected by the
            # full-pool greedy novelty pass.
            {"trace_id": "c", "relevance": 9.0, "cluster": "c2", "lineage": "l2"},
        ]
        selector = FeedbackDiversitySelector(config)
        rows = selector.rank({"eligible": candidates})
        self.assertEqual([row["trace_id"] for row in rows], ["a", "c"])
        self.assertEqual(rows[0]["component_scores"]["diversity"], 1.0)
        self.assertEqual(rows[1]["component_scores"]["diversity"], 1.0)

        # With zero diversity weight, the explicit lineage reservation still
        # reaches beyond the quota-limited base ranking.
        no_bonus = {"parameters": {**config["parameters"], "diversity_weight": 0.0}}
        rows = FeedbackDiversitySelector(no_bonus).rank({"eligible": candidates})
        self.assertEqual([row["trace_id"] for row in rows], ["a", "c"])

    def test_missing_cluster_and_lineage_are_trace_singletons(self):
        selector = NuStigmergySelector(self.nu_config)
        rows = selector.rank({"eligible": [
            {"trace_id": "a", "relevance": 1.0},
            {"trace_id": "b", "relevance": 1.0},
        ]})
        self.assertEqual(rows[0]["cluster"], rows[0]["trace_id"])
        self.assertEqual(rows[0]["lineage"], rows[0]["trace_id"])
        self.assertNotEqual(rows[0]["cluster"], rows[1]["cluster"])

    def test_hash_exploration_is_deterministic_and_zero_is_backward_compatible(self):
        base = {"parameters": {
            "weights": {"relevance": 1.0, "evidence": 0.0, "interaction": 0.0,
                        "structure": 0.0, "state": 0.0},
            "kappa": 1.0, "quota": 2, "score_precision": 8,
            "exploration": 0.75, "exploration_mode": "hash_v1",
            "exploration_offset": 3.0,
        }}
        snapshot = {"request": {"paired_seed": 17, "task_id": "t",
                                 "episode": 2, "search_ordinal": 4},
                    "eligible": [{"trace_id": "b", "relevance": 1.0},
                                 {"trace_id": "a", "relevance": 1.0}]}
        selector = NuStigmergySelector(base)
        first = selector.rank(snapshot)
        second = selector.rank({"request": dict(snapshot["request"]),
                                "eligible": list(reversed(snapshot["eligible"]))})
        self.assertEqual(first, second)
        for row in first:
            self.assertGreaterEqual(row["component_scores"]["exploration"], 0.0)
            self.assertLess(row["component_scores"]["exploration"], 0.75)

        zero = {"parameters": {**base["parameters"], "exploration": 0.0}}
        zero_rows = NuStigmergySelector(zero).rank(snapshot)
        self.assertTrue(all("exploration" not in row["component_scores"] for row in zero_rows))
        self.assertEqual([row["trace_id"] for row in zero_rows], ["a", "b"])
        with self.assertRaises(SelectorConfigError):
            NuStigmergySelector({"parameters": {**base["parameters"], "exploration": -1.0}})
        with self.assertRaises(SelectorConfigError):
            NuStigmergySelector({"parameters": {**base["parameters"], "exploration_mode": "random"}})


if __name__ == "__main__":
    unittest.main()
