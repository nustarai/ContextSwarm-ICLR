        # MathOlympiadBench Formal Task: `imo2024_p6`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# International Mathematical Olympiad 2024, Problem 6

A function `f: ℚ → ℚ` is called *aquaesulian* if the following
property holds: for every `x, y ∈ ℚ`,
`f(x + f(y)) = f(x) + y` or `f(f(x) + y) = x + f(y)`.

Show that there exists an integer `c` such that for any aquaesulian function `f`
there are at most `c` different rational numbers of the form `f(r)+f(-r)` for
some rational number `r`, and find the smallest possible value of `c`.

        ## Task Surface

        - Baseline file: `baseline/imo2024_p6.lean`.
        - Problem id: `Imo2024P6`
        - Namespace: `(none)`
        - Target theorem: `imo2024_p6`

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
