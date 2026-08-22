# I. Slot Machine

这是 ICPC World Finals 2025 官方题目的 ContextSwarm config 入口，不是 smoke fixture。

## Problem ID

- `wf2025_i_slot_machine`

## Provenance

- Public AC baseline: https://github.com/openai/openai-icpc-2025-public/blob/main/I-Slot Machine/Submission-1-AC.cpp
- Judge-side package collection: `ContextSwarmJudge/packages/icpc_wf_2025/wf2025_i_slot_machine`
- Judge-side canonical status surfaces:
  - `ContextSwarmJudge/packages/icpc_wf_2025/wf2025_i_slot_machine/problem.yaml`
  - `ContextSwarmJudge/packages/icpc_wf_2025/wf2025_i_slot_machine/validation/report.md`
- Judge verification status: `certified`

## Current Status

- judge 侧已经有官方题面 / provenance / public AC reference 的 package bootstrap。
- judge 侧已补齐 repository-authored interactor、hidden tests 和 package-local evidence，并将该题升级为 `certified`。
- 认证证据保存在 `ContextSwarmJudge/packages/icpc_wf_2025/wf2025_i_slot_machine/validation/evidence/`，不应由 bootstrap 脚本回退到 pending 文案。
