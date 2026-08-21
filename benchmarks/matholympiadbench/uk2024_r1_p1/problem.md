        # MathOlympiadBench Formal Task: `uk2024_r1_p1`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# British Mathematical Olympiad 2024, Round 1, Problem 1

An unreliable typist can guarantee that when they try to type a word with
different letters, every letter of the word will appear exactly once in what
they type, and each letter will occur at most one letter late (though it may
occur more than one letter early). Thus, when trying to type MATHS, the
typist may type MATHS, MTAHS or TMASH, but not ATMSH.

Determine, with proof, the number of possible spellings of OLYMPIADS
that might be typed.

        ## Task Surface

        - Baseline file: `baseline/uk2024_r1_p1.lean`.
        - Problem id: `UK2024R1P1`
        - Namespace: `(none)`
        - Target theorem: `uk2024_r1_p1`

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
