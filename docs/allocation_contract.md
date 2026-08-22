# Figure 4 allocation contract

This document freezes the public semantics for issue #39.  It defines the
inputs, policy names, scoring boundary, audit rows, and paired-run outputs; it
does not replace the registered run contract in `AGENTS.md`.  In particular,
an allocation arm still runs until full score or its fixed horizon, and a
failed candidate attempt releases its lease and is eligible for refill while
time and capacity remain.

## Canonical policies

Artifacts and new manifests use exactly these policy identifiers:

| Policy | Required behavior |
| --- | --- |
| `uniform_refill` | Choose the eligible unresolved task with the smallest current active-lease count, with lexicographic task ID as the only tie breaker. |
| `task_state` | Score ordinary task and checker state only.  It must not query, receive, or infer trace-store state. |
| `trace_state` | Use the identical Task-State score and add only the normalized trace increment below. |
| `llm_scheduler` | Give a read-only model the same bounded snapshot visible to Trace-State and strictly validate its choice against the snapshot. |

The old `uniform`, `formula`, and `agent` names, if retained for historical
manifests, are compatibility names and must not be emitted as Figure 4 policy
identifiers.

`n_q` is the number of live solver leases admitted to unresolved task `q` at
the snapshot boundary.  Admitted and running leases count.  Finished,
released, cancelled, rejected, and scheduler-reservation leases do not count.
It is not a cursor, cumulative assignment count, attempt count, or desired
allocation.  Uniform Refill minimizes this exact value.

## Immutable state and scores

Every choice is made from one immutable
`contextswarm_allocation_state_v1` snapshot.  A snapshot contains:

- `state_id`, `decision_id`, repeat/decision identity, elapsed and remaining
  horizon time;
- `total_capacity`, `active_solver_slots`, `scheduler_reserved_slots`, and
  `free_slots`, whose sum equals `total_capacity`;
- the fixed `task_order`, lexicographically ordered `eligible_task_ids`, and a
  task record for every task in that order;
- per-task `active_leases` (`n_q`), eligibility/resolution state, normalized
  ordinary features, normalized trace features, and feature provenance;
- the complete manifest-selected normalization and scoring parameters and
  their canonical configuration hash.

The state ID is the lowercase SHA-256 of canonical compact JSON (UTF-8,
sorted keys, no insignificant whitespace) for the snapshot with `state_id`
removed.  Consumers must treat all snapshot data as read-only.  Eligibility
and the lexicographic task-ID tie breaker are shared by all policies.  A policy
may return no task only when the eligible set is empty or the horizon prevents
admission.

For normalized ordinary features in `[0, 1]`, define

```text
U_task(q) = (vQ*Q_q + vDelta*Delta_q + vX*X_q - vG*G_q) / (1 + n_q)
```

where `Q` is checker-backed candidate quality, `Delta` is improvement recency,
`X` is starvation, and `G` is failure/no-improvement.  The manifest owns the
four coefficients and all feature windows and saturations.

Trace-State begins with exactly that value and adds

```text
D_q = dDup*duplicate_q + dRef*refutation_q
    + dStale*staleness_q + dLineage*lineage_stagnation_q

I_trace(q) = (wA*A_q + wV*V_q + wPlus*Fplus_q
              - wMinus*Fminus_q - wD*D_q) / (1 + n_q)

U_trace(q) = U_task(q) + I_trace(q)
```

`A` is frontier value, `V` is evidence association, and `Fplus`/`Fminus`
are positive/negative feedback normalized by trace exposure.  `D` combines
duplication, refutation, staleness, and lineage stagnation.  Every `v`, `w`,
`d`, normalization window, saturation, and feedback-kind mapping comes from
the manifest, is included in the canonical allocation configuration hash, and
is emitted with the run artifacts.  There are no outcome-dependent hidden
defaults or between-arm parameter changes.

Checker/Judge outcomes are ordinary state.  A trace projection must exclude
any source outcome whose stable ID appears in
`ordinary.checker_outcome_ids`; association, feedback, or links to that same
receipt cannot score it a second time.  The projection emits
`source_outcome_ids` so this disjointness is auditable.  When every
`I_trace(q)` is exactly zero, Trace-State must reproduce Task-State's scores,
tie break, fallback behavior, requested task, and admitted task exactly.

