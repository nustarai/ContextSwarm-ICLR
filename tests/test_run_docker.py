from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUN_DOCKER = ROOT / "scripts" / "run_docker.sh"


class RunDockerManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".test-run-docker-",
            dir=ROOT,
        )
        self.temp = Path(self.temporary.name)
        self.bin_dir = self.temp / "bin"
        self.bin_dir.mkdir()
        self.capture = self.temp / "docker-argv.json"

        fake_docker = self.bin_dir / "docker"
        fake_docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['FAKE_DOCKER_CAPTURE']).write_text(\n"
            "    json.dumps(sys.argv[1:]), encoding='utf-8'\n"
            ")\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)

        self.fake_nurouter = self.bin_dir / "fake-nurouter"
        self.fake_nurouter.write_text(
            "#!/usr/bin/env sh\nprintf '%s\\n' 'fake-nurouter 1.0'\n",
            encoding="utf-8",
        )
        self.fake_nurouter.chmod(0o755)

        self.parent_manifest = self.temp / "parent.toml"
        self.child_manifest = self.temp / "child.toml"
        self._write_parent_manifest(
            image="research/contextswarm-mini:paper",
            memory_mb=65_536,
        )
        self.child_manifest.write_text(
            'extends = ["parent.toml"]\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_parent_manifest(
        self,
        *,
        image: str,
        memory_mb: int,
        network: str = "host",
    ) -> None:
        self.parent_manifest.write_text(
            'extends = ["../configs/smoke.toml"]\n\n'
            "[docker]\n"
            f'image = "{image}"\n'
            f"memory_mb = {memory_mb}\n"
            f'network = "{network}"\n',
            encoding="utf-8",
        )

    def _run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        if self.capture.exists():
            self.capture.unlink()
        env = os.environ.copy()
        env.pop("CONTEXTSWARM_MINI_IMAGE", None)
        env.pop("CONTEXTSWARM_MINI_MEMORY", None)
        env.update(overrides)
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "FAKE_DOCKER_CAPTURE": str(self.capture),
                "CONTEXTSWARM_NUROUTER_BINARY": str(self.fake_nurouter),
            }
        )
        return subprocess.run(
            [
                "/bin/bash",
                str(RUN_DOCKER),
                "--config",
                str(self.child_manifest.relative_to(ROOT)),
                "--mock-agent",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def _captured_argv(self) -> list[str]:
        return json.loads(self.capture.read_text(encoding="utf-8"))

    def test_inherited_manifest_resources_reach_actual_docker_argv(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self._captured_argv()
        self.assertEqual(argv[0], "run")
        self.assertEqual(argv[argv.index("--memory") + 1], "65536m")
        self.assertEqual(argv[argv.index("--network") + 1], "host")
        self.assertNotIn("--add-host", argv)
        config_index = argv.index("--config")
        self.assertEqual(argv[config_index - 1], "research/contextswarm-mini:paper")
        self.assertEqual(
            argv[config_index + 1],
            str(self.child_manifest.relative_to(ROOT)),
        )

    def test_operator_environment_overrides_manifest_resources(self) -> None:
        result = self._run(
            CONTEXTSWARM_MINI_IMAGE="registry.example:5000/paper/mini:operator",
            CONTEXTSWARM_MINI_MEMORY="64g",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self._captured_argv()
        self.assertEqual(argv[argv.index("--memory") + 1], "64g")
        self.assertEqual(
            argv[argv.index("--config") - 1],
            "registry.example:5000/paper/mini:operator",
        )

    def test_bridge_network_is_manifest_selected_with_host_gateway_alias(self) -> None:
        self._write_parent_manifest(
            image="research/contextswarm-mini:paper",
            memory_mb=65_536,
            network="bridge",
        )

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self._captured_argv()
        self.assertEqual(argv[argv.index("--network") + 1], "bridge")
        self.assertEqual(
            argv[argv.index("--add-host") + 1],
            "host.docker.internal:host-gateway",
        )

    def test_invalid_manifest_network_fails_before_docker(self) -> None:
        self._write_parent_manifest(
            image="research/contextswarm-mini:paper",
            memory_mb=65_536,
            network="experiment-net",
        )

        result = self._run()

        self.assertEqual(result.returncode, 2)
        self.assertIn("docker.network must be host or bridge", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_unsafe_manifest_and_operator_values_fail_before_docker(self) -> None:
        sentinel = self.temp / "must-not-exist"
        cases = (
            (
                {"CONTEXTSWARM_MINI_IMAGE": f"image:tag$(touch {sentinel})"},
                "invalid Docker image",
            ),
            (
                {"CONTEXTSWARM_MINI_MEMORY": "64g --privileged"},
                "invalid Docker memory",
            ),
        )
        for overrides, expected_error in cases:
            with self.subTest(overrides=overrides):
                result = self._run(**overrides)
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected_error, result.stderr)
                self.assertFalse(self.capture.exists())
                self.assertFalse(sentinel.exists())

        self._write_parent_manifest(image="--privileged", memory_mb=65_536)
        result = self._run()
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid Docker image", result.stderr)
        self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
