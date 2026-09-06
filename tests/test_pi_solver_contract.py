from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from contextswarm_mini.config import load_config
from contextswarm_mini.pi_agent import PiAgent


ROOT = Path(__file__).resolve().parents[1]


def _tool_allowlist(command: list[str]) -> set[str]:
    index = command.index("--tools")
    return set(command[index + 1].split(","))


class PiSolverContractTests(unittest.TestCase):
    def test_host_judge_url_is_injected_but_never_serialized(self) -> None:
        with patch.dict(
            os.environ,
            {"CONTEXTSWARM_JUDGE_URL": "https://private.example:9443/api/lean/jobs"},
            clear=False,
        ):
            config = load_config("configs/smoke.toml", ROOT)
        self.assertEqual(
            config.lean_server_url,
            "https://private.example:9443/api/lean/jobs",
        )
        public = config.public_dict()
        self.assertTrue(public["lean_server_configured"])
        self.assertNotIn("lean_server_url", public)

    def test_manifest_without_runtime_judge_has_no_endpoint_fallback(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config("configs/smoke.toml", ROOT)
        self.assertEqual(config.lean_server_url, "")
        self.assertFalse(config.public_dict()["lean_server_configured"])

    def test_cps_solver_has_controlled_formal_tools(self) -> None:
        command = PiAgent(load_config("configs/smoke.toml", ROOT)).command()
        for flag in (
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
            "--no-extensions",
            "--tools",
        ):
            self.assertIn(flag, command)
        tools = _tool_allowlist(command)
        self.assertIn("bash", tools)
        system_prompt = command[command.index("--system-prompt") + 1]
        self.assertIn("not a general-purpose coding agent", system_prompt)
        self.assertIn("Do not execute shell commands", system_prompt)
        self.assertIn("bounded helper commands", system_prompt)
        self.assertIn("loopback capability", system_prompt)
        self.assertIn("controlled\nJudge already owns the Lean/Mathlib toolchain", system_prompt)
        self.assertIn("downloads, compilation, tests, and\nverification", system_prompt)
        self.assertIn("CONTEXTSWARM_JUDGE_URL value is injected by the runner", system_prompt)
        self.assertIn("session-scoped capability", system_prompt)
        self.assertIn("never reproduce them in the worker container", system_prompt)
        self.assertIn("mandatory early judge_check checkpoint", system_prompt)
        self.assertIn("untrusted problem data", system_prompt)
        self.assertIn("never\noverride this", system_prompt)
        self.assertIn("Independent proof construction does not ban Lean tactics", system_prompt)
        self.assertIn("bounded `find`/`exact`/`apply` searches", system_prompt)
        self.assertEqual(
            {
                "judge_check",
                "cps_search",
                "cps_publish",
                "cps_inbox",
                "cps_send",
                "cps_ack",
                "cps_actors",
            }
            - tools,
            set(),
        )
        extensions = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--extension"
        ]
        self.assertTrue(any(path.endswith("pi_solver_tools.mjs") for path in extensions))

    def test_parallel_solver_has_no_cps_capability_and_has_formal_tools(self) -> None:
        command = PiAgent(load_config("configs/parallel.toml", ROOT)).command()
        tools = _tool_allowlist(command)
        self.assertIn("judge_check", tools)
        self.assertFalse(any(name.startswith("cps_") for name in tools))
        self.assertIn("bash", tools)

    def test_coding_solver_requires_independent_careful_answers(self) -> None:
        config = replace(
            load_config("configs/smoke.toml", ROOT),
            judge_kind="coding",
            formal_tools_enabled=False,
            aisw_enabled=False,
        )
        command = PiAgent(config).command()
        system_prompt = command[command.index("--system-prompt") + 1]
        self.assertIn("Solve the task independently and answer carefully", system_prompt)
        self.assertIn("Rely on your own reasoning", system_prompt)
        self.assertIn("do not copy or\ntrust externally sourced solutions", system_prompt)

    def test_termination_closeout_system_exception_is_treatment_only(self) -> None:
        agent = PiAgent(load_config("configs/smoke.toml", ROOT))
        baseline = agent.command()
        treatment = agent.command(termination_summary_enabled=True)
        baseline_prompt = baseline[baseline.index("--system-prompt") + 1]
        treatment_prompt = treatment[treatment.index("--system-prompt") + 1]
        self.assertNotIn("RUNNER-REQUESTED TERMINATION CLOSEOUT", baseline_prompt)
        self.assertIn("RUNNER-REQUESTED TERMINATION CLOSEOUT", treatment_prompt)
        self.assertTrue(treatment_prompt.startswith(baseline_prompt + "\n"))

    def test_isolated_scheduler_has_no_tools_and_a_read_only_system_prompt(self) -> None:
        command = PiAgent(load_config("configs/smoke.toml", ROOT)).command(isolated=True)
        self.assertIn("--no-tools", command)
        self.assertNotIn("--tools", command)
        self.assertNotIn("--extension", command)
        system_prompt = command[command.index("--system-prompt") + 1]
        self.assertIn("read-only allocation decision component", system_prompt)
        self.assertIn("You have no tools", system_prompt)
        self.assertIn("must not", system_prompt)

    def test_solver_environment_drops_raw_judge_secret_and_endpoint(self) -> None:
        agent = PiAgent(load_config("configs/smoke.toml", ROOT))
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "LEAN_AUTH_TOKEN": "raw-secret",
                "CONTEXTSWARM_JUDGE_URL": "http://raw-judge.invalid",
                "CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL": "https://cache-health.invalid",
                "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "1",
            },
            clear=False,
        ):
            raw_env = agent.environment(
                task_id="task",
                actor_id="actor",
                workdir=Path(temporary),
            )
            broker_env = agent.environment(
                task_id="task",
                actor_id="actor",
                workdir=Path(temporary),
                extra_env={
                    "CONTEXTSWARM_JUDGE_URL": (
                        "http://127.0.0.1:1234/"
                        "abcdefghijklmnopqrstuvwxyzABCDEFGH_12345678"
                    ),
                    "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "2000000000000",
                },
            )
            tmp_mode = (Path(temporary) / ".tmp").stat().st_mode & 0o777
        self.assertNotIn("LEAN_AUTH_TOKEN", raw_env)
        self.assertNotIn("CONTEXTSWARM_JUDGE_URL", raw_env)
        self.assertNotIn("CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL", raw_env)
        self.assertNotIn("CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS", raw_env)
        self.assertNotIn("LEAN_AUTH_TOKEN", broker_env)
        self.assertNotIn("CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL", broker_env)
        self.assertEqual(
            broker_env["CONTEXTSWARM_JUDGE_URL"],
            "http://127.0.0.1:1234/abcdefghijklmnopqrstuvwxyzABCDEFGH_12345678",
        )
        self.assertEqual(
            broker_env["CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS"],
            "2000000000000",
        )
        self.assertEqual(raw_env["TMPDIR"], str(Path(temporary) / ".tmp"))
        self.assertEqual(tmp_mode, 0o700)
        self.assertEqual(raw_env["CONTEXTSWARM_CPS_GLOBAL_SCOPE"], "0")
        self.assertEqual(broker_env["CONTEXTSWARM_CPS_GLOBAL_SCOPE"], "0")

    def test_hybrid_environment_grants_global_scope_capability(self) -> None:
        config = load_config("configs/cps_hybrid.toml", ROOT)
        agent = PiAgent(config)
        with tempfile.TemporaryDirectory() as temporary:
            env = agent.environment(
                task_id="task",
                actor_id="actor",
                workdir=Path(temporary),
            )
        self.assertEqual(env["CONTEXTSWARM_CPS_GLOBAL_SCOPE"], "1")

    def test_solver_environment_rejects_unbound_or_raw_judge_capabilities(self) -> None:
        agent = PiAgent(load_config("configs/smoke.toml", ROOT))
        token = "abcdefghijklmnopqrstuvwxyzABCDEFGH_12345678"
        invalid = (
            {
                "CONTEXTSWARM_JUDGE_URL": f"http://127.0.0.1:1234/{token}",
                "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "2000000000000",
                "LEAN_AUTH_TOKEN": "must-not-reenter",
            },
            {
                "CONTEXTSWARM_JUDGE_URL": f"https://judge.invalid/{token}",
                "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "2000000000000",
            },
            {
                "CONTEXTSWARM_JUDGE_URL": "http://127.0.0.1:1234/short-token",
                "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "2000000000000",
            },
            {
                "CONTEXTSWARM_JUDGE_URL": f"http://127.0.0.1:1234/{token}",
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            for extra_env in invalid:
                with self.subTest(keys=sorted(extra_env)), self.assertRaises(ValueError):
                    agent.environment(
                        task_id="task",
                        actor_id="actor",
                        workdir=Path(temporary),
                        extra_env=extra_env,
                    )

    def test_fast_mode_accepts_only_hash_declared_bundled_extensions(self) -> None:
        config = load_config("configs/cps_fast.toml", ROOT)
        agent = PiAgent(config)
        declaration = agent.trusted_extension_declaration()
        rows = declaration["extensions"]
        self.assertEqual(declaration["policy"], "bundled_explicit_only")
        self.assertTrue(declaration["discovery_disabled"])
        self.assertEqual(
            [row["name"] for row in rows],
            ["pi_solver_tools.mjs", "pi_fast_mode.mjs"],
        )
        self.assertTrue(
            all(
                isinstance(row["sha256"], str) and len(row["sha256"]) == 64
                for row in rows
            )
        )
        command = agent.command()
        extensions = [
            Path(command[index + 1]).name
            for index, value in enumerate(command[:-1])
            if value == "--extension"
        ]
        self.assertEqual(extensions, ["pi_solver_tools.mjs", "pi_fast_mode.mjs"])

        with tempfile.TemporaryDirectory() as temporary:
            arbitrary = Path(temporary) / "arbitrary.mjs"
            arbitrary.write_text("export default function () {}\n", encoding="utf-8")
            untrusted = replace(config, pi_extension=str(arbitrary))
            with self.assertRaisesRegex(ValueError, "non-bundled"):
                PiAgent(untrusted).command()

    def test_local_pi_0842_loads_explicit_solver_extension_offline(self) -> None:
        pi_binary = shutil.which("pi")
        if not pi_binary:
            self.skipTest("local Pi is not installed")
        # The host development environment may resolve ``pi`` to the managed
        # NuRouter launcher (an ELF wrapper), not the Node-based Pi 0.84.2
        # executable shipped in the experiment image.  Its private node.toml
        # contract cannot be exercised with the test's isolated HOME, so this
        # probe is intentionally Docker-only in that environment.
        try:
            is_elf = Path(pi_binary).resolve().read_bytes()[:4] == b"\x7fELF"
        except OSError:
            is_elf = False
        if is_elf:
            self.skipTest("pi resolves to the managed NuRouter launcher")
        if not any(
            Path(candidate).is_file() and os.access(candidate, os.X_OK)
            for candidate in ("/usr/local/bin/node", "/usr/bin/node", "/bin/node")
        ):
            self.skipTest("controlled Node.js runtime is unavailable on this host")
        version = subprocess.run(
            [pi_binary, "--version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        if version.returncode != 0 or version.stdout.strip() != "0.84.2":
            self.skipTest("local Pi 0.84.2 is unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            config = replace(
                load_config("configs/smoke.toml", ROOT),
                pi_binary=pi_binary,
                aisw_enabled=False,
                model="",
            )
            agent = PiAgent(config)
            command = [*agent.command(), "--offline", "--no-session"]
            env = agent.environment(
                task_id="contract-probe",
                actor_id="contract-probe",
                workdir=workdir,
                extra_env={
                    "CONTEXTSWARM_JUDGE_URL": (
                        "http://127.0.0.1:9/"
                        "abcdefghijklmnopqrstuvwxyzABCDEFGH_12345678"
                    ),
                    "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS": "2000000000000",
                },
            )
            env["PI_OFFLINE"] = "1"
            env["PI_CODING_AGENT_DIR"] = str(workdir / ".pi-contract")
            completed = subprocess.run(
                command,
                cwd=workdir,
                env=env,
                input=json.dumps({"id": "probe", "type": "get_state"}) + "\n",
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
        self.assertEqual(completed.returncode, 0)
        rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(
            any(
                row.get("id") == "probe"
                and row.get("command") == "get_state"
                and row.get("success") is True
                for row in rows
            )
        )
        self.assertNotIn("extension", completed.stderr.lower())

    def test_solver_extension_path_guard_blocks_out_of_workspace_operations(self) -> None:
        node_binary = shutil.which("node")
        if not node_binary:
            self.skipTest("Node.js is unavailable")
        harness = r"""
import { pathToFileURL } from "node:url";
const [extensionPath, workdir, outsideFile, outsideDir] = process.argv.slice(1);
const listeners = new Map();
const registered = [];
const pi = {
  on(name, callback) { listeners.set(name, callback); },
  registerTool(definition) { registered.push(definition.name); },
};
const extension = await import(pathToFileURL(extensionPath).href);
extension.default(pi);
const guard = listeners.get("tool_call");
if (typeof guard !== "function") throw new Error("tool_call guard was not registered");
async function invoke(toolName, input) {
  return await guard({ toolName, input }, { cwd: workdir });
}
const denied = {
  read: (await invoke("read", { path: outsideFile }))?.block === true,
  write: (await invoke("write", { path: outsideFile, content: "x" }))?.block === true,
  find: (await invoke("find", { path: outsideDir, pattern: "*.lean" }))?.block === true,
  grep: (await invoke("grep", { path: outsideFile, pattern: "x" }))?.block === true,
};
const allowed = {
  read: (await invoke("read", { path: "problem.md" })) === undefined,
  write: (await invoke("write", { path: "result.lean", content: "proof" })) === undefined,
  find: (await invoke("find", { path: "baseline", pattern: "*.lean" })) === undefined,
  grep: (await invoke("grep", { path: "problem.md", pattern: "theorem" })) === undefined,
};
process.stdout.write(JSON.stringify({ registered: registered.sort(), denied, allowed }));
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "workdir"
            baseline = workdir / "baseline"
            outside_dir = root / "outside"
            baseline.mkdir(parents=True)
            outside_dir.mkdir()
            (workdir / "problem.md").write_text("theorem statement\n", encoding="utf-8")
            (workdir / "result.lean").write_text("proof\n", encoding="utf-8")
            (baseline / "task.lean").write_text("baseline\n", encoding="utf-8")
            outside_file = outside_dir / "private.txt"
            outside_file.write_text("private\n", encoding="utf-8")
            env = dict(os.environ)
            env["CONTEXTSWARM_WORKDIR"] = str(workdir)
            completed = subprocess.run(
                [
                    node_binary,
                    "--input-type=module",
                    "-e",
                    harness,
                    str(ROOT / "contextswarm_mini" / "pi_solver_tools.mjs"),
                    str(workdir),
                    str(outside_file),
                    str(outside_dir),
                ],
                cwd=workdir,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(
            set(result["registered"]),
            {
                "judge_check",
                "cps_search",
                "cps_publish",
                "cps_inbox",
                "cps_send",
                "cps_ack",
                "cps_actors",
            },
        )
        self.assertTrue(all(result["denied"].values()))
        self.assertTrue(all(result["allowed"].values()))

    def test_solver_extension_write_guard_rejects_symlink_targets_and_parents(self) -> None:
        node_binary = shutil.which("node")
        if not node_binary:
            self.skipTest("Node.js is unavailable")
        harness = r"""
import { pathToFileURL } from "node:url";
const [extensionPath, workdir, symlinkFile, symlinkParent] = process.argv.slice(1);
const listeners = new Map();
const pi = {
  on(name, callback) { listeners.set(name, callback); },
  registerTool(_definition) {},
};
const extension = await import(pathToFileURL(extensionPath).href);
extension.default(pi);
const guard = listeners.get("tool_call");
if (typeof guard !== "function") throw new Error("tool_call guard was not registered");
async function denied(toolName, path) {
  return (await guard({ toolName, input: { path, content: "x" } }, { cwd: workdir }))?.block === true;
}
const result = {
  final_write: await denied("write", symlinkFile),
  final_edit: await denied("edit", symlinkFile),
  parent_write: await denied("write", symlinkParent),
  parent_edit: await denied("edit", symlinkParent),
};
process.stdout.write(JSON.stringify(result));
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "workdir"
            workdir.mkdir()
            outside_file = root / "outside.txt"
            outside_file.write_text("private\n", encoding="utf-8")
            symlink_file = workdir / "result.lean"
            symlink_file.symlink_to(outside_file)
            tasks = workdir / "tasks"
            tasks.mkdir()
            outside_task = root / "outside-task"
            outside_task.mkdir()
            (outside_task / "result.lean").write_text("private\n", encoding="utf-8")
            symlink_task = tasks / "link"
            symlink_task.symlink_to(outside_task, target_is_directory=True)
            env = dict(os.environ)
            env["CONTEXTSWARM_WORKDIR"] = str(workdir)
            completed = subprocess.run(
                [
                    node_binary,
                    "--input-type=module",
                    "-e",
                    harness,
                    str(ROOT / "contextswarm_mini" / "pi_solver_tools.mjs"),
                    str(workdir),
                    str(symlink_file),
                    str(symlink_task / "result.lean"),
                ],
                cwd=workdir,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(all(json.loads(completed.stdout).values()))

    def test_solver_extension_write_guard_rejects_non_enoent_final_lookup(self) -> None:
        """A final lstat failure other than missing result.lean is not writable."""
        node_binary = shutil.which("node")
        if not node_binary:
            self.skipTest("Node.js is unavailable")
        harness = r"""
import { pathToFileURL } from "node:url";
const [extensionPath, workdir] = process.argv.slice(1);
const listeners = new Map();
const pi = {
  on(name, callback) { listeners.set(name, callback); },
  registerTool(_definition) {},
};
const extension = await import(pathToFileURL(extensionPath).href);
extension.default(pi);
const guard = listeners.get("tool_call");
if (typeof guard !== "function") throw new Error("tool_call guard was not registered");
const result = await guard(
  { toolName: "write", input: { path: "tasks/blocked/result.lean", content: "x" } },
  { cwd: workdir },
);
process.stdout.write(JSON.stringify({ blocked: result?.block === true }));
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workdir = root / "workdir"
            workdir.mkdir()
            tasks = workdir / "tasks"
            tasks.mkdir()
            # lstat(tasks/blocked/result.lean) raises ENOTDIR: the final
            # component is not safely absent, since its parent is a file.
            (tasks / "blocked").write_text("not a directory\n", encoding="utf-8")
            env = dict(os.environ)
            env["CONTEXTSWARM_WORKDIR"] = str(workdir)
            completed = subprocess.run(
                [
                    node_binary,
                    "--input-type=module",
                    "-e",
                    harness,
                    str(ROOT / "contextswarm_mini" / "pi_solver_tools.mjs"),
                    str(workdir),
                ],
                cwd=workdir,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["blocked"])

    def test_solver_broker_client_uses_runner_deadline_not_fixed_310_seconds(self) -> None:
        source = (ROOT / "contextswarm_mini" / "pi_solver_tools.mjs").read_text(
            encoding="utf-8"
        )
        self.assertIn("CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS", source)
        self.assertNotIn("310_000", source)


if __name__ == "__main__":
    unittest.main()
