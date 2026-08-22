# Six-dataset one-hour Mono/Parallel matrix

This directory contains the registered 12-cell formal matrix: each of the six
frozen benchmark bundles has one communication-free Mono arm and one
communication-free Parallel arm. Every cell fixes the same model, evaluator,
runtime limits, 12-task bundle, and one-hour horizon. A cell closes immediately
when all 12 tasks reach the authoritative full score; otherwise it closes at
the horizon.

The tracked manifests contain no Judge URL, NuRouter node configuration, or
credentials. Before a real run, provide the operator-local capabilities and
the revision-matched declaration index required by the formal arms, then run
the transport preflight for the selected manifest:

```bash
scripts/run_docker.sh \
  --config configs/formal_1h_6datasets/matholympiadbench_parallel.toml \
  preflight
```

After preflight succeeds, launch the arm with the same manifest and keep each
dataset/mode output directory separate. The launcher requires a clean,
commit-bound worktree for real runs.
