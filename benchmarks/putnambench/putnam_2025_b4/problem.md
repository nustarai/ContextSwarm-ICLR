        # PutnamBench Lean 4 Formal Task: `putnam_2025_b4`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

For $n \geq 2$, let $A = [a_{i,j}]_{i,j=1}^n$ be an $n$-by-$n$ matrix of nonnegative integers such that:
(a) $a_{i,j} = 0$ when $i + j \leq n$;
(b) $a_{i+1,j} \in \{a_{i,j}, a_{i,j} + 1\}$ when $1 \leq i \leq n-1$ and $1 \leq j \leq n$; and
(c) $a_{i,j+1} \in \{a_{i,j}, a_{i,j} + 1\}$ when $1 \leq i \leq n$ and $1 \leq j \leq n-1$.

Let $S$ be the sum of the entries of $A$, and let $N$ be the number of nonzero entries of $A$.
Prove that $S \leq \frac{(n+2)N}{3}$.

        ## Task Surface

        - Baseline file: `baseline/putnam_2025_b4.lean`.
        - Problem id: `putnam_2025_b4`
        - Namespace: `(none)`
        - Target theorem: `putnam_2025_b4`

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