An LLM Scheduler receives the same bounded task records and parameters as
Trace-State, with no write tools.  Its output is strict JSON containing one
eligible `task_id`, a bounded `reason`, and trace references present in the
snapshot.  Malformed, stale, timed-out, or ineligible output uses the shared
deterministic Task-State fallback.  Each call reserves one scheduler slot from
the same fixed capacity while it is running; retry time remains inside the
fixed horizon.  Calls, input/output tokens, latency, reservation count,
occupied slot-seconds, invalid output count, and fallback count are recorded.

## Decision, counterfactual, and cost artifacts

`allocation_decisions.jsonl` uses
`contextswarm_allocation_decision_v2`.  Every row records the decision and
state IDs, canonical policy, eligible set, requested and admitted task,
task-only score, trace increment and total score by task, fallback reason,
parameter/config hash, and scheduler cost for that decision.  An admission
revalidation failure is recorded without silently recomputing from a new
state.

For every executed Trace-State admission, `allocation_audit.jsonl` contains a
`contextswarm_allocation_audit_v1` row.  Both decisions are computed before
dispatch from the same snapshot:

- `allocation_before`, `trace_state_allocation_after`, and
  `task_state_allocation_after` are integer vectors keyed by every task ID;
- the row includes the state/decision ID, eligible tasks, task-only scores,
  trace increments, total scores, both selected tasks, the actually admitted
  task, fallback reason, and active/free/reserved/total capacity before and
  after;
- each after vector equals the before vector plus one admitted solver lease,
  unless no admission occurred; both counterfactuals use the same admission
  count;
- `sum_q(trace_after[q] - task_after[q]) == 0` is mandatory and is emitted as
  `capacity_delta_sum: 0` with `capacity_conserved: true`.

The Task-State counterfactual is diagnostic only: it must not dispatch work,
reserve capacity, mutate the trace store, or advance any policy state.

`figure4_run_summary.json` uses
`contextswarm_figure4_run_summary_v1` and contains accepted-score history,
final accepted score, `time_to_k_seconds`, fixed-horizon nAUC, solver
calls/tokens/slot-seconds, evaluator calls/admissions/terminal receipts,
scheduler cost, allocation/fallback counts, policy parameters/config hash,
and comparison-contract identity.  `figure4_paired_repeats.jsonl` uses
`contextswarm_figure4_paired_repeat_v1`; one row contains all four arm metrics
for a paired repeat plus explicit registered contrasts, including
`trace_state_minus_task_state`, so paired-bootstrap intervals need no join
across unpaired runs.  `time_to_k_seconds` is null when `k` is not reached.
nAUC is the integral of accepted score over the fixed horizon divided by
`max_score * horizon_seconds`.

## Fixed-arm comparison and selection

The following fields are identical across the four arms and appear, directly
or via a canonical contract hash, in every paired-repeat row:

- dataset and ordered task IDs; paired repeat ID and seed;
- selector identity/configuration selected by Figure 3 and Shared trace
  visibility;
- model and inference settings;
- evaluator/Judge contract and runtime limits;
- horizon, total CPS capacity, initial allocation, and stopping rule;
- candidate-solution transfer behavior;
- communication mode with direct messages disabled.

Only `allocation.policy` may differ.  Output names and run IDs may differ but
are not experimental inputs.  Mono and Parallel remain communication-free.
Formal repeats start only after the Figure 3 selector is frozen.

Before formal repeats, the allocator-selection artifact must freeze a rule ID,
the registered paired performance metric (normally nAUC), validation split,
tie breaker, and numeric cost guardrails.  Select only among arms satisfying
those guardrails, by that frozen metric and tie breaker; do not choose from a
visually preferred trajectory or retune after observing formal outcomes.
The deterministic example in
`tests/fixtures/allocation_contract_v1.json` is normative for field names and
the score/audit invariants, but its values are not experiment defaults.
