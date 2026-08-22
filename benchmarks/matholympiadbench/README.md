# MathOlympiadBench latest12 integrity v2

This directory contains the twelve paper-facing task definitions copied from
the upstream ContextSwarm snapshot pinned by `manifest.json`. Only public problem
statements, metadata, and baseline Lean skeletons are retained. Candidate
`result.lean` files are created under a run output directory, never in this source
tree.

`imo2023_p2_v2` supersedes the semantically invalid `imo2023_p2`; results from the
two contracts must not be combined. `PROVENANCE.md` and
`benchmark_integrity.json` record that boundary and the other curated repairs.
The Work Mode text is localized to the mini runtime's controlled external Judge
contract without changing any Lean theorem bytes.
