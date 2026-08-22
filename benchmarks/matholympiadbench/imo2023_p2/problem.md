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

        - Follow the mandatory execution and verification contract in the worker prompt.
        - Treat this as offline proof construction. The sole checking exception is
          `judge_check`, the experiment-provided controlled external Judge interface.
        - If the manifest exposes `evaluate.py` or `formal_query`, use them only as bounded
          advisory diagnostics; they never establish official success or select a candidate.
        - Never execute local Lean/lake/elan, install or download Lean/Mathlib/toolchains,
          run a local verifier or proof search, perform resource-heavy computation, or
          start background or parallel processes. Never call raw Judge HTTP endpoints.
        - If `judge_check` is unavailable or overloaded, retry/wait only through that tool
          within budget, or leave the best `result.lean`; do not create a local fallback.
        - The assigned scope includes this task directory and any shared CPS context,
          shared candidate, or helper tool explicitly named by the runner's worker prompt.
          Do not browse any other home, system, runtime, worker, or session artifacts.
        - Edit `result.lean` only within the allowed proof surface described above.
