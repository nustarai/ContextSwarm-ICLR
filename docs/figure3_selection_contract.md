# Issue #38: Figure 3 selector contract

This repository contains a matched development matrix in
`configs/figure3_dev/`.  It uses the existing `matholympiadbench` bundle only;
adding or adapting the other six datasets is outside Issue #38.

`_base.toml` is an abstract inheritance layer and is not itself a launch
manifest; launch one of the eight named leaf files below.

## Eight arms

The leaf manifests are:

| Arm | Selector | Policy difference |
| --- | --- | --- |
| `random` | Random | paired hash-key sample without replacement |
| `recency` | Recency | `commit_seq` descending |
| `bm25_mmr` | BM25 + MMR | configured lexical fields, BM25, depth, cosine MMR |
| `smoothed_popularity` | Smoothed Popularity | Beta-smoothed delivered-feedback rate |
| `feedback_diversity` | Feedback–Diversity Heuristic | common typed-feedback score plus diversity term |
| `no_interaction_feedback` | No Interaction Feedback | interaction component disabled |
| `unnormalized_feedback` | Unnormalized Feedback | signed interaction without exposure denominator |
| `nustigmergy` | NuStigmergy Selector | signed interaction divided by `kappa + exposure` |

`_base.toml` fixes dataset/task order, model, CPS/Judge capacity, horizon,
retry/runtime limits, project-shared visibility, trace-slot limit, token
budget, tokenizer, seed, tie break, direct-message isolation, and candidate
transfer isolation.  A leaf changes only `[selection].selector_name` and its
`[selection.policy_params]` table (plus its human-readable name/output path).
The loader emits the complete policy table in the selector identity hash.

All feedback-aware arms declare the same eleven canonical kinds and signed
values.  The values are a development bootstrap, not a claim that they have
already been validated on an official repeat:

```text
useful=1, diagnostic_useful=0.75, route_improving=2,
route_attempted=0, not_used=0, needs_refinement=-0.25,
duplicate=-0.5, not_useful=-1, stale=-1.25,
misleading=-2, unsafe=-3
```

The three Nu profiles share all fields and differ only in interaction mode:
`none`, `unnormalized`, and `nu`.  Their common development weights are
`relevance=1, evidence=1, interaction=1, structure=.5, state=.5`, with
`kappa=1`, `quota=8`, `exploration=0`, score precision 8, and
`trace_id_asc` tie breaking.  Feedback–Diversity adds
`diversity_weight=.25`; it does not alter the Nu ablation fields.

## Runtime and attribution rule

Every selector receives the same immutable project-wide eligible snapshot.
Messages, actor profiles, candidate files, validation/control rows, delivery
receipts, and maintenance rows are excluded.  The common packer applies the
same eight-item and 4096-token limits after ranking.  Random derives a stable
key from the paired seed, task/episode/search ordinal, and trace ID; it does
not use process-global randomness.  Recency uses committed CPS sequence, then
`trace_id ASC`.

An exposed result is auditable through:

```text
search_event_id
  -> exposure_id
  -> exposure_item_id (trace_id, rank, component scores)
  -> feedback_event_id (one effective terminal worker outcome at most)
```

Worker interaction feedback is aggregated separately from verifier evidence
and maintenance events.  Delivered exposure items, including items with no
feedback, form the denominator used by Nu and popularity.  The search,
prompt digest, and broker callback all use the same selection runtime.

## Formal-readiness gate and selector-selection rule

The files under `figure3_dev/` deliberately use
`allocation.policy = "uniform"` for offline/mock development.  In this code
base that identifier is the legacy deterministic round-robin allocator; it is
not Issue #39's registered **Uniform Refill** (which minimizes current active
leases).  Therefore these manifests must not be used to report formal Figure 3
results.  A formal launcher must fail closed until #39 supplies the
`uniform_refill` implementation and updates the matched base without changing
any other arm-invariant field.

After that gate is satisfied, freeze the selector configuration hashes before
the first official repeat.  Select the downstream feedback component using the
following registered rule, recorded as a rule ID in the run artifact:

1. Use paired-repeat fixed-horizon nAUC of accepted score as the primary
   metric; retain the full accepted-score history, final score, and
   time-to-k (including full score) for every arm.
2. Use a prespecified validation split of paired repeats.  Exclude an arm if
   it violates an isolation, attribution, evaluator, runtime, or exposure
   guardrail, or if its artifacts cannot reconstruct the full attribution
   chain.  Guardrail failures are not repaired by tuning that arm.
3. Among arms passing guardrails, choose the highest mean validation nAUC.  Use
   the lower paired-bootstrap 95% interval endpoint as the first tie-break,
   then lower median time to the prespecified target k, then the fixed registry
   order shown above.  No visual curve preference or post-hoc parameter change
   is allowed.
4. Record the selected arm name, selector configuration ID/hash, comparison
   contract ID, paired seed set, validation split, metric, tie-breaks, and
   guardrail results in the machine-readable Figure 3 summary.  Hold this
   selector and configuration fixed for the downstream allocation and
   integrated runs.

For the downstream formal Figure 4 exception, the selected selector's
`selection.seed` is part of that frozen identity and may remain fixed while a
paired repeat varies `[experiment].seed`.  The runner uses the experiment seed
for paired request derivation in that phase.  Ordinary Issue #38 selection
manifests still require the two seed fields to be equal; this separation must
not be used to create an unmatched Figure 3 arm.

This rule is a development protocol until the owner freezes its repeat count,
split, and numeric guardrail thresholds in the formal run manifest.  It is
intentionally explicit about the decision order so those values cannot be
chosen after looking at formal outcomes.

## Reproducibility checklist

Before a formal run, verify that all eight loaded manifests have identical
comparison-contract hashes and task order, differ only in selector identity or
policy parameters, have `direct_messages=false` and `candidate_transfer=false`,
and expose the same model/Judge/horizon/CPS/token limits.  Keep private
endpoints and credentials in the operator environment; never place them in a
tracked manifest or summary.
