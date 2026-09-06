# MathOlympiadBench：终止总结与 CPS 发布实验

## 背景和动机

我们复查原版 MathOlympiadBench（12 道题、每次 1 小时、带 profiling）的运行记录时发现：Agent 在对话或工作区里经常已经产生了反例、排除的路线、验证反馈和下一步，但在 timeout、cancel 或异常结束前，没有一个稳定的动作把这些成果发布到 task-local CPS。于是，后续 Agent 只能看到不完整的消息或文件，无法可靠地知道“已经做过什么、哪些方向不要再做、当前可以从哪里继续”。

这项改动要解决的是**终止时共享知识的回收与发布**。它不是把整个对话保存下来，也不是 retry 时恢复 Agent 运行状态的 checkpoint 机制；后者属于另一条工作流。

## 具体改了什么

正常结束路径保持不变：Agent 按原有 prompt 自主发布有价值的发现。

对仍然存活的 Agent，在 timeout、cancel 或可识别的 provider/assistant error 结束前，runner 预留一个有界的总结窗口（默认 45 秒），向同一个 session 发送一次 closeout command。Agent 需要停止当前方向，回看自己的对话和 task-local CPS，并通过 `cps_publish(kind="termination_summary")` 发布尚未发布且可复用的内容，至少包括：

- 新发现；
- 反例或已经排除的方向；
- 验证反馈；
- 建议的下一步。

runner 只负责触发和核验是否真的写入 CPS，不读取整个 transcript，也不替 Agent 编造摘要。如果进程已经崩溃或被硬杀，没有可用 session，则只记录无法回收，不伪造内容。

## 具体的实验

实验分为两层：先用原版历史记录确认问题，再做同条件的真实 A/B 对比。

| 项目 | 设置 |
| --- | --- |
| 题目 | MathOlympiadBench 12 题 |
| 单次运行时长 | 3600 秒 |
| 模型 | `openai-codex/gpt-5.6-sol`，thinking=`max` |
| 调度配置 | `max_parallel=32`，每题初始 2 个 Agent、2 个 episode |
| Pi invocation timeout | 900 秒 |
| 正式 A/B 次数 | 2 轮；每轮一个 baseline、一个 treatment，共 4 次运行 |
| 实验变量 | baseline 关闭 `termination_summary`；treatment 仅打开该功能 |
| 其他条件 | 相同源码、镜像、题目顺序和模型配置；每个 arm 使用独立 Judge |
| checkpoint / mock | checkpoint transfer/publish 全部关闭；既有 Pi 进程 recovery 两臂保持不变；无 mock，运行真实 Pi 和 Judge |

四个 arm 的 contract 均标记 `mock_flags=none`，最终 Agent rows 的 `mocked=0`；四次 transport preflight 都是 `status=ok`，每套 Lean 栈有 `ready_workers=32`，四个 AUC 审计均为 `auc_match=true`。Judge/Lean runtime、输出和端口按 arm 隔离。这里的 preflight 只说明 transport 和 worker readiness；declaration index 仍未配置，不能扩大解释为所有 formal capability 都完整可用。

本次按实际运行要求没有等待外部负载清空，因此结果适合做方向判断，不能包装成完全无干扰的因果实验。原始运行数据和配置仍保留在受控归档中，PR 不附原始日志。

## 结论

1. **共享知识可见性有改善。** treatment 确实在终止阶段发出了额外总结，并把一部分原本只停留在对话或文件中的局部成果写入了 CPS。
2. **最终解题效果没有明显提升。** 新增两轮的 score 是一轮下降、一轮持平；两轮 nAUC 都低于 baseline，平均下降约 14.80%。因此不能把这项改动描述成 score/AUC 优化。
3. **当前实现还不能保证全量回收。** 有些 closeout 在 runner 审计窗口内没有完成，且事件与 CPS 写入之间存在晚到/对账问题。
4. **阶段性决策。** 如果目标是减少终止时局部成果完全丢失，这个方向值得保留；如果目标是提高最终成绩，当前数据不支持默认开启或宣称有性能收益。下一步应先修复收尾可靠性，再评估后续 Agent 是否真正采用这些总结。

## 支撑结论的数据和分析

### 1. 原版记录说明问题确实存在

原版三次 1 小时运行的可复核摘要如下：

| run | score | logical assignments | timeout / cancel / abnormal | CPS pieces / messages |
| --- | ---: | ---: | ---: | ---: |
| `20260901T012227Z-8c90d3f0` | 5/12 | 82 | 62 / 17 / 2 | 67 / 439 |
| `20260902T075657Z-eda06caf` | 4/12 | 76 | 64 / 10 / 0 | 74 / 434 |
| `20260902T090313Z-ecee9c07` | 5/12 | 82 | 64 / 15 / 1 | 70 / 375 |
| 合计 | — | **240** | **190 / 42 / 3** | **211 / 1248** |

这些记录里，235 个非成功 logical attempt 中有 206 个候选 `result.lean` 与题目 baseline 不同，说明运行中确实产生了大量局部尝试；但它们没有在结束边界被绑定成可检索的共享结论。消息审计还发现，1170 条定向消息中有 302 条是在 recipient 已结束后发送，说明“文件存在”或“消息发送成功”都不等于后续 Agent 能消费到知识。人工复核看到的典型缺口包括 `imo2024_p2` 的反例、`usa2024_p2` 的假下界，以及 `uk2024_r1_p1` 的 `rfl/decide` 失败。

