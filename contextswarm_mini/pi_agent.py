"""Bounded Pi RPC launcher with NuRouter/AISW environment wiring."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import shutil
import subprocess
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit
import uuid

from .config import ExperimentConfig
from .models import AgentResult


_STDERR_LINE_LIMIT_BYTES = 256 * 1024
_FILE_TOOLS = ("read", "edit", "write", "grep", "find", "ls")
_CPS_SHARED_TOOLS = ("cps_search", "cps_publish", "cps_actors")
_CPS_DIRECT_TOOLS = ("cps_inbox", "cps_send", "cps_ack")
_SOLVER_EXTENSION_NAME = "pi_solver_tools.mjs"
_FAST_MODE_EXTENSION_NAME = "pi_fast_mode.mjs"
_BROKER_ENVIRONMENT_KEYS = frozenset(
    {
        "CONTEXTSWARM_JUDGE_URL",
        "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS",
    }
)
_BROKER_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_SOLVER_SYSTEM_PROMPT = """You are a bounded formal-proof construction worker, not a general-purpose coding agent.
Work only on the assigned result.lean and use only the explicitly provided tools.
Do not execute shell commands, spawn background or parallel processes, run a local
Lean/verifier/proof-search service, install or download software, or make raw network
requests. All dynamic Lean verification must use the runner-provided judge_check tool.
If that tool is busy or unavailable, continue static proof reasoning or leave the best
candidate for the runner; never create a local or raw-network fallback. The user prompt
defines the assigned proof task and, when present, the controlled CPS protocol."""
_ISOLATED_SYSTEM_PROMPT = """You are a read-only allocation decision component in a bounded experiment.
Use only the snapshot in the user prompt. You have no tools and must not inspect files,
execute commands, spawn processes, use the network, or change run state. Return only the
decision format requested by the user prompt."""
_CPS_ENVIRONMENT_KEYS = frozenset(
    {
        "CONTEXTSWARM_CPS_DB",
        "CONTEXTSWARM_ACTORS_FILE",
        "CONTEXTSWARM_HORIZON_EPOCH_MS",
        "CONTEXTSWARM_ASSIGNMENT_FILE",
        "CONTEXTSWARM_BEST_CANDIDATE_FILE",
        "CONTEXTSWARM_TASK_ROOT",
    }
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass
class PiAgent:
    config: ExperimentConfig
    trace_path: Path | None = None
    _trace_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def binary(self) -> str:
        configured = self.config.pi_binary.strip() or os.environ.get("MINI_SWARM_PI_BIN", "").strip()
        if configured:
            return str(self.config.resolve_runtime_path(configured))
        if self.config.aisw_enabled:
            configured_aisw = str(self.config.resolve_runtime_path(self.config.aisw_binary))
            if Path(configured_aisw).is_file():
                return configured_aisw
            discovered = shutil.which("nurouter") or shutil.which("aisw")
            if discovered:
                return discovered
            return configured_aisw
        return os.environ.get("PI_BIN", "pi")

    def command(
        self,
        *,
        session_dir: Path | None = None,
        session_id: str | None = None,
        isolated: bool = False,
    ) -> list[str]:
        command = [
            self.binary(),
            "--mode",
            "rpc",
            "--approve",
            "--thinking",
            self.config.thinking,
            "--system-prompt",
            _ISOLATED_SYSTEM_PROMPT if isolated else _SOLVER_SYSTEM_PROMPT,
        ]
        if session_dir is not None:
            command.extend(["--session-dir", str(session_dir)])
        if session_id:
            command.extend(["--session-id", session_id])
        if isolated:
            command.extend(
                [
                    "--no-tools",
                    "--no-context-files",
                    "--no-skills",
                    "--no-prompt-templates",
                    "--no-extensions",
                ]
            )
        else:
            command.extend(
                [
                    "--no-context-files",
                    "--no-skills",
                    "--no-prompt-templates",
                    "--no-extensions",
                    "--tools",
                    ",".join(self.solver_tools()),
                ]
            )
            for _role, extension_path in self._trusted_extensions():
                command.extend(["--extension", str(extension_path)])
        if self.config.model:
            command.extend(["--model", self.config.model])
        return command

    def _trusted_extensions(self) -> tuple[tuple[str, Path], ...]:
        """Resolve the complete explicit extension allowlist or fail closed."""

        solver_extension = Path(__file__).with_name(_SOLVER_EXTENSION_NAME).resolve()
        if not solver_extension.is_file():
            raise ValueError(
                f"controlled Pi solver extension is missing: {solver_extension}"
            )
        extensions: list[tuple[str, Path]] = [
            ("solver_capabilities", solver_extension),
        ]
        if self.config.fast_mode:
            configured = self.config.pi_extension.strip()
            if not configured:
                raise ValueError("fast mode requires the bundled trusted Pi extension")
            expected = Path(__file__).with_name(_FAST_MODE_EXTENSION_NAME).resolve()
            configured_path = self.config.resolve_runtime_path(configured).resolve()
            if configured_path != expected:
                raise ValueError(
                    "fast mode rejects non-bundled Pi extensions; "
                    f"expected {_FAST_MODE_EXTENSION_NAME}"
                )
            if not expected.is_file():
                raise ValueError(f"trusted fast-mode Pi extension is missing: {expected}")
            extensions.append(("fast_mode_provider_policy", expected))
        return tuple(extensions)

    def trusted_extension_declaration(self) -> dict[str, Any]:
        """Return a value-free, hash-bound declaration of explicit extensions."""

        rows = []
        for role, path in self._trusted_extensions():
            rows.append(
                {
                    "role": role,
                    "name": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        return {
            "schema_version": "contextswarm_pi_extension_policy_v1",
            "policy": "bundled_explicit_only",
            "discovery_disabled": True,
            "extensions": rows,
        }

    def solver_tools(self) -> tuple[str, ...]:
        tools = [*_FILE_TOOLS, "judge_check"]
        if self.config.uses_cps:
            tools.extend(_CPS_SHARED_TOOLS)
            tools.extend(_CPS_DIRECT_TOOLS)
        return tuple(tools)

    def environment(
        self,
        *,
        task_id: str,
        actor_id: str,
        workdir: Path,
        extra_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        env = dict(os.environ)
        # The supervisor alone owns raw Judge credentials and endpoints.  A
        # session-scoped broker capability may be injected below via extra_env.
        env.pop("LEAN_AUTH_TOKEN", None)
        env.pop("CONTEXTSWARM_JUDGE_URL", None)
        env.pop("CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL", None)
        env.pop("CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS", None)
        # A notebook/operator shell may still carry variables from a previous
        # CPS run.  Baselines inherit the ordinary process environment, but
        # never an implicit communication surface; CPS call sites explicitly
        # add the current run's values through ``extra_env`` below.
        for key in tuple(env):
            if key in _CPS_ENVIRONMENT_KEYS or key.startswith("CONTEXTSWARM_CPS_"):
                env.pop(key, None)
        private_tmp = workdir / ".tmp"
        private_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(private_tmp, 0o700)
        env.update(
            {
                "PI_BIN": self.binary(),
                "EXPERIMENT_PI_BINARY": self.binary(),
                "CONTEXTSWARM_TASK_ID": task_id,
                "CONTEXTSWARM_ACTOR_ID": actor_id,
                "CONTEXTSWARM_WORKDIR": str(workdir),
                "CONTEXTSWARM_EXPERIMENT_SEED": str(self.config.seed),
                "TMPDIR": str(private_tmp),
                "AISW_LEASE_WAIT_SECONDS": str(self.config.aisw_lease_wait_seconds),
                "AISW_LEASE_RETRY_INTERVAL_SECONDS": str(self.config.aisw_lease_retry_interval_seconds),
            }
        )
        existing_pythonpath = env.get("PYTHONPATH", "")
        repo_path = str(self.config.repo_root)
        env["PYTHONPATH"] = repo_path if not existing_pythonpath else f"{repo_path}{os.pathsep}{existing_pythonpath}"
        if self.config.aisw_enabled:
            env["AISW_HOME"] = env.get("AISW_HOME", "/run/contextswarm-aisw")
            env["CONTEXTSWARM_AISW_PRIVATE_HOME_REQUIRED"] = "1"
            env["AISW_DISABLE_LOCAL_FALLBACK"] = "1"
            env["CONTEXTSWARM_REAL_PI_BINARY"] = env.get("CONTEXTSWARM_REAL_PI_BINARY", "/usr/local/bin/pi")
            node_config = os.environ.get("MINI_SWARM_AISW_NODE_CONFIG", "").strip() or self.config.aisw_node_config.strip()
            if node_config:
                env["AISW_NODE_CONFIG"] = str(self.config.resolve_runtime_path(node_config))
                env["CONTEXTSWARM_AISW_NODE_CONFIG"] = env["AISW_NODE_CONFIG"]
            if self.config.aisw_coordinator_url:
                env["AISW_COORDINATOR_URL"] = self.config.aisw_coordinator_url
            if self.config.aisw_account:
                env["AISW_PI_ACCOUNT"] = self.config.aisw_account
            if self.config.aisw_group:
                env["AISW_PI_GROUP"] = self.config.aisw_group
        if self.config.fast_mode and self.trace_path is not None:
            env["CONTEXTSWARM_PI_FAST_MODE_EVIDENCE_PATH"] = str(
                self.trace_path.with_name("pi_fast_mode_provider_requests.jsonl")
            )
        if extra_env:
            controlled = {str(key): str(value) for key, value in extra_env.items()}
            if set(controlled) != _BROKER_ENVIRONMENT_KEYS:
                raise ValueError(
                    "unsupported solver environment capability; expected only the controlled broker"
                )
            broker_url = controlled["CONTEXTSWARM_JUDGE_URL"].strip()
            try:
                parsed_broker = urlsplit(broker_url)
                broker_port = parsed_broker.port
            except ValueError as exc:
                raise ValueError("invalid controlled broker capability") from exc
            token = parsed_broker.path.removeprefix("/")
            if (
                parsed_broker.scheme != "http"
                or parsed_broker.hostname not in {"127.0.0.1", "localhost", "::1"}
                or broker_port is None
                or parsed_broker.username is not None
                or parsed_broker.password is not None
                or parsed_broker.query
                or parsed_broker.fragment
                or parsed_broker.path != f"/{token}"
                or _BROKER_TOKEN_PATTERN.fullmatch(token) is None
            ):
                raise ValueError("invalid controlled broker capability")
            raw_deadline = controlled[
                "CONTEXTSWARM_BROKER_DEADLINE_EPOCH_MS"
            ].strip()
            if not raw_deadline.isascii() or not raw_deadline.isdigit() or int(raw_deadline) <= 0:
                raise ValueError("invalid controlled broker deadline")
            env.update(controlled)
        return env

    def run(
        self,
        *,
        task_id: str,
        actor_id: str,
        episode: int,
        prompt: str,
        workdir: Path,
        extra_env: Mapping[str, str] | None = None,
        deadline_monotonic: float | None = None,
        cancel_event: threading.Event | None = None,
        isolated: bool = False,
    ) -> AgentResult:
        started = now_iso()
        command = self.command(isolated=isolated)
        output = _TailBuffer(6_000)
        errors = _TailBuffer(4_000)
        events = 0
        timed_out = False
        cancelled = False
        returncode = 1
        process: subprocess.Popen[bytes] | None = None
        trace_handle = None
        selector: selectors.BaseSelector | None = None
        settled_seen = False
        agent_end_seen = False
        prompt_rejected = False
        pending_assistant_error = ""
        retry_final_error = ""
        assistant_streamed = False
        request_id = f"contextswarm-{uuid.uuid4().hex}"
        session_id = _session_id(self.trace_path, actor_id, episode)
        session_root = workdir / ".pi" / "sessions"
        session_dir = session_root / session_id
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stderr_overflow = False
        index_path: Path | None = None

        def consume_stdout_line(line: str) -> None:
            nonlocal events
            nonlocal settled_seen
            nonlocal agent_end_seen
            nonlocal prompt_rejected
            nonlocal pending_assistant_error
            nonlocal retry_final_error
            nonlocal assistant_streamed

            payload = _parse_json_line(line)
            if payload is None:
                value = line.strip()
                if value:
                    errors.append(f"Pi emitted non-JSON RPC output: {_redact_sensitive_text(value)}")
                return
            events += 1
            event_type = str(payload.get("type") or payload.get("event") or "unknown")

            if event_type == "message_start" and _message_role(payload) == "assistant":
                assistant_streamed = False
            rendered = _event_text(payload)
            if event_type == "message_update" and rendered:
                assistant_streamed = True
            elif event_type == "message_end" and assistant_streamed:
                # The streamed deltas are already in the rolling tail. Avoid
                # duplicating the authoritative final message.
                rendered = ""
            if rendered:
                output.append(_redact_sensitive_text(rendered), separator="")

            outcome = _assistant_outcome(payload)
            if outcome is not None:
                stop_reason, error_message = outcome
                if stop_reason == "error":
                    pending_assistant_error = error_message or "Pi assistant stopped with an error"
                elif stop_reason in {"stop", "toolUse"}:
                    pending_assistant_error = ""
                    retry_final_error = ""

            if event_type == "auto_retry_end":
                if payload.get("success") is False:
                    retry_final_error = _text_field(payload, "finalError", "errorMessage")
                    if retry_final_error and not pending_assistant_error:
                        pending_assistant_error = retry_final_error
                elif payload.get("success") is True:
                    retry_final_error = ""
            if event_type == "agent_end":
                agent_end_seen = True
            elif event_type == "agent_settled":
                settled_seen = True
            elif (
                event_type == "response"
                and payload.get("id") == request_id
                and payload.get("success") is False
            ):
                prompt_rejected = True
                pending_assistant_error = _text_field(payload, "error", "message") or "Pi RPC prompt rejected"

            diagnostic = _event_error(payload)
            if diagnostic:
                errors.append(f"{event_type}: {_redact_sensitive_text(diagnostic)}")
            if trace_handle is not None:
                row = {
                    "at": now_iso(),
                    "task_id": task_id,
                    "actor_id": actor_id,
                    "episode": episode,
                    "session_id": session_id,
                    "type": event_type,
                    "has_text": bool(rendered),
                    "text_chars": len(rendered),
                    **_usage_fields(payload),
                    **_event_trace_fields(payload),
                }
                with self._trace_lock:
                    trace_handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                    trace_handle.flush()

        def consume_stdout_bytes(chunk: bytes, *, final: bool = False) -> None:
            stdout_buffer.extend(chunk)
            while True:
                newline = stdout_buffer.find(b"\n")
                if newline < 0:
                    break
                raw = bytes(stdout_buffer[:newline])
                del stdout_buffer[: newline + 1]
                consume_stdout_line(raw.decode("utf-8", errors="replace"))
            if final and stdout_buffer:
                raw = bytes(stdout_buffer)
                stdout_buffer.clear()
                consume_stdout_line(raw.decode("utf-8", errors="replace"))

        def consume_stderr_line(raw: bytes) -> None:
            value = raw.decode("utf-8", errors="replace").rstrip("\r")
            if value:
                errors.append(_redact_sensitive_text(value))

        def consume_stderr_bytes(chunk: bytes, *, final: bool = False) -> None:
            nonlocal stderr_overflow
            pending = chunk
            while pending:
                newline = pending.find(b"\n")
                if stderr_overflow:
                    if newline < 0:
                        pending = b""
                        break
                    errors.append("Pi stderr line omitted because it exceeded the framing limit")
                    stderr_overflow = False
                    pending = pending[newline + 1 :]
                    continue
                if newline >= 0:
                    segment = pending[:newline]
                    if len(stderr_buffer) + len(segment) > _STDERR_LINE_LIMIT_BYTES:
                        errors.append("Pi stderr line omitted because it exceeded the framing limit")
                    else:
                        stderr_buffer.extend(segment)
                        consume_stderr_line(bytes(stderr_buffer))
                    stderr_buffer.clear()
                    pending = pending[newline + 1 :]
                    continue
                if len(stderr_buffer) + len(pending) > _STDERR_LINE_LIMIT_BYTES:
                    stderr_buffer.clear()
                    stderr_overflow = True
                else:
                    stderr_buffer.extend(pending)
                pending = b""
            if final:
                if stderr_overflow:
                    errors.append("Pi stderr line omitted because it exceeded the framing limit")
                    stderr_overflow = False
                elif stderr_buffer:
                    raw = bytes(stderr_buffer)
                    stderr_buffer.clear()
                    consume_stderr_line(raw)

        try:
            session_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(session_root, 0o700)
            session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(session_dir, 0o700)
            _prepare_project_settings(workdir, self.config)
            command = self.command(
                session_dir=session_dir,
                session_id=session_id,
                isolated=isolated,
            )
            if self.trace_path is not None:
                self.trace_path.parent.mkdir(parents=True, exist_ok=True)
                trace_handle = self.trace_path.open("a", encoding="utf-8")
                index_path = self.trace_path.with_name("pi_session_index.jsonl")
            process = subprocess.Popen(  # noqa: S603 - command is manifest/array-derived.
                command,
                cwd=workdir,
                env=self.environment(
                    task_id=task_id,
                    actor_id=actor_id,
                    workdir=workdir,
                    extra_env=extra_env,
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
            assert process.stdin is not None
            process.stdin.write(
                json.dumps({"id": request_id, "type": "prompt", "message": prompt}, ensure_ascii=False)
                .encode("utf-8")
                + b"\n"
            )
            process.stdin.flush()
            assert process.stdout is not None and process.stderr is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            timeout_seconds = float(self.config.pi_timeout_seconds)
            if deadline_monotonic is not None:
                timeout_seconds = min(timeout_seconds, max(0.1, deadline_monotonic - time.monotonic()))
            deadline = time.monotonic() + timeout_seconds
            while True:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                ready = selector.select(timeout=0.5)
                if not ready and process.poll() is not None:
                    break
                for key, _ in ready:
                    chunk = os.read(key.fileobj.fileno(), 65_536)
                    if not chunk:
                        try:
                            selector.unregister(key.fileobj)
                        except Exception:
                            pass
                        continue
                    if key.data == "stderr":
                        consume_stderr_bytes(chunk)
                        continue
                    consume_stdout_bytes(chunk)
                if settled_seen or prompt_rejected:
                    break
            selector.close()
            selector = None
            _close_stdin(process)
            remaining_stdout, remaining_stderr, drain_error = _drain_process(
                process,
                terminate=timed_out or cancelled,
            )
            consume_stdout_bytes(remaining_stdout, final=True)
            consume_stderr_bytes(remaining_stderr, final=True)
            if drain_error:
                errors.append(drain_error)
            returncode = process.returncode if process.returncode is not None else 1
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append(_redact_sensitive_text(str(exc)))
            if process is not None:
                try:
                    _close_stdin(process)
                    remaining_stdout, remaining_stderr, drain_error = _drain_process(process, terminate=True)
                    consume_stdout_bytes(remaining_stdout, final=True)
                    consume_stderr_bytes(remaining_stderr, final=True)
                    if drain_error:
                        errors.append(drain_error)
                    returncode = process.returncode if process.returncode is not None else 1
                except Exception:
                    returncode = 124
        finally:
            if selector is not None:
                selector.close()
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            if trace_handle is not None:
                trace_handle.close()
            if index_path is not None:
                session_file = _find_session_file(session_dir, session_id)
                run_dir = self.trace_path.parent if self.trace_path is not None else None
                index_row = {
                    "at": now_iso(),
                    "task_id": task_id,
                    "actor_id": actor_id,
                    "episode": episode,
                    "session_id": session_id,
                    "session_dir": _artifact_path(session_dir, run_dir),
                    "session_file": _artifact_path(session_file, run_dir) if session_file else None,
                }
                try:
                    with self._trace_lock:
                        with index_path.open("a", encoding="utf-8") as index_handle:
                            index_handle.write(json.dumps(index_row, ensure_ascii=False, sort_keys=True) + "\n")
                except OSError as exc:
                    errors.append(f"Unable to write Pi session index: {_redact_sensitive_text(str(exc))}")

        if timed_out:
            errors.append("Pi RPC deadline elapsed before agent_settled")
            returncode = returncode if returncode != 0 else 124
        elif cancelled:
            errors.append("Pi RPC was cancelled before agent_settled")
            returncode = returncode if returncode != 0 else 130
        elif prompt_rejected:
            returncode = returncode if returncode != 0 else 1
        elif settled_seen:
            final_error = pending_assistant_error or retry_final_error
            if final_error:
                errors.append(f"Pi RPC agent settled with an error: {_redact_sensitive_text(final_error)}")
                returncode = returncode if returncode != 0 else 1
        elif process is not None:
            suffix = " after agent_end" if agent_end_seen else ""
            errors.append(f"Pi RPC process exited before agent_settled{suffix}")
            returncode = returncode if returncode != 0 else 1
        return AgentResult(
            agent_id=actor_id,
            task_id=task_id,
            episode=episode,
            returncode=returncode,
            started_at=started,
            finished_at=now_iso(),
            command=command,
            # Re-sanitize the assembled tails so a secret split across Pi RPC
            # text-delta events cannot be reconstructed in final artifacts.
            output_tail=_redact_sensitive_text(output.value()),
            error_tail=_redact_sensitive_text(errors.value()),
            events=events,
            timed_out=timed_out,
            cancelled=cancelled,
        )


def _parse_json_line(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _event_text(payload: Mapping[str, Any]) -> str:
    event_type = str(payload.get("type") or "")
    if event_type == "message_update":
        update = payload.get("assistantMessageEvent")
        if isinstance(update, Mapping) and update.get("type") == "text_delta":
            delta = update.get("delta")
            return delta if isinstance(delta, str) else ""
        return ""
    if event_type == "message_end" and _message_role(payload) == "assistant":
        message = payload.get("message")
        if isinstance(message, Mapping):
            return _content_text(message.get("content"))
    return ""


def _usage_fields(payload: Mapping[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        message = payload.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else None
    if not isinstance(usage, Mapping):
        return {}
    result: dict[str, int] = {}
    for source, target in (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("cacheRead", "cache_read_tokens"),
        ("cacheWrite", "cache_write_tokens"),
        ("totalTokens", "total_tokens"),
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = usage.get(source)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[target] = value
    return result


class _TailBuffer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: deque[str] = deque()
        self.size = 0

    def append(self, value: str, *, separator: str = "\n") -> None:
        text = str(value or "")
        if not text:
            return
        if self.parts:
            text = separator + text
        self.parts.append(text)
        self.size += len(text)
        while self.size > self.limit and self.parts:
            excess = self.size - self.limit
            first = self.parts[0]
            if len(first) <= excess:
                self.parts.popleft()
                self.size -= len(first)
            else:
                self.parts[0] = first[excess:]
                self.size -= excess

    def value(self) -> str:
        return "".join(self.parts)


def _prepare_project_settings(workdir: Path, config: ExperimentConfig) -> Path:
    settings_dir = workdir / ".pi"
    settings_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(settings_dir, 0o700)
    settings_path = settings_dir / "settings.json"
    existing: dict[str, Any] = {}
    if settings_path.exists():
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Pi project settings must contain an object: {settings_path}")
        existing = payload
    retry = dict(existing.get("retry")) if isinstance(existing.get("retry"), Mapping) else {}
    provider = dict(retry.get("provider")) if isinstance(retry.get("provider"), Mapping) else {}
    provider.update(
        {
            "maxRetries": config.pi_provider_max_retries,
            "maxRetryDelayMs": config.pi_provider_max_retry_delay_ms,
        }
    )
    retry.update(
        {
            "enabled": config.pi_retry_enabled,
            "maxRetries": config.pi_retry_max_retries,
            "baseDelayMs": config.pi_retry_base_delay_ms,
            "provider": provider,
        }
    )
    existing.update(
        {
            "httpIdleTimeoutMs": config.pi_http_idle_timeout_ms,
            "retry": retry,
        }
    )
    temporary = settings_path.with_name(f"settings.json.tmp-{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(settings_path)
    os.chmod(settings_path, 0o600)
    return settings_path


def _session_id(trace_path: Path | None, actor_id: str, episode: int) -> str:
    run_label = trace_path.parent.name if trace_path is not None else "local"
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{run_label}-{actor_id}-e{episode}").strip("-._")
    digest_input = f"{trace_path or ''}\0{actor_id}\0{episode}".encode()
    digest = hashlib.sha256(digest_input).hexdigest()[:16]
    return f"{readable[:64] or 'contextswarm'}-{digest}"


def _find_session_file(session_dir: Path, session_id: str) -> Path | None:
    try:
        candidates = [
            path
            for path in session_dir.glob("*.jsonl")
            if path.name == f"{session_id}.jsonl" or path.name.endswith(f"_{session_id}.jsonl")
        ]
        return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None
    except OSError:
        return None


def _artifact_path(path: Path, run_dir: Path | None) -> str:
    resolved = path.resolve()
    if run_dir is not None:
        try:
            return str(resolved.relative_to(run_dir.resolve()))
        except ValueError:
            pass
    return str(resolved)


def _message_role(payload: Mapping[str, Any]) -> str:
    message = payload.get("message")
    return str(message.get("role") or "") if isinstance(message, Mapping) else ""


def _assistant_outcome(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    if payload.get("type") not in {"message_end", "turn_end"}:
        return None
    message = payload.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return None
    stop_reason = str(message.get("stopReason") or message.get("stop_reason") or "")
    error_message = _text_field(message, "errorMessage", "error_message")
    return stop_reason, error_message


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _text_field(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _event_error(payload: Mapping[str, Any]) -> str:
    outcome = _assistant_outcome(payload)
    if outcome is not None and outcome[0] == "error":
        return outcome[1]
    direct = _text_field(payload, "errorMessage", "finalError", "error")
    if direct:
        return direct
    if payload.get("type") == "tool_execution_end" and payload.get("isError") is True:
        result = payload.get("result")
        if isinstance(result, Mapping):
            return _content_text(result.get("content"))
    return ""


def _event_trace_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for source, target in (
        ("willRetry", "will_retry"),
        ("success", "success"),
        ("attempt", "retry_attempt"),
        ("maxAttempts", "retry_max_attempts"),
        ("delayMs", "retry_delay_ms"),
        ("isError", "tool_error"),
    ):
        value = payload.get(source)
        if isinstance(value, (bool, int)):
            fields[target] = value
    tool_name = payload.get("toolName")
    if isinstance(tool_name, str) and tool_name:
        fields["tool_name"] = tool_name[:120]
    outcome = _assistant_outcome(payload)
    if outcome is not None and outcome[0]:
        fields["stop_reason"] = outcome[0][:80]
    error = _event_error(payload)
    if error:
        sanitized = _redact_sensitive_text(error)
        fields.update(
            {
                "error_category": _error_category(sanitized),
                "error_chars": len(error),
                "error_sha256": hashlib.sha256(sanitized.encode()).hexdigest()[:16],
            }
        )
    return fields


def _error_category(value: str) -> str:
    lowered = value.lower()
    for category, needles in (
        ("timeout", ("timeout", "timed out")),
        ("rate_limit", ("rate limit", "too many requests", "429")),
        ("provider_5xx", ("500", "502", "503", "504", "server error", "overloaded")),
        ("transport", ("connection", "network", "socket", "websocket", "fetch failed")),
        ("context", ("context window", "context length", "overflow")),
        ("authentication", ("unauthorized", "forbidden", "authentication")),
    ):
        if any(needle in lowered for needle in needles):
            return category
    return "other"


_URL_PATTERN = re.compile(r"\b(?:https?|wss?)://[^\s<>'\"]+", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+")
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)([\"']?authorization[\"']?\s*[:=]\s*)"
    r"(?:[\"']?(?:Bearer|Basic)\s+[A-Za-z0-9._~+\-/=]+[\"']?)"
)
_SECRET_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|token|password|"
    r"credential|client[_-]?secret|secret)[\"']?\s*[:=]\s*)"
    r"(?:[\"'][^\"']*[\"']|[^\s,;}]+)"
)
_OPAQUE_SECRET_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"(?:sk|tok|nur|aisw)[_-][A-Za-z0-9_-]{12,}"
    r"|eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,}){2}"
    r"|[A-Za-z0-9_-]{48,}"
    r")(?![A-Za-z0-9])"
)


def _redact_sensitive_text(value: str) -> str:
    text = _AUTHORIZATION_PATTERN.sub(r"\1<redacted>", str(value or ""))
    text = _BEARER_PATTERN.sub("Bearer <redacted>", text)
    text = _SECRET_PATTERN.sub(r"\1<redacted>", text)
    text = _URL_PATTERN.sub("<redacted-url>", text)
    return _OPAQUE_SECRET_PATTERN.sub("<redacted-secret>", text)


def _close_stdin(process: subprocess.Popen[bytes]) -> None:
    stream = process.stdin
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass
        process.stdin = None


def _signal_process(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, sig)
    except (OSError, ProcessLookupError):
        try:
            if sig == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
        except OSError:
            pass


def _drain_process(process: subprocess.Popen[bytes], *, terminate: bool) -> tuple[bytes, bytes, str]:
    if terminate:
        _signal_process(process, signal.SIGTERM)
    grace_seconds = 5 if terminate else 10
    try:
        stdout, stderr = process.communicate(timeout=grace_seconds)
        diagnostic = ""
    except subprocess.TimeoutExpired:
        _signal_process(process, signal.SIGKILL)
        diagnostic = (
            f"Pi RPC process drain exceeded {grace_seconds}s after "
            f"{'SIGTERM' if terminate else 'stdin close'}; sent SIGKILL"
        )
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.output or b""
            stderr = exc.stderr or b""
            diagnostic += "; process still did not exit within 5s of SIGKILL"
    return stdout or b"", stderr or b"", diagnostic
