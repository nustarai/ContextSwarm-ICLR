"""Bounded Pi RPC launcher with NuRouter/AISW environment wiring."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import selectors
import signal
import shutil
import subprocess
import threading
import time
from typing import Any, Mapping
import uuid

from .config import ExperimentConfig
from .models import AgentResult


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass
class PiAgent:
    config: ExperimentConfig
    trace_path: Path | None = None

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

    def command(self) -> list[str]:
        command = [self.binary(), "--mode", "rpc", "--thinking", self.config.thinking]
        extension = self.config.pi_extension.strip()
        if self.config.fast_mode and extension:
            extension_path = self.config.resolve_runtime_path(extension)
            if extension_path.is_file():
                command.extend(["--extension", str(extension_path)])
        if self.config.model:
            command.extend(["--model", self.config.model])
        return command

    def environment(
        self,
        *,
        task_id: str,
        actor_id: str,
        workdir: Path,
        extra_env: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PI_BIN": self.binary(),
                "EXPERIMENT_PI_BINARY": self.binary(),
                "CONTEXTSWARM_TASK_ID": task_id,
                "CONTEXTSWARM_ACTOR_ID": actor_id,
                "CONTEXTSWARM_WORKDIR": str(workdir),
                "CONTEXTSWARM_EXPERIMENT_SEED": str(self.config.seed),
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
            env.update({str(key): str(value) for key, value in extra_env.items()})
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
    ) -> AgentResult:
        started = now_iso()
        command = self.command()
        output: list[str] = []
        errors: list[str] = []
        events = 0
        timed_out = False
        cancelled = False
        returncode = 1
        process: subprocess.Popen[str] | None = None
        trace_handle = None
        try:
            if self.trace_path is not None:
                self.trace_path.parent.mkdir(parents=True, exist_ok=True)
                trace_handle = self.trace_path.open("a", encoding="utf-8")
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
                text=True,
                encoding="utf-8",
                bufsize=1,
                start_new_session=True,
            )
            assert process.stdin is not None
            request_id = f"contextswarm-{uuid.uuid4().hex}"
            process.stdin.write(
                json.dumps({"id": request_id, "type": "prompt", "message": prompt}, ensure_ascii=False)
                + "\n"
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
            terminal_seen = False
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                ready = selector.select(timeout=0.5)
                if not ready and process.poll() is not None:
                    break
                for key, _ in ready:
                    line = key.fileobj.readline()
                    if line == "":
                        try:
                            selector.unregister(key.fileobj)
                        except Exception:
                            pass
                        continue
                    if key.data == "stderr":
                        errors.append(line.rstrip())
                        continue
                    payload = _parse_json_line(line)
                    if payload is None:
                        output.append(line.rstrip())
                        continue
                    events += 1
                    event_type = str(payload.get("type") or payload.get("event") or "unknown")
                    rendered = _event_text(payload)
                    if rendered:
                        output.append(rendered)
                    if trace_handle is not None:
                        usage = _usage_fields(payload)
                        trace_handle.write(
                            json.dumps(
                                {
                                    "at": now_iso(),
                                    "task_id": task_id,
                                    "actor_id": actor_id,
                                    "episode": episode,
                                    "type": event_type,
                                    "has_text": bool(rendered),
                                    **usage,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        trace_handle.flush()
                    if event_type in {"agent_end", "agent_settled"}:
                        terminal_seen = True
                if terminal_seen:
                    break
            else:
                timed_out = True
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if timed_out or cancelled:
                _terminate(process)
            elif process.poll() is None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _terminate(process)
            returncode = process.wait(timeout=10)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
            if process is not None:
                _terminate(process)
                try:
                    returncode = process.wait(timeout=5)
                except Exception:
                    returncode = 124
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            if trace_handle is not None:
                trace_handle.close()
        return AgentResult(
            agent_id=actor_id,
            task_id=task_id,
            episode=episode,
            returncode=returncode,
            started_at=started,
            finished_at=now_iso(),
            command=command,
            output_tail="\n".join(output)[-6_000:],
            error_tail="\n".join(errors)[-4_000:],
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
    for key in ("text", "message", "delta", "content", "output"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _usage_fields(payload: Mapping[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return {}
    result: dict[str, int] = {}
    for source, target in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = usage.get(source)
        if isinstance(value, int) and value >= 0:
            result[target] = value
    return result


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
