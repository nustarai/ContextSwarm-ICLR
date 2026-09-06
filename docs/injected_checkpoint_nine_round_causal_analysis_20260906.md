# 九轮 GPT-6-Astra checkpoint A/B 的逐题因果分析

## 这份分析要回答什么

九轮 12 题配对实验的总体结果是 baseline `5.78/12`、treatment `5.22/12`，最终分差 `-0.56`；treatment 在 3 轮获胜、5 轮落后、1 轮持平。这个平均值只能说明方向，不能说明每个任务为什么改变。本文件把 19 个发生分数变化的 task-round 逐项对回：

- baseline 和 treatment 谁先证明，证明发生在人工注入信号之前还是之后；
- treatment 实际保存了多少 checkpoint、发生了多少 handoff/recovery；
- checkpoint 中记录的是可继续的数学进展、只有未完成的 partial，还是明确的 baseline-only/candidate-unavailable 状态；
- 最终候选 hash 是否曾经出现在保存记录或 handoff 记录中；
- 哪些结论有直接日志证据，哪些只能归入搜索路线随机性。

“checkpoint 已保存”只代表一个有界、未验证的快照写入 ledger；“checkpoint 已 materialize”只代表文件出现在 replacement workspace 的 `checkpoint/` 目录；Agent 是否读了它、是否把它复制成当前 `result.lean`、是否沿着它继续，以及它是否最终通过 Judge，分别需要独立证据。本分析没有把这几个状态合并成一个成功率。

## 最重要的发现：损失首先暴露的是 adoption 缺口

九轮 treatment 的 `events.jsonl` 一共包含 **721** 次 `checkpoint_handoff`，事件字段 `transfer_candidate=true` 为 **0**，`false` 为 **721**。这项数字需要按当前代码语义解释：formal arm 的 `candidate_transfer` capability 为 true，`prepare_workspace` 会把 runner 维护的 `state.best_candidate` 放到活动候选路径，同时把 checkpoint snapshot materialize 到 `workdir/checkpoint/`；事件里的 `transfer_candidate` 表达式记录的是“是否走另一条把 checkpoint raw candidate 直接覆盖 active candidate 的分支”，因此全为 false 不能直接证明 snapshot 没有写入。它证明的是：实验没有记录“checkpoint candidate 已成为 active candidate”的 adoption receipt。

代码链路是：

1. `checkpoint.py` 的 `materialize_for_agent` 总是写 `checkpoint.json`；`transfer_candidate=true` 时再校验 SHA-256 并写候选 snapshot。
2. `runner.py` 的 fresh assignment handoff 把 snapshot 放进 `workdir/checkpoint/`，活动 `result.lean` 仍由 `state.best_candidate` 分支决定。
3. 同一 actor 的 replacement recovery 在下一次 Pi 调用前再次 materialize 精确 snapshot，并通过 continuation prompt 告诉 Agent 去检查它。
4. 当前日志没有记录 Agent 真正执行了“读取 checkpoint snapshot → 复制/合并到 active result → 首次新 Judge check”的三步确认。

这正好对应失败任务中的多条原始上下文：

- r2 `imo2024_p2`：Agent 写出“checkpoint 有更强的 partial，但当前 `result.lean` 仍是 baseline sorry，能否由 runner seed strongest candidate”；
- r8 `imo2023_p3`：Agent 写出“checkpoint 声称有约 22KB candidate，但当前 `result.lean` 是 baseline sorry”；
- r9 `imo2023_p3`：Agent 写出“checkpoint 有 14811-byte partial，但当前 `result.lean` 被重置成 607-byte simp candidate”；
- r9 `imo2023_p4`：Agent 多次写出“workspace baseline-only、candidate inaccessible”；
- r7 `imo2023_p5`：Agent 最终把 baseline 记为 strongest candidate，并记录 self-recursion 已被 termination checker 拒绝。

因此，当前实验出现的下降可以由一个具体链路解释：snapshot 持久化成功，后续 Agent 仍需自行发现和采用 snapshot；在采用没有被强制完成、也没有被 receipt 记录的情况下，Agent 可能重新做已经做过的工作，或者在 baseline 上重新搜索。这个解释比“恢复一定把系统带到了错误状态”更符合日志。

