# MathOlympiadBench 一小时运行中的 Judge 延迟、资源与提前终止决策分析

> **分析日期**：2026-09-05
>
> **报告修订**：`judge-latency-early-termination-20260907`
>
> **实验快照**：`20260901T012227Z-8c90d3f0`，12 道 MathOlympiadBench 题，CPS/blackboard，配置 horizon 3600 s
>
> **实际执行源码**：`33296b07634c708412326c2808d5782dab3f788e`；NuRouter/AISW runtime 与 Judge image 见“具体的实验”
>
> **文档性质**：一次真实、带 profiling 的诊断运行；不是 treatment/control 对照，也不是 adaptive timeout 的效果证明

## 背景和动机

这次运行暴露了一个清晰的决策问题：Judge 的大多数验证很快返回，但极少数请求会持续数百秒，甚至接近 600 秒的硬上限。长请求主要阻塞发起它的 Agent；本次 Judge 全局并没有被并发打满。因此，我们需要判断的不是“Judge 是否整体不可用”，而是以下四件事：

1. 长尾是否足够集中，值得让 Agent 对前台等待时间做自主判断；
2. 在不误杀合法长证明的前提下，渐进式反馈或软性提前终止是否值得实验；
3. proof 已取得后，sibling cancellation 是否已经正确，剩下的优化点在哪里；
4. 是否存在可安全去重的重复验证，以及应先做哪一层 singleflight。

本报告只声称证据能支持的范围。它要回答的是一次 1 小时真实运行的 Judge 运行状况和工程决策，不把“未收到结构化结果”解释成“证明一定不存在”，也不把 backend 的 `failed` 统一解释成 Judge 服务崩溃。

## 具体改了什么

本轮没有改变实验处理、源码、Judge 配置或运行状态；改变的是分析组织方式。按照决策导向的报告结构，先给出可执行判断，再把详细分母、耗时、资源和生命周期证据集中放在最后一节。运行时的相关行为如下：

| 观察对象 | 本次运行实际行为 | 对本报告的含义 |
|---|---|---|
| 单次 Judge attempt | backend hard timeout 300 s，`max_retries=1`；Judge-facing elapsed 可到约 603 s | timeout 是一次候选 attempt 的终态，不是服务 crash |
| completed-result cache | Judge 本地 completed cache 生效；本次 accepted 中有 189 条 cache reuse | 已完成重复能被压低，但不能消除 in-flight race |
| backend result cache | 明确 disabled | 不把跨请求 cache 命中误计为 backend 能力 |
| session admission | 每 session 最多 32 次 accepted probe，最小间隔 1 s，一次只允许一个 in-flight probe | `SESSION_PROBE_IN_FLIGHT`/quota reject 是控制结果 |
| proof 后 sibling cancellation | `cancel_on_proved=true`；proof commit 后停止后续 admission，并向同题活动传播取消 | 主路径已经存在；本报告只评估语义和观测边界 |
| Agent 前台等待 | 没有 Agent-facing soft timeout、continuation handle 或自选等待档位 | “60 s soft wait + extension”仍是待验证的设计，不是本次 treatment |
| exact-key in-flight 去重 | 没有 pending future/singleflight map；同 hash 的并发请求可各自提交 | 本次真实观察到跨 Agent fresh duplicate |

因此，后文提出的 soft wait、singleflight 和 winner 语义都是下一步任务预期，不应被读成已经部署的功能。

## 具体的实验

| 项目 | 设置 |
|---|---|
| 实验问题 | 量化 Judge 验证延迟、资源、失败类别和并发；判断软性等待、提前终止、sibling cancellation 与去重的优先级 |
| 任务范围 | 12 道 MathOlympiadBench 数学奥林匹克题 |
| 运行时长 | 配置 horizon 3600 s；`horizon.start → horizon.end` 实测约 3605.460189 s |
| 重复次数 | 1 次真实 run；无 baseline/treatment replicate |
| 运行形态 | CPS、blackboard communication、uniform allocation；`initial_agents_per_task=2`、`episodes_per_task=2` |
| 并发合同 | `max_parallel=32`、`aisw_max_in_flight=32`、`lean_max_concurrent_evaluations=32` |
| Judge | `formal_matholympiadbench`，Lean timeout 300 s，backend `max_retries=1`，configured workers 32 |
| Agent/model | `openai-codex/gpt-5.6-sol`；Pi timeout 900 s；provider retry 0 |
| 资源合同 | Docker memory 32,768 MiB，pids 4096；worker RSS recycle 8192 MiB、hard 24,576 MiB；CPU 无硬 cap |
| 真实性 | real workload + profiling；不是 mock、replay、canary 或 schema-only smoke |
| 运行健康 | 最终 score `5/12`，状态 `DEGRADED`；22 个 Judge probe infrastructure error、2 个 unexpected process error、62 个 solver timeout |
| 统计分母 | 分开统计 all checks、accepted、fresh、cache reused、backend jobs、candidate-bound terminal、control/infrastructure 和 closeout |

