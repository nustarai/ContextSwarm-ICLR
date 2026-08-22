"""Fail-closed audit for the four-arm Figure 4 allocation artifact.

This is intentionally independent of the runner and of the legacy allocation
closeout audit.  It validates a completed set of run directories without
opening any private configuration (credentials and endpoints are rejected).
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import math
from pathlib import Path
import re
from typing import Any


POLICIES = ("uniform_refill", "task_state", "trace_state", "llm_scheduler")
SUMMARY_SCHEMA = "contextswarm_figure4_run_summary_v1"
AUDIT_SCHEMA = "contextswarm_allocation_audit_v1"
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|secret|password|credential|authorization|"
    r"coordinator[_-]?url|judge[_-]?url|base[_-]?url|node\.toml|private[_-]?endpoint)",
    re.I,
)
_URL_WITH_AUTH = re.compile(r"https?://[^\s/]+:[^\s/@]+@", re.I)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I)
_HEX64 = re.compile(r"^[0-9a-f]{64}$", re.I)


def _issue(code: str, field: str = "", detail: str = "") -> dict[str, str]:
    item = {"code": code}
    if field:
        item["field"] = field
    if detail:
        item["detail"] = detail
    return item


class _DuplicateKey(ValueError):
    pass


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(
        text,
        object_pairs_hook=_object_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(
            line,
            object_pairs_hook=_object_pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
        if not isinstance(value, dict):
            raise ValueError(f"line {index} is not an object")
        rows.append(value)
    return rows


def _scan_sensitive(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if _SENSITIVE_KEY.search(key_text):
                found.append(child_path)
            found.extend(_scan_sensitive(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_scan_sensitive(child, f"{path}[{index}]"))
    elif isinstance(value, str) and (_URL_WITH_AUTH.search(value) or _BEARER.search(value)):
        found.append(path)
    return found


def _finite_number(value: Any, field: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError(f"{field} must be finite and non-negative")
    return result


def _required(mapping: Mapping[str, Any], key: str, field: str | None = None) -> Any:
    if key not in mapping:
        raise ValueError(f"missing {field or key}")
    return mapping[key]


def _alias(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    raise ValueError(f"missing one of {', '.join(keys)}")


def _task_order(summary: Mapping[str, Any]) -> tuple[str, ...]:
    value = _alias(summary, "task_order", "ordered_task_ids")
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError("task_order must be a non-empty string list")
    if len(set(value)) != len(value):
        raise ValueError("task_order contains duplicates")
    return tuple(value)


def _contract(summary: Mapping[str, Any], meta: Mapping[str, Any]) -> dict[str, Any]:
    explicit = summary.get("comparison_contract")
    if not isinstance(explicit, Mapping) or not explicit:
        raise ValueError("comparison_contract must be an object")
    return dict(explicit)


def _contract_value(contract: Mapping[str, Any], *paths: str) -> Any:
    """Read a required comparison field, allowing the documented aliases."""
    for path in paths:
        current: Any = contract
        for component in path.split("."):
            if not isinstance(current, Mapping) or component not in current:
                break
            current = current[component]
        else:
            return current
    raise ValueError(f"comparison_contract missing one of {', '.join(paths)}")


def _validate_contract(contract: Mapping[str, Any], summary: Mapping[str, Any]) -> None:
    # These are the only fields Figure 4 is allowed to vary.  Keep the
    # selector identity itself in the contract even when #38's runtime bridge
    # is not present yet; a stable placeholder is preferable to an implicit
    # arm-specific default.
    selector = _contract_value(contract, "selector_identity", "selector.identity", "selector_name", "selection.selector_name")
    visibility = _contract_value(contract, "selector_visibility", "trace_visibility", "selector.visibility", "visibility", "selection.visibility")
    if not isinstance(selector, (str, Mapping)) or not selector:
        raise ValueError("selector identity must be non-empty")
    if visibility != "project_shared":
        raise ValueError("selector visibility must be project_shared")
    model = _contract_value(contract, "model")
    task_order = _contract_value(contract, "tasks", "task_order", "ordered_task_ids")
    evaluator = _contract_value(contract, "evaluator", "evaluator_contract", "judge_contract")
    horizon = _contract_value(contract, "horizon_seconds", "horizon")
    capacity = _contract_value(contract, "total_capacity", "capacity", "cps_capacity")
    initial = _contract_value(contract, "initial_allocation", "initial_assignment", "initial_pool")
    transfer = _contract_value(contract, "candidate_transfer", "candidate_solution_transfer")
    stopping = _contract_value(contract, "stopping", "stopping_rule")
    direct = _contract_value(contract, "direct_messages", "direct_messages_enabled", "direct_message", "communication.direct_messages")
    if not isinstance(model, str) or not model or not isinstance(evaluator, (str, Mapping)) or not evaluator:
        raise ValueError("model and evaluator contracts must be non-empty")
    if not isinstance(task_order, (list, tuple)) or list(task_order) != list(_task_order(summary)):
        raise ValueError("comparison contract task order mismatch")
    if horizon != summary["horizon_seconds"] or capacity != summary["total_capacity"] or initial != summary["initial_allocation"]:
        raise ValueError("comparison contract allocation boundary mismatch")
    if not isinstance(transfer, bool):
        raise ValueError("candidate transfer must be boolean")
    if not isinstance(stopping, (str, Mapping)) or not stopping:
        raise ValueError("stopping rule must be non-empty")
    if direct is not False:
        raise ValueError("comparison contract must disable direct messages")
    # Top-level values are duplicated deliberately for paired-repeat analysis;
    # all arms must carry the same values even if a contract implementation
    # stores them under a nested object.


def _crosscheck_selection(selection: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    if selection.get("direct_messages") is not False:
        raise ValueError("direct messages must be disabled")
    if selection.get("visibility", "project_shared") != "project_shared":
        raise ValueError("selection visibility must be project_shared")
    contract_selection = contract.get("selection")
    if isinstance(contract_selection, Mapping):
        if dict(contract_selection) != dict(selection):
            raise ValueError("summary and run_meta selection contracts differ")
        return
    identity = _contract_value(contract, "selector_identity", "selector.identity", "selector_name")
    if isinstance(identity, Mapping):
        for key in ("selector_name", "selector_version", "selection_config_id"):
            if key in identity and selection.get(key) != identity[key]:
                raise ValueError(f"run_meta selector {key} differs from comparison contract")
    elif identity != selection.get("selection_config_id", selection.get("selector_name")):
        raise ValueError("run_meta selector identity differs from comparison contract")


def _audit_arm(policy: str, run_dir: Path) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    meta: Mapping[str, Any] = {}
    summary: Mapping[str, Any] = {}
    decisions: list[dict[str, Any]] = []
    trace_rows: list[dict[str, Any]] = []
    try:
        meta_value = _load_json(run_dir / "run_meta.json")
        summary_value = _load_json(run_dir / "figure4_run_summary.json")
        decisions = _load_jsonl(run_dir / "allocation_decisions.jsonl")
        if policy == "trace_state":
            trace_rows = _load_jsonl(run_dir / "allocation_audit.jsonl")
        if not isinstance(meta_value, Mapping) or not isinstance(summary_value, Mapping):
            raise ValueError("run_meta and figure4 summary must be objects")
        meta, summary = meta_value, summary_value
        if summary.get("schema_version") != SUMMARY_SCHEMA:
            raise ValueError("unsupported figure4 summary schema")
        if summary.get("policy") != policy:
            raise ValueError("summary policy does not match arm")
        run_id = _required(summary, "run_id")
        contract_id = _alias(summary, "comparison_contract_id", "comparison_contract_sha256")
        if not isinstance(run_id, str) or not run_id or not isinstance(contract_id, str) or not contract_id:
            raise ValueError("run_id and comparison_contract_id must be non-empty strings")
        paired_seed = _finite_number(_alias(summary, "paired_seed", "seed"), "paired_seed")
        repeat = _finite_number(_alias(summary, "repeat", "paired_repeat_id"), "repeat")
        if int(paired_seed) != paired_seed or int(repeat) != repeat:
            raise ValueError("paired_seed and repeat must be integers")
        tasks = _task_order(summary)
        horizon = _finite_number(summary["horizon_seconds"], "horizon_seconds")
        capacity = int(_finite_number(summary["total_capacity"], "total_capacity"))
        if capacity <= 0:
            raise ValueError("total_capacity must be positive")
        initial = summary["initial_allocation"]
        if not isinstance(initial, Mapping) or set(initial) != set(tasks):
            raise ValueError("initial_allocation must cover task_order exactly")
        initial_total = 0
        for task in tasks:
            count = int(_finite_number(initial[task], f"initial_allocation.{task}"))
            if float(count) != float(initial[task]):
                raise ValueError("initial allocation counts must be integers")
            initial_total += count
        if initial_total > capacity:
            raise ValueError("initial allocation exceeds total capacity")
        history = summary["accepted_score_history"]
        if not isinstance(history, list):
            raise ValueError("accepted_score_history must be a list")
        previous_time = -1.0
        for row in history:
            if not isinstance(row, Mapping):
                raise ValueError("accepted_score_history rows must be objects")
            elapsed = _finite_number(_alias(row, "elapsed_seconds", "time_seconds"), "history.elapsed_seconds")
            if elapsed < previous_time or elapsed > horizon:
                raise ValueError("accepted score history times are not monotone/in horizon")
            previous_time = elapsed
            _finite_number(_alias(row, "score", "accepted_score"), "history.score")
        final = _finite_number(summary["final_accepted_score"], "final_accepted_score")
        if history and final != _finite_number(_alias(history[-1], "score", "accepted_score"), "history.score"):
            raise ValueError("final_accepted_score does not match history")
        _finite_number(summary["nauc"], "nauc")
        ttk = _alias(summary, "time_to_k", "time_to_k_seconds")
        if not isinstance(ttk, Mapping):
            raise ValueError("time_to_k must be an object")
        for key, value in ttk.items():
            if value is not None:
                elapsed = _finite_number(value, f"time_to_k.{key}")
                if elapsed > horizon:
                    raise ValueError("time_to_k exceeds horizon")
        for section, required_keys in {
            "solver_usage": ("calls", "input_tokens", "output_tokens"),
            "evaluator_usage": ("calls",),
        }.items():
            obj = summary.get(section)
            if not isinstance(obj, Mapping):
                raise ValueError(f"{section} must be an object")
            for key in required_keys:
                _finite_number(_required(obj, key, f"{section}.{key}"), f"{section}.{key}")
        scheduler = summary.get("scheduler_cost", summary.get("llm_scheduler_cost"))
        if not isinstance(scheduler, Mapping):
            raise ValueError("scheduler_cost must be an object")
        for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "latency_seconds", "reserved_slot_seconds"):
            _finite_number(_required(scheduler, key, f"scheduler_cost.{key}"), f"scheduler_cost.{key}")
        allocation_obj = summary.get("allocation_metrics")
        if allocation_obj is None:
            if "allocation_decisions" not in summary or "fallback_decisions" not in summary:
                raise ValueError("allocation_metrics is missing")
            allocation_obj = {
                "decisions": summary["allocation_decisions"],
                "fallbacks": summary["fallback_decisions"],
            }
        if not isinstance(allocation_obj, Mapping):
            raise ValueError("allocation_metrics must be an object")
        for key in ("decisions", "fallbacks"):
            _finite_number(_required(allocation_obj, key, f"allocation_metrics.{key}"), f"allocation_metrics.{key}")
        if scheduler["total_tokens"] != scheduler["input_tokens"] + scheduler["output_tokens"]:
            raise ValueError("scheduler total_tokens mismatch")
        if policy != "llm_scheduler" and any(float(scheduler[key]) != 0.0 for key in (
            "calls", "input_tokens", "output_tokens", "total_tokens", "latency_seconds", "reserved_slot_seconds"
        )):
            raise ValueError("non-LLM scheduler cost must be zero")
        if policy == "llm_scheduler" and scheduler["calls"] != allocation_obj["decisions"]:
            raise ValueError("LLM scheduler calls must match allocation decisions")
        if allocation_obj["fallbacks"] > allocation_obj["decisions"]:
            raise ValueError("allocation fallbacks exceed decisions")
        if int(allocation_obj["decisions"]) != len(decisions):
            raise ValueError("allocation_metrics.decisions does not match decision artifact")
        decision_ids: set[str] = set()
        for row in decisions:
            if not isinstance(row.get("policy"), str) or row["policy"] != policy:
                raise ValueError("allocation decision policy mismatch")
            decision_id = row.get("decision_id", row.get("state_id"))
            if not isinstance(decision_id, str) or not decision_id:
                raise ValueError("allocation decision missing decision_id")
            if decision_id in decision_ids:
                raise ValueError("duplicate allocation decision_id")
            decision_ids.add(decision_id)
        if policy == "trace_state":
            counts["trace_audit_rows"] = len(trace_rows)
            if len(trace_rows) != len(decisions):
                raise ValueError("every trace-state decision requires an audit row")
            audit_ids: set[str] = set()
            for row in trace_rows:
                _validate_trace_audit(row, capacity, tasks)
                audit_ids.add(str(row["decision_id"]))
            if audit_ids != decision_ids or len(audit_ids) != len(trace_rows):
                raise ValueError("trace audit decision IDs do not join one-to-one")
        contract = _contract(summary, meta)
        _validate_contract(contract, summary)
        selection = meta.get("selection")
        if selection is not None:
            if not isinstance(selection, Mapping):
                raise ValueError("run_meta.selection must be an object")
            _crosscheck_selection(selection, contract)
        allocation_meta = meta.get("allocation")
        if isinstance(allocation_meta, Mapping) and allocation_meta.get("policy") != policy:
            raise ValueError("run_meta allocation policy mismatch")
        counts.update({"decisions": len(decisions), "history_rows": len(history), "trace_audit_rows": len(trace_rows)})
        return {"ok": not errors, "policy": policy, "run_dir": str(run_dir), "counts": counts, "contract": contract, "summary": dict(summary), "errors": errors}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        errors.append(_issue("figure4_artifact_invalid", detail=str(exc)))
    return {"ok": False, "policy": policy, "run_dir": str(run_dir), "counts": counts, "contract": {}, "summary": {}, "errors": errors}


def _validate_trace_audit(row: Mapping[str, Any], capacity: int, tasks: tuple[str, ...]) -> None:
    if row.get("schema_version") != AUDIT_SCHEMA:
        raise ValueError("unsupported allocation audit schema")
    for key in ("state_id", "decision_id", "allocation_config_sha256"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise ValueError(f"trace audit missing {key}")
    if not _HEX64.fullmatch(row["allocation_config_sha256"]):
        raise ValueError("allocation_config_sha256 must be a sha256")
    eligible = row.get("eligible_task_ids")
    if not isinstance(eligible, list) or not eligible or len(set(eligible)) != len(eligible) or not set(eligible) <= set(tasks):
        raise ValueError("eligible_task_ids must be a unique task subset")
    for key in ("task_only_scores", "trace_increments", "trace_total_scores"):
        scores = row.get(key)
        if not isinstance(scores, Mapping) or set(scores) != set(eligible):
            raise ValueError(f"{key} must cover eligible_task_ids")
        for task_id in eligible:
            _finite_number(scores[task_id], f"{key}.{task_id}", nonnegative=False)
    for key in ("trace_state_selected_task_id", "task_state_selected_task_id"):
        if row.get(key) not in eligible:
            raise ValueError(f"{key} must be eligible")
    admitted = row.get("admitted_task_id")
    if admitted is not None and admitted not in eligible:
        raise ValueError("admitted_task_id must be eligible or null")
    if not isinstance(row.get("fallback_reason"), str):
        raise ValueError("fallback_reason must be a string")
    before = row.get("allocation_before")
    trace = row.get("trace_state_allocation_after")
    task = row.get("task_state_allocation_after")
    if not isinstance(trace, Mapping) or not isinstance(task, Mapping) or set(trace) != set(tasks) or set(task) != set(tasks):
        raise ValueError("trace/task allocation vectors must cover task_order")
    if not isinstance(before, Mapping) or set(before) != set(tasks):
        raise ValueError("allocation_before must cover task_order")
    before_total = trace_total = task_total = 0
    for item in tasks:
        b = _finite_number(before[item], f"allocation_before.{item}")
        t = _finite_number(trace[item], f"trace_slots.{item}")
        q = _finite_number(task[item], f"task_slots.{item}")
        if int(b) != b or int(t) != t or int(q) != q:
            raise ValueError("allocation vectors must contain integers")
        before_total += int(b)
        trace_total += int(t)
        task_total += int(q)
    if trace_total > capacity or task_total > capacity or trace_total != task_total:
        raise ValueError("same-state allocation capacity is not conserved")
    if int(_finite_number(row.get("total_capacity"), "total_capacity")) != capacity:
        raise ValueError("trace audit total_capacity mismatch")
    if _finite_number(row.get("capacity_delta_sum"), "capacity_delta_sum", nonnegative=False) != 0:
        raise ValueError("capacity_delta_sum must be zero")
    if row.get("capacity_conserved") is not True:
        raise ValueError("capacity_conserved must be true")
    trace_selected = row["trace_state_selected_task_id"]
    task_selected = row["task_state_selected_task_id"]
    admission_count = 0 if admitted is None else 1
    for task_id in tasks:
        expected_trace = int(before[task_id]) + (
            admission_count if task_id == trace_selected else 0
        )
        expected_task = int(before[task_id]) + (
            admission_count if task_id == task_selected else 0
        )
        if int(trace[task_id]) != expected_trace or int(task[task_id]) != expected_task:
            raise ValueError("same-state after vectors are not one-slot admissions")
    if admitted is not None and admitted != trace_selected:
        raise ValueError("admitted_task_id must match trace-state selection")
    for moment in ("before", "after"):
        active = _finite_number(row.get(f"active_slots_{moment}"), f"active_slots_{moment}")
        free = _finite_number(row.get(f"free_slots_{moment}"), f"free_slots_{moment}")
        reserved = _finite_number(row.get(f"scheduler_reserved_slots_{moment}"), f"scheduler_reserved_slots_{moment}")
        if active + free + reserved != capacity:
            raise ValueError(f"{moment} slot accounting does not equal capacity")
        vector_total = before_total if moment == "before" else trace_total
        if active != vector_total:
            raise ValueError(f"{moment} active slots do not match allocation vector")


def audit_figure4(paths: Mapping[str, str | Path]) -> dict[str, Any]:
    """Audit exactly four policy directories and return a JSON-safe report."""
    report: dict[str, Any] = {"schema_version": "contextswarm_figure4_audit_v1", "ok": True, "arms": {}, "errors": []}
    if set(paths) != set(POLICIES):
        report["ok"] = False
        report["errors"].append(_issue("policy_set_invalid", detail=f"expected {POLICIES}"))
        return report
    resolved = [Path(paths[policy]).resolve() for policy in POLICIES]
    if len(set(resolved)) != len(POLICIES):
        report["ok"] = False
        report["errors"].append(_issue("policy_directories_not_distinct"))
        return report
    contracts: dict[str, dict[str, Any]] = {}
    boundaries: dict[str, tuple[Any, ...]] = {}
    for policy in POLICIES:
        run_dir = Path(paths[policy])
        arm = _audit_arm(policy, run_dir)
        report["arms"][policy] = arm
        if not arm["ok"]:
            report["ok"] = False
        else:
            contracts[policy] = arm["contract"]
            summary = arm["summary"]
            try:
                boundaries[policy] = (
                    summary.get("comparison_contract_id", summary.get("comparison_contract_sha256")),
                    summary.get("paired_seed", summary.get("seed")),
                    summary.get("repeat", summary.get("paired_repeat_id")),
                    tuple(_task_order(summary)),
                    summary.get("horizon_seconds"),
                    summary.get("total_capacity"),
                    summary.get("initial_allocation"),
                )
            except (TypeError, ValueError):
                report["ok"] = False
                report["arms"][policy]["errors"].append(_issue("comparison_boundary_invalid"))
        for filename in ("run_meta.json", "figure4_run_summary.json", "allocation_decisions.jsonl") + (("allocation_audit.jsonl",) if policy == "trace_state" else ()):
            path = run_dir / filename
            if path.exists():
                try:
                    value = _load_json(path) if path.suffix == ".json" else _load_jsonl(path)
                    sensitive = _scan_sensitive(value)
                    if sensitive:
                        report["ok"] = False
                        report["arms"][policy]["ok"] = False
                        report["arms"][policy]["errors"].append(_issue("sensitive_field_present", detail=", ".join(sensitive[:8])))
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
    if len(contracts) == len(POLICIES):
        baseline = contracts[POLICIES[0]]
        for policy in POLICIES[1:]:
            if contracts[policy] != baseline:
                report["ok"] = False
                report["errors"].append(_issue("comparison_contract_mismatch", field=policy))
    if len(boundaries) == len(POLICIES):
        baseline_boundary = boundaries[POLICIES[0]]
        for policy in POLICIES[1:]:
            if boundaries[policy] != baseline_boundary:
                report["ok"] = False
                report["errors"].append(_issue("paired_boundary_mismatch", field=policy))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for policy in POLICIES:
        parser.add_argument(f"--{policy.replace('_', '-')}", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    paths = {policy: getattr(args, policy) for policy in POLICIES}
    report = audit_figure4(paths)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