## 逐轮结论

### r1：`5 → 3`，两项损失都在人工注入前已经分叉

变化只有 `imo2023_p4` 和 `imo2024_p2`。

- `imo2023_p4`：baseline 在约 `322.5s` 已 PROVED；treatment 保存 48 次、handoff 9 次、recovery prompt 7 次，最终只有 `COMPILES_WITH_SORRY`。baseline 证明早于约 1800s 注入窗口，因而这次分差不能归因于注入式中断。最符合的解释是 treatment 的自然 recovery/search 路线更早分叉，随后没有重新找到 baseline 的短路线。因果信心：**中等，属于路线差异；注入因果为低**。
- `imo2024_p2`：baseline 在约 `1723.0s` 已 PROVED，也早于注入窗口；treatment 保存 53 次、handoff 10 次、prompt 8 次，仍无证明。checkpoint context 留下了 partial，但 replacement 仍多次从可编译的未完成候选开始。因果信心：**中等，候选采用不足与路线差异共同解释**。

r1 说明 treatment 影响了自然运行中的 recovery 行为，但没有提供“人工杀进程造成下降”的证据。

### r2：`7 → 3`，四项损失中有两项具备 post-signal 证据，另外两项已经在 signal 前决定

- `imo2023_p3`：baseline 在约 `3099.4s` PROVED，treatment 没有证明，保存 37、handoff 6、prompt 6，并有 1 次 recovery succeeded。checkpoint 表明 forward direction 已经完成，reverse 仍是完整的 IMO classification；最终 hash 没有出现在 saved/handoff 集合。这里存在严格同题、注入后继续运行的比较，最合理的解释是 treatment 继承了“有实质 partial 但没有 converse”的状态，剩余 formalization 仍未完成，recovery 没有把它变成可用 active candidate。因果信心：**中等偏高，机制缺口证据较强；不能证明错误状态本身导致失败**。
- `imo2024_p1`：baseline 在约 `3086.6s` PROVED；treatment 保存 46、handoff 7、prompt 7，有 1 次 recovery succeeded。checkpoint 中有 floor/congruence 路线和 forward partial，但没有最终 reverse proof。两边都经历了长时间搜索，treatment 没有达到 Judge proof。因果信心：**中等，未完成 formal route 加上 deadline**。
- `imo2024_p2`：baseline 在约 `1156.4s` 已 PROVED，早于注入；treatment 保存 48、handoff 8、prompt 8。checkpoint 文本直接记录“更强的 partial 存在，但当前 result.lean 仍是 baseline sorry”。这项是最清楚的 adoption 缺口案例之一，但不是人工注入导致的 loss。因果信心：**高，针对候选没有进入 active workspace 有直接上下文证据**。
- `imo2024_p6`：baseline 在约 `2183.4s` PROVED；treatment 保存 47、handoff 7、prompt 8，有 algebraic helper partial，最终 hash 同时出现在 saved 和 handoff hash 集合，但 treatment 仍未证明。hash presence 说明字节快照存在，不能证明 replacement Agent 采用了它。因果信心：**中等，partial 尚未闭合、采用不可见和 deadline 共同作用**。

r2 是总体下降最大的轮次，但四项并非同一种失败：两项已经由 signal 前后的路线差异决定，另外两项显示了 snapshot 到 active proof 之间仍有缺口。

### r3：`5 → 7`，两项提升都不能归因于注入式 recovery

r3 的 treatment 目标进程没有找到，严格中断合同不成立。

- `imo2023_p4`：treatment 在约 `2168.0s` PROVED，checkpoint 29 次；baseline 没有证明。因为目标注入缺失，且 treatment 的 proof 属于自然搜索路径，不能把提升标成 checkpoint recovery 的效果。
- `imo2023_p5`：treatment 在约 `708.9s` PROVED，几乎没有 handoff/recovery（9 个 save，0 handoff，0 prompt）；这更像一次早期路线命中，而不是 checkpoint 带来的延续。

