#!/usr/bin/env python3
"""Export and compare the machine-readable Figure 3 run artifacts.

The exporter is intentionally run-directory driven.  A comparison contract,
the ordered task list, and the paired seed are read from the run's own
``run_meta.json`` (``figure3`` block), rather than supplied as command-line
options.  This is important: allowing an operator to type a convenient task
order or contract after seeing results would make a paired comparison
non-reproducible.

Examples::

    python3 scripts/export_figure3.py export runs/figure3/.../random/SEED
    python3 scripts/export_figure3.py compare \
        runs/figure3/.../random runs/figure3/.../nustigmergy

``compare`` accepts either one completed run directory per side or a parent
directory containing completed run directories.  It emits left-minus-right
paired differences and deterministic percentile bootstrap intervals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sqlite3
import sys
from typing import Any, Mapping, Sequence

# ``python scripts/export_figure3.py`` puts ``scripts`` (not the repository
# root) on sys.path.  Add the root solely so the local package can be imported
# without requiring an editable install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from contextswarm_mini.selection_artifacts import (  # noqa: E402
    ArtifactValidationError,
    build_figure3_run_summary,
    collect_paired_metrics,
    validate_selection_store_export,
    write_figure3_run_summary,
)
from contextswarm_mini.selection_store import (  # noqa: E402
    EXPORT_SCHEMA_VERSION,
    SelectionStore,
)


FIGURE3_CONTRACT_SCHEMA = "contextswarm_figure3_contract_v1"
SUMMARY_SCHEMA = "contextswarm_selection_artifacts_v1"
_KNOWN_EXPORT_TYPES = {
    "selector_config",
    "search_event",
    "search_candidate",
    "search_ranking",
    "exposure",
    "exposure_item",
    "feedback_event",
    "verifier_evidence",
    "maintenance_event",
    "trace_relation",
}
_TERMINAL_STATUSES = {"COMPLETED", "DEGRADED", "DRY_RUN"}
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class Figure3ExportError(ArtifactValidationError):
    """Raised when a run cannot be audited without guessing an input."""


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Figure3ExportError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, Mapping):
        raise Figure3ExportError(f"{label} must be a JSON object")
    return dict(value)


def _text(value: Any, name: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise Figure3ExportError(f"{name} must be a bounded non-empty string")
    return value.strip()


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Figure3ExportError(f"{name} must be a non-negative integer")
    return value


def _selection_identity(value: Any) -> str:
    """Mirror SelectionStore's public identity normalization."""

    if isinstance(value, str) and _SHA256_RE.fullmatch(value.strip()):
        return value.strip().lower()
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contract_from_meta(root: Path, meta: Mapping[str, Any]) -> dict[str, Any]:
    """Read and cross-check the runner-owned Figure 3 contract.

    ``figure3_contract.json`` is accepted as a forward-compatible spelling
    for runners that choose a separate artifact.  If both forms exist they
    must be byte-equivalent semantically; silently preferring one would hide a
    torn or manually edited run metadata file.
    """

    candidates: list[dict[str, Any]] = []
    embedded = meta.get("figure3")
    if embedded is not None:
        if not isinstance(embedded, Mapping):
            raise Figure3ExportError("run_meta.figure3 must be an object")
        candidates.append(dict(embedded))
    separate = root / "figure3_contract.json"
    if separate.exists():
        candidates.append(_object(separate, "figure3_contract.json"))
    if not candidates:
        raise Figure3ExportError(
            "run has no runner-owned Figure 3 contract; refusing to infer "
            "comparison_contract or task_order"
        )

    # Ignore only the schema marker when comparing the two supported envelope
    # spellings.  All substantive fields must agree exactly.
    normalized = []
    for index, raw in enumerate(candidates):
        schema = raw.get("schema_version", FIGURE3_CONTRACT_SCHEMA)
        if schema != FIGURE3_CONTRACT_SCHEMA:
            raise Figure3ExportError(
                f"Figure 3 contract {index} has unsupported schema {schema!r}"
            )
        contract_id = raw.get("comparison_contract_id")
        # A hash is preferred, but a versioned opaque identity is also valid
        # for historical artifacts.  Never manufacture one here.
        _text(contract_id, "figure3.comparison_contract_id", max_length=256)
        order = raw.get("task_order")
        if not isinstance(order, list) or not order or not all(
            isinstance(task, str) and task.strip() for task in order
        ):
            raise Figure3ExportError(
                "figure3.task_order must be a unique non-empty ordered list"
            )
        if len(order) != len(set(order)):
            raise Figure3ExportError("figure3.task_order must not contain duplicates")
        seed = _nonnegative_int(raw.get("paired_seed"), "figure3.paired_seed")
        selector_name = _text(raw.get("selector_name"), "figure3.selector_name")
        selector_version = _text(
            raw.get("selector_version"), "figure3.selector_version"
        )
        selection_config_id = _text(
            raw.get("selection_config_id"), "figure3.selection_config_id", max_length=256
        )
        normalized.append(
            {
                "schema_version": FIGURE3_CONTRACT_SCHEMA,
                "comparison_contract_id": str(contract_id).strip(),
                "task_order": [str(task).strip() for task in order],
                "paired_seed": seed,
                "selector_name": selector_name,
                "selector_version": selector_version,
                "selection_config_id": selection_config_id,
            }
        )
    if any(item != normalized[0] for item in normalized[1:]):
        raise Figure3ExportError(
            "embedded and separate Figure 3 contracts disagree"
        )

    contract = normalized[0]
    # The contract is an isolation boundary.  If these fields are emitted by
    # the runner, an explicit true value is always a hard failure.
    for source_name, source in (
        ("run_meta.selection", meta.get("selection")),
        ("figure3", candidates[0]),
    ):
        if isinstance(source, Mapping):
            for field in ("direct_messages", "candidate_transfer"):
                if source.get(field) is True:
                    raise Figure3ExportError(
                        f"{source_name}.{field} violates Figure 3 isolation contract"
                    )
    selection = meta.get("selection")
    if isinstance(selection, Mapping):
        for field, expected in (
            ("selector_name", contract["selector_name"]),
            ("selector_version", contract["selector_version"]),
            ("selection_config_id", contract["selection_config_id"]),
        ):
            value = selection.get(field)
            if value is not None and value != expected:
                raise Figure3ExportError(
                    f"run_meta.selection.{field} disagrees with Figure 3 contract"
                )
    return contract


