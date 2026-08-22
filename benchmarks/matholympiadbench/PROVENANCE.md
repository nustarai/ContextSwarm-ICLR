# MathOlympiadBench latest12 provenance and errata

## Canonical source chain

The imported benchmark artifact is `MOBench.jsonl` from
`Goedel-LM/MathOlympiadBench` revision
`390cec5f70461573a41f9be1576777264e2fc3d1`. Its byte digest is
`f286b8942a7b9517be103139d2240adaeabbc1e5f195eb2817aacf9109b1bb95`.
The same artifact was published in `Goedel-LM/Goedel-Prover-V2` commit
`1d98207aa4bd5c32ead97733810f5bada68b8e55`.
ContextSwarm imported the dataset in commit
`129edc7877d500fbc488ad6e544759bf29dd450f`.

`dwrensha/compfiles` is the lower-level formalization source. It is not the
revision pin of the immutable MOBench artifact: Compfiles can and does fix
statements after the JSON snapshot was published. When ContextSwarm applies one
of those fixes, the task metadata records both the immutable dataset row and the
exact Compfiles reference commit.

The eight tasks not listed in the errata table are unmodified semantic imports
from that artifact: `imo2024_p1`, `imo2024_p2`, `imo2024_p3`, `imo2024_p5`,
`imo2024_p6`, `uk2024_r1_p2`, `imo2023_p3`, and `imo2023_p4`.
`benchmark_integrity.json` is the machine-readable suite authority: it fixes the
ordered 12-task set, source row/index, derivation class, and checked-in baseline
SHA-256 for every task, including those eight unchanged semantic imports.

## Curated task contracts

| Canonical task | Derivation | Evidence and reason | Result compatibility |
| --- | --- | --- | --- |
| `imo2023_p2_v2` | Upstream fix semantics backported to Lean 4.9 | Compfiles `9eb1eefae03f7da96911ae1551e588a5f8f63ced` adds affine independence, a proper acute triangle, tangent dimension 1, and `Sphere.IsTangentAt`. Those last two Mathlib APIs postdate the benchmark's Lean 4.9 environment, so this task uses their exact three-angle and pointwise orthogonal-radius logical characterizations. The old MOBench row admits degenerate tangent objects and is not a faithful theorem. | New problem id; never aggregate with `imo2023_p2`. |
| `imo2023_p5` | ContextSwarm repaired fork | ContextSwarm `5ce40d200affbb9e9d432da1a49d565a1c5ab7b0` makes the natural-language positive-integer domain explicit. Compfiles `bdc6550f227feab8a6b1f9ef0bf3d5bbba470715` later chose a different valid repair by totalizing `n = 0`. | Pre-correction theorem-header results are incompatible. |
| `usa2024_p2` | ContextSwarm repair, later upstream-aligned | ContextSwarm `5ce40d200affbb9e9d432da1a49d565a1c5ab7b0` restores 100 sets, the divisibility direction, and threshold 50. Compfiles `a70ebe163bdeed2eea3e6d5f7c9d89ab2af6d953` later makes the same semantic repair. | Pre-correction statement results are incompatible. |
| `uk2024_r1_p1` | Semantics-preserving representation normalization | ContextSwarm `5ce40d200affbb9e9d432da1a49d565a1c5ab7b0` replaces `Set.ncard` by `Fintype.card` of the corresponding subtype. | Mathematical scores are comparable; old proof text is not mechanically reusable against the new header. |

Each row's `metadata.json` is the machine-readable authority for its derivation
status, source path and commits, semantics revision, correction identifiers, and
compatibility rule.

## Identity and experiment rule

A semantic statement change must receive a new canonical task id; it must not be
patched silently. Thus the invalid `imo2023_p2` task is retired from the latest12
manifest and replaced by `imo2023_p2_v2`.

The formal project-multi-assignment runner hashes the ordered manifest plus every
task's behavior-bearing `metadata.json`, rendered `problem.md`, `evaluate.py`, and
baseline source into `semantic_problem_contract_sha256`. Recovery and finalization
must match that digest. Formal experiment reports and proof bundles therefore need
both the task id and semantic contract digest; grouping only by the historical
olympiad label is invalid.