本次 run 的分数和健康状态只用于说明背景，不能作为后续策略的因果对照。`selection.enabled=false`，所以本报告也不推断 Selection 路径的开销或收益。

## 结论

1. **验证时间确实由极少数长尾请求主导。** 1,252 条 fresh accepted probe 的累计 Judge-facing elapsed 约 12,384.7 s；仅 9 条超过 600 s，却贡献 5,423.5 s（43.792%），且这 9 条在本次运行中全部是 `EXECUTION_TIMEOUT`。这足以支持“前台等待应允许软性决策”的方向。

2. **本次没有观察到 Judge 全局排队瓶颈。** backend active job 的时间加权平均并发约 3.98、中位数 4、峰值 19，queue depth 中位数 1、峰值 3；solver 槽位反而长期接近 32/32。因此长请求首先是单个 Agent 的等待问题，同时仍需监控长尾数量上升后对 32 个 backend worker 的压力。

3. **失败必须按候选结果和基础设施结果分开。** 大多数 backend `failed` 都有可关联的 candidate、Lean 错误或资源终态；本次没有 service crash、runner/worker error、OOM 或 throttle 证据。`EXECUTION_TIMEOUT`、`RESOURCE_LIMIT` 是 candidate-bound attempt 的终态，不能直接写成 Judge 服务崩溃。

4. **Agent 自主决定等待时间是合理的下一步，但应是“Agent 建议、server/broker 裁决”。** 推荐先实验 `60 s soft wait + 最多一次 extension 到 300 s`；soft deadline 只表示 Agent 不再阻塞前台，不等于 `VERIFY_FAIL`。优先让 Agent 继续等待同一个 remote job；若必须重提，必须用新的 attempt/retry token 绕过旧 timeout cache。当前证据不支持全局 60 s hard cutoff，也不支持“连续 K 次失败就停止”。

5. **渐进式反馈很可能减少 Agent 的无效前台等待，但尚未证明能提高分数或节省等量 Judge CPU。** 反馈应报告 `queued/running`、compile checkpoint、资源警告、剩余 soft/hard 预算和可执行选项；最终效果要用 fixed-horizon score-time nAUC、proof 数、false-stop、worker-seconds、P95/P99 和 settlement latency 一起判断。

6. **proof 后取消 sibling 的主路径已经实现且本次有效。** 四次 early proof callback 后，同题 Agent 约在 0.489–0.604 s 内结束，最终 closeout 没有 active handler、FIFO backlog 或 remote unsettled job。下一步不是重写取消，而是修正 winner self-cancel 的语义，并补齐 proof→cancel→remote terminal→slot release 的时间线。

7. **exact-candidate 的跨 Agent in-flight 重复真实存在，值得优先做 singleflight。** fresh accepted 中有 10 个 exact key 发生跨 Agent 重叠，共 15 条额外 fresh call；completed cache 已解决的 189 条不能覆盖这个 race。建议先做带 execution-policy fingerprint 的 cross-session singleflight；同 session 的 36 条 in-flight reject 没有产生 backend job，直接资源收益较小，优先级较低。

8. **当前决策：保留现有 hard safety boundary，按 P0→P1→P2→P3 推进。** 先修取消观测和 winner 语义，再做 exact-key singleflight，随后进行 adaptive timeout/progressive feedback 的 matched A/B；在 trace 证明 retry churn 足够大之前，不单独投入 same-session merge，也不把本次单 run 写成性能提升结论。

## 支撑结论的数据和分析

### 1. 验证次数与耗时分布

#### 1.1 分母和请求数量

| 口径 | 次数 | 解释 |
|---|---:|---|
| 全部 `judge_check` 记录 | 1,499 | accepted 与 broker 控制拒绝的总记录 |
| accepted | 1,441 | broker 接受 probe；不代表验证成功 |
| rejected | 58 | `SESSION_PROBE_IN_FLIGHT=36`，`SESSION_PROBE_BUDGET_EXHAUSTED=22` |
| fresh accepted | 1,252 | 排除 completed/probe cache reuse |
| completed-cache reused | 189 | accepted 的约 13.1%，约 1 ms 级 |
| accepted 且有 `judge_job_id` | 1,429 | 12 条 `LOCAL_REJECTED` 未提交 backend |
| `judge.receipt` | 1,581 | 1,499 条 check receipt + 82 条 solver/agent outcome receipt；不是 1,581 次新验证 |

