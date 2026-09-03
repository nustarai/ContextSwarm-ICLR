# Agent 提议验证超时：MathOlympiadBench 对照实验记录

> **实验目的**：验证让 Agent 为 `judge_check` 和 `evaluate_local` 提议一次验证预算，
> 同时由 broker/evaluator 做硬上限裁剪，是否能缓解少量验证长尾占用大量总时长的问题。
>
> **记录状态**：实现与一轮 treatment run 的结果在本文件中逐步补齐；在没有 run 产物
> 前，任何“节省”或“提升”都只是待验证假设。

## 1. 问题与假设

此前的 MathOlympiadBench 12 题、一小时、CPS/blackboard、并发 32 实验显示，
`judge_check` 的 fresh accepted 请求大多数很快结束，但极少数请求会接近两个
300 秒 backend attempt 的总尾部。这个实验不把“较短等待”直接等同于“验证失败”，而是
测试以下四个可观测假设：

1. Agent 会在 treatment arm 中实际为大部分验证调用提供 `timeout_seconds`（或
   `evaluate.py --timeout N`），并能根据候选复杂度选择不同档位。
2. 在相同的一小时 horizon 下，`judge_check` 的 fresh elapsed 长尾（尤其是 >60 s、
   >120 s、>300 s）和累计等待时间下降。
3. `PROVED` 数量、最终 score 和候选反馈质量不会因为过早放弃而明显下降；
   `EXECUTION_TIMEOUT` 仍被视为不确定反馈，而不是 `VERIFY_FAIL`。
4. 取消、远端结算、gate release 和 closeout/drain 保持完整，不会以减少前台等待为代价
   留下 `remote_unsettled_jobs` 或隐藏的 backend 工作。

这是一轮启发式 Agent 行为实验，不是固定随机种子的算法因果证明。只有在 run 的 source、
image、manifest、Judge health、closeout 和 profiling 证据都齐全时，才把数值用于下一轮
设计。

## 2. 历史基线（只读）

### 2.1 主参考 run

主参考采用已有报告中的 `20260901T012227Z-8c90d3f0`（配置名
`mob-formal-1h-cps32-profiled`，源码 `33296b07634c708412326c2808d5782dab3f788e`）。
它与本 treatment 共享 12 题、CPS/blackboard、3600 s horizon、`max_parallel=32`、
Judge/evaluator gate 32 和 profiling 目标；差异是本次新增的 opt-in Agent timeout
能力。该历史 run 的最终健康标记为 `DEGRADED`，所以它适合作为 Judge 成本画像，不能单独
作为无故障算法 baseline。

`judge_checks.jsonl` 的主要口径如下（`fresh accepted` 排除 completed-cache reuse）：

| 指标 | 历史值 |
|---|---:|
| 全部 `judge_check` 记录 | 1,499 |
| accepted / rejected | 1,441 / 58 |
| fresh accepted | 1,252 |
| completed-cache reused | 189 |
| fresh elapsed 平均 / 中位数 | 9.891936 s / 1.274114 s |
| fresh elapsed P90 / P95 / P99 | 6.293134 s / 20.205775 s / 279.994298 s |
| fresh elapsed 最大 | 603.289839 s |
| fresh `EXECUTION_TIMEOUT` | 9 |

| fresh elapsed 阈值 | 请求数 | 累计耗时 | 占 fresh elapsed |
|---|---:|---:|---:|
| >60 s | 25 | 8,502.0 s | 68.650% |
| >120 s | 17 | 7,897.3 s | 63.767% |
| >300 s | 13 | 7,008.4 s | 56.589% |
| >600 s | 9 | 5,423.5 s | 43.792% |

历史长尾的直接机制是 backend 单次 hard timeout 300 s 且 `max_retries=1`，因此一次
超时后的第二次 attempt 会产生约 600 s 的端到端尾部。它不是单纯的全局排队问题：
backend active job 平均约 3.98、P95 9、峰值 19，queue depth 中位数 1、峰值 3。
题目异质性也很明显：例如 `imo2023_p3` 普通请求的中位数约 1.274 s 但有约 603 s
timeout；`imo2023_p2_v2` 的非 timeout P95 已约 46.491 s；`imo2023_p4` 整体中位数
约 21.413 s。因此不能把全局固定 60 s 当作所有题目的安全硬上限。

历史数据还给出两个保护性结论：四次 early proof 的耗时约为 5.04、12.61、12.61、
41.77 s，合法 proof 不能简单地被 60 s 硬截断；而 `EXECUTION_TIMEOUT`、
`RESOURCE_LIMIT`、取消和基础设施错误必须与候选 `VERIFY_FAIL` 分层统计。

