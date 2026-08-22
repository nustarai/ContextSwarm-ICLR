# Figure 4 allocator-selection rule

`contextswarm_mini.allocator_selection` is a standalone analysis step for
issue #39.  It consumes `contextswarm_figure4_paired_repeat_v1` rows and
publishes a separate `contextswarm_allocator_selection_v1` artifact.  It does
not change a runner, re-run an arm, or replace the independent Figure 4 audit.

## Frozen decision order

Before official repeats, the experiment owner must publish one rule artifact
with a rule ID, at least eight validation repeat IDs, target `k`, bootstrap
algorithm/seed, tie-break, and numeric cost thresholds.  The frozen interval
uses exactly 10,000 paired-block percentile draws, 95% confidence, and linear
quantiles.  The development helper emits
`figure4_allocator_selection_v1` with explicit proposal values; those values
are development defaults, not silently registered formal settings.

The input must contain complete paired blocks with exactly these four arms, in
this registry order:

```text
uniform_refill, task_state, trace_state, llm_scheduler
```

Only complete blocks are used.  A candidate-bound compile/verification failure,
`RESOURCE_LIMIT`, or `EXECUTION_TIMEOUT` is a zero-progress candidate attempt
and does not invalidate its block.  Unreconciled candidate-independent
infrastructure or contract failures invalidate the block; surviving arms are
never paired ad hoc.

The validator also enforces the registered comparison boundary instead of
trusting arm labels.  All four arms, and every validation repeat, must share
one `max_score`.  Canonical allocation parameters must be byte-for-byte equal
after removing only an optional top-level `policy`/`allocation_policy` arm
identity; when either identity is present it must name the enclosing arm.
Every arm's full parameter object still has its own verified configuration
hash.  Any other parameter or normalization drift invalidates the artifact.

Eligible arms are ranked by the same deterministic order used for the Figure 3
selector:

1. highest mean fixed-horizon nAUC over the named validation blocks;
2. highest paired-block bootstrap 95% lower endpoint of that arm's mean nAUC;
3. lowest median time to the rule's fixed target `k` (unreached is `+∞`);
4. the fixed registry order above.

Cost checks are hard eligibility gates, not post-hoc ranking knobs.  The result
records observed values, thresholds, pass/fail, and the per-block details for:

- scheduler occupied capacity slot-seconds as a fraction of `B × H`;
- scheduler-token share of solver plus scheduler tokens;
- fallback fraction (`fallbacks / decisions`);
- total solver plus scheduler occupied slot-seconds against `B × H`;
- optional maximum occupied slots.

Missing, negative, non-finite, contradictory, or non-reconcilable values fail
closed.  Deterministic arms must have exactly zero scheduler calls, tokens,
latency, reservations, fallbacks, and occupied capacity.  LLM calls and
fallbacks remain charged to the LLM arm.  LLM scheduler calls, capacity
reservations, and allocation decisions have identical cardinality.  Rule and
artifact aliases may coexist only when their canonical values agree; digests
must be lowercase SHA-256 and integer fields must remain exactly representable.
Selection always reconstructs nAUC from complete accepted-score histories.

The output contains the selected policy, exact allocation parameters and
`allocation_config_sha256`, rule/config/source hashes, validation IDs and
seeds, bootstrap metadata, all guardrail outcomes, and the paired
`trace_state - task_state` interval.  If no arm passes, the output is
`status: "no_selection"` with a bounded reason; downstream Ours must stop
instead of guessing an allocator.

This artifact is a validation/development decision.  It must not include
formal/official repeat-control fields or be changed after formal outcomes are
observed.  If `trace_state` is selected, a separate claim gate still requires
nonzero same-state reallocation and a positive lower interval for
Trace-State minus Task-State; selection alone is not a benefit claim.

## CLI

```bash
python3 scripts/select_allocator.py \
  --paired-repeats runs/figure4_paired_repeats.jsonl \
  --rule configs/allocator_selection_rule_dev.json \
  --output runs/allocator_selection.json
```

The command uses atomic publication and exits non-zero without a partial output
when either input is malformed.  When every arm fails a cost gate, it publishes
the auditable `status: "no_selection"` artifact and exits non-zero so downstream
Ours cannot continue accidentally.
