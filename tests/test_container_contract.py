from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContainerContractTests(unittest.TestCase):
    def _launcher_environment(self, temporary: Path) -> dict[str, str]:
        source_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        fake_docker = temporary / "docker"
        fake_docker.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = image ] && [ \"${2:-}\" = inspect ]; then\n"
            "  image_reference=''\n"
            "  for argument in \"$@\"; do image_reference=\"$argument\"; done\n"
            "  case \"$*\" in\n"
            "    *org.opencontainers.image.revision*)\n"
            "      if [ -n \"${FAKE_IMAGE_REVISION_ONLY_FOR:-}\" ] && [ \"$image_reference\" != \"$FAKE_IMAGE_REVISION_ONLY_FOR\" ]; then\n"
            "        printf '%s\\n' unknown\n"
            "      else\n"
            f"        printf '%s\\n' \"${{FAKE_IMAGE_REVISION:-{source_revision}}}\"\n"
            "      fi\n"
            "      ;;\n"
            "    *) printf '%s\\n' \"${FAKE_IMAGE_ID:-sha256:0000000000000000000000000000000000000000000000000000000000000000}\" ;;\n"
            "  esac\n"
            "  exit 0\n"
            "fi\n"
            "for argument in \"$@\"; do\n"
            "  printf '<%s>\\n' \"$argument\"\n"
            "done\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)

        fake_aisw = temporary / "nurouter"
        fake_aisw.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = --version ]; then printf '%s\\n' test-release; fi\n",
            encoding="utf-8",
        )
        fake_aisw.chmod(0o755)
        node_config = temporary / "node.toml"
        node_config.write_text("real_pi = '/unused'\n", encoding="utf-8")

        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{temporary}{os.pathsep}{env.get('PATH', '')}",
                "CONTEXTSWARM_NUROUTER_BINARY": str(fake_aisw),
                "CONTEXTSWARM_NUROUTER_NODE_CONFIG": str(node_config),
                "CONTEXTSWARM_MINI_RUN_UID": "12345",
                "CONTEXTSWARM_MINI_RUN_GID": "12345",
            }
        )
        env.pop("CONTEXTSWARM_JUDGE_URL", None)
        env.pop("CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL", None)
        env.pop("LEAN_AUTH_TOKEN", None)
        return env

    def _run_launcher(
        self,
        temporary: Path,
        *arguments: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "scripts" / "run_docker.sh"), *arguments],
            cwd=ROOT,
            env=env or self._launcher_environment(temporary),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_mock_launch_uses_read_only_non_root_limits(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temporary:
            temporary = Path(raw_temporary)
            image_id = "sha256:" + "4" * 64
            env = self._launcher_environment(temporary)
            env["FAKE_IMAGE_ID"] = image_id
            env["FAKE_IMAGE_REVISION_ONLY_FOR"] = image_id
            completed = self._run_launcher(
                temporary,
                "--config",
                "configs/smoke.toml",
                "--mock-agent",
                env=env,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        arguments = completed.stdout.splitlines()
        self.assertIn("<--read-only>", arguments)
        self.assertIn("<--pids-limit>", arguments)
        self.assertIn("<--cap-drop>", arguments)
        self.assertIn("<--security-opt>", arguments)
        self.assertIn("<no-new-privileges=true>", arguments)
        self.assertIn("<--user>", arguments)
        self.assertIn("<12345:12345>", arguments)
        self.assertTrue(any(value.startswith("</run:rw,nosuid,nodev,exec,") for value in arguments))
        self.assertTrue(any(value.startswith("</tmp:rw,nosuid,nodev,noexec,") for value in arguments))
        self.assertNotIn(f"<{ROOT}:/opt/contextswarm:ro>", arguments)
        self.assertIn(f"<{ROOT / 'runs'}:/opt/contextswarm/runs>", arguments)
        self.assertNotIn("<CONTEXTSWARM_JUDGE_URL>", arguments)
        self.assertIn(f"<CONTEXTSWARM_IMAGE_ID={image_id}>", arguments)
        self.assertTrue(
            any(value.startswith("<CONTEXTSWARM_IMAGE_REVISION=") for value in arguments)
        )
        self.assertIn(f"<{image_id}>", arguments)
        self.assertNotIn("<contextswarm-iclr-mini:latest>", arguments)

    def test_real_launch_requires_judge_and_passes_only_variable_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temporary:
            temporary = Path(raw_temporary)
            env = self._launcher_environment(temporary)
            missing = self._run_launcher(
                temporary,
                "--config",
                "configs/canary.toml",
                env=env,
            )
            self.assertEqual(missing.returncode, 2)
            self.assertIn("CONTEXTSWARM_JUDGE_URL must be set", missing.stderr)
            self.assertEqual(missing.stdout, "")

            private_value = "https:" + "//judge.invalid/operator-private-path"
            private_cache_value = (
                "https:" + "//cache-health.invalid/operator-private-path"
            )
            private_token = "operator-private-auth-marker"
            env["CONTEXTSWARM_JUDGE_URL"] = private_value
            env["LEAN_AUTH_TOKEN"] = private_token
            missing_cache = self._run_launcher(
                temporary,
                "--config",
                "configs/canary.toml",
                env=env,
            )
            self.assertEqual(missing_cache.returncode, 2)
            self.assertIn(
                "CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL must be set",
                missing_cache.stderr,
            )
            self.assertNotIn(private_value, missing_cache.stderr)
            self.assertNotIn(private_token, missing_cache.stderr)

            env["CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL"] = private_cache_value
            completed = self._run_launcher(
                temporary,
                "--config",
                "configs/canary.toml",
                env=env,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn(private_value, completed.stdout)
        self.assertNotIn(private_cache_value, completed.stdout)
        self.assertNotIn(private_token, completed.stdout)
        self.assertNotIn(private_cache_value, completed.stderr)
        self.assertNotIn(private_token, completed.stderr)
        arguments = completed.stdout.splitlines()
        judge_index = arguments.index("<CONTEXTSWARM_JUDGE_URL>")
        self.assertEqual(arguments[judge_index - 1], "<-e>")
        cache_index = arguments.index("<CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL>")
        self.assertEqual(arguments[cache_index - 1], "<-e>")
        token_index = arguments.index("<LEAN_AUTH_TOKEN>")
        self.assertEqual(arguments[token_index - 1], "<-e>")

    def test_real_launch_xtrace_never_expands_private_judge_value(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temporary:
            temporary = Path(raw_temporary)
            env = self._launcher_environment(temporary)
            private_value = "https:" + "//judge.invalid/operator-private-xtrace-marker"
            private_cache_value = (
                "https:" + "//cache-health.invalid/operator-private-xtrace-marker"
            )
            private_token = "operator-private-auth-xtrace-marker"
            env["CONTEXTSWARM_JUDGE_URL"] = private_value
            env["CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL"] = private_cache_value
            env["LEAN_AUTH_TOKEN"] = private_token
            trace_path = temporary / "launcher-xtrace.log"
            with trace_path.open("w+", encoding="utf-8") as trace:
                env["BASH_XTRACEFD"] = str(trace.fileno())
                env["PS4"] = "+ contextswarm-launcher-test "
                completed = subprocess.run(
                    [
                        "bash",
                        "-x",
                        str(ROOT / "scripts" / "run_docker.sh"),
                        "--config",
                        "configs/canary.toml",
                    ],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    pass_fds=(trace.fileno(),),
                )
                trace.seek(0)
                xtrace = trace.read()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn(private_value, completed.stdout)
        self.assertNotIn(private_value, completed.stderr)
        self.assertNotIn(private_value, xtrace)
        self.assertNotIn(private_cache_value, completed.stdout)
        self.assertNotIn(private_cache_value, completed.stderr)
        self.assertNotIn(private_cache_value, xtrace)
        self.assertNotIn(private_token, completed.stdout)
        self.assertNotIn(private_token, completed.stderr)
        self.assertNotIn(private_token, xtrace)
        arguments = completed.stdout.splitlines()
        judge_index = arguments.index("<CONTEXTSWARM_JUDGE_URL>")
        self.assertEqual(arguments[judge_index - 1], "<-e>")
        cache_index = arguments.index("<CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL>")
        self.assertEqual(arguments[cache_index - 1], "<-e>")
        token_index = arguments.index("<LEAN_AUTH_TOKEN>")
        self.assertEqual(arguments[token_index - 1], "<-e>")

    def test_dry_run_does_not_require_judge_capability(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temporary:
            temporary = Path(raw_temporary)
            env = self._launcher_environment(temporary)
            completed = self._run_launcher(
                temporary,
                "--config",
                "configs/smoke.toml",
                "--dry-run",
                env=env,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        arguments = completed.stdout.splitlines()
        self.assertIn("<--dry-run>", arguments)
        self.assertNotIn("<CONTEXTSWARM_JUDGE_URL>", arguments)

    def test_launch_rejects_unbound_image_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temporary:
            temporary = Path(raw_temporary)
            env = self._launcher_environment(temporary)
            env["FAKE_IMAGE_REVISION"] = "unknown"
            completed = self._run_launcher(
                temporary,
                "--config",
                "configs/smoke.toml",
                "--mock-agent",
                env=env,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("source revision label", completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_codex_home_is_mounted_outside_root_home(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temporary:
            temporary = Path(raw_temporary)
            codex_home = temporary / "codex-home"
            codex_home.mkdir()
            env = self._launcher_environment(temporary)
            env["CONTEXTSWARM_JUDGE_URL"] = "https:" + "//judge.invalid/base"
            env["CONTEXTSWARM_CODEX_HOME"] = str(codex_home)
            completed = self._run_launcher(
                temporary,
                "--config",
                "configs/smoke.toml",
                env=env,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("/root/.codex", completed.stdout)
        self.assertIn(
            f"<{codex_home}:/opt/contextswarm-input/codex-home:ro>",
            completed.stdout.splitlines(),
        )

    def test_tracked_manifests_do_not_contain_judge_endpoints(self) -> None:
        for manifest in (ROOT / "configs").glob("*.toml"):
            with self.subTest(manifest=manifest.name):
                self.assertNotIn("server_url", manifest.read_text(encoding="utf-8"))

    def test_formal_image_build_is_clean_and_commit_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temporary:
            temporary = Path(raw_temporary)
            repository = temporary / "repo"
            scripts = repository / "scripts"
            scripts.mkdir(parents=True)
            build_script = scripts / "build_image.sh"
            build_script.write_bytes((ROOT / "scripts" / "build_image.sh").read_bytes())
            build_script.chmod(0o755)
            (repository / "tracked.txt").write_text("frozen\n", encoding="utf-8")
            (repository / ".gitignore").write_text("operator-private.txt\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=ContextSwarm Test",
                    "-c",
                    "user.email=contextswarm-test@example.invalid",
                    "commit",
                    "-qm",
                    "frozen source",
                ],
                cwd=repository,
                check=True,
            )
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            fake_bin = temporary / "bin"
            fake_bin.mkdir()
            fake_docker = fake_bin / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                "for argument in \"$@\"; do context=\"$argument\"; printf '<%s>\\n' \"$argument\"; done\n"
                "test -f \"$context/tracked.txt\" || exit 41\n"
                "test ! -e \"$context/operator-private.txt\" || exit 42\n"
                "test ! -e \"$context/.git\" || exit 43\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o755)
            (repository / "operator-private.txt").write_text(
                "must not enter image context\n", encoding="utf-8"
            )
            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

            clean = subprocess.run(
                [str(build_script)],
                cwd=repository,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertIn(
                f"<CONTEXTSWARM_SOURCE_COMMIT={source_commit}>",
                clean.stdout.splitlines(),
            )
            self.assertIn(
                f"<org.opencontainers.image.revision={source_commit}>",
                clean.stdout.splitlines(),
            )

            (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            dirty = subprocess.run(
                [str(build_script)],
                cwd=repository,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(dirty.returncode, 2)
            self.assertIn("dirty worktree", dirty.stderr)
            self.assertEqual(dirty.stdout, "")

    def test_shell_entrypoints_parse(self) -> None:
        for script in (
            ROOT / "docker-entrypoint.sh",
            ROOT / "scripts" / "build_image.sh",
            ROOT / "scripts" / "run_docker.sh",
        ):
            with self.subTest(script=script.name):
                subprocess.run(["bash", "-n", str(script)], check=True)


if __name__ == "__main__":
    unittest.main()
