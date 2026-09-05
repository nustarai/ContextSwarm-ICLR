# MathOlympiadBench 一小时外部路线去重仲裁的三轮 paired 实验报告

## 背景和动机

此前原版 MathOlympiadBench 实验的 profiling 和选择记录显示，不同 Agent 经常反复尝试相近方向。重复探索会占用有限的 Agent 时隙和模型调用，却未必增加新的证明信息。这个 proposal 要验证的是：由 Agent 外部的 CPS/runner 观察路线并主动仲裁，能否在新 Agent 开始一条路线前要求它切换方向，从而减少重复尝试。

本报告只讨论外部路线去重。超时前 checkpoint、终止时总结、恢复流程以及向 CPS 发布恢复信息属于其他改动，没有进入本次 treatment。

## 具体改了什么

Agent 在开始探索前提交一个简短的路线摘要和 route claim。CPS 在写入 claim 前，对同一题目仍处于 `active` 或 `blocked` 状态的其他路线做有界 lexical 比较：

- `external_dedup_mode=off`（baseline）保留路线 claim 和活动摘要，但不做重叠判断，也不阻止 Agent。
- `external_dedup_mode=enforce`（treatment）在分数达到 `0.78` 且至少共享 3 个有效 token 时返回 `semantic_conflict` 和 `switch_required=true`，要求 Agent 换方向；Agent 明确说明是在做独立验证时可以继续。

比较器只处理隐私安全的短摘要，过滤停用词和过短、过泛的文本。判断发生在 claim 写入前；它不会异步终止已经运行的 Agent，也不会替 Agent 选择数学路线。实现提交为 `e4353b44c07515fc9e23dca9668340fa42a7678a`，相关单元测试共 81 项通过。实现入口见 [`route_dedup.py`](../contextswarm_mini/route_dedup.py) 和 [`cps.py`](../contextswarm_mini/cps.py)。

## 具体的实验

| 项目 | 设置 |
| --- | --- |
| 实验问题 | 外部 enforce 是否减少路线重复，并改善证明结果或单位资源产出 |
| 任务范围 | MathOlympiadBench 的 12 道题，两个 arm 使用相同题目顺序和 seed=0 |
| 运行时长 | 每个 arm 的 horizon 为 3,600 秒，随后完成 drain/closeout |
| 重复次数 | 共 3 个 paired replicate：此前 1 轮 + 本次并发复跑 2 轮；同一声明 seed=0，用于观察 provider/调度随机性 |
| run ID | 此前 r0：baseline `20260905T071908Z-df7b2db9`、treatment `20260905T071908Z-e6b3eb73`；新增 r1/r2 见下方四臂表和证据链接 |
| baseline / treatment | 唯一变量为 `external_dedup_mode=off` 对 `enforce` |
| 模型与版本 | `openai-codex/gpt-6-astra`；两边相同的 ContextSwarm 镜像和源码提交 |
| 并行与调度 | 最大并行 32；每题初始 2 个 Agent；其余 allocator、Judge、通信和 timeout 合同保持一致 |
| 真实性 | 6 个 arm 均为 real run；非 mock、非 replay、非 canary；本次新增 4 个 arm 同一 barrier 并发启动 |
| 运行隔离 | 两边使用独立 CPS/数据库、Judge、worker workspace 和 container stack；transport preflight 为 `ok`，Judge 有 32 个 ready worker，Judge result cache disabled |

## 结论

