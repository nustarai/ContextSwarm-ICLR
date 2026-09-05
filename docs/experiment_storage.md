# Experiment storage and launch contract

Keep one primary source checkout on `main`. Experiment data belongs in a
separate `ContextSwarm-ICLR-experiments` directory. The Docker launcher accepts
`CONTEXTSWARM_MINI_RUNS_ROOT` to mount its `runs` subtree and obtains source,
manifests, and tasks exclusively
from its immutable image. Removing an old development worktree must therefore
never remove the only copy of an experiment or an operator service script.

The operator store has these roles:

- `runs/revalidation`: the accepted #38, #39, and matched Parallel artifacts.
- `runs/maintenance_smoke`: bounded checks excluded from paper aggregates.
- `legacy/<worktree-name>`: historical runs, diagnostic files, inventories,
  untracked tools, and private runtime state from retired worktrees. Historical
  status labels and scores are preserved, not relabelled during migration.
- `source-archives`: exact Git snapshots and a verified bundle preserving
  branch and detached histories before cleanup.
- `maintenance`: old-to-new path mapping and pre/post SHA-256 verification.
- `runtime`: operator-only Judge connection settings and proxy launch files.

Raw artifacts retain their original paths and provenance. Use the migration
map to locate moved files; do not rewrite a completed run's source/image hash.
Operator archives may contain private runtime files and must not be committed
or uploaded as public paper artifacts. The plotting ZIP is a selected export,
not a replacement for candidate files and full raw records.

## Running after worktree cleanup

Build from a clean, committed checkout with `bash scripts/build_image.sh` and
pin the resulting image through `CONTEXTSWARM_MINI_IMAGE`. Its revision must
match the checkout; the launcher verifies the complete tracked manifest
closure. Retain an old immutable image separately when exact replay is needed.

Provide `CONTEXTSWARM_JUDGE_URL`, `CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL`,
`CONTEXTSWARM_NUROUTER_BINARY`, and `CONTEXTSWARM_NUROUTER_NODE_CONFIG` through
the operator environment. The Judge endpoint must be reachable from the
manifest's Docker network, attest disabled result caching, and accept the
registered Mathlib revision. The shared declaration-index directory is set
through `CONTEXTSWARM_FORMAL_INDEX_DIR`; an explicit
`CONTEXTSWARM_MINI_DECL_INDEX` takes precedence. One revision-matched index is
sufficient; no retired worktree is a runtime dependency.

`scripts/run_revalidation.sh` defaults to the sibling experiment store. The
generic Docker launcher retains `runs/` as its default. Keep that checkout
directory real so relative mock manifest inheritance remains valid; optional
links inside it may point to archived result namespaces. External output uses
tracked source-tree manifests and does not relax formal launch admission.
For direct host mocks, supply an explicit `--output` in the experiment store.

```bash
bash scripts/run_revalidation.sh issue38 recency
bash scripts/run_revalidation.sh issue39 trace_state
bash scripts/run_revalidation.sh parallel

# Each real check has a 180-second worker horizon, followed by Judge closeout.
bash scripts/run_revalidation.sh smoke38
bash scripts/run_revalidation.sh smoke39
bash scripts/run_revalidation.sh smoke-parallel
```

Formal runs retain the 3600-second contract. Issue #38 retains its own
helper-disabled selector contract; Issue #39 retains its helper-enabled
allocator contract and frozen selector. Parallel matches the #39 runtime,
model, task order, seed, and 24-slot ceiling but runs one independent worker
per task (12 initial workers), with communication and selection disabled.
The slot ceiling must not be described as 24 simultaneously active Parallel
workers or as equal consumed compute.

The historical `configs/formal_1h_6datasets` Mono/Parallel leaves retain their
original seed and capacity and are separate from the matched revalidation
baseline. Their source/image and outcome semantics must remain visible in
comparisons. The former disposable diagnostic manifests and superseded
runtime patches are preserved in source snapshots rather than installed over
current runtime code.

Judge code, current deployment workspaces, NuRouter credentials, and services
used by other experiments remain outside worktree cleanup. Migrate operational
paths or retain an explicit compatibility link before unregistering any
worktree still referenced by a resident service. Check health, admitted jobs,
and terminal closeout; an idle process alone does not prove a service can be
deleted.
