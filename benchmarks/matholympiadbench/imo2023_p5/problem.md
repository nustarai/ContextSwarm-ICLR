        # MathOlympiadBench Formal Task: `imo2023_p5`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# International Mathematical Olympiad 2023, Problem 5

Let n be a positive integer. A _Japanese triangle_ is defined as
a set of 1 + 2 + ... + n dots arranged as an equilateral
triangle. Each dot is colored white or red, such that each row
has exactly one red dot.

A _ninja path_ is a sequence of n dots obtained by starting in the
top row (which has length 1), and then at each step going to one of
the dot immediately below the current dot, until the bottom
row is reached.

In terms of n, determine the greatest k such that in each Japanese triangle
there is a ninja path containing at least k red dots.

        ## Task Surface

        - Baseline file: `baseline/imo2023_p5.lean`.
        - Problem id: `Imo2023P5`
        - Namespace: `(none)`
        - Target theorem: `imo2023_p5`

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
