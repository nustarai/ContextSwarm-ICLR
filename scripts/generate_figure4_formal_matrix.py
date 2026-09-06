#!/usr/bin/env python3
"""Generate the tracked six-dataset, three-repeat Figure 4 manifest matrix.

The generated leaves are deliberately boring: every leaf inherits one common
formal Figure 4 contract, one dataset overlay, and one repeat seed.  The only
arm-level field that differs is ``allocation.policy``.  Keeping the matrix in
tracked TOML files is important because the formal Docker launcher binds the
manifest closure to the image revision.

This script has no operator capabilities or endpoint defaults.  Judge URLs,
declaration-index paths, and credentials remain runtime-only inputs.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


DATASETS = {
    "clever": {
        "kind": "formal",
        "env_id": "formal_clever",
        "profile": "formal_proof",
        "mode": "fast",
    },
    "icpc_wf_2025": {
        "kind": "coding",
        "env_id": "",
        "profile": "coding_icpc_contest",
        "mode": "coding",
    },
    "matholympiadbench": {
        "kind": "formal",
        "env_id": "formal_matholympiadbench",
        "profile": "formal_proof",
        "mode": "fast",
    },
    "putnambench": {
        "kind": "formal",
        "env_id": "formal_putnambench",
        "profile": "formal_proof",
        "mode": "fast",
    },
    "usaco": {
        "kind": "coding",
        "env_id": "",
        "profile": "coding_usaco_contest",
        "mode": "coding",
    },
    "verina": {
        "kind": "formal",
        "env_id": "formal_verina",
        "profile": "formal_proof",
        "mode": "fast",
    },
}
POLICIES = ("uniform_refill", "task_state", "trace_state", "llm_scheduler")
REPEATS = {1: 1729, 2: 1730, 3: 1731}


BASE = '''# Formal Issue #39 allocator matrix: six datasets, three paired repeats.
#
# The selector identity is frozen from the corrected remote Figure 3 result.
# Every arm keeps the same selector, model, horizon, CPS capacity, evaluator,
# and candidate-transfer boundary; only allocation.policy changes in a leaf.
extends = ["../figure3_formal/_base.toml"]

[experiment]
name = "figure4-formal-6datasets-base"
output_root = "runs/figure4_formal_6datasets"
figure4_phase = "formal"
figure3_readiness = "formal_issue39_recency_freeze_v1"
mode = "cps"
communication = "blackboard"
max_parallel = 24
initial_agents_per_task = 2
max_attempts_per_task = 0
cancel_on_proved = true
assignment_policy = "least_active"
episodes_per_task = 2
max_tasks = 12
time_limit_seconds = 3600
# Repeat leaves override only this paired-run seed.  The selector identity
# seed remains 1729 in every leaf, as required by the formal Figure 4 bridge.
seed = 1729

[allocation]
policy = "uniform_refill"
piece_limit_per_task = 3
piece_body_chars = 1200
agent_timeout_seconds = 3600
prompt_max_bytes = 65536
prompt_max_tokens = 65536

[aisw]
enabled = true
max_in_flight = 24

[pi]
timeout_seconds = 3600

[judge]
max_concurrent_evaluations = 4
require_result_cache_disabled = true

[formal_tools]
require_decl_index = true

[docker]
image = "contextswarm-iclr-mini:formal-issue39-6x3"
memory_mb = 16384
network = "bridge"

[selection]
enabled = true
selector_name = "recency"
selector_version = "icpc_formal_v1"
visibility = "project_shared"
trace_slot_limit = 8
context_token_budget = 4096
tokenizer = "utf8_bytes_ceil_div4_v1"
seed = 1729
tie_break = "trace_id_asc"
direct_messages = false
# Formal Figure 4 intentionally preserves task-local best-candidate handoff.
candidate_transfer = true

[selection.policy_params]
primary_sort = "commit_seq_desc"
'''


def _dataset_overlay(dataset: str, spec: dict[str, str]) -> str:
    formal = spec["kind"] == "formal"
    lines = [
        f'# Dataset overlay for {dataset}; no arm-specific policy belongs here.',
        'extends = ["_base.toml"]',
        "",
        "[experiment]",
        f'dataset = "{dataset}"',
        f'dataset_root = "benchmarks/{dataset}"',
        f'problem_ids = "benchmarks/{dataset}/problem_ids.json"',
        "",
        "[judge]",
        f'kind = "{spec["kind"]}"',
        f'env_id = "{spec["env_id"]}"',
        f'verification_profile = "{spec["profile"]}"',
        f'judge_mode = "{spec["mode"]}"',
        "",
        "[formal_tools]",
        f"enabled = {'true' if formal else 'false'}",
        f"require_decl_index = {'true' if formal else 'false'}",
        "",
    ]
    return "\n".join(lines)


def _leaf(dataset: str, repeat: int, seed: int, policy: str) -> str:
    return f'''# Generated formal Figure 4 leaf: {dataset}, repeat {repeat}, {policy}.
extends = ["../../_{dataset}.toml"]

[experiment]
name = "figure4-formal-{dataset}-r{repeat:02d}-{policy}"
output_root = "runs/figure4_formal_6datasets/{dataset}/repeat-{repeat:02d}/{policy}"
paired_repeat_id = {repeat}
seed = {seed}

[allocation]
policy = "{policy}"
'''


def generate(root: Path, *, clean: bool) -> list[Path]:
    if clean and root.exists():
        for child in root.iterdir():
            if child.name == "README.md":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    base = root / "_base.toml"
    base.write_text(BASE, encoding="utf-8")
    written.append(base)
    for dataset, spec in DATASETS.items():
        overlay = root / f"_{dataset}.toml"
        overlay.write_text(_dataset_overlay(dataset, spec), encoding="utf-8")
        written.append(overlay)
        for repeat, seed in REPEATS.items():
            for policy in POLICIES:
                leaf_dir = root / dataset / f"repeat{repeat}"
                leaf_dir.mkdir(parents=True, exist_ok=True)
                leaf = leaf_dir / f"{policy}.toml"
                leaf.write_text(_leaf(dataset, repeat, seed, policy), encoding="utf-8")
                written.append(leaf)
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("configs/figure4_formal_6datasets"),
        help="tracked matrix directory",
    )
    parser.add_argument("--clean", action="store_true", help="remove generated children first")
    args = parser.parse_args()
    paths = generate(args.root, clean=args.clean)
    print(f"wrote {len(paths)} manifests under {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
