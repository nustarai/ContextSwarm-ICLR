from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from contextswarm_mini.config import load_config
from contextswarm_mini.preflight import PreflightError, _result_cache_health, run_preflight


ROOT = Path(__file__).resolve().parents[1]


class _HealthHandler(BaseHTTPRequestHandler):
    enabled = False
    ok = True
    workspace_ready = True
    safeverify_ready = True
    formal_strict_safeverify_ready = True
    accepted_lean_env_ids = ["formal_matholympiadbench"]
    seen_path = ""

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen_path = self.path
        payload = {
            "ok": type(self).ok,
            "workspace_ready": type(self).workspace_ready,
            "safeverify_ready": type(self).safeverify_ready,
            "formal_strict_safeverify_ready": (
                type(self).formal_strict_safeverify_ready
            ),
            "accepted_lean_env_ids": type(self).accepted_lean_env_ids,
            "result_cache": {
                "enabled": type(self).enabled,
                "backend": "memory",
                "stats": {"private": "must-not-be-recorded"},
            },
        }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class CacheHealthServer:
    def __enter__(self) -> "CacheHealthServer":
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


class PreflightResultCacheTests(unittest.TestCase):
    def test_allocation_and_canary_manifests_require_disabled_cache(self) -> None:
        for path in (
            "configs/allocation_1h_cps48_uniform.toml",
            "configs/allocation_1h_cps48_formula.toml",
            "configs/allocation_1h_cps48_agent.toml",
            "configs/canary.toml",
        ):
            with self.subTest(path=path):
                config = load_config(path, ROOT)
                self.assertTrue(config.lean_require_result_cache_disabled)
                self.assertTrue(config.public_dict()["lean_require_result_cache_disabled"])

    def test_lean_health_requires_explicit_core_readiness(self) -> None:
        config = replace(
            load_config("configs/smoke.toml", ROOT),
            aisw_enabled=False,
            lean_server_url="http://judge.invalid",
            lean_require_result_cache_disabled=False,
        )
        healthy = {
            "ok": True,
            "workspace_ready": True,
            "accepted_lean_env_ids": [config.lean_env_id],
        }

        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            with (
                patch(
                    "contextswarm_mini.preflight.PiAgent.binary",
                    return_value="/bin/true",
                ),
                patch(
                    "contextswarm_mini.preflight.LeanEvaluator.health",
                    return_value=healthy,
                ),
            ):
                report = run_preflight(config, output)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["lean"]["requested_env_accepted"])
        self.assertNotIn("available_service_units", report["lean"])
        self.assertNotIn("capacity_state", report["lean"])

        failures = (
            ("missing_ok", {key: value for key, value in healthy.items() if key != "ok"}),
            ("false_ok", {**healthy, "ok": False}),
            (
                "missing_workspace",
                {key: value for key, value in healthy.items() if key != "workspace_ready"},
            ),
            ("unready_workspace", {**healthy, "workspace_ready": False}),
            (
                "missing_accepted_envs",
                {
                    "ok": True,
                    "workspace_ready": True,
                    "supported_lean_env_ids": [config.lean_env_id],
                },
            ),
            (
                "wrong_environment",
                {**healthy, "accepted_lean_env_ids": ["different-env"]},
            ),
        )
        for label, health in failures:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                with (
                    patch(
                        "contextswarm_mini.preflight.PiAgent.binary",
                        return_value="/bin/true",
                    ),
                    patch(
                        "contextswarm_mini.preflight.LeanEvaluator.health",
                        return_value=health,
                    ),
                ):
                    with self.assertRaises(PreflightError):
                        run_preflight(config, Path(raw))

    def test_lean_health_rejects_advertised_unavailable_capacity(self) -> None:
        config = replace(
            load_config("configs/smoke.toml", ROOT),
            aisw_enabled=False,
            lean_server_url="http://judge.invalid",
            lean_require_result_cache_disabled=False,
        )
        healthy = {
            "ok": True,
            "workspace_ready": True,
            "accepted_lean_env_ids": [config.lean_env_id],
            "available_service_units": 1,
            "capacity_state": "AVAILABLE",
        }
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch(
                    "contextswarm_mini.preflight.PiAgent.binary",
                    return_value="/bin/true",
                ),
                patch(
                    "contextswarm_mini.preflight.LeanEvaluator.health",
                    return_value=healthy,
                ),
            ):
                report = run_preflight(config, Path(raw))
        self.assertEqual(report["lean"]["available_service_units"], 1)
        self.assertEqual(report["lean"]["capacity_state"], "AVAILABLE")

        failures = (
            ("zero_units", {**healthy, "available_service_units": 0}),
            ("negative_units", {**healthy, "available_service_units": -1}),
            ("invalid_units", {**healthy, "available_service_units": True}),
            ("degraded", {**healthy, "capacity_state": "DEGRADED"}),
            ("saturated", {**healthy, "capacity_state": "SATURATED"}),
            ("unknown", {**healthy, "capacity_state": "UNKNOWN"}),
        )
        for label, health in failures:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                with (
                    patch(
                        "contextswarm_mini.preflight.PiAgent.binary",
                        return_value="/bin/true",
                    ),
                    patch(
                        "contextswarm_mini.preflight.LeanEvaluator.health",
                        return_value=health,
                    ),
                ):
                    with self.assertRaises(PreflightError):
                        run_preflight(config, Path(raw))

    def test_cache_health_accepts_base_or_healthz_and_keeps_only_safe_evidence(self) -> None:
        _HealthHandler.enabled = False
        _HealthHandler.ok = True
        _HealthHandler.workspace_ready = True
        _HealthHandler.safeverify_ready = True
        _HealthHandler.formal_strict_safeverify_ready = True
        _HealthHandler.accepted_lean_env_ids = ["formal_matholympiadbench"]
        with CacheHealthServer() as server:
            base = _result_cache_health(
                server.base_url, "formal_matholympiadbench"
            )
            explicit = _result_cache_health(
                server.base_url + "/healthz", "formal_matholympiadbench"
            )
        self.assertEqual(
            base,
            {
                "enabled": False,
                "backend": "memory",
                "backend_ready": True,
                "requested_env_accepted": True,
            },
        )
        self.assertEqual(explicit, base)
        self.assertEqual(_HealthHandler.seen_path, "/healthz")

    def test_cache_health_rejects_wrong_environment_or_unready_backend(self) -> None:
        _HealthHandler.enabled = False
        _HealthHandler.ok = True
        _HealthHandler.workspace_ready = True
        _HealthHandler.safeverify_ready = True
        _HealthHandler.formal_strict_safeverify_ready = True
        _HealthHandler.accepted_lean_env_ids = ["different-environment"]
        with CacheHealthServer() as server:
            with self.assertRaisesRegex(PreflightError, "requested environment"):
                _result_cache_health(server.base_url, "formal_matholympiadbench")

        _HealthHandler.accepted_lean_env_ids = ["formal_matholympiadbench"]
        for field in (
            "ok",
            "workspace_ready",
            "safeverify_ready",
            "formal_strict_safeverify_ready",
        ):
            with self.subTest(field=field):
                setattr(_HealthHandler, field, False)
                with CacheHealthServer() as server:
                    with self.assertRaisesRegex(PreflightError, "not ready"):
                        _result_cache_health(
                            server.base_url, "formal_matholympiadbench"
                        )
                setattr(_HealthHandler, field, True)

    def test_required_cache_health_fails_closed_when_missing_or_enabled(self) -> None:
        config = replace(
            load_config("configs/smoke.toml", ROOT),
            aisw_enabled=False,
            lean_server_url="http://judge.invalid",
            lean_require_result_cache_disabled=True,
        )
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            with (
                patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                patch(
                    "contextswarm_mini.preflight.LeanEvaluator.health",
                    return_value={
                        "ok": True,
                        "workspace_ready": True,
                        "accepted_lean_env_ids": [config.lean_env_id],
                    },
                ),
                patch.dict(
                    "os.environ",
                    {"CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL": ""},
                    clear=False,
                ),
            ):
                with self.assertRaisesRegex(PreflightError, "CACHE_HEALTH_URL"):
                    run_preflight(config, output)

            _HealthHandler.enabled = True
            _HealthHandler.ok = True
            _HealthHandler.workspace_ready = True
            _HealthHandler.safeverify_ready = True
            _HealthHandler.formal_strict_safeverify_ready = True
            _HealthHandler.accepted_lean_env_ids = ["formal_matholympiadbench"]
            with CacheHealthServer() as server:
                with (
                    patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                    patch(
                        "contextswarm_mini.preflight.LeanEvaluator.health",
                        return_value={
                            "ok": True,
                            "workspace_ready": True,
                            "accepted_lean_env_ids": [config.lean_env_id],
                        },
                    ),
                    patch.dict(
                        "os.environ",
                        {"CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL": server.base_url},
                        clear=False,
                    ),
                ):
                    with self.assertRaisesRegex(PreflightError, "not verifiably disabled"):
                        run_preflight(config, output)

    def test_disabled_cache_evidence_is_written_without_endpoint(self) -> None:
        config = replace(
            load_config("configs/smoke.toml", ROOT),
            aisw_enabled=False,
            lean_server_url="http://judge.invalid",
            lean_require_result_cache_disabled=True,
        )
        _HealthHandler.enabled = False
        _HealthHandler.ok = True
        _HealthHandler.workspace_ready = True
        _HealthHandler.safeverify_ready = True
        _HealthHandler.formal_strict_safeverify_ready = True
        _HealthHandler.accepted_lean_env_ids = ["formal_matholympiadbench"]
        with tempfile.TemporaryDirectory() as raw, CacheHealthServer() as server:
            output = Path(raw)
            with (
                patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                patch(
                    "contextswarm_mini.preflight.LeanEvaluator.health",
                    return_value={
                        "ok": True,
                        "workspace_ready": True,
                        "accepted_lean_env_ids": [config.lean_env_id],
                    },
                ),
                patch.dict(
                    "os.environ",
                    {"CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL": server.base_url},
                    clear=False,
                ),
            ):
                report = run_preflight(config, output)
            rendered = (output / "transport_preflight.json").read_text(encoding="utf-8")
        self.assertFalse(report["lean"]["result_cache"]["enabled"])
        self.assertTrue(report["lean"]["result_cache"]["backend_ready"])
        self.assertTrue(
            report["lean"]["result_cache"]["requested_env_accepted"]
        )
        self.assertNotIn(server.base_url, rendered)


if __name__ == "__main__":
    unittest.main()
