# PutnamBench 2025 latest12 provenance

The canonical suite contains Putnam 2025 A1--A6 and B1--B6 from
`trishullab/PutnamBench`, pinned at Git commit
`77ea5a04b28b284f2b95f5c02dd46096bf75d33b` (2026-04-20). The corresponding
source path for each task is `lean4/src/<problem_id>.lean` and is also recorded
in that task's `metadata.json`.

ContextSwarm imported the formal benchmark in commit
`129edc7877d500fbc488ad6e544759bf29dd450f`. Six upstream files already had a
fully specified answer definition and remain byte-identical to the pin. The
other six used `sorry` for the answer definition while documenting the expected
answer in a comment. ContextSwarm commit
`54c3a91b9b1e18f4d3aeecb00adc341a98fb04a4` materialized those answers so the
public tasks are closed Lean propositions:

- exact upstream snapshot: A1, A6, B1, B2, B4, B5;
- answer materialized locally: A2, A3, A4, A5, B3, B6.

The A5 answer is represented as adjacent sign alternation rather than the
upstream comment's disjunction of the two alternating sequences. For
`s : Fin n → ℤˣ` under the theorem's `1 ≤ n` hypothesis these characterize the
same two sequences. Although the printed target theorem headers did not change,
materializing six previously opaque `sorry` answer definitions was a semantic
repair. Any result produced against the pre-`54c3a91b` placeholders is not a
valid result for the current suite.

`benchmark_integrity.json` records both upstream and checked-in SHA-256 values,
making the six deliberate derivations distinguishable from accidental drift.
The upstream repository has not changed any 2025 Lean source between the pinned
revision and the audited repository head `dfb0a47a1c1ec3a10f2a9acfdf41a2043920f33c`
(checked 2026-08-22).

This 2026-08-22 provenance/integrity revision does not change the already
materialized Lean task contracts. Results produced from commit `54c3a91b` or
later remain compatible with it; the pre-materialization boundary above does
not. A future semantic statement correction must use a new problem slug and
must not be combined with results for the old task id.
