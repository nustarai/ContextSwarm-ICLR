from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

from contextswarm_mini.evaluator import (
    FORMAL_VERDICT_SCHEMA_VERSION,
    LeanEvaluator,
    _safe_probe_response,
    _safe_response,
    _settled_outcome,
    _terminal,
)
from contextswarm_mini.models import Task


def _proved_receipt(candidate_sha256: str, *, job_id: str = "job-1", **extra: object) -> dict[str, object]:
    return {
        "job_id": job_id,
        "status": "succeeded",
        "formal_status": "PROVED",
        "formal_verdict_schema_version": FORMAL_VERDICT_SCHEMA_VERSION,
        "is_valid_no_sorry": True,
        "canonical_verdict": {
            "schema_version": FORMAL_VERDICT_SCHEMA_VERSION,
            "status": "PROVED",
            "score": 1.0,
            "correct": True,
            "cheating": False,
            "source_contract_status": "ok",
            "signature_check_status": "ok",
            "solution_hash": candidate_sha256,
        },
        **extra,
    }


class _LifecycleServer:
    def __init__(self, mode: str):
        self.mode = mode
        self.submitted_at = 0.0
        self.deletes = 0
        self.post_count = 0
        self.post_payloads: list[dict[str, object]] = []
        self.candidate_sha256 = ""
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def _send(
                self,
                payload: dict[str, object],
                status_code: int = 200,
            ) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                request_payload = json.loads(self.rfile.read(length) or b"{}")
                if isinstance(request_payload, dict):
                    owner.post_payloads.append(request_payload)
                    owner.candidate_sha256 = hashlib.sha256(
                        str(request_payload.get("code") or "").encode("utf-8")
                    ).hexdigest()
                owner.post_count += 1
                owner.submitted_at = time.monotonic()
                if owner.mode == "unconfirmed_503":
                    self._send(
                        {"ok": False, "error": "upstream unavailable"},
                        status_code=503,
                    )
                elif owner.mode == "unconfirmed_429":
                    self._send(
                        {"error": "account_quota_exhausted"},
                        status_code=429,
                    )
                elif owner.mode == "always_http_429" or (
                    owner.mode == "http_429_then_proved" and owner.post_count < 3
                ):
                    self._send(
                        {
                            "error": "admission_capacity_exceeded",
                            "message": "HTTP ingress capacity is exhausted",
                            "retry_after_ms": 250,
                        },
                        status_code=429,
                    )
                elif owner.mode == "always_overloaded" or (
                    owner.mode == "overloaded_then_proved" and owner.post_count < 3
                ):
                    self._send(
                        {"ok": False, "error": "queue is full"},
                        status_code=503,
                    )
                elif owner.mode == "always_terminal_overloaded" or (
                    owner.mode == "terminal_overloaded_then_proved"
                    and owner.post_count < 3
                ):
                    self._send(
                        {
                            "job_id": f"rejected-{owner.post_count}",
                            "status": "rejected_overloaded",
                            "terminal_reason": "router_capacity",
                            "error_kind": "lean_router_overloaded",
                            "retryable": True,
                        }
                    )
                elif owner.mode in {
                    "http_429_then_proved",
                    "overloaded_then_proved",
                    "terminal_overloaded_then_proved",
                }:
                    self._send(_proved_receipt(owner.candidate_sha256))
                elif (
                    owner.mode == "queued_rejected_then_proved"
                    and owner.post_count >= 2
                ):
                    self._send(
                        _proved_receipt(owner.candidate_sha256, job_id="job-2")
                    )
                elif owner.mode in {
                    "queued_rejected_then_proved",
                    "queued_always_rejected",
                }:
                    self._send(
                        {
                            "job_id": f"job-{owner.post_count}",
                            "status": "queued",
                            "submitted_at_ms": 1_000,
                            "lifecycle_deadline_ms": 2_000,
                        }
                    )
                elif owner.mode == "memory":
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "failed",
                            "formal_status": "NETWORK_ERROR",
                            "error_kind": "memory_limit_exceeded",
                            "terminal_reason": "verification_failed",
                            "retryable": False,
                            "execution_ms": 125,
                            "queue_wait_ms": 4,
                        }
                    )
                elif owner.mode == "stale_timeout_proved":
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "timed_out",
                            "formal_status": "PROVED",
                            "error_kind": "timeout",
                            "terminal_reason": "execution_timeout",
                        }
                    )
                elif owner.mode == "stale_resource_proved":
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "failed",
                            "formal_status": "PROVED",
                            "error_kind": "memory_limit_exceeded",
                        }
                    )
                elif owner.mode == "nested_stale_timeout_proved":
                    self._send(
                        {
                            "job_id": "job-1",
                            "response": {
                                "status": "timed_out",
                                "formal_status": "PROVED",
                                "error_kind": "timeout",
                            },
                        }
                    )
                elif owner.mode == "ambiguous_succeeded":
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "succeeded",
                            "finished_at_ms": 2_000,
                        }
                    )
                elif owner.mode == "huge_lifecycle_deadline":
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "queued",
                            "submitted_at_ms": 1_000,
                            "lifecycle_deadline_ms": 1e300,
                        }
                    )
                elif owner.mode == "contradictory_terminal":
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "completed",
                            "formal_status": "RUNNING",
                            "finished_at_ms": 2_000,
                        }
                    )
                elif owner.mode == "missing_job_id":
                    self._send({"status": "queued"})
                elif owner.mode == "queued_then_proved":
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "queued",
                            "submitted_at_ms": 1_000,
                            "queue_deadline_ms": 2_000,
                        }
                    )
                else:
                    self._send({"job_id": "job-1", "status": "queued"})

            def do_GET(self) -> None:  # noqa: N802
                elapsed = time.monotonic() - owner.submitted_at
                if owner.mode == "cancel_disappears" and owner.deletes:
                    self._send(
                        {"error": "job_not_found", "message": "job was evicted"},
                        status_code=404,
                    )
                elif owner.mode in {
                    "queued_rejected_then_proved",
                    "queued_always_rejected",
                }:
                    self._send(
                        {
                            "job_id": f"job-{owner.post_count}",
                            "status": "rejected_overloaded",
                            "terminal_reason": "queue_wait_timeout",
                            "error_kind": "overloaded",
                            "retryable": True,
                        }
                    )
                elif owner.mode == "queued_then_proved" and elapsed >= 1.2:
                    self._send(
                        _proved_receipt(
                            owner.candidate_sha256,
                            submitted_at_ms=1_000,
                            queue_deadline_ms=2_000,
                            started_at_ms=1_600,
                            finished_at_ms=2_200,
                            queue_wait_ms=600,
                            execution_ms=600,
                        )
                    )
                elif owner.mode == "queued_then_proved" and elapsed >= 0.6:
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "running",
                            "submitted_at_ms": 1_000,
                            "queue_deadline_ms": 2_000,
                            "started_at_ms": 1_600,
                        }
                    )
                elif owner.mode == "queued_then_proved":
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "queued",
                            "submitted_at_ms": 1_000,
                            "queue_deadline_ms": 2_000,
                        }
                    )
                elif owner.mode == "late_timeout" and elapsed >= 1.05:
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "timed_out",
                            "formal_status": "NETWORK_ERROR",
                            "error_kind": "timeout",
                            "terminal_reason": "execution_timeout",
                            "retryable": False,
                            "execution_ms": 1_050,
                            "queue_wait_ms": 2,
                        }
                    )
                else:
                    self._send({"job_id": "job-1", "status": "running"})

            def do_DELETE(self) -> None:  # noqa: N802
                owner.deletes += 1
                if owner.mode == "cancel_terminal":
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "cancelled",
                            "formal_status": "PROVED",
                            "terminal_reason": "cancelled",
                            "cancel_requested": True,
                        }
                    )
                else:
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "running",
                            "cancel_requested": True,
                        }
                    )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @contextmanager
    def running(self):
        self.thread.start()
        try:
            yield f"http://127.0.0.1:{self.server.server_port}"
        finally:
            self.server.shutdown()
            self.thread.join(timeout=2)
            self.server.server_close()


