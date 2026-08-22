# Verina Formal Task: `verina_advanced_67`

## Goal

Complete the Lean proof in `result.lean` so that the evaluator returns `score = 1.0`.

## Provenance

- Baseline file: `baseline/verina_advanced_67.lean`.
- Benchmark source: `Goedel-Code-Prover/benchmark/verina_bench.json`
- Original problem id: `verina/verina_advanced_67.lean`
- Subset: `advanced`
- Namespace: `verina_advanced_67`
- Target theorem: `runLengthEncode_spec_satisfied`

## Allowed Edits

- Primary target is the proof body of the final theorem.
- You may add local helper lemmas or intermediate proof terms if they are genuinely needed.

## Forbidden Edits

- Do not weaken or change the theorem statement.
- Do not modify imports, namespace, preconditions, postconditions, or `task_code`.
- Do not introduce `axiom`, `admit`, or leave any `sorry`.

## Evaluator Contract

- `score = 1.0` means Lean verification passed with no `sorry`.
- `status = "COMPILES_WITH_SORRY"` means the file compiles but still contains incomplete proof placeholders.
- `status = "CHEATING"` means the theorem signature or axiom usage violated the original contract.
- Lean verifier endpoint is supplied by the resolved experiment config runtime context.

## Verification Workflow

- Default to `evaluate.py` for local checks.
- Do not run local Lean/Lake discovery, version, direct-file, environment, or build probes; use `evaluate.py` / the HTTP verifier instead.
- Do not probe for local Lean/Lake command-line tools; the intended verification path is the task-local evaluator/verifier feedback.
- Do not search outside the current task directory for Lean/Mathlib internals, including home, system, runtime source, aggregate output, or other worker/session artifact directories. Do not use network requests, external documentation sites, or online Lean/Mathlib search; proof context is limited to the current task directory and evaluator/verifier feedback.
- Avoid repeated long-timeout retries when the verifier already returned the same infra failure. Change approach, shorten the check, or go back to proof construction.

## Local Check

```bash
python3 evaluate.py
# or, if uv is needed:
uv run python evaluate.py
```

Do not read or print evaluate.py; use it only as the task-local evaluator command.

- Final scoring comes from the official evaluator, which checks Lean verification, theorem signature, and axiom usage.