accepted probe 的 Agent-facing elapsed 分布：

| 口径 | n | 平均 | 中位数 | P90 | P95 | P99 | 最大 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 全部 probe | 1,499 | 8.262126 s | 1.271270 s | 5.041063 s | 10.077065 s | 211.379118 s | 603.289839 s |
| accepted | 1,441 | 8.594674 s | 1.271681 s | 5.044537 s | 11.324690 s | 211.846658 s | 603.289839 s |
| fresh accepted | 1,252 | 9.891936 s | 1.274114 s | 6.293134 s | 20.205775 s | 279.994298 s | 603.289839 s |
| cache reused | 189 | 0.001169 s | 0.001047 s | 0.001680 s | 0.001882 s | 0.002615 s | 0.006604 s |
| rejected | 58 | 0.000022 s | 0.000018 s | 0.000026 s | 0.000056 s | 0.000099 s | 0.000100 s |

`judge.receipt.evaluator_seconds` 是 evaluator 子阶段耗时；accepted 累计约 12,383.009555 s，平均 8.593345 s，中位数 1.270268 s，P95 11.323395 s，最大 603.288469 s。它与端到端 elapsed 只差 snapshot、gate、audit 等薄层开销，不能把两者相加当成串行时间。

#### 1.2 长尾贡献

以下只统计 fresh accepted 的端到端 `elapsed_seconds`，总和为 12,384.704116 s：

| 耗时阈值 | 请求数 | 累计耗时 | 占 fresh elapsed |
|---|---:|---:|---:|
| > 1 s | 1,245 | 12,383.866544 s | 99.993% |
| > 5 s | 157 | 10,411.460588 s | 84.067% |
| > 30 s | 43 | 9,226.104046 s | 74.496% |
| > 60 s | 25 | 8,502.040693 s | 68.650% |
| > 120 s | 17 | 7,897.338186 s | 63.767% |
| > 300 s | 13 | 7,008.377741 s | 56.589% |
| > 600 s | 9 | 5,423.513161 s | 43.792% |

`>300 s` 的 13 条由 9 条 `EXECUTION_TIMEOUT`、3 条 `TASK_CANCELLED` 和 1 条长耗时 `VERIFY_FAIL` 构成；`>600 s` 的 9 条全部是 `EXECUTION_TIMEOUT`。因此本次“奔着超时去的请求全部不成功”是一个被数据支持的本 run 观察，不应推广成所有复杂题目的普遍规律。

#### 1.3 fresh 终态的耗时结构

下表的均值、中位数、P95 和最大值使用 `judge.receipt.evaluator_seconds`；最后一列使用同一条 receipt 的端到端 elapsed 计算时间占比。`LOCAL_REJECTED` 没有 backend job，故只表示本地合同层快速返回。

| fresh 状态 | n | evaluator 平均 | 中位数 | P95 | 最大 | elapsed 占比 |
|---|---:|---:|---:|---:|---:|---:|
| `VERIFY_FAIL` | 912 | 3.505705 s | 1.272274 s | 7.567527 s | 300.578204 s | 25.825% |
| `COMPILES_WITH_SORRY` | 269 | 2.960167 s | 1.272439 s | 6.419522 s | 36.524220 s | 6.432% |
| `CHEATING` | 39 | 15.518283 s | 1.271122 s | 69.421880 s | 70.328792 s | 4.887% |
| `PROVED` | 4 | 17.998387 s | 12.598015 s | 37.390008 s | 41.764336 s | 0.582% |
| `EXECUTION_TIMEOUT` | 9 | 602.611442 s | 602.718460 s | 603.190121 s | 603.288469 s | 43.792% |
| `RESOURCE_LIMIT` | 3 | 209.580194 s | 211.362103 s | 212.087367 s | 212.167952 s | 5.077% |
| `TASK_CANCELLED` | 10 | 166.004332 s | 50.275264 s | 482.654698 s | 500.511823 s | 13.405% |
| `LOCAL_REJECTED` | 6 | 0.000150 s | 0.000146 s | 0.000191 s | 0.000205 s | <0.001% |

早期 `PROVED` 只有 4 条，因为它只统计 early `judge_check`；run 的最终 durable score 是 5/12，不能把这两个分母混在一起。

### 2. 题目异质性与预期反馈时间

#### 2.1 题目级分布

