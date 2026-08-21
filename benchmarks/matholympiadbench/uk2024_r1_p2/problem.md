        # MathOlympiadBench Formal Task: `uk2024_r1_p2`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# British Mathematical Olympiad 2024, Round 1, Problem 2

The sequence of integers a₀, a₁, ⋯ has the property that for each
i ≥ 2, aᵢ is either 2 * aᵢ₋₁ - aᵢ₋₂, or 2 * aᵢ₋₂ - aᵢ₋₁.

Given that a₂₀₂₃ and a₂₀₂₄ are consecutive integers, prove that a₀
and a₁ are consecutive.

        ## Task Surface

        - Baseline file: `baseline/uk2024_r1_p2.lean`.
        - Problem id: `UK2024R1P2`
        - Namespace: `(none)`
        - Target theorem: `uk2024_r1_p2`

        ## Allowed Edits

        - Fill in the implementation, answer placeholders, or proof placeholders needed to prove the target theorem.
        - Add helper lemmas or intermediate definitions when they preserve the original specification.

        ## Forbidden Edits

        - Do not weaken or change any target theorem statement.
        - Do not change imports, namespace, preconditions, postconditions, or required function signatures.
        - Do not introduce `axiom`, `admit`, `unsafe`, or leave any `sorry`.

        ## Work Mode

        - Treat this as an offline proof-construction task.
        - Edit `result.lean` only within the allowed proof surface described above.
        - Use static Lean reasoning and edit only the allowed proof surface.
        - Do not search outside the current task directory for Lean/Mathlib internals, including home, system, runtime source, aggregate output, or other worker/session artifact directories. Do not use network requests, external documentation sites, or online Lean/Mathlib search; proof context is limited to the current task directory and evaluator/verifier feedback.
