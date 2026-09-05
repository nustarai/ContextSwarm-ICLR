# 超时前强制保存 checkpoint 对 MathOlympiadBench 的三轮 GPT-6-Astra A/B 结果

## 背景和动机

Agent 在 timeout、cancel 或进程错误边界附近可能已经产生了候选文件、尝试记录和有限的证明上下文，但这些内容没有以结构化状态交给后续 assignment。checkpoint 改动的目标是让局部成果能够跨进程延续，并让后续 Agent 知道已经做过什么、哪些方向暂时受阻以及可以从哪里继续。本报告回答第二个问题：这种延续是否会改变 12 道数学题的最终证明成绩、时间加权成绩和运行成本。

上一轮单配对实验观察到 baseline 5/12、treatment 3/12，但人工注入没有命中相同的逻辑题目，不能把全部差异归因于 recovery。本报告把实验扩展到三轮，并把严格配对的中断证据和非配对的自然运行结果分开解释。

## 具体改了什么

baseline 关闭 checkpoint；treatment 打开 `checkpoint.enabled`、`checkpoint.transfer` 和 `checkpoint.publish`。treatment 在 timeout、cancel 或 live RPC error 等边界保存不可变候选快照、SHA-256、终止原因和有限上下文，并在后续 assignment 前提供 checkpoint handoff/recovery prompt。checkpoint 始终标记为未验证，必须经过新的 Judge 检查才能成为有效证明。

两条 arm 仍使用同一题目、同一模型、同一并发和同一 Judge 合同。共享 CPS 的普通知识发布与 checkpoint 交接保持为不同机制；本次 treatment 只增加 checkpoint 保存、transfer 和 publish。

## 具体的实验

| 项目 | 设置 |
| --- | --- |
| 实验问题 | checkpoint recovery 是否改变最终 score、normalized score-time AUC、证明时间和成本 |
| 任务范围 | MathOlympiadBench 固定 12 题，三轮每轮使用相同顺序和分母 |
| 运行时长 | 每个 arm 3600 秒 solver horizon，随后完成候选冻结和 Judge closeout |
| 重复次数 | 3 个配对 replicate；每轮包含 baseline 和 treatment 两个 arm |
| baseline / treatment | checkpoint off / checkpoint enabled + transfer + publish |
| 模型与版本 | `openai-codex/gpt-6-astra`，Pi 0.85.0，Codex 0.153.4 |
| 源码与镜像 | source commit `587543f`；两条 arm 使用同一 image digest `sha256:4f051f0c…` |
| 运行真实性 | 三轮均为真实 Pi、NuRouter、Judge/Lean 和正式候选 closeout，不是 mock 或 synthetic |
| 固定条件 | 32 个 solver slot、每题初始 2 个 Agent、每题 2 episodes、Agent/Pi timeout 900 秒、Judge result cache disabled、profiling enabled |
| 注入合同 | r2 两边均在同一 `imo2023_p5` 发送 SIGTERM；r3 baseline 命中该题，但 treatment 没有活跃该题进程，按合同不换题；r1 使用了历史的非严格配对注入 |

## 结论

1. **机制层：** checkpoint 在三轮真实运行中都发生了保存和交接。treatment 每轮分别保存 444、405、433 个 checkpoint，产生 92、69、71 次 handoff，并收到 53、60、58 次 recovery prompt；baseline 每轮保存和 handoff 都是 0。局部状态确实没有因为进程边界而完全消失。
2. **结果层：** 三轮的 score 差值分别为 `-2`、`-4` 和 `+2`（treatment 减 baseline）。treatment 在 2/3 轮较低、1/3 轮较高，说明结果具有明显的路径随机性；三轮平均 score 为 baseline `5.67/12`、treatment `4.33/12`，平均差 `-1.33` 分。平均 normalized score-time AUC 为 `0.27011` 对 `0.22696`，treatment 低 `0.04316`，约低 16.0%。这支持“当前 treatment 没有稳定性能收益”，也不支持“checkpoint 每次都会降低性能”。
3. **成本与可靠性：** 三轮平均 solver total tokens 为 baseline 2.22M、treatment 2.78M，treatment 高 25.0%；input tokens 高 33.8%，完成的 attempt 少 8.3%，而两边 solver slot-seconds 几乎相同。六个 arm 都是 `DEGRADED`，每个 arm 至少有 Judge probe infrastructure error；因此性能差异不能脱离 provider/Judge 健康和宿主背景负载解释。
4. **决策：** checkpoint 作为“局部成果不消失”的可靠性层仍应保留，但当前“自动把最新未验证状态作为 recovery 起点”的策略不能被当作性能优化，也不应在没有额外保护时默认开启。下一步应把持久化和搜索策略解耦：保留 checkpoint sidecar、给 recovery 保留 fresh exploration 分支、把 blocker 当作软证据、限制 task-latest 路线连续重复，并在新的 Judge evidence 后再提升候选优先级。

## 支撑结论的数据和分析

### 结果对比