def _task(root: Path) -> Task:
    baseline = "import Mathlib\ntheorem task : True := by\n  sorry\n"
    return Task(
        slug="task",
        root=root,
        problem_text="",
        baseline_code=baseline,
        metadata={"problem_id": "Task", "theorem_name": "task"},
    )


class EvaluatorLifecycleTests(unittest.TestCase):
    def _candidate(self, root: Path) -> Path:
        candidate = root / "result.lean"
        candidate.write_text("import Mathlib\ntheorem task : True := by\n  trivial\n", encoding="utf-8")
        return candidate

    def test_backend_timeout_settles_after_execution_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("late_timeout")
            with server.running() as url:
                verdict = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    timeout_seconds=1,
                    poll_interval_seconds=0.01,
                    settlement_grace_seconds=0.25,
                    cancel_grace_seconds=0.1,
                ).evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "EXECUTION_TIMEOUT")
        self.assertEqual(verdict.response["error_kind"], "timeout")
        self.assertEqual(verdict.response["execution_ms"], 1_050)
        self.assertEqual(server.deletes, 0)

    def test_memory_limit_is_not_flattened_to_network_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("memory")
            with server.running() as url:
                verdict = LeanEvaluator(url, lean_env_id="test").evaluate(
                    _task(root), self._candidate(root)
                )

        self.assertEqual(verdict.status, "RESOURCE_LIMIT")
        self.assertEqual(verdict.response["error_kind"], "memory_limit_exceeded")
        self.assertEqual(verdict.response["queue_wait_ms"], 4)

    def test_horizon_abandonment_cancels_job_and_returns_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("cancel_terminal")
            with server.running() as url:
                verdict = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    timeout_seconds=5,
                    poll_interval_seconds=0.01,
                    settlement_grace_seconds=0.1,
                    cancel_grace_seconds=0.1,
                ).evaluate(
                    _task(root),
                    self._candidate(root),
                    deadline_monotonic=time.monotonic() + 1.01,
                )

        self.assertEqual(verdict.status, "OUT_OF_HORIZON")
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual(server.deletes, 1)
        self.assertTrue(verdict.response["cancel_requested"])

    def test_cancelled_job_disappearing_is_reconciled_as_settled(self) -> None:
        lifecycle: list[tuple[str, dict[str, object]]] = []
        server = _LifecycleServer("cancel_disappears")
        with server.running() as url:
            response, error = LeanEvaluator(
                url,
                lean_env_id="test",
                poll_interval_seconds=0.01,
                cancel_grace_seconds=0.1,
                lifecycle_observer=lambda event, payload: lifecycle.append(
                    (event, dict(payload))
                ),
            ).cancel_job("job-1")

        self.assertIsNone(error)
        self.assertTrue(_terminal(response))
        self.assertEqual(response["terminal_reason"], "job_not_found")
        self.assertEqual(server.deletes, 1)
        self.assertEqual(
            [event for event, _payload in lifecycle],
            ["cancel_requested", "settled"],
        )

    def test_raw_failure_never_scores_stale_proved_marker(self) -> None:
        expected = {
            "stale_timeout_proved": "EXECUTION_TIMEOUT",
            "stale_resource_proved": "RESOURCE_LIMIT",
            "nested_stale_timeout_proved": "EXECUTION_TIMEOUT",
        }
        for mode, expected_status in expected.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                server = _LifecycleServer(mode)
                with server.running() as url:
                    verdict = LeanEvaluator(url, lean_env_id="test").evaluate(
                        _task(root), self._candidate(root)
                    )

                self.assertEqual(verdict.status, expected_status)
                self.assertEqual(verdict.score, 0.0)

    def test_specific_lifecycle_failure_precedes_negative_canonical_verdict(self) -> None:
        canonical = {
            "schema_version": FORMAL_VERDICT_SCHEMA_VERSION,
            "status": "VERIFY_FAIL",
            "score": 0.0,
            "correct": False,
            "cheating": False,
        }
        for payload, expected in (
            (
                {
                    "status": "timed_out",
                    "error_kind": "timeout",
                    "canonical_verdict": canonical,
                },
                "EXECUTION_TIMEOUT",
            ),
            (
                {
                    "status": "failed",
                    "error_kind": "memory_limit_exceeded",
                    "canonical_verdict": canonical,
                },
                "RESOURCE_LIMIT",
            ),
        ):
            with self.subTest(expected=expected):
                status, proved, error = _settled_outcome(payload)
                self.assertEqual(status, expected)
                self.assertFalse(proved)
                self.assertIsNone(error)

    def test_succeeded_envelope_without_verdict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("ambiguous_succeeded")
            with server.running() as url:
                verdict = LeanEvaluator(url, lean_env_id="test").evaluate(
                    _task(root), self._candidate(root)
                )

        self.assertEqual(verdict.status, "EVALUATOR_ERROR")
        self.assertEqual(verdict.score, 0.0)
        self.assertIn("lacks an authoritative verdict", verdict.error or "")

    def test_nonterminal_admission_without_job_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("missing_job_id")
            with server.running() as url:
                verdict = LeanEvaluator(url, lean_env_id="test").evaluate(
                    _task(root), self._candidate(root)
                )

        self.assertEqual(verdict.status, "EVALUATOR_ERROR")
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual(verdict.response["last_observed_lifecycle"], "queued")
        self.assertIn("without a job id", verdict.error or "")
        self.assertEqual(server.post_count, 1)
        self.assertEqual(server.deletes, 0)

    def test_definitive_admission_overload_is_retried(self) -> None:
        for mode in (
            "http_429_then_proved",
            "overloaded_then_proved",
            "terminal_overloaded_then_proved",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                server = _LifecycleServer(mode)
                with server.running() as url:
                    verdict = LeanEvaluator(
                        url,
                        lean_env_id="test",
                        poll_interval_seconds=0.05,
                        admission_retry_seconds=1.0,
                    ).evaluate(_task(root), self._candidate(root))

                self.assertEqual(verdict.status, "PROVED")
                self.assertEqual(verdict.score, 1.0)
                self.assertEqual(server.post_count, 3)

    def test_admission_overload_retry_is_bounded(self) -> None:
        for mode in (
            "always_http_429",
            "always_overloaded",
            "always_terminal_overloaded",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                server = _LifecycleServer(mode)
                with server.running() as url:
                    verdict = LeanEvaluator(
                        url,
                        lean_env_id="test",
                        poll_interval_seconds=0.05,
                        admission_retry_seconds=0.12,
                    ).evaluate(_task(root), self._candidate(root))

                self.assertEqual(verdict.status, "REJECTED_OVERLOADED")
                self.assertEqual(verdict.score, 0.0)
                self.assertGreaterEqual(server.post_count, 2)
                self.assertGreaterEqual(verdict.response["admission_attempts"], 2)

    def test_polled_terminal_overload_resubmits_whole_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("queued_rejected_then_proved")
            with server.running() as url:
                verdict = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    poll_interval_seconds=0.05,
                    terminal_overload_retries=1,
                ).evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "PROVED")
        self.assertEqual(verdict.score, 1.0)
        self.assertEqual(server.post_count, 2)
        self.assertEqual(verdict.response["evaluator_overload_resubmissions"], 1)

    def test_polled_terminal_overload_resubmit_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("queued_always_rejected")
            with server.running() as url:
                verdict = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    poll_interval_seconds=0.05,
                    terminal_overload_retries=1,
                ).evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "REJECTED_OVERLOADED")
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual(server.post_count, 2)
        self.assertEqual(verdict.response["evaluator_overload_resubmissions"], 1)

    def test_unconfirmed_http_error_is_not_resubmitted(self) -> None:
        for mode in ("unconfirmed_429", "unconfirmed_503"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                server = _LifecycleServer(mode)
                with server.running() as url:
                    verdict = LeanEvaluator(
                        url,
                        lean_env_id="test",
                        admission_retry_seconds=1.0,
                    ).evaluate(_task(root), self._candidate(root))

                self.assertEqual(verdict.status, "EVALUATOR_ERROR")
                self.assertEqual(verdict.score, 0.0)
                self.assertEqual(server.post_count, 1)

    def test_probe_distinguishes_confirmed_admission_overload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("always_http_429")
            with server.running() as url:
                result = LeanEvaluator(url, lean_env_id="test").probe(
                    _task(root),
                    "import Mathlib\n#check Nat.succ\n",
                )

        self.assertEqual(result["status"], "probe_admission_closed")
        self.assertEqual(result["error_kind"], "judge_admission_overloaded")
        self.assertEqual(server.post_count, 1)

    def test_huge_finite_lifecycle_deadline_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("huge_lifecycle_deadline")
            with server.running() as url:
                verdict = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    cancel_grace_seconds=0.1,
                    max_lifecycle_seconds=60.0,
                ).evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "EVALUATOR_ERROR")
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual(server.deletes, 1)
        self.assertIn("client safety cap", verdict.error or "")

    def test_queue_time_does_not_consume_backend_execution_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("queued_then_proved")
            with server.running() as url:
                verdict = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    timeout_seconds=1,
                    poll_interval_seconds=0.01,
                    settlement_grace_seconds=0.1,
                    cancel_grace_seconds=0.1,
                ).evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "PROVED")
        self.assertEqual(server.deletes, 0)
        self.assertEqual(verdict.response["queue_wait_ms"], 600)
        self.assertEqual(verdict.response["execution_ms"], 600)
        self.assertEqual(server.post_payloads[0]["max_retries"], 1)

    def test_unsettled_cancel_never_persists_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("never_terminal")
            with server.running() as url:
                verdict = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    timeout_seconds=1,
                    poll_interval_seconds=0.01,
                    settlement_grace_seconds=0.1,
                    cancel_grace_seconds=0.1,
                ).evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "EVALUATOR_TIMEOUT")
        self.assertEqual(server.deletes, 1)
        self.assertEqual(verdict.response["reason"], "judge_settlement_timeout")
        self.assertNotIn("status", verdict.response)
        self.assertEqual(verdict.response["last_observed_lifecycle"], "running")

    def test_contradictory_terminal_envelope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("contradictory_terminal")
            with server.running() as url:
                verdict = LeanEvaluator(url, lean_env_id="test").evaluate(
                    _task(root), self._candidate(root)
                )

        self.assertEqual(verdict.status, "EVALUATOR_ERROR")
        self.assertNotIn("formal_status", verdict.response)
        self.assertEqual(verdict.response["last_observed_lifecycle"], "running")

    def test_raw_lifecycle_status_has_precedence_over_stale_formal_status(self) -> None:
        self.assertFalse(
            _terminal({"status": "running", "formal_status": "NETWORK_ERROR"})
        )
        self.assertTrue(_terminal({"status": "failed", "formal_status": "RUNNING"}))
        self.assertFalse(
            _terminal(
                {"response": {"status": "running", "formal_status": "PROVED"}}
            )
        )
        self.assertTrue(
            _terminal(
                {"response": {"status": "timed_out", "formal_status": "PROVED"}}
            )
        )

    def test_terminal_compatibility_and_safe_metrics(self) -> None:
        payload = {
            "status": "timed_out",
            "formal_status": "NETWORK_ERROR",
            "error_kind": "timeout",
            "terminal_reason": "execution_timeout",
            "retryable": False,
            "queue_wait_ms": 7,
            "execution_ms": 300_001,
            "diagnostics": {"private": "not retained"},
        }
        self.assertTrue(_terminal(payload))
        self.assertEqual(
            _safe_response(payload),
            {
                "status": "timed_out",
                "formal_status": "NETWORK_ERROR",
                "error_kind": "timeout",
                "terminal_reason": "execution_timeout",
                "retryable": False,
                "queue_wait_ms": 7,
                "execution_ms": 300_001,
            },
        )

    def test_probe_profile_mapping_preserves_only_bounded_diagnostics(self) -> None:
        result = _safe_probe_response(
            {
                "status": "succeeded",
                "is_valid_with_sorry": False,
                "probe_diagnostics": {
                    "items": [
                        {
                            "severity": "error",
                            "data": "unknown identifier",
                            "line": 3,
                            "column": 7,
                            "path": "/private/Main.lean",
                            "source": "secret source",
                        }
                    ],
                    "truncated": True,
                },
            }
        )
        self.assertEqual(result["status"], "elab_failed")
        self.assertTrue(result["diagnostics_truncated"])
        self.assertEqual(
            result["diagnostics"],
            [
                {
                    "severity": "error",
                    "message": "unknown identifier",
                    "line": 3,
                    "column": 7,
                }
            ],
        )

    def test_only_exact_canonical_proved_can_score(self) -> None:
        expected_hash = "a" * 64
        canonical = {
            "schema_version": FORMAL_VERDICT_SCHEMA_VERSION,
            "status": "PROVED",
            "score": 1.0,
            "correct": True,
            "cheating": False,
            "source_contract_status": "ok",
            "signature_check_status": "ok",
            "safeverify_status": "accepted",
            "solution_hash": expected_hash,
        }
        valid = {
            "status": "succeeded",
            "formal_verdict_schema_version": FORMAL_VERDICT_SCHEMA_VERSION,
            "is_valid_no_sorry": True,
            "canonical_verdict": canonical,
        }
        self.assertEqual(
            _settled_outcome(valid, expected_candidate_sha256=expected_hash),
            ("PROVED", True, None),
        )

        mutations = {
            "wrong_schema": {"schema_version": "legacy"},
            "partial_score": {"score": 0.999},
            "incorrect": {"correct": False},
            "cheating": {"cheating": True},
            "source_drift": {"source_contract_status": "failed"},
            "signature_drift": {"signature_check_status": "skipped"},
            "safeverify_rejected": {"safeverify_status": "rejected"},
            "candidate_mismatch": {"solution_hash": "b" * 64},
        }
        for name, update in mutations.items():
            with self.subTest(name=name):
                payload = {
                    **valid,
                    "canonical_verdict": {**canonical, **update},
                }
                status, proved, error = _settled_outcome(
                    payload,
                    expected_candidate_sha256=expected_hash,
                )
                self.assertEqual(status, "EVALUATOR_ERROR")
                self.assertFalse(proved)
                self.assertTrue(error)

        missing_no_sorry = {**valid, "is_valid_no_sorry": False}
        self.assertEqual(
            _settled_outcome(
                missing_no_sorry,
                expected_candidate_sha256=expected_hash,
            )[0],
            "EVALUATOR_ERROR",
        )

    def test_legacy_positive_markers_are_diagnostic_only(self) -> None:
        for payload in (
            {"status": "PROVED"},
            {"status": "AC", "score": 1},
            {"status": "succeeded", "correct": True},
            {"status": "succeeded", "is_valid_no_sorry": True},
        ):
            with self.subTest(payload=payload):
                status, proved, error = _settled_outcome(payload)
                self.assertEqual(status, "EVALUATOR_ERROR")
                self.assertFalse(proved)
                self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
