import hashlib
import json
import math
import unittest

from contextswarm_mini.allocation_core import (
    AllocationStateSnapshot,
    LLMSchedulerResponse,
    ReadOnlyLLMSchedulerPolicy,
    TaskState,
    TaskStateAllocationPolicy,
    TaskStateScorer,
    TraceFeatures,
    TraceStateAllocationPolicy,
    UniformRefillAllocationPolicy,
    parse_llm_scheduler_output,
)


def task(task_id: str, active: int = 1, *, trace: TraceFeatures | None = None, **kwargs) -> TaskState:
    return TaskState(task_id, True, active, trace=trace or TraceFeatures(), **kwargs)


def snapshot(*tasks: TaskState, free: int = 1, decision_id: str = "d1") -> AllocationStateSnapshot:
    return AllocationStateSnapshot(
        decision_id,
        1,
        10.0,
        20.0,
        sum(item.active_allocations for item in tasks) + free,
        sum(item.active_allocations for item in tasks),
        0,
        free,
        tuple(tasks),
    )


class AllocationCoreTests(unittest.TestCase):
    def test_uniform_refill_uses_live_count_and_lexical_tie(self) -> None:
        first = snapshot(task("z"), task("a"), free=1)
        second = snapshot(task("a"), task("z"), free=1)
        self.assertEqual(UniformRefillAllocationPolicy().choose(first).selected_task_id, "a")
        self.assertEqual(UniformRefillAllocationPolicy().choose(second).selected_task_id, "a")

    def test_task_state_formula_and_trace_isolation(self) -> None:
        ordinary = task(
            "a",
            active=1,
            checker_quality=0.8,
            recent_progress=0.4,
            starvation=0.2,
            failure_no_progress=0.1,
        )
        changed_trace = task(
            "a",
            active=1,
            checker_quality=0.8,
            recent_progress=0.4,
            starvation=0.2,
            failure_no_progress=0.1,
            trace=TraceFeatures(actionability=1, evidence_association=1, positive_feedback=1),
        )
        base = TaskStateScorer().score_task(ordinary)
        self.assertEqual(base, TaskStateScorer().score_task(changed_trace))
        self.assertAlmostEqual(base, (0.8 + 0.4 + 0.2 - 0.1) / 2)

    def test_trace_zero_increment_is_exact_task_state(self) -> None:
        snap = snapshot(task("b", checker_quality=0.3), task("a", checker_quality=0.3))
        task_decision = TaskStateAllocationPolicy().choose(snap)
        trace_decision = TraceStateAllocationPolicy().choose(snap)
        self.assertEqual(task_decision.selected_task_id, trace_decision.selected_task_id)
        self.assertEqual(dict(task_decision.scores), dict(trace_decision.scores))
        self.assertEqual(dict(trace_decision.trace_increments), {"a": 0.0, "b": 0.0})

    def test_state_id_is_reproducible_and_tamper_evident(self) -> None:
        snap = snapshot(task("a"), task("b"))
        public = snap.public_dict()
        state_id = public.pop("state_id")
        canonical = json.dumps(public, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(state_id, hashlib.sha256(canonical.encode()).hexdigest())
        altered = snapshot(task("a", active=0), task("b"))
        self.assertNotEqual(state_id, altered.state_id)

    def test_capacity_identity_and_nonfinite_rejection(self) -> None:
        with self.assertRaises(ValueError):
            AllocationStateSnapshot("d", 1, 0, 1, 2, 1, 0, 0, (task("a"),))
        with self.assertRaises(ValueError):
            task("a", checker_quality=math.nan)
        with self.assertRaises(ValueError):
            TraceFeatures(drag=math.inf)

    def test_strict_llm_parser_and_task_state_fallback(self) -> None:
        snap = snapshot(
            task("a", checker_quality=0.1, trace=TraceFeatures(actionability=0.5)),
            task("b", checker_quality=0.9),
        )
        raw = json.dumps({"decision_id": "d1", "task_id": "a", "reason": "route", "trace_reference_ids": []})
        self.assertEqual(parse_llm_scheduler_output(raw, snap)[0], "a")
        with self.assertRaises(ValueError):
            parse_llm_scheduler_output(raw[:-1], snap)
        with self.assertRaises(ValueError):
            parse_llm_scheduler_output(raw.replace("}", ',"task_id":"b"}'), snap)
        policy = ReadOnlyLLMSchedulerPolicy(lambda current, prompt: LLMSchedulerResponse("{}"))
        decision = policy.choose(snap)
        self.assertTrue(decision.fallback)
        self.assertEqual(decision.selected_task_id, "b")
        self.assertEqual(dict(decision.task_scores), dict(TaskStateScorer().score_snapshot(snap)))
        self.assertEqual(decision.trace_increments["a"], 0.25)

        failed = ReadOnlyLLMSchedulerPolicy(
            lambda current, prompt: (_ for _ in ()).throw(ConnectionError("offline"))
        ).choose(snap)
        self.assertTrue(failed.fallback)
        self.assertEqual(failed.selected_task_id, "b")
        self.assertIn("ConnectionError", failed.fallback_reason)
        self.assertEqual(failed.scheduler_cost.calls, 1)

    def test_llm_does_not_call_without_admission_capacity(self) -> None:
        calls = []
        snap = AllocationStateSnapshot("d", 1, 0, 1, 1, 1, 0, 0, (task("a"),))
        decision = ReadOnlyLLMSchedulerPolicy(
            lambda current, prompt: calls.append(prompt)
        ).choose(snap)
        self.assertEqual(calls, [])
        self.assertEqual(decision.selected_task_id, "")
        self.assertIsNone(decision.scheduler_cost)

    def test_llm_invalid_invoker_response_is_one_charged_fallback(self) -> None:
        snap = snapshot(task("a", checker_quality=0.1), task("b", checker_quality=0.9))
        decision = ReadOnlyLLMSchedulerPolicy(
            lambda _current, _prompt: None
        ).choose(snap)
        self.assertTrue(decision.fallback)
        self.assertEqual(decision.selected_task_id, "b")
        self.assertEqual(decision.scheduler_cost.calls, 1)
        self.assertIn("invalid response", decision.fallback_reason)

    def test_llm_can_use_its_owned_scheduler_reservation(self) -> None:
        calls = []
        snap = AllocationStateSnapshot(
            "d",
            1,
            0,
            1,
            2,
            1,
            1,
            0,
            (task("a"),),
            owned_scheduler_reservation_slots=1,
        )

        def invoke(current, prompt):
            calls.append((current.state_id, prompt))
            return LLMSchedulerResponse(
                json.dumps(
                    {
                        "decision_id": "d",
                        "task_id": "a",
                        "reason": "admit through owned reservation",
                        "trace_reference_ids": [],
                    }
                )
            )

        decision = ReadOnlyLLMSchedulerPolicy(invoke).choose(snap)
        self.assertEqual(len(calls), 1)
        self.assertEqual(decision.selected_task_id, "a")
        self.assertFalse(decision.fallback)

    def test_owned_scheduler_reservation_is_validated_and_hashed(self) -> None:
        unowned = AllocationStateSnapshot(
            "d", 1, 0, 1, 2, 1, 1, 0, (task("a"),)
        )
        owned = AllocationStateSnapshot(
            "d",
            1,
            0,
            1,
            2,
            1,
            1,
            0,
            (task("a"),),
            owned_scheduler_reservation_slots=1,
        )
        self.assertEqual(unowned.public_dict()["owned_scheduler_reservation_slots"], 0)
        self.assertEqual(owned.public_dict()["owned_scheduler_reservation_slots"], 1)
        self.assertNotEqual(unowned.state_id, owned.state_id)
        calls = []
        unowned_decision = ReadOnlyLLMSchedulerPolicy(
            lambda current, prompt: calls.append(prompt)
        ).choose(unowned)
        self.assertEqual(calls, [])
        self.assertEqual(unowned_decision.selected_task_id, "")

        with self.assertRaisesRegex(ValueError, "must not exceed"):
            AllocationStateSnapshot(
                "d",
                1,
                0,
                1,
                1,
                1,
                0,
                0,
                (task("a"),),
                owned_scheduler_reservation_slots=1,
            )
        with self.assertRaisesRegex(ValueError, "at most 1"):
            AllocationStateSnapshot(
                "d",
                1,
                0,
                1,
                3,
                1,
                2,
                0,
                (task("a"),),
                owned_scheduler_reservation_slots=2,
            )


if __name__ == "__main__":
    unittest.main()
