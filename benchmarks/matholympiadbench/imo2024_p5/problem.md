        # MathOlympiadBench Formal Task: `imo2024_p5`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# International Mathematical Olympiad 2024, Problem 5

Turbo the snail plays a game on a board with $2024$ rows and $2023$ columns. There are hidden
monsters in $2022$ of the cells. Initially, Turbo does not know where any of the monsters are,
but he knows that there is exactly one monster in each row except the first row and the last
row, and that each column contains at most one monster.

Turbo makes a series of attempts to go from the first row to the last row. On each attempt,
he chooses to start on any cell in the first row, then repeatedly moves to an adjacent cell
sharing a common side. (He is allowed to return to a previously visited cell.) If he reaches a
cell with a monster, his attempt ends and he is transported back to the first row to start a
new attempt. The monsters do not move, and Turbo remembers whether or not each cell he has
visited contains a monster. If he reaches any cell in the last row, his attempt ends and the
game is over.

Determine the minimum value of $n$ for which Turbo has a strategy that guarantees reaching
the last row on the $n$th attempt or earlier, regardless of the locations of the monsters.

        ## Task Surface

        - Baseline file: `baseline/imo2024_p5.lean`.
        - Problem id: `Imo2024P5`
        - Namespace: `(none)`
        - Target theorem: `imo2024_p5`

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
