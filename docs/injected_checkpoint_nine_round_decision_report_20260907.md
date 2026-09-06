# 超时前强制保存 checkpoint 对 MathOlympiadBench 的九轮 GPT-6-Astra 恢复效果和成绩影响

## 背景和动机

此前的 recovery 在 Agent 被 timeout、cancel、live RPC error 或进程终止后，能够继续启动后续进程，但后续 Agent 经常只看到共享 workspace 的当前文件，缺少上一进程已经完成的局部证明、已经排除的路线和下一步建议。这样会让 replacement Agent 重复尝试已经失败的方向，也可能把已经存在但未经验证的 partial 当成不存在。

本实验评估的具体问题是：在一小时 MathOlympiadBench 运行中，超时或终止前保存 checkpoint，是否能让后续进程更好地延续已有工作，并因此改善最终证明数、证明速度或 score。实验还需要区分四种结果：checkpoint 是否保存、后续 assignment 是否拿到、Agent 是否采用候选、候选是否通过 Judge。只有最后一个层次属于数学结果。

## 具体改了什么

baseline 关闭 checkpoint save、transfer 和 publish。treatment 在 timeout、cancel、live error 或进程即将关闭的边界保存不可变 checkpoint，其中包含候选文件、候选 SHA-256、候选大小、终止原因以及有界的 `completed_work`、`ruled_out` 和 `next_step` 上下文。后续 assignment 可以在 `workdir/checkpoint/` 看到 `checkpoint.json` 和候选 snapshot；同一 actor 的 replacement 还会在下一次 Pi 调用前 materialize 对应 snapshot，并收到 continuation prompt。

checkpoint 始终是 `unverified`，不能直接作为证明或计分候选。CPS shared-knowledge publication 与 checkpoint/recovery 是两个独立机制，本实验没有把 CPS piece 的发布当成数学证明，也没有改变 Judge 的 score 规则。

本报告特别区分了“snapshot 已写入”与“snapshot 已采用”。当前 runner 会把 checkpoint snapshot 放在 `checkpoint/` 目录，同时由 `state.best_candidate` 决定活动的 `result.lean`。实验没有记录 Agent 已读取 snapshot、把它复制或合并到活动候选、并从该候选发起首次新 Judge check 的完整回执链。

## 具体的实验

| 项目 | 设置 |
| --- | --- |
| 实验问题 | 超时前 checkpoint 是否改善 recovery 的延续性、最终 score 或证明效率 |
| 任务范围 | 固定 12 道 MathOlympiadBench 题，每轮 baseline/treatment 使用同一题目顺序 |
| 运行时长 | 每个 arm 的 solver horizon 为 3600 秒 |
| 重复次数 | 9 个 paired replicate，每轮 12 题，合计 18 个 arm |
| baseline / treatment | 唯一预期变量是 checkpoint save/transfer/publish 开关；baseline 全关闭，treatment 全开启 |
| 模型与版本 | `openai-codex/gpt-6-astra`；两边使用同一源码和镜像合同 |
| 并发与 solver | 32 个并发 slot、每题 2 个初始 Agent、每题 2 个 episode、同一 Judge/Lean 合同 |
| 观测与注入 | profiling 开启、Judge result cache 关闭；约 1800 秒尝试向两边同题 `imo2023_p5` 发送终止信号 |
| 严格注入子集 | r2、r4、r5、r6、r7、r9；r1 不是同题匹配，r3 和 r8 的 treatment 目标进程未找到 |
| 真实性 | 真实 Pi/Judge/Lean 运行；不是 mock score 或 schema-only harness |

r1 使用了较早的手工注入合同，不能用于严格同题中断推断。r3 和 r8 的完整 workload 仍计入九轮总体结果，但目标进程缺失，因此不把它们当成严格 injection treatment。所有 arm 都保留原始运行结果；没有用其他任务替换未找到的注入目标。

## 结论

