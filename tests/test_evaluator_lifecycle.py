from __future__ import annotations

from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from contextswarm_mini.evaluator import (
    CodingEvaluator,
    EvaluatorError,
    LeanEvaluator,
    _safe_response,
    _terminal,
    safe_worker_response,
)
from contextswarm_mini.models import Task


class _LifecycleServer:
    def __init__(self, mode: str):
        self.mode = mode
        self.submitted_at = 0.0
        self.deletes = 0
        self.get_seen = threading.Event()
        self.post_count = 0
        self.post_payloads: list[dict[str, object]] = []
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
                    self._send(
                        {
                            "job_id": "job-1",
                            "status": "succeeded",
                            "formal_status": "PROVED",
                            "correct": True,
                        }
                    )
                elif owner.mode == "cache_reused":
                    self._send(
                        {
                            "job_id": "job-cache-hit",
                            "status": "succeeded",
                            "formal_status": "PROVED",
                            "correct": True,
                            "cache_reused": True,
                        }
                    )
                elif (
                    owner.mode == "queued_rejected_then_proved"
                    and owner.post_count >= 2
                ):
                    self._send(
                        {
                            "job_id": "job-2",
                            "status": "succeeded",
                            "formal_status": "PROVED",
                            "correct": True,
                        }
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
                owner.get_seen.set()
                elapsed = time.monotonic() - owner.submitted_at
                if owner.mode in {
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
                        {
                            "job_id": "job-1",
                            "status": "succeeded",
                            "formal_status": "PROVED",
                            "correct": True,
                            "submitted_at_ms": 1_000,
                            "queue_deadline_ms": 2_000,
                            "started_at_ms": 1_600,
                            "finished_at_ms": 2_200,
                            "queue_wait_ms": 600,
                            "execution_ms": 600,
                        }
                    )
                elif owner.mode == "terminal_without_receipt_job_id":
                    self._send(
                        {
                            "status": "succeeded",
                            "formal_status": "PROVED",
                            "correct": True,
                        }
                    )
                elif owner.mode == "terminal_with_wrong_receipt_job_id":
                    self._send(
                        {
                            "job_id": "job-other",
                            "status": "succeeded",
                            "formal_status": "PROVED",
                            "correct": True,
                        }
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

    def test_remote_cache_reuse_receipt_propagates_to_verdict(self) -> None:
        for method in ("evaluate", "probe"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                server = _LifecycleServer("cache_reused")
                with server.running() as url:
                    evaluator = LeanEvaluator(url, lean_env_id="test")
                    verdict = getattr(evaluator, method)(
                        _task(root), self._candidate(root)
                    )

                self.assertEqual(verdict.status, "PROVED")
                self.assertTrue(verdict.response["cache_reused"])
                self.assertTrue(verdict.cache_reused)

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

    def test_horizon_cancel_defers_known_job_until_router_receipt(self) -> None:
        """A deadline-driven DELETE must not latch the whole evaluator."""

        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.005,
            settlement_grace_seconds=0.02,
            cancel_grace_seconds=0.01,
        )
        evaluator.deferred_settlement_timeout_seconds = 1.0
        foreground_polls = 0
        delete_seen = False
        watcher_polls = 0

        def request(
            method: str,
            _path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            nonlocal delete_seen, foreground_polls, watcher_polls
            if method == "POST":
                return {"job_id": "horizon-job", "status": "queued"}
            if method == "DELETE":
                delete_seen = True
                return {
                    "job_id": "horizon-job",
                    "status": "cancel_requested",
                    "cancel_requested": True,
                    "retryable": True,
                }
            # The short foreground reconciliation window sees only a
            # nonterminal receipt.  The background watcher then observes the
            # terminal receipt a few polls later.
            if delete_seen and threading.current_thread().name.startswith(
                "judge-settlement-"
            ):
                watcher_polls += 1
                if watcher_polls >= 3:
                    return {"job_id": "horizon-job", "status": "cancelled"}
            if not evaluator.pending_settlement_watchers:
                foreground_polls += 1
            return {"job_id": "horizon-job", "status": "running"}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(evaluator, "_request", side_effect=request):
                verdict = evaluator.evaluate(
                    _task(root),
                    self._candidate(root),
                    deadline_monotonic=time.monotonic() + 1.05,
                )
                self.assertEqual(verdict.status, "TASK_CANCELLED")
                self.assertTrue(verdict.response["judge_cancellation"]["deferred"])
                self.assertGreaterEqual(foreground_polls, 1)
                deadline = time.monotonic() + 1.0
                while (
                    evaluator.pending_settlement_watchers
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.005)

        self.assertEqual(evaluator.pending_settlement_watchers, 0)
        self.assertEqual(evaluator.remote_unsettled_jobs, 0)

    def test_cancel_reconciles_returned_status_capability_before_success(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.2,
        )
        calls: list[tuple[str, str, float]] = []

        def request(
            method: str,
            path: str,
            *_args: object,
            timeout_seconds: float,
            **_kwargs: object,
        ) -> dict[str, object]:
            calls.append((method, path, timeout_seconds))
            if method == "DELETE":
                return {
                    "job_id": "job-1",
                    "status": "cancel_requested",
                    "cancel_requested": True,
                    "status_endpoint": "/settlement/capability?opaque=receipt",
                }
            return {
                "job_id": "job-1",
                "status": "cancelled",
                "terminal_reason": "cancelled",
            }

        with patch.object(evaluator, "_request", side_effect=request):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={"job_id": "job-1", "status": "running"},
                cancel_endpoint="/cancel/capability?opaque=cancel",
            )

        self.assertEqual(calls[0][0:2], ("DELETE", "/cancel/capability?opaque=cancel"))
        self.assertEqual(calls[1][0], "GET")
        self.assertTrue(
            calls[1][1].startswith(
                "/settlement/capability?opaque=receipt&wait_ms="
            )
        )
        self.assertEqual(
            summary,
            {
                "attempted": True,
                "succeeded": True,
                "settled": True,
                "unconfirmed": False,
                "failure_category": None,
            },
        )
        self.assertEqual(evaluator.remote_unsettled_jobs, 0)

    def test_cancel_2xx_is_unconfirmed_without_job_bound_terminal_receipt(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.12,
        )
        calls: list[tuple[str, str, float]] = []

        def request(
            method: str,
            path: str,
            *_args: object,
            timeout_seconds: float,
            **_kwargs: object,
        ) -> dict[str, object]:
            calls.append((method, path, timeout_seconds))
            if method == "DELETE":
                time.sleep(0.07)
                return {
                    "job_id": "job-1",
                    "status": "cancel_requested",
                    "cancel_requested": True,
                    "status_endpoint": "/settlement/job-1",
                }
            # A terminal receipt for a different job is not authoritative.
            return {"job_id": "job-other", "status": "cancelled"}

        started = time.monotonic()
        with patch.object(evaluator, "_request", side_effect=request):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={"job_id": "job-1", "status": "running"},
            )
        elapsed = time.monotonic() - started

        self.assertTrue(any(method == "GET" for method, _path, _timeout in calls))
        delete_timeout = calls[0][2]
        first_get_timeout = next(
            timeout for method, _path, timeout in calls if method == "GET"
        )
        self.assertLess(first_get_timeout, delete_timeout)
        self.assertLess(elapsed, 0.25)
        self.assertEqual(
            summary,
            {
                "attempted": True,
                "succeeded": False,
                "settled": False,
                "unconfirmed": True,
                "failure_category": "cancel_settlement_unconfirmed",
            },
        )
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)
        self.assertEqual(
            safe_worker_response({"judge_cancellation": summary})[
                "judge_cancellation"
            ],
            summary,
        )

    def test_cancel_event_waits_for_bounded_remote_settlement_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("never_terminal")
            cancelled = threading.Event()
            verdicts = []
            with server.running() as url:
                evaluator = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    timeout_seconds=5,
                    poll_interval_seconds=0.01,
                    cancel_grace_seconds=0.12,
                )
                worker = threading.Thread(
                    target=lambda: verdicts.append(
                        evaluator.probe_source(
                            _task(root),
                            self._candidate(root).read_text(encoding="utf-8"),
                            deadline_monotonic=time.monotonic() + 2,
                            cancel_event=cancelled,
                        )
                    )
                )
                worker.start()
                self.assertTrue(server.get_seen.wait(timeout=1))
                cancel_started = time.monotonic()
                cancelled.set()
                worker.join(timeout=1)
                cancel_elapsed = time.monotonic() - cancel_started

        self.assertFalse(worker.is_alive())
        self.assertEqual(server.deletes, 1)
        self.assertGreaterEqual(cancel_elapsed, 0.08)
        self.assertLess(cancel_elapsed, 0.4)
        self.assertEqual(verdicts[0].status, "TASK_CANCELLED")
        self.assertEqual(
            verdicts[0].response["judge_cancellation"],
            {
                "attempted": True,
                "succeeded": False,
                "settled": False,
                "unconfirmed": True,
                "failure_category": "cancel_settlement_unconfirmed",
            },
        )

    def test_peer_cancel_defers_known_job_without_global_latch(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.03,
        )
        evaluator.deferred_settlement_timeout_seconds = 1.0
        terminal = threading.Event()
        released = threading.Event()
        calls = 0

        def request(
            method: str,
            _path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            nonlocal calls
            if method == "DELETE":
                return {"job_id": "job-1", "status": "cancel_requested"}
            calls += 1
            if calls >= 4:
                terminal.set()
                return {"job_id": "job-1", "status": "cancelled"}
            return {"job_id": "job-1", "status": "running"}

        with patch.object(evaluator, "_request", side_effect=request):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={"job_id": "job-1", "status": "running"},
                cancellation_reason="task_solved_by_peer",
                on_settled=released.set,
            )
            self.assertTrue(summary["deferred"])
            self.assertFalse(summary["unconfirmed"])
            self.assertEqual(evaluator.remote_unsettled_jobs, 0)
            self.assertEqual(evaluator.pending_settlement_watchers, 1)
            self.assertTrue(terminal.wait(timeout=1))
            self.assertTrue(released.wait(timeout=1))

        self.assertEqual(evaluator.pending_settlement_watchers, 0)
        self.assertEqual(evaluator.remote_unsettled_jobs, 0)

    def test_retryable_router_cancel_defers_without_peer_reason(self) -> None:
        """A query/agent timeout must not poison the whole arm while DELETE settles."""

        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.005,
            cancel_grace_seconds=0.01,
        )
        evaluator.deferred_settlement_timeout_seconds = 1.0
        calls = 0
        released = threading.Event()

        def request(
            method: str,
            _path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            nonlocal calls
            if method == "DELETE":
                return {
                    "job_id": "job-1",
                    "status": "running",
                    "cancel_requested": True,
                    "router_cancel_disposition": "cancel_requested",
                    "retryable": True,
                    "status_endpoint": "/api/lean/jobs/job-1",
                }
            calls += 1
            if calls >= 3:
                return {
                    "job_id": "job-1",
                    "status": "cancelled",
                    "formal_status": "NETWORK_ERROR",
                    "terminal_reason": "cancelled",
                    "retryable": True,
                }
            return {
                "job_id": "job-1",
                "status": "running",
                "cancel_requested": True,
                "router_cancel_disposition": "cancel_requested",
                "retryable": True,
                "status_endpoint": "/api/lean/jobs/job-1",
            }

        with patch.object(evaluator, "_request", side_effect=request):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={
                    "job_id": "job-1",
                    "status": "running",
                    "status_endpoint": "/api/lean/jobs/job-1",
                },
                on_settled=released.set,
            )
            self.assertTrue(summary.get("deferred"))
            self.assertFalse(summary["unconfirmed"])
            self.assertEqual(evaluator.remote_unsettled_jobs, 0)
            self.assertTrue(released.wait(timeout=1))

        self.assertEqual(evaluator.pending_settlement_watchers, 0)
        self.assertEqual(evaluator.remote_unsettled_jobs, 0)

    def test_terminal_cancelled_network_error_is_bound_and_nonfatal(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.005,
            cancel_grace_seconds=0.02,
        )

        def request(
            method: str,
            _path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            if method == "DELETE":
                return {
                    "job_id": "job-1",
                    "status": "cancelled",
                    "formal_status": "NETWORK_ERROR",
                    "error_kind": "cancelled",
                    "terminal_reason": "cancelled",
                    "retryable": True,
                }
            return {"job_id": "job-1", "status": "cancelled"}

        with patch.object(evaluator, "_request", side_effect=request):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={"job_id": "job-1", "status": "running"},
            )

        self.assertEqual(
            summary,
            {
                "attempted": True,
                "succeeded": True,
                "settled": True,
                "unconfirmed": False,
                "failure_category": None,
            },
        )
        self.assertEqual(evaluator.remote_unsettled_jobs, 0)

    def test_peer_cancel_watcher_timeout_latches_and_retains_callback(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.02,
        )
        evaluator.deferred_settlement_timeout_seconds = 0.05
        released = threading.Event()

        def request(
            method: str,
            _path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            return {
                "job_id": "job-1",
                "status": "cancel_requested" if method == "DELETE" else "running",
            }

        with patch.object(evaluator, "_request", side_effect=request):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={"job_id": "job-1", "status": "running"},
                cancellation_reason="task_solved_by_peer",
                on_settled=released.set,
            )
            self.assertTrue(summary["deferred"])
            self.assertTrue(evaluator.remote_settlement_event.wait(timeout=1))

        self.assertFalse(released.is_set())
        self.assertEqual(evaluator.pending_settlement_watchers, 0)
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)

    def test_peer_cancel_watcher_keeps_gate_accounted_until_callback_finishes(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.02,
        )
        evaluator.deferred_settlement_timeout_seconds = 1.0
        gate = threading.BoundedSemaphore(1)
        self.assertTrue(gate.acquire(timeout=0))
        callback_started = threading.Event()
        callback_release = threading.Event()
        callback_finished = threading.Event()

        def release_gate() -> None:
            callback_started.set()
            callback_release.wait(timeout=1)
            gate.release()
            callback_finished.set()

        def request(
            method: str,
            _path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            if method == "DELETE":
                return {"job_id": "job-1", "status": "cancel_requested"}
            return {"job_id": "job-1", "status": "cancelled"}

        with patch.object(evaluator, "_request", side_effect=request):
            self.assertTrue(
                evaluator._start_settlement_watcher(  # noqa: SLF001
                    "job-1",
                    {"job_id": "job-1", "status": "running"},
                    on_settled=release_gate,
                )
            )
            self.assertTrue(callback_started.wait(timeout=1))
            self.assertEqual(evaluator.pending_settlement_watchers, 1)
            self.assertFalse(gate.acquire(blocking=False))
            callback_release.set()
            self.assertTrue(callback_finished.wait(timeout=1))

        deadline = time.monotonic() + 1
        while evaluator.pending_settlement_watchers and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(evaluator.pending_settlement_watchers, 0)
        self.assertTrue(gate.acquire(blocking=False))

    def test_peer_cancel_without_cancel_attempt_is_not_deferred(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.02,
        )
        # Simulate a caller whose cancellation deadline has already elapsed.
        evaluator.cancel_grace_seconds = 0.0
        with patch.object(evaluator, "_request") as request:
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={"job_id": "job-1", "status": "running"},
                cancellation_reason="task_solved_by_peer",
            )

        request.assert_not_called()
        self.assertFalse(summary.get("deferred", False))
        self.assertTrue(summary["unconfirmed"])
        self.assertEqual(summary["failure_category"], "cancel_settlement_unconfirmed")
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)

    def test_broker_revocation_defers_delete_timeout_settlement(self) -> None:
        """A broker revoke is known cancellation, even when DELETE times out."""

        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.02,
        )
        response = {
            "job_id": "job-1",
            "status": "running",
            "status_endpoint": "/settlement/job-1",
        }

        def request(
            method: str,
            _path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            if method == "DELETE":
                raise EvaluatorError("DELETE timed out")
            return response

        with (
            patch.object(evaluator, "_request", side_effect=request),
            patch.object(
                evaluator,
                "_start_settlement_watcher",
                return_value=True,
            ) as watcher,
        ):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response=response,
                cancellation_reason="broker_revoked",
            )

        watcher.assert_called_once()
        self.assertTrue(summary["deferred"])
        self.assertFalse(summary["unconfirmed"])
        self.assertEqual(summary["failure_category"], "cancel_settlement_deferred")
        self.assertEqual(evaluator.remote_unsettled_jobs, 0)

    def test_full_score_cancel_defers_with_legacy_coding_receipt(self) -> None:
        """A bound runner stop is recoverable without a retryable marker."""

        evaluator = CodingEvaluator(
            "https://judge.invalid",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.02,
        )
        response = {
            "job_id": "job-1",
            "status": "running",
            "status_endpoint": "/api/judge/jobs/job-1",
            "cancel_endpoint": "/api/judge/jobs/job-1",
        }

        def request(
            method: str,
            _path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            if method == "DELETE":
                return {
                    "job_id": "job-1",
                    "status": "cancel_requested",
                    "status_endpoint": "/api/judge/jobs/job-1",
                    "cancel_endpoint": "/api/judge/jobs/job-1",
                }
            return {
                "job_id": "job-1",
                "status": "cancel_requested",
                "status_endpoint": "/api/judge/jobs/job-1",
                "cancel_endpoint": "/api/judge/jobs/job-1",
            }

        with (
            patch.object(evaluator, "_request", side_effect=request),
            patch.object(
                evaluator,
                "_start_settlement_watcher",
                return_value=True,
            ) as watcher,
        ):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response=response,
                cancellation_reason="full_score",
            )

        watcher.assert_called_once()
        self.assertTrue(summary["deferred"])
        self.assertFalse(summary["unconfirmed"])
        self.assertEqual(summary["failure_category"], "cancel_settlement_deferred")
        self.assertEqual(evaluator.remote_unsettled_jobs, 0)

    def test_peer_cancel_watcher_callback_failure_latches_remote_work(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.02,
        )
        evaluator.deferred_settlement_timeout_seconds = 1.0
        callback_finished = threading.Event()

        def broken_callback() -> None:
            callback_finished.set()
            raise RuntimeError("permit release failed")

        with patch.object(
            evaluator,
            "_request",
            return_value={"job_id": "job-1", "status": "cancelled"},
        ):
            self.assertTrue(
                evaluator._start_settlement_watcher(  # noqa: SLF001
                    "job-1",
                    {"job_id": "job-1", "status": "running"},
                    on_settled=broken_callback,
                )
            )
            self.assertTrue(callback_finished.wait(timeout=1))
            deadline = time.monotonic() + 1
            while evaluator.pending_settlement_watchers and time.monotonic() < deadline:
                time.sleep(0.005)

        self.assertEqual(evaluator.pending_settlement_watchers, 0)
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)
        self.assertTrue(evaluator.remote_settlement_event.is_set())

    def test_peer_cancel_watcher_rotates_capabilities_and_binds_idless_terminal(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.02,
        )
        evaluator.deferred_settlement_timeout_seconds = 1.0
        released = threading.Event()
        calls: list[str] = []

        def request(
            method: str,
            path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            calls.append(f"{method} {path}")
            if method == "DELETE":
                return {
                    "job_id": "job-1",
                    "status": "cancel_requested",
                    "status_endpoint": "/status/first",
                }
            if path.startswith("/status/first"):
                return {
                    "job_id": "job-1",
                    "status": "running",
                    "status_endpoint": "/status/rotated",
                }
            if path.startswith("/status/rotated"):
                # A successful response from this job-scoped same-origin
                # capability may omit the redundant job id.
                return {"status": "cancelled"}
            return {"job_id": "job-1", "status": "running"}

        with patch.object(evaluator, "_request", side_effect=request):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={
                    "job_id": "job-1",
                    "status": "running",
                    "status_endpoint": "/status/first",
                },
                cancellation_reason="task_solved_by_peer",
                on_settled=released.set,
            )
            self.assertTrue(summary["deferred"])
            self.assertTrue(released.wait(timeout=1))

        self.assertTrue(any("/status/rotated" in call for call in calls))
        self.assertEqual(evaluator.pending_settlement_watchers, 0)
        self.assertEqual(evaluator.remote_unsettled_jobs, 0)

    def test_peer_cancel_watcher_rejects_contradictory_terminal_job(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.01,
            cancel_grace_seconds=0.02,
        )
        evaluator.deferred_settlement_timeout_seconds = 0.05
        released = threading.Event()

        def request(
            method: str,
            _path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            if method == "DELETE":
                return {"job_id": "job-1", "status": "cancel_requested"}
            return {"job_id": "job-other", "status": "cancelled"}

        with patch.object(evaluator, "_request", side_effect=request):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={"job_id": "job-1", "status": "running"},
                cancellation_reason="task_solved_by_peer",
                on_settled=released.set,
            )
            self.assertTrue(summary["deferred"])
            self.assertTrue(evaluator.remote_settlement_event.wait(timeout=1))

        self.assertFalse(released.is_set())
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)

    def test_peer_cancel_watcher_does_not_follow_endpoint_from_contradictory_receipt(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.005,
            cancel_grace_seconds=0.02,
        )
        evaluator.deferred_settlement_timeout_seconds = 0.08
        released = threading.Event()
        calls: list[str] = []

        def request(
            method: str,
            path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            calls.append(f"{method} {path}")
            if method == "DELETE":
                return {
                    "job_id": "job-1",
                    "status": "cancel_requested",
                    "status_endpoint": "/status/first",
                }
            if path.startswith("/status/first"):
                return {
                    "job_id": "job-other",
                    "status": "running",
                    "status_endpoint": "/status/evil",
                }
            if path.startswith("/status/evil"):
                # This is the endpoint an untrusted receipt tried to inject.
                return {"status": "cancelled"}
            return {"job_id": "job-1", "status": "running"}

        with patch.object(evaluator, "_request", side_effect=request):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={
                    "job_id": "job-1",
                    "status": "running",
                    "status_endpoint": "/status/first",
                },
                cancellation_reason="task_solved_by_peer",
                on_settled=released.set,
            )
            self.assertTrue(summary["deferred"])
            self.assertTrue(evaluator.remote_settlement_event.wait(timeout=1))

        self.assertFalse(released.is_set())
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)
        self.assertFalse(any("/status/evil" in call for call in calls))

    def test_peer_cancel_watcher_rejects_malformed_nested_job_identity(self) -> None:
        evaluator = LeanEvaluator(
            "https://judge.invalid",
            lean_env_id="test",
            poll_interval_seconds=0.005,
            cancel_grace_seconds=0.02,
        )
        evaluator.deferred_settlement_timeout_seconds = 0.08
        released = threading.Event()

        def request(
            method: str,
            path: str,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            if method == "DELETE":
                return {
                    "job_id": "job-1",
                    "status": "cancel_requested",
                    "status_endpoint": "/status/malformed",
                }
            if path.startswith("/status/malformed"):
                return {
                    "status": "cancelled",
                    "response": {"job_id": "not a valid job id"},
                }
            return {"job_id": "job-1", "status": "running"}

        with patch.object(evaluator, "_request", side_effect=request):
            summary = evaluator._cancel_submitted_job(  # noqa: SLF001
                "job-1",
                response={
                    "job_id": "job-1",
                    "status": "running",
                    "status_endpoint": "/status/malformed",
                },
                cancellation_reason="task_solved_by_peer",
                on_settled=released.set,
            )
            self.assertTrue(summary["deferred"])
            self.assertTrue(evaluator.remote_settlement_event.wait(timeout=1))

        self.assertFalse(released.is_set())
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)

    def test_global_latch_wakes_and_settles_an_inflight_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("cancel_terminal")
            verdicts = []
            with server.running() as url:
                evaluator = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    timeout_seconds=5,
                    poll_interval_seconds=0.01,
                    settlement_grace_seconds=1.0,
                    cancel_grace_seconds=0.2,
                )
                worker = threading.Thread(
                    target=lambda: verdicts.append(
                        evaluator.evaluate(_task(root), self._candidate(root))
                    )
                )
                worker.start()
                self.assertTrue(server.get_seen.wait(timeout=1))
                evaluator._mark_remote_unsettled()  # noqa: SLF001
                worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(server.deletes, 1)
        self.assertEqual(verdicts[0].status, "TASK_CANCELLED")
        self.assertEqual(
            verdicts[0].response["judge_cancellation"],
            {
                "attempted": True,
                "succeeded": True,
                "settled": True,
                "unconfirmed": False,
                "failure_category": None,
            },
        )
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)

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
                evaluator = LeanEvaluator(url, lean_env_id="test")
                verdict = evaluator.evaluate(
                    _task(root), self._candidate(root)
                )

        self.assertEqual(verdict.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual(verdict.response["last_observed_lifecycle"], "queued")
        self.assertIs(verdict.response["remote_settlement_unconfirmed"], True)
        self.assertEqual(
            verdict.response["evaluator_failure"]["category"],
            "missing_job_identifier",
        )
        self.assertIn("bindable job id", verdict.error or "")
        self.assertEqual(server.post_count, 1)
        self.assertEqual(server.deletes, 0)
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)
        self.assertTrue(evaluator.remote_settlement_event.is_set())

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

    def test_unconfirmed_proxy_503_is_not_resubmitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("unconfirmed_503")
            with server.running() as url:
                evaluator = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    admission_retry_seconds=1.0,
                )
                verdict = evaluator.evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
        self.assertEqual(verdict.score, 0.0)
        self.assertIs(verdict.response["remote_settlement_unconfirmed"], True)
        self.assertEqual(server.post_count, 1)
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)
        self.assertTrue(evaluator.remote_settlement_event.is_set())

    def test_unstructured_http_429_is_not_resubmitted_and_is_latched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("unconfirmed_429")
            with server.running() as url:
                evaluator = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    admission_retry_seconds=1.0,
                )
                verdict = evaluator.evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
        self.assertEqual(verdict.score, 0.0)
        self.assertIs(verdict.response["remote_settlement_unconfirmed"], True)
        self.assertEqual(server.post_count, 1)
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)
        self.assertTrue(evaluator.remote_settlement_event.is_set())

    def test_huge_finite_lifecycle_deadline_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("huge_lifecycle_deadline")
            with server.running() as url:
                evaluator = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    cancel_grace_seconds=0.1,
                    max_lifecycle_seconds=60.0,
                )
                verdict = evaluator.evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual(server.deletes, 1)
        self.assertIs(verdict.response["remote_settlement_unconfirmed"], True)
        self.assertEqual(
            verdict.response["settlement_error"],
            "cancel_settlement_unconfirmed",
        )
        self.assertIn("client safety cap", verdict.error or "")
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)

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

    def test_submit_job_id_binds_terminal_receipt_that_omits_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("terminal_without_receipt_job_id")
            with server.running() as url:
                verdict = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    poll_interval_seconds=0.01,
                ).evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "PROVED")
        self.assertEqual(verdict.judge_job_id, "job-1")
        self.assertEqual(verdict.response["job_id"], "job-1")
        self.assertIsNotNone(verdict.candidate_sha256)
        self.assertIsNotNone(verdict.task_contract_sha256)

    def test_terminal_poll_with_wrong_job_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("terminal_with_wrong_receipt_job_id")
            with server.running() as url:
                evaluator = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    timeout_seconds=1,
                    max_lifecycle_seconds=1,
                    poll_interval_seconds=0.01,
                    settlement_grace_seconds=0.05,
                    cancel_grace_seconds=0.1,
                )
                verdict = evaluator.evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
        self.assertEqual(verdict.score, 0.0)
        self.assertEqual(server.deletes, 1)
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)

    def test_unsettled_cancel_never_persists_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = _LifecycleServer("never_terminal")
            with server.running() as url:
                evaluator = LeanEvaluator(
                    url,
                    lean_env_id="test",
                    timeout_seconds=1,
                    poll_interval_seconds=0.01,
                    settlement_grace_seconds=0.1,
                    cancel_grace_seconds=0.1,
                )
                verdict = evaluator.evaluate(_task(root), self._candidate(root))

        self.assertEqual(verdict.status, "REMOTE_SETTLEMENT_UNCONFIRMED")
        self.assertEqual(server.deletes, 1)
        self.assertEqual(verdict.response["reason"], "remote_settlement_unconfirmed")
        self.assertIs(verdict.response["remote_settlement_unconfirmed"], True)
        self.assertEqual(
            verdict.response["settlement_error"],
            "cancel_settlement_unconfirmed",
        )
        self.assertNotIn("status", verdict.response)
        self.assertEqual(verdict.response["last_observed_lifecycle"], "running")
        self.assertEqual(evaluator.remote_unsettled_jobs, 1)

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


if __name__ == "__main__":
    unittest.main()
