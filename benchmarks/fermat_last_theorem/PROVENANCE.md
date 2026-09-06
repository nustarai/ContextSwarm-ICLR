# Fermat's Last Theorem statement-only provenance

This experimental bundle uses the exact elementary theorem contract from
`anthropics/fermats-last-theorem` commit
`aa2d8b34692b16c70f699536de0d8e75a3e9ef`.

Only the theorem statement and its Mathlib import are projected into this
repository. The FLT repository's Definitions, P2M proof modules, completed
theorem, compiled `.olean` files, comparator artifacts, and declaration index
are deliberately absent from the Agent-visible bundle. They must not be
mounted into the `formal_flt` Judge environment.

The private Judge environment is pinned to Lean 4.33.1 and Mathlib commit
`db584cd6d46c92f209a44c0f1c829460d327499d`. This task measures strict
from-scratch proof construction; a one-hour run is an infrastructure and
attempt benchmark, not a claim that an Agent can recreate the complete Wiles
proof in one hour.
