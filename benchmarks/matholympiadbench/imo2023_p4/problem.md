        # MathOlympiadBench Formal Task: `imo2023_p4`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# International Mathematical Olympiad 2023, Problem 4

Let x₁, x₂, ... x₂₀₂₃ be distinct positive real numbers.
Define

   aₙ := √((x₁ + x₂ + ... + xₙ)(1/x₁ + 1/x₂ + ... + 1/xₙ)).

Suppose that aₙ is an integer for all n ∈ {1,...,2023}.
Prove that 3034 ≤ a₂₀₂₃.

        ## Task Surface

        - Baseline file: `baseline/imo2023_p4.lean`.
        - Problem id: `Imo2023P4`
        - Namespace: `(none)`
        - Target theorem: `imo2023_p4`

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