| task | fresh n | 中位数 | 非 timeout P95 | 特殊尾部 |
|---|---:|---:|---:|---|
| `imo2023_p3` | 154 | 1.274 s | 4.352 s | 2 次约 603 s timeout |
| `usa2024_p2` | 135 | 1.270 s | 3.879 s | 2 次约 603 s timeout，另有取消 |
| `imo2023_p2_v2` | 78 | 1.273 s | 46.491 s | 4 次约 603 s timeout；非 timeout 也有长尾 |
| `imo2024_p5` | 222 | 1.272 s | 3.786 s | 1 次约 602 s timeout |
| `uk2024_r1_p1` | 183 | 2.530 s | 66.565 s | 3 次 `RESOURCE_LIMIT`，约 205–212 s |
| `imo2023_p4` | 51 | 21.413 s | 35.259 s | 没有 600 s 尾部，但整体基线更慢 |

4 次 early `PROVED` 的 Judge elapsed 约为 5.04、12.61、12.61、41.77 s，样本太小，不能把 60 s 当作合法 proof 的硬上限。成功 Agent 在取得 proof 前最多经历过 28 次连续 `VERIFY_FAIL`，所以连续失败 K 次也不是可靠的全局终止规则。

#### 2.2 对启发式的含义

这组数据支持按 `(task, phase, candidate/Agent policy)` 建立滚动先验，而不是设一个全局常数：

| 规则候选 | 当前判断 | 依据/风险 |
|---|---|---|
| 同一 hash 已得到确定的 `VERIFY_FAIL`/`CHEATING`，不做无条件同策略重试 | 可直接写入设计 | 重试若有意义，必须是显式 fresh attempt |
| soft wait 或基础设施错误不改写为 `VERIFY_FAIL` | 可直接写入设计 | 保持候选结果与控制/基础设施层分开 |
| 题目历史 p95 很低、queue 正常、hash 未变，当前耗时超过 `max(60 s, 若干倍 robust median)` | 值得 A/B | `imo2023_p2_v2`/`imo2023_p4` 说明不能只用低基线 |
| 已通过 compile checkpoint 且题目历史有重 tail，允许一次 extension | 值得 A/B | 可能保留合法长证明，但增加 worker work |
| live RSS/CPU slope 接近 resource-limit 模式时提前取消 | 暂不强制 | 当前 timeout/resource-limit 终态 RSS 不完整 |
| 所有请求 60 s hard stop | 不采用 | 会误伤可能合法的长证明 |
| 连续失败 K 次即停止 | 不采用 | 已成功 Agent 的失败 streak 可达 28 |

### 3. 资源消耗与并发

#### 3.1 backend job 的执行和 RSS

backend 日志匹配到 1,964 个 job。它与 1,499 条 `judge_check` 不一一对应，因为包含 retry、formal helper 和不同层的 job。下面按 backend terminal reason 展开；Judge-facing 的 3 条 `RESOURCE_LIMIT` 是 `verification_failed` 中 `error_kind=memory_limit_exceeded` 的子集，不应再相加。

| backend 终态/语义 | n | execution 平均 | 中位数 | P95 | 最大 | RSS（MiB） |
|---|---:|---:|---:|---:|---:|---|
| `verified_without_sorry`（完整 proof） | 231 | 1.131446 s | 0.107 s | 6.302 s | 21.211 s | n=231；均值 3,104.96；中位数 2,752；P95 4,596.5；最大 10,689 |
| `verified_with_sorry`（可编译但不完整） | 329 | 2.066125 s | 0.904 s | 5.911 s | 36.031 s | n=329；均值 3,248.78；中位数 2,850；P95 4,635.8；最大 7,528 |
| `verification_failed`（含 3 条 memory-limit 子集） | 1,382 | 3.514349 s | 0.796 s | 12.994 s | 300.372 s | n=1,373；均值 3,457.40；中位数 2,843；P95 5,001；最大 13,308 |
| `execution_timeout` | 11 | 601.987727 s | 601.969 s | 602.131 s | 602.226 s | 终态 RSS 不可靠 |
| `cancelled` | 11 | 178.803 s | 92.997 s | 480.473 s | 500.306 s | 终态 RSS 不可靠 |

`verification_failed` 的可用 RSS 均值比两类 `verified` 成功结果更高，但中位数接近；这是候选/代码形态的描述性差异，不足以推出单一因果。所有 backend job 的累计 worker execution 约 14,386.647 s，是并行 work-seconds，不是串行墙钟。

#### 3.2 外部资源 sampler

外部 sampler 共 730 个样本，覆盖约 3,645 s 窗口。Judge 与 compatibility proxy 共用一个 cgroup，因此 shared cgroup 数字不是 Judge 独占值；RSS sum 也可能重复计算共享页。

