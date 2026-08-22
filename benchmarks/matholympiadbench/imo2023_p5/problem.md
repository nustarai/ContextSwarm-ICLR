        # MathOlympiadBench Formal Task: `imo2023_p5`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# International Mathematical Olympiad 2023, Problem 5

Let n be a positive integer. A _Japanese triangle_ is defined as
a set of 1 + 2 + ... + n dots arranged as an equilateral
triangle. Each dot is colored white or red, such that each row
has exactly one red dot.

A _ninja path_ is a sequence of n dots obtained by starting in the
top row (which has length 1), and then at each step going to one of
the dot immediately below the current dot, until the bottom
row is reached.

In terms of n, determine the greatest k such that in each Japanese triangle
there is a ninja path containing at least k red dots.

## Formalization Revision

This task is a documented repaired fork of the original MOBench row. ContextSwarm
commit `5ce40d200affbb9e9d432da1a49d565a1c5ab7b0` made the natural-language
positive-integer domain explicit with `0 < n`. Current Compfiles instead handles
`n = 0` by totalizing the answer, so these are two valid but distinct repairs.
Proof artifacts for the pre-correction theorem header must not be reused here.

        ## Task Surface

        - Baseline file: `baseline/imo2023_p5.lean`.
        - Problem id: `Imo2023P5`
        - Namespace: `(none)`
        - Target theorem: `imo2023_p5`

        ## Allowed Edits

        - Fill in the implementation, answer placeholders, or proof placeholders needed to prove the target theorem.
        - Add helper lemmas or intermediate definitions when they preserve the original specification.

        ## Forbidden Edits

        - Do not weaken or change any target theorem statement.
        - Do not change imports, namespace, preconditions, postconditions, or required function signatures.
        - Do not introduce `axiom`, `admit`, `unsafe`, or leave any `sorry`.

        ## Work Mode

        - Follow the mandatory execution and verification contract in the worker prompt.
        - Treat this as offline proof construction. All Lean execution belongs to the
          controlled external Judge; `judge_check` is the sole authoritative interface.
        - If the manifest exposes `evaluate.py` or `formal_query`, use them only as bounded
          remote Judge diagnostics; they never run Lean locally, establish official success,
          or select a candidate. The runner injects and owns their Judge capability and URL.
        - The Judge already provides Lean/Mathlib downloads, compilation, tests, and
          verification; submit those operations through the runner-controlled interfaces
          instead of doing them in the local worker environment.
        - Never execute local Lean/lake/elan, install or download Lean/Mathlib/toolchains,
          run a local verifier or proof search, perform resource-heavy computation, or
          start background or parallel processes. Never call raw Judge HTTP endpoints.
        - If `judge_check` is unavailable or overloaded, retry/wait only through that tool
          within budget, or leave the best `result.lean`; do not create a local fallback.
        - The assigned scope includes this task directory and any shared CPS context,
          shared candidate, or helper tool explicitly named by the runner's worker prompt.
          Do not browse any other home, system, runtime, worker, or session artifacts.
        - Edit `result.lean` only within the allowed proof surface described above.
