# 外部路线去重三轮实验的逐题原因分析

这份附录回答一个比总分更具体的问题：每一轮 control/treatment 的差异，究竟来自哪一道题、哪一批 Agent、哪一种路线和运行错误。它只使用已经完成的六个 real arm 的落盘日志，不把最终 verdict 的差异直接等同于算法因果。

## 证据口径

每个任务的 `attempts/timeouts` 来自该 run 的 `final.json` 中 Agent assignment；路线来自 `communication_trace.jsonl` 的 `route_claim_created`；证明时间来自 `scoreboard_history.jsonl` 中第一次 `source=judge_check,status=PROVED`；provider retry 来自 `pi_events.jsonl` 的 `auto_retry_start,error_category=provider_5xx`；Judge/Lean 反馈来自 `judge_checks.jsonl` 和 `formal_tool_calls.jsonl`。

这些字段的含义不同：Agent timeout 是 runner 终止一个 Agent，provider retry 是 Pi 记录的服务重试，Lean `RESOURCE_LIMIT`/`EXECUTION_TIMEOUT` 是 Judge 对候选检查的反馈，route claim 是 Agent 声明的方向。它们不能互相替代，也不能只凭其中一个字段断言根因。

## 六个 arm 的任务结果矩阵

符号含义：`P` 是 `PROVED`，`S` 是 `COMPILES_WITH_SORRY`，`V` 是 `VERIFY_FAIL`。括号内是 `attempts/timeouts`；证明时间只对 `P` 显示。

| 任务 | r0 control | r0 treatment | r1 control | r1 treatment | r2 control | r2 treatment |
| --- | --- | --- | --- | --- | --- | --- |
| `imo2023_p2_v2` | V (15/7) | V (13/2) | S (14/3) | S (16/3) | S (18/4) | S (17/6) |
| `imo2023_p3` | S (15/3) | V (13/5) | V (13/7) | S (16/7) | V (17/7) | S (17/7) |
| `imo2023_p4` | P 3022s (11/2) | S (12/10) | P 1354s (4/0) | P 1658s (5/0) | P 461s (3/0) | P 410s (2/0) |
| `imo2023_p5` | S (15/3) | S (12/6) | S (13/5) | P 1151s (4/0) | P 2735s (11/0) | S (17/2) |
| `imo2024_p1` | P 851s (6/0) | P 1519s (7/0) | P 583s (4/0) | P 2742s (12/5) | P 3203s (15/4) | P 498s (5/0) |
| `imo2024_p2` | P 2830s (12/4) | P 1791s (8/0) | P 1130s (5/0) | P 2773s (13/4) | P 1593s (7/0) | P 2394s (12/4) |
| `imo2024_p3` | S (17/9) | S (14/4) | S (15/8) | S (18/8) | V (19/9) | S (19/13) |
| `imo2024_p5` | S (17/13) | S (14/10) | S (15/15) | S (18/15) | S (19/15) | V (19/14) |
| `imo2024_p6` | P 2402s (10/2) | P 2517s (11/3) | P 2594s (10/4) | P 554s (5/0) | P 1059s (5/0) | P 1659s (7/0) |
| `uk2024_r1_p1` | P 3279s (14/3) | S (14/10) | P 3474s (14/7) | P 245s (3/0) | P 194s (3/0) | P 442s (4/0) |
| `uk2024_r1_p2` | P 176s (3/0) | P 236s (3/0) | P 140s (3/0) | P 78s (3/0) | P 101s (3/0) | P 89s (3/0) |
| `usa2024_p2` | S (16/5) | S (14/2) | V (15/5) | V (18/13) | S (19/6) | S (19/3) |

这个矩阵先排除了一个容易造成误判的现象：很多任务的最终字符串状态变了，但 score 没变。例如 `imo2023_p3` 在 r0 是 `S` 对 `V`，r1/r2 是 `V` 对 `S`，但两边都得 0 分；`imo2024_p3` 和 `imo2024_p5` 也属于同类。真正改变分数的只有 r0 的两道题和 r1/r2 的 `imo2023_p5`。

## r0：treatment 少两题的逐题原因