| 指标 | 平均 | 中位数 | P95 | P99 | 最大/峰值 |
|---|---:|---:|---:|---:|---:|
| Judge process-tree RSS sum | 109.747 GB | 108.729 GB | 128.114 GB | 135.751 GB | 142.252 GB |
| Judge process-tree PSS（可用样本 n=716） | 32.707 GB | 31.301 GB | 49.824 GB | 57.315 GB | 62.587 GB |
| shared cgroup memory current | 50.803 GB | 49.305 GB | 67.937 GB | 75.182 GB | cumulative peak 83.403 GB |
| Judge process-tree CPU（窗口累计） | — | — | — | — | 8,832.2 CPU-s，约 2.42 核 |
| shared cgroup CPU（窗口累计） | — | — | — | — | 41,178.9 CPU-s，约 11.3 核 |
| 进程数 | 96.39 | 97 | 97 | 97 | 98 |
| 线程数 | 1,089.9 | 1,117 | 1,651 | 1,658.7 | 1,670 |

本次没有观察到 OOM 或 CPU throttle。资源数字适合做压力和容量背景，不能把 process-tree RSS 与每个 backend job RSS 相加。

#### 3.3 实际并发与排队

| 层 | 平均/中位数 | P90/P95/P99 | 峰值 | 限制或解释 |
|---|---:|---:|---:|---|
| backend active jobs | 时间加权平均 3.98095；中位数 4 | 7 / 9 / 10 | 19 | configured workers 32 |
| client `judge.execute` | 平均 3.4421；中位数 3 | 6 / 7 / 8 | 20 | evaluator gate 32 |
| solver active slots | 中位数约 31 | 32 / 32 / 32 | 32 | solver capacity 基本满载 |
| broker queue depth | 平均 1.0023；中位数 1 | 1 / 1 / 1 | 3 | gate wait 平均约 0.105 ms，最大 5.601 ms |
| backend queue wait | — | 中位数约 0.149 s；P95 约 0.174 s | 约 0.696 s | 不支持“全局排队导致 600 s”解释 |

#### 3.4 已施加的限制

| 限制层 | 当前值/合同 |
|---|---|
| 全局并发 | `max_parallel=32`、`aisw_max_in_flight=32`、`lean_max_concurrent_evaluations=32` |
| backend worker | `configured_workers=32`、`min_ready_workers=32`、`max_pending=512` |
| session probe | 每 session 最多 32 次；最小间隔 1 s；一次只能有一个 in-flight probe |
| Judge hard timeout | 300 s/attempt；`max_retries=1` |
| run 生命周期 | horizon 3600 s；lifecycle allowance 4500 s；drain allowance 360 s |
| formal helper | evaluate calls/task 120；backend jobs/task 120；query calls 60；query backend probes 120 |
| candidate | 最大 2 MiB |
| Agent | Pi timeout 900 s；provider retry 0；Pi retry max 10；Agent recovery max restart 1 |
| 资源 | Docker memory 32,768 MiB；pids 4096；CPU 无硬 cap；worker RSS recycle 8192 MiB、hard 24,576 MiB |

### 4. 失败归类与运行健康

#### 4.1 候选绑定的验证结果

accepted rows 中，候选绑定的结果包括 `VERIFY_FAIL`、`COMPILES_WITH_SORRY`、`CHEATING`、`EXECUTION_TIMEOUT`、`RESOURCE_LIMIT`、`LOCAL_REJECTED` 和 `PROVED`。fresh 分母下对应的数量已经在 1.3 节列出；它们的共同点是可以绑定到 task contract 和 candidate snapshot。

- `VERIFY_FAIL`、`COMPILES_WITH_SORRY`、`CHEATING` 是候选内容反馈。
- `EXECUTION_TIMEOUT` 是候选 attempt 在执行上限内未完成；本次 9 条 fresh 超长样本全部落在这里。
- `RESOURCE_LIMIT` 是候选触发 Lean 资源边界；backend 原始记录把这 3 条写为 `status=failed, terminal_reason=verification_failed, error_kind=memory_limit_exceeded`，Judge 再归一化为 `RESOURCE_LIMIT`。
- `LOCAL_REJECTED` 在 broker/snapshot/contract 层拒绝，没有 backend job。

这些状态可以是 zero-progress feedback，但不能自动升级为 Judge 服务故障，也不能在没有显式新策略的情况下无限 retry。

#### 4.2 控制、生命周期和基础设施结果

