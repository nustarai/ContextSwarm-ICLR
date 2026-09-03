# Agent 提议验证预算：MathOlympiadBench 最终实验报告

日期：2026-09-04

范围：12 道数学奥林匹克题、3600 秒 horizon、CPS/blackboard、`max_parallel=32`

## 结论

本方案值得提交 PR，结论限定为“验证耗时长尾治理有效”：

1. 原版 `judge_check` 的一次验证可能包含两个约 300 秒的 backend attempt，极少数请求因此拖到约 600 秒。
2. 新方案让 Agent 提议一次验证预算，由 broker/evaluator 以同一个绝对 deadline 约束整个逻辑调用；每个 backend attempt 都是 fresh job，timeout 本身不会再自动复制出完整的第二个 300 秒尾部。
3. 三轮正式 treatment 的 `>300 s`、`>600 s` 长尾均为 0；`>120 s` 耗时占比从 50.952% 降至 3.889%。
4. 这足以支持提交实现 PR，但不能据此宣称数学解题质量已经得到因果证明。score 方向较好，但实验规模小、运行不是随机配对，并且存在 recovery、cache、调度和候选轨迹差异。

## 对照结果

baseline 为原始三轮 B0/B1/B2；treatment 为累计预算语义的 r2/r3/r4。`fresh` 指 accepted 且未复用任何 completed/probe/remote cache；tail share 是对应请求耗时总和占全部 fresh elapsed 的比例。

| 指标 | baseline（3 轮） | treatment（3 轮） | 结论 |
|---|---:|---:|---|
| score / 12 | `5,4,5`；均值 `4.667 ± 0.577` | `6,6,5`；均值 `5.667 ± 0.577` | 方向较好，但不是因果结论 |
| nAUC | `0.231024 ± 0.039587` | `0.268013 ± 0.006530` | 方向较好，仍受轨迹混杂 |
| fresh 请求数 | `3,395` | `4,985` | treatment 请求更多，不能直接比较总量 |
| fresh 平均耗时 | `7.112 s` | `4.213 s` | 降低 40.8% |
| fresh P99 | `69.694 s` | `47.988 s` | 极端分位数改善 |
| fresh 最大耗时 | `603.290 s` | `180.865 s` | 降低 70.0% |
| `>60 s` 耗时占比 | `57.191%` | `17.055%` | 降低 40.136 个百分点 |
| `>120 s` 耗时占比 | `50.952%` | `3.889%` | 降低 47.063 个百分点 |
| `>300 s` 耗时占比 | `42.750%` | `0%` | 目标长尾消失 |
| `>600 s` 耗时占比 | `34.937%` | `0%` | 双 attempt 尾部消失 |
| backend jobs / execution work | `5,372 / 25,446.181 s` | `6,601 / 20,220.548 s` | 更多 job 仍使用更少 execution work；不作纯因果归因 |

最直接的效果是：baseline 中超过 300 秒的请求只占 fresh 请求约 0.56%，却消耗 42.75% 的 fresh Judge 时间；treatment 中超过 300 秒和 600 秒的耗时占比均归零。

## 配置修正后的确认轮

这一轮专门确认 prompt 不再写死 300 秒，而是读取实际 manifest cap。配置为 cap=`300 s`，一小时、CPS32；结果如下：

| 指标 | confirmation |
|---|---:|
| score / nAUC / first proof | `5/12` / `0.302987` / `147.579 s` |
| fresh Judge rows | `2,116` |
| fresh mean / P99 / max | `4.879 s` / `60.386 s` / `180.391 s` |
| `>120 s` 耗时占比 | `4.084%` |
| `>300 s` / `>600 s` | `0 / 0` |
| explicit timeout adoption | `2,116/2,116 = 100%` |
| omitted / clamped | `0 / 0` |
| natural retry count | `0` |

这轮说明配置驱动的 prompt/tool 路径实际生效，且长尾仍受硬 cap 约束。它没有自然触发 transient retry，因此不能把该轮 score 解释成 retry 带来的质量变化。该正式 workload 使用 primary-fix commit `57b115e` 启动；之后的 helper transport 和 nested metadata hardening 已在 PR 中补上，最终 PR head 另以 exact-image mock smoke 验证。

## Retry 语义

`timeout_seconds` 是一次逻辑验证调用的累计总预算，不是每个 backend job 各自拥有的预算。

- 第一次 job 若在 30 秒因 candidate-independent transport/runtime 异常结束，第二个 fresh job 只拿绝对 deadline 剩余的约 270 秒。
- 第一次已经耗尽 300 秒，或返回确定性 verdict、timeout/cancellation，则不再自动 replay。
- 每个 fresh job 都发送 `max_retries=0`；retry 由 evaluator 外层按剩余预算管理。
- retry 仍停留在同一个 broker handler、evaluator gate 和 Agent/Pi session 中，不回到 CPS allocator，也不创建新的 Agent/Pi session。
- 远端取消/结算可能有短暂 cleanup grace，但不会增加验证预算本身。

