        # PutnamBench Lean 4 Formal Task: `putnam_2025_a1`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

Let $m_0$ and $n_0$ be distinct positive integers. For every positive integer $k$,
define $m_k$ and $n_k$ to be the relatively prime positive integers such that
$$\frac{m_k}{n_k} = \frac{2m_{k-1} + 1}{2n_{k-1} + 1}.$$
Prove that $2m_k + 1$ and $2n_k + 1$ are relatively prime for all but finitely many
positive integers $k$.

        ## Task Surface

        - Baseline file: `baseline/putnam_2025_a1.lean`.
        - Problem id: `putnam_2025_a1`
        - Namespace: `(none)`
        - Target theorem: `putnam_2025_a1`

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
