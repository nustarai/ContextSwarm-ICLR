from __future__ import annotations

import json
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from contextswarm_mini.evaluator import EvaluatorError, LeanEvaluator
from contextswarm_mini.models import Task


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

    def _request(self, method: str, path: str, payload=None, *, timeout_seconds=None):  # type: ignore[no-untyped-def]
        self.payloads.append(dict(payload or {}))
        result: dict[str, object] = {
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


class _OneFailureEvaluator(_CountingEvaluator):
    def _request(self, method: str, path: str, payload=None, *, timeout_seconds=None):  # type: ignore[no-untyped-def]
        self.payloads.append(dict(payload or {}))
        if len(self.payloads) == 1:
            raise EvaluatorError("temporary")
        return {"status": "failed", "formal_status": "VERIFY_FAIL"}


class _DeadlineRecordingEvaluator(_CountingEvaluator):
    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float | None] = []

    def _request(self, method: str, path: str, payload=None, *, timeout_seconds=None):  # type: ignore[no-untyped-def]
        self.timeouts.append(timeout_seconds)
        return super()._request(
            method, path, payload, timeout_seconds=timeout_seconds
        )


class _OverloadedEvaluator(_CountingEvaluator):
    def _request(self, method: str, path: str, payload=None, *, timeout_seconds=None):  # type: ignore[no-untyped-def]
        del method, path, payload, timeout_seconds
        raise EvaluatorError(
            "https://judge.invalid/private token=test-secret /home/test/source.lean",
            category="judge_overloaded_deadline",
            http_status=429,
            attempts=7,
            retry_after_seconds=12.5,
        )


class _UnsafeResponseEvaluator(_CountingEvaluator):
    def _request(self, method: str, path: str, payload=None, *, timeout_seconds=None):  # type: ignore[no-untyped-def]
        del method, path, payload, timeout_seconds
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

    def _request(self, method: str, path: str, payload=None, *, timeout_seconds=None):  # type: ignore[no-untyped-def]
        del method, path, payload, timeout_seconds
        self.clock[0] = 2.0
        raise EvaluatorError(
            "The Judge request deadline elapsed.",
            category="request_deadline_elapsed",
            attempts=1,
        )


class EvaluatorProbeTests(unittest.TestCase):
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

    def test_http_429_and_503_retry_past_three_attempts_within_horizon(self) -> None:
        requests = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                nonlocal requests
                requests += 1
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if requests < 5:
                    self.send_response(429 if requests == 1 else 503)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                raw = b'{"status":"failed","formal_status":"VERIFY_FAIL"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            evaluator = LeanEvaluator(
                f"http://127.0.0.1:{server.server_port}",
                lean_env_id="fixed-env",
                timeout_seconds=5,
            )
            with patch(
                "contextswarm_mini.evaluator._MIN_HTTP_BACKOFF_SECONDS", 0.001
            ), patch(
                "contextswarm_mini.evaluator._MAX_HTTP_BACKOFF_SECONDS", 0.002
            ):
                payload = evaluator._request(
                    "POST", "/api/lean/jobs", {"code": "proof"}
                )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
        self.assertEqual(payload["formal_status"], "VERIFY_FAIL")
        self.assertEqual(requests, 5)

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
                return b'{"status":"failed","formal_status":"VERIFY_FAIL"}'

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
                    None,
                )
            return Response()

        evaluator = LeanEvaluator(
            "http://unused", lean_env_id="fixed-env", timeout_seconds=20
        )
        with patch(
            "contextswarm_mini.evaluator.urlopen", side_effect=fake_urlopen
        ), patch("contextswarm_mini.evaluator.time.sleep") as sleep:
            response = evaluator._request(
                "POST", "/api/lean/jobs", {"code": "proof"}, timeout_seconds=20
            )
        self.assertEqual(response["formal_status"], "VERIFY_FAIL")
        self.assertEqual(attempts, 2)
        sleep.assert_called_once_with(7.25)

    def test_retry_after_beyond_remaining_horizon_fails_without_sleeping(self) -> None:
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
                    "POST", "/api/lean/jobs", {"code": "proof"}, timeout_seconds=0.1
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
                return b'{"status":"failed","formal_status":"VERIFY_FAIL"}'

        def fake_urlopen(request, *, timeout):  # type: ignore[no-untyped-def]
            del timeout
            submitted_timeouts.append(json.loads(request.data)["timeout"])
            if len(submitted_timeouts) == 1:
                raise HTTPError(
                    "https://judge.invalid/private",
                    429,
                    "busy",
                    headers,
                    None,
                )
            return Response()

        evaluator = LeanEvaluator(
            "http://unused", lean_env_id="fixed-env", timeout_seconds=10
        )
        with patch(
            "contextswarm_mini.evaluator.urlopen", side_effect=fake_urlopen
        ), patch(
            "contextswarm_mini.evaluator.time.monotonic",
            side_effect=lambda: clock[0],
        ), patch(
            "contextswarm_mini.evaluator.time.sleep",
            side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ):
            response = evaluator._request(
                "POST",
                "/api/lean/jobs",
                {"code": "proof", "timeout": 10},
                timeout_seconds=8,
            )
        self.assertEqual(response["formal_status"], "VERIFY_FAIL")
        self.assertEqual(submitted_timeouts, [8, 1])


if __name__ == "__main__":
    unittest.main()
