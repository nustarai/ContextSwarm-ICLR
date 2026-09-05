# 超时前强制保存 checkpoint 对 MathOlympiadBench 的九轮 GPT-6-Astra A/B 结果

## 背景和动机

这项改动的目标是：Agent 在 timeout、cancel、live RPC error 或进程被终止前没有完成最终证明时，已经产生的候选、尝试记录和有限证明上下文仍然能够交给后续进程。后续 Agent 应该知道已经做过什么、哪些方向已经失败、当前可以从哪里继续，而不是因为 recovery 重新从空白状态开始。

前六轮的结果已经显示 checkpoint 能稳定保存和交接局部状态，但 score 方向仍然随搜索轨迹变化，样本量不足以判断追加运行是否会改变结论。本报告加入额外的 r7–r9 三轮，把总数扩展到九个配对 replicate，分别观察机制是否继续可靠发生、最终数学成绩是否改善、以及成本和健康边界。

## 具体改了什么

baseline 关闭 `checkpoint.enabled`、`checkpoint.transfer` 和 `checkpoint.publish`；treatment 打开这三个开关。treatment 在进程边界保存不可变候选快照、候选 SHA-256、终止原因和有限上下文，并在后续 assignment 前提供 handoff/recovery prompt。checkpoint 始终标记为未验证，必须经过新的 Judge 检查才能进入有效证明；CPS 共享知识发布与 checkpoint 交接是两条不同链路，本实验没有把 checkpoint publication 或 CPS 消息当作证明采用。

这次追加运行没有改变 checkpoint 实现或模型，只复用了已经冻结的实现镜像和实验配置。新增的三轮为独立的 Judge/Lean 栈，并在每轮结束后停止和清理；动态端口只属于内部运行证据，没有作为结果写入本报告。

## 具体的实验

九轮都是固定 12 题的配对 A/B。每个 arm 使用 3600 秒 solver horizon、32 个并发 slot、每题 2 个初始 Agent、每题 2 个 episode、profiling 开启、Judge result cache 关闭和同一任务顺序。模型字符串为 `openai-codex/gpt-6-astra`，两边使用同一源码 commit `587543f` 和同一镜像 digest（完整值见批次合同）。baseline 使用 `configs/formal_1h_cps32_profiled_gpt6_astra.toml`，treatment 使用 `configs/formal_1h_cps32_profiled_checkpoint_gpt6_astra.toml`。

每轮启动后约 1800 秒，注入器尝试向两边同一 `imo2023_p5` Pi 进程发送 SIGTERM；5 秒内未退出才升级 SIGKILL，不替换目标任务。严格同题双边注入的轮次是 r2、r4、r5、r6、r7、r9。r1 使用了较早的手工注入合同，不能证明同题双边匹配；r3 treatment 和 r8 treatment 都没有找到活跃的目标题进程，因此按合同保留原始证据、排除严格中断子集，不用其他题目替换。

r7–r9 的批次合同、每轮端口选择记录、六个 arm 的 `final.json`、注入证据、profiling audit 和九轮汇总分别保存在内部 evidence bundle `checkpoint-gpt6-astra-real-ab-r7-r9-20260906` 及其 `batch-contract.json`、`evidence/nine-round-summary.json`、`evidence/compare-runs-nine-rounds.txt`。此前六轮的证据 bundle 仍保持不变。

## 结论

1. **保存和交接机制在九轮中持续可靠发生。** baseline 九轮的 checkpoint save、handoff 和 recovery prompt 均为零；treatment 每轮都保存 checkpoint，数量为 `444、405、433、412、412、468、478、455、406`，平均 `434.8` 次。treatment 九轮共保存 `3913` 次、handoff `721` 次、recovery prompt `498` 次；r7–r9 分别保存 `478、455、406` 次，并产生 `111、90、65` 次 handoff。保存计数说明局部状态确实被持久化并送达后续 assignment，不等价于候选被采用，也不等价于最终证明。

