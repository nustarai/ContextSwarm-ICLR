from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from contextswarm_mini.config import ConfigError, load_config
from contextswarm_mini.models import Task
from contextswarm_mini.pi_agent import PiAgent
from contextswarm_mini.prompts import build_task_prompt


ROOT = Path(__file__).resolve().parents[1]


def _load_manifest(body: str):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "route-claims.toml"
        path.write_text(
            f'extends = ["{ROOT / "configs" / "smoke.toml"}"]\n{body}',
            encoding="utf-8",
        )
        return load_config(path, ROOT)


class CPSFeatureConfigTests(unittest.TestCase):
    def test_legacy_manifests_are_route_claim_off_and_publicly_explicit(self) -> None:
        config = load_config("configs/smoke.toml", ROOT)
        self.assertFalse(config.cps_features.route_claim_required)
        self.assertFalse(config.cps_features.active_roster_enabled)
        self.assertEqual(config.cps_features.route_claim_ttl_seconds, 900)
        self.assertEqual(config.cps_features.activity_feedback_prompt_mode, "advisory")
        self.assertFalse(config.cps_features.per_recipient_receipts)
        self.assertFalse(config.cps_features.knowledge_promotion)
        self.assertEqual(
            config.public_dict()["cps_features"], config.cps_features.public_dict()
        )
        self.assertFalse(config.route_claim_required)

    def test_route_treatment_parses_and_changes_identity_only_through_feature_table(self) -> None:
        baseline = load_config("configs/smoke.toml", ROOT)
        treatment = _load_manifest(
            """
[cps_features]
route_claim_required = true
route_claim_ttl_seconds = 321
per_recipient_receipts = false
knowledge_promotion = false
"""
        )
        self.assertTrue(treatment.route_claim_required)
        self.assertTrue(treatment.active_roster_enabled)
        self.assertEqual(treatment.route_claim_ttl_seconds, 321)
        self.assertNotEqual(
            baseline.public_dict()["cps_features"],
            treatment.public_dict()["cps_features"],
        )
        self.assertEqual(
            treatment.public_dict()["cps_features"],
            {
                "route_claim_required": True,
                "route_claim_ttl_seconds": 321,
                "activity_feedback_prompt_mode": "advisory",
                "per_recipient_receipts": False,
                "knowledge_promotion": False,
            },
        )

    def test_profiled_cps32_control_and_treatment_differ_only_by_route_flag(self) -> None:
        control = load_config(
            "configs/formal_1h_cps32_profiled_route_control.toml", ROOT
        )
        treatment = load_config(
            "configs/formal_1h_cps32_profiled_route_claim.toml", ROOT
        )
        self.assertEqual(control.max_parallel, 32)
        self.assertEqual(control.aisw_max_in_flight, 32)
        self.assertEqual(control.lean_max_concurrent_evaluations, 32)
        self.assertEqual(control.time_limit_seconds, 3600)
        self.assertFalse(control.route_claim_required)
        self.assertTrue(treatment.route_claim_required)

        expected = control.public_dict()
        expected["name"] = treatment.name
        expected["cps_features"] = dict(expected["cps_features"])
        expected["cps_features"]["route_claim_required"] = True
        self.assertEqual(treatment.public_dict(), expected)

    def test_route_mock_control_and_treatment_differ_only_by_route_flag(self) -> None:
        control = load_config("configs/route_claim_smoke_control.toml", ROOT)
        treatment = load_config("configs/route_claim_smoke.toml", ROOT)
        expected = control.public_dict()
        expected["name"] = treatment.name
        expected["cps_features"] = dict(expected["cps_features"])
        expected["cps_features"]["route_claim_required"] = True
        self.assertEqual(treatment.public_dict(), expected)

    def test_feature_table_is_strict_and_route_requires_cps(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown cps_features fields"):
            _load_manifest("[cps_features]\nunknown = true\n")
        with self.assertRaisesRegex(ConfigError, "activity_feedback_prompt_mode"):
            _load_manifest('[cps_features]\nactivity_feedback_prompt_mode = "force"\n')
        with self.assertRaisesRegex(ConfigError, "activity_feedback_prompt_mode"):
            _load_manifest('[cps_features]\nactivity_feedback_prompt_mode = true\n')
        with self.assertRaisesRegex(ConfigError, "must be a boolean"):
            _load_manifest('[cps_features]\nroute_claim_required = "true"\n')
        with self.assertRaisesRegex(ConfigError, "must be positive"):
            _load_manifest("[cps_features]\nroute_claim_ttl_seconds = 0\n")
        with self.assertRaisesRegex(ConfigError, "must be an integer"):
            _load_manifest("[cps_features]\nroute_claim_ttl_seconds = true\n")
        with self.assertRaisesRegex(ConfigError, "must be an integer"):
            _load_manifest("[cps_features]\nroute_claim_ttl_seconds = 1.5\n")
        with self.assertRaisesRegex(ConfigError, "must not exceed 86400"):
            _load_manifest("[cps_features]\nroute_claim_ttl_seconds = 86401\n")
        with self.assertRaisesRegex(ConfigError, "requires experiment.mode = cps"):
            _load_manifest(
                '[experiment]\nmode = "parallel"\ncommunication = "none"\n\n'
                "[cps_features]\nroute_claim_required = true\n"
            )

    def test_short_cps_table_alias_is_canonicalized(self) -> None:
        config = _load_manifest(
            "[cps]\nroute_claim_required = true\nroute_claim_ttl_seconds = 17\n"
        )
        self.assertTrue(config.cps_features.route_claim_required)
        self.assertEqual(config.cps_features.route_claim_ttl_seconds, 17)
        self.assertEqual(
            config.public_dict()["cps_features"]["route_claim_required"], True
        )

    def test_activity_prompt_policy_manifests_differ_only_by_name_and_mode(self) -> None:
        advisory = load_config(
            "configs/formal_1h_cps32_profiled_activity_advisory.toml", ROOT
        )
        strong = load_config(
            "configs/formal_1h_cps32_profiled_activity_strong.toml", ROOT
        )
        self.assertEqual(advisory.cps_features.activity_feedback_prompt_mode, "advisory")
        self.assertEqual(strong.cps_features.activity_feedback_prompt_mode, "strong")
        expected = advisory.public_dict()
        expected["name"] = strong.name
        expected["cps_features"] = dict(expected["cps_features"])
        expected["cps_features"]["activity_feedback_prompt_mode"] = "strong"
        self.assertEqual(strong.public_dict(), expected)

    def test_pi_session_receives_controlled_route_capability_and_tools(self) -> None:
        config = _load_manifest(
            "[cps_features]\nroute_claim_required = true\nroute_claim_ttl_seconds = 77\n"
        )
        agent = PiAgent(config)
        tools = set(agent.solver_tools())
        self.assertTrue(
            {
                "cps_active_routes",
                "cps_claim_route",
                "cps_update_route",
                "cps_release_route",
            }.issubset(tools)
        )
        with tempfile.TemporaryDirectory() as temporary:
            env = agent.environment(
                task_id="task",
                actor_id="actor",
                workdir=Path(temporary),
            )
        self.assertEqual(env["CONTEXTSWARM_CPS_ROUTE_CLAIM_REQUIRED"], "1")
        self.assertEqual(env["CONTEXTSWARM_CPS_ACTIVE_ROSTER_ENABLED"], "1")
        self.assertEqual(env["CONTEXTSWARM_CPS_ROUTE_CLAIM_TTL_SECONDS"], "77")
        bypass_env = agent.environment(
            task_id="task",
            actor_id="actor",
            workdir=Path(temporary),
            route_claim_bypass_reason="unavailable",
        )
        self.assertEqual(
            bypass_env["CONTEXTSWARM_CPS_ROUTE_CLAIM_BYPASS_REASON"],
            "unavailable",
        )
        command = agent.command()
        system_prompt = command[command.index("--system-prompt") + 1]
        self.assertIn("cps_active_routes", system_prompt)
        self.assertIn("route_claim_bypass_reason=unavailable", system_prompt)

    def test_pi_cannot_relax_manifest_required_route_gate(self) -> None:
        config = _load_manifest(
            "[cps_features]\nroute_claim_required = true\n"
        )
        agent = PiAgent(config)
        with self.assertRaisesRegex(ValueError, "cannot disable manifest-required"):
            agent.solver_tools(route_claim_required=False)
        with self.assertRaisesRegex(ValueError, "cannot disable manifest-required"):
            agent.command(route_claim_required=False)

    def test_feature_off_keeps_legacy_solver_surface(self) -> None:
        config = load_config("configs/smoke.toml", ROOT)
        agent = PiAgent(config)
        tools = set(agent.solver_tools())
        self.assertFalse(any(name.startswith("cps_active_routes") for name in tools))
        self.assertFalse(any(name.startswith("cps_claim_route") for name in tools))
        with tempfile.TemporaryDirectory() as temporary:
            env = agent.environment(
                task_id="task", actor_id="actor", workdir=Path(temporary)
            )
        self.assertEqual(env["CONTEXTSWARM_CPS_ROUTE_CLAIM_REQUIRED"], "0")

    def test_prompt_states_route_order_and_fail_open_boundary(self) -> None:
        task = Task(
            "sample",
            Path("benchmarks/sample"),
            "theorem example : True := by sorry",
            {},
            {},
        )
        prompt = build_task_prompt(
            task,
            task_workspace="tasks/sample",
            agent_id="worker-sample-e1",
            episode=1,
            communication_enabled=True,
            route_claim_required=True,
            route_claim_ttl_seconds=123,
        )
        route_index = prompt.index("cps_active_routes")
        claim_index = prompt.index("cps_claim_route")
        checkpoint_index = prompt.index("Complete the mandatory early `judge_check`", route_index)
        self.assertLess(route_index, checkpoint_index)
        self.assertLess(claim_index, checkpoint_index)
        self.assertIn("manifest-bound lease TTL of 123 seconds", prompt)
        self.assertIn("route_claim_bypass_reason=unavailable", prompt)


class SolverToolRouteSurfaceTests(unittest.TestCase):
    def _registered_tools(self, *, required: bool) -> dict[str, dict]:
        script = """
const { default: register } = await import(process.argv[1]);
const tools = {};
register({ registerTool(definition) { tools[definition.name] = definition; }, on() {} });
console.log(JSON.stringify(tools));
"""
        env = os.environ | {
            "CONTEXTSWARM_CPS_ROUTE_CLAIM_REQUIRED": "1" if required else "0",
        }
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "--eval",
                script,
                str(ROOT / "contextswarm_mini/pi_solver_tools.mjs"),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return json.loads(result.stdout)

    def test_route_tools_have_bounded_payloads(self) -> None:
        tools = self._registered_tools(required=True)
        self.assertTrue(
            {
                "cps_active_routes",
                "cps_claim_route",
                "cps_update_route",
                "cps_release_route",
            }.issubset(tools)
        )
        claim = tools["cps_claim_route"]
        self.assertEqual(claim["parameters"]["required"], ["route_key", "summary"])
        self.assertEqual(
            set(claim["parameters"]["properties"]),
            {"route_key", "summary", "ttl_seconds", "independent_verification_reason"},
        )
        self.assertEqual(
            tools["cps_update_route"]["parameters"]["required"], ["claim_id"]
        )
        self.assertEqual(
            tools["cps_release_route"]["parameters"]["required"], ["claim_id"]
        )

    def test_write_gate_is_treatment_only(self) -> None:
        script = """
const { default: register } = await import(process.argv[1]);
let guard;
const tools = {};
register({
  registerTool(definition) { tools[definition.name] = definition; },
  on(event, callback) { if (event === "tool_call") guard = callback; },
});
const answer = guard({ toolName: "write", input: { path: "result.lean" } }, { cwd: process.env.CONTEXTSWARM_WORKDIR });
console.log(JSON.stringify({ answer, names: Object.keys(tools) }));
"""
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ | {
                "CONTEXTSWARM_CPS_ROUTE_CLAIM_REQUIRED": "1",
                "CONTEXTSWARM_WORKDIR": temporary,
            }
            result = subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "--eval",
                    script,
                    str(ROOT / "contextswarm_mini/pi_solver_tools.mjs"),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
        payload = json.loads(result.stdout)
        self.assertTrue(payload["answer"]["block"])
        self.assertIn("cps_claim_route", payload["answer"]["reason"])

    def test_write_gate_accepts_only_explicit_fail_open_marker(self) -> None:
        script = """
const { default: register } = await import(process.argv[1]);
let guard;
register({ registerTool() {}, on(event, callback) { if (event === "tool_call") guard = callback; } });
const answer = guard({ toolName: "write", input: { path: "result.lean" } }, { cwd: process.env.CONTEXTSWARM_WORKDIR });
console.log(JSON.stringify({ blocked: answer?.block === true, answer }));
"""
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ | {
                "CONTEXTSWARM_CPS_ROUTE_CLAIM_REQUIRED": "1",
                "CONTEXTSWARM_CPS_ROUTE_CLAIM_BYPASS_REASON": "unavailable",
                "CONTEXTSWARM_WORKDIR": temporary,
            }
            result = subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "--eval",
                    script,
                    str(ROOT / "contextswarm_mini/pi_solver_tools.mjs"),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
        payload = json.loads(result.stdout)
        self.assertFalse(payload["blocked"])

    def test_invalid_request_never_bypasses_and_claim_negative_retires_local_lease(self) -> None:
        script = r"""
const { default: register } = await import(process.argv[1]);
let guard;
const tools = {};
const responses = [
  { ok: false, accepted: false, status: "INVALID_REQUEST", retryable: false },
  {
    ok: true,
    accepted: true,
    acquired: true,
    claimed: true,
    status: "active",
    claim: {
      claim_id: "claim-one",
      task_id: "task",
      actor_id: "actor",
      episode: 1,
      route_key: "same-route",
      summary: "primary",
      status: "active",
      active: true,
      is_primary: true,
      primary: true,
      expires_at: "2999-01-01T00:00:00.000000Z",
    },
  },
  { ok: false, accepted: false, status: "conflict" },
];
globalThis.fetch = async () => ({
  ok: true,
  async json() { return responses.shift(); },
});
register({
  registerTool(definition) { tools[definition.name] = definition; },
  on(event, callback) { if (event === "tool_call") guard = callback; },
});
const checkWrite = () => guard(
  { toolName: "write", input: { path: "result.lean" } },
  { cwd: process.env.CONTEXTSWARM_WORKDIR },
);
await tools.cps_active_routes.execute("call-1", { limit: 1 });
const afterInvalid = checkWrite();
await tools.cps_claim_route.execute(
  "call-2",
  { route_key: "same-route", summary: "primary" },
);
const afterClaim = checkWrite();
await tools.cps_claim_route.execute(
  "call-3",
  { route_key: "same-route", summary: "retry" },
);
const afterNegative = checkWrite();
console.log(JSON.stringify({
  invalidBlocked: afterInvalid?.block === true,
  claimBlocked: afterClaim?.block === true,
  negativeBlocked: afterNegative?.block === true,
}));
"""
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ | {
                "CONTEXTSWARM_CPS_ROUTE_CLAIM_REQUIRED": "1",
                "CONTEXTSWARM_CPS_ROUTE_CLAIMS_ENABLED": "1",
                "CONTEXTSWARM_TASK_ID": "task",
                "CONTEXTSWARM_ACTOR_ID": "actor",
                "CONTEXTSWARM_EPISODE": "1",
                "CONTEXTSWARM_WORKDIR": temporary,
                "CONTEXTSWARM_JUDGE_URL": (
                    "http://127.0.0.1:12345/"
                    "abcdefghijklmnopqrstuvwxyzABCDEFGH_12345678"
                ),
                "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "32503680000000",
            }
            result = subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "--eval",
                    script,
                    str(ROOT / "contextswarm_mini/pi_solver_tools.mjs"),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
        payload = json.loads(result.stdout)
        self.assertTrue(payload["invalidBlocked"])
        self.assertFalse(payload["claimBlocked"])
        self.assertTrue(payload["negativeBlocked"])


if __name__ == "__main__":
    unittest.main()
