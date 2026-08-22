from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from contextswarm_mini.launch_contract import (
    LaunchContractError,
    manifest_closure_sha256,
    resolve_launch_contract,
    verify_manifest_binding,
)


ROOT = Path(__file__).resolve().parents[1]


class LaunchContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        shutil.copytree(ROOT / "configs", self.root / "configs")
        (self.root / "README.md").write_text("frozen\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=ContextSwarm Test",
                "-c",
                "user.email=contextswarm-test@example.invalid",
                "commit",
                "-qm",
                "frozen manifests",
            ],
            cwd=self.root,
            check=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_formal_tracked_closure_has_host_container_digest_parity(self) -> None:
        contract = resolve_launch_contract(
            "configs/smoke.toml",
            self.root,
            formal=True,
        )

        self.assertEqual(contract.container_manifest, "configs/smoke.toml")
        self.assertEqual(
            contract.manifest_sha256,
            manifest_closure_sha256(contract.container_manifest, self.root),
        )
        self.assertEqual(
            verify_manifest_binding(
                contract.container_manifest,
                self.root,
                contract.manifest_sha256,
            ),
            contract.manifest_sha256,
        )
        with self.assertRaisesRegex(LaunchContractError, "does not match"):
            verify_manifest_binding(
                contract.container_manifest,
                self.root,
                "0" * 64,
            )

    def test_dirty_tree_is_rejected_only_for_formal_launches(self) -> None:
        (self.root / "README.md").write_text("dirty\n", encoding="utf-8")

        development = resolve_launch_contract("configs/smoke.toml", self.root)
        self.assertEqual(development.container_manifest, "configs/smoke.toml")
        with self.assertRaisesRegex(LaunchContractError, "modified tracked files"):
            resolve_launch_contract(
                "configs/smoke.toml",
                self.root,
                formal=True,
            )

    def test_runs_manifest_is_development_only_and_mutation_breaks_binding(self) -> None:
        runs = self.root / "runs"
        runs.mkdir()
        local = runs / "local.toml"
        local.write_text(
            'extends = ["../configs/smoke.toml"]\n',
            encoding="utf-8",
        )
        contract = resolve_launch_contract(local, self.root)
        self.assertEqual(contract.container_manifest, "runs/local.toml")

        with self.assertRaisesRegex(LaunchContractError, "must be tracked"):
            resolve_launch_contract(local, self.root, formal=True)

        local.write_text(
            'extends = ["../configs/smoke.toml"]\n\n[experiment]\nseed = 9\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(LaunchContractError, "does not match"):
            verify_manifest_binding(local, self.root, contract.manifest_sha256)

    def test_cycle_and_outside_root_manifest_are_rejected(self) -> None:
        runs = self.root / "runs"
        runs.mkdir()
        first = runs / "first.toml"
        second = runs / "second.toml"
        first.write_text('extends = ["second.toml"]\n', encoding="utf-8")
        second.write_text('extends = ["first.toml"]\n', encoding="utf-8")
        with self.assertRaisesRegex(LaunchContractError, "cycle"):
            resolve_launch_contract(first, self.root)

        outside = Path(self.temporary.name) / "outside.toml"
        outside.write_text(
            f'extends = ["{self.root / "configs" / "smoke.toml"}"]\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(LaunchContractError, "inside the launcher worktree"):
            resolve_launch_contract(outside, self.root)

    def test_invalid_binding_digest_fails_closed(self) -> None:
        for value in ("", "A" * 64, "f" * 63, "z" * 64):
            with self.subTest(value=value), self.assertRaisesRegex(
                LaunchContractError,
                "digest is invalid",
            ):
                verify_manifest_binding("configs/smoke.toml", self.root, value)


if __name__ == "__main__":
    unittest.main()