### `imo2023_p4`：control 找到完整路线，treatment 同时遭遇 provider overload 和长 timeout

control 最终在 episode 8、约 `3022.47s` 得到 proof。它前面虽然尝试了 vacuity、endpoint 和局部整数界，但 episode 8 的声明路线已经变成 `full-proof-prefix-two-step`，明确要组装“两步严格增长 + 前缀和”的完整证明；这条路线之后 Judge 返回 `PROVED`，runner 立即取消了同题其余 Agent。

treatment 的 12 个 episode 没有形成同样的收敛点：前几次主要是 vacuity、endpoint、整数增量和局部 shortcut，之后才尝试 `pairwise-integer-increments-telescope`，但没有完成可验证候选。运行信号明显更差：

| 指标 | r0 control | r0 treatment |
| --- | ---: | ---: |
| attempts | 11 | 12 |
| Agent timeout | 2 | 10 |
| `provider_5xx` auto-retry | 5 | 46 |
| turn-level `stop_reason=error` | 14 | 50 |
| Judge `EXECUTION_TIMEOUT` | 0 | 6 |
| 最终状态 | `PROVED` | `COMPILES_WITH_SORRY` |

这不是一个单纯的“treatment 选错路线”案例。路线没有在 treatment 中及时收敛是一个因素，但 treatment 在同一道题上的 provider overload 重试数量约为 control 的 9 倍，Agent timeout 也由 2 增至 10；因此这里有直接日志支持的运行服务不稳定因素。由于四个 arm 的 CPS 都没有产生 `semantic_conflict`/`switch_required`，没有证据显示这是 external dedup 把某条好路线挡掉了。

### `uk2024_r1_p1`：control 最终转向结构递推，treatment 长时间停留在有限枚举/计算路线

control 在 episode 9、约 `3278.69s` 通过 `cardinality-recurrence-final` 路线得到 proof。前面的路线主要尝试 `native_decide`、显式 Fin 9 枚举和 subtype cardinality；最终成功的路线是把有限计数拆成结构递推，而不是继续扩大直接计算。

treatment 的 14 个 episode 也反复尝试 finite-cardinality、`native_decide`、显式枚举和结构化分解，但没有形成可验证的递推闭环。这个任务的 provider retry 并没有像 `imo2023_p4` 那样显著增加（`10` 对 `11`），更明显的是 Lean/Judge 反馈和 Agent timeout：

| 指标 | r0 control | r0 treatment |
| --- | ---: | ---: |
| attempts | 14 | 14 |
| Agent timeout | 3 | 10 |
| provider_5xx retry | 10 | 11 |
| Judge `RESOURCE_LIMIT` | 4 | 8 |
| Judge `EXECUTION_TIMEOUT` | 0 | 2 |
| 最终状态 | `PROVED` | `COMPILES_WITH_SORRY` |

因此这道题更像是“搜索路线和形式化资源预算没有及时切换”的失败，而不是 provider overload 主导。control 也花了很久才成功，但最终找到了结构递推；treatment 在计算路线上的重复尝试消耗了更多 timeout，直到 horizon 结束仍没有到达可验证 proof。

### `imo2023_p3`：状态变化但不影响 score

r0 control 是 `COMPILES_WITH_SORRY`，treatment 是 `VERIFY_FAIL`；r1/r2 方向变成 control `VERIFY_FAIL`、treatment `COMPILES_WITH_SORRY`。这道题三轮都没有 proof，变化的是最后保留的候选是“可编译但含 sorry”还是“验证失败”。它不能解释 r0 的 -2，也不应被当作 treatment 的分数损失。

## r1：treatment 多一题的逐题原因

### `imo2023_p5`：treatment 更早进入真正的 dyadic recursion，control 长时间停留在 vacuity/Finset shortcut

r1 control 的 13 个 episode 主要使用 `vacuity-cardinality`、`n1_counterexample`、`n2-fin-contradiction`、`structural-fin-path` 等形式化 shortcut，随后仍然没有找到完整证明；其中 5 个 Agent timeout，最终为 `COMPILES_WITH_SORRY`。