| replicate | baseline score | treatment score | 差值 T-B | baseline nAUC | treatment nAUC | nAUC 差值 | 首个 proof（B/T 秒） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| r1 | 5 | 3 | -2 | 0.21986297 | 0.12814466 | -0.09171831 | 81.20 / 79.70 |
| r2 | 7 | 3 | -4 | 0.25225792 | 0.18629280 | -0.06596512 | 117.01 / 114.33 |
| r3 | 5 | 7 | +2 | 0.33822051 | 0.36642946 | +0.02820895 | 89.05 / 75.90 |
| 三轮平均 | 5.67 | 4.33 | -1.33 | 0.27011380 | 0.22695564 | -0.04315816 | 95.76 / 89.98 |

r3 treatment 的 7/12 说明 checkpoint 并非必然有害；如果只看 r1 或 r2，会得到过强的负面结论。另一方面，treatment 三轮都更早得到第一个 proof，但 r1/r2 的后续 proof 不足以维持更高的 score-time AUC。这正是路径随机性和固定 horizon 下搜索分支数量共同作用的表现：早期找到一个 proof，不代表后续还能快速找到更多题的 proof。

三轮 baseline 的 proved-task 数分别为 5、7、5，treatment 分别为 3、3、7。treatment 在 r3 证明了 baseline 没有证明的 `imo2023_p4` 和 `imo2023_p5`，说明恢复路线有时能够延续到有效方向；r1/r2 则出现了相反情况。

### 机制与成本证据

| 指标 | r1 B/T | r2 B/T | r3 B/T | 分母与解释 |
| --- | ---: | ---: | ---: | --- |
| checkpoint saved | 0 / 444 | 0 / 405 | 0 / 433 | runner 保存事件，不是证明数 |
| checkpoint handoff | 0 / 92 | 0 / 69 | 0 / 71 | 后续 assignment 的交接次数 |
| recovery prompt | 0 / 53 | 0 / 60 | 0 / 58 | 实际注入 continuation prompt 的 assignment 数 |
| checkpoint published | 0 / 222 | 0 / 207 | 0 / 195 | CPS piece 发布，不等于候选被采用 |
| solver attempts | 145 / 136 | 131 / 112 | 121 / 116 | 固定 horizon 内的 process-attempt 数 |
| solver total tokens | 2.95M / 3.34M | 2.26M / 2.90M | 1.46M / 2.10M | treatment 平均高 25.0% |
| solver input tokens | 2.35M / 2.82M | 1.76M / 2.49M | 1.21M / 1.81M | treatment 平均高 33.8% |
| solver slot-seconds | 115197.98 / 115198.39 | 115198.55 / 115198.62 | 115198.48 / 115197.17 | 两边均接近 32-slot 满载 |

checkpoint saved、published 和 handoff 是不同身份层的机制计数，不能直接相加，也不能解释成“候选被采用”。本轮尚未建立每个最终 proof 是否源自某个 checkpoint 候选的 adoption 因果链；最终分数仍只来自权威 Judge closeout。

### 稳定性、限制与下一步

- 样本量是 3 个配对 replicate，足以显示 r3 的反向结果和路径随机性，但不足以声称统计显著或估计稳定的平均 treatment effect。
- r2 的人工中断最接近目标因果比较：两边在同一 `imo2023_p5`、同一 UTC 时刻发送 SIGTERM。r3 treatment 没有活跃的 `imo2023_p5` 进程，因此没有注入；r1 两边命中的逻辑题不同。三轮完整 A/B 结果仍然有效，但人工中断子分析不能合并成三次严格配对。
- 六个 arm 的最终状态都是 `DEGRADED`。Judge probe infrastructure error、solver process error 和 provider 过载都可能改变具体题目的完成机会；没有 OOM，但健康限制降低了外推强度。新增两轮的 profiling audit 也不是 clean：r2 baseline/treatment 分别有 1700/1488 个 dropped fields，treatment 另有 1750 个 unknown event；r3 baseline/treatment 分别有 1588/1528 个 dropped fields，treatment 另有 1832 个 unknown event；四个 audit 各有 1 个未闭合 span。
- 批次开始时宿主上有 5 个无关 formal 容器正在运行。两条 arm 共享宿主 CPU、内存和网络物理资源；该背景负载作为 batch-level validity boundary 被记录，不能从本批次结果中完全剥离。
- r1/r2 的 treatment 更低、r3 更高，说明“继续旧状态”与“重新探索”之间不是固定方向的收益或损失。当前更合理的产品目标是避免局部成果完全丢失，同时避免让单个未验证 checkpoint 成为唯一搜索路线。
- 下一轮最小实验应使用固定的目标 task/episode 注入，保证 baseline/treatment 在每个 replicate 都命中同一题；同时记录 checkpoint adoption、首次新编辑时间、首次 fresh `judge_check`、重复路线率、token、slot-seconds 和最终 proof。若 score 仍无稳定改善，但保存和交接指标稳定，则应把该功能定位为可靠性改动，而不是性能改动。

原始证据未随文档提供。可复核 evidence ID 为：`checkpoint-gpt6-astra-real-ab-20260905`（r1）、`checkpoint-gpt6-astra-real-ab-r2-r3-20260905`（r2/r3）、`three-round-summary.json`（三轮汇总）、`batch-contract.json`（合同）、`injection-r2-baseline.txt`、`injection-r2-treatment.txt`、`injection-r3-baseline.txt` 和 `injection-r3-treatment.txt`（中断证据）。