r3 的正分差是随机路线证据，不是机制增益证据。

### r4：`4 → 6`，一个早期路线命中，一个有合理但未闭合的 recovery 受益假设

- `imo2023_p4`：treatment 在约 `536.4s` PROVED，只有 6 个 save、0 handoff、0 prompt；提升发生在注入前，属于早期搜索路线命中。
- `uk2024_r1_p1`：baseline 没有证明，treatment 在约 `2992.2s` PROVED，保存 48、handoff 6、prompt 9。checkpoint context 包含 `Good` subtype、`lift/restrict`、固定点和递归结构等已编译 helper，后续 treatment 最终通过 Judge。这个任务是严格同题、注入后仍然发生 proof 的正例，说明有限上下文可能帮助 Agent 避免从空白开始；但 final proof hash 没有出现在 saved/handoff hash 集合，也没有 adoption receipt，所以只能说“具有机制上的合理性”，不能把提升直接归因给 checkpoint。

### r5：`6 → 5`，单项 post-signal loss，partial 仍停留在 helper 层

`imo2024_p6` 的 baseline 在约 `2192.0s` PROVED；treatment 保存 43、handoff 7、prompt 6。checkpoint 中记录了 `f(0)=0`、injectivity、surjectivity、固定点等 helper，主定理仍是 sorry/未完成。treatment 没有把 helper 集合闭合成全局分类，也没有 proof。这个结果符合“恢复保留了局部成果，但局部成果不足以在剩余时间完成 theorem”的解释；日志没有证明恢复到了错误命题。因果信心：**中等**。

### r6：`5 → 7`，是最有说服力的 recovery 正例，但仍缺少 adoption receipt

- `imo2023_p5`：严格同题。baseline 没有证明；treatment 在约 `3071.4s` PROVED，保存 37、handoff 11、prompt 3，有 1 次 recovery succeeded。checkpoint context 记录了动态 `2^{-f}` min-weight invariant、dyadic upper coloring，以及之后“upper branch complete、lower branch remains sorry”的精确 formal progress。这个上下文告诉后续 Agent 哪些工作已经完成、哪条 lower-bound 路线仍然是唯一缺口，明显比空白重启更有用。它是 checkpoint 可能帮助延续的最强正例。可是最终 proof hash 没有出现在 saved/handoff 集合，因此还不能证明最后的 `PROVED` 文件就是由该 snapshot 直接恢复而来。
- `imo2024_p2`：treatment 在约 `281.2s` PROVED，只有 12 个 save、0 handoff、0 prompt；属于注入前的早期命中，与 recovery 无关。

r6 的结果支持“正确的 partial context 可能帮助继续”，但需要后续记录 adoption 和首次新 Judge check 才能把可能性变成因果证据。

### r7：`7 → 6`，与 r6 同一目标形成反向配对

`imo2023_p5` 是 baseline 在约 `3376.0s` PROVED、treatment 没有证明的严格同题比较。treatment 保存 42、handoff 15、prompt 1，并有 1 次 recovery succeeded。checkpoint 反复记录 baseline 是 strongest compiling candidate、self-recursion 已被 termination checker 拒绝，说明 recovery 知道哪些捷径不可用，却没有新的可验证 lower-bound proof。

这项与 r6 的同题结果相反：同一题、同一 model、同一 treatment，r6 treatment 成功，r7 treatment 失败。它直接说明“checkpoint 只要存在就会稳定提升”不成立；最终能否把 partial 转成 proof，仍受当轮 Agent 选路、formalization 速度和剩余时间影响。这里最合理的分类是**随机路线 + partial 不足**，而非“恢复状态一定错误”。

### r8：总分持平，变化方向相互抵消，且严格注入不成立

r8 treatment 的目标进程没有找到，因此不进入严格中断子集。

- `imo2023_p3`：baseline 在约 `3254.4s` PROVED；treatment 没有证明，checkpoint context 明确说约 22KB candidate 存在但当前 `result.lean` 是 baseline sorry。此项是 candidate adoption 缺口的强观察，但不能归因于人工 signal。
- `imo2023_p5`：treatment 在约 `1158.6s` PROVED，13 个 save、3 个 handoff、0 个 prompt，属于早期自然命中。

