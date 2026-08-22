from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from contextswarm_mini.config import load_config
from contextswarm_mini.evaluator import (
    EvaluatorError,
    LeanEvaluator,
    safe_worker_response,
)
from contextswarm_mini.models import Task
from contextswarm_mini.runner import (
    _FrozenCandidate,
    RunLogger,
    _run_closeout,
)


ROOT = Path(__file__).resolve().parents[1]


def _task(root: Path) -> Task:
    return Task(
        slug="task",
        root=root,
        problem_text="",
        baseline_code="import Mathlib\ntheorem task : True := by sorry\n",
        metadata={"problem_id": "Task", "theorem_name": "task"},
    )


class _CountingEvaluator(LeanEvaluator):
    def __init__(self) -> None:
        super().__init__("http://unused", lean_env_id="fixed-env")
        self.payloads: list[dict[str, object]] = []

    def _request(  # type: ignore[no-untyped-def]
        self, method, path, payload=None, *, timeout_seconds=None, cancel_event=None
    ):
        del method, path, timeout_seconds, cancel_event
        self.payloads.append(dict(payload or {}))
        result: dict[str, object] = {
            "job_id": "counting-job",
            "status": "failed",
            "formal_status": "VERIFY_FAIL",
        }
        if payload and payload.get("response_profile") == "lean_probe_v1":
            result["probe_diagnostics"] = {
                "items": [
                    {
                        "severity": "error",
                        "data": "type mismatch",
                        "line": 2,
                        "column": 1,
                        "private": "must not escape",
                    }
                ],
                "truncated": False,
            }
        return result


class _CountingProofEvaluator(_CountingEvaluator):
    def _request(  # type: ignore[no-untyped-def]
        self, method, path, payload=None, *, timeout_seconds=None, cancel_event=None
    ):
        del method, path, timeout_seconds, cancel_event
        self.payloads.append(dict(payload or {}))
        return {
            "job_id": f"counting-proof-job-{len(self.payloads)}",
            "status": "succeeded",
            "formal_status": "PROVED",
            "is_valid_no_sorry": True,
        }


class _OneFailureEvaluator(_CountingEvaluator):
    def _request(  # type: ignore[no-untyped-def]
        self, method, path, payload=None, *, timeout_seconds=None, cancel_event=None
    ):
        del method, path, timeout_seconds, cancel_event
        self.payloads.append(dict(payload or {}))
        if len(self.payloads) == 1:
            raise EvaluatorError("temporary")
        return {
            "job_id": "one-failure-job",
            "status": "failed",
            "formal_status": "VERIFY_FAIL",
        }


class _DeadlineRecordingEvaluator(_CountingEvaluator):
    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float | None] = []

    def _request(  # type: ignore[no-untyped-def]
        self, method, path, payload=None, *, timeout_seconds=None, cancel_event=None
    ):
        del cancel_event
        self.timeouts.append(timeout_seconds)
        return super()._request(
            method, path, payload, timeout_seconds=timeout_seconds
        )


class _OverloadedEvaluator(_CountingEvaluator):
    def _request(  # type: ignore[no-untyped-def]
        self, method, path, payload=None, *, timeout_seconds=None, cancel_event=None
    ):
        del method, path, payload, timeout_seconds, cancel_event
        raise EvaluatorError(
            "https://judge.invalid/private token=test-secret /home/test/source.lean",
            category="judge_overloaded_deadline",
            http_status=429,
            attempts=7,
            retry_after_seconds=12.5,
        )


class _UnsafeResponseEvaluator(_CountingEvaluator):
    def _request(  # type: ignore[no-untyped-def]
        self, method, path, payload=None, *, timeout_seconds=None, cancel_event=None
    ):
        del method, path, payload, timeout_seconds, cancel_event
        return {
            "status": "failed",
            "formal_status": "VERIFY_FAIL",
            "job_id": "https://judge.invalid/private-job",
            "error_message": (
                "Bearer test-bearer at /tmp/private/source.lean "
                "via https://judge.invalid/private sk-abcdefghijklmnopqrstuv"
            ),
            "private": "must-not-escape",
            "probe_diagnostics": {
                "items": [
                    {
                        "severity": "error",
                        "data": "token=test-token /home/test/private.lean",
                        "line": 2,
                        "column": 1,
                    }
                ]
            },
        }