1. **机制层：** checkpoint 持久化和交接在九轮中稳定发生。treatment 每轮都产生 save、handoff 和 recovery prompt，说明局部状态具备写入和传递能力。但这项实验没有证明后续 Agent 一定读取或采用候选。九轮共记录 3913 次 `checkpoint_saved`、721 次 `checkpoint_handoff` 和 498 次 recovery prompt；721 次 handoff 的 `transfer_candidate=true` 为 0。这个字段在当前 formal arm 中记录的是“是否走 raw candidate 覆盖 active candidate 的另一分支”，所以它不能单独证明 snapshot 没有 materialize；它说明采用 active candidate 的回执没有出现。
2. **结果层：** 当前数据没有显示稳定的 score 或证明效率提升。九轮 baseline 平均 score 为 `5.78/12`，treatment 为 `5.22/12`，差值为 `-0.56`；normalized score-time AUC 为 `0.29405` 对 `0.27481`，差值为 `-0.01924`。treatment 3 轮获胜、5 轮落后、1 轮持平。19 个发生变化的 task-round 中有 12 个下降、7 个上升。这个下降不能归结为“恢复状态必然错误”：同一道 `imo2023_p5` 在 r6 treatment 获胜、r7 treatment 落后，说明搜索路线和剩余 formalization 时间具有明显随机性。
3. **成本与可靠性：** treatment 平均 solver total tokens 增加约 `27.7%`，input tokens 增加约 `36.4%`；平均 attempts 下降约 `7.7%`，slot-seconds 基本相同。18 个 arm 最终都带有 `DEGRADED` 健康状态；没有 OOM 或 exit 137，但存在 Judge probe、solver process、timeout 和 profiling audit 问题。因此 checkpoint 在当前形态下更像可靠性和可恢复性机制，不能当作默认的性能或成本优化。
4. **决策：** 保留 checkpoint 持久化能力，但暂不把它宣布为数学性能提升，也不建议强制后续 Agent 沿用未经验证的 snapshot。下一步应补齐 `checkpoint_read`、`checkpoint_adopted`、`candidate_materialized_hash`、`first_post_recovery_judge_check` 和 `adoption_to_proof` 回执，并用 fresh restart、Agent 自主采用、runner 自动采用三个条件分离保存价值、采用价值和新路线探索价值。

## 支撑结论的数据和分析

### 九轮结果对比

| replicate | baseline score | treatment score | Δ T−B | baseline nAUC | treatment nAUC | Δ nAUC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| r1 | 5 | 3 | -2 | 0.219863 | 0.128145 | -0.091718 |
| r2 | 7 | 3 | -4 | 0.252258 | 0.186293 | -0.065965 |
| r3 | 5 | 7 | +2 | 0.338221 | 0.366429 | +0.028209 |
| r4 | 4 | 6 | +2 | 0.212467 | 0.285291 | +0.072824 |
| r5 | 6 | 5 | -1 | 0.300574 | 0.172518 | -0.128056 |
| r6 | 5 | 7 | +2 | 0.325251 | 0.391370 | +0.066120 |
| r7 | 7 | 6 | -1 | 0.276787 | 0.334781 | +0.057994 |
| r8 | 7 | 7 | 0 | 0.394873 | 0.404335 | +0.009461 |
| r9 | 6 | 3 | -3 | 0.326193 | 0.204169 | -0.122024 |
| **九轮平均** | **5.78** | **5.22** | **-0.56** | **0.294054** | **0.274815** | **-0.019239** |

nAUC 是时间加权的 score 指标；它和最终 score 方向可以不同。例如 r7 treatment 的 nAUC 较高，但最终 score 仍比 baseline 少 1 分，说明 treatment 可能较早获得部分进展，却没有在 horizon 结束前完成同样数量的最终证明。

### 机制、采用和成本证据

| 指标 | baseline 九轮 | treatment 九轮 | 解释和分母 |
| --- | ---: | ---: | --- |
| `checkpoint_saved` | 0 | 3913 | runner 保存事件，不是证明数 |
| `checkpoint_handoff` | 0 | 721 | 后续 assignment 的 task-level handoff 次数 |
| `checkpoint_recovery_prompt` | 0 | 498 | 实际追加 continuation prompt 的 assignment 数 |
| `checkpoint_published` | 0 | 1819 | CPS piece 发布次数，不等于候选采用 |
| handoff `transfer_candidate=true` | 0 | 0 | 当前字段只表示 raw candidate 覆盖 active candidate 的分支是否发生 |
| solver attempts 平均 | 134.4 | 124.1 | treatment 低约 7.7% |
| solver input tokens 平均 | 1.585M | 2.162M | treatment 高约 36.4% |
| solver total tokens 平均 | 1.993M | 2.546M | treatment 高约 27.7% |
| solver slot-seconds 平均 | 115198.31 | 115197.99 | 两边基本相同 |