两项一负一正抵消成总分 0；这轮不能支持恢复有利或有害的单方向结论。

### r9：`6 → 3`，三项都是 baseline 先在 signal 前证明，treatment 的 workspace 证据最清楚

- `imo2023_p3`：baseline 在约 `1624.4s` PROVED；treatment 保存 43、handoff 7、prompt 7。最终 checkpoint 写明“没有可访问的 partial candidate”；Agent 还请求恢复 14811-byte candidate。最终 hash 虽然出现在 saved 和 handoff hash 集合，但这仍只是 hash presence，当前 workspace 仍曾回到小的 simp candidate。因果信心：**高，candidate 可见性/adoption 问题直接出现；人工注入不是主要原因**。
- `imo2023_p4`：baseline 在约 `1623.5s` PROVED；treatment 保存 46、handoff 6、prompt 8。多个 checkpoint 记录 workspace baseline-only、需要恢复 `hstep/K/hEqA/hnoadj/hpair` 等 partial。最终 hash 没有出现在 saved/handoff 集合。因果信心：**高，workspace 处于 baseline-only 的证据明确**。
- `uk2024_r1_p1`：baseline 在约 `494.0s` PROVED；treatment 最终 `VERIFY_FAIL`，保存 47、handoff 6、prompt 8。baseline 的 proof 远早于 signal，treatment 的失败来自更早的 formal route 分叉；不能将其解释成 pretermination checkpoint 把已完成 proof 覆盖掉。

r9 的大幅下降主要是 treatment 自然路线没有复现 baseline 的早期 proof，并叠加了两项明确的 candidate 不可用记录。

## 变化任务的汇总表

下表中的“解释”是证据分级：A 表示时间和 checkpoint 文本有直接支持；B 表示机制上合理但缺少 adoption receipt；C 表示主要只能判定为搜索路线随机性。

| 轮次 / 题目 | Δ(T−B) | B/T 最终状态与首个 proof | treatment save / handoff / prompt | 直接观察 | 最合理归因 | 证据级别 |
| --- | ---: | --- | ---: | --- | --- | --- |
| r1 `imo2023_p4` | -1 | B PROVED 322.5s / T 未证明 | 48 / 9 / 7 | B 早于 signal | 自然路线分叉、恢复开销 | C |
| r1 `imo2024_p2` | -1 | B PROVED 1723.0s / T 未证明 | 53 / 10 / 8 | T 有 partial、未闭合 | partial 不足 + 路线分叉 | B |
| r2 `imo2023_p3` | -1 | B PROVED 3099.4s / T 未证明 | 37 / 6 / 6 | forward 已完成，reverse 未完成 | continuation 没把 partial 变成 proof | A/B |
| r2 `imo2024_p1` | -1 | B PROVED 3086.6s / T 未证明 | 46 / 7 / 7 | arithmetic/floor 路线仍有缺口 | formalization 未完成 + deadline | B |
| r2 `imo2024_p2` | -1 | B PROVED 1156.4s / T 未证明 | 48 / 8 / 8 | “更强 partial”仍未 seed 到当前文件 | adoption 缺口 | A |
| r2 `imo2024_p6` | -1 | B PROVED 2183.4s / T 未证明 | 47 / 7 / 8 | helper partial，hash 有记录 | theorem 未闭合、采用不可见 | B |
| r3 `imo2023_p4` | +1 | B 未证明 / T PROVED 2168.0s | 29 / 6 / 3 | 目标 signal 缺失 | 普通路线命中 | C |
| r3 `imo2023_p5` | +1 | B 未证明 / T PROVED 708.9s | 9 / 0 / 0 | 早期 proof | 普通路线命中 | C |
| r4 `imo2023_p4` | +1 | B 未证明 / T PROVED 536.4s | 6 / 0 / 0 | 早于 signal | 普通路线命中 | C |
| r4 `uk2024_r1_p1` | +1 | B 未证明 / T PROVED 2992.2s | 48 / 6 / 9 | context 有 compiled structural helpers | 可能受益于 continuation | B |
| r5 `imo2024_p6` | -1 | B PROVED 2192.0s / T 未证明 | 43 / 7 / 6 | helper 层 progress，主定理未闭合 | partial 不足 + deadline | B |
| r6 `imo2023_p5` | +1 | B 未证明 / T PROVED 3071.4s | 37 / 11 / 3 | min-weight/upper coloring context | 可能受益于正确 continuation | B |
| r6 `imo2024_p2` | +1 | B 未证明 / T PROVED 281.2s | 12 / 0 / 0 | 早期 proof | 普通路线命中 | C |
| r7 `imo2023_p5` | -1 | B PROVED 3376.0s / T 未证明 | 42 / 15 / 1 | baseline strongest、self-recursion rejected | 路线随机性 + partial 不足 | B/C |
| r8 `imo2023_p3` | -1 | B PROVED 3254.4s / T 未证明 | 56 / 15 / 7 | partial 存在但当前文件 baseline-only | adoption 缺口 | A |
| r8 `imo2023_p5` | +1 | B 未证明 / T PROVED 1158.6s | 13 / 3 / 0 | 早期 proof，目标 signal 缺失 | 普通路线命中 | C |
| r9 `imo2023_p3` | -1 | B PROVED 1624.4s / T 未证明 | 43 / 7 / 7 | 无可访问 partial、workspace 曾回到小 candidate | candidate 可见性/adoption | A |
| r9 `imo2023_p4` | -1 | B PROVED 1623.5s / T 未证明 | 46 / 6 / 8 | baseline-only、candidate inaccessible | candidate 可见性/adoption | A |
| r9 `uk2024_r1_p1` | -1 | B PROVED 494.0s / T VERIFY_FAIL | 47 / 6 / 8 | B 远早于 signal | 早期路线分叉 | C |

