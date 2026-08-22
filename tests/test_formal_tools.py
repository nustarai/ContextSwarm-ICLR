from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from contextswarm_mini.config import load_config
from contextswarm_mini.evaluator_broker import (
    BROKER_REQUEST_SCHEMA,
    BrokerError,
    EvaluatorBroker,
)
from contextswarm_mini.formal_tools import DeclarationIndex, stage_worker_tools
from contextswarm_mini.models import Task, Verdict
from contextswarm_mini.pi_agent import PiAgent
from contextswarm_mini.preflight import PreflightError, run_preflight
from contextswarm_mini.secure_io import SnapshotStore, read_regular_bytes


ROOT = Path(__file__).resolve().parents[1]


class _FakeFormalEvaluator:
    def __init__(self) -> None:
        self.evaluation_calls = 0
        self.probe_calls = 0
        self.cancelled_jobs: list[str] = []
        self.lifecycle_observer = None

    def evaluate_bytes(
        self,
        task: Task,
        candidate_bytes: bytes,
        *,
        deadline_monotonic: float | None = None,
        started: float | None = None,
    ) -> Verdict:
        del deadline_monotonic, started
        self.evaluation_calls += 1
        job_id = f"fake-{self.evaluation_calls}"
        if callable(self.lifecycle_observer):
            self.lifecycle_observer("submitted", {"job_id": job_id})
            self.lifecycle_observer("settled", {"job_id": job_id})
        return Verdict(
            task.slug,
            "PROVED",
            1.0,
            0.0,
            {"candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest()},
        )

    def probe(
        self,
        task: Task,
        source: str,
        *,
        timeout_seconds: int = 30,
        deadline_monotonic: float | None = None,
    ) -> dict[str, object]:
        del task, timeout_seconds, deadline_monotonic
        self.probe_calls += 1
        return {
            "status": "elaborated",
            "is_valid_with_sorry": True,
            "is_valid_no_sorry": "sorry" not in source,
            "diagnostics": [],
            "elapsed_ms": 1,
        }

    def cancel_job(self, job_id: str) -> tuple[dict[str, object], None]:
        self.cancelled_jobs.append(job_id)
        return {"job_id": job_id, "status": "cancelled"}, None


class _InitiallyUnadmittedEvaluator(_FakeFormalEvaluator):
    def evaluate_bytes(
        self,
        task: Task,
        candidate_bytes: bytes,
        *,
        deadline_monotonic: float | None = None,
        started: float | None = None,
    ) -> Verdict:
        del deadline_monotonic, started
        if self.evaluation_calls == 0:
            self.evaluation_calls += 1
            return Verdict(task.slug, "REJECTED_OVERLOADED", 0.0, 0.0)
        return super().evaluate_bytes(task, candidate_bytes)

    def probe(
        self,
        task: Task,
        source: str,
        *,
        timeout_seconds: int = 30,
        deadline_monotonic: float | None = None,
    ) -> dict[str, object]:
        if self.probe_calls == 0:
            self.probe_calls += 1
            return {
                "status": "probe_admission_closed",
                "error_kind": "judge_admission_overloaded",
            }
        return super().probe(
            task,
            source,
            timeout_seconds=timeout_seconds,
            deadline_monotonic=deadline_monotonic,
        )


class _PreflightLeanEvaluator:
    endpoint_revision = "rev-1"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def health(self) -> dict[str, object]:
        return {
            "ok": True,
            "workspace_ready": True,
            "accepted_lean_env_ids": ["formal_matholympiadbench"],
            "mathlib_revision": self.endpoint_revision,
        }

    def probe(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "elaborated",
            "is_valid_with_sorry": True,
            "elapsed_ms": 1,
            "mathlib_revision": self.endpoint_revision,
            "lean_version": "4.9.0",
        }


def _task(root: Path) -> Task:
    return Task(
        slug="task",
        root=root,
        problem_text="Prove True.",
        baseline_code="import Mathlib\ntheorem task : True := by\n  sorry\n",
        metadata={"problem_id": "Task", "theorem_name": "task"},
    )


def _config(**updates: object):
    values = {
        "time_limit_seconds": 30,
        "lean_max_concurrent_evaluations": 2,
        "lean_official_reserved_evaluations": 1,
        "lean_agent_local_cutoff_seconds": 0,
        "lean_closeout_timeout_seconds": 2,
        "formal_tools_evaluate_calls_per_task": 8,
        "formal_tools_evaluate_backend_jobs_per_task": 8,
        "formal_tools_query_calls_per_task": 8,
        "formal_tools_query_backend_probes_per_task": 8,
    }
    values.update(updates)
    return replace(load_config("configs/smoke.toml", ROOT), **values)


def _stage_fixture(
    root: Path,
    *,
    config=None,
    evaluator: _FakeFormalEvaluator | None = None,
) -> tuple[EvaluatorBroker, _FakeFormalEvaluator, Task, Path, dict[str, object]]:
    cfg = config or _config()
    fake = evaluator or _FakeFormalEvaluator()
    run_dir = root / "run"
    run_dir.mkdir()
    task = _task(root / "source")
    broker = EvaluatorBroker(
        cfg,
        [task],
        run_dir,
        fake,
        solver_deadline_monotonic=time.monotonic() + cfg.time_limit_seconds,
    )
    broker.start()
    workspace = run_dir / "workers" / task.slug
    (workspace / "baseline").mkdir(parents=True)
    (workspace / "problem.md").write_text(task.problem_text, encoding="utf-8")
    (workspace / "baseline" / "task.lean").write_text(task.baseline_code, encoding="utf-8")
    (workspace / "result.lean").write_text(
        task.baseline_code.replace("sorry", "trivial"),
        encoding="utf-8",
    )
    capability = broker.register_worker(task, workspace, actor_id="worker-task")
    stage_worker_tools(
        workspace,
        capability=capability,
        baseline_names=["task.lean"],
        context_piece_enabled=False,
    )
    payload = json.loads(
        (workspace / ".contextswarm_tool_capability.json").read_text(encoding="utf-8")
    )
    return broker, fake, task, workspace, payload


class FormalToolBrokerTests(unittest.TestCase):
    def test_cps_handoff_does_not_consume_agent_formal_tool_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(
                formal_tools_evaluate_calls_per_task=1,
                formal_tools_evaluate_backend_jobs_per_task=1,
            )
            broker, fake, task, workspace, _capability = _stage_fixture(
                Path(temporary),
                config=config,
            )
            try:
                handoff = broker.evaluate_handoff(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="runner:worker-task",
                    actor_id="worker-task",
                )
                local = broker.evaluate_local(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="worker-capability",
                    actor_id="worker-task",
                )
                exhausted = broker.evaluate_local(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="worker-capability",
                    actor_id="worker-task",
                )

                self.assertEqual(handoff.status, "PROVED")
                self.assertEqual(handoff.response["lane"], "cps_handoff")
                self.assertEqual(local.status, "PROVED")
                self.assertEqual(local.response["lane"], "agent_local")
                self.assertEqual(local.response["call_number"], 1)
                self.assertEqual(exhausted.status, "BUDGET_EXHAUSTED")
                self.assertEqual(fake.evaluation_calls, 2)
            finally:
                broker.close()

    def test_shims_use_broker_and_only_outer_lane_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker, fake, task, workspace, _capability = _stage_fixture(Path(temporary))
            try:
                evaluated = subprocess.run(
                    ["python3", "evaluate.py"],
                    cwd=workspace,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(evaluated.returncode, 0, evaluated.stderr)
                local = json.loads(evaluated.stdout)
                self.assertEqual(local["status"], "PROVED")
                self.assertTrue(local["advisory_only"])
                self.assertFalse(local["official_score_eligible"])

                query = subprocess.run(
                    ["./formal_query", "check", "Nat.succ"],
                    cwd=workspace,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(query.returncode, 0, query.stderr)
                self.assertEqual(json.loads(query.stdout)["status"], "elaborated")

                local_snapshot = broker.best_local_proved(task.slug)
                self.assertIsNotNone(local_snapshot)
                official = broker.evaluate_official(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                )
                self.assertEqual(official.status, "PROVED")
                self.assertEqual(official.score, 1.0)
                self.assertEqual(fake.evaluation_calls, 2)

                diagnostic_rows = (
                    broker.telemetry_root / "agent_local_evaluations.jsonl"
                ).read_text(encoding="utf-8").splitlines()
                official_rows = (
                    broker.telemetry_root / "official_verdicts.jsonl"
                ).read_text(encoding="utf-8").splitlines()
                self.assertEqual(json.loads(diagnostic_rows[-1])["score"], 0.0)
                self.assertEqual(json.loads(official_rows[-1])["score"], 1.0)
            finally:
                broker.close()

    def test_capability_is_bound_to_task_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            broker, _fake, task, _workspace, capability = _stage_fixture(Path(temporary))
            try:
                base = {
                    "schema_version": BROKER_REQUEST_SCHEMA,
                    "token": capability["token"],
                    "task_id": task.slug,
                    "op": "evaluate_local",
                }
                response = broker.handle_worker_request(base)
                self.assertEqual(response["status"], "PROVED")
                with self.assertRaises(BrokerError):
                    broker.handle_worker_request({**base, "token": "wrong"})
                with self.assertRaises(BrokerError):
                    broker.handle_worker_request({**base, "task_id": "other-task"})
                with self.assertRaises(BrokerError):
                    broker.handle_worker_request({**base, "schema_version": "legacy"})
            finally:
                broker.close()

    def test_backend_budgets_count_admitted_jobs_not_cache_hits(self) -> None:
        config = _config(
            formal_tools_evaluate_calls_per_task=4,
            formal_tools_evaluate_backend_jobs_per_task=1,
            formal_tools_query_calls_per_task=4,
            formal_tools_query_backend_probes_per_task=1,
        )
        with tempfile.TemporaryDirectory() as temporary:
            broker, fake, task, workspace, capability = _stage_fixture(
                Path(temporary), config=config
            )
            try:
                first = broker.evaluate_local(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="scope",
                    actor_id="worker",
                )
                cached = broker.evaluate_local(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="scope",
                    actor_id="worker",
                )
                (workspace / "result.lean").write_text(
                    task.baseline_code.replace("sorry", "exact True.intro"),
                    encoding="utf-8",
                )
                exhausted = broker.evaluate_local(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="scope",
                    actor_id="worker",
                )
                self.assertEqual(first.status, "PROVED")
                self.assertTrue(cached.response["cache_hit"])
                self.assertEqual(exhausted.status, "BUDGET_EXHAUSTED")
                self.assertEqual(fake.evaluation_calls, 1)

                request = {
                    "schema_version": BROKER_REQUEST_SCHEMA,
                    "token": capability["token"],
                    "task_id": task.slug,
                    "op": "formal_query",
                    "command": "check",
                    "query": ["Nat.succ"],
                }
                first_query = broker.handle_worker_request(request)
                cached_query = broker.handle_worker_request(request)
                exhausted_query = broker.handle_worker_request(
                    {**request, "query": ["Nat.pred"]}
                )
                self.assertEqual(first_query["status"], "elaborated")
                self.assertTrue(cached_query["cache_hit"])
                self.assertEqual(exhausted_query["status"], "probe_budget_exhausted")
                self.assertEqual(fake.probe_calls, 1)

                rows = [
                    json.loads(line)
                    for line in (
                        broker.telemetry_root / "formal_query_calls.jsonl"
                    ).read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(
                    [row["backend_probe_count"] for row in rows[-3:]],
                    [1, 0, 0],
                )
                self.assertEqual(
                    [row["cache_hit_count"] for row in rows[-3:]],
                    [0, 1, 0],
                )
            finally:
                broker.close()

    def test_unadmitted_calls_release_backend_budget(self) -> None:
        config = _config(
            formal_tools_evaluate_calls_per_task=4,
            formal_tools_evaluate_backend_jobs_per_task=1,
            formal_tools_query_calls_per_task=4,
            formal_tools_query_backend_probes_per_task=1,
        )
        evaluator = _InitiallyUnadmittedEvaluator()
        with tempfile.TemporaryDirectory() as temporary:
            broker, _fake, task, workspace, capability = _stage_fixture(
                Path(temporary), config=config, evaluator=evaluator
            )
            try:
                rejected = broker.evaluate_local(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="scope",
                    actor_id="worker",
                )
                admitted = broker.evaluate_local(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="scope",
                    actor_id="worker",
                )
                (workspace / "result.lean").write_text(
                    task.baseline_code.replace("sorry", "simp"),
                    encoding="utf-8",
                )
                exhausted = broker.evaluate_local(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="scope",
                    actor_id="worker",
                )

                self.assertEqual(rejected.status, "REJECTED_OVERLOADED")
                self.assertTrue(rejected.response["backend_admission_released"])
                self.assertEqual(admitted.status, "PROVED")
                self.assertEqual(exhausted.status, "BUDGET_EXHAUSTED")

                request = {
                    "schema_version": BROKER_REQUEST_SCHEMA,
                    "token": capability["token"],
                    "task_id": task.slug,
                    "op": "formal_query",
                    "command": "check",
                }
                closed = broker.handle_worker_request(
                    {**request, "query": ["Nat.succ"]}
                )
                probed = broker.handle_worker_request(
                    {**request, "query": ["Nat.pred"]}
                )
                probe_exhausted = broker.handle_worker_request(
                    {**request, "query": ["Nat.add"]}
                )

                self.assertEqual(closed["status"], "probe_admission_closed")
                self.assertEqual(probed["status"], "elaborated")
                self.assertEqual(
                    probe_exhausted["status"], "probe_budget_exhausted"
                )

                journal = [
                    json.loads(line)
                    for line in broker.journal_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]
                releases = [
                    row
                    for row in journal
                    if row.get("event") == "budget_counter"
                    and row.get("operation") == "release_unadmitted"
                ]
                self.assertEqual(
                    {row["counter"] for row in releases},
                    {"evaluate_backend_jobs", "query_backend_probes"},
                )
            finally:
                broker.close()

    def test_recovery_restores_released_budget_as_available(self) -> None:
        config = _config(
            formal_tools_evaluate_calls_per_task=4,
            formal_tools_evaluate_backend_jobs_per_task=1,
            formal_tools_query_calls_per_task=4,
            formal_tools_query_backend_probes_per_task=1,
        )
        with tempfile.TemporaryDirectory() as temporary:
            broker, _fake, task, workspace, capability = _stage_fixture(
                Path(temporary),
                config=config,
                evaluator=_InitiallyUnadmittedEvaluator(),
            )
            try:
                rejected = broker.evaluate_local(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="scope",
                    actor_id="worker",
                )
                closed = broker.handle_worker_request(
                    {
                        "schema_version": BROKER_REQUEST_SCHEMA,
                        "token": capability["token"],
                        "task_id": task.slug,
                        "op": "formal_query",
                        "command": "check",
                        "query": ["Nat.succ"],
                    }
                )
                self.assertTrue(rejected.response["backend_admission_released"])
                self.assertEqual(closed["status"], "probe_admission_closed")
            finally:
                broker.close()

            recovered_evaluator = _FakeFormalEvaluator()
            recovered = EvaluatorBroker(
                config,
                [task],
                broker.run_dir,
                recovered_evaluator,
                solver_deadline_monotonic=time.monotonic() + config.time_limit_seconds,
            )
            recovered.start()
            try:
                recovered_capability = recovered.register_worker(
                    task,
                    workspace,
                    actor_id="recovered-worker",
                )
                admitted = recovered.evaluate_local(
                    task,
                    workspace / "result.lean",
                    trusted_root=workspace,
                    scope_id="recovered-scope",
                    actor_id="recovered-worker",
                )
                probed = recovered.handle_worker_request(
                    {
                        "schema_version": BROKER_REQUEST_SCHEMA,
                        "token": recovered_capability.token,
                        "task_id": task.slug,
                        "op": "formal_query",
                        "command": "check",
                        "query": ["Nat.pred"],
                    }
                )
                self.assertEqual(admitted.status, "PROVED")
                self.assertEqual(probed["status"], "elaborated")
            finally:
                recovered.close()

    def test_closeout_timeout_starts_at_actual_closeout(self) -> None:
        config = _config(lean_closeout_timeout_seconds=2)
        with tempfile.TemporaryDirectory() as temporary:
            broker, _fake, _task_value, _workspace, _capability = _stage_fixture(
                Path(temporary), config=config
            )
            try:
                before = time.monotonic()
                deadline = broker.begin_closeout()
                self.assertGreaterEqual(deadline - before, 1.9)
                self.assertLessEqual(deadline - before, 2.1)
                self.assertEqual(broker.begin_closeout(), deadline)
            finally:
                broker.close()

    def test_recovery_cancels_a_journaled_abandoned_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            run_dir.mkdir()
            task = _task(root / "source")
            config = _config()
            first = EvaluatorBroker(
                config,
                [task],
                run_dir,
                _FakeFormalEvaluator(),
                solver_deadline_monotonic=time.monotonic() + 30,
            )
            first._journal(
                "judge_job",
                lifecycle_event="submitted",
                job_id="abandoned-job",
            )

            recovered_evaluator = _FakeFormalEvaluator()
            recovered = EvaluatorBroker(
                config,
                [task],
                run_dir,
                recovered_evaluator,
                solver_deadline_monotonic=time.monotonic() + 30,
            )
            try:
                recovered.start()
                self.assertEqual(
                    recovered_evaluator.cancelled_jobs,
                    ["abandoned-job"],
                )
            finally:
                recovered.close()


class SecureSnapshotTests(unittest.TestCase):
    def test_snapshot_is_immutable_and_rejects_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            candidate = workspace / "result.lean"
            candidate.write_bytes(b"first")
            store = SnapshotStore(root / "private", max_bytes=64)
            snapshot = store.capture(
                task_id="task",
                source=candidate,
                trusted_root=workspace,
                captured_at_monotonic=1.0,
            )
            candidate.write_bytes(b"second")
            self.assertEqual(snapshot.payload, b"first")
            self.assertEqual(snapshot.path.read_bytes(), b"first")
            self.assertEqual(snapshot.path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(store.load(task_id="task", sha256=snapshot.sha256).payload, b"first")

            symlink = workspace / "symlink.lean"
            symlink.symlink_to(candidate)
            with self.assertRaises(OSError):
                read_regular_bytes(symlink, trusted_root=workspace)
            hardlink = workspace / "hardlink.lean"
            os.link(candidate, hardlink)
            with self.assertRaises(OSError):
                read_regular_bytes(candidate, trusted_root=workspace)
            with self.assertRaises(OSError):
                read_regular_bytes(root / "outside.lean", trusted_root=workspace)


class DeclarationIndexTests(unittest.TestCase):
    def test_builder_records_qualified_declarations_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Mathlib"
            source.mkdir()
            (source / "Demo.lean").write_text(
                (
                    "namespace Demo\n"
                    "section Inner\n"
                    "lemma useful (n : Nat) : n = n := by rfl\n"
                    "end Inner -- retain the outer namespace\n"
                    "noncomputable section LinearEquiv.finsuppUnique\n"
                    "lemma dottedSection : True := by trivial\n"
                    "end LinearEquiv.finsuppUnique\n"
                    "mutual\n"
                    "def mutualFirst : Nat := 1\n"
                    "def mutualSecond : Nat := 2\n"
                    "end\n"
                    "lemma afterMutual : True := by trivial\n"
                    "@[simp] theorem tagged : True := by trivial\n"
                    "private lemma hidden : True := by trivial\n"
                    "end Demo\n"
                ),
                encoding="utf-8",
            )
            output = root / "decls.sqlite3"
            built = subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts" / "build_decl_index.py"),
                    "--source-root",
                    str(source),
                    "--output",
                    str(output),
                    "--mathlib-revision",
                    "rev-1",
                    "--lean-toolchain",
                    "v4.9.0",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            report = json.loads(built.stdout)
            self.assertEqual(report["declaration_count"], 6)
            index = DeclarationIndex(
                output,
                expected_sha256=report["sha256"],
                expected_revision="rev-1",
            )
            self.assertEqual(
                [row["name"] for row in index.search("useful", limit=5)],
                ["Demo.useful"],
            )
            self.assertEqual(
                [row["name"] for row in index.search("tagged", limit=5)],
                ["Demo.tagged"],
            )
            self.assertEqual(
                [row["name"] for row in index.search("dottedSection", limit=5)],
                ["Demo.dottedSection"],
            )
            self.assertEqual(
                [row["name"] for row in index.search("afterMutual", limit=5)],
                ["Demo.afterMutual"],
            )
            self.assertEqual(index.search("hidden", limit=5), [])

    def test_revision_bound_index_filters_guarded_and_private_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decls.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute(
                    "CREATE TABLE decls (name TEXT, kind TEXT, file TEXT, line INTEGER, head TEXT, snippet TEXT)"
                )
                connection.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    (("schema", "decl_index_v1"), ("mathlib_revision", "rev-1"), ("lean_toolchain", "v4.9.0")),
                )
                connection.executemany(
                    "INSERT INTO decls VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ("Nat.succ", "def", "Mathlib/Data/Nat.lean", 10, "Nat", "Nat.succ : Nat → Nat"),
                        ("task", "theorem", "Answers/Task.lean", 1, "task", "task : True"),
                        ("answer_helper", "lemma", "Answers/Task.lean", 2, "answer", "hidden"),
                        ("private_decl", "lemma", "../private.lean", 1, "private", "hidden"),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            index = DeclarationIndex(
                path,
                expected_sha256=digest,
                expected_revision="rev-1",
            )
            self.assertTrue(index.info.compatible)
            matches = index.search("Nat succ", limit=10, guarded_names={"task"})
            self.assertEqual([row["name"] for row in matches], ["Nat.succ"])
            self.assertFalse(
                DeclarationIndex(
                    path,
                    expected_sha256=digest,
                    expected_revision="other",
                ).info.compatible
            )


class PreflightContractTests(unittest.TestCase):
    def _index(self, root: Path) -> tuple[Path, str]:
        source = root / "Mathlib"
        source.mkdir()
        (source / "Demo.lean").write_text(
            "def demo : Nat := 1\n",
            encoding="utf-8",
        )
        output = root / "decls.sqlite3"
        built = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts" / "build_decl_index.py"),
                "--source-root",
                str(source),
                "--output",
                str(output),
                "--mathlib-revision",
                "rev-1",
                "--lean-toolchain",
                "v4.9.0",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return output, str(json.loads(built.stdout)["sha256"])

    def test_preflight_binds_index_to_endpoint_revision(self) -> None:
        true_binary = shutil.which("true")
        if true_binary is None:
            self.skipTest("true executable is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index, digest = self._index(root)
            config = replace(
                _config(),
                pi_binary=true_binary,
                aisw_enabled=False,
                formal_tools_decl_index=str(index),
                formal_tools_decl_index_sha256=digest,
                formal_tools_mathlib_revision="rev-1",
                formal_tools_require_decl_index=True,
            )
            environment = {
                "CONTEXTSWARM_MINI_DECL_INDEX": "",
                "CONTEXTSWARM_MINI_DECL_INDEX_SHA256": "",
                "CONTEXTSWARM_MINI_MATHLIB_REVISION": "",
                "MINI_SWARM_DECL_INDEX": "",
            }
            with patch.dict(os.environ, environment, clear=False), patch(
                "contextswarm_mini.preflight.LeanEvaluator",
                _PreflightLeanEvaluator,
            ):
                _PreflightLeanEvaluator.endpoint_revision = "rev-1"
                report = run_preflight(config, root / "good")
                self.assertEqual(
                    report["formal_tools"]["endpoint_mathlib_revision"],
                    "rev-1",
                )
                _PreflightLeanEvaluator.endpoint_revision = "rev-2"
                with self.assertRaises(PreflightError):
                    run_preflight(config, root / "mismatch")


class PiGuardTests(unittest.TestCase):
    def _guard(self, workspace: Path, tool_name: str, tool_input: dict[str, object]):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")
        script = """
import {pathToFileURL} from 'node:url';
const {default: register} = await import(pathToFileURL(process.argv[1]).href);
let handler = null;
register({on(name, callback) { if (name === 'tool_call') handler = callback; }});
const event = JSON.parse(process.argv[2]);
const result = await handler(event);
process.stdout.write(JSON.stringify(result ?? null));
"""
        event = json.dumps({"toolName": tool_name, "input": tool_input})
        env = {
            **os.environ,
            "CONTEXTSWARM_WORKER_GUARD": "1",
            "CONTEXTSWARM_WORKDIR": str(workspace),
            "CONTEXTSWARM_EXPERIMENT_MODE": "parallel",
            "CONTEXTSWARM_WORKER_MAX_WRITE_BYTES": "1024",
            "CONTEXTSWARM_EVALUATOR_COMMAND_TIMEOUT_SECONDS": "420",
        }
        result = subprocess.run(
            [
                node,
                "--input-type=module",
                "--eval",
                script,
                str(ROOT / "contextswarm_mini" / "pi_worker_guard.mjs"),
                event,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        return json.loads(result.stdout)

    def test_guard_allows_public_surface_and_blocks_escape_hatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "problem.md").write_text("problem", encoding="utf-8")
            (workspace / "result.lean").write_text("result", encoding="utf-8")
            (workspace / "evaluate.py").write_text("", encoding="utf-8")
            (workspace / "formal_query").write_text("", encoding="utf-8")

            self.assertIsNone(self._guard(workspace, "read", {"path": "problem.md"}))
            self.assertIsNone(
                self._guard(
                    workspace,
                    "edit",
                    {
                        "path": "result.lean",
                        "edits": [{"oldText": "result", "newText": "trivial"}],
                    },
                )
            )
            self.assertIsNone(
                self._guard(
                    workspace,
                    "bash",
                    {"command": "sed -n '1,10p' problem.md"},
                )
            )
            self.assertIsNone(
                self._guard(
                    workspace,
                    "bash",
                    {"command": "python3 evaluate.py"},
                )
            )
            self.assertIsNone(
                self._guard(
                    workspace,
                    "bash",
                    {"command": "./context_piece search --query induction"},
                )
            )
            self.assertIsNone(
                self._guard(
                    workspace,
                    "bash",
                    {"command": "sed -n '1,10p' problem.md | head -n 1"},
                )
            )
            self.assertIsNone(
                self._guard(
                    workspace,
                    "bash",
                    {"command": "rg -n problem problem.md"},
                )
            )
            blocked = (
                ("read", {"path": "/etc/passwd"}),
                ("write", {"path": "../escape", "content": "x"}),
                ("write", {"path": "result.lean", "content": "x" * 1025}),
                (
                    "edit",
                    {
                        "path": "result.lean",
                        "edits": [{"oldText": "result", "newText": "x" * 1025}],
                    },
                ),
                ("bash", {"command": "python3 -c 'print(1)'"}),
                ("bash", {"command": "sed -n 'e id' problem.md"}),
                ("bash", {"command": "head problem.md & id"}),
                ("bash", {"command": "rg --pre=id pattern problem.md"}),
                ("bash", {"command": "rg problem.md"}),
                ("bash", {"command": "grep --recursive problem.md"}),
                ("bash", {"command": "rg pattern --replace problem.md"}),
                ("bash", {"command": "rg --hostname-bin=env pattern problem.md"}),
                ("bash", {"command": "rg --hostname-b=env pattern problem.md"}),
                ("bash", {"command": "rg --host\\\nname-bin=env pattern problem.md"}),
                ("bash", {"command": "pwd | head *"}),
                ("bash", {"command": "pwd | head evaluate.py"}),
                ("bash", {"command": "grep import evaluate.py problem.md"}),
                ("bash", {"command": "diff problem.md evaluate.py"}),
                ("bash", {"command": "cd scratch/missing; head ../../problem.md"}),
                (
                    "bash",
                    {
                        "command": (
                            "./context_piece create --title leak "
                            "--bo\\\ndy-file /etc/passwd"
                        )
                    },
                ),
                (
                    "bash",
                    {
                        "command": (
                            "./context_piece create --title leak "
                            "--body-file /etc/passwd"
                        )
                    },
                ),
                (
                    "bash",
                    {
                        "command": (
                            "./context_piece create --title leak "
                            "--body-f=/etc/passwd"
                        )
                    },
                ),
                (
                    "bash",
                    {
                        "command": (
                            "./context_piece create --title leak "
                            "--body-=/etc/passwd"
                        )
                    },
                ),
                ("web_search", {"query": "secret"}),
            )
            for tool_name, tool_input in blocked:
                with self.subTest(tool_name=tool_name, tool_input=tool_input):
                    response = self._guard(workspace, tool_name, tool_input)
                    self.assertTrue(response["block"])

    def test_pi_environment_does_not_inherit_operator_secrets(self) -> None:
        secrets = {
            "LEAN_AUTH_TOKEN": "lean-secret",
            "OPENAI_API_KEY": "openai-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "CONTEXTSWARM_MINI_DECL_INDEX": "/private/index",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            secrets,
            clear=False,
        ):
            env = PiAgent(load_config("configs/smoke.toml", ROOT)).environment(
                task_id="task",
                actor_id="worker",
                workdir=Path(temporary),
            )
        self.assertTrue(secrets.keys().isdisjoint(env))
        self.assertEqual(env["CONTEXTSWARM_TASK_ID"], "task")
        self.assertEqual(Path(env["HOME"]).name, "home")


if __name__ == "__main__":
    unittest.main()
