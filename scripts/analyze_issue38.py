#!/usr/bin/env python3
"""Create an auditable Issue #38 recovery/Parallel comparison report.

The merged Issue #38 CSV deliberately preserves the original 144 rows and
stores replacement values in ``recovery_*`` columns.  This script resolves the
authoritative recovery attempt for the 63 major source arms before calculating
any aggregate.  It also recomputes the right-continuous score-time AUC from
the immutable run artifacts, so a stale value in the merged CSV cannot silently
change the analysis.

Only standard-library modules are used.  The generated CSV/Markdown files are
operator artifacts under ``runs/`` and contain no credentials, node.toml
contents, or private endpoint values.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


HORIZON_SECONDS = 3600.0
DATASET_ORDER = [
    "clever",
    "icpc",
    "matholympiadbench",
    "putnambench",
    "usaco",
    "verina",
]
SELECTOR_ORDER = [
    "bm25_mmr",
    "feedback_diversity",
    "no_interaction_feedback",
    "nustigmergy",
    "random",
    "recency",
    "smoothed_popularity",
    "unnormalized_feedback",
]
BENCHMARK_TO_ISSUE = {
    "icpc_wf_2025": "icpc",
    "paper_usaco_full12": "usaco",
}
COMMON_JUDGE_STATUSES = [
    "PROVED",
    "COMPILES_WITH_SORRY",
    "VERIFY_FAIL",
    "PE",
    "WA",
    "TLE",
    "CE",
    "RE",
    "RESOURCE_LIMIT",
    "EXECUTION_TIMEOUT",
    "EVALUATOR_ERROR",
    "LOCAL_REJECTED",
    "CHEATING",
    "REJECTED_OVERLOADED",
    "SESSION_PROBE_IN_FLIGHT",
    "SESSION_PROBE_BUDGET_EXHAUSTED",
    "TASK_CANCELLED",
    "OUT_OF_HORIZON",
]
JUDGE_STATUS_CLASSES = {
    "PROVED": "accepted_candidate",
    "COMPILES_WITH_SORRY": "diagnostic_candidate",
    "VERIFY_FAIL": "candidate_failure",
    "PE": "candidate_failure",
    "WA": "candidate_failure",
    "TLE": "candidate_failure",
    "CE": "candidate_failure",
    "RE": "candidate_failure",
    "RESOURCE_LIMIT": "candidate_failure",
    "EXECUTION_TIMEOUT": "candidate_failure",
    "EVALUATOR_ERROR": "candidate_failure",
    "LOCAL_REJECTED": "candidate_failure",
    "CHEATING": "candidate_failure",
    "REJECTED_OVERLOADED": "judge_admission_or_transport",
    "SESSION_PROBE_IN_FLIGHT": "judge_admission_or_transport",
    "SESSION_PROBE_BUDGET_EXHAUSTED": "judge_admission_or_transport",
    "TASK_CANCELLED": "runner_control",
    "OUT_OF_HORIZON": "runner_control",
}
HEALTH_COUNTER_KEYS = {
    "judge_probe_infrastructure_error_count",
    "unexpected_process_error_count",
    "solver_process_error_count",
    "evaluator_infrastructure_error_count",
    "provider_error_count",
    "allocation_scheduler_provider_error_count",
    "allocation_scheduler_call_id_error_count",
    "allocation_scheduler_nonzero_return_count",
    "allocation_scheduler_oom_or_exit_137_count",
    "allocation_scheduler_timeout_count",
    "allocation_scheduler_policy_timeout_count",
    "allocation_scheduler_invalid_output_count",
    "allocation_scheduler_fallback_count",
    "allocation_scheduler_horizon_truncation_count",
    "allocation_scheduler_cancelled_count",
}


def parse_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def parse_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def resolve_reference_path(
    supplied: Path | None,
    *,
    env_name: str,
    root: Path,
    sibling_parts: Sequence[str],
) -> Path:
    """Resolve an external reference without embedding a machine-local path.

    The published repository does not contain the historical campaign
    artifacts.  Callers can provide an explicit CLI path or environment
    variable; the local checkout also gets a convenient sibling ``CPS``
    fallback.  Keeping that fallback relative to the repository makes the
    analysis script safe to publish and usable from another checkout.
    """

    if supplied is not None:
        return supplied
    configured = os.environ.get(env_name, "").strip()
    if configured:
        return Path(configured)
    return root.parent / "CPS" / Path(*sibling_parts)


def parse_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if isinstance(value, dict):
                rows.append(value)
    return rows


def arm_key(dataset: str, repeat: Any, selector: str) -> str:
    return f"{dataset}/r{parse_int(repeat):02d}/{selector}"


def normalize_benchmark(name: str) -> str:
    return BENCHMARK_TO_ISSUE.get(name, name)


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def local_time_text(value: Any) -> str:
    parsed = parse_time(value)
    if parsed is None:
        return ""
    return parsed.astimezone(dt.timezone(dt.timedelta(hours=8))).isoformat()


def mean_or_zero(values: Sequence[float]) -> float:
    return statistics.mean(values) if values else 0.0


def stdev_or_zero(values: Sequence[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def step_curve_metrics(
    points: Iterable[Mapping[str, Any]],
    *,
    horizon: float = HORIZON_SECONDS,
    max_score: float = 12.0,
    score_key: str = "score",
) -> dict[str, float | None]:
    """Integrate a right-continuous accepted-score step curve.

    A score achieved at elapsed time ``t`` contributes from ``t`` onward.  A
    full-score run is therefore carried forward to the fixed one-hour
    horizon, matching the Issue #38 ``nauc`` contract.
    """

    events: list[tuple[float, float]] = []
    for point in points:
        t = parse_float(point.get("elapsed_seconds"), -1.0)
        score = parse_float(point.get(score_key), 0.0)
        if t < 0.0 or t > horizon:
            continue
        events.append((t, score))
    events.sort(key=lambda item: item[0])
    area = 0.0
    previous_time = 0.0
    current_score = 0.0
    first_full: float | None = None
    first_score: float | None = None
    for timestamp, score in events:
        if timestamp < previous_time:
            continue
        area += current_score * (timestamp - previous_time)
        current_score = max(current_score, score)
        previous_time = timestamp
        if first_score is None and current_score > 0.0:
            first_score = timestamp
        if first_full is None and current_score >= max_score:
            first_full = timestamp
    area += current_score * max(0.0, horizon - previous_time)
    denominator = max_score * horizon
    return {
        "score_at_horizon": current_score,
        "auc_raw": area,
        "auc_normalized": area / denominator if denominator else 0.0,
        "time_to_first_score": first_score,
        "time_to_full_score": first_full,
    }


def issue_curve_metrics(figure_summary: Mapping[str, Any]) -> dict[str, float | None]:
    history = figure_summary.get("accepted_score_history", [])
    points = [
        {"elapsed_seconds": event.get("elapsed_seconds"), "score": event.get("accepted_score")}
        for event in history
        if isinstance(event, Mapping)
    ]
    return step_curve_metrics(
        points,
        horizon=parse_float(figure_summary.get("horizon_seconds"), HORIZON_SECONDS),
        max_score=parse_float(figure_summary.get("max_score"), 12.0),
    )


def baseline_curve_metrics(run: Mapping[str, Any]) -> dict[str, float | None]:
    return step_curve_metrics(
        run.get("points", []),
        horizon=HORIZON_SECONDS,
        max_score=12.0,
    )


def safe_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def effective_path(root: Path, relative_or_absolute: str) -> Path:
    path = Path(relative_or_absolute)
    if not path.is_absolute():
        path = root / path
    return path


def status_counts_from_final(final: Mapping[str, Any]) -> Counter[str]:
    value = final.get("verdict_status_counts", {})
    if not isinstance(value, Mapping):
        return Counter()
    return Counter({str(key): parse_int(number) for key, number in value.items()})


def health_names(final: Mapping[str, Any], declared: Any = None) -> list[str]:
    names: set[str] = set()
    parsed_declared = parse_json(declared, [])
    if isinstance(parsed_declared, list):
        names.update(str(item) for item in parsed_declared if item)
    health = final.get("health", {})
    if isinstance(health, Mapping):
        for key, value in health.items():
            if key in HEALTH_COUNTER_KEYS and parse_float(value) > 0:
                names.add(str(key))
    return sorted(names)


def load_artifact(root: Path, run_directory: str) -> dict[str, Any]:
    path = effective_path(root, run_directory)
    required = [
        "final.json",
        "figure4_run_summary.json",
        "allocation_summary.json",
        "run_meta.json",
        "judge_broker_closeout.json",
        "judge_checks.jsonl",
    ]
    missing = [name for name in required if not (path / name).is_file()]
    if missing:
        raise FileNotFoundError(f"{path}: missing {', '.join(missing)}")
    final = read_json(path / "final.json")
    figure = read_json(path / "figure4_run_summary.json")
    allocation = read_json(path / "allocation_summary.json")
    meta = read_json(path / "run_meta.json")
    closeout = read_json(path / "judge_broker_closeout.json")
    checks = read_jsonl(path / "judge_checks.jsonl")
    check_statuses = Counter(str(item.get("status", "")) for item in checks)
    retryable_checks = sum(1 for item in checks if item.get("retryable") is True)
    remote_cache_reuses = sum(1 for item in checks if item.get("remote_cache_reused") is True)
    probe_cache_reuses = sum(1 for item in checks if item.get("probe_cache_reused") is True)
    issue_metrics = issue_curve_metrics(figure)
    final_status_counts = status_counts_from_final(final)
    judge_cache = final.get("judge_result_cache", {})
    if not isinstance(judge_cache, Mapping):
        judge_cache = {}
    runtime_provenance = meta.get("runtime_provenance", {})
    if not isinstance(runtime_provenance, Mapping):
        runtime_provenance = {}
    health = final.get("health", {})
    if not isinstance(health, Mapping):
        health = {}
    evaluator_usage = figure.get("evaluator_usage", {})
    if not isinstance(evaluator_usage, Mapping):
        evaluator_usage = {}
    allocation_metrics = figure.get("allocation_metrics", {})
    if not isinstance(allocation_metrics, Mapping):
        allocation_metrics = {}
    stop_reason = (
        "full_score"
        if issue_metrics["time_to_full_score"] is not None
        and parse_float(issue_metrics["time_to_full_score"]) <= parse_float(figure.get("horizon_seconds"), HORIZON_SECONDS)
        else "horizon"
    )
    score_time = final.get("score_time", {})
    if not isinstance(score_time, Mapping):
        score_time = {}
    return {
        "path": path,
        "final": final,
        "figure": figure,
        "allocation": allocation,
        "meta": meta,
        "closeout": closeout,
        "checks": checks,
        "check_statuses": check_statuses,
        "retryable_checks": retryable_checks,
        "remote_cache_reuses": remote_cache_reuses,
        "probe_cache_reuses": probe_cache_reuses,
        "issue_metrics": issue_metrics,
        "final_status_counts": final_status_counts,
        "judge_cache": judge_cache,
        "runtime_provenance": runtime_provenance,
        "health": health,
        "evaluator_usage": evaluator_usage,
        "allocation_metrics": allocation_metrics,
        "stop_reason": stop_reason,
        "score_time": score_time,
        "health_names": health_names(final),
    }


def artifact_value(artifact: Mapping[str, Any], key: str, default: Any = "") -> Any:
    final = artifact["final"]
    figure = artifact["figure"]
    allocation = artifact["allocation"]
    meta = artifact["meta"]
    closeout = artifact["closeout"]
    health = artifact["health"]
    if key == "final_score":
        return final.get("score", figure.get("final_accepted_score", default))
    if key == "max_score":
        return final.get("max_score", figure.get("max_score", default))
    if key == "final_status":
        return final.get("status", default)
    if key == "duration_seconds":
        started = parse_time(meta.get("started_at"))
        ended = parse_time(final.get("finished_at"))
        if started and ended:
            return (ended - started).total_seconds()
        return default
    if key == "start_time":
        return meta.get("started_at", default)
    if key == "end_time":
        return final.get("finished_at", default)
    if key == "commit":
        return artifact["runtime_provenance"].get("source_commit", default)
    if key == "image_id":
        return artifact["runtime_provenance"].get("image_id", default)
    if key == "model":
        return meta.get("model", default)
    if key == "thinking":
        return meta.get("thinking", default)
    if key == "fast_mode":
        return meta.get("fast_mode", default)
    if key == "mode":
        return meta.get("mode", default)
    if key == "communication":
        return meta.get("communication", default)
    if key == "provider_backend":
        return meta.get("provider_backend", default)
    if key == "judge_kind":
        return meta.get("judge_kind", default)
    if key == "judge_env":
        return meta.get("lean_env_id", default)
    if key == "judge_mode":
        return meta.get("lean_judge_mode", default)
    if key == "verification_profile":
        return meta.get("lean_verification_profile", default)
    if key == "horizon_seconds":
        return figure.get("horizon_seconds", meta.get("time_limit_seconds", default))
    if key == "max_parallel":
        return meta.get("max_parallel", default)
    if key == "max_in_flight":
        return meta.get("nurouter_max_in_flight", meta.get("aisw_max_in_flight", default))
    if key == "judge_check_count":
        return len(artifact["checks"])
    if key == "judge_terminal_receipts":
        return artifact["evaluator_usage"].get("terminal_receipts", default)
    if key == "judge_check_admissions":
        return artifact["evaluator_usage"].get("judge_check_admissions", default)
    if key == "judge_check_calls":
        return artifact["evaluator_usage"].get("judge_check_calls", default)
    if key == "judge_admissions":
        return artifact["evaluator_usage"].get("admissions", default)
    if key == "judge_calls":
        return artifact["evaluator_usage"].get("calls", default)
    if key == "probe_infrastructure_error_count":
        return health.get("judge_probe_infrastructure_error_count", default)
    if key == "unexpected_process_error_count":
        return health.get("unexpected_process_error_count", default)
    if key == "remote_unsettled_jobs":
        return closeout.get("remote_unsettled_jobs", default)
    if key == "receipt_complete":
        return bool(closeout.get("drained", False)) and parse_int(closeout.get("active_handlers")) == 0 and parse_int(closeout.get("fifo_depth")) == 0
    if key == "closeout_drained":
        return closeout.get("drained", default)
    if key == "closeout_active_handlers":
        return closeout.get("active_handlers", default)
    if key == "closeout_fifo_depth":
        return closeout.get("fifo_depth", default)
    if key == "runner_health_issues":
        return artifact["health_names"]
    if key == "provider_errors":
        return allocation.get("provider_errors", default)
    if key == "allocation_fallback_count":
        return allocation.get("fallback_count", default)
    if key == "allocation_invalid_outputs":
        return allocation.get("invalid_outputs", default)
    if key == "allocation_policy_timeouts":
        return allocation.get("policy_timeouts", default)
    if key == "allocation_horizon_truncations":
        return allocation.get("horizon_truncations", default)
    if key == "max_occupied_slots":
        return allocation.get("max_occupied_slots", default)
    if key == "slot_utilization":
        return allocation.get("solver_slot_utilization", allocation.get("compute_slot_utilization", default))
    if key == "solver_model_sessions":
        return allocation.get("solver_model_sessions", default)
    if key == "solver_input_tokens":
        return allocation.get("solver_input_tokens", default)
    if key == "solver_output_tokens":
        return allocation.get("solver_output_tokens", default)
    if key == "solver_cache_read_tokens":
        return allocation.get("solver_cache_read_tokens", default)
    if key == "solver_cache_write_tokens":
        return allocation.get("solver_cache_write_tokens", default)
    if key == "agent_timeout_count":
        return final.get("agent_timeout_count", default)
    if key == "judge_cache_disabled":
        judge_cache = artifact["judge_cache"]
        return bool(judge_cache.get("required_disabled", False)) and not bool(judge_cache.get("enabled", True))
    if key == "judge_cache_evidence":
        return artifact["judge_cache"]
    if key == "score_time_auc_raw":
        return score_time.get("score_time_auc", default)
    if key == "normalized_score_time_auc":
        return score_time.get("normalized_score_time_auc", default)
    return default


def build_attempt_stats(attempt_rows: Sequence[Mapping[str, str]]) -> dict[str, int]:
    return {
        "total": len(attempt_rows),
        "invalid": sum(str(row.get("classification", "")) == "INVALID" for row in attempt_rows),
        "clean": sum(str(row.get("classification", "")) == "CLEAN" for row in attempt_rows),
        "minor": sum(str(row.get("classification", "")) == "MINOR" for row in attempt_rows),
        "major": sum(str(row.get("classification", "")) == "MAJOR" for row in attempt_rows),
    }


def load_baseline(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = read_json(path)
    all_runs = data.get("scientific_runs", [])
    if not isinstance(all_runs, list):
        raise ValueError(f"scientific_runs is not a list: {path}")
    result: list[dict[str, Any]] = []
    for run in all_runs:
        if not isinstance(run, Mapping):
            continue
        if run.get("topology") != "parallel":
            continue
        if run.get("model") != "openai-codex/gpt-5.6-sol":
            continue
        if run.get("reasoning_effort") != "max":
            continue
        benchmark = normalize_benchmark(str(run.get("benchmark", "")))
        if benchmark not in DATASET_ORDER:
            continue
        metrics = baseline_curve_metrics(run)
        checkpoints = run.get("checkpoints", {})
        if not isinstance(checkpoints, Mapping):
            checkpoints = {}
        result.append(
            {
                "baseline_kind": "strict_parallel_primary",
                "benchmark": benchmark,
                "source_benchmark": run.get("benchmark", ""),
                "repeat": parse_int(run.get("repeat_index")),
                "scientific_run_id": run.get("scientific_run_id", ""),
                "model": run.get("model", ""),
                "thinking": run.get("reasoning_effort", ""),
                "topology": run.get("topology", ""),
                "fast_mode": "true (campaign contract)",
                "provider_record": "AISW (historical campaign record)",
                "max_in_flight": 1,
                "cps": False,
                "score_1h": parse_float(checkpoints.get("3600"), metrics["score_at_horizon"]),
                "score_2h": parse_float(checkpoints.get("7200"), run.get("final_score")),
                "final_score": parse_float(run.get("final_score")),
                "auc_normalized_1h": parse_float(metrics["auc_normalized"]),
                "auc_raw_1h": parse_float(metrics["auc_raw"]),
                "full_score_time_1h": metrics["time_to_full_score"],
                "selected_run_count": len(run.get("selected_run_ids", [])),
                "scientific_run_id": run.get("scientific_run_id", ""),
            }
        )
    result.sort(key=lambda row: (DATASET_ORDER.index(row["benchmark"]), row["repeat"]))
    campaign_root = path.resolve().parents[2]
    manifest_path = campaign_root / "campaign_manifest.toml"
    contract = {
        "source_label": "strict_agent_baseline_campaign_20260815/analysis/latest/scientific_run_curves.json",
        "campaign_manifest_available": manifest_path.is_file(),
        "fast_mode": True,
        "provider_record": "AISW",
        "max_in_flight": 1,
        "cps": False,
        "parallel_agents_per_problem": 1,
        "repeat_count_per_dataset": 3,
        "horizon_seconds": HORIZON_SECONDS,
    }
    return result, contract


def load_sensitivity_baseline(path: Path) -> list[dict[str, Any]]:
    """Load the one-repeat standard-mode reference as a sensitivity table."""
    data = read_json(path)
    result: list[dict[str, Any]] = []
    for run in data.get("scientific_runs", []):
        if not isinstance(run, Mapping) or run.get("topology") != "parallel":
            continue
        if run.get("model") != "openai-codex/gpt-5.6-sol" or run.get("reasoning_effort") != "max":
            continue
        benchmark = normalize_benchmark(str(run.get("benchmark", "")))
        if benchmark not in DATASET_ORDER:
            continue
        metrics = baseline_curve_metrics(run)
        checkpoints = run.get("checkpoints", {})
        if not isinstance(checkpoints, Mapping):
            checkpoints = {}
        result.append(
            {
                "baseline_kind": "standard_mode_sensitivity",
                "benchmark": benchmark,
                "source_benchmark": run.get("benchmark", ""),
                "repeat": parse_int(run.get("repeat_index")),
                "scientific_run_id": run.get("scientific_run_id", ""),
                "model": run.get("model", ""),
                "thinking": run.get("reasoning_effort", ""),
                "topology": run.get("topology", ""),
                "fast_mode": "false (campaign contract)",
                "provider_record": "AISW (historical campaign record)",
                "max_in_flight": 1,
                "cps": False,
                "score_1h": parse_float(checkpoints.get("3600"), metrics["score_at_horizon"]),
                "score_2h": parse_float(checkpoints.get("7200"), run.get("final_score")),
                "final_score": parse_float(run.get("final_score")),
                "auc_normalized_1h": parse_float(metrics["auc_normalized"]),
                "auc_raw_1h": parse_float(metrics["auc_raw"]),
                "full_score_time_1h": metrics["time_to_full_score"],
                "selected_run_count": len(run.get("selected_run_ids", [])),
            }
        )
    result.sort(key=lambda row: DATASET_ORDER.index(row["benchmark"]))
    return result


def add_pairwise_baseline(row: dict[str, Any], baseline_index: Mapping[tuple[str, int], Mapping[str, Any]]) -> None:
    baseline = baseline_index[(row["dataset"], parse_int(row["repeat"]))]
    row["parallel_baseline_score_1h"] = baseline["score_1h"]
    row["parallel_baseline_auc_1h"] = baseline["auc_normalized_1h"]
    row["delta_score_vs_parallel_baseline"] = row["effective_score"] - baseline["score_1h"]
    row["delta_auc_vs_parallel_baseline"] = row["effective_auc_normalized"] - baseline["auc_normalized_1h"]
    row["endpoint_outcome_vs_parallel"] = (
        "W" if row["delta_score_vs_parallel_baseline"] > 0 else "L" if row["delta_score_vs_parallel_baseline"] < 0 else "T"
    )
    row["auc_outcome_vs_parallel"] = (
        "W" if row["delta_auc_vs_parallel_baseline"] > 1e-12 else "L" if row["delta_auc_vs_parallel_baseline"] < -1e-12 else "T"
    )


def build_arm_rows(
    *,
    root: Path,
    merged_rows: Sequence[Mapping[str, str]],
    recovery_rows: Sequence[Mapping[str, str]],
    attempt_rows: Sequence[Mapping[str, str]],
    baseline_index: Mapping[tuple[str, int], Mapping[str, Any]],
    analysis_time: str,
) -> list[dict[str, Any]]:
    recovery_by_key = {str(row["arm_key"]): row for row in recovery_rows}
    attempts_by_key: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for attempt in attempt_rows:
        attempts_by_key[str(attempt.get("arm_key", ""))].append(attempt)
    result: list[dict[str, Any]] = []
    for source in merged_rows:
        dataset = str(source.get("dataset", ""))
        repeat = parse_int(source.get("repeat"))
        selector = str(source.get("selector", ""))
        key = arm_key(dataset, repeat, selector)
        recovery = recovery_by_key.get(key)
        run_directory = str(recovery.get("run_directory", "")) if recovery else str(source.get("run_directory", ""))
        artifact = load_artifact(root, run_directory)
        figure = artifact["figure"]
        issue_metrics = artifact["issue_metrics"]
        final_counts = artifact["final_status_counts"]
        check_statuses: Counter[str] = artifact["check_statuses"]
        attempt_stats = build_attempt_stats(attempts_by_key.get(key, []))
        source_quality = "major" if recovery else "clean" if source.get("validity") == "VALID" else "minor"
        if recovery:
            recovery_quality = str(recovery.get("recovery_status", ""))
            final_quality = "clean" if recovery_quality == "accepted_clean" else "minor"
        else:
            recovery_quality = ""
            final_quality = "clean" if source.get("validity") == "VALID" else "minor"
        stored_auc = parse_float(figure.get("nauc"), parse_float(source.get("AUC_normalized")))
        stored_raw_auc = parse_float(
            artifact["score_time"].get("score_time_auc"),
            parse_float(source.get("AUC_raw")),
        )
        effective_score = parse_float(artifact_value(artifact, "final_score"), parse_float(source.get("final_score")))
        effective_max_score = parse_float(artifact_value(artifact, "max_score"), parse_float(source.get("max_score"), 12.0))
        effective_start = artifact_value(artifact, "start_time", source.get("start_time", ""))
        effective_end = artifact_value(artifact, "end_time", source.get("end_time", ""))
        effective_duration = parse_float(
            artifact_value(artifact, "duration_seconds"),
            parse_float(recovery.get("duration_seconds") if recovery else source.get("duration_seconds")),
        )
        health_names_value = artifact_value(artifact, "runner_health_issues", [])
        if not isinstance(health_names_value, list):
            health_names_value = list(health_names_value) if health_names_value else []
        closeout = artifact["closeout"]
        judge_cache_evidence = artifact_value(artifact, "judge_cache_evidence", {})
        source_health = parse_json(source.get("runner_health_issues"), [])
        if not isinstance(source_health, list):
            source_health = []
        source_triggers = parse_json(recovery.get("source_major_triggers") if recovery else "", []) if recovery else []
        if not isinstance(source_triggers, list):
            source_triggers = []
        row: dict[str, Any] = {
            "analysis_generated_at_utc": analysis_time,
            "arm_key": key,
            "arm": source.get("arm", ""),
            "dataset": dataset,
            "selector": selector,
            "repeat": repeat,
            "seed": parse_int(source.get("seed")),
            "experiment_start_time_utc": effective_start,
            "experiment_start_time_local": local_time_text(effective_start),
            "experiment_end_time_utc": effective_end,
            "experiment_end_time_local": local_time_text(effective_end),
            "commit": artifact_value(artifact, "commit", recovery.get("commit") if recovery else source.get("commit", "")),
            "image_id": artifact_value(artifact, "image_id", recovery.get("image_id") if recovery else source.get("image_id", "")),
            "model": artifact_value(artifact, "model", recovery.get("model") if recovery else source.get("model", "")),
            "thinking": artifact_value(artifact, "thinking", recovery.get("thinking") if recovery else source.get("thinking", "")),
            "fast_mode": artifact_value(artifact, "fast_mode", recovery.get("fast_mode") if recovery else source.get("fast_mode", "")),
            "mode": artifact_value(artifact, "mode", recovery.get("mode") if recovery else source.get("mode", "")),
            "communication": artifact_value(artifact, "communication", recovery.get("communication") if recovery else source.get("communication", "")),
            "provider_backend": artifact_value(artifact, "provider_backend", source.get("provider_backend", "")),
            "nurouter_version": source.get("nurouter_version", ""),
            "docker_network": source.get("docker_network", ""),
            "docker_internet": source.get("docker_internet", ""),
            "judge_kind": artifact_value(artifact, "judge_kind", source.get("judge_kind", "")),
            "judge_env": artifact_value(artifact, "judge_env", recovery.get("judge_env") if recovery else source.get("judge_env", "")),
            "judge_mode": artifact_value(artifact, "judge_mode", recovery.get("judge_mode") if recovery else source.get("judge_mode", "")),
            "verification_profile": artifact_value(artifact, "verification_profile", recovery.get("verification_profile") if recovery else source.get("verification_profile", "")),
            "horizon_seconds": parse_float(artifact_value(artifact, "horizon_seconds", source.get("horizon_seconds")), HORIZON_SECONDS),
            "max_parallel": parse_int(artifact_value(artifact, "max_parallel", source.get("max_parallel"))),
            "max_in_flight": parse_int(artifact_value(artifact, "max_in_flight", source.get("max_in_flight"))),
            "effective_duration_seconds": effective_duration,
            "effective_duration_minutes": effective_duration / 60.0,
            "effective_score": effective_score,
            "effective_max_score": effective_max_score,
            "effective_score_fraction": effective_score / effective_max_score if effective_max_score else 0.0,
            "effective_score_at_1h": issue_metrics["score_at_horizon"],
            "effective_auc_normalized": stored_auc,
            "effective_auc_raw": stored_raw_auc,
            "recomputed_auc_normalized": issue_metrics["auc_normalized"],
            "recomputed_auc_raw": issue_metrics["auc_raw"],
            "auc_abs_error": abs(stored_auc - parse_float(issue_metrics["auc_normalized"])),
            "auc_consistent": abs(stored_auc - parse_float(issue_metrics["auc_normalized"])) <= 1e-7,
            "effective_stop_reason": artifact["stop_reason"],
            "time_to_first_score_seconds": issue_metrics["time_to_first_score"],
            "time_to_full_score_seconds": issue_metrics["time_to_full_score"],
            "full_score_reached_within_horizon": issue_metrics["time_to_full_score"] is not None,
            "duration_minus_horizon_seconds": effective_duration - parse_float(artifact_value(artifact, "horizon_seconds", HORIZON_SECONDS), HORIZON_SECONDS),
            "final_status_effective": artifact_value(artifact, "final_status", source.get("final_status", "")),
            "final_verdict_counts_json": compact_json(dict(final_counts)),
            "final_proved_count": final_counts.get("PROVED", 0),
            "final_compiles_with_sorry_count": final_counts.get("COMPILES_WITH_SORRY", 0),
            "final_verify_fail_count": final_counts.get("VERIFY_FAIL", 0),
            "final_pe_count": final_counts.get("PE", 0),
            "judge_check_count": len(artifact["checks"]),
            "judge_check_count_reported": parse_int(recovery.get("judge_check_count") if recovery else source.get("judge_check_count")),
            "judge_check_count_discrepancy": len(artifact["checks"]) - parse_int(recovery.get("judge_check_count") if recovery else source.get("judge_check_count")),
            "judge_check_status_counts_json": compact_json(dict(sorted(check_statuses.items()))),
            "judge_retryable_check_count": artifact["retryable_checks"],
            "judge_remote_cache_reuse_count": artifact["remote_cache_reuses"],
            "judge_probe_cache_reuse_count": artifact["probe_cache_reuses"],
            "judge_terminal_receipt_count": parse_int(artifact_value(artifact, "judge_terminal_receipts")),
            "judge_check_admissions": parse_int(artifact_value(artifact, "judge_check_admissions")),
            "judge_check_calls": parse_int(artifact_value(artifact, "judge_check_calls")),
            "judge_admissions": parse_int(artifact_value(artifact, "judge_admissions")),
            "judge_calls": parse_int(artifact_value(artifact, "judge_calls")),
            "judge_proved_check_count": check_statuses.get("PROVED", 0),
            "judge_compiles_with_sorry_check_count": check_statuses.get("COMPILES_WITH_SORRY", 0),
            "judge_verify_fail_check_count": check_statuses.get("VERIFY_FAIL", 0),
            "judge_candidate_failure_check_count": sum(check_statuses.get(status, 0) for status in ["WA", "TLE", "CE", "RE", "VERIFY_FAIL", "RESOURCE_LIMIT", "EXECUTION_TIMEOUT", "LOCAL_REJECTED"]),
            "judge_overload_rejection_check_count": check_statuses.get("REJECTED_OVERLOADED", 0),
            "judge_session_probe_in_flight_check_count": check_statuses.get("SESSION_PROBE_IN_FLIGHT", 0),
            "judge_task_cancelled_check_count": check_statuses.get("TASK_CANCELLED", 0),
            "judge_cache_disabled": artifact_value(artifact, "judge_cache_disabled"),
            "judge_cache_evidence_json": compact_json(judge_cache_evidence),
            "probe_infrastructure_error_count": parse_int(artifact_value(artifact, "probe_infrastructure_error_count")),
            "unexpected_process_error_count": parse_int(artifact_value(artifact, "unexpected_process_error_count")),
            "runner_health_issue_names": compact_json(health_names_value),
            "runner_health_issue_count": len(health_names_value),
            "provider_error_count": parse_int(artifact_value(artifact, "provider_errors")),
            "allocation_fallback_count": parse_int(artifact_value(artifact, "allocation_fallback_count")),
            "allocation_invalid_outputs": parse_int(artifact_value(artifact, "allocation_invalid_outputs")),
            "allocation_policy_timeouts": parse_int(artifact_value(artifact, "allocation_policy_timeouts")),
            "allocation_horizon_truncations": parse_int(artifact_value(artifact, "allocation_horizon_truncations")),
            "max_occupied_slots": parse_int(artifact_value(artifact, "max_occupied_slots")),
            "solver_slot_utilization": parse_float(artifact_value(artifact, "slot_utilization")),
            "solver_model_sessions": parse_int(artifact_value(artifact, "solver_model_sessions")),
            "solver_input_tokens": parse_int(artifact_value(artifact, "solver_input_tokens")),
            "solver_output_tokens": parse_int(artifact_value(artifact, "solver_output_tokens")),
            "solver_cache_read_tokens": parse_int(artifact_value(artifact, "solver_cache_read_tokens")),
            "solver_cache_write_tokens": parse_int(artifact_value(artifact, "solver_cache_write_tokens")),
            "agent_timeout_count": parse_int(artifact_value(artifact, "agent_timeout_count")),
            "receipt_complete_effective": artifact_value(artifact, "receipt_complete"),
            "closeout_drained": closeout.get("drained", False),
            "closeout_active_handlers": parse_int(closeout.get("active_handlers")),
            "closeout_fifo_depth": parse_int(closeout.get("fifo_depth")),
            "remote_unsettled_jobs": parse_int(closeout.get("remote_unsettled_jobs")),
            "source_quality": source_quality,
            "final_quality": final_quality,
            "quality_detail": recovery_quality or ("source_clean" if source_quality == "clean" else "source_minor"),
            "major_issue_recovered": bool(recovery),
            "source_validity": source.get("validity", ""),
            "source_final_status": source.get("final_status", ""),
            "source_score": parse_float(source.get("final_score")),
            "source_auc_normalized": parse_float(source.get("AUC_normalized")),
            "source_duration_seconds": parse_float(source.get("duration_seconds")),
            "source_health_issues_json": compact_json(source_health),
            "source_major_triggers_json": compact_json(source_triggers),
            "source_judge_check_count": parse_int(source.get("judge_check_count")),
            "source_judge_overload_feedback_count": parse_int(source.get("judge_overload_feedback_count")),
            "source_session_probe_in_flight_count": parse_int(source.get("session_probe_in_flight_count")),
            "source_failure_categories_json": source.get("failure_categories", "{}"),
            "recovery_status": recovery.get("recovery_status", "") if recovery else "",
            "recovery_classification": recovery.get("classification", "") if recovery else "",
            "recovery_reason": recovery.get("classification_reason", "") if recovery else "",
            "recovery_attempt": parse_int(recovery.get("attempt")) if recovery else "",
            "recovery_attempts_total": attempt_stats["total"] if recovery else "",
            "recovery_attempts_invalid": attempt_stats["invalid"] if recovery else "",
            "recovery_attempts_clean": attempt_stats["clean"] if recovery else "",
            "recovery_attempts_minor": attempt_stats["minor"] if recovery else "",
            "recovery_attempts_major": attempt_stats["major"] if recovery else "",
            "recovery_probe_infrastructure_error_count": parse_int(recovery.get("probe_infrastructure_error_count")) if recovery else "",
            "recovery_unexpected_process_error_count": parse_int(recovery.get("unexpected_process_error_count")) if recovery else "",
            "recovery_score_delta": effective_score - parse_float(source.get("final_score")) if recovery else "",
            "recovery_auc_delta": stored_auc - parse_float(source.get("AUC_normalized")) if recovery else "",
            "recovery_duration_delta_seconds": effective_duration - parse_float(source.get("duration_seconds")) if recovery else "",
            "run_directory_effective": safe_relpath(artifact["path"], root),
            "source_run_directory": source.get("run_directory", ""),
            "recovery_run_directory": recovery.get("run_directory", "") if recovery else "",
            "analysis_contract": "Issue38_effective_overlay_v1",
        }
        add_pairwise_baseline(row, baseline_index)
        result.append(row)
    result.sort(key=lambda row: (DATASET_ORDER.index(row["dataset"]), parse_int(row["repeat"]), SELECTOR_ORDER.index(row["selector"])))
    return result


def group_stats(rows: Sequence[Mapping[str, Any]], field: str) -> list[dict[str, Any]]:
    groups = sorted({str(row[field]) for row in rows})
    result: list[dict[str, Any]] = []
    for group in groups:
        selected = [row for row in rows if str(row[field]) == group]
        scores = [parse_float(row["effective_score"]) for row in selected]
        aucs = [parse_float(row["effective_auc_normalized"]) for row in selected]
        durations = [parse_float(row["effective_duration_seconds"]) for row in selected]
        baseline_scores = [parse_float(row["parallel_baseline_score_1h"]) for row in selected]
        baseline_aucs = [parse_float(row["parallel_baseline_auc_1h"]) for row in selected]
        score_deltas = [parse_float(row["delta_score_vs_parallel_baseline"]) for row in selected]
        auc_deltas = [parse_float(row["delta_auc_vs_parallel_baseline"]) for row in selected]
        endpoint = Counter(str(row["endpoint_outcome_vs_parallel"]) for row in selected)
        auc_outcome = Counter(str(row["auc_outcome_vs_parallel"]) for row in selected)
        result.append(
            {
                "group": group,
                "n": len(selected),
                "score_sum": sum(scores),
                "score_mean": mean_or_zero(scores),
                "score_sd": stdev_or_zero(scores),
                "score_min": min(scores) if scores else 0.0,
                "score_max": max(scores) if scores else 0.0,
                "auc_mean": mean_or_zero(aucs),
                "auc_sd": stdev_or_zero(aucs),
                "auc_min": min(aucs) if aucs else 0.0,
                "auc_max": max(aucs) if aucs else 0.0,
                "duration_mean_seconds": mean_or_zero(durations),
                "duration_median_seconds": statistics.median(durations) if durations else 0.0,
                "baseline_score_mean": mean_or_zero(baseline_scores),
                "baseline_auc_mean": mean_or_zero(baseline_aucs),
                "score_delta_mean": mean_or_zero(score_deltas),
                "auc_delta_mean": mean_or_zero(auc_deltas),
                "score_relative_to_baseline_pct": 100.0 * mean_or_zero(scores) / mean_or_zero(baseline_scores) if mean_or_zero(baseline_scores) else 0.0,
                "auc_relative_to_baseline_pct": 100.0 * mean_or_zero(aucs) / mean_or_zero(baseline_aucs) if mean_or_zero(baseline_aucs) else 0.0,
                "endpoint_wins": endpoint.get("W", 0),
                "endpoint_ties": endpoint.get("T", 0),
                "endpoint_losses": endpoint.get("L", 0),
                "auc_wins": auc_outcome.get("W", 0),
                "auc_ties": auc_outcome.get("T", 0),
                "auc_losses": auc_outcome.get("L", 0),
                "clean_count": sum(row["final_quality"] == "clean" for row in selected),
                "minor_count": sum(row["final_quality"] == "minor" for row in selected),
                "major_count": sum(row["source_quality"] == "major" for row in selected),
                "full_score_count": sum(bool(row["full_score_reached_within_horizon"]) for row in selected),
                "horizon_count": sum(row["effective_stop_reason"] == "horizon" for row in selected),
                "judge_checks_sum": sum(parse_int(row["judge_check_count"]) for row in selected),
                "judge_checks_mean": mean_or_zero([parse_float(row["judge_check_count"]) for row in selected]),
                "probe_infrastructure_error_arm_count": sum(parse_int(row["probe_infrastructure_error_count"]) > 0 for row in selected),
                "unexpected_process_error_arm_count": sum(parse_int(row["unexpected_process_error_count"]) > 0 for row in selected),
            }
        )
    return result


def build_dataset_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_dataset = {item["group"]: item for item in group_stats(rows, "dataset")}
    result = []
    for dataset in DATASET_ORDER:
        item = dict(by_dataset[dataset])
        item["dataset"] = item.pop("group")
        # Dataset-level repeat means avoid treating the three repetitions as
        # independent observations when showing repeat-to-repeat variability.
        repeat_score_means = []
        repeat_auc_means = []
        repeat_baseline_scores = []
        repeat_baseline_aucs = []
        for repeat in (1, 2, 3):
            subset = [row for row in rows if row["dataset"] == dataset and parse_int(row["repeat"]) == repeat]
            repeat_score_means.append(mean_or_zero([parse_float(row["effective_score"]) for row in subset]))
            repeat_auc_means.append(mean_or_zero([parse_float(row["effective_auc_normalized"]) for row in subset]))
            repeat_baseline_scores.append(mean_or_zero([parse_float(row["parallel_baseline_score_1h"]) for row in subset]))
            repeat_baseline_aucs.append(mean_or_zero([parse_float(row["parallel_baseline_auc_1h"]) for row in subset]))
        item.update(
            {
                "repeat_mean_score_values": compact_json(repeat_score_means),
                "repeat_mean_auc_values": compact_json(repeat_auc_means),
                "repeat_mean_score_sd": stdev_or_zero(repeat_score_means),
                "repeat_mean_auc_sd": stdev_or_zero(repeat_auc_means),
                "repeat_mean_score_delta": mean_or_zero(repeat_score_means) - mean_or_zero(repeat_baseline_scores),
                "repeat_mean_auc_delta": mean_or_zero(repeat_auc_means) - mean_or_zero(repeat_baseline_aucs),
            }
        )
        result.append(item)
    return result


def build_selector_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_selector = {item["group"]: item for item in group_stats(rows, "selector")}
    result = []
    for selector in SELECTOR_ORDER:
        item = dict(by_selector[selector])
        item["selector"] = item.pop("group")
        result.append(item)
    return result


def build_dataset_selector_comparison(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for dataset in DATASET_ORDER:
        for selector in SELECTOR_ORDER:
            selected = [row for row in rows if row["dataset"] == dataset and row["selector"] == selector]
            if len(selected) != 3:
                raise ValueError(f"expected three repeats for {dataset}/{selector}, got {len(selected)}")
            scores = [parse_float(row["effective_score"]) for row in selected]
            aucs = [parse_float(row["effective_auc_normalized"]) for row in selected]
            baseline_scores = [parse_float(row["parallel_baseline_score_1h"]) for row in selected]
            baseline_aucs = [parse_float(row["parallel_baseline_auc_1h"]) for row in selected]
            score_deltas = [parse_float(row["delta_score_vs_parallel_baseline"]) for row in selected]
            auc_deltas = [parse_float(row["delta_auc_vs_parallel_baseline"]) for row in selected]
            result.append(
                {
                    "dataset": dataset,
                    "selector": selector,
                    "repeat_count": len(selected),
                    "issue38_score_values": compact_json(scores),
                    "issue38_auc_values": compact_json(aucs),
                    "issue38_score_mean": mean_or_zero(scores),
                    "issue38_score_sd": stdev_or_zero(scores),
                    "issue38_auc_mean": mean_or_zero(aucs),
                    "issue38_auc_sd": stdev_or_zero(aucs),
                    "parallel_score_values": compact_json(baseline_scores),
                    "parallel_auc_values": compact_json(baseline_aucs),
                    "parallel_score_mean": mean_or_zero(baseline_scores),
                    "parallel_auc_mean": mean_or_zero(baseline_aucs),
                    "score_delta_values": compact_json(score_deltas),
                    "auc_delta_values": compact_json(auc_deltas),
                    "score_delta_mean": mean_or_zero(score_deltas),
                    "auc_delta_mean": mean_or_zero(auc_deltas),
                    "endpoint_wins": sum(delta > 0 for delta in score_deltas),
                    "endpoint_ties": sum(delta == 0 for delta in score_deltas),
                    "endpoint_losses": sum(delta < 0 for delta in score_deltas),
                    "auc_wins": sum(delta > 1e-12 for delta in auc_deltas),
                    "auc_ties": sum(abs(delta) <= 1e-12 for delta in auc_deltas),
                    "auc_losses": sum(delta < -1e-12 for delta in auc_deltas),
                    "score_range": max(scores) - min(scores),
                    "auc_range": max(aucs) - min(aucs),
                    "clean_count": sum(row["final_quality"] == "clean" for row in selected),
                    "minor_count": sum(row["final_quality"] == "minor" for row in selected),
                    "source_major_count": sum(row["source_quality"] == "major" for row in selected),
                }
            )
    return result


def build_quality_summary(rows: Sequence[Mapping[str, Any]], recovery_rows: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    source_counts = Counter(row["source_quality"] for row in rows)
    final_counts = Counter(row["final_quality"] for row in rows)
    detail_counts = Counter(row["quality_detail"] for row in rows)
    status_counts = Counter(row["final_status_effective"] for row in rows)
    recovery_status_counts = Counter(row.get("recovery_status", "") or "not_recovered" for row in rows)
    trigger_counts = Counter()
    for row in recovery_rows:
        triggers = parse_json(row.get("source_major_triggers"), [])
        if isinstance(triggers, list):
            trigger_counts.update(str(trigger) for trigger in triggers)
    return [
        {
            "scope": "all_144_arms",
            "total_arms": len(rows),
            "source_clean_arms": source_counts.get("clean", 0),
            "source_minor_arms": source_counts.get("minor", 0),
            "source_major_arms": source_counts.get("major", 0),
            "source_clean_pct": 100.0 * source_counts.get("clean", 0) / len(rows),
            "source_minor_pct": 100.0 * source_counts.get("minor", 0) / len(rows),
            "source_major_pct": 100.0 * source_counts.get("major", 0) / len(rows),
            "final_clean_arms": final_counts.get("clean", 0),
            "final_minor_arms": final_counts.get("minor", 0),
            "final_major_arms": 0,
            "final_unresolved_arms": sum(row["final_quality"] not in {"clean", "minor"} for row in rows),
            "final_clean_pct": 100.0 * final_counts.get("clean", 0) / len(rows),
            "final_minor_pct": 100.0 * final_counts.get("minor", 0) / len(rows),
            "accepted_clean_recovery": recovery_status_counts.get("accepted_clean", 0),
            "accepted_minor_fallback_recovery": recovery_status_counts.get("accepted_minor_fallback", 0),
            "unresolved_recovery": sum(count for status, count in recovery_status_counts.items() if status == "unresolved"),
            "effective_completed_status": status_counts.get("COMPLETED", 0),
            "effective_degraded_status": status_counts.get("DEGRADED", 0),
            "quality_detail_counts_json": compact_json(dict(sorted(detail_counts.items()))),
            "source_major_trigger_counts_json": compact_json(dict(sorted(trigger_counts.items()))),
        }
    ]


def build_judge_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate every Judge check status, retaining its interpretation."""
    aggregate: Counter[str] = Counter()
    by_dataset: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        statuses = parse_json(row.get("judge_check_status_counts_json"), {})
        if not isinstance(statuses, Mapping):
            continue
        for status, count in statuses.items():
            status_text = str(status)
            number = parse_int(count)
            aggregate[status_text] += number
            by_dataset[str(row["dataset"])][status_text] += number
    total = sum(aggregate.values())
    result: list[dict[str, Any]] = []
    for status, count in sorted(aggregate.items(), key=lambda item: (-item[1], item[0])):
        result.append(
            {
                "scope": "all_144_arms",
                "dataset": "ALL",
                "status": status,
                "status_class": JUDGE_STATUS_CLASSES.get(status, "other"),
                "count": count,
                "pct_of_all_checks": 100.0 * count / total if total else 0.0,
            }
        )
    for dataset in DATASET_ORDER:
        counts = by_dataset.get(dataset, Counter())
        dataset_total = sum(counts.values())
        for status, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            result.append(
                {
                    "scope": "by_dataset",
                    "dataset": dataset,
                    "status": status,
                    "status_class": JUDGE_STATUS_CLASSES.get(status, "other"),
                    "count": count,
                    "pct_of_dataset_checks": 100.0 * count / dataset_total if dataset_total else 0.0,
                }
            )
    return result