### 2.2 可用的同类控制样本

工作区中还保留了若干同一 benchmark 家族的历史 run（例如 20260902 的 baseline 和
recovery/no-timeout arms）。它们的 Agent 轨迹、accepted 数量和健康标记不同，不能直接
平均成一个“基线均值”。本报告只把上面的 run 作为主参考，并在结果表中同时列出本次
treatment 的完整分母；若要做正式效应量，应再按同一 source/config/Judge 合同补跑
matched control。

## 3. Treatment 合同

### 3.1 能力开关与范围

- treatment manifest：`configs/formal_1h_cps32_profiled_adaptive_timeout.toml`；只在
  `[judge] agent_timeout_enabled = true` 打开能力，其他继承配置不变。
- baseline manifest 默认 `false`。关闭时工具 schema、prompt 和 broker response
  保持历史表面；旧 run 不会因为新增字段而改变分母。
- `judge_check` 请求字段：可选整数 `timeout_seconds`。
- `evaluate_local`：可选 `python3 evaluate.py --timeout N`，由同一 broker 字段承载。
- 广告范围为 **5–300 秒**。broker 在 capability 边界校验类型并记录原值；超出范围
  进行裁剪，malformed 值拒绝。evaluator 再按自身配置的 timeout ceiling 做第二次
  防御性裁剪。
- receipt、audit 和 profiling 记录 `requested_timeout_seconds`、
  `effective_timeout_seconds`、`timeout_clamped`、`timeout_source`。这四个字段只记录
  有界策略元数据，不记录 prompt、候选源码、token 或原始 Judge response。

### 3.2 时钟和 retry 语义

`timeout_seconds` 是一次 backend verification attempt 的执行预算，不是 Judge admission
等待、HTTP transport、Pi/provider timeout，也不是一小时 horizon 的改写。Agent 明确给出
预算时，本次 formal submission 使用 `max_retries=0`，避免一个名义 60 s 的建议再次隐式
扩张为约 120 s 的 backend retry 尾部；省略字段则保留历史 evaluator timeout 和
`max_retries=1`，便于审计“Agent 选择了 legacy 行为”的比例。

超时结果仍然是 `EXECUTION_TIMEOUT`/不确定反馈，不改写为 `VERIFY_FAIL`，也不开放本地
checker 或 raw Judge access。固定的 run horizon、candidate budget、session probe quota、
closeout evaluation 和 remote settlement 语义不变。

### 3.3 Prompt 引导（实验 treatment）

启用能力的动态 prompt 和工具描述建议 Agent：

- routine incremental check 约 30–60 s；
- 有重 import/elaboration/resource 风险、但候选很有希望时 120–180 s；
- 只有已接近完整且已知偏慢时才用 300 s；
- 5–15 s 只适合明显小改动后的廉价 sanity check，不适合首个 checkpoint 或刚改大定义。

Prompt 明确说明：值会被 runner clamp，生效值会在 receipt 返回；超时不是错误证明；超时
后应检查反馈并做实质修改或保留最佳候选，再决定是否重试。为了让 treatment 有可测的
adoption，prompt 默认要求正常验证调用显式给值；只有刻意测试 legacy fallback 时才省略。

静态 benchmark `problem.md` 不被写入这段实验提示，避免同步脚本和历史题面发生漂移；
提示只在 run 生成的动态 task/mono prompt 中出现。

## 4. 实验协议

1. 从本 worktree 的单一 commit 构建带 revision label 的镜像；正式 launcher 拒绝 dirty
   tracked tree，并在容器内再次校验 source/manifest/image 绑定。
2. 只使用 operator 注入的 Judge URL、cache-health capability、NuRouter binary/node
   config 和 revision-matched declaration index；这些值不写入 manifest、日志或本文。
3. treatment 使用自然 3600 s horizon、CPS/blackboard、并发 32 和 profiling；让 Agent
   正常结束，保留 closeout/drain，不人为提前杀 run。
4. 结果解析统一以 `judge_checks.jsonl` 的 `accepted`、`fresh`、status 和
   `elapsed_seconds` 为主；并独立读取 `profiling.jsonl` 的 evaluator/backend/queue
   层，避免把嵌套 span 重复相加。
5. 同时检查：timeout adoption、请求/生效值分布、clamp 数、>60/>120/>300/>600 尾部、
   fresh cumulative elapsed、backend job 数和 execution work-seconds、score/proof 数、
   `remote_unsettled_jobs`、closeout active handlers/FIFO/drain、profiling audit 结果。

