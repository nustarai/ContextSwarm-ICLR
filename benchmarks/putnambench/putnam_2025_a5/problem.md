        # PutnamBench Lean 4 Formal Task: `putnam_2025_a5`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

Let $n$ be an integer with $n \ge 2$. For a sequence $s = (s_1, \ldots, s_{n-1})$ where each
$s_i = \pm 1$, let $f(s)$ be the number of permutations $(a_1, \ldots, a_n)$ of $\{1, 2, \ldots, n\}$
such that $s_i(a_{i+1} - a_i) > 0$ for all $i$. For each $n$, determine the sequences $s$ for which
$f(s)$ is maximal.

        ## Task Surface

        - Baseline file: `baseline/putnam_2025_a5.lean`.
        - Problem id: `putnam_2025_a5`
        - Namespace: `(none)`
        - Target theorem: `putnam_2025_a5`

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