def _check_runtime_evidence(root: Path, contract: Mapping[str, Any], meta: Mapping[str, Any], final: Mapping[str, Any]) -> None:
    """Cross-check optional runner closeout files when present."""

    runtime_path = root / "selection_runtime.json"
    if runtime_path.exists():
        runtime = _object(runtime_path, "selection_runtime.json")
        for field, expected in (
            ("comparison_contract_id", contract["comparison_contract_id"]),
            ("selector_name", contract["selector_name"]),
            ("selector_version", contract["selector_version"]),
            ("selection_config_id", contract["selection_config_id"]),
        ):
            if runtime.get(field) != expected and field in runtime:
                raise Figure3ExportError(
                    f"selection_runtime.json.{field} disagrees with Figure 3 contract"
                )
        trace_search = runtime.get("trace_search")
        if isinstance(trace_search, Mapping) and trace_search.get("status") not in {
            None,
            "available",
        }:
            raise Figure3ExportError("selection runtime trace search is not available")

    summary_path = root / "selection_summary.json"
    if summary_path.exists():
        summary = _object(summary_path, "selection_summary.json")
        for field, expected in (
            ("comparison_contract_id", contract["comparison_contract_id"]),
            ("selector_name", contract["selector_name"]),
            ("selector_version", contract["selector_version"]),
            ("selection_config_id", contract["selection_config_id"]),
        ):
            if summary.get(field) != expected and field in summary:
                raise Figure3ExportError(
                    f"selection_summary.json.{field} disagrees with Figure 3 contract"
                )
        if summary.get("status") not in {None, "closed", "dry_run"}:
            raise Figure3ExportError("selection closeout is not closed")

    final_selection = final.get("selection")
    if isinstance(final_selection, Mapping):
        if final_selection.get("enabled") is False:
            raise Figure3ExportError("final selection evidence says selection is disabled")
        if final_selection.get("status") in {"missing_or_invalid", "broker_not_drained"}:
            raise Figure3ExportError("final selection evidence is incomplete")
        final_id = final_selection.get("comparison_contract_id")
        if final_id is not None and final_id != contract["comparison_contract_id"]:
            raise Figure3ExportError("final.selection comparison contract mismatch")
        for field, expected in (
            ("selector_name", contract["selector_name"]),
            ("selector_version", contract["selector_version"]),
            ("selection_config_id", contract["selection_config_id"]),
        ):
            if field in final_selection and final_selection[field] not in {None, expected}:
                raise Figure3ExportError(f"final.selection.{field} mismatch")