### 2. 两轮真实 A/B 的最终效果

`nAUC` 是 3600 秒内按累计已证明题数计算的时间积分；越高表示越早得到更多正确证明。每个 arm 的数值都由独立 analyzer 重算，未把 summary 文本计入 AUC。

| replicate | score baseline → treatment | nAUC baseline → treatment | nAUC 差值 | raw AUC 差值 |
| --- | ---: | ---: | ---: | ---: |
| r1 | 6/12 → 5/12 | 0.24917126 → 0.21180470 | **-0.03736656** | -1614.235053 |
| r2 | 6/12 → 6/12 | 0.32907670 → 0.28084785 | **-0.04822885** | -2083.486156 |
| 两轮平均 | 6.0/12 → 5.5/12 | 0.28912398 → 0.24632628 | **-0.04279770（-14.80%）** | **-1848.860604（-14.80%）** |

补充的绝对时间和成本如下；第六个 proof 按 replicate 分开列出，避免把未完成的值当成普通平均数：

| 指标 | baseline | treatment | 差异/说明 |
| --- | ---: | ---: | --- |
| 首个 proof 平均时间 | 162.217s | 126.769s | treatment 早 35.447s |
| 第六个 proof（r1） | 2934.982s | 未完成 | treatment 少一个后续 proof |
| 第六个 proof（r2） | 1722.372s | 3083.298s | treatment 晚 1360.925s |
| 两轮平均 raw AUC | 12490.155879 | 10641.295275 | 约下降 14.80% |
| 两轮平均 logical assignments | 87.0 | 107.5 | +23.56% |
| 两轮平均 solver tokens | 2,776,441 | 3,260,669 | +17.44% |
| occupied slot-seconds 相对变化 | — | — | 约 +0.00076%，基本不变 |

因此，treatment 虽然更早得到第一道题，但没有抵消后续证明变慢或缺失造成的 AUC 损失；运行成本也没有下降。

### 3. 终止总结确实产生了新内容，但还不完整

两个 treatment 合计的终止回收记录是：

下面的 termination request 是 **process-attempt 级**计数，不是不同 logical Agent 的数量。既有 Pi 进程 recovery 没有作为本次变量，因此同一个 logical assignment 可能贡献多个 termination attempt；这也是 request、completion、publication 和 CPS piece 不能直接共用一个分母的原因。

| 指标 | 数量 |
| --- | ---: |
| eligible termination requests | 367 |
| request sent | 367/367（100%） |
| closeout completed | 190（51.77%） |
| publication events | 207（56.40%） |
| runner audit missing | 160（43.60%） |
| 最终 CPS `termination_summary` pieces | 215；每轮覆盖 12/12 题 |

所有最终 summary piece 都包含 `new_findings`、`counterexamples_or_ruled_out`、`validation_feedback`、`next_step` 四个结构段。这直接证明 treatment 不是只记录“命令已发送”，而是确实把一部分内容写进了共享 CPS。

另一方面，request、completion、publication event 和 CPS piece 不是同一个分母。按 unique `closeout_id` 对账，r1 有 3 个、r2 有 4 个 CPS closeout 在 runner 的 publication-event 审计边界内没有匹配项；终局重读时才发现对应 CPS row，表现为 late-write/receipt/drain race。因此这 3/4 个是“审计时未对上”的 closeout，不等同于永久丢失；同样，160 个 runner-missing 也不能被静默算成成功回收。这正是下一步需要修复的可靠性问题。

### 4. 结果的稳定性和适用范围

此前另一轮匹配 paired 实验曾得到 `+9.405%` 的 nAUC 差值；本次新增两轮没有复现，差值分别为 `-0.03736656` 和 `-0.04822885`。把三轮做描述性合并后，平均 nAUC 差值约为 `-6.65%`，但此前一轮等待过安静窗口，本次没有等待外部负载，条件并不完全同质，因此不能把这个合并当成统计显著性或严格因果结论。

本次四臂均为真实运行、`mocked=0`、checkpoint 关闭，AUC 独立重算一致，没有 OOM 或 `exit 137`；但四臂最终健康状态均为 `DEGRADED`，新增正式 replicate 仍然只有两轮，且尚未测量后续 Agent 是否读取和采用 summary。这些是当前结论的明确边界。

### 5. 后续建议

下一轮应优先做三件事：

1. 统一 closeout request、complete、publish 和 CPS piece 的 `closeout_id`，并增加持久化 receipt 与终局 drain/reconciliation；
2. 比较不同 grace window 对实际 publish capture 的影响，同时保留 timeout、cancel、error 的独立分母；
3. 增加 continuation/adoption 指标，观察后续 Agent 是否减少重复路线、避开已确认 blocker，再用更多同合同 replicate 评估 nAUC、score 和 token 成本。

原始证据未随 PR 提供。受控归档标识为 `quad-20260905:20260905T214412Z-quad2` 和 `paired-20260905:20260904T192316Z-2917853`；本报告保留了决策所需的聚合数据、分母和限制条件。