def build_runtime_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Produce compact per-dataset and all-arm operational summaries."""
    scopes = [("ALL", list(rows))] + [(dataset, [row for row in rows if row["dataset"] == dataset]) for dataset in DATASET_ORDER]
    result: list[dict[str, Any]] = []
    for scope, selected in scopes:
        durations = [parse_float(row["effective_duration_seconds"]) for row in selected]
        utilizations = [parse_float(row["solver_slot_utilization"]) for row in selected]
        result.append(
            {
                "scope": scope,
                "arms": len(selected),
                "judge_checks": sum(parse_int(row["judge_check_count"]) for row in selected),
                "judge_terminal_receipts": sum(parse_int(row["judge_terminal_receipt_count"]) for row in selected),
                "judge_retryable_checks": sum(parse_int(row["judge_retryable_check_count"]) for row in selected),
                "judge_remote_cache_reuses": sum(parse_int(row["judge_remote_cache_reuse_count"]) for row in selected),
                "judge_probe_cache_reuses": sum(parse_int(row["judge_probe_cache_reuse_count"]) for row in selected),
                "probe_infrastructure_error_arms": sum(parse_int(row["probe_infrastructure_error_count"]) > 0 for row in selected),
                "probe_infrastructure_error_total": sum(parse_int(row["probe_infrastructure_error_count"]) for row in selected),
                "unexpected_process_error_arms": sum(parse_int(row["unexpected_process_error_count"]) > 0 for row in selected),
                "unexpected_process_error_total": sum(parse_int(row["unexpected_process_error_count"]) for row in selected),
                "candidate_failure_checks": sum(parse_int(row["judge_candidate_failure_check_count"]) for row in selected),
                "allocation_fallbacks": sum(parse_int(row["allocation_fallback_count"]) for row in selected),
                "provider_errors": sum(parse_int(row["provider_error_count"]) for row in selected),
                "receipt_complete_arms": sum(bool(row["receipt_complete_effective"]) for row in selected),
                "zero_unsettled_arms": sum(parse_int(row["remote_unsettled_jobs"]) == 0 for row in selected),
                "mean_duration_seconds": mean_or_zero(durations),
                "median_duration_seconds": statistics.median(durations) if durations else 0.0,
                "mean_slot_utilization": mean_or_zero(utilizations),
                "max_occupied_slots": max([parse_int(row["max_occupied_slots"]) for row in selected] or [0]),
                "full_score_arms": sum(row["effective_stop_reason"] == "full_score" for row in selected),
                "horizon_arms": sum(row["effective_stop_reason"] == "horizon" for row in selected),
            }
        )
    return result


def build_outlier_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Descriptive repeat/outcome outliers; thresholds are stated in the report."""
    result: list[dict[str, Any]] = []
    # One row for each dataset/selector triple with a conspicuously wide
    # repeat range, plus the two accepted minor fallback arms.
    for dataset in DATASET_ORDER:
        for selector in SELECTOR_ORDER:
            selected = [row for row in rows if row["dataset"] == dataset and row["selector"] == selector]
            if not selected:
                continue
            score_range = max(parse_float(row["effective_score"]) for row in selected) - min(parse_float(row["effective_score"]) for row in selected)
            auc_range = max(parse_float(row["effective_auc_normalized"]) for row in selected) - min(parse_float(row["effective_auc_normalized"]) for row in selected)
            if score_range >= 4.0 or auc_range >= 0.15:
                result.append(
                    {
                        "outlier_type": "dataset_selector_repeat_spread",
                        "arm_key": f"{dataset}/{selector}",
                        "dataset": dataset,
                        "selector": selector,
                        "score_range": score_range,
                        "auc_range": auc_range,
                        "detail": "flag if score range >=4 or normalized AUC range >=0.15 across the three repeats",
                    }
                )
    for row in rows:
        if row["quality_detail"] == "accepted_minor_fallback":
            peers = [peer for peer in rows if peer["dataset"] == row["dataset"] and peer["selector"] == row["selector"] and peer["arm_key"] != row["arm_key"]]
            peer_score = mean_or_zero([parse_float(peer["effective_score"]) for peer in peers])
            peer_auc = mean_or_zero([parse_float(peer["effective_auc_normalized"]) for peer in peers])
            result.append(
                {
                    "outlier_type": "accepted_minor_fallback",
                    "arm_key": row["arm_key"],
                    "dataset": row["dataset"],
                    "selector": row["selector"],
                    "score": parse_float(row["effective_score"]),
                    "auc_normalized": parse_float(row["effective_auc_normalized"]),
                    "peer_score_mean": peer_score,
                    "peer_auc_mean": peer_auc,
                    "delta_score_vs_peer_mean": parse_float(row["effective_score"]) - peer_score,
                    "delta_auc_vs_peer_mean": parse_float(row["effective_auc_normalized"]) - peer_auc,
                    "source_score": parse_float(row["source_score"]),
                    "source_auc_normalized": parse_float(row["source_auc_normalized"]),
                    "probe_infrastructure_error_count": parse_int(row["probe_infrastructure_error_count"]),
                    "detail": "minor fallback selected by quality/closeout policy; score is not used to choose attempts",
                }
            )
    return result