这些计数的身份层不同，不能相除得到一个“recovery 成功率”。`checkpoint_saved` 表示 durable write，`checkpoint_handoff` 表示后续 assignment 可见，`checkpoint_recovery_prompt` 表示 prompt 注入，CPS publish 表示知识片段写入；它们都不表示 Agent 采用了候选，也不表示 Judge proof 成功。

### 逐轮、逐题变化及原因判断

下表只列最终 score 发生变化的 19 个 task-round。证据级别 A 表示 checkpoint context 和时间顺序直接支持该解释；B 表示机制上合理但缺少 adoption receipt；C 表示主要只能确认搜索路线差异。

| 轮次 / 题目 | Δ T−B | B/T 结果与 proof 时间 | treatment save / handoff / prompt | 观测到的原因 | 级别 |
| --- | ---: | --- | ---: | --- | --- |
| r1 `imo2023_p4` | -1 | B PROVED 322.5s / T 未证明 | 48 / 9 / 7 | B 早于注入；自然路线分叉和 recovery 开销 | C |
| r1 `imo2024_p2` | -1 | B PROVED 1723.0s / T 未证明 | 53 / 10 / 8 | partial 有保存，但没有在当前运行中闭合 | B |
| r2 `imo2023_p3` | -1 | B PROVED 3099.4s / T 未证明 | 37 / 6 / 6 | forward 已完成，reverse 仍是主缺口；未见最终候选采用 | A/B |
| r2 `imo2024_p1` | -1 | B PROVED 3086.6s / T 未证明 | 46 / 7 / 7 | floor/congruence partial 尚未完成 reverse formalization | B |
| r2 `imo2024_p2` | -1 | B PROVED 1156.4s / T 未证明 | 48 / 8 / 8 | checkpoint 有更强 partial，但当前 `result.lean` 仍 baseline sorry | A |
| r2 `imo2024_p6` | -1 | B PROVED 2183.4s / T 未证明 | 47 / 7 / 8 | algebraic helper 已保存，主 theorem 未闭合；hash presence 不代表采用 | B |
| r3 `imo2023_p4` | +1 | B 未证明 / T PROVED 2168.0s | 29 / 6 / 3 | treatment 注入目标缺失，属于普通路线命中 | C |
| r3 `imo2023_p5` | +1 | B 未证明 / T PROVED 708.9s | 9 / 0 / 0 | 早期自然 proof，几乎没有 recovery | C |
| r4 `imo2023_p4` | +1 | B 未证明 / T PROVED 536.4s | 6 / 0 / 0 | proof 早于注入，属于普通路线命中 | C |
| r4 `uk2024_r1_p1` | +1 | B 未证明 / T PROVED 2992.2s | 48 / 6 / 9 | checkpoint 有 compiled structural helpers，可能帮助继续；无 adoption receipt | B |
| r5 `imo2024_p6` | -1 | B PROVED 2192.0s / T 未证明 | 43 / 7 / 6 | injectivity/fixed-point helpers 尚未闭合为全局分类 | B |
| r6 `imo2023_p5` | +1 | B 未证明 / T PROVED 3071.4s | 37 / 11 / 3 | min-weight invariant、dyadic upper coloring 和 upper branch context 可能帮助继续 | B |
| r6 `imo2024_p2` | +1 | B 未证明 / T PROVED 281.2s | 12 / 0 / 0 | 早期自然 proof，和 recovery 无关 | C |
| r7 `imo2023_p5` | -1 | B PROVED 3376.0s / T 未证明 | 42 / 15 / 1 | baseline strongest、self-recursion 被拒绝；没有新的 valid lower-bound route | B/C |
| r8 `imo2023_p3` | -1 | B PROVED 3254.4s / T 未证明 | 56 / 15 / 7 | checkpoint 有约 22KB partial，但当前文件 baseline-only | A |
| r8 `imo2023_p5` | +1 | B 未证明 / T PROVED 1158.6s | 13 / 3 / 0 | 早期自然 proof；注入目标缺失 | C |
| r9 `imo2023_p3` | -1 | B PROVED 1624.4s / T 未证明 | 43 / 7 / 7 | checkpoint 记录 partial，但 Agent 说没有可访问 candidate | A |
| r9 `imo2023_p4` | -1 | B PROVED 1623.5s / T 未证明 | 46 / 6 / 8 | 多次记录 workspace baseline-only、candidate inaccessible | A |
| r9 `uk2024_r1_p1` | -1 | B PROVED 494.0s / T VERIFY_FAIL | 47 / 6 / 8 | baseline 远早于注入；treatment 更早走入失败 formal route | C |

