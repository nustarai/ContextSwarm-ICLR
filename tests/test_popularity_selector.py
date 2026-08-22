import unittest
from types import SimpleNamespace

from contextswarm_mini.selectors.popularity import PopularityConfigError, SmoothedPopularitySelector


def candidate(trace_id, exposure, counts):
    return SimpleNamespace(trace_id=trace_id, token_count=2,
                           feedback=SimpleNamespace(exposure_count=exposure, kind_counts=counts))


class PopularityTests(unittest.TestCase):
    def config(self, **extra):
        params = {"alpha": 1, "beta": 1, "positive_kinds": ["useful"],
                  "negative_kinds": ["not_useful"], "score_precision": 8,
                  "tie_break": "trace_id_asc"}
        params.update(extra)
        return SimpleNamespace(parameters=params)

    def test_beta_smoothed_effective_exposure_formula(self):
        selector = SmoothedPopularitySelector(self.config())
        rows = selector.rank(SimpleNamespace(eligible=(candidate("b", 3, {"useful": 2, "not_useful": 1}),)))
        self.assertAlmostEqual(rows[0]["total_score"], 3 / 5)
        self.assertEqual(rows[0]["component_scores"]["effective_exposure"], 3.0)

    def test_deterministic_trace_id_tie_break(self):
        selector = SmoothedPopularitySelector(self.config())
        rows = selector.rank(SimpleNamespace(eligible=(candidate("z", 0, {}), candidate("a", 0, {}))))
        self.assertEqual([row["trace_id"] for row in rows], ["a", "z"])

    def test_parameters_are_explicit(self):
        with self.assertRaises(PopularityConfigError):
            SmoothedPopularitySelector(SimpleNamespace(parameters={"alpha": 1, "beta": 1}))

    def test_feedback_cannot_exceed_delivered_exposure(self):
        selector = SmoothedPopularitySelector(self.config())
        with self.assertRaises(PopularityConfigError):
            selector.rank(SimpleNamespace(eligible=(candidate("a", 0, {"useful": 1}),)))


if __name__ == "__main__":
    unittest.main()
