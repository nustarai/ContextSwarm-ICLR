        # PutnamBench Lean 4 Formal Task: `putnam_2025_a4`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

Find the minimal value of $k$ such that there exist $k$-by-$k$ real matrices
$A_1, \ldots, A_{2025}$ with the property that $A_i A_j = A_j A_i$ if and only if
$|i - j| \in \{0, 1, 2024\}$.

        ## Task Surface

        - Baseline file: `baseline/putnam_2025_a4.lean`.
        - Problem id: `putnam_2025_a4`
        - Namespace: `(none)`
        - Target theorem: `putnam_2025_a4`

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
