"""Contract tests for the two deterministic reference selectors.

The parsed manifest wrapper is deliberately tested separately from the small
mapping fixtures used by older selector tests.  This catches accidental policy
defaults while retaining backwards compatibility for callers that construct a
pure selector with an empty configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
import unittest

from contextswarm_mini.selection import (
    PAIRED_SEED_DERIVATION,
    RECENCY_PRIMARY_SORT,
    TRACE_ID_ASC_TIE_BREAK,
    RandomSelector,
    RecencySelector,
    SelectionRequest,
    SimpleSelectorConfigError,
    TraceCandidate,
    derive_seed,
    make_snapshot,
)


def _snapshot(*candidates: TraceCandidate, request: SelectionRequest | None = None):
    return make_snapshot(request or SelectionRequest(
        task_id="task-a",
        actor_id="worker-1",
        episode=2,
        search_ordinal=3,
        paired_seed=91,
    ), candidates)


class _FormalRandom:
    enabled = True
    policy_params = {"sample_without_replacement": True}
    tie_break = TRACE_ID_ASC_TIE_BREAK
    seed_derivation = PAIRED_SEED_DERIVATION


class _FormalRecency:
    enabled = True
    policy_params = {"primary_sort": RECENCY_PRIMARY_SORT}
    tie_break = TRACE_ID_ASC_TIE_BREAK


class SimpleSelectorContractTests(unittest.TestCase):
    def test_formal_random_requires_true_without_replacement(self) -> None:
        self.assertEqual(
            [row["trace_id"] for row in RandomSelector(_FormalRandom()).rank(_snapshot(
                TraceCandidate("a"), TraceCandidate("b"),
            ))],
            [row["trace_id"] for row in RandomSelector(_FormalRandom()).rank(_snapshot(
                TraceCandidate("a"), TraceCandidate("b"),
            ))],
        )
        for params in (
            {},
            {"sample_without_replacement": False},
            {"sample_without_replacement": 1},
            {"sample_without_replacement": True, "seed_derivation": PAIRED_SEED_DERIVATION},
        ):
            with self.subTest(params=params):
                config = type("Formal", (), {
                    "enabled": True,
                    "policy_params": params,
                    "tie_break": TRACE_ID_ASC_TIE_BREAK,
                    "seed_derivation": PAIRED_SEED_DERIVATION,
                })()
                with self.assertRaises(SimpleSelectorConfigError):
                    RandomSelector(config)

    def test_formal_recency_requires_commit_sequence_policy(self) -> None:
        valid = RecencySelector(_FormalRecency())
        rows = valid.rank(_snapshot(
            TraceCandidate("z", commit_seq=4),
            TraceCandidate("a", commit_seq=4),
            TraceCandidate("old", commit_seq=1),
        ))
        self.assertEqual([row["trace_id"] for row in rows], ["a", "z", "old"])
        for params in (
            {},
            {"primary_sort": "created_at_desc"},
            {"primary_sort": RECENCY_PRIMARY_SORT, "tie_break": TRACE_ID_ASC_TIE_BREAK},
        ):
            with self.subTest(params=params):
                config = type("Formal", (), {
                    "enabled": True,
                    "policy_params": params,
                    "tie_break": TRACE_ID_ASC_TIE_BREAK,
                })()
                with self.assertRaises(SimpleSelectorConfigError):
                    RecencySelector(config)

    def test_common_tie_and_seed_fields_are_not_policy_overrides(self) -> None:
        for selector, params in (
            (RandomSelector, {"sample_without_replacement": True}),
            (RecencySelector, {"primary_sort": RECENCY_PRIMARY_SORT}),
        ):
            with self.subTest(selector=selector.__name__):
                config = type("Formal", (), {
                    "enabled": True,
                    "policy_params": params,
                    "tie_break": "score_desc",
                    "seed_derivation": PAIRED_SEED_DERIVATION,
                })()
                with self.assertRaisesRegex(SimpleSelectorConfigError, "tie_break"):
                    selector(config)
        bad_seed = type("Formal", (), {
            "enabled": True,
            "policy_params": {"sample_without_replacement": True},
            "tie_break": TRACE_ID_ASC_TIE_BREAK,
            "seed_derivation": "run_id_seed",
        })()
        with self.assertRaisesRegex(SimpleSelectorConfigError, "seed_derivation"):
            RandomSelector(bad_seed)

    def test_empty_pure_fixtures_remain_compatible(self) -> None:
        # Existing callers construct these selectors without a manifest.
        self.assertIsNotNone(RandomSelector({}))
        self.assertIsNotNone(RecencySelector(None))
        self.assertIsNotNone(RecencySelector({"parameters": {}}))

    def test_random_uses_only_common_seed_inputs_and_trace_tie_break(self) -> None:
        candidates = (TraceCandidate("a"), TraceCandidate("b"), TraceCandidate("c"))
        request = SelectionRequest(
            task_id="task-a", actor_id="worker-1", episode=1,
            search_ordinal=2, paired_seed=7,
        )
        first = RandomSelector({"parameters": {"sample_without_replacement": True}}).rank(
            _snapshot(*candidates, request=request)
        )
        reordered = RandomSelector({"parameters": {"sample_without_replacement": True}}).rank(
            _snapshot(*reversed(candidates), request=request)
        )
        self.assertEqual(first, reordered)
        expected = {
            row["trace_id"]: derive_seed(
                7, task_id="task-a", actor_episode_key="worker-1",
                episode=1, search_ordinal=2, trace_id=row["trace_id"],
            )
            for row in first
        }
        self.assertEqual(
            {row["trace_id"]: row["component_scores"]["random_key"] for row in first},
            expected,
        )

    def test_random_key_is_paired_across_actor_and_run_identity(self) -> None:
        candidates = (TraceCandidate("a"), TraceCandidate("b"), TraceCandidate("c"))
        base = SelectionRequest(
            run_id="arm-a-run", task_id="task-a", actor_id="worker-a",
            episode=1, search_ordinal=2, paired_seed=7,
        )
        other = SelectionRequest(
            run_id="arm-b-run", task_id="task-a", actor_id="worker-b",
            episode=1, search_ordinal=2, paired_seed=7,
        )
        selector = RandomSelector({"parameters": {"sample_without_replacement": True}})
        self.assertEqual(selector.rank(_snapshot(*candidates, request=base)),
                         selector.rank(_snapshot(*candidates, request=other)))

    def test_request_and_seed_counters_are_strict_nonnegative_integers(self) -> None:
        for field in ("episode", "search_ordinal", "paired_seed", "max_items",
                      "context_token_budget"):
            for value in (-1, True, 1.5, "1"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        SelectionRequest(**{field: value})
        for kwargs in (
            {"paired_seed": -1},
            {"paired_seed": True},
            {"episode": -1},
            {"search_ordinal": True},
        ):
            with self.subTest(kwargs=kwargs):
                seed_args = {"paired_seed": 7}
                seed_args.update(kwargs)
                with self.assertRaises(ValueError):
                    derive_seed(**seed_args)

    def test_random_rejects_invalid_mapping_request_counters(self) -> None:
        selector = RandomSelector({"parameters": {"sample_without_replacement": True}})
        candidate = {"trace_id": "a"}
        for field, value in (("paired_seed", True), ("episode", 1.5),
                             ("search_ordinal", -1)):
            with self.subTest(field=field, value=value):
                snapshot = {"request": {field: value}, "eligible": [candidate]}
                with self.assertRaises(ValueError):
                    selector.rank(snapshot)

    def test_recency_rejects_non_integer_or_negative_commit_sequence(self) -> None:
        selector = RecencySelector({"parameters": {"primary_sort": RECENCY_PRIMARY_SORT}})
        for value in (True, 1.5, "4", -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    selector.rank(_snapshot(TraceCandidate("bad", commit_seq=value)))

    def test_random_rejects_duplicate_trace_ids(self) -> None:
        selector = RandomSelector({"parameters": {"sample_without_replacement": True}})
        with self.assertRaisesRegex(ValueError, "unique trace_id"):
            selector.rank(_snapshot(TraceCandidate("same"), TraceCandidate("same")))


if __name__ == "__main__":
    unittest.main()
