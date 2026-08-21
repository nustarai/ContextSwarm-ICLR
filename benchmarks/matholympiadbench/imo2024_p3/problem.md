        # MathOlympiadBench Formal Task: `imo2024_p3`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# International Mathematical Olympiad 2024, Problem 3

Let a₁, a₂, a₃, ... be an infinite sequence of positive integers,
and let N be a positive integer. Suppose that, for each n > N,
aₙ is equal to the number of times aₙ₋₁ appears in the list
a₁, a₂, ..., aₙ₋₁.

Prove that at least one of the sequences a₁, a₃, a₅, ... and
a₂, a₄, a₆, ... is eventually periodic.

        ## Task Surface

        - Baseline file: `baseline/imo2024_p3.lean`.
        - Problem id: `Imo2024P3`
        - Namespace: `(none)`
        - Target theorem: `imo2024_p3`

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
