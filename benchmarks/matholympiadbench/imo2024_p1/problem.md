        # MathOlympiadBench Formal Task: `imo2024_p1`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# International Mathematical Olympiad 2024, Problem 1

Determine all real numbers α such that, for every positive integer n, the
integer

     ⌊α⌋ + ⌊2α⌋ + ... + ⌊nα⌋

is a multiple of n.

        ## Task Surface

        - Baseline file: `baseline/imo2024_p1.lean`.
        - Problem id: `Imo2024P1`
        - Namespace: `(none)`
        - Target theorem: `imo2024_p1`

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