r6/r7 的 `imo2023_p5` 是最关键的同题反向对照：r6 treatment 成功、r7 treatment 失败。它说明有用的 checkpoint context 可能节省重复探索，但不能保证在剩余 horizon 内完成最后的 Lean formalization。r2 `imo2024_p2`、r8 `imo2023_p3` 和 r9 `imo2023_p3/p4` 则显示，当前系统可能把 partial 保存下来，却让后续 Agent 从 baseline 或小 candidate 继续，这正是需要补 adoption contract 的地方。

### 运行健康、限制和解释边界

九个 baseline 和九个 treatment arm 的最终运行状态均为 `DEGRADED`。没有 OOM 或 exit 137，但不同 arm 出现 Judge probe infrastructure error、solver process error、solver cancellation、timeout 和 profiling audit 的 dropped fields/unknown events。profiling 仍然可以说明生命周期覆盖和成本趋势，但不能把所有细粒度事件计数当作无偏的数学性能指标。

r1 的注入合同不是严格同题匹配，r3/r8 的目标进程没有找到；这些轮次仍保留在总体统计中，但已经从严格 injection 结论中分开。九轮数量足以说明保存机制可靠发生、结果方向不稳定、成本上升和 adoption 证据缺失；不足以建立“某一个 checkpoint 被采用后直接导致某一次 proof 或 failure”的单步因果链。

原始 `final.json`、`events.jsonl`、`scoreboard_history.jsonl`、CPS checkpoint body、注入记录和 profiling audit 没有随本报告一起发布。完整的逐题叙述性分析作为同目录的 [逐题因果分析附录](injected_checkpoint_nine_round_causal_analysis_20260906.md) 保留；内部 evidence bundle 以逻辑 run ID 和批次名称管理。

### 决策建议和下一步实验

建议保留 checkpoint save 和有限上下文，因为它们确实防止局部成果只存在于被终止进程的内存中；当前数据也显示 recovery context 在 r6 `imo2023_p5` 这类任务中可能具有实际价值。暂不建议把当前 implementation 当作 score 优化或强制 continuation 策略，因为 adoption 没有回执，且整体 score/AUC 没有改善。

下一轮最小可行实验应保持当前 12 题、3600 秒、GPT-6-Astra 和 Judge/Lean 合同，增加以下可观测事件：`checkpoint_read`、`checkpoint_adopted`、`candidate_materialized_hash`、`first_post_recovery_edit`、`first_post_recovery_judge_check` 和 `adoption_to_proof`。条件至少分为：

- fresh restart：不提供 checkpoint；
- Agent continuation：提供 checkpoint，Agent 自主读取和采用；
- runner adoption：runner 自动把候选 snapshot materialize 为 active candidate，Agent 直接继续验证。

在这三组条件拥有 adoption、首次新 Judge check、重复路线和最终 proof hash 的配对数据以前，不应把 checkpoint 宣布为默认的性能改进。

完整证据：原始 evidence bundle 未随本报告提供；决策相关的九轮 score、nAUC、成本、健康限制和逐题原因已经在本文和同目录附录中列出。
