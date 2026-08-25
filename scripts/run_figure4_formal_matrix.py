#!/usr/bin/env python3
"""Run the six-dataset Figure 4 matrix in three bounded 24-arm waves.

Each wave contains one run for every dataset/policy pair (6 × 4 = 24).
Leaves themselves use 24 CPS in-flight slots, as required by the formal
manifest.  The supervisor waits for an entire wave to settle before starting
the next repeat, so the host-side experiment count never exceeds 24.

The supervisor intentionally treats pre-admission health/startup failures as
recoverable.  Once a run has a horizon timestamp, its candidate outcomes are
owned by the runner and are never replaced merely because a terminal status is
degraded.  URLs and declaration-index capabilities are copied to child
environments but are never written to the state file or printed.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, asdict
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

# ``python scripts/run_figure4_formal_matrix.py`` sets ``sys.path[0]`` to
# ``scripts`` rather than the checkout root.  Keep the direct operator entry
# point usable without requiring an operator-specific ``PYTHONPATH``.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextswarm_mini.formal_matrix_artifacts import (
    artifact_eligibility,
    is_recovered_transport_event,
)
from contextswarm_mini.provider_diagnostics import provider_diagnostic_class


DATASETS = ("clever", "icpc_wf_2025", "matholympiadbench", "putnambench", "usaco", "verina")
POLICIES = ("uniform_refill", "task_state", "trace_state", "llm_scheduler")
FORMAL_DATASETS = {"clever", "matholympiadbench", "putnambench", "verina"}
CODING_DATASETS = {"icpc_wf_2025", "usaco"}
DEFAULT_FORMAL_URL = "http://host.docker.internal:38100"
DEFAULT_CODING_URL = "http://host.docker.internal:38081"

# A short, candidate-independent provider burst is handled as a bounded
# supervisor stop.  Isolated errors remain recoverable agent noise; this
# threshold prevents a broken account/router from causing endless slot
# refills while preserving the fixed arm horizon for already-admitted runs.
INFRA_ERROR_WINDOW_SECONDS = 60.0
INFRA_ERROR_LIMIT = 20


@dataclass
class Slot:
    dataset: str
    repeat: int
    policy: str
    attempts: int = 0
    pid: int | None = None
    status: str = "pending"
    last_exit_code: int | None = None
    last_reason: str = ""
    next_launch_at: float = 0.0
    horizon_run_id: str = ""
    latest_run_id: str = ""
    infrastructure_failures: int = 0
    last_artifact_reasons: list[str] | None = None

    @property
    def output_root(self) -> str:
        return f"runs/figure4_formal_6datasets/{self.dataset}/repeat-{self.repeat:02d}/{self.policy}"

    @property
    def manifest(self) -> str:
        return f"configs/figure4_formal_6datasets/{self.dataset}/repeat{self.repeat}/{self.policy}.toml"


class _AdoptedProcess:
    """Small ``Popen``-compatible view over a child owned by an old supervisor.

    A replacement supervisor cannot manufacture a ``subprocess.Popen`` object
    for a process it did not spawn.  It only needs the bounded ``poll``/``wait``
    surface used by this module, so keep that surface deliberately tiny.  The
    process identity is checked before this wrapper is created; a dead or
    mismatched PID is never adopted.
    """

    def __init__(self, pid: int) -> None:
        self.pid = int(pid)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if _pid_alive(self.pid):
            return None
        # We cannot recover the original wait status after a supervisor loss.
        # The artifact lifecycle is authoritative; a missing/incomplete
        # artifact is handled as a bounded slot retry by the caller.
        self.returncode = 0
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("adopted process", timeout)
            time.sleep(0.05)
        return int(self.returncode or 0)


def _now() -> float:
    return time.monotonic()


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _pid_alive(pid: Any) -> bool:
    """Return whether *pid* currently names a process."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # ``kill(pid, 0)`` also succeeds for a zombie.  A replacement supervisor
    # cannot wait on a child it did not create, so treat a zombie as already
    # settled and let artifact reconciliation decide whether the slot needs a
    # bounded retry.
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = stat_text[stat_text.rfind(")") + 2 :].split()
        if tail and tail[0] == "Z":
            return False
    except (OSError, UnicodeError):
        pass
    return True


def _proc_cmdline(pid: Any) -> str:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return ""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace"
        )
    except OSError:
        return ""


