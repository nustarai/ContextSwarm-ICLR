        # MathOlympiadBench Formal Task: `imo2023_p2`

        ## Goal

        Complete `result.lean` while preserving the original task contract.

## Source Statement

# International Mathematical Olympiad 2023, Problem 2

Let ABC be an acute-angled triangle with AB < AC.
Let Ω be the circumcircle of ABC.
Let S be the midpoint of the arc CB of Ω containing A.
The perpendicular from A to BC meets BS at D and meets Ω again at E ≠ A.
The line through D parallel to BC meets line BE at L.
Denote the circumcircle of triangle BDL by ω.
Let ω meet Ω again at P ≠ B.
Prove that the line tangent to ω at P meets line BS on the internal angle bisector of ∠BAC.

        ## Task Surface

        - Baseline file: `baseline/imo2023_p2.lean`.
        - Problem id: `Imo2023P2`
        - Namespace: `(none)`
        - Target theorem: `imo2023_p1`

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
