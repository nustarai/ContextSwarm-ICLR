import unittest

from contextswarm_mini.selection import (
    SelectionRequest,
    SelectionSnapshot,
    TraceCandidate,
    pack_selection,
)
from contextswarm_mini.selectors.text import BM25MMRSelector, SelectorConfigError


class TextSelectorTests(unittest.TestCase):
    def config(self, **overrides):
        params = {
            "tokenizer_pattern": r"[A-Za-z0-9]+",
            "lowercase": True,
            "unicode_normalization": "NFKC",
            "fields": {"title": 2.0, "body": 1.0, "tags": 1.5},
            "bm25_k1": 1.2,
            "bm25_b": 0.75,
            "bm25_idf": "robertson_log1p",
            "candidate_depth": 3,
            "mmr_lambda": 0.7,
            "similarity": "cosine_tfidf",
            "similarity_idf": "robertson_log1p",
            "similarity_field_handling": "field_qualified_weighted_tf",
            "score_precision": 8,
            "tie_break": "trace_id_asc",
        }
        params.update(overrides)
        return {"parameters": params}

    def snapshot(self):
        return {"request": {"query": "alpha solver"}, "eligible": [
            {"trace_id": "b", "title": "Alpha", "body": "solver solver", "tags": ["x"], "token_count": 31},
            {"trace_id": "a", "title": "Alpha", "body": "solver solver", "tags": ["x"], "token_count": 29},
            {"trace_id": "c", "title": "Gamma", "body": "unrelated", "tags": [], "token_count": 7},
            {"trace_id": "d", "title": "Alpha", "body": "solver", "tags": ["different"], "token_count": 23},
        ]}

    def test_deterministic_and_tie_break(self):
        selector = BM25MMRSelector(self.config())
        self.assertEqual(selector.rank(self.snapshot()), selector.rank(self.snapshot()))
        ids = [row["trace_id"] for row in selector.rank(self.snapshot())]
        self.assertEqual(ids[0], "a")
        self.assertEqual(set(ids), {"a", "b", "d"})

    def test_candidate_depth_is_explicit(self):
        result = BM25MMRSelector(self.config(candidate_depth=2)).rank(self.snapshot())
        self.assertEqual(len(result), 2)
        self.assertEqual({row["trace_id"] for row in result}, {"a", "b"})
        packed = pack_selection(result, max_items=4, context_token_budget=1_000)
        self.assertEqual({row.trace_id for row in packed if row.selected}, {"a", "b"})

    def test_rows_carry_candidate_and_frozen_token_count(self):
        candidates = tuple(
            TraceCandidate(
                trace_id=row["trace_id"], title=row["title"], body=row["body"],
                tags=tuple(row["tags"]), token_count=row["token_count"],
            )
            for row in self.snapshot()["eligible"]
        )
        snapshot = SelectionSnapshot(
            request=SelectionRequest(query="alpha solver"), eligible=candidates,
        )
        candidate_by_id = {row.trace_id: row for row in snapshot.eligible}
        result = BM25MMRSelector(self.config()).rank(snapshot)
        for row in result:
            self.assertIs(row["candidate"], candidate_by_id[row["trace_id"]])
            self.assertEqual(row["token_count"], row["candidate"].token_count)

        # Packing uses the frozen count rather than the much smaller lexical
        # word count computed internally for BM25.
        packed = pack_selection(result, max_items=3, context_token_budget=28)
        self.assertFalse(packed[0].selected)
        self.assertEqual(packed[0].drop_reason, "token_budget")

    def test_missing_policy_parameter_fails_closed(self):
        params = self.config()["parameters"]
        params.pop("mmr_lambda")
        with self.assertRaises(SelectorConfigError):
            BM25MMRSelector({"parameters": params})

    def test_unicode_and_case_are_configured(self):
        selector = BM25MMRSelector(self.config(lowercase=False, unicode_normalization="none"))
        rows = selector.rank({"request": {"query": "ALPHA"}, "eligible": [
            {"trace_id": "a", "title": "alpha", "body": "", "tags": [], "token_count": 2},
            {"trace_id": "b", "title": "ALPHA", "body": "", "tags": [], "token_count": 2},
        ]})
        self.assertEqual(rows[0]["trace_id"], "b")


if __name__ == "__main__":
    unittest.main()