class _RunHorizonExpiredEvaluator(_CountingEvaluator):
    def __init__(self, clock: list[float]) -> None:
        super().__init__()
        self.clock = clock

    def _request(  # type: ignore[no-untyped-def]
        self, method, path, payload=None, *, timeout_seconds=None, cancel_event=None
    ):
        del method, path, payload, timeout_seconds, cancel_event
        self.clock[0] = 2.0
        raise EvaluatorError(
            "The Judge request deadline elapsed.",
            category="request_deadline_elapsed",
            attempts=1,
        )


class EvaluatorProbeTests(unittest.TestCase):
    def test_public_evaluate_accepts_cancel_event_without_submitting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            candidate.write_text(
                "import Mathlib\ntheorem task : True := by trivial\n",
                encoding="utf-8",
            )
            cancelled = threading.Event()
            cancelled.set()
            evaluator = _CountingEvaluator()
            verdict = evaluator.evaluate(
                _task(root), candidate, cancel_event=cancelled
            )

        self.assertEqual(verdict.status, "TASK_CANCELLED")
        self.assertEqual(evaluator.payloads, [])

    def test_exact_probe_verdict_is_reused_by_final_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            candidate.write_text("import Mathlib\ntheorem task : True := by trivial\n")
            evaluator = _CountingEvaluator()
            probe = evaluator.probe(_task(root), candidate)
            final = evaluator.evaluate(_task(root), candidate)

            self.assertEqual(len(evaluator.payloads), 1)
            self.assertEqual(evaluator.payloads[0]["lean_env_id"], "fixed-env")
            self.assertEqual(evaluator.payloads[0]["response_profile"], "lean_probe_v1")
            self.assertEqual(probe.status, "VERIFY_FAIL")
            self.assertEqual(final.status, "VERIFY_FAIL")
            self.assertRegex(str(probe.candidate_sha256), r"^[0-9a-f]{64}$")
            self.assertRegex(str(probe.task_contract_sha256), r"^[0-9a-f]{64}$")
            self.assertEqual(final.candidate_sha256, probe.candidate_sha256)
            self.assertEqual(final.task_contract_sha256, probe.task_contract_sha256)
            self.assertTrue(final.cache_reused)
            self.assertTrue(final.response["probe_cache_reused"])
            diagnostic = final.response["probe_diagnostics"]["items"][0]
            self.assertEqual(set(diagnostic), {"severity", "data", "line", "column"})

            candidate.write_text(candidate.read_text() + "\n")
            evaluator.evaluate(_task(root), candidate)
            self.assertEqual(len(evaluator.payloads), 2)
            self.assertNotIn("response_profile", evaluator.payloads[1])

    def test_evaluator_error_probe_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            candidate.write_text("import Mathlib\ntheorem task : True := by trivial\n")
            evaluator = _OneFailureEvaluator()
            self.assertEqual(evaluator.probe(_task(root), candidate).status, "EVALUATOR_ERROR")
            self.assertEqual(evaluator.evaluate(_task(root), candidate).status, "VERIFY_FAIL")
            self.assertEqual(len(evaluator.payloads), 2)

    def test_broker_source_snapshot_reuses_only_an_exact_final_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            source = "import Mathlib\ntheorem task : True := by trivial\n"
            candidate.write_text(source, encoding="utf-8")
            evaluator = _CountingEvaluator()

            evaluator.probe_source(_task(root), source)
            evaluator.probe_source(_task(root), source)
            exact = evaluator.evaluate(_task(root), candidate)
            self.assertEqual(len(evaluator.payloads), 1)
            self.assertTrue(exact.response["probe_cache_reused"])

            candidate.write_text(source + "\n-- changed\n", encoding="utf-8")
            evaluator.evaluate(_task(root), candidate)
            self.assertEqual(len(evaluator.payloads), 2)

    def test_runner_closeout_forces_fresh_remote_receipt_after_broker_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = _task(root)
            source = "import Mathlib\ntheorem task : True := by trivial\n"
            candidate = root / "closeout_candidates" / task.slug / "result.lean"
            candidate.parent.mkdir(parents=True)
            candidate.write_text(source, encoding="utf-8")
            evaluator = _CountingProofEvaluator()

            prior = evaluator.probe_source(task, source)
            config = load_config(ROOT / "configs" / "smoke.toml", ROOT)
            verdicts = _run_closeout(
                config,
                [task],
                {
                    task.slug: _FrozenCandidate(
                        task.slug,
                        candidate,
                        prior.candidate_sha256,
                    )
                },
                RunLogger(root),
                evaluator,
                threading.BoundedSemaphore(1),
                reusable_verdicts=[prior],
            )

            events = [
                json.loads(line)
                for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(len(evaluator.payloads), 2)
        self.assertEqual(
            evaluator.payloads[0]["response_profile"],
            "lean_probe_v1",
        )
        self.assertNotIn("response_profile", evaluator.payloads[1])
        self.assertEqual(prior.judge_job_id, "counting-proof-job-1")
        self.assertEqual(verdicts[task.slug].judge_job_id, prior.judge_job_id)
        confirmation = next(
            row for row in events if row["event"] == "closeout_authority_confirmed"
        )
        self.assertEqual(confirmation["prior_judge_job_id"], prior.judge_job_id)
        self.assertEqual(
            confirmation["observed_judge_job_id"],
            "counting-proof-job-2",
        )
        self.assertNotEqual(
            confirmation["observed_judge_job_id"],
            confirmation["prior_judge_job_id"],
        )

    def test_evaluation_budget_is_clamped_to_exact_run_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            candidate.write_text("import Mathlib\ntheorem task : True := by trivial\n")
            evaluator = _DeadlineRecordingEvaluator()
            verdict = evaluator.probe(
                _task(root),
                candidate,
                deadline_monotonic=time.monotonic() + 1.3,
            )
            self.assertEqual(verdict.status, "VERIFY_FAIL")
            self.assertEqual(evaluator.payloads[0]["timeout"], 1)
            self.assertGreater(evaluator.timeouts[0] or 0.0, 0.0)
            self.assertLessEqual(evaluator.timeouts[0] or 2.0, 1.3)

    def test_evaluator_total_deadline_includes_admission_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            candidate.write_text("import Mathlib\ntheorem task : True := by trivial\n")
            evaluator = _DeadlineRecordingEvaluator()
            verdict = evaluator.probe(
                _task(root),
                candidate,
                deadline_monotonic=time.monotonic() + 400.0,
            )
            self.assertEqual(verdict.status, "VERIFY_FAIL")
            self.assertEqual(evaluator.payloads[0]["timeout"], 300)
            self.assertGreater(evaluator.timeouts[0] or 0.0, 399.0)

    def test_run_horizon_expiry_is_not_reported_as_infrastructure_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            candidate.write_text("import Mathlib\ntheorem task : True := by trivial\n")
            clock = [0.0]
            evaluator = _RunHorizonExpiredEvaluator(clock)
            with patch(
                "contextswarm_mini.evaluator.time.monotonic",
                side_effect=lambda: clock[0],
            ):
                verdict = evaluator.probe(
                    _task(root), candidate, deadline_monotonic=1.5
                )
            self.assertEqual(verdict.status, "OUT_OF_HORIZON")
            self.assertEqual(
                verdict.response["evaluator_failure"]["category"],
                "request_deadline_elapsed",
            )

    def test_overload_exhaustion_has_auditable_nonsecret_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            candidate.write_text("import Mathlib\ntheorem task : True := by trivial\n")
            verdict = _OverloadedEvaluator().probe(_task(root), candidate)
            rendered = json.dumps(verdict.as_dict(), ensure_ascii=False)
            self.assertEqual(verdict.status, "REJECTED_OVERLOADED")
            self.assertEqual(
                verdict.response["evaluator_failure"],
                {
                    "category": "judge_overloaded_deadline",
                    "attempts": 7,
                    "http_status": 429,
                    "retry_after_seconds": 12.5,
                },
            )
            self.assertNotIn("judge.invalid", rendered)
            self.assertNotIn("test-secret", rendered)
            self.assertNotIn("/home/test", rendered)

    def test_evaluator_sanitizes_remote_response_before_returning_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            candidate.write_text("import Mathlib\ntheorem task : True := by trivial\n")
            verdict = _UnsafeResponseEvaluator().probe(_task(root), candidate)
            rendered = json.dumps(verdict.as_dict(), ensure_ascii=False)
            for forbidden in (
                "judge.invalid",
                "test-bearer",
                "test-token",
                "sk-abcdefghijklmnopqrstuv",
                "/tmp/private",
                "/home/test",
                "must-not-escape",
            ):
                self.assertNotIn(forbidden, rendered)
            self.assertIn("<redacted-", rendered)
            self.assertIsNone(verdict.judge_job_id)

    def test_invalid_endpoint_construction_has_fixed_nonsecret_error(self) -> None:
        private_marker = "operator-private-invalid-url-marker"
        evaluator = LeanEvaluator(
            f"http://judge.invalid/{private_marker}\nfragment",
            lean_env_id="fixed-env",
            timeout_seconds=1,
        )
        with self.assertRaises(EvaluatorError) as raised:
            evaluator.health()

        rendered = "".join(
            traceback.format_exception(
                type(raised.exception),
                raised.exception,
                raised.exception.__traceback__,
            )
        )
        self.assertEqual(
            raised.exception.category,
            "invalid_request_configuration",
        )
        self.assertNotIn(private_marker, str(raised.exception))
        self.assertNotIn(private_marker, rendered)
        self.assertIn("configuration is invalid", str(raised.exception))

    def test_empty_or_malformed_post_503_fails_closed_without_replay(self) -> None:
        headers = Message()
        headers["Retry-After"] = "0"

        for body in (b"", b"{not-json"):
            with self.subTest(body=body):
                attempts = 0

                def unavailable(_request, *, timeout):  # type: ignore[no-untyped-def]
                    nonlocal attempts
                    self.assertGreater(timeout, 0)
                    attempts += 1
                    raise HTTPError(
                        "https://judge.invalid/private",
                        503,
                        "unavailable",
                        headers,
                        BytesIO(body),
                    )

                evaluator = LeanEvaluator(
                    "http://unused", lean_env_id="fixed-env", timeout_seconds=5
                )
                with patch(
                    "contextswarm_mini.evaluator.urlopen",
                    side_effect=unavailable,
                ), patch("contextswarm_mini.evaluator.time.sleep") as sleep:
                    with self.assertRaises(EvaluatorError) as raised:
                        evaluator._request(
                            "POST", "/api/lean/jobs", {"code": "proof"}
                        )

                self.assertEqual(raised.exception.category, "http_error")
                self.assertEqual(raised.exception.http_status, 503)
                self.assertEqual(raised.exception.attempts, 1)
                self.assertEqual(attempts, 1)
                sleep.assert_not_called()

    def test_ambiguous_submission_latches_and_gates_later_entries(self) -> None:
        cases = (
            ("transport_timeout", None, "network_error"),
            ("empty_2xx", b"", "malformed_response"),
            ("malformed_2xx", b"{not-json", "malformed_response"),
            (
                "nonterminal_missing_id",
                b'{"status":"queued"}',
                "missing_job_identifier",
            ),
            (
                "terminal_missing_id",
                b'{"status":"failed","formal_status":"VERIFY_FAIL"}',
                "missing_job_identifier",
            ),
            ("generic_503", None, "http_error"),
        )

        for mode, body, failure_category in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                attempts = 0
                root = Path(temporary)
                candidate = root / "result.lean"
                candidate.write_text(
                    "import Mathlib\ntheorem task : True := by trivial\n",
                    encoding="utf-8",
                )

                class Response:
                    def __enter__(self):  # type: ignore[no-untyped-def]
                        return self

                    def __exit__(self, *_args: object) -> None:
                        return None

                    def read(self) -> bytes:
                        return body or b""

                def fake_urlopen(_request, *, timeout):  # type: ignore[no-untyped-def]
                    nonlocal attempts
                    self.assertGreater(timeout, 0)
                    attempts += 1
                    if mode == "transport_timeout":
                        raise TimeoutError("private transport detail")
                    if mode == "generic_503":
                        raise HTTPError(
                            "https://judge.invalid/private",
                            503,
                            "unavailable",
                            Message(),
                            BytesIO(b""),
                        )
                    return Response()

                evaluator = LeanEvaluator(
                    "http://unused", lean_env_id="fixed-env", timeout_seconds=5
                )
                with patch(
                    "contextswarm_mini.evaluator.urlopen",
                    side_effect=fake_urlopen,
                ):
                    causal = evaluator.evaluate(_task(root), candidate)
                    gated = evaluator.probe(_task(root), candidate)

                self.assertEqual(causal.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
                self.assertIs(
                    causal.response["remote_settlement_unconfirmed"], True
                )
                self.assertEqual(
                    causal.response["evaluator_failure"]["category"],
                    failure_category,
                )
                self.assertIs(
                    safe_worker_response(causal.response)[
                        "remote_settlement_unconfirmed"
                    ],
                    True,
                )
                self.assertEqual(evaluator.remote_unsettled_jobs, 1)
                self.assertTrue(evaluator.remote_settlement_event.is_set())
                self.assertEqual(gated.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
                self.assertEqual(
                    gated.response["reason"], "remote_settlement_gate_latched"
                )
                self.assertNotIn(
                    "remote_settlement_unconfirmed", gated.response
                )
                self.assertEqual(attempts, 1)

    def test_explicit_pre_admission_overload_receipt_is_retried(self) -> None:
        attempts = 0
        headers = Message()
        headers["Retry-After"] = "0"

        class Response:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return (
                    b'{"job_id":"job-1","status":"succeeded",'
                    b'"formal_status":"PROVED","correct":true}'
                )

        def fake_urlopen(_request, *, timeout):  # type: ignore[no-untyped-def]
            nonlocal attempts
            self.assertGreater(timeout, 0)
            attempts += 1
            if attempts < 3:
                receipt = json.dumps(
                    {
                        "job_id": f"rejected-{attempts}",
                        "status": "rejected_overloaded",
                        "terminal_reason": "router_capacity",
                        "error_kind": "lean_router_overloaded",
                        "retryable": True,
                    }
                ).encode("utf-8")
                raise HTTPError(
                    "https://judge.invalid/private",
                    503,
                    "overloaded",
                    headers,
                    BytesIO(receipt),
                )
            return Response()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            candidate.write_text(
                "import Mathlib\ntheorem task : True := by trivial\n",
                encoding="utf-8",
            )
            evaluator = LeanEvaluator(
                "http://unused",
                lean_env_id="fixed-env",
                timeout_seconds=5,
                poll_interval_seconds=0.05,
                admission_retry_seconds=1.0,
            )
            with patch(
                "contextswarm_mini.evaluator.urlopen",
                side_effect=fake_urlopen,
            ), patch("contextswarm_mini.evaluator.time.sleep"):
                verdict = evaluator.evaluate(_task(root), candidate)

        self.assertEqual(verdict.status, "PROVED")
        self.assertEqual(verdict.score, 1.0)
        self.assertEqual(attempts, 3)
        self.assertEqual(evaluator.remote_unsettled_jobs, 0)
        self.assertFalse(evaluator.remote_settlement_event.is_set())

    def test_global_latch_blocks_an_overload_admission_retry(self) -> None:
        attempts = 0
        evaluator = LeanEvaluator(
            "http://unused",
            lean_env_id="fixed-env",
            poll_interval_seconds=0.05,
            admission_retry_seconds=1.0,
        )

        def fake_urlopen(_request, *, timeout):  # type: ignore[no-untyped-def]
            nonlocal attempts
            self.assertGreater(timeout, 0)
            attempts += 1
            # Simulate a different concurrent submission latching unknown
            # remote work while this call receives a safe overload rejection.
            evaluator._mark_remote_unsettled()  # noqa: SLF001
            receipt = json.dumps(
                {
                    "status": "rejected_overloaded",
                    "terminal_reason": "router_capacity",
                    "retryable": True,
                }
            ).encode("utf-8")
            raise HTTPError(
                "https://judge.invalid/private",
                503,
                "overloaded",
                Message(),
                BytesIO(receipt),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "result.lean"
            candidate.write_text(
                "import Mathlib\ntheorem task : True := by trivial\n",
                encoding="utf-8",
            )
            with patch(
                "contextswarm_mini.evaluator.urlopen",
                side_effect=fake_urlopen,
            ):
                verdict = evaluator.evaluate(_task(root), candidate)

        self.assertEqual(verdict.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
        self.assertEqual(verdict.response["reason"], "remote_settlement_gate_latched")
        self.assertNotIn("remote_settlement_unconfirmed", verdict.response)
        self.assertEqual(attempts, 1)

    def test_unstructured_get_503_remains_capacity_retriable(self) -> None:
        attempts = 0
        headers = Message()
        headers["Retry-After"] = "0"

        class Response:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"status":"running"}'

        def fake_urlopen(_request, *, timeout):  # type: ignore[no-untyped-def]
            nonlocal attempts
            self.assertGreater(timeout, 0)
            attempts += 1
            if attempts < 3:
                raise HTTPError(
                    "https://judge.invalid/private",
                    503,
                    "unavailable",
                    headers,
                    BytesIO(b"{not-json"),
                )
            return Response()

        evaluator = LeanEvaluator(
            "http://unused", lean_env_id="fixed-env", timeout_seconds=5
        )
        with patch(
            "contextswarm_mini.evaluator.urlopen", side_effect=fake_urlopen
        ), patch("contextswarm_mini.evaluator.time.sleep") as sleep:
            response = evaluator._request("GET", "/api/lean/jobs/job-1")

        self.assertEqual(response["status"], "running")
        self.assertEqual(attempts, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_retry_after_above_five_seconds_is_honored_when_horizon_allows(self) -> None:
        attempts = 0
        headers = Message()
        headers["Retry-After"] = "7.25"

        class Response:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return (
                    b'{"job_id":"job-1","status":"failed",'
                    b'"formal_status":"VERIFY_FAIL"}'
                )

        def fake_urlopen(_request, *, timeout):  # type: ignore[no-untyped-def]
            nonlocal attempts
            self.assertGreater(timeout, 0)
            attempts += 1
            if attempts == 1:
                raise HTTPError(
                    "https://judge.invalid/private",
                    429,
                    "busy",
                    headers,
                    BytesIO(
                        b'{"error":"admission_capacity_exceeded",'
                        b'"message":"HTTP ingress capacity is exhausted"}'
                    ),
                )
            return Response()

        evaluator = LeanEvaluator(
            "http://unused", lean_env_id="fixed-env", timeout_seconds=20
        )
        with patch(
            "contextswarm_mini.evaluator.urlopen", side_effect=fake_urlopen
        ), patch.object(
            evaluator, "_combined_cancel_event", return_value=None
        ), patch("contextswarm_mini.evaluator.time.sleep") as sleep:
            verdict = evaluator.probe_source(
                _task(Path(".")),
                "import Mathlib\ntheorem task : True := by trivial\n",
            )
        self.assertEqual(verdict.status, "VERIFY_FAIL")
        self.assertEqual(attempts, 2)
        sleep.assert_called_once_with(7.25)

    def test_get_retry_after_beyond_remaining_horizon_fails_without_sleeping(self) -> None:
        headers = Message()
        headers["Retry-After"] = "20"

        def overloaded(_request, *, timeout):  # type: ignore[no-untyped-def]
            self.assertGreater(timeout, 0)
            raise HTTPError(
                "https://judge.invalid/private",
                503,
                "busy",
                headers,
                None,
            )

        evaluator = LeanEvaluator(
            "http://unused", lean_env_id="fixed-env", timeout_seconds=5
        )
        with patch(
            "contextswarm_mini.evaluator.urlopen", side_effect=overloaded
        ), patch("contextswarm_mini.evaluator.time.sleep") as sleep:
            with self.assertRaises(EvaluatorError) as raised:
                evaluator._request(
                    "GET", "/api/lean/jobs/job-1", timeout_seconds=0.1
                )
        self.assertEqual(raised.exception.category, "judge_overloaded_deadline")
        self.assertEqual(raised.exception.http_status, 503)
        self.assertEqual(raised.exception.retry_after_seconds, 20.0)
        sleep.assert_not_called()

    def test_submission_timeout_shrinks_after_retry_after_consumes_horizon(self) -> None:
        clock = [0.0]
        submitted_timeouts: list[int] = []
        headers = Message()
        headers["Retry-After"] = "7"

        class Response:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return (
                    b'{"job_id":"job-1","status":"failed",'
                    b'"formal_status":"VERIFY_FAIL"}'
                )

        def fake_urlopen(request, *, timeout):  # type: ignore[no-untyped-def]
            del timeout
            submitted_timeouts.append(json.loads(request.data)["timeout"])
            if len(submitted_timeouts) == 1:
                raise HTTPError(
                    "https://judge.invalid/private",
                    429,
                    "busy",
                    headers,
                    BytesIO(
                        b'{"error":"admission_capacity_exceeded",'
                        b'"message":"HTTP ingress capacity is exhausted"}'
                    ),
                )
            return Response()

        evaluator = LeanEvaluator(
            "http://unused", lean_env_id="fixed-env", timeout_seconds=10
        )
        with patch(
            "contextswarm_mini.evaluator.urlopen", side_effect=fake_urlopen
        ), patch.object(
            evaluator, "_combined_cancel_event", return_value=None
        ), patch(
            "contextswarm_mini.evaluator.time.monotonic",
            side_effect=lambda: clock[0],
        ), patch(
            "contextswarm_mini.evaluator.time.sleep",
            side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ):
            verdict = evaluator.probe_source(
                _task(Path(".")),
                "import Mathlib\ntheorem task : True := by trivial\n",
                deadline_monotonic=8,
            )
        self.assertEqual(verdict.status, "VERIFY_FAIL")
        self.assertEqual(submitted_timeouts, [8, 1])


if __name__ == "__main__":
    unittest.main()