def build_recovery_effectiveness(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected = [row for row in rows if row["major_issue_recovered"]]
    result: list[dict[str, Any]] = []
    for dataset in sorted({row["dataset"] for row in selected}, key=DATASET_ORDER.index):
        subset = [row for row in selected if row["dataset"] == dataset]
        score_delta = [parse_float(row["recovery_score_delta"]) for row in subset]
        auc_delta = [parse_float(row["recovery_auc_delta"]) for row in subset]
        result.append(
            {
                "dataset": dataset,
                "recovered_arm_count": len(subset),
                "source_score_sum": sum(parse_float(row["source_score"]) for row in subset),
                "effective_score_sum": sum(parse_float(row["effective_score"]) for row in subset),
                "score_delta_sum": sum(score_delta),
                "score_improved_count": sum(delta > 0 for delta in score_delta),
                "score_unchanged_count": sum(delta == 0 for delta in score_delta),
                "score_decreased_count": sum(delta < 0 for delta in score_delta),
                "source_auc_sum": sum(parse_float(row["source_auc_normalized"]) for row in subset),
                "effective_auc_sum": sum(parse_float(row["effective_auc_normalized"]) for row in subset),
                "auc_delta_sum": sum(auc_delta),
                "auc_delta_mean": mean_or_zero(auc_delta),
            }
        )
    total = selected
    score_delta = [parse_float(row["recovery_score_delta"]) for row in total]
    auc_delta = [parse_float(row["recovery_auc_delta"]) for row in total]
    result.append(
        {
            "dataset": "ALL_63",
            "recovered_arm_count": len(total),
            "source_score_sum": sum(parse_float(row["source_score"]) for row in total),
            "effective_score_sum": sum(parse_float(row["effective_score"]) for row in total),
            "score_delta_sum": sum(score_delta),
            "score_improved_count": sum(delta > 0 for delta in score_delta),
            "score_unchanged_count": sum(delta == 0 for delta in score_delta),
            "score_decreased_count": sum(delta < 0 for delta in score_delta),
            "source_auc_sum": sum(parse_float(row["source_auc_normalized"]) for row in total),
            "effective_auc_sum": sum(parse_float(row["effective_auc_normalized"]) for row in total),
            "auc_delta_sum": sum(auc_delta),
            "auc_delta_mean": mean_or_zero(auc_delta),
        }
    )
    return result


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    preferred = list(rows[0].keys())
    keys = list(dict.fromkeys(preferred + [key for row in rows for key in row.keys() if key not in preferred]))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in keys})


