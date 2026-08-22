import hashlib
import json
import math
import unittest

from contextswarm_mini.allocation_core import (
    AllocationStateSnapshot,
    LLM_SCHEDULER_PROMPT_MAX_BYTES,
    LLM_SCHEDULER_PROMPT_MAX_TOKENS,
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

    def test_llm_wire_level_parser_errors_are_deterministic_fallbacks(self) -> None:
        snap = snapshot(task("a", checker_quality=0.1), task("b", checker_quality=0.9))

        # Duplicate keys are rejected by the parser hook and must be treated
        # like any other malformed provider response, not escape as ValueError
        # from the policy boundary.
        duplicate = (
            '{"decision_id":"d1","task_id":"a","task_id":"b",'
            '"reason":"route","trace_reference_ids":[]}'
        )
        with self.assertRaises(ValueError):
            parse_llm_scheduler_output(duplicate, snap)
        duplicate_decision = ReadOnlyLLMSchedulerPolicy(
            lambda _current, _prompt: LLMSchedulerResponse(duplicate)
        ).choose(snap)
        self.assertTrue(duplicate_decision.fallback)
        self.assertEqual(duplicate_decision.selected_task_id, "b")
        self.assertIn("duplicate", duplicate_decision.fallback_reason)
        self.assertEqual(duplicate_decision.scheduler_cost.calls, 1)

        # Python's JSON decoder accepts NaN/Infinity unless parse_constant is
        # supplied.  Both values are rejected and charged exactly once.
        for constant in ("NaN", "Infinity", "-Infinity"):
            malformed = (
                '{"decision_id":'
                f'{constant},"task_id":"a","reason":"route",'
                '"trace_reference_ids":[]}'
            )
            decision = ReadOnlyLLMSchedulerPolicy(
                lambda _current, _prompt, output=malformed: LLMSchedulerResponse(output)
            ).choose(snap)
            self.assertTrue(decision.fallback)
            self.assertEqual(decision.selected_task_id, "b")
            self.assertEqual(decision.scheduler_cost.calls, 1)

        # A non-string invoker result is another untrusted transport failure.
        wrong_type = ReadOnlyLLMSchedulerPolicy(
            lambda _current, _prompt: {"output": "not a response"}
        ).choose(snap)
        self.assertTrue(wrong_type.fallback)
        self.assertEqual(wrong_type.selected_task_id, "b")
        self.assertEqual(wrong_type.scheduler_cost.calls, 1)

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

    def test_llm_horizon_truncation_is_not_a_policy_fallback(self) -> None:
        snap = snapshot(task("a", checker_quality=0.1), task("b", checker_quality=0.9))
        decision = ReadOnlyLLMSchedulerPolicy(
            lambda _current, _prompt: LLMSchedulerResponse(
                "", returncode=124, timed_out=True, run_horizon_reached=True
            )
        ).choose(snap)
        self.assertTrue(decision.agent_run_horizon_reached)
        self.assertFalse(decision.fallback)
        self.assertEqual(decision.selected_task_id, "")
        self.assertEqual(decision.scheduler_cost.calls, 1)

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
    def test_llm_prompt_is_compact_utf8_bounded_and_private_data_free(self) -> None:
        marker_path = "/operator/private/transcript.lean"
        marker_transcript = "PRIVATE TRANSCRIPT SHOULD NEVER REACH MODEL"
        snap = AllocationStateSnapshot(
            "decision-é",
            2,
            1.5,
            20.0,
            2,
            1,
            0,
            1,
            (
                TaskState(
                    "任务-é",
                    True,
                    1,
                    trace=TraceFeatures(actionability=0.5),
                    trace_reference_ids=("trace-é",),
                    checker_outcome_ids=("checker-1",),
                ),
            ),
            trace_watermark=marker_path,
            allocation_parameters={
                "task_state": {"checker_quality": 1.0},
                "private_path": marker_path,
                "transcript": marker_transcript,
            },
        )
        prompt = ReadOnlyLLMSchedulerPolicy.prompt(snap)
        self.assertLessEqual(
            len(prompt.encode("utf-8")), LLM_SCHEDULER_PROMPT_MAX_BYTES
        )
        self.assertLessEqual(
            len(prompt.encode("utf-8")), LLM_SCHEDULER_PROMPT_MAX_TOKENS * 4
        )
        self.assertNotIn(marker_path, prompt)
        self.assertNotIn(marker_transcript, prompt)
        self.assertIn("任务-é", prompt)
        self.assertIn("trace-é", prompt)
        # The wire payload is compact JSON, not a byte-truncated fragment.
        payload = json.loads(prompt.split("SNAPSHOT:\n", 1)[1])
        self.assertEqual(payload["decision_id"], "decision-é")
        self.assertEqual(payload["tasks"][0]["task_id"], "任务-é")

    def test_llm_prompt_accepts_registered_paired_decision_identifier(self) -> None:
        snap = snapshot(
            task("task-a", active=0),
            decision_id="paired-007/decision-000042",
        )
        prompt = ReadOnlyLLMSchedulerPolicy.prompt(snap)
        self.assertIn("paired-007/decision-000042", prompt)

    def test_llm_prompt_byte_overflow_is_charged_fallback_without_invocation(self) -> None:
        snap = snapshot(task("a", checker_quality=0.9), task("b"))
        calls: list[tuple[object, str]] = []

        def invoke(current, prompt):
            calls.append((current, prompt))
            return LLMSchedulerResponse("{}")

        decision = ReadOnlyLLMSchedulerPolicy(
            invoke,
            prompt_max_bytes=1,
        ).choose(snap)
        self.assertEqual(calls, [])
        self.assertTrue(decision.fallback)
        self.assertEqual(decision.fallback_reason, "scheduler prompt rejected: size_limit")
        self.assertEqual(decision.selected_task_id, "a")
        self.assertEqual(decision.scheduler_cost.calls, 1)
        self.assertEqual(decision.scheduler_cost.input_tokens, 0)
        self.assertEqual(decision.scheduler_cost.output_tokens, 0)

    def test_llm_prompt_token_overflow_is_charged_fallback_without_invocation(self) -> None:
        snap = snapshot(task("a", checker_quality=0.9), task("b"))
        calls: list[str] = []
        decision = ReadOnlyLLMSchedulerPolicy(
            lambda current, prompt: calls.append(prompt),
            prompt_max_tokens=1,
        ).choose(snap)
        self.assertEqual(calls, [])
        self.assertTrue(decision.fallback)
        self.assertEqual(decision.fallback_reason, "scheduler prompt rejected: token_limit")
        self.assertEqual(decision.scheduler_cost.calls, 1)

    def test_llm_unsafe_echo_identifier_fails_closed_without_leaking_value(self) -> None:
        private_identifier = "/tmp/private/transcript"
        snap = snapshot(TaskState(private_identifier, True, 0))
        calls: list[str] = []
        decision = ReadOnlyLLMSchedulerPolicy(
            lambda current, prompt: calls.append(prompt),
            prompt_max_bytes=LLM_SCHEDULER_PROMPT_MAX_BYTES,
        ).choose(snap)
        self.assertEqual(calls, [])
        self.assertTrue(decision.fallback)
        self.assertEqual(decision.fallback_reason, "scheduler prompt rejected: unsafe_snapshot")
        self.assertNotIn(private_identifier, decision.fallback_reason)
        self.assertEqual(decision.scheduler_cost.calls, 1)


if __name__ == "__main__":
    unittest.main()
