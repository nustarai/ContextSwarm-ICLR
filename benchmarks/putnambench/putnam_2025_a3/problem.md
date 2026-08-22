        # PutnamBench Lean 4 Formal Task: `putnam_2025_a3`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

Alice and Bob play a game with a string of $n$ digits, each of which is restricted
to be 0, 1, or 2. Initially all the digits are 0. A legal move is to add or subtract 1
from one digit to create a new string that has not appeared before. A player with no
legal move loses, and the other player wins. Alice goes first, and the players alternate
moves. For each $n \ge 1$, determine which player has a strategy that guarantees winning.

        ## Task Surface

        - Baseline file: `baseline/putnam_2025_a3.lean`.
        - Problem id: `putnam_2025_a3`
        - Namespace: `(none)`
        - Target theorem: `putnam_2025_a3`

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