因此这不是通过降低 retry 次数掩盖真实耗时，而是保证所有 retry 的总和不能超过同一个逻辑预算。

## 配置与实现

`[judge].timeout_seconds` 是 Agent timeout cap 的单一来源；缺失时回退到 `[lean].timeout_seconds`，默认值仍为 300 秒。prompt 和工具建议按实际 cap 的比例生成，而不是固定写 300：

| 配置 cap | Agent 范围 | routine 建议 | heavy 建议 |
|---:|---:|---:|---:|
| `600 s` | `5–600 s` | `60–120 s` | `240–360 s` |
| `300 s` | `5–300 s` | `30–60 s` | `120–180 s` |
| `60 s` | `5–60 s` | `6–12 s` | `24–36 s` |
| `3 s` | `3–3 s` | 按 cap 舍入 | 按 cap 舍入 |

实现分层如下：

- [timeout_policy.py:54](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/adaptive-timeout-20260903/contextswarm_mini/timeout_policy.py:54)：统一计算实际 worker-facing bounds 和 clamp。
- [prompts.py:188](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/adaptive-timeout-20260903/contextswarm_mini/prompts.py:188)：按 cap 生成范围、比例和使用建议。
- [config.py:1045](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/adaptive-timeout-20260903/contextswarm_mini/config.py:1045)：读取 `[judge]` / `[lean]` 配置，并把 formal helper 外层 timeout 至少设为 cap+120 秒。
- [evaluator.py:1740](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/adaptive-timeout-20260903/contextswarm_mini/evaluator.py:1740)：实现累计 deadline、fresh retry 和 terminal verdict 边界。
- [judge_broker.py:314](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/adaptive-timeout-20260903/contextswarm_mini/judge_broker.py:314)：执行 broker 侧硬上限和有界审计字段。
- [pi_solver_tools.mjs:117](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/adaptive-timeout-20260903/contextswarm_mini/pi_solver_tools.mjs:117)：让 Pi tool schema/说明读取同一 cap。

baseline 默认关闭 capability；未提供显式 timeout 时保留 legacy timeout/retry 合同。`formal_query` 仍是 legacy 查询，不在本次能力范围内。

## 验证与交付

- timeout/formal focused tests：`134 passed, 1 skipped`。
- merge 后 adaptive-timeout focused tests：`27 passed`。
- launch contract tests：`3 passed`。
- `compileall`、`node --check`、`git diff --check`：全部通过。
- 完整 unittest discovery：`796 tests, OK (skipped=1)`；未抑制告警时出现过两个极短 horizon 的环境性时序 flake，单测重复通过，最终完整复跑未重现。
- 最终 PR-head exact image mock smoke：`COMPLETED`，source revision 与 `21f9fd9` 一致，broker closeout 为 `active_handlers=0 / drained=true / remote_unsettled_jobs=0`。该 smoke 只证明当前 head 的生命周期和 provenance，不是数学质量实验。

所有正式 treatment 和 confirmation run 的最终状态都是 `DEGRADED`；profiling audit 因 dropped fields/一个未闭合 span 返回 exit 1，但未发现敏感字段，不能把它们称为 clean production benchmark。

## 交付状态与后续

PR：[Make Agent validation budgets config-driven and cumulative](https://github.com/nustarai/ContextSwarm-ICLR/pull/50)

- base：`main`
- head：`adaptive-timeout-20260903`，`21f9fd942b0b18225f4dbb66baf711ecd602c367`
- 状态：`OPEN`，`MERGEABLE`，`CLEAN`
- 未执行 merge、release 或部署。

当前证据足以支持合并评审。若要继续提高结论强度，优先级最高的是两项独立验证：

1. 同 source、cache、recovery 和 Judge 合同下的 matched flag-off control，用于估计质量和工作量的可比效应量；
2. 一次小型 transient-fault injection，直接断言 `30 s → 剩余约 270 s` 且总验证预算不超过 `300 s`。

## 证据索引

- [详细实验记录与逐轮数据](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/adaptive-timeout-20260903/docs/adaptive_timeout_experiment_20260903_details.md:1)
- [confirmation final.json](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/adaptive-timeout-20260903/runs/adaptive-timeout-20260904-confirm/20260903T205925Z-12d09a89/final.json:1)
- [最终 PR-head mock run_meta.json](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/adaptive-timeout-20260903/runs/adaptive-timeout-final-pr-image-smoke/20260903T223542Z-66db6269/run_meta.json:1)
- [PR 最新证据 comment](https://github.com/nustarai/ContextSwarm-ICLR/pull/50#issuecomment-5533040155)