def _accepted_score_history(root: Path, task_order: Sequence[str]) -> list[dict[str, Any]]:
    """Read a bounded, credential-free accepted-score event history."""

    path = root / "scoreboard_history.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    known = set(task_order)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise Figure3ExportError(f"cannot read scoreboard_history.jsonl: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Figure3ExportError(
                f"invalid scoreboard history JSON at line {line_number}"
            ) from exc
        if not isinstance(raw, Mapping):
            raise Figure3ExportError("scoreboard history rows must be objects")
        task = raw.get("task_id")
        if task not in known:
            raise Figure3ExportError("scoreboard history contains an unknown task")
        # Closeout re-evaluation is deliberately excluded from the in-horizon
        # accepted-score objective (runner._score_time_metrics uses the same
        # rule).  Keep this filter here so a replay cannot inflate nAUC.
        if str(raw.get("source") or "") == "closeout":
            continue
        score = raw.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or float(score) < 0.0
            or float(score) > 1.0
        ):
            raise Figure3ExportError("scoreboard history has an invalid score")
        elapsed = raw.get("horizon_elapsed_seconds", raw.get("elapsed_seconds"))
        if elapsed is not None:
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(float(elapsed)):
                raise Figure3ExportError("scoreboard history has invalid elapsed time")
            elapsed = max(0.0, float(elapsed))
        episode = raw.get("episode")
        if episode is not None and (
            isinstance(episode, bool) or not isinstance(episode, int) or episode < 0
        ):
            raise Figure3ExportError("scoreboard history has an invalid episode")
        # Keep only fields needed to replay accepted-score curves.  Responses,
        # errors, statuses, and agent commands are intentionally not copied
        # into a paper-facing export.
        events.append(
            {
                "task_id": str(task),
                "episode": episode,
                "score": float(score),
                "elapsed_seconds": elapsed,
            }
        )
    events.sort(
        key=lambda row: (
            float("inf") if row["elapsed_seconds"] is None else row["elapsed_seconds"],
            row["task_id"],
            row.get("episode") or 0,
        )
    )
    accepted: list[dict[str, Any]] = []
    best: dict[str, float] = {task: 0.0 for task in task_order}
    for event in events:
        task = event["task_id"]
        if float(event["score"]) <= best[task]:
            continue
        best[task] = float(event["score"])
        accepted.append(event)
    return accepted


