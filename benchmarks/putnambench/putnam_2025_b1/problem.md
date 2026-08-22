        # PutnamBench Lean 4 Formal Task: `putnam_2025_b1`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

Suppose that each point in the plane is colored either red or green, subject to the following
condition: For every three noncollinear points $A$, $B$, $C$ of the same color, the center of the
circle passing through $A$, $B$, $C$ is also this color. Prove that all points of the plane are the
same color.

        ## Task Surface

        - Baseline file: `baseline/putnam_2025_b1.lean`.
        - Problem id: `putnam_2025_b1`
        - Namespace: `(none)`
        - Target theorem: `putnam_2025_b1`

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