2. **九轮没有显示 checkpoint 带来稳定的成绩提升；追加三轮反而继续呈现较大的随机波动。** 九轮 score 差值（treatment−baseline）依次为 `-2、-4、+2、+2、-1、+2、-1、0、-3`，treatment 赢 3 轮、输 5 轮、平 1 轮。平均 score 为 baseline `5.78/12`、treatment `5.22/12`，差值 `-0.56` 分（相对 baseline 约 `-9.6%`）。平均 normalized score-time AUC 为 `0.29405` 对 `0.27481`，差值 `-0.01924`（相对约 `-6.5%`）。新增 r7–r9 的平均 score 差值为 `-1.33`，nAUC 差值为 `-0.01819`；它没有把前六轮的近零方向推向稳定正收益，反而使九轮总体均值更偏向 baseline。

3. **成本增加是比成绩方向更一致的信号。** 九轮平均 solver total tokens 从 `1.993M` 增至 `2.546M`，增加 `27.7%`；input tokens 从 `1.585M` 增至 `2.162M`，增加 `36.4%`。平均 solver attempts 从 `134.4` 降到 `124.1`，但平均 solver slot-seconds 基本相同（`115198.31` 对 `115197.99`）。在当前实现和 prompt 形态下，checkpoint recovery 更像可靠性和可恢复性层，会扩大上下文和输入成本，不能当作默认的性能或成本优化。

4. **这不能简单解释为“恢复状态一定是错的”。** 严格注入六轮的 score 差值为 `-4、+2、-1、+2、-1、-3`，平均 `-0.83`；nAUC 平均差值为 `-0.01985`。正负方向同时出现，说明搜索路径、候选质量和 recovery 时机共同影响结果。当前日志还没有逐次证明“某个 checkpoint 被采用后导致某个错误方向”的语义因果链，所以不能把每一次下降归因于恢复了错误状态；同样，也不能据此断言 fresh restart 必然更好。

5. **建议保留持久化能力，但不要把它变成唯一的 recovery 路线。** 对用户最初的目标，保存候选、哈希、终止原因、尝试记录和有限上下文是必要的；下一步应继续保留这条可靠性链路，同时保留 fresh exploration 分支，把 checkpoint 当作可验证的起点而不是必须沿用的路线。后续实现应记录 checkpoint adoption、恢复后首次新编辑、首次新 `judge_check`、重复路线率和 blocker 命中率，只有在这些指标与最终 proof 建立可追溯关系后，才适合改变候选优先级或强制恢复策略。

## 支撑结论的数据和分析

### 九轮逐轮结果

| replicate | baseline score | treatment score | Δ T−B | baseline nAUC | treatment nAUC | Δ nAUC | 首个 proof（B/T 秒） | solver total tokens（B/T） | checkpoint saved（B/T） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| r1 | 5 | 3 | -2 | 0.21986297 | 0.12814466 | -0.09171831 | 81.20 / 79.70 | 2,947,685 / 3,339,675 | 0 / 444 |
| r2 | 7 | 3 | -4 | 0.25225792 | 0.18629280 | -0.06596512 | 117.01 / 114.33 | 2,262,245 / 2,903,804 | 0 / 405 |
| r3 | 5 | 7 | +2 | 0.33822051 | 0.36642946 | +0.02820895 | 89.05 / 75.90 | 1,460,581 / 2,096,782 | 0 / 433 |
| r4 | 4 | 6 | +2 | 0.21246700 | 0.28529134 | +0.07282434 | 100.54 / 536.38 | 1,393,622 / 1,791,616 | 0 / 412 |
| r5 | 6 | 5 | -1 | 0.30057433 | 0.17251839 | -0.12805594 | 95.24 / 104.16 | 1,356,969 / 2,123,979 | 0 / 412 |
| r6 | 5 | 7 | +2 | 0.32525074 | 0.39137041 | +0.06611967 | 172.46 / 89.36 | 2,369,453 / 2,717,836 | 0 / 468 |
| r7 | 7 | 6 | -1 | 0.27678701 | 0.33478112 | +0.05799411 | 168.51 / 258.29 | 2,438,941 / 3,173,459 | 0 / 478 |
| r8 | 7 | 7 | 0 | 0.39487346 | 0.40433488 | +0.00946142 | 148.08 / 284.82 | 2,064,278 / 2,600,222 | 0 / 455 |
| r9 | 6 | 3 | -3 | 0.32619294 | 0.20416920 | -0.12202374 | 230.62 / 149.87 | 1,646,987 / 2,163,640 | 0 / 406 |
| **九轮平均** | **5.78** | **5.22** | **-0.56** | **0.29405410** | **0.27481470** | **-0.01923940** | — | **1.993M / 2.546M** | **0 / 434.8** |

