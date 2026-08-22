        # MathOlympiadBench Formal Task: `usa2024_p2`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# USA Mathematical Olympiad 2024, Problem 2

Let S₁, S₂, ..., S₁₀₀ be finite sets of integers whose intersection
is not empty. For each non-empty T ⊆ {S₁, S₂, ..., S₁₀₀}, the size of
the intersection of the sets in T is a multiple of the number of
sets in T. What is the least possible number of elements that are in
at least 50 sets?

## Formalization Revision

This task is a documented repaired fork of the original MOBench row. ContextSwarm
commit `5ce40d200affbb9e9d432da1a49d565a1c5ab7b0` restored all three load-bearing
parts of the official statement: exactly 100 sets, intersection cardinality a
multiple of the number of selected sets, and the fixed threshold 50. Compfiles
later made the same semantic repair in commit
`a70ebe163bdeed2eea3e6d5f7c9d89ab2af6d953`. Pre-correction results are not
comparable.

        ## Task Surface

        - Baseline file: `baseline/usa2024_p2.lean`.
        - Problem id: `Usa2024P2`
        - Namespace: `(none)`
        - Target theorem: `usa2024_p2`

        ## Allowed Edits

        - Fill in the implementation, answer placeholders, or proof placeholders needed to prove the target theorem.
        - Add helper lemmas or intermediate definitions when they preserve the original specification.

        ## Forbidden Edits

        - Do not weaken or change any target theorem statement.
        - Do not change imports, namespace, preconditions, postconditions, or required function signatures.
        - Do not introduce `axiom`, `admit`, `unsafe`, or leave any `sorry`.

        ## Work Mode

        - Follow the mandatory execution and verification contract in the worker prompt.
        - Treat this as offline proof construction. The sole checking exception is
          `judge_check`, the experiment-provided controlled external Judge interface.
        - Never execute local Lean/lake/elan, install or download Lean/Mathlib/toolchains,
          run a local verifier or proof search, perform resource-heavy computation, or
          start background or parallel processes. Never call raw Judge HTTP endpoints.
        - If `judge_check` is unavailable or overloaded, retry/wait only through that tool
          within budget, or leave the best `result.lean`; do not create a local fallback.
        - The assigned scope includes this task directory and any shared CPS context,
          shared candidate, or helper tool explicitly named by the runner's worker prompt.
          Do not browse any other home, system, runtime, worker, or session artifacts.
        - Edit `result.lean` only within the allowed proof surface described above.