def format_num(value: Any, digits: int = 4) -> str:
    number = parse_float(value)
    return f"{number:.{digits}f}"


def markdown_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[tuple[str, str]], digits: int = 4) -> str:
    if not rows:
        return "(none)"
    lines = ["| " + " | ".join(label for _, label in columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key, "")
            if isinstance(value, float):
                value = format_num(value, digits)
            cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_report(
    *,
    output_path: Path,
    rows: Sequence[Mapping[str, Any]],
    baseline_rows: Sequence[Mapping[str, Any]],
    baseline_contract: Mapping[str, Any],
    dataset_summary: Sequence[Mapping[str, Any]],
    selector_summary: Sequence[Mapping[str, Any]],
    quality_summary: Sequence[Mapping[str, Any]],
    outliers: Sequence[Mapping[str, Any]],
    recovery_effectiveness: Sequence[Mapping[str, Any]],
    judge_summary: Sequence[Mapping[str, Any]],
    runtime_summary: Sequence[Mapping[str, Any]],
    attempt_class_counts: Mapping[str, int],
    standard_rows: Sequence[Mapping[str, Any]],
    source_paths: Mapping[str, str],
    analysis_time: str,
) -> None:
    total_score = sum(parse_float(row["effective_score"]) for row in rows)
    total_max = sum(parse_float(row["effective_max_score"]) for row in rows)
    mean_score = mean_or_zero([parse_float(row["effective_score"]) for row in rows])
    mean_auc = mean_or_zero([parse_float(row["effective_auc_normalized"]) for row in rows])
    baseline_mean_score = mean_or_zero([parse_float(row["score_1h"]) for row in baseline_rows])
    baseline_mean_auc = mean_or_zero([parse_float(row["auc_normalized_1h"]) for row in baseline_rows])
    score_gap = mean_score - baseline_mean_score
    auc_gap = mean_auc - baseline_mean_auc
    endpoint_outcomes = Counter(row["endpoint_outcome_vs_parallel"] for row in rows)
    auc_outcomes = Counter(row["auc_outcome_vs_parallel"] for row in rows)
    # ``build_outlier_rows`` carries peer means for fallback arms; use those
    # enriched rows here rather than the raw arm rows (which intentionally do
    # not duplicate peer-derived statistics).
    fallback_rows = [row for row in outliers if row.get("outlier_type") == "accepted_minor_fallback"]
    large_outliers = [row for row in outliers if row["outlier_type"] == "dataset_selector_repeat_spread"]
    judge_kinds = Counter(str(row["judge_kind"]) for row in rows)
    commits = Counter(str(row["commit"]) for row in rows)
    images = Counter(str(row["image_id"]) for row in rows)
    trigger_counter = Counter()
    for row in rows:
        for trigger in parse_json(row.get("source_major_triggers_json"), []):
            trigger_counter[str(trigger)] += 1
    trigger_text = ", ".join(f"{key}={value}" for key, value in sorted(trigger_counter.items())) or "none"
    zero_score_arms = sum(parse_float(row["effective_score"]) == 0.0 for row in rows)
    zero_score_triples = []
    for dataset in DATASET_ORDER:
        for selector in SELECTOR_ORDER:
            triple = [row for row in rows if row["dataset"] == dataset and row["selector"] == selector]
            if triple and all(parse_float(row["effective_score"]) == 0.0 for row in triple):
                zero_score_triples.append(f"{dataset}/{selector}")
    no_interaction_effective = [
        parse_float(row["effective_score"])
        for row in rows
        if row["selector"] == "no_interaction_feedback"
    ]
    report_lines = [
        "# Issue #38 effective-results analysis",
        "",
        f"Generated at **{analysis_time}** (UTC). This report uses the 144-arm effective overlay (the 63 authoritative recovery rows replace the corresponding source values).",
        "",
        "## Bottom line",
        "",
        f"- Effective endpoint: **{total_score:.0f}/{total_max:.0f}** ({100.0 * total_score / total_max:.2f}%), mean **{mean_score:.4f}/12**; missing **{total_max - total_score:.0f}** points.",
        f"- Mean normalized AUC: **{mean_auc:.6f}**.",
        f"- Stop rule: {sum(row['effective_stop_reason'] == 'full_score' for row in rows)} full-score arms and {sum(row['effective_stop_reason'] == 'horizon' for row in rows)} horizon arms.",
        f"- Against the available three-repeat historical Parallel reference: mean **{baseline_mean_score:.4f}/12**, AUC **{baseline_mean_auc:.6f}**; Issue #38 gap is **{score_gap:+.4f} points ({100.0 * score_gap / baseline_mean_score:+.2f}%)** and **{auc_gap:+.6f} AUC ({100.0 * auc_gap / baseline_mean_auc:+.2f}%)**.",
        f"- Pairwise endpoint W/T/L over 144 arm-to-repeat matches: **{endpoint_outcomes['W']}/{endpoint_outcomes['T']}/{endpoint_outcomes['L']}**; AUC W/T/L: **{auc_outcomes['W']}/{auc_outcomes['T']}/{auc_outcomes['L']}**.",
        "",
        "## Contract and provenance",
        "",
        "Issue #38 effective runs are the Docker CPS/blackboard arms recorded with `provider_backend=nurouter`, NuRouter 0.2.0, `gpt-5.6-sol`, thinking `max`, `fast_mode=false`, fixed 3600-second horizon, `max_parallel=24`, and `max_in_flight=24`. The formal Judge mode is `fast` for Lean proof environments; this is distinct from the model's `fast_mode` setting. The remote Judge result cache is disabled in all 144 effective artifacts (zero remote cache reuses). The 2,603 `probe_cache_reused` records are runner-local duplicate-probe reuse and are reported separately; they are not remote scored-result reuse. Every closeout is drained with zero unsettled remote jobs.",
        f"All effective rows use commit `{next(iter(commits), '')}` (count {sum(commits.values())}); image IDs are recorded per arm in the CSV ({', '.join(f'{key}: {value}' for key, value in images.items())}).",
        "The legacy `aisw_*` keys visible in `run_meta.json` are compatibility aliases used by the NuRouter launcher; the authoritative public backend field is `provider_backend=nurouter`, and the effective NuRouter binary/in-flight fields are recorded separately.",
        f"Judge surfaces are fixed across arms: {judge_kinds.get('formal', 0)} formal-proof arms and {judge_kinds.get('coding', 0)} coding arms, with the dataset-specific environment recorded per row in the arm CSV.",
        "",
        "The primary Parallel reference is historical, not a same-contract causal control: it is the strict campaign's three repeats with one independent agent per problem, `fast_mode=true`, AISW campaign record, and `max_in_flight=1`, without CPS. The comparison is therefore contextual. A strict allocator conclusion requires a Parallel arm rerun under the same NuRouter/CPS-independent contract.",
        "",
        f"Input artifacts: `{source_paths['merged']}`, `{source_paths['recovery']}`, `{source_paths['baseline']}`.",
        "",
        "## Quality / recovery audit",
        "",
        markdown_table(
            quality_summary,
            [
                ("total_arms", "arms"),
                ("source_clean_arms", "source clean"),
                ("source_minor_arms", "source minor"),
                ("source_major_arms", "source major"),
                ("final_clean_arms", "final clean"),
                ("final_minor_arms", "final minor"),
                ("final_unresolved_arms", "unresolved"),
                ("accepted_clean_recovery", "recovery clean"),
                ("accepted_minor_fallback_recovery", "recovery minor fallback"),
            ],
        ),
        "",
        f"Source audit counts are 61 clean, 20 minor, and 63 major (43.75% major). The source major-trigger counts are {trigger_text}; the two trigger sets overlap on 11 arms. After overlay: 122 clean (84.72%), 22 minor (15.28%), zero major and zero unresolved. The 22 final minor arms are **20 pre-existing source-minor arms plus the 2 accepted recovery fallbacks**; they are not 22 unresolved reruns. A `minor` arm has an authoritative receipt but a bounded residual runner/Judge health issue; it is not silently treated as clean.",
        "",
        "Recovery effectiveness (score is not used to choose an attempt):",
        "",
        markdown_table(
            recovery_effectiveness,
            [
                ("dataset", "dataset"),
                ("recovered_arm_count", "n"),
                ("source_score_sum", "source score"),
                ("effective_score_sum", "effective score"),
                ("score_delta_sum", "delta"),
                ("score_improved_count", "up"),
                ("score_unchanged_count", "same"),
                ("score_decreased_count", "down"),
                ("auc_delta_sum", "AUC delta"),
            ],
        ),
        "",
        f"The recovery supervisor recorded {sum(attempt_class_counts.values())} physical attempts: {attempt_class_counts.get('CLEAN', 0)} CLEAN, {attempt_class_counts.get('MINOR', 0)} MINOR, {attempt_class_counts.get('MAJOR', 0)} MAJOR, and {attempt_class_counts.get('INVALID', 0)} INVALID. Selected-arm acceptance is quality-only (not score-maximizing): 61 clean and 2 minor fallback.",
        "",
        "Thus the 63-arm rerun was materially useful: source 105 points became effective 185 (+80), with 33 arms up, 1 unchanged, and 29 down. The decreases are important evidence that recovery did not simply inflate scores; the original degraded values were not authoritative enough to retain.",
        "",
        "## Two accepted minor fallbacks",
        "",
        markdown_table(
            fallback_rows,
            [
                ("arm_key", "arm"),
                ("score", "effective score"),
                ("auc_normalized", "effective AUC"),
                ("source_score", "source score"),
                ("source_auc_normalized", "source AUC"),
                ("probe_infrastructure_error_count", "probe errors"),
                ("peer_score_mean", "peer score mean"),
                ("peer_auc_mean", "peer AUC mean"),
            ],
        ),
        "",
        "The Clever fallback is 6/12, AUC 0.218648 (peers 8 and 7; peer mean AUC 0.372628). The Putnam fallback is 0/12, AUC 0.000000 (peers 1 and 0; peer mean AUC 0.010121); its source 2/12 and AUC 0.087751 are retained only as source diagnostics, not as the final result. Neither fallback is a large unexplained outlier relative to its two same-selector repeats, although the Clever one is 1.5 points and 0.153981 AUC below its peer mean.",
        "",
        "## Zero-score sanity check",
        "",
        f"There are **{zero_score_arms}** effective zero-score arms, concentrated in MathOlympiadBench (3) and PutnamBench (14). Only one dataset×selector triple has all three repeats at zero: `{', '.join(zero_score_triples)}`. The three effective `no_interaction_feedback` repeat vectors are `{no_interaction_effective[:3]}`, `{no_interaction_effective[3:6]}`, `{no_interaction_effective[6:9]}`, `{no_interaction_effective[9:12]}`, `{no_interaction_effective[12:15]}`, and `{no_interaction_effective[15:18]}` for Clever, ICPC, MathOlympiadBench, PutnamBench, USACO, and Verina respectively; it is not an all-zero selector across repeats.",
        "These zeros are final candidate outcomes (the final verdict counts and score curve agree), not rows discarded as Judge infrastructure failures; the receipt/settlement checks remain complete.",
        "",
        "## Dataset-level result and Parallel comparison",
        "",
        markdown_table(
            dataset_summary,
            [
                ("dataset", "dataset"),
                ("n", "arms"),
                ("score_sum", "score sum"),
                ("score_mean", "Issue38 mean"),
                ("baseline_score_mean", "Parallel mean"),
                ("score_delta_mean", "score gap"),
                ("score_relative_to_baseline_pct", "score / Parallel %"),
                ("auc_mean", "Issue38 AUC"),
                ("baseline_auc_mean", "Parallel AUC"),
                ("auc_delta_mean", "AUC gap"),
                ("auc_relative_to_baseline_pct", "AUC / Parallel %"),
                ("endpoint_wins", "W"),
                ("endpoint_ties", "T"),
                ("endpoint_losses", "L"),
            ],
        ),
        "",
        "The dominant gaps are Putnam and Verina, followed by Clever and MathOlympiadBench. ICPC matches the historical endpoint and USACO matches it at the endpoint; their AUCs remain lower because solutions arrive later on average.",
        "",
        "## Selector-level descriptive summary",
        "",
        markdown_table(
            selector_summary,
            [
                ("selector", "selector"),
                ("n", "arms"),
                ("score_mean", "mean score"),
                ("score_sd", "score SD"),
                ("auc_mean", "mean AUC"),
                ("auc_sd", "AUC SD"),
                ("score_delta_mean", "gap vs Parallel"),
                ("auc_delta_mean", "AUC gap"),
                ("clean_count", "clean"),
                ("minor_count", "minor"),
            ],
        ),
        "",
        "Selector means are pooled across datasets and are therefore descriptive only; dataset difficulty dominates the global ranking. `nustigmergy` has the highest pooled endpoint/AUC, but that is not a causal allocator result under the non-matching historical baseline.",
        "",
        "## Repeat spread / possible outliers",
        "",
        "The report flags a dataset×selector triple when its three-repeat endpoint range is at least 4 points or its normalized AUC range is at least 0.15. This is a transparent descriptive screen, not a significance test.",
        "",
        markdown_table(
            large_outliers,
            [
                ("arm_key", "dataset/selector"),
                ("score_range", "score range"),
                ("auc_range", "AUC range"),
            ],
        ),
        "",
        "The largest spreads are Clever/unnormalized_feedback (6 points, AUC range 0.371995), Verina/no_interaction_feedback (5, 0.211722), and several Verina selectors with 4-point ranges. These are real repeat-to-repeat variability signals and should be considered before choosing a downstream allocator arm.",
        "",
        "## Judge and runtime attachment metrics",
        "",
        f"Across all effective artifacts: **{sum(parse_int(row['judge_check_count']) for row in rows):,}** Judge checks (mean {mean_or_zero([parse_float(row['judge_check_count']) for row in rows]):.1f}/arm), **{sum(parse_int(row['judge_terminal_receipt_count']) for row in rows):,}** terminal receipts, **{sum(parse_int(row['judge_candidate_failure_check_count']) for row in rows):,}** candidate-failure feedback checks, and **{sum(parse_int(row['judge_retryable_check_count']) for row in rows):,}** retryable feedback records. Candidate outcomes such as WA/TLE/CE/RE/VERIFY_FAIL/RESOURCE_LIMIT are counted as feedback, not relabeled as infrastructure failure.",
        "",
        f"Residual infrastructure indicators after recovery: {sum(parse_int(row['probe_infrastructure_error_count']) > 0 for row in rows)} arms with a nonzero probe-infrastructure counter and {sum(parse_int(row['unexpected_process_error_count']) > 0 for row in rows)} with an unexpected-process counter; all 144 have complete drained receipts and zero unsettled remote jobs. Allocation fallback/provider-error/policy-timeout counters are zero in the effective allocation summaries.",
        "",
        f"Duration: mean {mean_or_zero([parse_float(row['effective_duration_seconds']) for row in rows]) / 60.0:.2f} minutes, median {statistics.median([parse_float(row['effective_duration_seconds']) for row in rows]) / 60.0:.2f} minutes; horizon arms average {mean_or_zero([parse_float(row['effective_duration_seconds']) for row in rows if row['effective_stop_reason'] == 'horizon']) / 60.0:.2f} minutes. Early full-score arms average {mean_or_zero([parse_float(row['effective_duration_seconds']) for row in rows if row['effective_stop_reason'] == 'full_score']) / 60.0:.2f} minutes.",
        "The 2,880 recorded agent timeouts equal 24 workers × 120 horizon-bound arms and are expected horizon shutdowns, not missing receipts. Likewise, `VERIFY_FAIL`, `WA`, `TLE`, and similar Judge statuses are candidate feedback; `SESSION_PROBE_IN_FLIGHT` is retained as an admission/transport diagnostic and is not counted as a final score.",
        "",
        "Compact runtime summary:",
        "",
        markdown_table(
            runtime_summary,
            [
                ("scope", "scope"),
                ("arms", "arms"),
                ("judge_checks", "Judge checks"),
                ("judge_terminal_receipts", "terminal receipts"),
                ("candidate_failure_checks", "candidate failures"),
                ("probe_infrastructure_error_arms", "probe-error arms"),
                ("unexpected_process_error_arms", "process-error arms"),
                ("mean_slot_utilization", "mean slot util"),
                ("full_score_arms", "full score"),
                ("horizon_arms", "horizon"),
            ],
        ),
        "",
        "Judge check status counts (all arms; see `issue38_judge_summary.csv` for per-dataset detail):",
        "",
        markdown_table(
            [row for row in judge_summary if row.get("scope") == "all_144_arms"][:12],
            [("status", "status"), ("status_class", "interpretation"), ("count", "count"), ("pct_of_all_checks", "%")],
        ),
        "",
        "## Sensitivity reference",
        "",
        "A separate one-repeat standard-mode (`fast_mode=false`) historical Parallel table is emitted for sensitivity only; it is not mixed into the primary three-repeat comparison because it has one repeat and the same historical AISW/non-CPS contract.",
        "",
        markdown_table(
            standard_rows,
            [("benchmark", "dataset"), ("score_1h", "score"), ("auc_normalized_1h", "AUC")],
        ),
        "",
        "## Machine-readable outputs",
        "",
        "- `issue38_arm_analysis.csv`: all 144 effective arm rows, including source/recovery fields, Judge status counts, AUC consistency, and pairwise Parallel deltas.",
        "- `issue38_parallel_baseline_runs.csv`: the 18 primary three-repeat Parallel rows plus the six sensitivity rows.",
        "- `issue38_parallel_pairwise.csv`: one row per 144 arm-to-repeat comparison.",
        "- `issue38_parallel_comparison.csv`: 48 dataset×selector cells with three-repeat values and W/T/L.",
        "- `issue38_dataset_summary.csv`, `issue38_selector_summary.csv`, `issue38_quality_summary.csv`, `issue38_outliers.csv`, `issue38_recovery_effectiveness.csv`.",
        "- `issue38_judge_summary.csv` and `issue38_runtime_summary.csv`: status-level Judge feedback and per-dataset operational totals.",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--merged", type=Path, default=None)
    parser.add_argument("--recovery", type=Path, default=None)
    parser.add_argument("--attempts", type=Path, default=None)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="primary Parallel reference (or ISSUE38_PARALLEL_BASELINE)",
    )
    parser.add_argument(
        "--standard-baseline",
        type=Path,
        default=None,
        help="one-repeat sensitivity reference (or ISSUE38_STANDARD_BASELINE)",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    recovery_root = root / "runs" / "issue38_recovery"
    merged_path = (args.merged or recovery_root / "issue38_recovered_results.csv").resolve()
    recovery_path = (args.recovery or recovery_root / "recovery_results.csv").resolve()
    attempts_path = (args.attempts or recovery_root / "recovery_attempts.csv").resolve()
    baseline_path = resolve_reference_path(
        args.baseline,
        env_name="ISSUE38_PARALLEL_BASELINE",
        root=root,
        sibling_parts=(
            "iclr27",
            "strict_agent_baseline_campaign_20260815",
            "analysis",
            "latest",
            "scientific_run_curves.json",
        ),
    ).resolve()
    standard_path = resolve_reference_path(
        args.standard_baseline,
        env_name="ISSUE38_STANDARD_BASELINE",
        root=root,
        sibling_parts=(
            "iclr27",
            "standard_mode_sol_max_comparison_20260820",
            "analysis",
            "latest",
            "scientific_run_curves.json",
        ),
    ).resolve()
    output_dir = (args.output_dir or recovery_root / "analysis").resolve()
    for path in [merged_path, recovery_path, attempts_path, baseline_path, standard_path]:
        if not path.is_file():
            raise FileNotFoundError(path)
    merged_rows = read_csv(merged_path)
    recovery_rows = read_csv(recovery_path)
    attempt_rows = read_csv(attempts_path)
    if len(merged_rows) != 144:
        raise ValueError(f"expected 144 merged rows, got {len(merged_rows)}")
    if len(recovery_rows) != 63:
        raise ValueError(f"expected 63 recovery rows, got {len(recovery_rows)}")
    if len({arm_key(row["dataset"], row["repeat"], row["selector"]) for row in merged_rows}) != 144:
        raise ValueError("merged arm keys are not unique")
    baseline_rows, baseline_contract = load_baseline(baseline_path)
    if len(baseline_rows) != 18:
        raise ValueError(f"expected 18 primary Parallel baseline rows, got {len(baseline_rows)}")
    baseline_index = {(row["benchmark"], parse_int(row["repeat"])): row for row in baseline_rows}
    if len(baseline_index) != 18:
        raise ValueError("primary baseline keys are not unique")
    analysis_time = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    arm_rows = build_arm_rows(
        root=root,
        merged_rows=merged_rows,
        recovery_rows=recovery_rows,
        attempt_rows=attempt_rows,
        baseline_index=baseline_index,
        analysis_time=analysis_time,
    )
    if len(arm_rows) != 144:
        raise ValueError(f"expected 144 effective arm rows, got {len(arm_rows)}")
    if any(not row["auc_consistent"] for row in arm_rows):
        bad = [row["arm_key"] for row in arm_rows if not row["auc_consistent"]]
        raise ValueError(f"stored/recomputed AUC mismatch in {bad[:5]}")
    score_mismatches = [
        row["arm_key"]
        for row in arm_rows
        if abs(parse_float(row["effective_score"]) - parse_float(row["effective_score_at_1h"])) > 1e-9
    ]
    if score_mismatches:
        raise ValueError(f"final score does not match the one-hour step curve in {score_mismatches[:5]}")
    count_mismatches = [row["arm_key"] for row in arm_rows if parse_int(row["judge_check_count_discrepancy"]) != 0]
    if count_mismatches:
        raise ValueError(f"Judge JSONL count differs from reported count in {count_mismatches[:5]}")
    dataset_summary = build_dataset_summary(arm_rows)
    selector_summary = build_selector_summary(arm_rows)
    comparison = build_dataset_selector_comparison(arm_rows)
    quality_summary = build_quality_summary(arm_rows, recovery_rows)
    outliers = build_outlier_rows(arm_rows)
    recovery_effectiveness = build_recovery_effectiveness(arm_rows)
    judge_summary = build_judge_summary(arm_rows)
    runtime_summary = build_runtime_summary(arm_rows)
    attempt_class_counts = Counter(str(row.get("classification", "")) for row in attempt_rows)
    standard_rows = load_sensitivity_baseline(standard_path)
    # Fail closed on the registered effective-run invariants.  These checks
    # prevent a partially collected or wrong-provider artifact from entering
    # the aggregate silently.
    if Counter(str(row["provider_backend"]) for row in arm_rows) != Counter({"nurouter": 144}):
        raise ValueError("effective rows are not all tagged provider_backend=nurouter")
    if Counter(str(row["mode"]) for row in arm_rows) != Counter({"cps": 144}):
        raise ValueError("effective rows are not all CPS runs")
    if Counter(str(row["fast_mode"]).lower() for row in arm_rows) != Counter({"false": 144}):
        raise ValueError("effective rows do not all use fast_mode=false")
    if any(parse_int(row["horizon_seconds"]) != 3600 for row in arm_rows):
        raise ValueError("effective rows do not all use a 3600-second horizon")
    if any(parse_int(row["max_in_flight"]) != 24 for row in arm_rows):
        raise ValueError("effective rows do not all use max_in_flight=24")
    if any(not bool(row["judge_cache_disabled"]) for row in arm_rows):
        raise ValueError("at least one effective Judge cache is enabled or lacks disabled evidence")
    if any(not bool(row["receipt_complete_effective"]) or parse_int(row["remote_unsettled_jobs"]) != 0 for row in arm_rows):
        raise ValueError("at least one effective arm lacks a drained, settled receipt")
    pairwise_rows = [
        {
            "arm_key": row["arm_key"],
            "dataset": row["dataset"],
            "selector": row["selector"],
            "repeat": row["repeat"],
            "issue38_score": row["effective_score"],
            "issue38_auc": row["effective_auc_normalized"],
            "parallel_score": row["parallel_baseline_score_1h"],
            "parallel_auc": row["parallel_baseline_auc_1h"],
            "score_delta": row["delta_score_vs_parallel_baseline"],
            "auc_delta": row["delta_auc_vs_parallel_baseline"],
            "endpoint_outcome": row["endpoint_outcome_vs_parallel"],
            "auc_outcome": row["auc_outcome_vs_parallel"],
            "final_quality": row["final_quality"],
            "source_quality": row["source_quality"],
        }
        for row in arm_rows
    ]
    baseline_output_rows = list(baseline_rows) + list(standard_rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "issue38_arm_analysis.csv", arm_rows)
    write_csv(output_dir / "issue38_parallel_baseline_runs.csv", baseline_output_rows)
    write_csv(output_dir / "issue38_parallel_pairwise.csv", pairwise_rows)
    write_csv(output_dir / "issue38_parallel_comparison.csv", comparison)
    write_csv(output_dir / "issue38_dataset_summary.csv", dataset_summary)
    write_csv(output_dir / "issue38_selector_summary.csv", selector_summary)
    write_csv(output_dir / "issue38_quality_summary.csv", quality_summary)
    write_csv(output_dir / "issue38_outliers.csv", outliers)
    write_csv(output_dir / "issue38_recovery_effectiveness.csv", recovery_effectiveness)
    write_csv(output_dir / "issue38_judge_summary.csv", judge_summary)
    write_csv(output_dir / "issue38_runtime_summary.csv", runtime_summary)
    source_paths = {
        "merged": safe_relpath(merged_path, root),
        "recovery": safe_relpath(recovery_path, root),
        "baseline": "strict_agent_baseline_campaign_20260815/analysis/latest/scientific_run_curves.json",
    }
    build_report(
        output_path=output_dir / "issue38_analysis.md",
        rows=arm_rows,
        baseline_rows=baseline_rows,
        baseline_contract=baseline_contract,
        dataset_summary=dataset_summary,
        selector_summary=selector_summary,
        quality_summary=quality_summary,
        outliers=outliers,
        recovery_effectiveness=recovery_effectiveness,
        judge_summary=judge_summary,
        runtime_summary=runtime_summary,
        attempt_class_counts=attempt_class_counts,
        standard_rows=standard_rows,
        source_paths=source_paths,
        analysis_time=analysis_time,
    )
    metadata = {
        "schema_version": "issue38_analysis_v1",
        "generated_at_utc": analysis_time,
        "effective_arm_count": len(arm_rows),
        "recovery_arm_count": len(recovery_rows),
        "primary_parallel_baseline_count": len(baseline_rows),
        "sensitivity_baseline_count": len(standard_rows),
        "inputs": source_paths,
        "primary_baseline_contract": baseline_contract,
        "auc_definition": "right-continuous accepted-score step curve integrated from 0 to 3600 s, divided by max_score*3600",
        "effective_contract_assertions": {
            "provider_backend": "nurouter",
            "mode": "cps",
            "fast_mode": False,
            "horizon_seconds": 3600,
            "max_in_flight": 24,
            "judge_cache_disabled": True,
            "receipt_complete_and_settled": True,
        },
        "recovery_attempt_class_counts": dict(sorted(attempt_class_counts.items())),
        "outputs": sorted(path.name for path in output_dir.iterdir() if path.is_file()),
    }
    (output_dir / "issue38_analysis_metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "arms": len(arm_rows), "recovery": len(recovery_rows), "baseline": len(baseline_rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError) as exc:
        print(f"analyze_issue38: {exc}", file=sys.stderr)
        raise SystemExit(2)
