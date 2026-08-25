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

from contextswarm_mini.formal_matrix_artifacts import artifact_eligibility


DATASETS = ("clever", "icpc_wf_2025", "matholympiadbench", "putnambench", "usaco", "verina")
POLICIES = ("uniform_refill", "task_state", "trace_state", "llm_scheduler")
FORMAL_DATASETS = {"clever", "matholympiadbench", "putnambench", "verina"}
CODING_DATASETS = {"icpc_wf_2025", "usaco"}
DEFAULT_FORMAL_URL = "http://host.docker.internal:38100"
DEFAULT_CODING_URL = "http://host.docker.internal:38081"


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


def _refresh(slot: Slot, root: Path) -> None:
    slot.last_artifact_reasons = None
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
) -> int:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    all_slots: list[Slot] = []
    processes: dict[int, subprocess.Popen[bytes]] = {}
    urls, caches = _discover_capabilities()

    for repeat in (1, 2, 3):
        slots = _slots_for_wave(repeat)
        all_slots.extend(slots)
        _write_state(state_path, all_slots, event=f"wave_{repeat}_starting", root=root, pid=os.getpid())
        while not stopping:
            now = _now()
            for slot in slots:
                _refresh(slot, root)
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
    args = parser.parse_args()
    return run(
        args.repo.resolve(),
        state_path=args.state.resolve(),
        log_root=args.logs.resolve(),
        root=args.root.resolve(),
        max_attempts=max(1, args.max_attempts),
        retry_seconds=max(1.0, args.retry_seconds),
        max_infrastructure_failures=max(1, args.max_infrastructure_failures),
    )


if __name__ == "__main__":
    raise SystemExit(main())
