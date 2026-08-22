# Frozen benchmark bundles

The six public, paper-facing benchmark bundles in this directory are synchronized
from `shiyegao/ContextSwarm` commit
`cfed6e508193a0eeebdc56e1e8846f70a0ecc635` (2026-08-22). The machine-readable
catalog is `catalog.json`; every bundle also carries a pinned `manifest.json` and
the upstream `benchmark_integrity.json`.

| Bundle | Subset | Public tasks | Mini runtime |
| --- | --- | ---: | --- |
| `usaco/` | Season 26 Contest 3 Full12 | 12 | definition bundle only |
| `icpc_wf_2025/` | World Finals 2025 A--L | 12 | definition bundle only |
| `putnambench/` | 2025 latest12 | 12 | definition bundle only |
| `matholympiadbench/` (`imobench`) | latest12 integrity v2 | 12 | runnable/default |
| `clever/` | hard12 integrity v2 | 12 | definition bundle only |
| `verina/` | functional hard12 integrity v2 | 12 | definition bundle only |

Only public statements or public metadata, task metadata, and baseline skeletons
are redistributed. Upstream production evaluators, hidden tests, oracle outputs,
solutions, and Judge-side packages are intentionally absent. In particular, the
USACO directory contains the public metadata projection and expected resident
test contracts, not the hidden test corpus.

Maintainers can refresh the projection from a clean upstream checkout with:

```bash
python3 scripts/sync_benchmark_bundles.py /path/to/ContextSwarm
python3 scripts/sync_benchmark_bundles.py /path/to/ContextSwarm --check
```

The default experiment manifests remain bound to MathOlympiadBench. Adding a
bundle here does not silently change the task/model/time/evaluator contract of
Mono, Parallel, or CPS experiments.
