# Issue #38 ICPC selector freeze for Issue #39

This note records the downstream engineering choice made from the corrected
ICPC batch (`20260823T030657Z-*`).  The earlier `20260823T015*` batch is
excluded because its benchmark baselines contained answer leakage.

## Choice

The Issue #39 allocator manifests use `recency`:

```text
selector_name      = recency
selector_version   = icpc_formal_v1
primary_sort       = commit_seq_desc
visibility         = project_shared
tie_break          = trace_id_asc
```

The four allocator leaves under `configs/figure4_formal_icpc/` share this
identity and differ only in the registered allocator policy.  For the
downstream Figure 4 contract, task-local candidate handoff is enabled as the
explicit formal Figure 4 exception; direct messages remain disabled.  With
seed `1729`, the resulting downstream selector identity is
`5dae09c95a6a15c9744a884e495d64fb3f1c1207d5be64f58e282b2e0d5eae0a`.

## Evidence

Across the 8 selectors and 3 paired ICPC repeats, fixed-horizon nAUC for
`recency` was `0.769721`, `0.778051`, and `0.784363` (mean `0.777378`).
The next two means were `unnormalized_feedback = 0.776148` and
`smoothed_popularity = 0.775233`.  All 24 arms finished at `11/12`; the
common unresolved task was `wf2025_b_blackboard_game`, so final score does not
separate selectors.  The complete local evidence and source hashes are in
`runs/icpc_formal/selector_selection.json`.

## Seed binding for downstream Figure 4

Issue #38 binds `selection.seed` to `experiment.seed`: the selector identity
and the paired stochastic run seed are one value on ordinary Figure 3 arms.
Formal Figure 4 is an explicit downstream exception. It keeps the selected
`selection.seed = 1729` in the selector identity while a future paired repeat
may change `[experiment].seed`; the runner then uses that experiment seed for
paired request derivation. This keeps the selector hash stable without
silently changing the Issue #38 contract. The checked-in Figure 4 manifests
currently use 1729 for both values, so no independent repeat claim is made by
this setup yet.

## Status and limitation

This is a **provisional, retrospective engineering freeze**, not a formal
Issue #38 statistical selection claim.  Only three paired repeats are
available, while the registered validation protocol requires at least eight,
and the ICPC matrix is explicitly separate from the registered
MathOlympiadBench Figure 3 matrix.  The paired bootstrap comparison against
`unnormalized_feedback` has a 95% interval crossing zero.  The choice should
therefore be revalidated with a pre-frozen rule/split before any publication
claim; downstream allocator arms must keep this selector fixed while that
validation is pending.

Recoverable Judge/provider/runtime noise is retained and reported in the local
artifact rather than used as a post-hoc selector filter.  A strict
`health.ok`-only filter would leave `feedback_diversity` as the sole survivor,
but that is a sensitivity analysis, not the selected engineering policy.