19 个变化 task-round 中，12 个下降、7 个上升，净变化 `-5` 分。严格同题注入轮次 r2、r4、r5、r6、r7、r9 中，变化任务为 13 个，其中 9 个下降、4 个上升；按“baseline proof 在 signal 后、且 treatment 也运行到 signal 后”的更窄 post-signal 子集，观察到 5 个下降、2 个上升。这仍然是小样本观察，不能作为显著性检验。

## 同一题跨轮次：`imo2023_p5` 说明结果确实具有随机性

`imo2023_p5` 在九轮中的变化为：

- r3：treatment 早期 PROVED，baseline 未证明；目标 signal 缺失；
- r6：严格同题，treatment 在约 3071s PROVED，baseline 未证明；checkpoint 带有实质 upper/lower 数学上下文；
- r7：严格同题，baseline 在约 3376s PROVED，treatment 未证明；checkpoint 记录 baseline strongest、self-recursion 失败；
- r8：treatment 在约 1159s PROVED，目标 signal 缺失；
- 其他轮次：双方最终都没有形成 score 差。

同一题的 r6/r7 一正一负是关键证据。状态保留的价值取决于 partial 是否真实接近可完成证明、后续 Agent 能否读取并采用它，以及剩余时间是否够完成最后的 Lean/Judge 工作。fresh restart 也有机会碰到另一条有效路线。当前数据支持“结果具有路径随机性”，同时暴露“snapshot 到 active candidate 的采用链路还不完整”。

## 为什么 treatment 的总体成绩会变差

有三个可分别验证的因素：

1. **treatment 改变了运行过程，而不是只在最后多写一个文件。** 九轮 treatment 的 solver total tokens 比 baseline 高约 `27.7%`，input tokens 高约 `36.4%`；平均 attempts 反而低约 `7.7%`，slot-seconds 基本相同。recovery prompt、checkpoint context 和重复的 Judge/编译检查增加了输入和处理负担。
2. **局部成果未必进入活动候选。** 保存的 snapshot 仍是 `unverified`，Agent 需要主动读取和验证。失败上下文反复显示“partial 在 checkpoint，但当前 result.lean 是 baseline-only”。这种状态会让 recovery 同时承担恢复和重新发现文件的成本。
3. **正确 partial 也可能不够完成定理。** r6 的 `imo2023_p5` 有很好的 invariant/upper coloring 进展仍要补 lower branch；r5 的 `imo2024_p6` 有多个 algebraic helper 仍缺全局分类；r7 的同题 checkpoint 记录了多个失败路线但没有新的 proof。保存更多信息不会自动增加剩余 formalization 时间。

