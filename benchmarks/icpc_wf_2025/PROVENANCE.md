# ICPC WF 2025 statement-faithful derived provenance

## Claim boundary

The benchmark uses the official ICPC World Finals 2025 A--L statements, but it
does **not** use unpublished official hidden tests, checkers, or the official
interactor. It is a `statement_faithful_derived` benchmark: those evaluation
assets were authored in ContextSwarmJudge from the official statements. It must not be reported as an official judge-parity result.

ContextSwarm introduced the per-problem configs in commit
`c6598036d41ddbdf458ce2ba61483f24533be5d0` and the aggregate entrypoint in
commit `61fe1d41f2babf89b40a85e2b14340b93213964a`. ContextSwarmJudge bootstrapped
the package collection in commit
`4773cb134e7673d600acc672c17182f316652830`. The audited evaluator is pinned to
ContextSwarmJudge revision
[`cc16c9768c659f3bfc1b0536f9de0b06317a180f`](https://github.com/shiyegao/ContextSwarmJudge/commit/cc16c9768c659f3bfc1b0536f9de0b06317a180f),
resident service version `2026-08-22.07`.

## Source chain

Each package pins its official single-problem PDF and checks extracted
`statement/official.txt` text against it. The official contest-wide memory
limit comes from the
[ICPC WF 2025 problem index](https://worldfinals.icpc.global/problems/2025/finals/index.html).
The cross-repository source and scope authorities at the pinned Judge revision
are:

- [REFERENCE_PROVENANCE.md](https://github.com/shiyegao/ContextSwarmJudge/blob/cc16c9768c659f3bfc1b0536f9de0b06317a180f/packages/icpc_wf_2025/REFERENCE_PROVENANCE.md)
- [EXPERIMENT_SCOPE.md](https://github.com/shiyegao/ContextSwarmJudge/blob/cc16c9768c659f3bfc1b0536f9de0b06317a180f/packages/icpc_wf_2025/EXPERIMENT_SCOPE.md)
- [audit_report.md](https://github.com/shiyegao/ContextSwarmJudge/blob/cc16c9768c659f3bfc1b0536f9de0b06317a180f/packages/icpc_wf_2025/audit_report.md)
- [reference_source_manifest.json](https://github.com/shiyegao/ContextSwarmJudge/blob/cc16c9768c659f3bfc1b0536f9de0b06317a180f/packages/icpc_wf_2025/reference_source_manifest.json)

The public AC references come from
[`openai/openai-icpc-2025@37cfae422caed99a8b550cd7219e5590798b6bf0`](https://github.com/openai/openai-icpc-2025/tree/37cfae422caed99a8b550cd7219e5590798b6bf0).
They are public, non-official accepted implementations used to cross-check the
repository-authored oracles/checkers/interactor. They are not official ICPC
judge artifacts. The source manifest records the exact A--L paths and SHA-256
values and the Judge audit verifies each copied source byte-for-byte.

## Repository defect history

The 2026-08-22 input-domain audit found defects in repository-authored hidden
tests, not in the official statements:

- F case 48 repeated a preference that the statement requires to be distinct;
- G cases 100/101 violated official size or distinct-elevation constraints, so
  the old case-101 external-failure witness was retired rather than presented
  as a legal counterexample;
- 42 of K's 55 inputs used negative known depths or duplicate coordinates;
- 30 multi-rectangle L inputs had rectangles that touched or intersected.

The exact repairs and preserved witnesses are documented in the pinned
[F](https://github.com/shiyegao/ContextSwarmJudge/blob/cc16c9768c659f3bfc1b0536f9de0b06317a180f/packages/icpc_wf_2025/wf2025_f_herding_cats/validation/report.md),
[G](https://github.com/shiyegao/ContextSwarmJudge/blob/cc16c9768c659f3bfc1b0536f9de0b06317a180f/packages/icpc_wf_2025/wf2025_g_lava_moat/validation/report.md),
[K](https://github.com/shiyegao/ContextSwarmJudge/blob/cc16c9768c659f3bfc1b0536f9de0b06317a180f/packages/icpc_wf_2025/wf2025_k_treasure_map/validation/report.md),
and [L](https://github.com/shiyegao/ContextSwarmJudge/blob/cc16c9768c659f3bfc1b0536f9de0b06317a180f/packages/icpc_wf_2025/wf2025_l_walking_on_sunshine/validation/report.md)
reports. Regeneration builders now preserve the valid corpus and fail on drift.

## Release gate

The fail-closed input-domain gate validates all scalar, aggregate, structural,
uniqueness, geometry, and interactive-state constraints that are mechanically
checkable from the A--L statements. The canonical audit covers 12/12 packages
and 902 public/hidden inputs, verifies the pinned public-reference provenance,
reports all 12 packages `certified`, and finishes with 0 issues / 0 warnings.
Mode-specific certification also runs positive public-reference and negative or
threshold-sensitive candidates through the real ContextSwarmJudge to embedded
Rust OJ path.

These checks establish that every current local test input is within the stated
domain and that the local evaluator is usable for a formal experiment. These checks do not establish exhaustive official contest coverage. Every result must retain
the suite revision `icpc-wf-2025-statement-faithful-derived-v1`, the Judge
revision, and the `statement_faithful_derived` claim scope.