r1 treatment 只有 4 个 episode，前面也尝试了 vacuity/cardinality，但 episode 2 的 claim 更新为 `dyadic-recursion-episode2`，明确开始完成真正的 dyadic recursion 和 logarithm arithmetic。该 Agent 在约 `1150.84s` 得到 `PROVED`，没有 Agent timeout，也没有 provider_5xx retry。对应对比是：

| 指标 | r1 control | r1 treatment |
| --- | ---: | ---: |
| attempts | 13 | 4 |
| Agent timeout | 5 | 0 |
| provider_5xx retry | 2 | 0 |
| final proof | 无 | 1150.84s，episode 2 |

这里最强的解释是路线收敛差异：treatment 恰好从“形式化是否 vacuous”的探索切换到了实际的 dyadic recursion，而 control 没有在 horizon 内完成这个切换。由于没有 `switch_required` 事件，不能把这次切换归功于 external dedup；它是模型搜索轨迹的随机结果。

### `imo2023_p3`：仍然是零分状态互换

r1 control 为 `VERIFY_FAIL`，treatment 为 `COMPILES_WITH_SORRY`，但两边都是 0 分。treatment 的 attempts 和 timeout 都更多（16/7 对 13/7），并不显示出一个清晰的资源优势；这只是候选末态不同，不能解释 r1 的 +1。

### r1 AUC 为什么明显更高

r1 treatment 的 score 多一题只是一个因素。AUC 变高还来自多个 proof 的时间顺序：

- `uk2024_r1_p2`：treatment `78s`，control `140s`；
- `imo2024_p6`：treatment `554s`，control `2594s`；
- `uk2024_r1_p1`：treatment `245s`，control `3474s`；
- treatment 新增 `imo2023_p5` proof（`1151s`）；
- 反方向的慢化是 `imo2024_p1`（2742s 对 583s）和 `imo2024_p2`（2773s 对 1130s）。

因此 r1 的 AUC 改善是“多一道 proof + 几道题更早 proof”与“另几道题更晚 proof”的净结果，不能归因于单个 dedup 判断。

## r2：treatment 少一题的逐题原因

### `imo2023_p5`：control 最终修复完整候选，treatment 的 17 个 episode 没有走通真正组合路线

r2 control 的前 5 个 episode 仍在 vacuity、small-n、IsGreatest simplification 等方向上探索，但 episode 6 的路线变为 `repair-current-candidate`，日志明确写的是修复完整 combinatorial candidate、path monotonicity 和最终 logarithm floor arithmetic；约 `2735.14s` 得到 proof。

r2 treatment 则运行了 17 个 episode，其中大多数仍是 structural-vacuity、Finset/cardinality、dependent-Fin indexing 等 shortcut。最后虽然出现了 `logarithmic-red-chain-poset` 这样的真实组合路线，但没有在 horizon 内完成；最终有 2 个 Agent timeout，保留 `COMPILES_WITH_SORRY`。关键对比：

| 指标 | r2 control | r2 treatment |
| --- | ---: | ---: |
| attempts | 11 | 17 |
| Agent timeout | 0 | 2 |
| provider_5xx retry | 3 | 0 |
| final proof | 2735.14s，episode 6 | 无 |
| 最终路线特征 | 修复完整组合候选 | 大量 shortcut，末段才尝试组合路线 |

这一次不能归因于 provider overload：treatment 没有 provider_5xx retry，control 反而有 3 次。更符合日志的解释是 search trajectory：control 较早拿到可修复的完整候选，treatment 花了更多 assignment 在相近的形式化 shortcut 上，直到最后才尝试真正的组合结构，时间不够完成。

### `imo2023_p3` 和 `imo2024_p3/p5`：状态变化不贡献 -1

r2 control/treatment 的 `imo2023_p3` 分别是 `VERIFY_FAIL`/`COMPILES_WITH_SORRY`，`imo2024_p3` 是 `VERIFY_FAIL`/`COMPILES_WITH_SORRY`，`imo2024_p5` 是 `COMPILES_WITH_SORRY`/`VERIFY_FAIL`。三道题两边都没有 score，因此 r2 的 -1 只来自 `imo2023_p5`。

### r2 AUC 为什么仍略高