| 类别 | 本次证据 | 统计处理 |
|---|---|---|
| session admission reject | `SESSION_PROBE_IN_FLIGHT=36`、`SESSION_PROBE_BUDGET_EXHAUSTED=22` | 控制拒绝；没有 backend job，不计入候选失败 |
| task/control cancellation | `TASK_CANCELLED` fresh 10 条；backend `cancelled` 11 个 | 单独保留 cancel reason，不计为验证失败 |
| Judge probe infrastructure error | run health 22 条 | infrastructure 层，需 retry/reconcile；不写成 candidate `VERIFY_FAIL` |
| solver/process error | unexpected process error 2 条 | Agent/solver 生命周期问题，与 Judge 候选结果分开 |

#### 4.3 是否发生 Judge 自身崩溃

本次没有 candidate-independent 的 service crash 证据：

- backend `service_start` 1 次，没有 `service_crash`、restart 或 autoscale 事件；
- runner/worker error 为 0，OOM 与 throttle 为 0；
- 1,964 个 matched backend job 都有 start/finish 边界；
- 最终证据 `judge_broker_closeout.json` 为 `active_handlers=0`、`fifo_depth=0`、`remote_unsettled_jobs=0`；原始文件未随文档分发。

因此，“backend failed 很多”与“Judge 服务崩溃”是两个不同结论。本 run 的健康状态仍是 `DEGRADED`，所以不能把“无 crash”扩大成“无基础设施问题”。

### 5. Proof 后 sibling cancellation：已实现的主路径与剩余风险

#### 5.1 本次已经有效的链路

runner 的 proof admission 会原子记录 proof 和 candidate provenance，调用 `scheduler.task_solved()` 停止后续 admission，并通过 task-specific cancel event 传播到同题活动。已提交的 Judge job 由 evaluator 发 DELETE；远端 terminal receipt 延迟时由 watcher reconcile；closeout 等待 handler、队列和 remote unsettled job 清零。四次 early proof 的时序如下：

| task | proof credit | 同题 Agent（含 winner）结束 | 远端结算 |
|---|---|---:|---|
| `uk2024_r1_p2` | `01:24:01.420` | 3 个，约 0.489 s 内 | 正常 |
| `imo2023_p4` | `01:45:03.510` | 2 个，约 0.495 s 内 | 正常 |
| `imo2024_p6` | `01:51:42.726` | 4 个，约 0.536 s 内 | 一个活跃 job 约 1.14 s 内收到 terminal cancel |
| `uk2024_r1_p1` | `02:18:09.521` | 5 个，约 0.604 s 内 | 一次 DELETE network error，约 3.52 s 后 reconcile 到 terminal cancel |

本次 profiling 记录 12 次 DELETE（9 `cancel_requested`、1 `failed`、2 `network_error`），backend 记录 11 个 `cancelled` job。closeout 的 drain 结果说明主路径不是缺失功能。

#### 5.2 winner self-cancel 是语义/观测问题

四次 early proof 中 winner 自身都记录了 `returncode=-15`、`cancelled=true`。proof 在此之前已经 durable，因此没有看到 correctness 损失；但 winner 被计成 recovery failure 会污染生命周期指标。可选方案是：

1. 继续快速停止所有同题进程，但把 winner 标为 `proof_winner_cancelled`/expected；或
2. winner 收到 `PROVED` 后豁免 peer-cancel，只取消 sibling，并测量是否引入有意义的额外 Agent 时间。

无论采用哪种方案，都必须保留 scheduler 的 slot accounting：`task_solved()` 停止未来 admission，但 active lease 只有在进程真正退出、worker finish/release 后才能算释放，不能提前调用取消接口制造虚假的空槽。

### 6. 重复提交与 deduplicate

#### 6.1 三类重复的边界

| 层次 | 本次观察 | 决策 |
|---|---|---|
| task-only singleflight | 同题不同候选可能被错误合并 | 不安全，不做 |
| exact `(task, contract, candidate)` in-flight | 当前无 pending future map；存在跨 Agent fresh 重复 | P1 优先实现 |
| same-session in-flight merge | 36 条 `SESSION_PROBE_IN_FLIGHT` reject，均无 backend job | 主要改善 tool/API churn，P3/低优先级 |

#### 6.2 Agent-facing 主 Judge 的 exact duplicate

在 accepted rows 中：

