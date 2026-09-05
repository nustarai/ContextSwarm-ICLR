# MathOlympiadBench 一小时外部路线去重仲裁的 paired 实验报告

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
| 重复次数 | 1 个 paired replicate（control 与 treatment 各 1 次）；不是多轮平均 |
| run ID | baseline `20260905T071908Z-df7b2db9`；treatment `20260905T071908Z-e6b3eb73` |
| baseline / treatment | 唯一变量为 `external_dedup_mode=off` 对 `enforce` |
| 模型与版本 | `openai-codex/gpt-6-astra`；两边相同的 ContextSwarm 镜像和源码提交 |
| 并行与调度 | 最大并行 32；每题初始 2 个 Agent；其余 allocator、Judge、通信和 timeout 合同保持一致 |
| 真实性 | real run；非 mock、非 replay、非 canary |
| 运行隔离 | 两边使用独立 CPS/数据库、Judge、worker workspace 和 container stack；transport preflight 为 `ok`，Judge 有 32 个 ready worker，Judge result cache disabled |

## 结论

1. **机制层：** 本次真实运行没有产生任何 `external_route_dedup_decision`、`semantic_conflict` 或 `switch_required`。因此实现接口和单元测试得到验证，但真实 treatment 没有被高重叠路线触发，不能据此证明它在实际冲突中能有效切换方向。
2. **结果层：** 在这一个 degraded paired replicate 中，treatment 得到 4/12，baseline 得到 6/12；AUC 为 0.192996 对 0.209250；首个 verified proof 为 235.65 秒对 175.86 秒。9/12 道题的最终状态完全相同，分数差的两道题都是 baseline 找到 proof、treatment 没有找到 proof；第三道状态变化的题两边都没有得分。观察结果偏向 baseline，但没有足够证据把差异归因于去重，因为 treatment 没有触发仲裁。
3. **原因与可靠性：** 丢分集中在两个长尾任务。`imo2023_p4` 中 treatment 有 46 次模型 `overloaded` 错误、10 次 runner timeout，baseline 分别为 5 和 2；`uk2024_r1_p1` 中 treatment 的第 9 个 episode 在 730 秒后 timeout，而 baseline 的第 9 个 episode 在 270 秒内找到 proof。两边都使用满 32 个计算槽；treatment 在这两道题上实际获得的 solver slot 时间更多，因此当前日志更符合模型路径随机性叠加 provider/timeout 波动，而不是 treatment 被资源或去重规则提前截断。两边最终状态都是 `DEGRADED`，且 profiling 精确审计均未通过，所以本轮不能作为稳定性或成本优势的证明。
4. **决策：** 暂不把 `enforce` 设为默认策略。保留 opt-in/advisory 形态；下一轮先让在线比较覆盖本轮有界历史中的 `released/done` claims，并加入可审计的比较窗口和决策字段，再进行至少 3 个 paired seeds 的真实比较。

## 支撑结论的数据和分析

### 结果对比

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

只有一个 replicate，不能计算有意义的方差或显著性。AUC 是整个时间窗口的累计指标；因此它可能与“首个 proof 较早”给出不同信号，本表同时保留两者。

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

这些记录能够确认“treatment 在本轮两个关键长尾任务上没有及时得到可验证 proof”，也能确认 treatment 没有实际执行去重仲裁；它们不能仅凭一个 paired replicate 证明 provider overload 是唯一根因，也不能证明下一轮一定会复现同样方向的差异。当前最稳妥的判断是：本轮的 -2 分是未触发的去重 treatment 与模型/服务时序随机性叠加后的观察结果，不能当作去重策略的负因果效果。

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

在线比较只读取 claim 时仍处于 `active`/`blocked` 的 bounded peer projection。两个 Agent 先后运行时，前一个 Agent 如果已经 `released` 或 `done`，后一个 Agent 的相同路线不会被这一版在线仲裁看到。离线复核也没有发现达到 `0.78` 的 lexical 候选，但 lexical 分数不能证明数学语义上的独立性或重复性。

### 稳定性、限制与下一步

两边 runner 都以 `rc=0` 完成 3,600 秒 horizon、drain 和 closeout；Judge broker 的 closeout 显示 `drained=true` 且没有 remote unsettled jobs。不过最终状态都是 `DEGRADED`：两边各有 4 次 `judge_probe_infrastructure_error`，baseline 另有 1 次、treatment 另有 2 次 `unexpected_process_error`。这使得本次得分差只能作为观察结果，不能作为去重的因果效果。

profiling 文件本身是 real 记录，主要 agent wrapper、CPS、Judge 和 max-parallel coverage 均存在，事件顺序也通过了基础检查；但精确审计仍失败：baseline 为 94,497 rows、120 rows/362 fields dropped、1 个 span missing end，treatment 为 87,988 rows、104 rows/314 fields dropped、1 个 span missing end。profiling 可用于规模和时序参考，不能被称为完整通过的审计证据。

下一轮最小修复和实验顺序是：

1. 把在线 peer 查询扩展到本 run 内有界的 `released/done` 历史路线，并记录比较窗口、peer 状态、分数、仲裁结果和独立验证放行原因。
2. 先用固定的合成冲突摘要做 broker/canary 验证，确认 treatment 确实产生 `semantic_conflict` 和 `switch_required`；这一步只验证机制，不纳入数学得分。
3. 在机制预检通过后，以相同模型、Judge、并行上限和 timeout 合同至少跑 3 个 paired seeds，报告 overlap rate、blocked/switch rate、独立验证放行率、人工抽样的重复判定 precision/recall、proof score/time、token 和 degraded/error rate。

在真实运行出现足够的可判定路线冲突，并且多轮结果显示机制收益没有被误拦截和基础设施波动抵消之前，不应宣布 `enforce` 有效，也不应把它设为默认开启。

完整证据：

- [control run_meta.json](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/external-dedup-control-20260905/runs/strong-activity-paired-20260905/control/20260905T071908Z-df7b2db9/run_meta.json)
- [control final.json](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/external-dedup-control-20260905/runs/strong-activity-paired-20260905/control/20260905T071908Z-df7b2db9/final.json)
- [treatment run_meta.json](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/strong-activity-feedback-20260905/runs/strong-activity-paired-20260905/treatment/20260905T071908Z-e6b3eb73/run_meta.json)
- [treatment final.json](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/strong-activity-feedback-20260905/runs/strong-activity-paired-20260905/treatment/20260905T071908Z-e6b3eb73/final.json)
- [control profiling exact audit](/home/ubuntu/workspace/.workspace/builds/external-dedup-gpt6-r3/evidence/control-profiling-audit-exact.json)
- [treatment profiling exact audit](/home/ubuntu/workspace/.workspace/builds/external-dedup-gpt6-r3/evidence/treatment-profiling-audit-exact.json)