1. **机制层：** 新增的四个真实 arm 仍然没有产生 `external_route_dedup_decision`、`semantic_conflict` 或 `switch_required`。因此实现接口和单元测试得到验证，但本轮 treatment 实际没有向 Agent 发出路线切换要求；`enforce` 在这个 workload 上基本是未触发的 treatment。
2. **结果层：** 两个并发复跑的 score 差分别为 `+1`（r1：7 对 6）和 `-1`（r2：6 对 7），方向相反；两轮平均 score 都是 6.5/12。把此前那一轮也纳入后，control 合计 19/36、treatment 合计 17/36，只有 3 个 paired replicate，不能把总体的 -2 题解释成稳定的去重负效应。新两轮 treatment 的平均 normalized AUC 高 `0.045415`，但所有 arm 都是 degraded，且机制没有触发，不能把 AUC 差归因于去重。
3. **原因与可靠性：** 结果更符合模型/provider 时序、恢复路径和共享容量造成的随机波动叠加，而不是已经观察到的外部去重因果效应。新两轮每一对的最终得分只差一道题，且两个方向相反；四个 arm 均占满约 0.999975 的 solver slot utilization、没有 OOM，但都有 Judge probe infrastructure error。宿主机当时还存在另一个独立的 `figure4_formal_gpt6` workload，因此这批数据不能被称为无干扰的因果实验。四个 profiling 精确 audit 也都未通过，原因是 dropped fields 和未闭合 span。
4. **决策：** 不能说“已经证明纯粹是随机波动”，但可以说原先单轮 -2 的结果在新增两轮中没有复现出稳定方向，当前最合理的工作判断是随机性和运行健康问题占主导。暂不把 `enforce` 设为默认策略；保留 opt-in/advisory 形态，先补足可观测的去重命中、阻断、切换和独立验证放行指标，再在安静且可隔离的 provider/Judge 环境跑干净的多轮比较。

## 支撑结论的数据和分析

### 三轮 paired 的结果对比

新增的两轮使用同一 model、source commit、镜像、horizon、并行上限和 seed=0；四个 arm 在同一 barrier 并发启动。seed 沿用 0 是为了观察同一声明配置下的 provider/调度波动，不应被当成独立随机种子抽样。

| paired replicate | control score | treatment score | score delta (treatment-control) | control norm AUC | treatment norm AUC | control first proof | treatment first proof |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 此前 r0 | 6/12 | 4/12 | -2 | 0.209250 | 0.192996 | 175.86 s | 235.65 s |
| 并发 r1 | 6/12 | 7/12 | +1 | 0.285328 | 0.370348 | 139.85 s | 78.05 s |
| 并发 r2 | 7/12 | 6/12 | -1 | 0.367019 | 0.372829 | 101.21 s | 89.47 s |
| 三轮总计 | 19/36 | 17/36 | -2 题 | — | — | — | — |

新增两轮的 treatment-control 平均 score 都是 0 差异；平均 normalized AUC 为 0.326174 对 0.371588，平均首个 proof 为 120.53 s 对 83.76 s。它们只是描述性统计，不能进行稳定性或显著性推断。

新增 arm 的运行摘要如下。`DEGRADED` 是运行最终健康状态；四个 runner 的退出码仍为 0。

| arm | score | norm AUC | attempts | CPS pieces/messages | decisions | judge probe/error | solver timeout | unexpected process error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| r1 control | 6/12 | 0.285328 | 125 | 204 / 448 | 93 | 999 / 4 | 54 | 0 |
| r1 treatment | 7/12 | 0.370348 | 131 | 221 / 537 | 99 | 996 / 2 | 55 | 2 |
| r2 control | 7/12 | 0.367019 | 139 | 241 / 435 | 107 | 966 / 2 | 45 | 1 |
| r2 treatment | 6/12 | 0.372829 | 141 | 297 / 694 | 109 | 1096 / 3 | 49 | 1 |

所有新增 arm 的 fallback decision 为 0，OOM/exit 137 为 0，solver slot utilization 在 0.999975 左右。treatment 在 r1 多得一道题，在 r2 少得一道题；这组相反方向的 paired 差异比单轮 baseline -2 更能说明目前没有稳定 treatment 效应。

### 此前第一轮的结果对比

