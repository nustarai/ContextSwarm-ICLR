"""Run-owned trusted broker for all worker and outer Lean evaluations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socketserver
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping

from .artifacts import append_jsonl, atomic_write_bytes
from .config import ExperimentConfig
from .evaluator import _local_contract_error, _terminal
from .formal_tools import DeclarationIndex, ToolCapability, sanitize_public_text
from .models import Task, Verdict
from .secure_io import CandidateSnapshot, SnapshotStore, read_regular_bytes


BROKER_REQUEST_SCHEMA = "contextswarm_mini_broker_request_v1"
BROKER_JOURNAL_SCHEMA = "contextswarm_mini_broker_journal_v1"
BROKER_TELEMETRY_SCHEMA = "contextswarm_mini_formal_telemetry_v1"

_MAX_REQUEST_BYTES = 128 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_LEAN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_'.₀-₉ⁿ¹²³@]*$")
_IMPORT_LINE = re.compile(r"(?m)^\s*import\s+[^\n]+\s*$")
_DEFAULT_TACTICS = (
    "simp",
    "aesop",
    "omega",
    "linarith",
    "nlinarith",
    "norm_num",
    "ring",
    "positivity",
)


class BrokerError(RuntimeError):
    """A fail-closed broker request or admission failure."""


@dataclass(frozen=True)
class WorkerBinding:
    task: Task
    workspace: Path
    token: str
    binding_id: str
    actor_id: str


class _PriorityAdmission:
    """Run-local official-priority admission with an optional reserved slot."""

    def __init__(self, total: int, reserved_official: int):
        self.total = max(1, int(total))
        self.reserved_official = max(0, int(reserved_official))
        if self.reserved_official >= self.total:
            raise ValueError("official reserve must be smaller than total evaluator capacity")
        self._condition = threading.Condition(threading.RLock())
        self._active_total = 0
        self._active_local = 0
        self._waiting_official = 0

    @contextmanager
    def acquire(self, role: str, *, deadline: float) -> Iterator[int]:
        if role not in {"agent_local", "formal_query", "official"}:
            raise ValueError(f"unsupported evaluator admission role: {role}")
        official = role == "official"
        started = time.monotonic()
        with self._condition:
            if official:
                self._waiting_official += 1
            try:
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        raise BrokerError("evaluator admission deadline elapsed")
                    local_capacity = self.total - self.reserved_official
                    available = self._active_total < self.total
                    if official:
                        admitted = available
                    else:
                        admitted = (
                            available
                            and self._active_local < local_capacity
                            and self._waiting_official == 0
                        )
                    if admitted:
                        self._active_total += 1
                        if not official:
                            self._active_local += 1
                        break
                    self._condition.wait(timeout=min(0.1, deadline - now))
            finally:
                if official:
                    self._waiting_official -= 1
        waited_ms = int((time.monotonic() - started) * 1_000)
        try:
            yield waited_ms
        finally:
            with self._condition:
                self._active_total -= 1
                if not official:
                    self._active_local -= 1
                self._condition.notify_all()


class _BrokerUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class _BrokerRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        broker: EvaluatorBroker = self.server.broker  # type: ignore[attr-defined]
        raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > _MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            response = {"status": "BROKER_REJECTED", "message": "invalid request framing"}
        else:
            try:
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, Mapping):
                    raise BrokerError("request must be an object")
                response = broker.handle_worker_request(payload)
            except (BrokerError, UnicodeError, json.JSONDecodeError, OSError, ValueError) as exc:
                response = {
                    "status": "BROKER_REJECTED",
                    "message": sanitize_public_text(str(exc), limit=400),
                }
            except Exception as exc:
                response = {
                    "status": "BROKER_ERROR",
                    "message": type(exc).__name__,
                }
        encoded = json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        if len(encoded) > _MAX_RESPONSE_BYTES:
            encoded = b'{"status":"BROKER_ERROR","message":"response exceeded bound"}\n'
        self.wfile.write(encoded)


class EvaluatorBroker:
    """The only component which owns Judge transport, budgets, and score lanes."""

    def __init__(
        self,
        config: ExperimentConfig,
        tasks: list[Task],
        run_dir: Path,
        evaluator: Any,
        *,
        solver_deadline_monotonic: float,
    ) -> None:
        self.config = config
        self.tasks = {task.slug: task for task in tasks}
        self.run_dir = run_dir.resolve()
        self.evaluator = evaluator
        self.solver_deadline = float(solver_deadline_monotonic)
        self.local_admission_deadline = max(
            time.monotonic(),
            self.solver_deadline - config.lean_agent_local_cutoff_seconds,
        )
        self.closeout_deadline: float | None = None
        self.private_root = self.run_dir / ".broker_private"
        self.private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.private_root, 0o700)
        self.snapshot_store = SnapshotStore(
            self.private_root / "candidate_snapshots",
            max_bytes=config.formal_tools_max_candidate_bytes,
        )
        self.journal_path = self.private_root / "journal.jsonl"
        self.telemetry_root = self.run_dir / "telemetry"
        self.telemetry_root.mkdir(parents=True, exist_ok=True)
        self._journal_lock = threading.RLock()
        self._telemetry_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._budget_lock = threading.RLock()
        self._bindings: dict[str, WorkerBinding] = {}
        self._counts: dict[tuple[str, str], int] = {}
        self._budget_serials: dict[tuple[str, str], int] = {}
        self._evaluation_cache: dict[tuple[str, str, str, str], Verdict] = {}
        self._probe_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._local_proved: dict[str, CandidateSnapshot] = {}
        self._active_jobs: set[str] = set()
        self._context = threading.local()
        self._query_metrics = threading.local()
        self._admission = _PriorityAdmission(
            config.lean_max_concurrent_evaluations,
            config.lean_official_reserved_evaluations,
        )
        self.declaration_index = DeclarationIndex(
            _resolve_decl_index(config),
            expected_sha256=_effective_decl_index_sha256(config),
            expected_revision=_effective_mathlib_revision(config),
        )
        self.socket_path = _socket_path(self.run_dir)
        self._server: _BrokerUnixServer | None = None
        self._server_thread: threading.Thread | None = None
        self._closed = False
        self._recover_journal()
        if hasattr(self.evaluator, "terminal_overload_retries"):
            # Backend jobs, not hidden client resubmissions, are the auditable
            # budget unit while broker-owned calls are active.
            self.evaluator.terminal_overload_retries = 0
        if hasattr(self.evaluator, "lifecycle_observer"):
            self.evaluator.lifecycle_observer = self._observe_evaluator_lifecycle

    def start(self) -> None:
        if self._server is not None:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = _BrokerUnixServer(str(self.socket_path), _BrokerRequestHandler)
        server.broker = self  # type: ignore[attr-defined]
        os.chmod(self.socket_path, 0o600)
        thread = threading.Thread(target=server.serve_forever, name="formal-evaluator-broker", daemon=True)
        thread.start()
        self._server = server
        self._server_thread = thread
        self._journal("broker_started", socket_id=hashlib.sha256(str(self.socket_path).encode()).hexdigest()[:16])
        self._cancel_recovered_jobs()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._server_thread
        if thread is not None:
            thread.join(timeout=5)
        self._cancel_recovered_jobs()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._journal("broker_closed", active_job_count=len(self._active_jobs))

    def __enter__(self) -> EvaluatorBroker:
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def register_worker(self, task: Task, workspace: Path, *, actor_id: str) -> ToolCapability:
        if task.slug not in self.tasks:
            raise BrokerError("worker task is not selected by this run")
        root = workspace.resolve()
        try:
            root.relative_to(self.run_dir)
        except ValueError as exc:
            raise BrokerError("worker workspace is outside the run") from exc
        token = secrets.token_urlsafe(32)
        binding_id = hashlib.sha256(token.encode()).hexdigest()[:20]
        binding = WorkerBinding(task, root, token, binding_id, actor_id)
        with self._state_lock:
            self._bindings[token] = binding
        self._journal(
            "worker_registered",
            task_id=task.slug,
            binding_id=binding_id,
            actor_id=actor_id,
        )
        return ToolCapability(
            socket_path=str(self.socket_path),
            token=token,
            task_id=task.slug,
            surface_version=self.config.formal_tools_version,
        )

    def begin_closeout(self) -> float:
        """Start the bounded outer phase at the actual candidate-freeze boundary."""

        with self._state_lock:
            if self.closeout_deadline is None:
                self.closeout_deadline = (
                    time.monotonic() + self.config.lean_closeout_timeout_seconds
                )
                self._journal(
                    "closeout_admission_opened",
                    timeout_seconds=self.config.lean_closeout_timeout_seconds,
                )
            return self.closeout_deadline

    def handle_worker_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("schema_version") != BROKER_REQUEST_SCHEMA:
            raise BrokerError("unsupported broker request schema")
        token = str(request.get("token") or "")
        with self._state_lock:
            binding = self._bindings.get(token)
        if binding is None or not secrets.compare_digest(token, binding.token):
            raise BrokerError("invalid formal tool capability")
        if str(request.get("task_id") or "") != binding.task.slug:
            raise BrokerError("capability is not valid for the requested task")
        operation = str(request.get("op") or "")
        if operation == "evaluate_local":
            verdict = self.evaluate_local(
                binding.task,
                binding.workspace / "result.lean",
                trusted_root=binding.workspace,
                scope_id=binding.binding_id,
                actor_id=binding.actor_id,
            )
            return _worker_verdict(verdict)
        if operation == "formal_query":
            return self.formal_query(binding, request)
        raise BrokerError("unsupported formal tool operation")

    def evaluate_local(
        self,
        task: Task,
        source: Path,
        *,
        trusted_root: Path,
        scope_id: str,
        actor_id: str,
        episode: int = 0,
    ) -> Verdict:
        started = time.monotonic()
        call_number = self._increment(task.slug, "evaluate_calls")
        if call_number > self.config.formal_tools_evaluate_calls_per_task:
            verdict = Verdict(
                task.slug,
                "BUDGET_EXHAUSTED",
                0.0,
                time.monotonic() - started,
                {"reason": "agent-local evaluate call budget exhausted", "call_number": call_number},
            )
            self._log_evaluation("agent_local", verdict, actor_id=actor_id, episode=episode, cache_hit=False)
            return verdict
        try:
            snapshot = self.snapshot_store.capture(
                task_id=task.slug,
                source=source,
                trusted_root=trusted_root,
                captured_at_monotonic=started,
            )
        except OSError as exc:
            verdict = Verdict(
                task.slug,
                "LOCAL_REJECTED",
                0.0,
                time.monotonic() - started,
                {"reason": sanitize_public_text(str(exc), limit=400), "call_number": call_number},
            )
            self._log_evaluation("agent_local", verdict, actor_id=actor_id, episode=episode, cache_hit=False)
            return verdict
        try:
            candidate_text = snapshot.payload.decode("utf-8")
        except UnicodeError as exc:
            verdict = Verdict(
                task.slug,
                "LOCAL_REJECTED",
                0.0,
                time.monotonic() - started,
                {
                    "reason": "candidate is not valid UTF-8",
                    "candidate_sha256": snapshot.sha256,
                    "call_number": call_number,
                },
                error=sanitize_public_text(str(exc), limit=400),
            )
            self._log_evaluation("agent_local", verdict, actor_id=actor_id, episode=episode, cache_hit=False)
            return verdict
        contract_error = _local_contract_error(task, candidate_text, task.baseline_code)
        if contract_error:
            verdict = Verdict(
                task.slug,
                "LOCAL_REJECTED",
                0.0,
                time.monotonic() - started,
                {
                    "reason": contract_error,
                    "candidate_sha256": snapshot.sha256,
                    "call_number": call_number,
                },
            )
            self._log_evaluation("agent_local", verdict, actor_id=actor_id, episode=episode, cache_hit=False)
            return verdict
        if time.monotonic() >= self.local_admission_deadline:
            verdict = Verdict(
                task.slug,
                "OUT_OF_HORIZON",
                0.0,
                time.monotonic() - started,
                {
                    "reason": "agent-local evaluator admission closed before candidate freeze",
                    "candidate_sha256": snapshot.sha256,
                    "call_number": call_number,
                },
            )
            self._log_evaluation("agent_local", verdict, actor_id=actor_id, episode=episode, cache_hit=False)
            return verdict
        key = ("agent_local", scope_id, task.slug, snapshot.sha256)
        with self._state_lock:
            cached = self._evaluation_cache.get(key)
        if cached is not None:
            verdict = _clone_verdict(cached)
            verdict.elapsed_seconds = time.monotonic() - started
            verdict.response.update({"cache_hit": True, "call_number": call_number})
            self._log_evaluation("agent_local", verdict, actor_id=actor_id, episode=episode, cache_hit=True)
            return verdict
        if not self._budget_available(
            task.slug,
            "evaluate_backend_jobs",
            self.config.formal_tools_evaluate_backend_jobs_per_task,
        ):
            verdict = Verdict(
                task.slug,
                "BUDGET_EXHAUSTED",
                0.0,
                time.monotonic() - started,
                {
                    "reason": "agent-local evaluator backend-job budget exhausted",
                    "candidate_sha256": snapshot.sha256,
                    "call_number": call_number,
                },
            )
            self._log_evaluation("agent_local", verdict, actor_id=actor_id, episode=episode, cache_hit=False)
            return verdict
        backend_number: int | None = None
        call_context: dict[str, Any] | None = None
        call_deadline = min(
            self.solver_deadline,
            started + max(1.0, self.config.formal_tools_command_timeout_seconds - 15.0),
        )
        try:
            with self._admission.acquire(
                "agent_local",
                deadline=min(self.local_admission_deadline, call_deadline),
            ) as waited_ms:
                backend_number = self._reserve_budget(
                    task.slug,
                    "evaluate_backend_jobs",
                    self.config.formal_tools_evaluate_backend_jobs_per_task,
                )
                if backend_number is None:
                    verdict = Verdict(
                        task.slug,
                        "BUDGET_EXHAUSTED",
                        0.0,
                        time.monotonic() - started,
                        {
                            "reason": "agent-local evaluator backend-job budget exhausted",
                            "candidate_sha256": snapshot.sha256,
                            "call_number": call_number,
                        },
                    )
                else:
                    call_context = {
                        "lane": "agent_local",
                        "task_id": task.slug,
                        "candidate_sha256": snapshot.sha256,
                        "submitted_jobs": 0,
                    }
                    self._context.value = call_context
                    verdict = self.evaluator.evaluate_bytes(
                        task,
                        snapshot.payload,
                        deadline_monotonic=call_deadline,
                    )
        except BrokerError as exc:
            verdict = Verdict(task.slug, "OUT_OF_HORIZON", 0.0, time.monotonic() - started, error=str(exc))
            waited_ms = int((time.monotonic() - started) * 1_000)
        except Exception as exc:
            verdict = Verdict(task.slug, "EVALUATOR_ERROR", 0.0, time.monotonic() - started, error=type(exc).__name__)
            waited_ms = int((time.monotonic() - started) * 1_000)
        finally:
            self._context.value = None
        if (
            backend_number is not None
            and call_context is not None
            and call_context.get("submitted_jobs") == 0
            and verdict.status in {"OUT_OF_HORIZON", "REJECTED_OVERLOADED"}
        ):
            self._release_budget(
                task.slug,
                "evaluate_backend_jobs",
                serial=backend_number,
            )
            backend_number = None
            verdict.response["backend_admission_released"] = True
        diagnostic_score = verdict.score
        verdict.score = 0.0
        verdict.response.update(
            {
                "candidate_sha256": snapshot.sha256,
                "lane": "agent_local",
                "authority": "diagnostic_only",
                "official_score_eligible": False,
                "diagnostic_score": diagnostic_score,
                "call_number": call_number,
                "admission_wait_ms": waited_ms,
                "cache_hit": False,
            }
        )
        if backend_number is not None:
            verdict.response["backend_job_number"] = backend_number
        if verdict.status == "PROVED":
            with self._state_lock:
                prior = self._local_proved.get(task.slug)
                if prior is None or snapshot.captured_at_monotonic >= prior.captured_at_monotonic:
                    self._local_proved[task.slug] = snapshot
        cache_eligible = backend_number is not None
        if cache_eligible:
            with self._state_lock:
                self._evaluation_cache[key] = _clone_verdict(verdict)
        self._journal(
            "evaluation_complete",
            lane="agent_local",
            task_id=task.slug,
            binding_id=scope_id,
            candidate_sha256=snapshot.sha256,
            cache_eligible=cache_eligible,
            verdict=verdict.as_dict(),
        )
        self._log_evaluation("agent_local", verdict, actor_id=actor_id, episode=episode, cache_hit=False)
        return verdict

    def evaluate_official(
        self,
        task: Task,
        source: Path,
        *,
        trusted_root: Path,
    ) -> Verdict:
        started = time.monotonic()
        closeout_deadline = self.begin_closeout()
        try:
            snapshot = self.snapshot_store.capture(
                task_id=task.slug,
                source=source,
                trusted_root=trusted_root,
                captured_at_monotonic=started,
            )
        except OSError as exc:
            verdict = Verdict(task.slug, "MISSING_CANDIDATE", 0.0, 0.0, error=sanitize_public_text(str(exc)))
            self._log_evaluation("official", verdict, actor_id="closeout", episode=0, cache_hit=False)
            return verdict
        key = ("official", "outer", task.slug, snapshot.sha256)
        with self._state_lock:
            cached = self._evaluation_cache.get(key)
        if cached is not None:
            verdict = _clone_verdict(cached)
            verdict.response["cache_hit"] = True
            self._log_evaluation("official", verdict, actor_id="closeout", episode=0, cache_hit=True)
            return verdict
        if time.monotonic() >= closeout_deadline:
            verdict = Verdict(
                task.slug,
                "EVALUATOR_TIMEOUT",
                0.0,
                0.0,
                {"reason": "official closeout deadline elapsed", "candidate_sha256": snapshot.sha256},
            )
            self._log_evaluation("official", verdict, actor_id="closeout", episode=0, cache_hit=False)
            return verdict
        try:
            with self._admission.acquire("official", deadline=closeout_deadline) as waited_ms:
                self._context.value = {
                    "lane": "official",
                    "task_id": task.slug,
                    "candidate_sha256": snapshot.sha256,
                }
                verdict = self.evaluator.evaluate_bytes(
                    task,
                    snapshot.payload,
                    deadline_monotonic=closeout_deadline,
                )
        except BrokerError as exc:
            verdict = Verdict(task.slug, "EVALUATOR_TIMEOUT", 0.0, time.monotonic() - started, error=str(exc))
            waited_ms = int((time.monotonic() - started) * 1_000)
        except Exception as exc:
            verdict = Verdict(task.slug, "EVALUATOR_ERROR", 0.0, time.monotonic() - started, error=type(exc).__name__)
            waited_ms = int((time.monotonic() - started) * 1_000)
        finally:
            self._context.value = None
        if verdict.status == "OUT_OF_HORIZON":
            verdict.status = "EVALUATOR_TIMEOUT"
            verdict.error = verdict.error or "official closeout deadline elapsed"
        if not (verdict.status == "PROVED" and verdict.score == 1.0):
            verdict.score = 0.0
        verdict.response.update(
            {
                "candidate_sha256": snapshot.sha256,
                "lane": "official",
                "authority": "outer_official",
                "official_score_eligible": verdict.status == "PROVED" and verdict.score == 1.0,
                "admission_wait_ms": waited_ms,
                "cache_hit": False,
            }
        )
        with self._state_lock:
            self._evaluation_cache[key] = _clone_verdict(verdict)
        self._journal(
            "evaluation_complete",
            lane="official",
            task_id=task.slug,
            binding_id="outer",
            candidate_sha256=snapshot.sha256,
            verdict=verdict.as_dict(),
        )
        self._log_evaluation("official", verdict, actor_id="closeout", episode=0, cache_hit=False)
        return verdict

    def best_local_proved(self, task_id: str) -> CandidateSnapshot | None:
        with self._state_lock:
            return self._local_proved.get(task_id)

    def materialize_snapshot(self, task_id: str, sha256: str, destination: Path) -> None:
        """Copy broker-owned immutable bytes to a runner-owned candidate path."""

        snapshot = self.snapshot_store.load(task_id=task_id, sha256=sha256)
        atomic_write_bytes(destination, snapshot.payload, mode=0o600)

    def formal_query(self, binding: WorkerBinding, request: Mapping[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        task = binding.task
        call_number = self._increment(task.slug, "query_calls")
        command = str(request.get("command") or "").strip().lower()
        if command not in {"search", "decl", "check", "type", "axioms", "deps"}:
            raise BrokerError("unsupported formal_query command")
        if call_number > self.config.formal_tools_query_calls_per_task:
            result = {
                "status": "scout_call_budget_exhausted",
                "call_number": call_number,
                "advisory_only": True,
            }
            self._log_query(task.slug, binding.actor_id, command, result, started, probes=0, cache_hits=0)
            return result
        query_values = request.get("query")
        if not isinstance(query_values, list):
            query_values = []
        query = [sanitize_public_text(str(value), limit=400) for value in query_values[:12]]
        query_text = " ".join(query).strip()
        limit = max(1, min(_integer(request.get("limit"), 12), 24))
        timeout = max(10, min(_integer(request.get("timeout"), 30), 120))
        guarded = {task.theorem_name, task.problem_id, task.slug}
        self._query_metrics.value = {"probes": 0, "cache_hits": 0}

        if command == "search":
            public = self._search_public(binding.workspace, query, limit=limit)
            matches = self.declaration_index.search(query_text, limit=limit, guarded_names=guarded)
            result = {
                "status": "ok" if self.declaration_index.info.compatible else "index_unavailable",
                "query_kind": "search",
                "public_results": public,
                "mathlib_matches": matches,
                "search_corpus_revision": self.declaration_index.info.mathlib_revision,
                "index_contract": self.declaration_index.info.public_dict(),
                "hint": "Index names are advisory; verify a name with ./formal_query check <name>.",
            }
        elif command == "decl":
            matches = self.declaration_index.search(query_text, limit=limit, guarded_names=guarded)
            result = {
                "status": "searched" if self.declaration_index.info.compatible else "index_unavailable",
                "query_kind": "decl",
                "matches": matches,
                "result_count": len(matches),
                "search_corpus_revision": self.declaration_index.info.mathlib_revision,
                "index_contract": self.declaration_index.info.public_dict(),
                "hint": "Verify any candidate name with ./formal_query check <name>.",
            }
        elif command == "deps":
            exact = self.declaration_index.search(query_text, limit=6, guarded_names=guarded)
            related_query = query_text.replace(".", " ").replace("_", " ")
            related = self.declaration_index.search(related_query, limit=10, guarded_names=guarded)
            result = {
                "status": "searched" if self.declaration_index.info.compatible else "index_unavailable",
                "query_kind": "deps",
                "query": query_text,
                "exact_matches": exact,
                "related_declarations": related,
                "semantics": "index_related_premises_not_dependency_graph",
                "hint": "Verify related declarations individually with check.",
            }
        else:
            result = self._kernel_query(binding, command, query, request, timeout=timeout, guarded=guarded)
        result.update(
            {
                "advisory_only": True,
                "final_verify_required": True,
                "call_number": call_number,
                "surface_version": self.config.formal_tools_version,
            }
        )
        metrics = getattr(self._query_metrics, "value", None)
        probes = int(metrics.get("probes") or 0) if isinstance(metrics, Mapping) else 0
        cache_hits = int(metrics.get("cache_hits") or 0) if isinstance(metrics, Mapping) else 0
        self._query_metrics.value = None
        self._log_query(task.slug, binding.actor_id, command, result, started, probes=probes, cache_hits=cache_hits)
        return result

    def _kernel_query(
        self,
        binding: WorkerBinding,
        command: str,
        query: list[str],
        request: Mapping[str, Any],
        *,
        timeout: int,
        guarded: set[str],
    ) -> dict[str, Any]:
        task = binding.task
        query_text = " ".join(query).strip()
        snippet = request.get("snippet")
        tactics = request.get("tactics")
        if any(name and _contains_guarded(query_text, name) for name in guarded):
            return {"status": "guarded_declaration_refused", "query_kind": command}
        imports = "\n".join(match.group(0).strip() for match in _IMPORT_LINE.finditer(task.baseline_code))
        if command == "check" and isinstance(snippet, str) and snippet.strip():
            code = snippet.strip()[:8_000]
            if any(name and _contains_guarded(code, name) for name in guarded):
                return {"status": "guarded_declaration_refused", "query_kind": "check_snippet"}
            probe = self._probe(binding, f"{imports}\n\n{code}\n", timeout=timeout)
            return {
                "status": probe.get("status"),
                "query_kind": "check_snippet",
                "contains_sorry": bool(re.search(r"(?<![A-Za-z0-9_])sorry(?![A-Za-z0-9_])", code)),
                "diagnostics": probe.get("diagnostics", []),
                "error_messages": probe.get("error_messages", []),
                "elapsed_ms": probe.get("elapsed_ms"),
                "cache_hit": probe.get("cache_hit", False),
            }
        if command == "check" and isinstance(tactics, str) and tactics.strip():
            header = tactics.strip()[:4_000]
            if any(name and _contains_guarded(header, name) for name in guarded):
                return {"status": "guarded_declaration_refused", "query_kind": "check_tactics"}
            raw_tactics = request.get("tactic")
            portfolio = [str(value).strip() for value in raw_tactics[:12]] if isinstance(raw_tactics, list) else []
            portfolio = [item[:500] for item in portfolio if item] or list(_DEFAULT_TACTICS)
            attempts: list[dict[str, Any]] = []
            closing: list[str] = []
            for tactic in portfolio:
                if re.search(r"(?<![A-Za-z0-9_])(?:sorry|admit)(?![A-Za-z0-9_])", tactic):
                    attempts.append(
                        {
                            "tactic": tactic,
                            "outcome": "placeholder_refused",
                            "diagnostics": [],
                            "cache_hit": False,
                        }
                    )
                    continue
                probe = self._probe(binding, f"{imports}\n\n{header} := by\n  {tactic}\n", timeout=timeout)
                closed = (
                    probe.get("status") == "elaborated"
                    and probe.get("is_valid_no_sorry") is True
                )
                attempts.append(
                    {
                        "tactic": tactic,
                        "outcome": "closed" if closed else str(probe.get("status") or "failed"),
                        "diagnostics": probe.get("diagnostics", [])[:4],
                        "elapsed_ms": probe.get("elapsed_ms"),
                        "cache_hit": probe.get("cache_hit", False),
                    }
                )
                if closed:
                    closing.append(tactic)
                    break
                if probe.get("status") == "probe_budget_exhausted":
                    break
            return {
                "status": "closed" if closing else "not_closed",
                "query_kind": "check_tactics",
                "closing_tactics": closing,
                "attempts": attempts,
                "note": "Each attempt is a separate backend probe and consumes the task probe budget.",
            }
        if command == "axioms":
            name = query[0].strip() if query else ""
            if not _LEAN_NAME.fullmatch(name):
                return {"status": "invalid_query", "query_kind": "axioms"}
            try:
                candidate = read_regular_bytes(
                    binding.workspace / "result.lean",
                    trusted_root=binding.workspace,
                    max_bytes=self.config.formal_tools_max_candidate_bytes,
                ).decode("utf-8")
            except (OSError, UnicodeError) as exc:
                return {"status": "candidate_unavailable", "message": sanitize_public_text(str(exc))}
            probe = self._probe(binding, f"{candidate}\n\n#print axioms {name}\n", timeout=timeout)
            return {
                "status": probe.get("status"),
                "query_kind": "axioms",
                "query": name,
                "diagnostics": probe.get("diagnostics", []),
                "error_messages": probe.get("error_messages", []),
                "elapsed_ms": probe.get("elapsed_ms"),
                "cache_hit": probe.get("cache_hit", False),
                "candidate_context_included": True,
            }
        if not query_text:
            return {"status": "empty_query", "query_kind": command}
        if command == "check" and all(_LEAN_NAME.fullmatch(name) for name in query[:8]):
            code = "\n".join(f"#check {name}" for name in query[:8])
        else:
            code = f"#check {query_text}"
        probe = self._probe(binding, f"{imports}\n\n{code}\n", timeout=timeout)
        return {
            "status": probe.get("status"),
            "query_kind": command,
            "query": query_text,
            "diagnostics": probe.get("diagnostics", []),
            "error_messages": probe.get("error_messages", []),
            "elapsed_ms": probe.get("elapsed_ms"),
            "cache_hit": probe.get("cache_hit", False),
        }

    def _probe(self, binding: WorkerBinding, source: str, *, timeout: int) -> dict[str, Any]:
        task = binding.task
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        key = (binding.binding_id, digest)
        with self._state_lock:
            cached = self._probe_cache.get(key)
        if cached is not None:
            self._increment(task.slug, "query_cache_hits")
            self._record_query_metric("cache_hits", 1)
            return {**cached, "cache_hit": True}
        if not self._budget_available(
            task.slug,
            "query_backend_probes",
            self.config.formal_tools_query_backend_probes_per_task,
        ):
            return {
                "status": "probe_budget_exhausted",
                "cache_hit": False,
            }
        if time.monotonic() >= self.local_admission_deadline:
            return {"status": "probe_admission_closed", "cache_hit": False}
        probe_number: int | None = None
        probe_context: dict[str, Any] | None = None
        probe_deadline = min(
            self.local_admission_deadline,
            self.solver_deadline,
            time.monotonic() + timeout + 30.0,
        )
        try:
            with self._admission.acquire("formal_query", deadline=probe_deadline) as waited_ms:
                probe_number = self._reserve_budget(
                    task.slug,
                    "query_backend_probes",
                    self.config.formal_tools_query_backend_probes_per_task,
                )
                if probe_number is None:
                    result = {"status": "probe_budget_exhausted"}
                else:
                    self._record_query_metric("probes", 1)
                    probe_context = {
                        "lane": "formal_query",
                        "task_id": task.slug,
                        "probe_sha256": digest,
                        "submitted_jobs": 0,
                    }
                    self._context.value = probe_context
                    result = self.evaluator.probe(
                        task,
                        source,
                        timeout_seconds=timeout,
                        deadline_monotonic=probe_deadline,
                    )
        except BrokerError:
            result = {"status": "probe_admission_closed", "error_kind": "admission_timeout"}
            waited_ms = 0
        except Exception as exc:
            result = {"status": "probe_transport_error", "error_kind": type(exc).__name__}
            waited_ms = 0
        finally:
            self._context.value = None
        if (
            probe_number is not None
            and probe_context is not None
            and probe_context.get("submitted_jobs") == 0
            and str(result.get("status") or "") == "probe_admission_closed"
        ):
            self._release_budget(
                task.slug,
                "query_backend_probes",
                serial=probe_number,
            )
            self._record_query_metric("probes", -1)
            probe_number = None
        safe = _sanitize_probe_result(result)
        safe.update(
            {
                "admission_wait_ms": waited_ms,
                "cache_hit": False,
            }
        )
        if probe_number is not None:
            safe["probe_number"] = probe_number
        if safe.get("status") in {"elaborated", "elab_failed"}:
            with self._state_lock:
                self._probe_cache[key] = dict(safe)
        self._journal(
            "formal_query_probe_complete",
            task_id=task.slug,
            binding_id=binding.binding_id,
            probe_sha256=digest,
            probe_number=probe_number,
            status=safe.get("status"),
            admission_wait_ms=waited_ms,
        )
        return safe

    def _search_public(self, workspace: Path, terms: list[str], *, limit: int) -> list[dict[str, Any]]:
        candidates = [workspace / "problem.md", workspace / "result.lean"]
        candidates.extend(sorted((workspace / "baseline").glob("*.lean")))
        lowered_terms = [term.lower() for term in terms if term]
        rows: list[dict[str, Any]] = []
        for path in candidates:
            try:
                text = read_regular_bytes(
                    path,
                    trusted_root=workspace,
                    max_bytes=self.config.formal_tools_max_candidate_bytes,
                ).decode("utf-8", errors="replace")
            except OSError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                lowered = line.lower()
                if lowered_terms and not all(term in lowered for term in lowered_terms):
                    continue
                relative = path.relative_to(workspace)
                rows.append(
                    {
                        "file": str(relative),
                        "line": line_number,
                        "text": sanitize_public_text(line.strip(), limit=260),
                    }
                )
                if len(rows) >= limit:
                    return rows
        return rows

    def _increment(self, task_id: str, kind: str) -> int:
        with self._budget_lock:
            with self._state_lock:
                key = (task_id, kind)
                value = self._counts.get(key, 0) + 1
                self._counts[key] = value
            self._journal("budget_counter", task_id=task_id, counter=kind, value=value)
        return value

    def _count(self, task_id: str, kind: str) -> int:
        with self._state_lock:
            return self._counts.get((task_id, kind), 0)

    def _record_query_metric(self, kind: str, delta: int) -> None:
        metrics = getattr(self._query_metrics, "value", None)
        if isinstance(metrics, dict):
            metrics[kind] = max(0, int(metrics.get(kind) or 0) + int(delta))

    def _budget_available(self, task_id: str, kind: str, limit: int) -> bool:
        with self._budget_lock:
            with self._state_lock:
                return self._counts.get((task_id, kind), 0) < int(limit)

    def _reserve_budget(self, task_id: str, kind: str, limit: int) -> int | None:
        with self._budget_lock:
            with self._state_lock:
                key = (task_id, kind)
                current = self._counts.get(key, 0)
                if current >= int(limit):
                    return None
                value = current + 1
                serial = self._budget_serials.get(key, 0) + 1
                self._counts[key] = value
                self._budget_serials[key] = serial
            self._journal(
                "budget_counter",
                task_id=task_id,
                counter=kind,
                value=value,
                serial=serial,
                operation="reserve",
            )
            return serial

    def _release_budget(self, task_id: str, kind: str, *, serial: int) -> None:
        with self._budget_lock:
            with self._state_lock:
                key = (task_id, kind)
                value = max(0, self._counts.get(key, 0) - 1)
                self._counts[key] = value
                self._budget_serials[key] = max(
                    self._budget_serials.get(key, 0),
                    int(serial),
                )
            self._journal(
                "budget_counter",
                task_id=task_id,
                counter=kind,
                value=value,
                serial=serial,
                operation="release_unadmitted",
            )

    def _journal(self, event: str, **payload: Any) -> None:
        append_jsonl(
            self.journal_path,
            {
                "schema_version": BROKER_JOURNAL_SCHEMA,
                "ts_ms": int(time.time() * 1_000),
                "event": event,
                **payload,
            },
            lock=self._journal_lock,
            mode=0o600,
        )

    def _log_evaluation(
        self,
        lane: str,
        verdict: Verdict,
        *,
        actor_id: str,
        episode: int,
        cache_hit: bool,
    ) -> None:
        path = (
            self.telemetry_root / "official_verdicts.jsonl"
            if lane == "official"
            else self.telemetry_root / "agent_local_evaluations.jsonl"
        )
        response = verdict.response
        append_jsonl(
            path,
            {
                "schema_version": BROKER_TELEMETRY_SCHEMA,
                "ts_ms": int(time.time() * 1_000),
                "lane": lane,
                "task_id": verdict.task_id,
                "actor_id": actor_id,
                "episode": episode,
                "candidate_sha256": response.get("candidate_sha256"),
                "call_number": response.get("call_number"),
                "backend_job_number": response.get("backend_job_number"),
                "status": verdict.status,
                "score": verdict.score if lane == "official" else 0.0,
                "elapsed_ms": int(verdict.elapsed_seconds * 1_000),
                "admission_wait_ms": response.get("admission_wait_ms"),
                "cache_hit": cache_hit,
                "diagnostic_category": _diagnostic_category(verdict),
                "timeout": verdict.status in {"EVALUATOR_TIMEOUT", "OUT_OF_HORIZON", "EXECUTION_TIMEOUT"},
                "cancel_outcome": response.get("cancel_requested") or response.get("terminal_reason"),
            },
            lock=self._telemetry_lock,
        )

    def _log_query(
        self,
        task_id: str,
        actor_id: str,
        command: str,
        result: Mapping[str, Any],
        started: float,
        *,
        probes: int,
        cache_hits: int,
    ) -> None:
        append_jsonl(
            self.telemetry_root / "formal_query_calls.jsonl",
            {
                "schema_version": BROKER_TELEMETRY_SCHEMA,
                "ts_ms": int(time.time() * 1_000),
                "task_id": task_id,
                "actor_id": actor_id,
                "command": command,
                "call_number": result.get("call_number"),
                "status": result.get("status"),
                "elapsed_ms": int((time.monotonic() - started) * 1_000),
                "backend_probe_count": probes,
                "cache_hit_count": cache_hits,
                "diagnostic_category": _query_category(result),
            },
            lock=self._telemetry_lock,
        )

    def _observe_evaluator_lifecycle(self, event: str, payload: Mapping[str, Any]) -> None:
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            return
        context = getattr(self._context, "value", None)
        if event == "submitted" and isinstance(context, dict):
            context["submitted_jobs"] = int(context.get("submitted_jobs") or 0) + 1
        row = dict(context) if isinstance(context, Mapping) else {}
        with self._state_lock:
            if event == "submitted":
                self._active_jobs.add(job_id)
            elif event == "settled":
                self._active_jobs.discard(job_id)
        self._journal("judge_job", lifecycle_event=event, job_id=job_id, **row)

    def _recover_journal(self) -> None:
        if not self.journal_path.is_file():
            return
        active: set[str] = set()
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, Mapping) or row.get("schema_version") != BROKER_JOURNAL_SCHEMA:
                continue
            if row.get("event") == "budget_counter":
                task_id = str(row.get("task_id") or "")
                counter = str(row.get("counter") or "")
                value = _integer(row.get("value"), 0)
                if task_id and counter:
                    key = (task_id, counter)
                    self._counts[key] = max(0, value)
                    self._budget_serials[key] = max(
                        self._budget_serials.get(key, 0),
                        _integer(row.get("serial"), 0),
                    )
            elif row.get("event") == "judge_job":
                job_id = str(row.get("job_id") or "")
                if row.get("lifecycle_event") == "submitted" and job_id:
                    active.add(job_id)
                elif row.get("lifecycle_event") == "settled":
                    active.discard(job_id)
            elif row.get("event") == "evaluation_complete":
                verdict_payload = row.get("verdict")
                if not isinstance(verdict_payload, Mapping):
                    continue
                try:
                    verdict = _verdict_from_dict(verdict_payload)
                except (TypeError, ValueError):
                    continue
                lane = str(row.get("lane") or "")
                binding_id = str(row.get("binding_id") or "")
                sha = str(row.get("candidate_sha256") or "")
                task_id = str(row.get("task_id") or "")
                if lane and binding_id and sha and task_id:
                    if lane == "official" or row.get("cache_eligible") is not False:
                        self._evaluation_cache[(lane, binding_id, task_id, sha)] = verdict
                    if lane == "agent_local" and verdict.status == "PROVED":
                        try:
                            snapshot = self.snapshot_store.load(
                                task_id=task_id,
                                sha256=sha,
                                captured_at_monotonic=0.0,
                            )
                        except OSError:
                            continue
                        prior = self._local_proved.get(task_id)
                        if prior is None or snapshot.captured_at_monotonic >= prior.captured_at_monotonic:
                            self._local_proved[task_id] = snapshot
        self._active_jobs = active

    def _cancel_recovered_jobs(self) -> None:
        cancel = getattr(self.evaluator, "cancel_job", None)
        if not callable(cancel):
            return
        with self._state_lock:
            pending = sorted(self._active_jobs)
        for job_id in pending:
            try:
                response, error = cancel(job_id)
                terminal = bool(response) and _terminal(response)
            except Exception as exc:
                error = type(exc).__name__
                terminal = False
            if terminal:
                with self._state_lock:
                    self._active_jobs.discard(job_id)
                self._journal("judge_job", lifecycle_event="settled", job_id=job_id, recovery=True)
            else:
                self._journal(
                    "recovery_cancel_unsettled",
                    job_id=job_id,
                    error_kind=sanitize_public_text(str(error or "unsettled"), limit=120),
                )


def _resolve_decl_index(config: ExperimentConfig) -> Path | None:
    raw = (
        os.environ.get("CONTEXTSWARM_MINI_DECL_INDEX", "").strip()
        or os.environ.get("MINI_SWARM_DECL_INDEX", "").strip()
        or config.formal_tools_decl_index.strip()
    )
    return config.resolve_runtime_path(raw) if raw else None


def _effective_decl_index_sha256(config: ExperimentConfig) -> str:
    return (
        os.environ.get("CONTEXTSWARM_MINI_DECL_INDEX_SHA256", "").strip().lower()
        or config.formal_tools_decl_index_sha256
    )


def _effective_mathlib_revision(config: ExperimentConfig) -> str:
    return (
        os.environ.get("CONTEXTSWARM_MINI_MATHLIB_REVISION", "").strip()
        or config.formal_tools_mathlib_revision
    )


def _socket_path(run_dir: Path) -> Path:
    preferred = run_dir / ".broker_private" / "evaluator.sock"
    if len(os.fsencode(preferred)) < 100:
        return preferred
    digest = hashlib.sha256(str(run_dir).encode()).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"contextswarm-mini-{digest}.sock"


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _contains_guarded(text: str, name: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_']){re.escape(name)}(?![A-Za-z0-9_'])", text))


def _clone_verdict(verdict: Verdict) -> Verdict:
    return Verdict(
        verdict.task_id,
        verdict.status,
        float(verdict.score),
        float(verdict.elapsed_seconds),
        json.loads(json.dumps(verdict.response)),
        verdict.error,
    )


def _verdict_from_dict(payload: Mapping[str, Any]) -> Verdict:
    response = payload.get("response")
    return Verdict(
        task_id=str(payload["task_id"]),
        status=str(payload["status"]),
        score=float(payload.get("score") or 0.0),
        elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
        response=dict(response) if isinstance(response, Mapping) else {},
        error=str(payload["error"]) if payload.get("error") is not None else None,
    )


def _worker_verdict(verdict: Verdict) -> dict[str, Any]:
    response = verdict.response
    return {
        "status": verdict.status,
        "candidate_sha256": response.get("candidate_sha256"),
        "call_number": response.get("call_number"),
        "backend_job_number": response.get("backend_job_number"),
        "elapsed_ms": int(verdict.elapsed_seconds * 1_000),
        "cache_hit": response.get("cache_hit", False),
        "diagnostics": response.get("probe_diagnostics", []),
        "error_messages": response.get("error_message", []),
        "reason": response.get("reason") or verdict.error,
        "advisory_only": True,
        "official_score_eligible": False,
        "note": "Agent-local feedback never writes the official scoreboard; outer closeout re-evaluates frozen bytes.",
    }


def _sanitize_probe_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "status",
        "is_valid_no_sorry",
        "is_valid_with_sorry",
        "error_kind",
        "terminal_reason",
        "elapsed_ms",
        "cache_hit",
        "queue_wait_ms",
        "execution_ms",
    ):
        value = payload.get(key)
        if isinstance(value, (str, bool, int, float)) and not (
            isinstance(value, float) and not value == value
        ):
            result[key] = value
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, list):
        safe_diagnostics: list[dict[str, Any]] = []
        for item in diagnostics[:24]:
            if not isinstance(item, Mapping):
                continue
            safe_diagnostics.append(
                {
                    "severity": sanitize_public_text(str(item.get("severity") or "information"), limit=40),
                    "message": sanitize_public_text(str(item.get("message") or item.get("data") or "")),
                    "line": item.get("line") if isinstance(item.get("line"), int) else None,
                    "column": item.get("column") if isinstance(item.get("column"), int) else None,
                }
            )
        result["diagnostics"] = safe_diagnostics
    errors = payload.get("error_messages")
    if isinstance(errors, list):
        result["error_messages"] = [sanitize_public_text(str(item)) for item in errors[:24]]
    elif isinstance(payload.get("error_message"), str):
        result["error_messages"] = [sanitize_public_text(str(payload["error_message"]))]
    return result


def _diagnostic_category(verdict: Verdict) -> str:
    value = " ".join(
        (
            verdict.status,
            verdict.error or "",
            str(verdict.response.get("error_kind") or ""),
            str(verdict.response.get("reason") or ""),
        )
    ).lower()
    if "timeout" in value or "horizon" in value:
        return "timeout"
    if "overload" in value or "capacity" in value:
        return "overload"
    if "contract" in value or "cheating" in value:
        return "contract"
    if verdict.status == "PROVED":
        return "proved"
    if "network" in value or "transport" in value or "evaluator_error" in value:
        return "infrastructure"
    return "proof_diagnostic"


def _query_category(result: Mapping[str, Any]) -> str:
    status = str(result.get("status") or "").lower()
    if "budget" in status:
        return "budget"
    if "timeout" in status or "closed" in status:
        return "timeout"
    if "unavailable" in status or "transport" in status or "failed" in status:
        return "infrastructure"
    if "refused" in status:
        return "guard"
    return "ok"


__all__ = [
    "BROKER_REQUEST_SCHEMA",
    "BrokerError",
    "EvaluatorBroker",
    "WorkerBinding",
]
