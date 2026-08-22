# Clever Formal Task: `cleverbench_54_v2`

## Goal

Complete `result.lean` so that the evaluator returns `score = 1.0`.

## Provenance

- Baseline file: `baseline/cleverbench_54_v2.lean`.
- Benchmark source: `Goedel-Code-Prover/benchmark/clever_bench.json`
- Goedel commit: `e9b744b32ad57b7775902597181ae6d42d471bc0`
- Benchmark sha256: `f9c40ba9c1cff83452f0abdd9a23b023b808b7a74f5fe8f0df414e4cf972eb42`
- Problem id: `cleverbench_54_v2`
- Namespace: `cleverbench_54_v2`
- Target theorem: `implementation_postcond_satisfied`

This is semantics revision 2 and supersedes `cleverbench_54`. The Goedel
conversion replaced the upstream inductive Fibonacci relation with an
incorrect executable oracle (`fibComp 2 = 0`). Revision 2 restores the
upstream relation from Clever `problem_55` and uses the standard recurrence.
Results for the old problem id are not comparable.

## Allowed Edits

- Fill in the implementation/proof placeholders needed to prove the target theorem.
- Add helper lemmas or intermediate definitions when they preserve the original specification.

## Forbidden Edits

- Do not weaken or change any target theorem statement.
- Do not change imports, namespace, preconditions, postconditions, or required function signatures.
- Do not introduce `axiom`, `admit`, `unsafe`, or leave any `sorry`.

## Evaluator Contract

- `score = 1.0` means Lean verification passed with no `sorry`.
- `status = "COMPILES_WITH_SORRY"` means the file compiles but still contains incomplete placeholders.
- `status = "CHEATING"` means the theorem signature or axiom usage violated the original contract.
- Lean verifier endpoint is supplied by the resolved experiment config runtime context.

## Verification Workflow

- Prefer `uv run python` with `evaluate.py` for local checks so the worker stays on the session/runtime Python environment.
- Do not run local Lean/Lake discovery, version, direct-file, environment, or build probes; use `evaluate.py` / the HTTP verifier instead.
- Do not probe for local Lean/Lake command-line tools; the intended verification path is the task-local evaluator/verifier feedback.
- Do not search outside the current task directory for Lean/Mathlib internals, including home, system, runtime source, aggregate output, or other worker/session artifact directories. Do not use network requests, external documentation sites, or online Lean/Mathlib search; proof context is limited to the current task directory and evaluator/verifier feedback.
- Avoid repeated long-timeout retries when the verifier already returned the same infra failure.

## Local Check

```bash
python3 evaluate.py
# or, if uv is needed:
uv run python evaluate.py
```

Do not read or print evaluate.py; use it only as the task-local evaluator command.
