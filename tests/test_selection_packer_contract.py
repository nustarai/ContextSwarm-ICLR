import unittest

from contextswarm_mini.selection import RankedTrace, pack_ranked_by_token_budget


class SelectionPackerContractTests(unittest.TestCase):
    def test_policy_rejected_rows_are_never_readmitted_or_counted(self) -> None:
        rejected_candidate = {"trace_id": "policy-drop", "body": "audit me"}
        rows = (
            RankedTrace(
                trace_id="policy-drop",
                rank=1,
                total_score=9.0,
                component_scores={"policy": 9.0},
                tie_key="policy-drop",
                token_count=100,
                selected=False,
                drop_reason="policy_ineligible",
                candidate=rejected_candidate,
            ),
            RankedTrace(trace_id="kept-a", rank=2, token_count=4, selected=True),
            RankedTrace(trace_id="kept-b", rank=3, token_count=4, selected=True),
            RankedTrace(trace_id="common-drop", rank=4, token_count=1, selected=True),
        )

        packed = pack_ranked_by_token_budget(
            rows,
            max_items=2,
            context_token_budget=8,
        )

        # Packing retains the complete ordered audit trail, including policy
        # exclusions and their evidence, while only policy-selected rows use
        # the two shared slots.
        self.assertEqual([row.trace_id for row in packed], [row.trace_id for row in rows])
        self.assertEqual([row.rank for row in packed], [1, 2, 3, 4])
        self.assertFalse(packed[0].selected)
        self.assertEqual(packed[0].drop_reason, "policy_ineligible")
        self.assertEqual(packed[0].component_scores, {"policy": 9.0})
        self.assertIs(packed[0].candidate, rejected_candidate)
        self.assertEqual(
            [row.trace_id for row in packed if row.selected],
            ["kept-a", "kept-b"],
        )
        self.assertFalse(packed[3].selected)
        self.assertEqual(packed[3].drop_reason, "max_items")

    def test_token_budget_only_applies_to_policy_selected_rows(self) -> None:
        packed = pack_ranked_by_token_budget(
            (
                {
                    "trace_id": "oversized-policy-drop",
                    "rank": 1,
                    "token_count": 1_000,
                    "selected": False,
                    "drop_reason": "outside_candidate_depth",
                },
                {"trace_id": "fits", "rank": 2, "token_count": 6, "selected": True},
                {"trace_id": "too-late", "rank": 3, "token_count": 5, "selected": True},
            ),
            max_items=3,
            context_token_budget=10,
        )

        self.assertEqual(len(packed), 3)
        self.assertFalse(packed[0].selected)
        self.assertEqual(packed[0].drop_reason, "outside_candidate_depth")
        self.assertTrue(packed[1].selected)
        self.assertEqual(packed[1].drop_reason, "")
        self.assertFalse(packed[2].selected)
        self.assertEqual(packed[2].drop_reason, "token_budget")


if __name__ == "__main__":
    unittest.main()