def _audit_selection_artifact(
    root: Path,
    *,
    required: bool,
    comparison_contract_id: str,
) -> dict[str, Any] | None:
    """Validate the runner export, generating it only for legacy fixtures.

    New runner closeout owns ``selection_events.jsonl`` and binds its digest
    and counts in ``selection_summary.json``.  Prefer that immutable closeout
    evidence.  The SQLite fallback keeps the CLI useful for completed
    development runs produced between store integration and runner wiring;
    it uses the same filename/schema and never creates a competing artifact.
    """

    database = root / "selection.sqlite3"
    destination = root / "selection_events.jsonl"
    closeout_path = root / "selection_summary.json"
    closeout = _object(closeout_path, "selection_summary.json") if closeout_path.exists() else None
    expected_artifact = closeout.get("artifact") if isinstance(closeout, Mapping) else None
    generated: dict[str, Any] | None = None
    if not destination.exists():
        if not database.exists():
            if required:
                raise Figure3ExportError(
                    "selection-enabled run is missing selection_events.jsonl and selection.sqlite3"
                )
            return None
        try:
            generated = SelectionStore(database).export_jsonl(destination)
        except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
            raise Figure3ExportError(f"cannot export selection store: {exc}") from exc
    try:
        raw = destination.read_bytes()
    except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
        raise Figure3ExportError(f"cannot read selection_events.jsonl: {exc}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if isinstance(expected_artifact, Mapping):
        if expected_artifact.get("path") != destination.name:
            raise Figure3ExportError("selection summary points to a different artifact")
        if expected_artifact.get("schema") != EXPORT_SCHEMA_VERSION:
            raise Figure3ExportError("selection summary has an unsupported artifact schema")
        if expected_artifact.get("sha256") != digest:
            raise Figure3ExportError("runner selection artifact digest mismatch")
        expected_counts = expected_artifact.get("record_type_counts")
        expected_record_count = expected_artifact.get("record_count")
    elif generated is not None:
        if generated.get("sha256") != digest:
            raise Figure3ExportError("selection export digest mismatch")
        expected_counts = generated.get("record_type_counts")
        expected_record_count = generated.get("record_count")
    else:
        # A pre-existing export without closeout metadata has no trustworthy
        # binding to the completed store snapshot.
        raise Figure3ExportError(
            "selection_events.jsonl is not bound by selection_summary.json"
        )
    # Validate the typed envelope and reconcile counts without exposing rows.
    seen: dict[str, int] = {}
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise Figure3ExportError("selection export is not UTF-8") from exc
    trace_ids: set[str] = set()
    selected_trace_ids: set[str] = set()
    exposed_trace_ids: set[str] = set()
    feedback_trace_ids: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Figure3ExportError(f"invalid selection export JSON at line {line_number}") from exc
        if not isinstance(envelope, Mapping) or envelope.get("schema") != EXPORT_SCHEMA_VERSION:
            raise Figure3ExportError("selection export has an unsupported envelope")
        record_type = envelope.get("record_type")
        if record_type not in _KNOWN_EXPORT_TYPES or not isinstance(envelope.get("record"), Mapping):
            raise Figure3ExportError("selection export has an invalid record type")
        seen[record_type] = seen.get(record_type, 0) + 1
        record = envelope["record"]
        trace_id = record.get("trace_id")
        if isinstance(trace_id, str) and trace_id:
            trace_ids.add(trace_id)
            if record_type == "exposure_item":
                exposed_trace_ids.add(trace_id)
            elif record_type == "feedback_event":
                feedback_trace_ids.add(trace_id)
        if record_type == "search_ranking" and record.get("selected") is True:
            selected_trace_id = record.get("trace_id")
            if isinstance(selected_trace_id, str) and selected_trace_id:
                selected_trace_ids.add(selected_trace_id)
    if isinstance(expected_counts, Mapping) and {
        str(key): int(value) for key, value in expected_counts.items()
    } != seen:
        # Empty record types are included by the store's count map; normalize
        # them before comparing.
        normalized_seen = {key: seen.get(key, 0) for key in expected_counts}
        if normalized_seen != {
            str(key): int(value) for key, value in expected_counts.items()
        }:
            raise Figure3ExportError("selection export record counts mismatch")
    if (
        isinstance(expected_record_count, bool)
        or not isinstance(expected_record_count, int)
        or expected_record_count != sum(seen.values())
    ):
        raise Figure3ExportError("selection artifact total record count mismatch")
    try:
        validated_summary = validate_selection_store_export(destination)
    except ArtifactValidationError as exc:
        raise Figure3ExportError(f"selection attribution validation failed: {exc}") from exc
    if not isinstance(validated_summary, Mapping):
        raise Figure3ExportError("selection attribution validator returned no summary")
    summary = dict(validated_summary)
    # The closeout summary is an independent snapshot binding.  Reconcile its
    # public counts and identity fields with the portable validator, but use
    # the validator's trace-oriented usage metrics in the report.
    bound_summary = None
    if isinstance(closeout, Mapping) and isinstance(closeout.get("store_summary"), Mapping):
        bound_summary = closeout["store_summary"]
    elif isinstance(generated, Mapping) and isinstance(generated.get("summary"), Mapping):
        bound_summary = generated["summary"]
    if isinstance(bound_summary, Mapping):
        for field in ("comparison_contract_ids", "selector_config_ids"):
            if field in bound_summary and field in summary and bound_summary.get(field) != summary.get(field):
                raise Figure3ExportError(f"selection closeout {field} mismatch")
        if (
            isinstance(bound_summary.get("counts"), Mapping)
            and isinstance(summary.get("counts"), Mapping)
            and bound_summary["counts"] != summary.get("counts")
        ):
            raise Figure3ExportError("selection closeout counts mismatch")
    stored_contracts = summary.get("comparison_contract_ids", [])
    if not isinstance(stored_contracts, list) or not all(
        isinstance(item, str) and item for item in stored_contracts
    ):
        raise Figure3ExportError("selection store has malformed comparison identities")
    expected_contract_identity = _selection_identity(comparison_contract_id)
    if stored_contracts and set(stored_contracts) != {expected_contract_identity}:
        raise Figure3ExportError(
            "selection store comparison identity disagrees with Figure 3 contract"
        )
    return {
        "path": destination.name,
        "sha256": digest,
        "record_count": expected_record_count,
        "record_type_counts": dict(sorted(seen.items())),
        "trace_usage": dict(summary.get("usage") or {
            "distinct_trace_ids": len(trace_ids),
            "selected_trace_ids": len(selected_trace_ids),
            "exposed_trace_ids": len(exposed_trace_ids),
            "feedback_trace_ids": len(feedback_trace_ids),
        }),
        "store_summary": summary,
    }


def export_run(run_dir: str | Path, *, output_path: str | Path | None = None) -> dict[str, Any]:
    """Audit and export one completed Figure 3 run."""

    root = Path(run_dir).resolve()
    meta = _object(root / "run_meta.json", "run_meta.json")
    final = _object(root / "final.json", "final.json")
    contract = _contract_from_meta(root, meta)
    status = final.get("status")
    if status is not None and status not in _TERMINAL_STATUSES:
        raise Figure3ExportError(f"run is not complete (status={status!r})")
    verdicts = final.get("verdicts")
    if not isinstance(verdicts, Mapping) or set(verdicts) != set(contract["task_order"]):
        raise Figure3ExportError("final.verdicts does not match the runner-owned task_order")
    _check_runtime_evidence(root, contract, meta, final)
    # Selection mode must carry a durable store for a formal export.  Dry-run
    # manifests are allowed to omit it because no worker was admitted.
    selection = meta.get("selection")
    required_store = not (status == "DRY_RUN")
    if not isinstance(selection, Mapping):
        raise Figure3ExportError("run_meta.selection is missing or invalid")
    if selection.get("enabled") is not True:
        raise Figure3ExportError("Figure 3 contract is present but selection is disabled")
    if status != "DRY_RUN":
        if not (root / "selection_runtime.json").is_file():
            raise Figure3ExportError("completed Figure 3 run is missing selection_runtime.json")
        if not (root / "selection_summary.json").is_file():
            raise Figure3ExportError("completed Figure 3 run is missing selection_summary.json")
    store_evidence = _audit_selection_artifact(
        root,
        required=required_store,
        comparison_contract_id=contract["comparison_contract_id"],
    )

    summary = build_figure3_run_summary(
        root,
        comparison_contract=contract["comparison_contract_id"],
        task_order=contract["task_order"],
        paired_seed=contract["paired_seed"],
    )
    metadata = summary["metadata"]
    metadata.update(
        {
            "comparison_contract_id": contract["comparison_contract_id"],
            "selector_name": contract["selector_name"],
            "selector_version": contract["selector_version"],
            "selection_config_id": contract["selection_config_id"],
        }
    )
    summary["contract"] = contract
    summary["metrics"]["accepted_score_history"] = _accepted_score_history(
        root, contract["task_order"]
    )
    summary["artifacts"] = {
        "run_meta": "run_meta.json",
        "final": "final.json",
        "scoreboard_history": (
            "scoreboard_history.jsonl"
            if (root / "scoreboard_history.jsonl").exists()
            else None
        ),
        "selection_store": "selection.sqlite3" if (root / "selection.sqlite3").exists() else None,
        "selection_events": store_evidence,
    }
    destination = Path(output_path) if output_path is not None else root / "figure3_summary.json"
    write_figure3_run_summary(destination, summary)
    return summary


def _discover_runs(path: str | Path) -> list[Path]:
    root = Path(path).resolve()
    if (root / "run_meta.json").is_file() and (root / "final.json").is_file():
        return [root]
    if not root.is_dir():
        raise Figure3ExportError(f"run path is not a directory: {path}")
    result = sorted(
        {candidate.parent for candidate in root.rglob("final.json") if (candidate.parent / "run_meta.json").is_file()},
        key=lambda item: str(item),
    )
    if not result:
        raise Figure3ExportError(f"no completed run directories under {path}")
    return result


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise Figure3ExportError("cannot calculate a bootstrap interval with no pairs")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def paired_bootstrap(
    report: Mapping[str, Any], *, replicates: int = 10_000, seed: int = 0
) -> dict[str, Any]:
    """Return deterministic percentile 95% paired-bootstrap intervals."""

    if isinstance(replicates, bool) or not isinstance(replicates, int) or not 100 <= replicates <= 1_000_000:
        raise Figure3ExportError("bootstrap replicates must be an integer in [100, 1000000]")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise Figure3ExportError("bootstrap seed must be a non-negative integer")
    pairs = report.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise Figure3ExportError("paired comparison has no matched runs")
    metrics = ("final_score", "normalized_score_time_auc")
    rng = random.Random(seed)
    result: dict[str, Any] = {
        "method": "paired_percentile_bootstrap",
        "confidence": 0.95,
        "replicates": replicates,
        "seed": seed,
        "n_pairs": len(pairs),
        "intervals": {},
    }
    for metric in metrics:
        values: list[float] = []
        for pair in pairs:
            differences = pair.get("differences") if isinstance(pair, Mapping) else None
            if not isinstance(differences, Mapping):
                raise Figure3ExportError("paired report contains malformed differences")
            value = differences.get(metric)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise Figure3ExportError(f"paired difference {metric} is invalid")
            values.append(float(value))
        means: list[float] = []
        for _ in range(replicates):
            sample = [values[rng.randrange(len(values))] for _ in values]
            means.append(sum(sample) / len(sample))
        result["intervals"][metric] = {
            "estimate": sum(values) / len(values),
            "lower": _percentile(means, 0.025),
            "upper": _percentile(means, 0.975),
        }
    return result


def compare_runs(
    left: str | Path,
    right: str | Path,
    *,
    output_path: str | Path | None = None,
    bootstrap_replicates: int = 10_000,
    bootstrap_seed: int = 0,
) -> dict[str, Any]:
    """Export both sides and construct a fail-closed paired report."""

    left_dirs = _discover_runs(left)
    right_dirs = _discover_runs(right)
    left_rows = [export_run(path) for path in left_dirs]
    right_rows = [export_run(path) for path in right_dirs]
    report = collect_paired_metrics(left_rows, right_rows)
    # Pair-level selector identities are useful in plots, but are not part of
    # the common contract checked by ``collect_paired_metrics``.
    left_names = sorted({str(row["metadata"].get("selector_name", "")) for row in left_rows})
    right_names = sorted({str(row["metadata"].get("selector_name", "")) for row in right_rows})
    report = {
        "schema": SUMMARY_SCHEMA,
        "left_selector_names": left_names,
        "right_selector_names": right_names,
        **report,
        "bootstrap": paired_bootstrap(
            report, replicates=bootstrap_replicates, seed=bootstrap_seed
        ),
    }
    if output_path is not None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="audit and export one or more run directories")
    export.add_argument("run", nargs="+", type=Path)
    export.add_argument("--output-dir", type=Path, help="directory for summary files")
    compare = sub.add_parser("compare", help="paired comparison of two run roots")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument("--output", type=Path)
    compare.add_argument("--bootstrap-replicates", type=int, default=10_000)
    compare.add_argument("--bootstrap-seed", type=int, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export":
            exported_rows: list[dict[str, Any]] = []
            for path in args.run:
                destination = None
                if args.output_dir is not None:
                    destination = args.output_dir / f"{path.resolve().name}.figure3_summary.json"
                exported_rows.append(export_run(path, output_path=destination))
            payload: Any = {
                "exported_runs": len(exported_rows),
                "summaries": [
                    {
                        "run_id": row["run_id"],
                        "selector_name": row["metadata"].get("selector_name"),
                        "paired_seed": row["metadata"].get("paired_seed"),
                        "final_score": row["metrics"].get("final_score"),
                        "normalized_score_time_auc": row["metrics"].get(
                            "normalized_score_time_auc"
                        ),
                    }
                    for row in exported_rows
                ],
            }
        else:
            payload = compare_runs(
                args.left,
                args.right,
                output_path=args.output,
                bootstrap_replicates=args.bootstrap_replicates,
                bootstrap_seed=args.bootstrap_seed,
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (Figure3ExportError, ArtifactValidationError, OSError, ValueError) as exc:
        print(f"figure3 export failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