- exact `(task_id, task_contract_sha256, candidate_sha256)` 唯一 key 为 1,242 个；
- 90 个 key 出现重复，除首次外共有 199 条 accepted extra rows；
- 其中 189 条是 completed cache hit；
- fresh 部分有 10 个 key 重复，共 25 条 fresh call，其中 15 条是额外 fresh call；
- 这 10 组全部是跨 Agent 的时间重叠，未发现同一 Agent 对同一 key 的 fresh 重复；
- 25 条 fresh duplicate call 的 elapsed 总和约 161.828 s，按每组保留最长一次的简单上界，额外约 83.235 s。它是可避免 work 的估计，不是可直接从墙钟扣除的节省。

根因是 cache lookup 与 remote submission 之间存在并发窗口：第一个请求尚未完成并写入 completed cache，其他请求已经通过 cache check，各自提交了 backend job。

#### 6.3 backend 宽口径重复

1,953 个带 code hash 的 backend job 中有 1,921 个唯一 `(problem_id, code_sha256)` pair；22 组重复 pair 产生 32 个 extra job，额外 worker execution 约 1,323 s。这个数字包含普通 probe、formal `evaluate_local`、query/helper 和 closeout；其中约 602 s 的重复组来自 formal helper，不能全部归因于 Agent-facing Judge check。因此它是容量上界，不是直接承诺的节省。

#### 6.4 推荐 singleflight key 和安全语义

建议 key 至少包含：

```text
scope / run or Judge deployment identity
request_class
task_contract_sha256
candidate_sha256
lean_env_id
verification_profile
judge_mode
Judge image/version
hard execution-policy fingerprint
```

不包含 Agent ID，使跨 Agent 合并可行；soft wait 如果只是 waiter 自己的属性，可以不放进 key。不同 hard timeout、retry 次数、resource class 必须进入 policy fingerprint，或改成同一 remote job、多 waiter deadline 的模型。`authoritative judge_check`、advisory formal helper 和 closeout 必须由 `request_class` 区分；closeout 的 `evaluate_fresh` 永远绕过普通 cache/singleflight。

leader/follower 必须满足：

1. 在同一把锁内完成 cache lookup、in-flight lookup 和 leader creation，消除双 leader race；
2. leader 只提交一个 remote job，follower 等待同一个 future；
3. 每个 caller 保留自己的 audit/receipt，并标记 `singleflight_reused=true`，不改变报告分母；
4. follower 到自己的 soft deadline 只能 detach，不能在仍有 waiter 时取消 leader job；
5. 没有 waiter 后才 DELETE，并等待 remote terminal；
6. 显式 fresh retry 必须创建新 attempt，绕过旧 future/cache；transport、overload、malformed receipt 等基础设施结果不能永久缓存成 candidate verdict；
7. proof callback 仍只允许一次有效 task-solved transition，后续 follower 结果必须幂等。

这条路线不会合并不同 candidate hash；主要风险在 cancellation ownership 和 receipt 语义，而不是 proof provenance。

### 7. Agent soft wait、渐进式反馈与提前终止方案

#### 7.1 建议的四个时钟

| 时钟 | 含义 | 控制者 |
|---|---|---|
| soft wait deadline | Agent 愿意为 foreground feedback 等待多久，例如 60 s | Agent 从有限档位建议，broker 约束 |
| backend execution cap | 一次 attempt 最多运行多久，例如 300 s | server/Judge |
| candidate cumulative budget | 同一候选是否允许 extension/retry、最多累计多久 | broker/任务策略 |
| run horizon | 整个实验剩余时间 | runner |

soft deadline 到达时，结果应是非权威的 `AGENT_WAIT_TIMEOUT`/`PROBE_WAIT_EXPIRED` 或 detach 事件，不能伪装成 `VERIFY_FAIL`。推荐状态机：

```text
candidate snapshot
  ├─ soft deadline 内得到 terminal verdict
  │    ├─ PROVED
  │    ├─ VERIFY_FAIL / COMPILES_WITH_SORRY / CHEATING
  │    ├─ RESOURCE_LIMIT / EXECUTION_TIMEOUT
  │    └─ infrastructure/control result
  │
  └─ soft deadline 到达，remote job 仍在运行
       ├─ abandon：取消并放弃当前等待
       ├─ continue_once：继续等待同一 remote job
       └─ retry_fresh：旧 job 终态后显式发起新 attempt
```

短期若只能实现“第一次 60 s、第二次 300 s”，必须同时提供 continuation 或 explicit fresh-retry token、旧 job settlement 状态、独立 attempt audit 和 cache bypass。否则本次已观察到的 timeout cache hit 会让升级请求直接得到旧 timeout。

#### 7.2 渐进式反馈的最小信息集

反馈不需要每秒日志，建议只发送低基数里程碑：