| 指标 | baseline (`off`) | treatment (`enforce`) | treatment - baseline | 分母/方向 |
| --- | ---: | ---: | ---: | --- |
| 最终得分 | 6/12 | 4/12 | -2 题 | 越高越好 |
| `normalized_score_time_auc` | 0.209250 | 0.192996 | -0.016254 | 越高越好；时间加权，不等同于首个成功时间 |
| 首个 verified proof | 175.86 s | 235.65 s | +59.79 s | 越低越好；仅在有 verified proof 时定义 |
| verified proofs | 6 | 4 | -2 | 12 道题 |
| `COMPILES_WITH_SORRY` | 5 | 6 | +1 | 12 道题 |
| `VERIFY_FAIL` | 1 | 2 | +1 | 12 道题 |
| Agent attempts | 151 | 135 | -16 | 完成的 Agent assignment |
| Agent timeout count | 51 | 52 | +1 | runner 记录的 timeout |

这一节只保留此前 r0 的逐题诊断；它单独只有一个 replicate。综合判断应以本报告前面的三轮 paired 表为准，不能计算有意义的方差或显著性。AUC 是整个时间窗口的累计指标；因此它可能与“首个 proof 较早”给出不同信号，本表同时保留两者。

### 为什么 treatment 少 2 题

先看差异是否真的由去重分支产生：两边的 system prompt hash 都是 `ba4b0f...`，baseline 的 151/151 个 Agent session 和 treatment 的 135/135 个 session 使用同一份 prompt；CPS SQLite 中两边都没有 `external_route_dedup_decision`、`semantic_conflict` 或 `switch_required` 事件。treatment 的 `enforce` 因而没有向模型发出切换要求，也没有拒绝任何一条路线。在线 route-claim 的 lexical 比较在这次 run 中没有达到 0.78 阈值；离线复核的最高分也只有 baseline 0.703、treatment 0.522。

分数变化集中在以下三道题：

| 任务 | baseline | treatment | 运行证据 |
| --- | --- | --- | --- |
| `imo2023_p4` | `PROVED`，约 3022.47 s | `COMPILES_WITH_SORRY` | baseline 11 attempts、2 timeouts；treatment 12 attempts、10 timeouts。treatment 的模型 session 有 658 个 assistant message、50 个 `stop:error`，baseline 为 595/14；treatment 有 46 次 `Codex error: ... overloaded`，baseline 为 5。 |
| `uk2024_r1_p1` | `PROVED`，约 3278.69 s | `COMPILES_WITH_SORRY` | 两边各 14 attempts；baseline 3 timeouts，treatment 10。baseline 的第 9 个 episode 在约 270 s 内完成 proof；treatment 的第 9 个 episode 首次约 900 s timeout，恢复后又运行约 730 s 直到 horizon，期间仍未完成 proof。 |
| `imo2023_p3` | `COMPILES_WITH_SORRY` | `VERIFY_FAIL` | 最终状态不同，但两边都计 0 分，不能解释 6→4 的分数下降。 |

`imo2023_p4` 的日志尤其清楚地显示了长尾失败：baseline 的第 8 个 episode 在 873 s 时提交了 proof，runner 随即取消同题其他 Agent；treatment 的第 8 个 episode 在首次 900 s timeout 后又进入恢复流程，恢复阶段运行约 625 s 直到 horizon，仍未产生可验证 proof。treatment 的第 8、11 个 episode 分别出现 23 次 overload 错误；这与其最终保留的 sorry candidate 相符。`uk2024_r1_p1` 也呈现相同方向：baseline 的结构化 recurrence 路线在第 9 个 episode 验证成功，treatment 的第 9 个 episode 在更早开始、获得更长总运行时间后仍 timeout。

模型请求层面，baseline 共记录 6,938 个 assistant messages，其中 186 个 `stop:error`、87 个明确的 provider overload；treatment 记录 6,336 个 messages，其中 208 个 `stop:error`、131 个 overload。对应比例分别是 2.68%/1.25% 和 3.28%/2.07%。treatment 的总 token 反而更高（3,120,089 对 3,039,260），说明少出的 16 个 Agent assignment 没有转化为更短的有效探索；它在两个丢分题上消耗了更多长会话和恢复时间。资源 profiling 没有发现 OOM 或明显的进程内存差异，CPS `route.claim` 的中位 wall time 只有 2.038 ms 对 2.143 ms，不能把这次下降解释为去重比较本身造成的 CPS 性能开销。

