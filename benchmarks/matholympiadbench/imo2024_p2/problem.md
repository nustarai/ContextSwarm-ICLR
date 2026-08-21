        # MathOlympiadBench Formal Task: `imo2024_p2`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# International Mathematical Olympiad 2024, Problem 2

Determine all pairs (a,b) of positive integers for which there exist positive integers
g and N such that

   gcd(aⁿ + b, bⁿ + a),   n = 1, 2, ...

holds for all integers n ≥ N.

        ## Task Surface

        - Baseline file: `baseline/imo2024_p2.lean`.
        - Problem id: `Imo2024P2`
        - Namespace: `(none)`
        - Target theorem: `imo2024_p2`

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