r7–r9 的单轮结果分别是 `-1、0、-3` 分；r7 treatment 的 nAUC 较高但最终 score 较低，r8 两边 score 相同而 treatment nAUC 略高，r9 treatment 的 score 和 nAUC 都较低。这说明“保留了更多候选”与“最终证明更多或更早”之间没有稳定的一步映射。

### 机制、结果、成本和健康分层

| 指标 | baseline 九轮 | treatment 九轮 | 解释 |
| --- | ---: | ---: | --- |
| checkpoint saved（每轮 B/T） | 0 / 0 / 0 / 0 / 0 / 0 / 0 / 0 / 0 | 444 / 405 / 433 / 412 / 412 / 468 / 478 / 455 / 406 | runner 保存事件，不是证明数 |
| checkpoint handoff（每轮 B/T） | 0 | 92 / 69 / 71 / 66 / 65 / 92 / 111 / 90 / 65 | 后续 assignment 的交接次数 |
| recovery prompt（每轮 B/T） | 0 | 53 / 60 / 58 / 59 / 60 / 52 / 46 / 52 / 58 | 实际注入 continuation prompt 的 assignment 数 |
| checkpoint published（每轮 B/T） | 0 | 222 / 207 / 195 / 198 / 203 / 199 / 200 / 187 / 208 | CPS piece 发布，不等于候选采用 |
| solver attempts 平均 | 134.4 | 124.1 | treatment 低约 7.7% |
| solver input tokens 平均 | 1.585M | 2.162M | treatment 高约 36.4% |
| solver total tokens 平均 | 1.993M | 2.546M | treatment 高约 27.7% |
| solver slot-seconds 平均 | 115198.31 | 115197.99 | 两边基本相同 |

九个 baseline 和九个 treatment arm 的最终状态均为 `DEGRADED`。r7–r9 的 Judge probe infrastructure error 计数分别为 baseline/treatment `5/4、1/2、3/1`；不同 arm 还出现 solver process error、solver cancellation 或 timeout。没有 OOM 或 exit 137。健康问题是两臂共同存在的运行边界，不能把它们直接解释为 checkpoint 引入的退化，但它降低了细粒度生命周期和成本差异的外推强度。

### 严格中断子集和 profiling 限制

严格同题双边注入子集为 r2、r4、r5、r6、r7、r9，共 6 个配对 replicate。score 差值为 `-4、+2、-1、+2、-1、-3`，平均 `-0.83`；nAUC 差值平均 `-0.01985`。r8 treatment 的注入证据明确记录为 `target_task_process_not_found`，因此没有把这一轮放入严格集合，但它的完整 workload 仍然保留在九轮总体统计中。

r7–r9 的 profiling audit 都确认输入是 real run，profile/horizon/drain/closeout 生命周期完整，但 audit 仍然返回 `ok=false`：每个 arm 都有 dropped fields 和一个或两个未闭合 span；treatment 另有 unknown event，三轮分别为 `1954/1874/1762`，对应 baseline 为零。dropped-field 总量为 r7 `2104/1960`、r8 `2044/1720`、r9 `1644/1456`（均为 baseline/treatment）。因此 profiling 可用于说明生命周期和覆盖范围，不能把未清洗的细粒度事件计数当作无偏的性能证据。

原始日志、逐 arm `final.json`、profiling audit、注入证据、端口选择记录和汇总 JSON 由内部 evidence ID `checkpoint-gpt6-astra-real-ab-20260905`、`checkpoint-gpt6-astra-real-ab-r2-r3-20260905`、`checkpoint-gpt6-astra-real-ab-r4-r6-20260905`、`checkpoint-gpt6-astra-real-ab-r7-r9-20260906` 标识。九轮样本足以说明机制保存可靠、成绩方向不稳定且成本偏高，但仍不足以声称统计显著，或建立 checkpoint adoption 到最终 proof 的因果链。
