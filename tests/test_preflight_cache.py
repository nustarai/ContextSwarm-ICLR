from __future__ import annotations

from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from contextswarm_mini.config import load_config
from contextswarm_mini.models import Verdict
from contextswarm_mini.preflight import (
    PreflightError,
    _result_cache_health,
    run_preflight,
)


ROOT = Path(__file__).resolve().parents[1]


class _HealthHandler(BaseHTTPRequestHandler):
    enabled = False
    ok = True
    workspace_ready = True
    safeverify_ready = True
    formal_strict_safeverify_ready = True
    accepted_lean_env_ids = ["formal_matholympiadbench"]
    deployment_id = None
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
        if type(self).deployment_id is not None:
            payload["deployment_id"] = type(self).deployment_id
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
    def setUp(self) -> None:
        _HealthHandler.deployment_id = None

    @staticmethod
    def _strict_index(root: Path, revision: str = "rev-1") -> tuple[Path, str]:
        path = root / "decl-index.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE decls (name TEXT, kind TEXT, file TEXT, line INTEGER, head TEXT, snippet TEXT)"
            )
            connection.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [
                    ("schema", "decl_index_v1"),
                    ("mathlib_revision", revision),
                    ("lean_toolchain", "v4.9"),
                ],
            )
            connection.execute(
                "INSERT INTO decls VALUES (?, ?, ?, ?, ?, ?)",
                ("Nat.succ", "def", "Mathlib/Nat.lean", 1, "Nat.succ", "Nat.succ : Nat -> Nat"),
            )
            connection.commit()
        finally:
            connection.close()
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    @classmethod
    def _strict_config(
        cls,
        index: Path,
        digest: str,
        revision: str = "rev-1",
    ):
        return replace(
            load_config("configs/smoke.toml", ROOT),
            aisw_enabled=False,
            lean_server_url="http://judge.invalid",
            lean_require_result_cache_disabled=False,
            formal_tools_enabled=True,
            formal_tools_require_decl_index=True,
            formal_tools_decl_index=str(index),
            formal_tools_decl_index_sha256=digest,
            formal_tools_mathlib_revision=revision,
        )

    @staticmethod
    def _healthy(revision: str = "rev-1") -> dict[str, object]:
        return {
            "ok": True,
            "workspace_ready": True,
            "accepted_lean_env_ids": ["formal_matholympiadbench"],
            "mathlib_revision": revision,
        }

    @staticmethod
    def _kernel(revision: str = "rev-1", *, status: str = "PROVED") -> Verdict:
        return Verdict(
            "__contextswarm_preflight_kernel__",
            status,
            0.0,
            0.001,
            {
                "is_valid_no_sorry": status == "PROVED",
                "mathlib_revision": revision,
            },
        )

    def test_strict_formal_preflight_binds_kernel_and_index_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, digest = self._strict_index(root)
            config = self._strict_config(index, digest)
            with (
                patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                patch(
                    "contextswarm_mini.preflight.LeanEvaluator.health",
                    return_value=self._healthy(),
                ),
                patch(
                    "contextswarm_mini.preflight._kernel_probe",
                    return_value=self._kernel(),
                ),
            ):
                report = run_preflight(config, root / "run")
        self.assertEqual(report["lean"]["endpoint_mathlib_revision"], "rev-1")
        self.assertEqual(
            report["formal_tools"]["declaration_index"]["sha256"],
            digest,
        )
        self.assertEqual(
            report["formal_tools"]["declaration_index"]["mathlib_revision"],
            "rev-1",
        )

    def test_strict_formal_preflight_rejects_kernel_status_or_revision_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, digest = self._strict_index(root)
            config = self._strict_config(index, digest)
            common = patch(
                "contextswarm_mini.preflight.PiAgent.binary",
                return_value="/bin/true",
            )
            with (
                common,
                patch(
                    "contextswarm_mini.preflight.LeanEvaluator.health",
                    return_value=self._healthy(),
                ),
                patch(
                    "contextswarm_mini.preflight._kernel_probe",
                    return_value=self._kernel(status="VERIFY_FAIL"),
                ),
            ):
                with self.assertRaisesRegex(PreflightError, "kernel probe"):
                    run_preflight(config, root / "bad-kernel")
            with (
                patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                patch(
                    "contextswarm_mini.preflight.LeanEvaluator.health",
                    return_value=self._healthy("rev-health"),
                ),
                patch(
                    "contextswarm_mini.preflight._kernel_probe",
                    return_value=self._kernel("rev-kernel"),
                ),
            ):
                with self.assertRaisesRegex(PreflightError, "revisions disagree"):
                    run_preflight(config, root / "bad-revision")

    def test_strict_formal_preflight_rejects_sha_and_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, digest = self._strict_index(root)
            config = self._strict_config(index, "0" * 64)
            with self.assertRaisesRegex(PreflightError, "snapshot preparation"):
                run_preflight(config, root / "bad-sha")

            bad = root / "bad-schema.sqlite3"
            connection = sqlite3.connect(bad)
            try:
                connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute(
                    "CREATE TABLE decls (name TEXT, kind TEXT, file TEXT, line INTEGER, head TEXT, snippet TEXT)"
                )
                connection.execute("INSERT INTO meta VALUES ('schema', 'wrong')")
                connection.execute("INSERT INTO meta VALUES ('mathlib_revision', 'rev-1')")
                connection.commit()
            finally:
                connection.close()
            bad_digest = hashlib.sha256(bad.read_bytes()).hexdigest()
            bad_config = self._strict_config(bad, bad_digest)
            with (
                patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                patch(
                    "contextswarm_mini.preflight.LeanEvaluator.health",
                    return_value=self._healthy(),
                ),
                patch(
                    "contextswarm_mini.preflight._kernel_probe",
                    return_value=self._kernel(),
                ),
            ):
                with self.assertRaisesRegex(PreflightError, "unavailable or incompatible"):
                    run_preflight(bad_config, root / "bad-schema")

    def test_cache_health_must_match_execution_deployment_when_endpoint_differs(self) -> None:
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
        _HealthHandler.accepted_lean_env_ids = [config.lean_env_id]
        _HealthHandler.deployment_id = "cache-deployment"
        try:
            with tempfile.TemporaryDirectory() as raw, CacheHealthServer() as server:
                with (
                    patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                    patch(
                        "contextswarm_mini.preflight.LeanEvaluator.health",
                        return_value={
                            **self._healthy(),
                            "deployment_id": "execution-deployment",
                        },
                    ),
                    patch.dict(
                        "os.environ",
                        {"CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL": server.base_url},
                        clear=False,
                    ),
                ):
                    with self.assertRaisesRegex(PreflightError, "deployment identity"):
                        run_preflight(config, Path(raw))
        finally:
            _HealthHandler.deployment_id = None
    def test_allocation_and_canary_manifests_require_disabled_cache(self) -> None:
        for path in (
            "configs/allocation_1h_cps48_uniform.toml",
            "configs/allocation_1h_cps48_formula.toml",
            "configs/allocation_1h_cps48_agent.toml",
            "configs/canary.toml",
            "configs/formal_1h_mono.toml",
            "configs/formal_1h_parallel.toml",
            "configs/formal_1h_cps12.toml",
            "configs/formal_1h_cps24.toml",
            "configs/formal_1h_cps48.toml",
            "configs/formal_1h_cps96.toml",
            "configs/formal_1h_cps192.toml",
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

    def test_lean_health_distinguishes_disabled_group_capacity_from_direct_pool(self) -> None:
        config = replace(
            load_config("configs/smoke.toml", ROOT),
            aisw_enabled=False,
            lean_server_url="http://judge.invalid",
            lean_require_result_cache_disabled=False,
        )
        health = {
            "ok": True,
            "workspace_ready": True,
            "accepted_lean_env_ids": [config.lean_env_id],
            "active_workers": 96,
            "ready_workers": 96,
            "available_service_units": 0,
            "capacity_state": "DEGRADED",
            "capacity_error_kind": "admission_disabled",
            "group_admission": {"enabled": False, "status": "disabled"},
        }
        with tempfile.TemporaryDirectory() as raw:
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
                report = run_preflight(config, Path(raw))
        self.assertFalse(report["lean"]["group_admission_enabled"])
        self.assertEqual(report["lean"]["ready_workers"], 96)
        self.assertNotIn("available_service_units", report["lean"])
        self.assertNotIn("capacity_state", report["lean"])

        unavailable = dict(health)
        unavailable["ready_workers"] = 0
        unavailable["active_workers"] = 0
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch(
                    "contextswarm_mini.preflight.PiAgent.binary",
                    return_value="/bin/true",
                ),
                patch(
                    "contextswarm_mini.preflight.LeanEvaluator.health",
                    return_value=unavailable,
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
            _HealthHandler.deployment_id = "judge-deployment"
            with CacheHealthServer() as server:
                with (
                    patch("contextswarm_mini.preflight.PiAgent.binary", return_value="/bin/true"),
                    patch(
                        "contextswarm_mini.preflight.LeanEvaluator.health",
                        return_value={
                            "ok": True,
                            "workspace_ready": True,
                            "accepted_lean_env_ids": [config.lean_env_id],
                            "deployment_id": "judge-deployment",
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
        _HealthHandler.deployment_id = "judge-deployment"
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
                        "deployment_id": "judge-deployment",
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
        _HealthHandler.deployment_id = None


if __name__ == "__main__":
    unittest.main()