这些记录能够确认“treatment 在此前 r0 的两个关键长尾任务上没有及时得到可验证 proof”，也能确认 treatment 没有实际执行去重仲裁；它们不能仅凭一个 paired replicate 证明 provider overload 是唯一根因。新增 r1/r2 的相反 score 方向进一步说明，r0 的 -2 分不能当作去重策略的负因果效果。

### 机制与成本证据

| 指标 | baseline | treatment | 解释/分母 |
| --- | ---: | ---: | --- |
| CPS route claims | 179 | 156 | route claim 事件总数；不是不同数学路线的数量 |
| CPS pieces | 271 | 263 | CPS piece 记录；不等于已被 Agent 采用的知识 |
| CPS messages | 541 | 471 | 消息记录；不等于送达、阅读或采用 |
| `external_route_dedup_decision` | 0 | 0 | treatment 没有进入实际仲裁分支 |
| `semantic_conflict` / `switch_required` | 0 / 0 | 0 / 0 | 没有观察到阻止或切换要求 |
| 同题不同 Agent 的离线 claim pairs | 1,382 | 998 | 全部 claim 的离线 lexical 配对复核 |
| 离线 score ≥ 0.60 | 1（最高 0.703） | 0 | 仅作候选重叠筛查，不是语义重复证明 |
| 离线 score ≥ 0.78 | 0 | 0 | 本轮 enforce 阈值 |
| solver model sessions | 151 | 135 | 与 Agent attempts 相对应 |
| solver input/output/total tokens | 2,618,979 / 420,281 / 3,039,260 | 2,668,446 / 451,643 / 3,120,089 | runner allocation summary 的总量；treatment 总 token 高 80,829（约 2.7%） |

### 新增两轮的外部去重观察

四个新增 arm 的 CPS 中都没有 `external_route_dedup_decision`、`semantic_conflict` 或 `switch_required`。`route_claim_reused` 只在 r1 treatment 出现 1 次，它表示同一个 Agent 重用自己的 claim，不能当作外部去重命中。route-claim 总数为 r1 control/treatment `154/156`、r2 control/treatment `164/172`；同一题同一 `route_key` 的重复组分别为 `3/0/2/1`，这些是事后 claim 统计，不能改变“在线 enforce 没有触发”的结论。

作为离线筛查，按 `route_key + summary` 做同题 claim 的 lexical Jaccard，四个 arm 的最大值分别为 `0.519/0.536/0.955/0.750`（r1 control、r1 treatment、r2 control、r2 treatment）。这是独立审计脚本的候选重叠指标，不是 runner 的在线语义判断；其中最高的 `0.955` 出现在 control，而不是 enforce treatment，且没有转化为在线仲裁事件。因此这批数据只能说明当前 workload 没有让 treatment 产生可观测的切换行为，不能说明路线实际上不存在重复。

在线比较只读取 claim 时仍处于 `active`/`blocked` 的 bounded peer projection。两个 Agent 先后运行时，前一个 Agent 如果已经 `released` 或 `done`，后一个 Agent 的相同路线不会被这一版在线仲裁看到。离线复核也没有发现达到 `0.78` 的 lexical 候选，但 lexical 分数不能证明数学语义上的独立性或重复性。

### 稳定性、限制与下一步

此前 r0 的两个 runner 以及新增 r1/r2 的四个 runner 都以 `rc=0` 完成 3,600 秒 horizon、drain 和 closeout；各自 Judge broker closeout 均完成 drain。六个 arm 的最终状态都是 `DEGRADED`：此前 r0 的两边各有 4 次 `judge_probe_infrastructure_error`，新增四个 arm 分别有 4、2、2、3 次；新增四个 arm 另有 0、2、1、1 次 `unexpected_process_error`。因此所有 score 差都只能作为观察结果，不能作为去重的因果效果。

