# Fermat's Last Theorem in Lean 4

## Goal

Complete `result.lean` with a proof of the theorem below. The Judge accepts a
candidate only when Lean checks it without `sorry`, `admit`, or proof-bypass
constructs.

## Theorem contract

For every natural exponent `n >= 3`, there are no positive natural numbers
`a`, `b`, and `c` satisfying `a ^ n + b ^ n = c ^ n`.

The exact Lean declaration, including all binders and hypotheses, is in the
baseline file. The task is intentionally statement-only: no FLT-specific
definitions, lemmas, completed proof, `.olean` files, comparator output, or
declaration index are exposed to the Agent.

## Allowed work surface

The public Lean environment is standard Mathlib at the pinned `formal_flt`
revision. The controlled Judge owns compilation and verification. Agents must
submit their candidate through the runner's `judge_check` interface.