因此，下降不能概括为“系统恢复了错误状态”。更准确的描述是：当前 recovery 同时引入了额外上下文成本，保留了未经验证的 partial，并把最终采用交给了 Agent；在某些轮次它保留了有用方向，在另一些轮次它让 Agent 花时间处理无法直接使用的 snapshot。fresh restart 偶尔走到更好的路线，正是同一搜索空间随机性的另一面。

## 这份分析能确认什么，不能确认什么

可以确认：

- checkpoint save、CPS publish、fresh handoff 和 actor recovery 在 treatment 中确实发生；
- checkpoint 中包含候选 hash、候选大小、有限 completed/ruled-out/next-step context；
- 多个 failure 明确暴露了“partial 已保存但当前 workspace baseline-only/candidate inaccessible”；
- 同题跨轮正负相反，说明恢复不会稳定地产生单方向成绩变化；
- 当前 checkpoint 运行路径伴随显著 input/total token 增加。

仍不能确认：

- 哪个具体 Agent 读取了哪个 snapshot；
- Agent 是否把 snapshot 复制或合并进了 active `result.lean`；
- 最终 PROVED 候选是否由某个 checkpoint byte-for-byte 延续而来；
- 某一次下降是否由某个错误 lemma、错误 route 或错误 checkpoint 直接造成。

最后三项缺少 `checkpoint_adopted`、`candidate_materialized_hash`、`first_post_recovery_edit`、`first_post_recovery_judge_check` 和 `adoption_to_proof` 事件。因此当前数据适合做机制定位和逐项归因，不能把所有分差写成严格的单步因果证明。

## 下一步改动和实验建议

恢复链路值得保留，但需要把“保存”和“采用”分开实现并记录。最小改动是让 replacement Agent 启动前拥有明确的 candidate path contract：runner materialize snapshot 后，在 prompt 中给出精确文件路径、SHA-256 和复制命令；Agent 首次成功读取后写 `checkpoint_read`，复制到 active candidate 后写 `checkpoint_adopted`，runner 再记录 `candidate_materialized_hash`。随后必须紧接一次 fresh `judge_check`，把这四个事件和 proof hash 连接起来。

下一次对比实验应至少记录以下 task/process-attempt 指标：

- checkpoint save success；
- snapshot materialize success；
- Agent read success；
- active candidate adoption success；
- adoption 后首次编辑时间；
- adoption 后首次 Judge check 状态；
- 重复路线数和命中 ruled-out blocker 的次数；
- proof 是否 byte-for-byte 来自 adopted snapshot 的延续；
- score、proof time、input tokens、total tokens。

实验条件可以保留当前 B/T，再增加一个只允许 fresh restart 的 recovery 对照，或增加一个由 runner 自动 adopt snapshot、Agent 只负责继续 proof 的 treatment。这样才能分离三件事：保存本身的价值、自动采用的价值、以及 Agent 自主选择 fresh route 的价值。

## 证据位置

- 九轮总体结果：`docs/injected_checkpoint_nine_round_ab_gpt6_astra_20260906.md`。
- 逐题逐轮结构化分析：`evidence/per_task_detailed.json`。
- 19 个变化 task-round 的压缩表：`evidence/per_task_score_deltas.tsv`。
- handoff transfer flag、逐题分类和限制：`evidence/causal_analysis_summary.json`。
- 原始每 arm `final.json`、`events.jsonl`、`scoreboard_history.jsonl`、CPS checkpoint body 和 profiling audit：对应九轮 evidence bundle。

本文件只记录分析，没有修改 checkpoint 实现，也没有新增数学实验。