def _proc_cwd(pid: Any) -> Path | None:
    """Return a process' working directory when procfs exposes it."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
    except (OSError, RuntimeError):
        return None


def _trusted_child_pid(pid: Any, slot: Slot, repo: Path) -> bool:
    """Validate a persisted child identity before supervisor adoption.

    A PID alone is never enough: after a process exits the kernel may reuse it
    for an unrelated command.  The launcher command must still identify this
    exact manifest and output root, while procfs must bind it to this
    checkout.  The check is intentionally conservative; a false negative
    causes a bounded relaunch, while a false positive could duplicate a
    formal arm.
    """

    if not _pid_alive(pid):
        return False
    command = _proc_cmdline(pid)
    if not command:
        return False
    if "run_docker.sh" not in command and "docker run" not in command:
        return False
    # ``subprocess.Popen`` starts the launcher with a relative command from
    # ``cwd=repo``.  That path is not present in argv after ``run_docker.sh``
    # execs ``docker run``; inspect procfs cwd instead of requiring a path in
    # the command line (which would reject every real child).
    process_cwd = _proc_cwd(pid)
    try:
        expected_cwd = repo.resolve()
    except OSError:
        return False
    if process_cwd != expected_cwd:
        return False
    if slot.manifest not in command and Path(slot.manifest).name not in command:
        return False
    return slot.output_root in command


def _adopt_process(pid: Any, slot: Slot, repo: Path) -> _AdoptedProcess | None:
    """Return an identity-checked process view, or ``None`` if unsafe."""

    if not _trusted_child_pid(pid, slot, repo):
        return None
    return _AdoptedProcess(int(pid))


def _run_records(root: Path, slot: Slot) -> list[tuple[Path, dict[str, Any]]]:
    base = root / slot.dataset / f"repeat-{slot.repeat:02d}" / slot.policy
    if not base.is_dir():
        return []
    records: list[tuple[Path, dict[str, Any]]] = []
    for meta_path in base.glob("*/run_meta.json"):
        meta = _json(meta_path)
        if not isinstance(meta, dict):
            continue
        # A run directory is forensic input, not an authority by itself.
        # Require the runner's explicit dataset/policy binding before it can
        # influence adoption or suppress a replacement launch.  In
        # particular, do not let a malformed/legacy metadata file with a
        # missing field masquerade as this slot merely because it lives below
        # the expected output directory.
        if meta.get("dataset") != slot.dataset:
            continue
        allocation = meta.get("allocation")
        if not isinstance(allocation, dict) or allocation.get("policy") != slot.policy:
            continue
        records.append((meta_path.parent, meta))
    records.sort(key=lambda item: item[0].stat().st_mtime)
    return records


def _completed_run(root: Path, slot: Slot) -> tuple[Path, dict[str, Any]] | None:
    """Find the newest eligible result, even if a later attempt is partial."""

    for directory, meta in reversed(_run_records(root, slot)):
        eligible, _reasons = artifact_eligibility(directory, policy=slot.policy)
        if eligible:
            return directory, meta
    return None


def _state_rows(path: Path, root: Path) -> dict[tuple[str, int, str], dict[str, Any]]:
    """Load only state rows bound to this result root and matrix contract."""

    payload = _json(path)
    if not isinstance(payload, dict):
        return {}
    if payload.get("schema_version") != "contextswarm_figure4_matrix_supervisor_v1":
        return {}
    recorded_root = payload.get("root")
    if not isinstance(recorded_root, str) or not recorded_root.strip():
        return {}
    try:
        if Path(recorded_root).resolve() != root.resolve():
            return {}
    except OSError:
        return {}
    if payload.get("wave_capacity") != 24 or payload.get("repeat_count") != 3:
        return {}
    if tuple(payload.get("datasets") or ()) != DATASETS:
        return {}
    if tuple(payload.get("policies") or ()) != POLICIES:
        return {}
    rows = payload.get("slots")
    if not isinstance(rows, list):
        return {}
    result: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        dataset = str(row.get("dataset") or "")
        policy = str(row.get("policy") or "")
        repeat = row.get("repeat")
        if dataset not in DATASETS or policy not in POLICIES:
            continue
        if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat not in (1, 2, 3):
            continue
        result[(dataset, repeat, policy)] = row
    return result


def _resume_slot(
    slot: Slot,
    row: dict[str, Any] | None,
    *,
    root: Path,
    repo: Path,
    processes: dict[int, Any],
) -> None:
    """Restore one slot without duplicating an admitted arm.

    Completed artifacts win over stale state.  A live, identity-checked child
    with a persisted horizon is adopted.  A dead/incomplete horizon or a
    pre-admission child is left pending so only that slot is relaunched.
    """

    old_pid = (row or {}).get("pid") if isinstance(row, dict) else None
    slot.pid = None
    slot.status = "pending"
    slot.horizon_run_id = ""
    slot.latest_run_id = ""
    slot.last_artifact_reasons = None
    slot.next_launch_at = 0.0
    if isinstance(row, dict):
        attempts = row.get("attempts")
        if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0:
            slot.attempts = attempts
        reason = row.get("last_reason")
        if isinstance(reason, str):
            slot.last_reason = reason[:240]

    completed = _completed_run(root, slot)
    if completed is not None:
        # A duplicate supervisor may have left a newer child alive after an
        # earlier attempt already produced a valid artifact.  The artifact is
        # authoritative; stop only an identity-checked child bound to this
        # exact slot so it cannot keep consuming provider capacity.
        if _trusted_child_pid(old_pid, slot, repo):
            _terminate_process(_AdoptedProcess(int(old_pid)))
        directory, meta = completed
        run_id = str(meta.get("run_id") or directory.name)
        slot.horizon_run_id = run_id
        slot.latest_run_id = run_id
        slot.status = "finished"
        return

    records = _run_records(root, slot)
    if isinstance(row, dict):
        persisted_horizon = str(row.get("horizon_run_id") or "").strip()
    else:
        persisted_horizon = ""
    # Prefer the exact persisted horizon over the newest directory.  A
    # pre-admission retry can have been created by an operator just before a
    # supervisor restart; adopting the older live horizon is still safer than
    # launching a duplicate for it.
    latest = next(
        ((directory, meta) for directory, meta in records if str(meta.get("run_id") or directory.name) == persisted_horizon),
        None,
    )
    if latest is None and records:
        latest = records[-1]
    if latest is None:
        return
    directory, meta = latest
    run_id = str(meta.get("run_id") or directory.name)
    slot.latest_run_id = run_id
    horizon = str(meta.get("horizon_started_at") or "").strip()
    old_horizon = persisted_horizon
    # Adopt only the exact run recorded by the previous supervisor.  This
    # prevents a stale PID from being attached to a newer retry directory.
    if horizon and old_horizon == run_id:
        process = _adopt_process(old_pid, slot, repo)
        if process is not None:
            slot.horizon_run_id = run_id
            slot.pid = process.pid
            slot.status = "running"
            processes[process.pid] = process
            return

    # No safe live owner remains.  A previous supervisor may have died while
    # a pre-admission launcher was still alive; terminate that exact, trusted
    # process before allowing a replacement, otherwise two launchers can race
    # the same slot and recreate the provider overload.  Never do this for an
    # admitted horizon whose state binding is ambiguous: candidate/CPS state
    # must remain untouched until the operator can reconcile it.
    if not horizon and _trusted_child_pid(old_pid, slot, repo):
        orphan = _AdoptedProcess(int(old_pid))
        _terminate_process(orphan)
        slot.last_reason = "pre_admission_process_terminated"
    else:
        slot.last_reason = "incomplete_horizon" if horizon else "pre_admission_retry"
    slot.attempts = max(slot.attempts, len(records))


def _write_state(path: Path, slots: list[Slot], *, event: str, root: Path, pid: int | None = None) -> None:
    payload = {
        "schema_version": "contextswarm_figure4_matrix_supervisor_v1",
        "updated_at": _utc(),
        "event": event,
        "root": str(root),
        "wave_capacity": 24,
        "repeat_count": 3,
        "datasets": list(DATASETS),
        "policies": list(POLICIES),
        "supervisor_pid": pid,
        "slots": [asdict(slot) | {"output_root": slot.output_root, "manifest": slot.manifest} for slot in slots],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _judge_urls() -> dict[str, str]:
    """Resolve endpoint capabilities without exposing their values."""

    formal = os.environ.get("CONTEXTSWARM_FORMAL_JUDGE_URL", "").strip() or DEFAULT_FORMAL_URL
    coding = os.environ.get("CONTEXTSWARM_CODING_JUDGE_URL", "").strip() or DEFAULT_CODING_URL
    return {"formal": formal, "coding": coding}


def _cache_urls(urls: dict[str, str]) -> dict[str, str]:
    return {
        "formal": os.environ.get("CONTEXTSWARM_FORMAL_JUDGE_CACHE_HEALTH_URL", "").strip() or urls["formal"],
        "coding": os.environ.get("CONTEXTSWARM_CODING_JUDGE_CACHE_HEALTH_URL", "").strip() or urls["coding"],
    }


def _health_ready(value: Any) -> bool:
    """Match the runner's admission readiness checks before launching.

    A router can transiently report ``ok=true`` while its advertised group
    capacity is degraded or while a disabled group projection masks an empty
    direct worker pool.  Launching in either state only creates a
    ``PREFLIGHT_FAILED`` artifact and consumes a supervisor retry.  Keep this
    bounded gate aligned with :mod:`contextswarm_mini.preflight`; the runner's
    own preflight remains authoritative after launch.
    """

    if not isinstance(value, dict) or value.get("ok") is not True:
        return False
    group = value.get("group_admission")
    group_disabled = (
        isinstance(group, dict)
        and group.get("enabled") is False
        and group.get("status") == "disabled"
        and value.get("capacity_error_kind") == "admission_disabled"
    )
    if group_disabled:
        direct_ready = value.get("ready_workers", value.get("active_workers"))
        return (
            not isinstance(direct_ready, bool)
            and isinstance(direct_ready, (int, float))
            and math.isfinite(float(direct_ready))
            and float(direct_ready) > 0
        )

    if "available_service_units" in value:
        available = value.get("available_service_units")
        if (
            isinstance(available, bool)
            or not isinstance(available, (int, float))
            or not math.isfinite(float(available))
            or float(available) <= 0
        ):
            return False
    if "capacity_state" in value and value.get("capacity_state") != "AVAILABLE":
        return False
    return True


def _health_ok(url: str) -> bool:
    # host.docker.internal is a container-only name; the supervisor probes the
    # corresponding host listener without printing the private URL.
    probe = url.replace("host.docker.internal", "127.0.0.1").rstrip("/") + "/healthz"
    try:
        with urlopen(probe, timeout=5.0) as response:
            value = json.loads(response.read(1_000_000).decode("utf-8"))
        return _health_ready(value)
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False


def _discover_capabilities() -> tuple[dict[str, str], dict[str, str]]:
    urls = _judge_urls()
    caches = _cache_urls(urls)
    # A live Docker deployment may expose a non-default endpoint.  Inspect
    # only the two capability variables and retain them in memory.
    try:
        ids = subprocess.check_output(["docker", "ps", "-q"], text=True, stderr=subprocess.DEVNULL).split()
    except (OSError, subprocess.CalledProcessError):
        ids = []
    for container_id in ids:
        try:
            raw = subprocess.check_output(["docker", "inspect", container_id], text=True, stderr=subprocess.DEVNULL)
            record = json.loads(raw)[0]
            env: dict[str, str] = {}
            for item in record.get("Config", {}).get("Env", []) or []:
                key, sep, value = str(item).partition("=")
                if sep:
                    env[key] = value
            judge = env.get("CONTEXTSWARM_JUDGE_URL", "").strip()
            cache = env.get("CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL", "").strip()
            if not judge:
                continue
            if ":38081" in judge:
                urls["coding"] = judge
                if cache:
                    caches["coding"] = cache
            elif ":38100" in judge:
                urls["formal"] = judge
                if cache:
                    caches["formal"] = cache
        except (OSError, subprocess.CalledProcessError, IndexError, KeyError, json.JSONDecodeError):
            continue
    return urls, caches


def _index_capability(dataset: str, child_env: dict[str, str]) -> None:
    if dataset not in FORMAL_DATASETS:
        for key in ("CONTEXTSWARM_MINI_DECL_INDEX", "CONTEXTSWARM_MINI_DECL_INDEX_SHA256", "CONTEXTSWARM_MINI_MATHLIB_REVISION"):
            child_env.pop(key, None)
        return
    prefix = f"CONTEXTSWARM_FIGURE4_INDEX_{dataset.upper()}"
    source = child_env.get(prefix, "").strip()
    digest = child_env.get(prefix + "_SHA256", "").strip().lower()
    revision = child_env.get(prefix + "_REVISION", "").strip()
    if not source or not digest or not revision:
        raise RuntimeError(f"missing declaration-index capability for {dataset} ({prefix}[_SHA256|_REVISION])")
    child_env["CONTEXTSWARM_MINI_DECL_INDEX"] = source
    child_env["CONTEXTSWARM_MINI_DECL_INDEX_SHA256"] = digest
    child_env["CONTEXTSWARM_MINI_MATHLIB_REVISION"] = revision


def _latest_run(root: Path, slot: Slot) -> tuple[Path | None, dict[str, Any] | None]:
    base = root / slot.dataset / f"repeat-{slot.repeat:02d}" / slot.policy
    if not base.exists():
        return None, None
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for meta_path in base.glob("*/run_meta.json"):
        meta = _json(meta_path)
        if meta is not None:
            candidates.append((meta_path.parent, meta))
    candidates.sort(key=lambda item: item[0].stat().st_mtime)
    return candidates[-1] if candidates else (None, None)


def _provider_infra_class(error_tail: Any) -> str | None:
    """Classify explicit provider failures, never ordinary candidate errors."""

    if not isinstance(error_tail, str):
        return None
    classified = provider_diagnostic_class(error_tail)
    if classified is not None:
        return classified
    low = error_tail.casefold()
    if "not found" in low or "404" in low:
        return "provider_not_found"
    if "coordinator run request failed" in low or "coordinator response failed" in low:
        return "coordinator_request"
    if "account auth refresh" in low:
        return "provider_auth"
    return None


def _provider_infra_burst(
    root: Path,
    slots: list[Slot],
    seen_offsets: dict[str, int],
    recent: deque[tuple[float, str, str]],
    *,
    now: float | None = None,
) -> tuple[str, str, int] | None:
    """Read appended terminal rows and detect a bounded provider-error burst.

    ``seen_offsets`` is caller-owned so a replacement supervisor can start at
    the current tail and avoid retriggering on old forensic evidence.  A
    recovered transport row is intentionally ignored; its settlement and
    assistant-success bits prove that the logical agent continued.
    """

    sample_time = time.monotonic() if now is None else float(now)
    for slot in slots:
        base = root / slot.dataset / f"repeat-{slot.repeat:02d}" / slot.policy
        if not base.is_dir():
            continue
        for events_path in base.glob("*/events.jsonl"):
            run_id = events_path.parent.name
            try:
                size = events_path.stat().st_size
                offset = min(max(0, int(seen_offsets.get(run_id, 0))), size)
                with events_path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                seen_offsets[run_id] = size
            except OSError:
                continue
            for raw in chunk.splitlines():
                try:
                    row = json.loads(raw.decode("utf-8", "replace"))
                except (TypeError, ValueError, UnicodeError):
                    continue
                if not isinstance(row, dict) or row.get("event") != "agent_finished":
                    continue
                if is_recovered_transport_event(row):
                    continue
                kind = _provider_infra_class(row.get("error_tail"))
                if kind is not None:
                    recent.append((sample_time, run_id, kind))

    cutoff = sample_time - INFRA_ERROR_WINDOW_SECONDS
    while recent and recent[0][0] < cutoff:
        recent.popleft()
    if len(recent) >= INFRA_ERROR_LIMIT:
        _at, run_id, kind = recent[-1]
        return run_id, kind, len(recent)
    return None


def _refresh(slot: Slot, root: Path) -> None:
    slot.last_artifact_reasons = None
    # A later forensic retry must not hide an earlier valid result.  This is
    # especially important after supervisor recovery: the old child may have
    # written a partial directory just before the replacement supervisor
    # starts, while the preceding arm already satisfied the formal closeout
    # contract.
    completed = _completed_run(root, slot)
    if completed is not None:
        directory, meta = completed
        run_id = str(meta.get("run_id") or directory.name)
        slot.latest_run_id = run_id
        slot.horizon_run_id = run_id
        slot.status = "finished"
        return
    run_dir, meta = _latest_run(root, slot)
    if run_dir is None or meta is None:
        return
    run_id = str(meta.get("run_id") or run_dir.name)
    slot.latest_run_id = run_id
    horizon = str(meta.get("horizon_started_at") or "")
    if horizon:
        slot.horizon_run_id = run_id
        final = _json(run_dir / "final.json")
        if final is not None:
            eligible, reasons = artifact_eligibility(run_dir, policy=slot.policy)
            slot.last_artifact_reasons = reasons or None
            if eligible:
                slot.status = "finished"
            elif slot.pid is None:
                slot.status = "pending"
                slot.last_reason = "invalid_terminal_artifact"
        elif slot.pid is None:
            slot.status = "closeout_pending"
        else:
            slot.status = "running"


def _child_env(dataset: str, urls: dict[str, str], caches: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env["CONTEXTSWARM_JUDGE_URL"] = urls["coding" if dataset in CODING_DATASETS else "formal"]
    env["CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL"] = caches["coding" if dataset in CODING_DATASETS else "formal"]
    env.setdefault("CONTEXTSWARM_NUROUTER_BINARY", "/home/ubuntu/.local/bin/nurouter")
    env.setdefault("CONTEXTSWARM_NUROUTER_NODE_CONFIG", "/home/ubuntu/.nurouter/node.toml")
    env.setdefault("CONTEXTSWARM_AISW_LAUNCHER_METADATA", "/home/ubuntu/.local/bin/.nurouter-pi-launcher.json")
    env.setdefault("CONTEXTSWARM_MINI_PIDS_LIMIT", "2048")
    return env


def _launch(slot: Slot, *, repo: Path, root: Path, urls: dict[str, str], caches: dict[str, str], log_root: Path) -> subprocess.Popen[bytes]:
    env = _child_env(slot.dataset, urls, caches)
    _index_capability(slot.dataset, env)
    command = ["bash", "scripts/run_docker.sh", "--config", slot.manifest, "--output", slot.output_root]
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = log_root / f"{slot.dataset}-r{slot.repeat:02d}-{slot.policy}-a{slot.attempts:02d}.log"
    handle = log_path.open("ab", buffering=0)
    process = subprocess.Popen(
        command,
        cwd=repo,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    handle.close()
    slot.pid = process.pid
    slot.status = "starting"
    slot.last_reason = "launch_submitted"
    return process


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """Stop one pre-admission child without touching unrelated processes."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        return
    try:
        process.wait(timeout=15.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


def _slots_for_wave(repeat: int) -> list[Slot]:
    return [Slot(dataset=dataset, repeat=repeat, policy=policy) for dataset in DATASETS for policy in POLICIES]


def run(
    repo: Path,
    *,
    state_path: Path,
    log_root: Path,
    root: Path,
    max_attempts: int,
    retry_seconds: float,
    max_infrastructure_failures: int,
    resume: bool = True,
) -> int:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    all_slots: list[Slot] = []
    # Values are either children launched by this invocation or the bounded
    # ``_AdoptedProcess`` view of a child owned by a prior supervisor.
    processes: dict[int, Any] = {}
    urls, caches = _discover_capabilities()
    prior_rows = _state_rows(state_path, root) if resume else {}

    for repeat in (1, 2, 3):
        slots = _slots_for_wave(repeat)
        for slot in slots:
            _resume_slot(
                slot,
                prior_rows.get((slot.dataset, slot.repeat, slot.policy)),
                root=root,
                repo=repo,
                processes=processes,
            )
        all_slots.extend(slots)
        _write_state(state_path, all_slots, event=f"wave_{repeat}_starting", root=root, pid=os.getpid())
        seen_provider_offsets: dict[str, int] = {}
        recent_provider_errors: deque[tuple[float, str, str]] = deque()
        # Do not turn old forensic rows into a fresh breaker trip when a
        # supervisor is intentionally started against an existing result root.
        for slot in slots:
            base = root / slot.dataset / f"repeat-{slot.repeat:02d}" / slot.policy
            for events_path in base.glob("*/events.jsonl") if base.is_dir() else ():
                try:
                    seen_provider_offsets[events_path.parent.name] = events_path.stat().st_size
                except OSError:
                    pass
        while not stopping:
            now = _now()
            # Bind any run that crossed the fixed horizon before inspecting
            # provider diagnostics.  A child can append a burst of terminal
            # rows in the same polling interval in which it writes
            # ``horizon_started_at``; checking the breaker first would then
            # mistake an admitted arm for a pre-admission child and kill the
            # very CPS state a replacement supervisor is meant to adopt.
            for slot in slots:
                _refresh(slot, root)
            burst = _provider_infra_burst(
                root,
                slots,
                seen_provider_offsets,
                recent_provider_errors,
            )
            if burst is not None:
                run_id, kind, count = burst
                for slot in slots:
                    if slot.status != "finished":
                        slot.last_reason = "provider_infrastructure_burst"
                _write_state(
                    state_path,
                    all_slots,
                    event="provider_infrastructure_burst",
                    root=root,
                    pid=os.getpid(),
                )
                # A pre-admission child has no persisted horizon and is safe
                # to stop.  An admitted arm is left alive with its candidate
                # and CPS state so a replacement supervisor can adopt it.
                for slot in slots:
                    if slot.horizon_run_id or slot.pid is None:
                        continue
                    process = processes.get(slot.pid)
                    if process is not None:
                        _terminate_process(process)
                        processes.pop(slot.pid, None)
                        slot.pid = None
                _write_state(
                    state_path,
                    all_slots,
                    event="stopped_for_provider_diagnosis",
                    root=root,
                    pid=os.getpid(),
                )
                return 4
            for slot in slots:
                if slot.pid is not None:
                    process = processes.get(slot.pid)
                    if process is not None:
                        code = process.poll()
                        if code is None:
                            continue
                        slot.last_exit_code = code
                        processes.pop(slot.pid, None)
                        slot.pid = None
                        _refresh(slot, root)
                        if slot.status != "finished":
                            if slot.last_artifact_reasons:
                                slot.infrastructure_failures += 1
                                slot.last_reason = "invalid_terminal_artifact"
                                if slot.infrastructure_failures >= max_infrastructure_failures:
                                    slot.status = "blocked_infrastructure"
                                    _write_state(
                                        state_path,
                                        all_slots,
                                        event="infrastructure_failure_threshold",
                                        root=root,
                                        pid=os.getpid(),
                                    )
                                    return 3
                            slot.status = "pending"
                            if not slot.last_artifact_reasons:
                                slot.last_reason = "startup_or_preflight_failure"
                            slot.next_launch_at = now + min(retry_seconds * max(1, slot.attempts), 300.0)
                if slot.status == "finished":
                    continue
                if slot.attempts >= max_attempts:
                    slot.status = "attempt_limit_exhausted"
                    continue
                if slot.pid is not None or now < slot.next_launch_at:
                    continue
                judge_kind = "coding" if slot.dataset in CODING_DATASETS else "formal"
                if not _health_ok(urls[judge_kind]):
                    slot.last_reason = "judge_health_gate"
                    slot.next_launch_at = now + min(retry_seconds, 30.0)
                    continue
                try:
                    slot.attempts += 1
                    process = _launch(slot, repo=repo, root=root, urls=urls, caches=caches, log_root=log_root)
                    processes[process.pid] = process
                except (OSError, RuntimeError) as exc:
                    slot.pid = None
                    slot.status = "pending"
                    slot.last_reason = type(exc).__name__
                    slot.next_launch_at = now + min(retry_seconds * max(1, slot.attempts), 300.0)
            _write_state(state_path, all_slots, event=f"wave_{repeat}_heartbeat", root=root, pid=os.getpid())
            if all(slot.status == "finished" for slot in slots):
                break
            if all(slot.status == "attempt_limit_exhausted" for slot in slots):
                _write_state(state_path, all_slots, event=f"wave_{repeat}_attempt_limit_exhausted", root=root, pid=os.getpid())
                return 2
            time.sleep(2.0)
        if stopping:
            _write_state(state_path, all_slots, event="stopped_children_left_running", root=root, pid=os.getpid())
            return 0
        _write_state(state_path, all_slots, event=f"wave_{repeat}_finished", root=root, pid=os.getpid())
    _write_state(state_path, all_slots, event="all_repeats_finished", root=root, pid=os.getpid())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--root", type=Path, default=Path("runs/figure4_formal_6datasets"))
    parser.add_argument("--state", type=Path, default=Path("tmp/figure4_formal_6datasets/state.json"))
    parser.add_argument("--logs", type=Path, default=Path("tmp/figure4_formal_6datasets/logs"))
    parser.add_argument("--max-attempts", type=int, default=20)
    parser.add_argument("--retry-seconds", type=float, default=30.0)
    parser.add_argument(
        "--max-infrastructure-failures",
        type=int,
        default=3,
        help="stop instead of endlessly relaunching after this many invalid terminal artifacts",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="ignore persisted supervisor state and start missing matrix arms afresh",
    )
    args = parser.parse_args()
    return run(
        args.repo.resolve(),
        state_path=args.state.resolve(),
        log_root=args.logs.resolve(),
        root=args.root.resolve(),
        max_attempts=max(1, args.max_attempts),
        retry_seconds=max(1.0, args.retry_seconds),
        max_infrastructure_failures=max(1, args.max_infrastructure_failures),
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    raise SystemExit(main())
