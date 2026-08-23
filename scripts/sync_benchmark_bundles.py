#!/usr/bin/env python3
"""Import the six public benchmark bundles from a clean ContextSwarm checkout.

ICPC worker artifacts intentionally contain a neutral C++ skeleton rather than
the public accepted reference that older upstream bundles called ``baseline``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sync_problem_work_mode import synchronized_text


UPSTREAM_REPOSITORY = "https://github.com/shiyegao/ContextSwarm"

# Coding workers must never start from a public accepted implementation.  The
# upstream ContextSwarm coding bundles historically called that implementation
# ``baseline.cpp``; retain the public source only in the Judge-side package and
# emit this neutral, compiling skeleton into the worker-facing bundle.
NEUTRAL_CPP_BASELINE = """#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    // Neutral starting skeleton. Construct the solution from the statement
    // and controlled Judge feedback.
    return 0;
}
"""


@dataclass(frozen=True)
class Bundle:
    name: str
    destination: str
    aggregate: str
    subset: str
    language: str
    candidate_filename: str
    kind: str
    lean_env_id: str | None = None
    aliases: tuple[str, ...] = ()
    runnable_with_mini: bool = False


BUNDLES = (
    Bundle(
        name="usaco",
        destination="usaco",
        aggregate="definitions/coding/usaco_contests/season26_contest3_full12",
        subset="season26_contest3_full12",
        language="cpp",
        candidate_filename="result.cpp",
        kind="usaco",
    ),
    Bundle(
        name="icpc_wf_2025",
        destination="icpc_wf_2025",
        aggregate="definitions/coding/icpc_wf_2025/all",
        subset="all",
        language="cpp",
        candidate_filename="result.cpp",
        kind="icpc",
        aliases=("icpc",),
    ),
    Bundle(
        name="putnambench",
        destination="putnambench",
        aggregate="definitions/formal/putnambench/latest12",
        subset="latest12",
        language="lean",
        candidate_filename="result.lean",
        kind="formal",
        lean_env_id="formal_putnambench",
    ),
    Bundle(
        name="matholympiadbench",
        destination="matholympiadbench",
        aggregate="definitions/formal/matholympiadbench/latest12",
        subset="latest12",
        language="lean",
        candidate_filename="result.lean",
        kind="formal",
        lean_env_id="formal_matholympiadbench",
        aliases=("imobench", "om_bench"),
        runnable_with_mini=True,
    ),
    Bundle(
        name="clever",
        destination="clever",
        aggregate="definitions/formal/clever/hard12",
        subset="hard12",
        language="lean",
        candidate_filename="result.lean",
        kind="formal",
        lean_env_id="formal_clever",
    ),
    Bundle(
        name="verina",
        destination="verina",
        aggregate="definitions/formal/verina/hard12",
        subset="hard12",
        language="lean",
        candidate_filename="result.lean",
        kind="formal",
        lean_env_id="formal_verina",
    ),
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _git_output(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _validate_source(source: Path) -> str:
    revision = _git_output(source, "rev-parse", "HEAD")
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("source checkout does not have a full Git revision")
    if _git_output(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("source checkout has tracked modifications")
    for bundle in BUNDLES:
        if not (source / bundle.aggregate / "problem_ids.json").is_file():
            raise ValueError(f"missing source bundle: {bundle.aggregate}")
    return revision


def _common_payloads(source: Path, bundle: Bundle) -> dict[Path, bytes]:
    aggregate = source / bundle.aggregate
    payloads = {
        Path("problem_ids.json"): (aggregate / "problem_ids.json").read_bytes(),
        Path("benchmark_integrity.json"): (
            aggregate / "benchmark_integrity.json"
        ).read_bytes(),
    }
    provenance = aggregate / "PROVENANCE.md"
    if provenance.is_file():
        payloads[Path("PROVENANCE.md")] = provenance.read_bytes()
    return payloads


def _formal_payloads(source: Path, bundle: Bundle, ids: list[str]) -> dict[Path, bytes]:
    payloads = _common_payloads(source, bundle)
    task_root = (source / bundle.aggregate).parent
    for problem_id in ids:
        source_task = task_root / problem_id
        problem_text = (source_task / "problem.md").read_text(encoding="utf-8")
        if bundle.name == "matholympiadbench":
            problem_text = synchronized_text(problem_text)
        payloads[Path(problem_id) / "problem.md"] = problem_text.encode("utf-8")
        payloads[Path(problem_id) / "metadata.json"] = (
            source_task / "metadata.json"
        ).read_bytes()
        baselines = sorted((source_task / "baseline").glob("*.lean"))
        if len(baselines) != 1:
            raise ValueError(f"{source_task} must contain exactly one Lean baseline")
        baseline = baselines[0]
        payloads[Path(problem_id) / "baseline" / baseline.name] = baseline.read_bytes()
    return payloads


def _icpc_payloads(source: Path, bundle: Bundle, ids: list[str]) -> dict[Path, bytes]:
    payloads = _common_payloads(source, bundle)
    task_root = (source / bundle.aggregate).parent
    for problem_id in ids:
        source_task = task_root / problem_id.removeprefix("wf2025_")
        # The upstream config entry is a provenance stub, not the contest
        # statement.  Keep the checked-in official statement text (which is
        # URL/reference-free) stable when refreshing the projection.
        tracked_statement = (
            ROOT / "benchmarks" / bundle.destination / problem_id / "problem.md"
        )
        if not tracked_statement.is_file():
            raise ValueError(
                f"missing tracked official ICPC statement: {tracked_statement}; "
                "refusing to fall back to the upstream provenance stub"
            )
        statement_text = tracked_statement.read_text(encoding="utf-8")
        if (
            not statement_text.startswith("Problem ")
            or "Public AC baseline" in statement_text
            or "http://" in statement_text.lower()
            or "https://" in statement_text.lower()
        ):
            raise ValueError(
                f"ICPC worker statement is not a URL-free official statement: "
                f"{tracked_statement}"
            )
        payloads[Path(problem_id) / "problem.md"] = statement_text.encode("utf-8")
        baselines = sorted((source_task / "baseline").glob("*.cpp"))
        if len(baselines) != 1:
            raise ValueError(f"{source_task} must contain exactly one C++ baseline")
        # Validate that the source still carries a single coding baseline, but
        # never copy its bytes into the worker-visible artifact.
        payloads[Path(problem_id) / "baseline" / "baseline.cpp"] = (
            NEUTRAL_CPP_BASELINE.encode("utf-8")
        )
    return payloads


def _usaco_payloads(source: Path, bundle: Bundle) -> dict[Path, bytes]:
    payloads = _common_payloads(source, bundle)
    aggregate = source / bundle.aggregate
    for relative in (
        Path("public_dataset/usaco_2025_dict.json"),
        Path("public_dataset/usaco_2025/.contextswarm_usaco_public_dataset_v1"),
    ):
        payloads[relative] = (aggregate / relative).read_bytes()
    return payloads


def _manifest(
    source: Path,
    revision: str,
    bundle: Bundle,
    ids: list[str],
) -> dict[str, Any]:
    aggregate = source / bundle.aggregate
    integrity = _load_json(aggregate / "benchmark_integrity.json")
    benchmark_revision = integrity.get("benchmark_revision") or integrity.get(
        "suite_revision"
    )
    payload: dict[str, Any] = {
        "schema_version": (
            "contextswarm_mini_matholympiadbench_v1"
            if bundle.name == "matholympiadbench"
            else "contextswarm_mini_benchmark_bundle_v1"
        ),
        "dataset": bundle.name,
        "subset": bundle.subset,
        "task_count": len(ids),
        "problem_ids": "problem_ids.json",
        "language": bundle.language,
        "candidate_filename": bundle.candidate_filename,
        "benchmark_revision": benchmark_revision,
        "integrity_manifest": "benchmark_integrity.json",
        "contextswarm_source": {
            "repository": UPSTREAM_REPOSITORY,
            "revision": revision,
            "path": bundle.aggregate,
        },
        "runnable_with_contextswarm_mini": bundle.runnable_with_mini,
    }
    if bundle.aliases:
        payload["aliases"] = list(bundle.aliases)
    if bundle.kind == "icpc":
        payload["worker_baseline"] = "neutral_cpp_skeleton_v1"
    if bundle.name == "matholympiadbench":
        # Retain the original mini-manifest field for external consumers. The
        # value is the immutable MOBench artifact digest, not the ContextSwarm
        # checkout revision recorded above.
        payload["source_revision"] = integrity["upstream_artifact_sha256"]
    if bundle.lean_env_id:
        payload.update(
            lean_env_id=bundle.lean_env_id,
            verification_profile="formal_proof",
        )
    else:
        payload["verification_profile"] = f"coding_{bundle.kind}_contest"
    if (aggregate / "PROVENANCE.md").is_file():
        payload["provenance"] = "PROVENANCE.md"
    if bundle.kind == "usaco":
        payload["public_metadata"] = "public_dataset/usaco_2025_dict.json"
    return payload


def _remove_stale_tasks(
    destination: Path,
    ids: set[str],
    *,
    check: bool,
    drift: list[str],
) -> None:
    if not destination.is_dir():
        return
    for child in sorted(destination.iterdir()):
        if not child.is_dir() or child.name in ids or child.name == "public_dataset":
            continue
        if not ((child / "problem.md").is_file() or (child / "baseline").is_dir()):
            continue
        drift.append(f"stale task: {child.relative_to(ROOT)}")
        if not check:
            shutil.rmtree(child)


def _synchronize(
    path: Path,
    expected: bytes,
    *,
    check: bool,
    drift: list[str],
) -> None:
    actual = path.read_bytes() if path.is_file() else None
    if actual == expected:
        return
    drift.append(f"out of sync: {path.relative_to(ROOT)}")
    if not check:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="clean upstream ContextSwarm checkout")
    parser.add_argument("--check", action="store_true", help="report drift without editing")
    args = parser.parse_args()

    source = args.source.resolve()
    try:
        revision = _validate_source(source)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"invalid ContextSwarm source: {exc}", file=sys.stderr)
        return 2

    drift: list[str] = []
    catalog_entries: list[dict[str, Any]] = []
    try:
        for bundle in BUNDLES:
            aggregate = source / bundle.aggregate
            ids = _load_json(aggregate / "problem_ids.json")
            if not isinstance(ids, list) or len(ids) != 12 or not all(
                isinstance(problem_id, str) for problem_id in ids
            ):
                raise ValueError(f"{bundle.aggregate} must select twelve string ids")
            if bundle.kind == "formal":
                payloads = _formal_payloads(source, bundle, ids)
            elif bundle.kind == "icpc":
                payloads = _icpc_payloads(source, bundle, ids)
            else:
                payloads = _usaco_payloads(source, bundle)

            manifest = _manifest(source, revision, bundle, ids)
            payloads[Path("manifest.json")] = _json_bytes(manifest)
            destination = ROOT / "benchmarks" / bundle.destination
            if bundle.kind in {"formal", "icpc"}:
                _remove_stale_tasks(
                    destination,
                    set(ids),
                    check=args.check,
                    drift=drift,
                )
            for relative, expected in sorted(payloads.items()):
                _synchronize(
                    destination / relative,
                    expected,
                    check=args.check,
                    drift=drift,
                )
            catalog_entries.append(
                {
                    "dataset": bundle.name,
                    "path": bundle.destination,
                    "subset": bundle.subset,
                    "task_count": len(ids),
                    "benchmark_revision": manifest["benchmark_revision"],
                    "runnable_with_contextswarm_mini": bundle.runnable_with_mini,
                    **({"aliases": list(bundle.aliases)} if bundle.aliases else {}),
                }
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"unable to synchronize benchmark bundles: {exc}", file=sys.stderr)
        return 2

    catalog = {
        "schema_version": "contextswarm_mini_benchmark_catalog_v1",
        "contextswarm_source": {
            "repository": UPSTREAM_REPOSITORY,
            "revision": revision,
        },
        "bundles": catalog_entries,
    }
    _synchronize(
        ROOT / "benchmarks" / "catalog.json",
        _json_bytes(catalog),
        check=args.check,
        drift=drift,
    )

    if args.check and drift:
        for line in drift:
            print(line, file=sys.stderr)
        return 1
    action = "checked" if args.check else "synchronized"
    print(f"{action} {len(BUNDLES)} benchmark bundles at {revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