- `queued` / `running`；
- compile checkpoint 成功或失败；
- resource warning；
- 当前 attempt/extension 次数；
- soft/hard/cumulative 剩余预算；
- queue/infrastructure 状态；
- 继续、放弃、修改候选等下一步选项。

它最可能先改善 Agent 的可用前台时间，而不是直接减少 backend CPU。remote job 若继续运行，Judge work 可能不变；若取消又重试，甚至可能增加 work。因此 A/B 必须同时看用户等待和服务成本。

### 8. 下一步任务预期、验收门槛与限制

#### P0：取消语义与观测补强

目标是不改变 proof 正确性，先把生命周期变得可解释。

- winner 豁免 peer-cancel，或保留 self-cancel 但改成 expected 语义；
- 为每次取消记录 `task_id/candidate_sha256/job_id/cancel_reason`；
- 记录 `proof_committed_at`、`cancel_signal_at`、`agent_exit_at`、`judge_delete_start/end`、`remote_terminal_at`、`slot_release_at`；
- 覆盖 proof race、DELETE network error、延迟 settlement、completion-wins 和 closeout independence。

验收：proof 数和 score 不下降；取消按 reason 分层；closeout 仍为 `remote_unsettled_jobs=0`；winner 不再被误计为普通 recovery failure，或该状态明确标为 expected；提供 cancellation reclaim latency 的 P50/P95。

#### P1：exact-key cross-session in-flight singleflight

目标是消除本次已经确认的跨 Agent 并发重复。

- 增加 `_probe_inflight` future map 和 execution-policy fingerprint；
- 保留 completed cache、closeout fresh、authoritative callback 和 request-class 边界；
- 实现 leader/follower detach、refcount、只在无 waiter 时远端取消；
- 加入 explicit fresh-retry token；每个 caller 保留独立 receipt。

验收：N 个并发相同 candidate 只产生 1 个 backend job；不同 hash、hard policy、request class 或 deployment 不合并；follower 取消不影响 leader；所有 waiter 离开后只发送一次 cancel 并完成 terminal settlement；能在本次 10 个 fresh duplicate group 的重放中观察到 job/worker-seconds 减少。

#### P2：adaptive timeout 与渐进式反馈 matched A/B

先做 shadow/advice-only，再做 enforced arm：

- control：当前固定 300 s hard policy；
- treatment：60 s soft wait，最多一次 extension 到 300 s；
- 相同任务、seed、Agent/model、Judge capacity、image、horizon 和非 policy 配置；
- 记录 `requested_policy`、`granted_soft_deadline`、`hard_deadline`、`extension_count`、`decision_reason`。

验收指标：fixed-horizon score-time nAUC、proof 数和每个 proof 时间、false-stop、Agent foreground wait、Judge worker-seconds、retry/timeout/cancel、P95/P99、RSS、queue、settlement、slot reclaim 和 fresh retry 比例。没有这些 matched 指标前，不把降低平均 elapsed 写成算法收益。

#### P3：same-session in-flight merge

仅在 Agent trace 证明 36 类似 reject 带来明显 retry/tool churn 后执行。必须 exact freeze candidate snapshot/hash，candidate 改变时 fail closed，并保留 cooldown、quota、receipt 和 cancellation 合同。

#### 限制和证据边界

- 这是单次真实 run，且最终 `DEGRADED`；没有 control/treatment replicate，不能估计 adaptive timeout 的因果收益。
- 题目基线差异显著；不能从一次 run 推导所有复杂题目的合法证明都应在 60 s 内返回。
- 外部 sampler 的 Judge 与 compatibility proxy 有 cgroup overlap；RSS/PSS/CPU 不能解释为纯 Judge 独占资源。
- backend job 统计包含 retry/helper/closeout；不能把宽口径 duplicate 上界全部归因于主 `judge_check`。
- 本次 `selection.enabled=false`；报告没有覆盖 Selection 相关成本。
- “soft timeout 后如果继续等会不会得到 proof”尚未观测，需要 matched continuation/fresh-retry 实验测 false-stop。

证据索引（原始文件未随 PR 分发）：

- `run_meta.json`
- `judge_checks.jsonl`
- `profiling.jsonl`
- `formal_tool_calls.jsonl`
- `events.jsonl`
- `final.json`
- `profiling-audit-r2.json`
- `external-profile-r2/summary.json`
- `lean_service_events.jsonl`

这些是逻辑证据文件名，用于在受控证据归档中定位；报告和 PR 不携带作者机器上的路径。

**本轮交付状态**：报告已按决策导向结构重整；没有修改源码、实验产物、服务或运行状态。下一步应在 P0/P1 语义确定后，再授权实现和 matched 实验。
