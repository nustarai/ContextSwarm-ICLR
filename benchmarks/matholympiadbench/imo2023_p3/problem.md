        # MathOlympiadBench Formal Task: `imo2023_p3`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# International Mathematical Olympiad 2023, Problem 3

For each integer k ≥ 2, determine all infinite sequences of positive
integers a₁, a₂, ... for which there exists a polynomial P of the form

  P(x) = xᵏ + cₖ₋₁xᵏ⁻¹ + ... + c₁x + c₀,

where c₀, c₁, ..., cₖ₋₁ are non-negative integers, such that

  P(aₙ) = aₙ₊₁aₙ₊₂⋯aₙ₊ₖ

for every integer n ≥ 1.

        ## Task Surface

        - Baseline file: `baseline/imo2023_p3.lean`.
        - Problem id: `Imo2023P3`
        - Namespace: `(none)`
        - Target theorem: `imo2023_p3`

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