虽然 treatment 少了 `imo2023_p5`，但它在 `imo2024_p1` 早得多（498s 对 3203s），`imo2023_p4` 也略早（410s 对 461s），`uk2024_r1_p2` 略早（89s 对 101s）；同时它在 `imo2024_p2`、`imo2024_p6`、`uk2024_r1_p1` 上更晚。于是 score 下降与 AUC 略升可以同时出现，AUC 不能替代逐题 score 分析。

## 始终得分任务的运行波动

即使最终 score 相同，proof time 也大幅波动，这说明 provider/搜索随机性在本 workload 中本来就很强：

| 任务 | r0 treatment-control proof time | r1 treatment-control | r2 treatment-control |
| --- | ---: | ---: | ---: |
| `imo2024_p1` | +668s | +2159s | -2704s |
| `imo2024_p2` | -1039s | +1643s | +801s |
| `imo2024_p6` | +114s | -2040s | +601s |
| `uk2024_r1_p1` | treatment 未得分 | -3228s | +249s |
| `uk2024_r1_p2` | +60s | -62s | -12s |

同一道题在不同 paired round 中可以从“treatment 快 2704 秒”变成“treatment 慢 2159 秒”，这比 dedup threshold 所能解释的范围更像模型搜索路径和服务时序差异。新 r1/r2 的 `imo2023_p4` 也都快速得到 proof（1354/1658s、461/410s），而 r0 treatment 单独失败；这进一步说明 r0 p4 的失败不是固定的 treatment 行为。

## 对“到底是什么原因”的结论

可以确认的部分：

1. r0 的 -2 由 `imo2023_p4` 和 `uk2024_r1_p1` 两道题造成；r1 的 +1 和 r2 的 -1 都只由 `imo2023_p5` 造成。其余状态变化均为零分状态变化。
2. r0 `imo2023_p4` 有明确 provider overload/timeout 差异：treatment `46` 次 provider_5xx retry、`10` 个 Agent timeout、`6` 个 Judge execution timeout，control 对应为 `5/2/0`。这是最接近“运行服务导致丢分”的证据。
3. r0 `uk2024_r1_p1` 的主要差异是路线和形式化资源：treatment 的 `RESOURCE_LIMIT`、`EXECUTION_TIMEOUT` 和 Agent timeout 都更多，而 provider_5xx 基本相同；control 最终走通了 structural cardinality recurrence。
4. r1/r2 `imo2023_p5` 的 score 方向相反，且 provider 错误不能解释它：r1 treatment 没有 provider_5xx retry，r2 treatment 也没有；一个 run 走通 dyadic recursion，另一个 run 没有及时走通完整组合候选。这是最直接的路线搜索随机性证据。
5. 六个 arm 都没有 external dedup 的实际仲裁事件，因此当前日志不能证明“外部去重让 Agent 变好或变坏”。它最多说明在 treatment 未触发的情况下，路线搜索和运行时随机性已经足以产生 1–2 题的波动。

仍然不能确认的部分：

- 不能仅凭 provider retry 数量证明 provider overload 是 r0 p4 的唯一根因；它和路线收敛、编辑/工具失败、timeout 是同时发生的。
- 不能把 route claim 的文字相似度直接当成数学路线重复；当前在线机制没有把这些候选重叠转成 `switch_required`。
- profiling audit 有 dropped fields 和未闭合 span，无法对每个模型请求建立完整的资源时间线。

因此当前最准确的判断不是“纯粹随机”，而是：**r0 的下降由 p4 的明显 provider/timeout 异常和 p1 的路线/形式化资源问题共同造成；新 r1/r2 的相反 p5 结果显示，剩余主要波动来自模型搜索路线的随机收敛。没有证据表明 external dedup 本身造成了这些 score 变化。**

## 可复核产物

逐题结构化 JSON 和 episode 摘要属于实验归档中的原始证据，没有随本 PR 分发；本文已经嵌入了这些文件用于决策的聚合结果。对应逻辑证据 ID 为 `six-arm-detail-20260906` 和 `six-arm-differing-tasks-20260906`。主报告位于仓库内的 [`external_route_dedup_experiment_gpt6_20260905.md`](external_route_dedup_experiment_gpt6_20260905.md)。