profiling 文件本身是 real 记录，主要 agent wrapper、CPS、Judge 和 max-parallel coverage 均存在，事件顺序也通过了基础检查；但六个 arm 的精确审计都未通过。此前 r0 的 baseline/treatment 分别有 94,497/87,988 rows，120/104 个 dropped rows；新增 r1-control、r1-treatment、r2-control、r2-treatment 分别有 72,407、77,684、70,888、77,970 rows，dropped rows 为 94、100、108、110，并各有 1、2、1、1 个 span missing end。profiling 可用于规模和时序参考，不能被称为完整通过的审计证据。

下一轮最小修复和实验顺序是：

1. 把在线 peer 查询扩展到本 run 内有界的 `released/done` 历史路线，并记录比较窗口、peer 状态、分数、仲裁结果和独立验证放行原因。
2. 先用固定的合成冲突摘要做 broker/canary 验证，确认 treatment 确实产生 `semantic_conflict` 和 `switch_required`；这一步只验证机制，不纳入数学得分。
3. 在机制预检通过、宿主机无其他大型 workload、profiling audit clean 后，以相同模型、Judge、并行上限和 timeout 合同至少跑 3 个真正独立的 paired seeds，报告 overlap rate、blocked/switch rate、独立验证放行率、人工抽样的重复判定 precision/recall、proof score/time、token 和 degraded/error rate。当前两轮沿用 seed=0，只能作为随机性观察，不替代独立 seed 复验。

在真实运行出现足够的可判定路线冲突，并且多轮结果显示机制收益没有被误拦截和基础设施波动抵消之前，不应宣布 `enforce` 有效，也不应把它设为默认开启。

完整证据：

- [control run_meta.json](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/external-dedup-control-20260905/runs/strong-activity-paired-20260905/control/20260905T071908Z-df7b2db9/run_meta.json)
- [control final.json](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/external-dedup-control-20260905/runs/strong-activity-paired-20260905/control/20260905T071908Z-df7b2db9/final.json)
- [treatment run_meta.json](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/strong-activity-feedback-20260905/runs/strong-activity-paired-20260905/treatment/20260905T071908Z-e6b3eb73/run_meta.json)
- [treatment final.json](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/strong-activity-feedback-20260905/runs/strong-activity-paired-20260905/treatment/20260905T071908Z-e6b3eb73/final.json)
- [control profiling exact audit](/home/ubuntu/workspace/.workspace/builds/external-dedup-gpt6-r3/evidence/control-profiling-audit-exact.json)
- [treatment profiling exact audit](/home/ubuntu/workspace/.workspace/builds/external-dedup-gpt6-r3/evidence/treatment-profiling-audit-exact.json)
- [新增两轮四臂汇总](/home/ubuntu/workspace/.workspace/builds/external-dedup-gpt6-reruns-20260905-v2/rerun-summary-clean.json)
- [新增两轮批次合同](/home/ubuntu/workspace/.workspace/builds/external-dedup-gpt6-reruns-20260905-v2/batch-contract.json)
- [新增两轮 control r1 final](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/external-dedup-reruns-r1-control-20260905/runs/external-dedup-gpt6-reruns-20260905/r1-control/20260905T110222Z-52921f61/final.json)
- [新增两轮 treatment r1 final](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/external-dedup-reruns-r1-treatment-20260905/runs/external-dedup-gpt6-reruns-20260905/r1-treatment/20260905T110222Z-9ad7d14a/final.json)
- [新增两轮 control r2 final](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/external-dedup-reruns-r2-control-20260905/runs/external-dedup-gpt6-reruns-20260905/r2-control/20260905T110222Z-ecbd77b5/final.json)
- [新增两轮 treatment r2 final](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/external-dedup-reruns-r2-treatment-20260905/runs/external-dedup-gpt6-reruns-20260905/r2-treatment/20260905T110222Z-28a36314/final.json)
- [新增四个 profiling audit 结果](/home/ubuntu/workspace/.workspace/builds/external-dedup-gpt6-reruns-20260905-v2/evidence/)
- [并发批次的 quiet-gate bypass 记录](/home/ubuntu/workspace/.workspace/builds/external-dedup-gpt6-reruns-20260905-v2/evidence/quiet-gate-bypass.txt)
