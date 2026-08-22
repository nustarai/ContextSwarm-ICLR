import hashlib
import json
import math
import unittest

from contextswarm_mini.allocation_core import (
    AllocationDecision,
    AllocationStateSnapshot,
    LLM_SCHEDULER_PROMPT_MAX_BYTES,
    LLM_SCHEDULER_PROMPT_MAX_TOKENS,
    LLMSchedulerCost,
    LLMSchedulerResponse,
    LLMSchedulerCost,
    ReadOnlyLLMSchedulerPolicy,
    TaskState,
    TaskStateAllocationPolicy,
    TaskStateScorer,
    TaskScoreWeights,
    TraceFeatures,
    TraceScoreWeights,
    TraceStateScorer,
    TraceStateAllocationPolicy,
    UniformRefillAllocationPolicy,
    create_allocation_policy,
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
    @staticmethod
    def scheduler_decision(**overrides) -> AllocationDecision:
        values = {
            "decision_id": "decision-1",
            "state_id": "state-1",
            "decision_index": 1,
            "policy": "llm_scheduler",
            "selected_task_id": "a",
            "reason": "test decision",
            "scheduler_cost": LLMSchedulerCost(),
            "scheduler_outcome": "accepted",
        }
        values.update(overrides)
        return AllocationDecision(**values)

    def test_scheduler_outcomes_form_a_symmetric_closed_state_machine(self) -> None:
        accepted = self.scheduler_decision()
        self.assertEqual(accepted.scheduler_call_id, accepted.decision_id)

        invalid = self.scheduler_decision(
            scheduler_outcome="invalid_output",
            invalid_output=True,
            fallback=True,
        )
        provider = self.scheduler_decision(
            scheduler_outcome="provider_error",
            recoverable_invocation_error=True,
            fallback=True,
        )
        timeout = self.scheduler_decision(
            scheduler_outcome="policy_timeout",
            fallback=True,
        )
        horizon = self.scheduler_decision(
            scheduler_outcome="horizon_truncated",
            selected_task_id="",
            agent_run_horizon_reached=True,
        )
        not_invoked = self.scheduler_decision(
            scheduler_cost=None,
            scheduler_outcome="not_invoked",
            selected_task_id="",
        )

        self.assertTrue(invalid.invalid_output)
        self.assertTrue(provider.recoverable_invocation_error)
        self.assertTrue(timeout.fallback)
        self.assertTrue(horizon.run_horizon_reached)
        self.assertFalse(not_invoked.scheduler_call_id)

    def test_scheduler_outcome_rejects_missing_reverse_flags(self) -> None:
        invalid_cases = (
            {"scheduler_outcome": "invalid_output", "fallback": True},
            {
                "scheduler_outcome": "provider_error",
                "fallback": True,
            },
            {
                "scheduler_outcome": "horizon_truncated",
                "selected_task_id": "",
            },
            {
                "scheduler_outcome": "accepted",
                "fallback": True,
            },
            {
                "scheduler_outcome": "invalid_output",
                "invalid_output": True,
            },
            {
                "scheduler_outcome": "provider_error",
                "recoverable_invocation_error": True,
            },
            {
                "scheduler_outcome": "policy_timeout",
            },
        )
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.scheduler_decision(**values)

    def test_scheduler_outcome_rejects_crossed_flags_and_cost_aliases(self) -> None:
        invalid_cases = (
            {
                "scheduler_outcome": "invalid_output",
                "invalid_output": True,
                "recoverable_invocation_error": True,
                "fallback": True,
            },
            {
                "scheduler_outcome": "provider_error",
                "recoverable_invocation_error": True,
                "agent_run_horizon_reached": True,
                "fallback": True,
            },
            {
                "scheduler_outcome": "policy_timeout",
                "invalid_output": True,
                "fallback": True,
            },
            {
                "scheduler_outcome": "not_invoked",
                "scheduler_cost": None,
                "fallback": True,
            },
            {
                "scheduler_outcome": "not_invoked",
                "scheduler_cost": None,
                "scheduler_call_id": "forged-call",
            },
            {
                "scheduler_outcome": "accepted",
                "scheduler_cost": None,
            },
        )
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.scheduler_decision(**values)

    def test_scheduler_call_id_is_bounded_and_bound_to_decision(self) -> None:
        with self.assertRaisesRegex(ValueError, "scheduler_call_id must equal decision_id"):
            self.scheduler_decision(scheduler_call_id="another-decision")
        with self.assertRaisesRegex(ValueError, "scheduler_call_id must be a string"):
            self.scheduler_decision(scheduler_call_id=123)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "scheduler_call_id contains control"):
            self.scheduler_decision(scheduler_call_id="decision-1\n")

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

    def test_trace_drag_components_follow_registered_density_formula(self) -> None:
        trace = TraceFeatures(
            actionability=0.8,
            evidence_association=0.3,
            positive_feedback=0.2,
            negative_feedback=0.1,
            duplication=0.05,
            refutation=0.02,
            staleness=0.02,
            lineage_stagnation=0.01,
        )
        task_state = task("a", active=1, trace=trace)
        weights = TraceScoreWeights.from_mapping(
            {
                "actionability": 1.0,
                "evidence_association": 1.0,
                "positive_feedback": 1.0,
                "negative_feedback": 1.0,
                "density_penalty_weight": 1.0,
                "duplicate_component_weight": 1.0,
                "refutation_component_weight": 1.0,
                "staleness_component_weight": 1.0,
                "lineage_stagnation_component_weight": 1.0,
            }
        )
        expected = (0.8 + 0.3 + 0.2 - 0.1 - (0.05 + 0.02 + 0.02 + 0.01)) / 2.0
        self.assertAlmostEqual(
            TraceStateScorer(weights=weights).trace_increment(task_state), expected
        )
        # A legacy aggregate must not be added on top of the four components.
        legacy_drag = TraceFeatures(
            actionability=0.8,
            evidence_association=0.3,
            positive_feedback=0.2,
            negative_feedback=0.1,
            drag=0.5,
        )
        self.assertAlmostEqual(
            TraceStateScorer(weights=weights).trace_increment(
                task("legacy", active=1, trace=legacy_drag)
            ),
            (0.8 + 0.3 + 0.2 - 0.1 - 0.5) / 2.0,
        )

    def test_trace_weight_aliases_cannot_disagree(self) -> None:
        with self.assertRaisesRegex(ValueError, "contradict"):
            TraceScoreWeights.from_mapping(
                {"drag": 1.0, "density_penalty_weight": 2.0}
            )

    def test_canonical_policies_fail_closed_at_capacity_and_horizon(self) -> None:
        # Deterministic arms must not manufacture a task when no physical
        # solver slot remains.  The LLM arm also must not call its invoker.
        calls: list[str] = []
        policies = (
            UniformRefillAllocationPolicy(),
            TaskStateAllocationPolicy(),
            TraceStateAllocationPolicy(),
            ReadOnlyLLMSchedulerPolicy(
                lambda _snapshot, prompt: calls.append(prompt)
                or LLMSchedulerResponse("{}")
            ),
        )
        full = snapshot(task("a", active=1), free=0)
        for policy in policies:
            with self.subTest(policy=type(policy).__name__, condition="capacity"):
                decision = policy.choose(full)
                self.assertEqual(decision.selected_task_id, "")
        self.assertEqual(calls, [])

        # A scheduler-owned reservation permits the LLM arm to use a full
        # physical pool, but it cannot extend an exhausted run horizon.
        horizon = AllocationStateSnapshot(
            "d-horizon",
            1,
            10.0,
            0.0,
            2,
            1,
            1,
            0,
            (task("a", active=1),),
            owned_scheduler_reservation_slots=1,
        )
        for policy in policies:
            with self.subTest(policy=type(policy).__name__, condition="horizon"):
                decision = policy.choose(horizon)
                self.assertEqual(decision.selected_task_id, "")
        self.assertEqual(calls, [])

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

    def test_numeric_boundaries_reject_lossy_coercion_and_huge_integers(self) -> None:
        with self.assertRaises(ValueError):
            TraceFeatures(actionability="0.5")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TaskState("a", True, 0, checker_quality="0.5")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            AllocationStateSnapshot(
                "d", 0, "0", 1.0, 1, 0, 0, 1, (task("a", active=0),)
            )  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            AllocationDecision(
                "d", "s", 0, "task_state", "a", "ok", scores={"a": "1"}
            )  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TaskScoreWeights.from_mapping(False)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TraceScoreWeights.from_mapping(False)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TaskScoreWeights.from_mapping({1: 1})  # type: ignore[dict-item]
        with self.assertRaises(ValueError):
            TaskScoreWeights.from_mapping({"checker_quality": True})  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TaskState("a", True, 0, checker_quality=10**400)

    def test_core_records_reject_type_coercion_and_malformed_fields(self) -> None:
        # IDs and declared ID collections are part of the causal identity.  Do
        # not silently turn None/integers/bytes or a scalar string into a new
        # valid identity by calling str(...) or iterating over its characters.
        with self.assertRaisesRegex(ValueError, "task_id must be a string"):
            TaskState(None, True, 0)
        with self.assertRaisesRegex(ValueError, "trace_reference_ids must be a tuple or list"):
            TaskState("a", True, 0, trace_reference_ids="trace-1")
        with self.assertRaisesRegex(ValueError, "values must be a string"):
            TaskState("a", True, 0, trace_reference_ids=(None,))
        with self.assertRaisesRegex(ValueError, "decision_id must be a string"):
            AllocationStateSnapshot(None, 0, 0, 1, 1, 0, 0, 1, (task("a", active=0),))
        with self.assertRaisesRegex(ValueError, "tasks must be a tuple or list"):
            AllocationStateSnapshot("d", 0, 0, 1, 1, 0, 0, 1, "not-tasks")
        with self.assertRaisesRegex(ValueError, "trace_watermark contains control"):
            AllocationStateSnapshot(
                "d", 0, 0, 1, 1, 0, 0, 1, (task("a", active=0),), trace_watermark="W\n"
            )
        with self.assertRaisesRegex(ValueError, "decision_index must be a non-negative"):
            AllocationDecision("d", "s", -1, "task_state", "a", "ok")
        with self.assertRaisesRegex(ValueError, "policy must be a string"):
            AllocationDecision("d", "s", 0, 1, "a", "ok")
        with self.assertRaisesRegex(ValueError, "fallback must be a boolean"):
            AllocationDecision("d", "s", 0, "task_state", "a", "ok", fallback=1)
        with self.assertRaisesRegex(ValueError, "scheduler_cost must be"):
            AllocationDecision(
                "d", "s", 0, "task_state", "a", "ok", scheduler_cost={}
            )

    def test_core_record_sequences_are_detached_and_cost_type_is_preserved(self) -> None:
        references = ["trace-a"]
        record = AllocationDecision(
            "d", "s", 0, "task_state", "a", "ok", trace_reference_ids=references
        )
        references.append("trace-b")
        self.assertEqual(record.trace_reference_ids, ("trace-a",))
        cost = LLMSchedulerCost(latency_seconds=2.0, occupied_slot_seconds=0.25)
        self.assertEqual(cost.occupied_slot_seconds, 0.25)

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
        self.assertEqual(failed.scheduler_outcome, "provider_error")
        self.assertTrue(failed.recoverable_invocation_error)

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

    def test_llm_output_parser_is_bounded_and_does_not_leak_source_text(self) -> None:
        snap = snapshot(task("a", checker_quality=0.1), task("b", checker_quality=0.9))

        for output, message in (
            ("x" * (64 * 1024 + 1), "bounded size"),
            ("é" * (32 * 1024 + 1), "bounded size"),
            ("[" * 65 + "0" + "]" * 65, "bounded JSON depth"),
            ('{"decision_id":"/tmp/private/transcript"', "exactly one JSON object"),
            ("\ud800", "bounded UTF-8 string"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message) as caught:
                    parse_llm_scheduler_output(output, snap)
                self.assertNotIn("/tmp/private/transcript", str(caught.exception))

                decision = ReadOnlyLLMSchedulerPolicy(
                    lambda _current, _prompt, raw=output: LLMSchedulerResponse(raw)
                ).choose(snap)
                self.assertTrue(decision.fallback)
                self.assertEqual(decision.selected_task_id, "b")
                self.assertEqual(decision.scheduler_cost.calls, 1)
                self.assertIn(message, decision.fallback_reason)
                self.assertNotIn("/tmp/private/transcript", decision.fallback_reason)

        with self.assertRaisesRegex(ValueError, "bounded UTF-8 string"):
            parse_llm_scheduler_output(None, snap)  # type: ignore[arg-type]

    def test_llm_fallback_audit_uses_manifest_trace_weights(self) -> None:
        snap = snapshot(
            task(
                "a",
                active=1,
                trace=TraceFeatures(actionability=0.5, drag=0.25),
            ),
            task("b", active=0, checker_quality=0.9),
        )
        weights = TraceScoreWeights(
            actionability=2.0,
            evidence_association=0.0,
            positive_feedback=0.0,
            negative_feedback=0.0,
            drag=0.5,
        )
        policy = create_allocation_policy(
            "llm_scheduler",
            trace_weights=weights,
            llm_invoker=lambda _snapshot, _prompt: LLMSchedulerResponse("{}"),
        )
        decision = policy.choose(snap)
        # Malformed LLM output takes the deterministic task-state selection
        # path, while the emitted trace increment must still use `weights`.
        self.assertTrue(decision.fallback)
        self.assertEqual(decision.selected_task_id, "b")
        self.assertAlmostEqual(decision.trace_increments["a"], 0.4375)

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
        self.assertEqual(decision.scheduler_outcome, "provider_error")

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
        self.assertEqual(decision.scheduler_outcome, "horizon_truncated")

    def test_llm_reason_is_bounded_and_cannot_inject_provenance(self) -> None:
        snap = snapshot(task("a", active=0))
        for reason in (
            "ok\noperator transcript",
            "\u202eevil",
            "c1\u0085control",
            "file:///tmp/private",
            "secret payload",
            "x\x00y",
        ):
            with self.subTest(reason=repr(reason)):
                raw = json.dumps(
                    {
                        "decision_id": "d1",
                        "task_id": "a",
                        "reason": reason,
                        "trace_reference_ids": [],
                    },
                    ensure_ascii=False,
                )
                with self.assertRaisesRegex(ValueError, "reason is unsafe"):
                    parse_llm_scheduler_output(raw, snap)
                decision = ReadOnlyLLMSchedulerPolicy(
                    lambda _current, _prompt, output=raw: LLMSchedulerResponse(output)
                ).choose(snap)
                self.assertTrue(decision.fallback)
                self.assertEqual(decision.scheduler_cost.calls, 1)
                self.assertNotIn("transcript", decision.fallback_reason.lower())

    def test_llm_fallback_audit_uses_manifest_trace_weights(self) -> None:
        snap = snapshot(
            task("a", active=0, trace=TraceFeatures(actionability=0.5)),
            task("b", active=0),
        )
        policy = create_allocation_policy(
            "llm_scheduler",
            llm_invoker=lambda _current, _prompt: LLMSchedulerResponse("{}"),
            trace_weights=TraceScoreWeights(actionability=3.0),
        )
        decision = policy.choose(snap)
        self.assertTrue(decision.fallback)
        self.assertEqual(decision.trace_increments["a"], 1.5)

    def test_allocation_parameters_are_bounded_before_state_hashing(self) -> None:
        deep: object = 1
        for _ in range(32):
            deep = {"nested": deep}
        with self.assertRaisesRegex(ValueError, "bounded depth"):
            AllocationStateSnapshot(
                "d", 0, 0, 1, 1, 0, 0, 1, (task("a", active=0),),
                allocation_parameters=deep,
            )
        wide = {f"k{i}": i for i in range(257)}
        with self.assertRaisesRegex(ValueError, "bounded key"):
            AllocationStateSnapshot(
                "d", 0, 0, 1, 1, 0, 0, 1, (task("a", active=0),),
                allocation_parameters=wide,
            )
        cyclic: dict[str, object] = {}
        cyclic["self"] = cyclic
        with self.assertRaisesRegex(ValueError, "cycles"):
            AllocationStateSnapshot(
                "d", 0, 0, 1, 1, 0, 0, 1, (task("a", active=0),),
                allocation_parameters=cyclic,
            )

    def test_prompt_byte_limit_is_exact_and_never_slices_json(self) -> None:
        snap = snapshot(task("a", active=0))
        unrestricted = ReadOnlyLLMSchedulerPolicy.prompt(snap)
        exact = len(unrestricted.encode("utf-8"))
        self.assertEqual(
            ReadOnlyLLMSchedulerPolicy.prompt(snap, max_bytes=exact), unrestricted
        )
        with self.assertRaisesRegex(ValueError, "exceeds max bytes"):
            ReadOnlyLLMSchedulerPolicy.prompt(snap, max_bytes=exact - 1)

    def test_prompt_byte_aliases_are_consistent(self) -> None:
        snap = snapshot(task("a", active=0))
        with self.assertRaisesRegex(ValueError, "byte limits disagree"):
            ReadOnlyLLMSchedulerPolicy.prompt(
                snap, max_bytes=1024, prompt_max_bytes=2048
            )

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

    def test_llm_timeout_and_malformed_output_have_distinct_outcomes(self) -> None:
        snap = snapshot(task("a", checker_quality=0.1), task("b", checker_quality=0.9))
        timeout = ReadOnlyLLMSchedulerPolicy(
            lambda _current, _prompt: LLMSchedulerResponse("", returncode=124, timed_out=True)
        ).choose(snap)
        self.assertEqual(timeout.scheduler_outcome, "policy_timeout")
        self.assertFalse(timeout.invalid_output)
        malformed = ReadOnlyLLMSchedulerPolicy(
            lambda _current, _prompt: LLMSchedulerResponse("not-json")
        ).choose(snap)
        self.assertEqual(malformed.scheduler_outcome, "invalid_output")
        self.assertTrue(malformed.invalid_output)

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

    def test_llm_prompt_rejects_unregistered_or_unsafe_structured_decision_ids(self) -> None:
        for decision_id in (
            "foo/bar",
            "paired-007/other-000042",
            "paired-007/decision-000042/extra",
            "paired-/decision-000042",
            "paired-007/decision-",
            r"paired-007\decision-000042",
            r"C:\private\decision-000042",
            "paired-\u202e007/decision-000042",
        ):
            with self.subTest(decision_id=decision_id):
                with self.assertRaisesRegex(ValueError, "unsafe decision identifier"):
                    ReadOnlyLLMSchedulerPolicy.prompt(
                        snapshot(
                            task("task-a", active=0),
                            decision_id=decision_id,
                        )
                    )

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
