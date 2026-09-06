"""Focused contract tests for selection-mode CPS capability gating."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import unittest

from contextswarm_mini.config import load_config
from contextswarm_mini.models import Task
from contextswarm_mini.pi_agent import PiAgent
from contextswarm_mini.prompts import build_task_prompt


ROOT = Path(__file__).resolve().parents[1]
_DIRECT = {"cps_inbox", "cps_send", "cps_ack", "cps_actors"}


def _registered_tools(
    *, direct_messages: bool, selection_enabled: bool, global_scope: bool = False
) -> dict[str, dict]:
    script = """
const { default: register } = await import(process.argv[1]);
const tools = {};
register({ registerTool(definition) { tools[definition.name] = definition; }, on() {} });
console.log(JSON.stringify(tools));
"""
    environment = os.environ | {
        "CONTEXTSWARM_CPS_DIRECT_MESSAGES": "1" if direct_messages else "0",
        "CONTEXTSWARM_CPS_SELECTION_ENABLED": "1" if selection_enabled else "0",
        "CONTEXTSWARM_CPS_GLOBAL_SCOPE": "1" if global_scope else "0",
    }
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script, str(ROOT / "contextswarm_mini/pi_solver_tools.mjs")],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(result.stdout)


class ToolCapabilityGateTests(unittest.TestCase):
    def test_selection_surface_has_shared_and_feedback_but_no_direct_messages(self) -> None:
        tools = _registered_tools(direct_messages=False, selection_enabled=True)
        self.assertTrue({"cps_search", "cps_publish", "cps_feedback"} <= tools.keys())
        self.assertFalse(_DIRECT & tools.keys())
        feedback = tools["cps_feedback"]
        self.assertIn("previously exposed", feedback["description"])
        self.assertEqual(
            feedback["parameters"]["required"],
            ["request_key", "exposure_item_id", "trace_id", "feedback_kind"],
        )
        self.assertEqual(
            set(feedback["parameters"]["properties"]),
            {"request_key", "exposure_item_id", "trace_id", "feedback_kind", "value", "note"},
        )

    def test_legacy_defaults_keep_the_existing_cps_surface(self) -> None:
        tools = _registered_tools(direct_messages=True, selection_enabled=False)
        self.assertTrue({"cps_search", "cps_publish"} | _DIRECT <= tools.keys())
        self.assertNotIn("cps_feedback", tools)
        self.assertNotIn(
            "scope", tools["cps_publish"]["parameters"]["properties"]
        )
        self.assertNotIn("scope", tools["cps_send"]["parameters"]["properties"])

    def test_hybrid_surface_is_the_only_worker_surface_with_global_scope(self) -> None:
        tools = _registered_tools(
            direct_messages=True,
            selection_enabled=False,
            global_scope=True,
        )
        for name in ("cps_publish", "cps_send"):
            scope = tools[name]["parameters"]["properties"]["scope"]
            self.assertEqual(scope["enum"], ["task", "global"])

        selection_tools = _registered_tools(
            direct_messages=False,
            selection_enabled=True,
            global_scope=True,
        )
        self.assertNotIn(
            "scope", selection_tools["cps_publish"]["parameters"]["properties"]
        )

    def test_runner_facing_allowlist_and_prompt_match_selection_surface(self) -> None:
        agent = PiAgent(load_config("configs/smoke.toml", ROOT))
        tools = set(agent.solver_tools(direct_messages=False, selection_enabled=True))
        self.assertTrue({"cps_search", "cps_publish", "cps_feedback"} <= tools)
        self.assertFalse(_DIRECT & tools)

        task = Task("sample", Path("benchmarks/sample"), "example", "theorem example : True := by sorry", {})
        prompt = build_task_prompt(
            task,
            task_workspace="tasks/sample",
            agent_id="worker-sample-e1",
            episode=1,
            communication_enabled=True,
            direct_messages=False,
            selection_enabled=True,
        )
        self.assertIn("cps_search", prompt)
        self.assertIn("cps_publish", prompt)
        self.assertIn("cps_feedback", prompt)
        for name in _DIRECT:
            self.assertNotIn(name, prompt)