### 指标定义

- **fresh**：`accepted == true` 且不是 `cache_reused`/`probe_cache_reused`；completed-cache
  命中单独报告。
- **tail share**：阈值以上 fresh `elapsed_seconds` 的累计值除以全部 fresh elapsed；这是
  Judge-facing wall/work 的描述，不等于 backend CPU 节省。
- **adoption**：treatment 中 fresh accepted 调用携带非空 Agent timeout 的比例；另报
  omitted legacy、clamped、按值档位和按题目分布。
- **安全结果**：任何 `remote_unsettled_jobs > 0`、未配对 lifecycle span、异常 closeout
  或 profiling audit error 都会把“只降低前台等待”的解释标为不成立。

## 5. 本轮结果（待填）

下表在 treatment 自然结束后填写；空白不代表零。

| 指标 | 主历史参考 | treatment 本轮 | 变化/解释 |
|---|---:|---:|---|
| run ID / source commit | `20260901T012227Z-8c90d3f0` / `33296b0…` | 待填 | 必须 exact readback |
| final status / score | `DEGRADED` / 5 | 待填 | 健康与算法分开 |
| all / accepted / rejected | 1,499 / 1,441 / 58 | 待填 | 分母不能只看成功 |
| fresh accepted | 1,252 | 待填 | 排除 completed-cache |
| timeout adoption | 不适用 | 待填 | explicit / omitted |
| requested→effective 分布 | 不适用 | 待填 | 含 clamp |
| fresh elapsed median / P95 / P99 / max | 1.274 / 20.206 / 279.994 / 603.290 s | 待填 | 同一口径 |
| fresh >60 s：n / 累计 / share | 25 / 8,502.0 s / 68.650% | 待填 | 主要 tail 指标 |
| fresh >120 s：n / 累计 / share | 17 / 7,897.3 s / 63.767% | 待填 | |
| fresh >300 s：n / 累计 / share | 13 / 7,008.4 s / 56.589% | 待填 | |
| fresh >600 s：n / 累计 / share | 9 / 5,423.5 s / 43.792% | 待填 | |
| `EXECUTION_TIMEOUT` / `RESOURCE_LIMIT` | 9 / 3 | 待填 | 不当作 VERIFY_FAIL |
| backend jobs / execution work-seconds | 历史报告口径 | 待填 | 与前台 elapsed 分开 |
| proof 数 / final score | 5 / 5（历史 closeout） | 待填 | score 需健康上下文 |
| remote unsettled / closeout | 0 / 正常 drain | 待填 | 安全门槛 |

## 6. 解释与决策门槛

### 可以支持“方案有希望”的条件

- adoption 足够高（若接近 0，应判为 prompt/tool adoption 失败，而不是 timeout 策略失败）；
- >60 s 与 >300 s tail share、fresh cumulative elapsed 和/或 backend work 明显下降；
- score/proof 没有明显下降，且没有把 timeout 错报成 `VERIFY_FAIL`；
- closeout、remote settlement、profiling sequence/span/privacy audit 均通过。

### 需要谨慎或停止扩展的信号

- Agent 普遍选择 5–15 s，导致合法 proof 或高复杂度题目过早丢失；
- 长尾下降只来自大量未结算/后台继续运行的 job，backend work-seconds 不降；
- timeout metadata 与实际 Judge payload 不一致，或有大量 clamp/invalid request；
- treatment 与历史 run 的 route、Mathlib/Judge revision、cache policy、模型或 source
  不同，无法做 matched comparison；
- run 以 `DEGRADED`、未结算 job、worker process failure 或 missing profiling 结束。

本轮只有初步结果时，建议下一步优先按同一 source/模型/Judge 合同补一个 matched fixed-
timeout control，再决定是否做 2–3 轮 treatment。若 adoption 高、tail 与 backend work 都
下降且 score 稳定，才值得研究更细的 task/history-aware 档位；不应直接把 60 s 固定成
全局 hard cutoff。

## 7. 证据边界

- 本文的历史数值来自既有只读 run 报告；本轮数值必须从新的 run 目录和 commit-bound
  image readback 得到。
- profiler 的 nested/parallel spans 不相加；Judge-facing elapsed、backend execution、
  queue/admission 和 Agent/Pi wall 是不同分母。
- source/commit、image、run artifact、Judge health、installation/deployment 和 live
  runtime 是独立事实；本文不把源码或镜像构建当成部署成功。
- 不记录私有 endpoint、Admin token、邮箱密码、auth JSON、raw candidate、raw model
  response 或其他 owner-only 输入。
