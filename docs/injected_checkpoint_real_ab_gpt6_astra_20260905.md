# 超时前强制保存 checkpoint 对 MathOlympiadBench 实验结果的影响：gpt-6-astra 注入式 A/B

## 背景和动机

之前的失败案例表明，Agent 在 timeout、cancel 或进程错误边界附近已经写出的局部候选，可能没有以结构化状态交给下一次 assignment。这样会让 recovery 重新走已经尝试过的方向。checkpoint 改动的直接目标是保存候选、哈希、终止原因和有限上下文，让后续进程知道已经做过什么、哪些候选可继续验证，以及从哪里继续。

本次实验进一步回答性能问题：这种状态交接是否会传导到 12 道数学题的最终 Judge 分数、时间加权分数和运行成本。checkpoint 文件存在或 CPS piece 已发布只能证明机制发生，不能代替 Judge 的最终证明结果。

## 具体改了什么

baseline 使用 checkpoint 关闭的 manifest；treatment 只打开 `checkpoint.enabled`、`checkpoint.transfer` 和 `checkpoint.publish`。两边其余正式合同保持一致：同一 source/image、12 题、CPS32、每题初始 2 个 Agent、Agent/Pi 900 秒超时、3600 秒 horizon、`openai-codex/gpt-6-astra`、同一 formal Judge 和关闭 Judge result cache。

treatment 在 timeout、cancel、live RPC error 等协作边界保存不可变 checkpoint，在下一次 assignment 前 materialize 候选并注入 continuation prompt；共享 CPS 的普通知识发布没有作为本次 treatment 的替代品。为了制造真实延续场景，运行中对每条 arm 的一个正在工作的 Pi RPC 进程发送了 SIGTERM；两边都在 5 秒内退出，没有升级到 SIGKILL。预定 injector 本身因 `awk` 语法错误没有发信号，随后立即进行了这次人工注入，实际注入时间约为运行第 32 分钟。

## 具体的实验

| 项目 | 设置 |
| --- | --- |
| 实验问题 | checkpoint recovery 是否改变最终数学得分、score-time AUC 或成本 |
| 任务范围 | MathOlympiadBench 固定 12 题：`imo2024_p1/p2/p3/p5/p6`、`uk2024_r1_p1/p2`、`usa2024_p2`、`imo2023_p2_v2/p3/p4/p5` |
| 运行时长 | 每条 arm 3600 秒，随后完成候选冻结和 Judge closeout |
| 重复次数 | 1 个配对 replicate；每条 arm 各 1 次真实运行 |
| baseline / treatment | checkpoint off / checkpoint enabled + transfer + publish |
| 模型与版本 | `openai-codex/gpt-6-astra`；同一 ContextSwarm image，source commit `587543f001859a7b78cef9b22390d4ad20d20c29` |
| 真实性 | real formal run；真实 Pi、NuRouter、Judge 和 Lean verdict，不是 synthetic/mock |
| 其他固定条件 | 32 并发 slot、初始 2 agents/task、agent timeout 900 秒、Judge cache disabled、profiling 开启；两边都执行一次人工 Pi 中断 |

## 结论

1. **机制层：** checkpoint 在真实运行中产生了实际 recovery。treatment 保存 444 次、handoff 92 次，并记录 53 次 `checkpoint_recovery_prompt`；人工中断后 treatment 先保存并发布 checkpoint，再启动 recovery prompt。baseline 保存和 handoff 都是 0。这个结果回答了“局部成果能否继续传递”：能。
2. **结果层：** 这一个真实配对中没有观察到性能提升，结果反而偏向 baseline：baseline 得分 **5/12**，treatment **3/12**；normalized score-time AUC 为 **0.21986297** 对 **0.12814466**，treatment 低 **0.09171831**（约 **41.7%**）。因此当前不能声称 checkpoint 会提高数学成绩。
3. **成本与可靠性：** 两边 solver slot-seconds 几乎相同（115197.979 对 115198.387）；treatment 总 solver token 为 3,339,675，比 baseline 2,947,685 高 **13.3%**，其中 input token 高 **19.9%**。两边最终状态都是 `DEGRADED`，各有 4 次 Judge probe infrastructure error，profiling audit 还分别发现 dropped fields 和一个未闭合 span，因此这是有健康度限制的方向性结果。
4. **决策：** 为了避免局部成果在 recovery 时消失，checkpoint 机制仍有保留价值；但它的数学性能收益尚未被证明，本轮还出现了更高 token 成本和更低分数。不要用这一轮把它宣传成 score improvement，也不要仅凭这一轮永久关闭；下一步应修正 injector、重复至少 3 个配对 replicate，并同时测量 recovery 后候选是否被采用、重复路线率和最终分数。在这些证据完成前，checkpoint 应作为受控 treatment 保持可开关，不能作默认开启的性能结论。

### 为什么 recovery 可能让这一轮变差

这轮结果最合理的解释不是“保存 checkpoint 的磁盘写入本身耗尽了 1 小时”，而是状态交接改变了模型的搜索路径，并叠加了 provider 噪声：

1. **checkpoint 是未验证的恢复证据。** 当前 runner 在普通 formal manifest 下仍把原来的 `best_candidate` 作为活动文件；checkpoint treatment 另外把最近的 checkpoint materialize 到 `checkpoint/`，并在 handoff prompt 中告诉新 Agent 使用它作为起点、避免已记录 blocker。也就是说，代码没有直接把未验证文件当成 Judge 已证明的结果，但模型很可能会围绕这个未验证候选继续，而不是重新探索一条独立路线。
2. **本轮确实出现了“同一条不成功路线反复延续”的轨迹。** `imo2023_p4` 的 baseline 在 3 个 assignment 内于 08:40:25 得到 proof；treatment 对同题运行了 12 个 assignment、7 次 recovery start 和多次 task-latest handoff，最终仍是 `COMPILES_WITH_SORRY`。`imo2024_p2` 的 baseline 在 09:03:46 已经 proof；treatment 在人工中断前后共运行 14 个 assignment，重复 recovery prompt/handoff 后仍没有 proof。它说明保存下来的状态被准确交接了，但状态本身没有给出可证明的下一步。
3. **恢复上下文有真实的模型预算成本。** treatment 每个 attempt 的平均 input token 约为 20,709，baseline 约为 16,198，高约 27.9%；平均 solver slot 占用从约 794.5 秒升到 847.0 秒，高约 6.6%。总 token 因此高 13.3%，但完成的 assignment 反而少 9 个（136 对 145）。这会减少独立尝试的数量，尤其容易影响需要多次换思路的题。
4. **Astra/provider 噪声放大了这个差异。** treatment 的 `AGENT_FAILURE` 为 24 对 21，`VERIFY_FAIL` 为 10 对 6；Agent 错误尾部中 treatment 有 23 个出现 provider overloaded，baseline 有 21 个。两边都 `DEGRADED`，各有 4 次 Judge probe infrastructure error，因此无法把所有失败都归到 checkpoint，但 treatment 的额外上下文和重复恢复确实发生在较差的服务健康度下。
5. **最终两分差中至少一分在外部注入前已经存在。** 实际人工注入前，baseline 已有 3 个 credited proof，treatment 有 2 个；注入后 baseline 又得到 2 个，treatment 得到 1 个。因此不能说整个 `5/12` 对 `3/12` 都是 recovery 造成的。再加上两边被人工杀掉的是不同题目（baseline `imo2024_p5`，treatment `imo2024_p2`），这轮只能支持“观察到负向结果并找到可能机制”，不能支持“checkpoint 必然降低分数”。

因此，可靠性目标和搜索策略需要解耦：应始终保存 checkpoint，但不要强制模型把单个未验证的最新候选当作唯一继续方向；恢复时保留新鲜探索预算，最好同时保留多个候选或把 checkpoint 作为可选参考，并在有新 Judge evidence 后再提升其优先级。

## 支撑结论的数据和分析

### 结果对比

| 指标 | baseline | treatment | treatment - baseline |
| --- | ---: | ---: | ---: |
| 最终 score / 12 | 5 | 3 | **-2** |
| normalized score-time AUC | 0.21986297 | 0.12814466 | **-0.09171831 (-41.7%)** |
| 原始 score-time AUC | 9498.080255 | 5535.849290 | -3962.230965 |
| 首个有效 proof 时间（秒） | 81.204490 | 79.704068 | -1.500422 |
| 有效 proof 数 | 5 | 3 | -2 |
| 3600 秒内完成的 attempt | 145 | 136 | -9 |
| solver total tokens | 2,947,685 | 3,339,675 | +391,990 (+13.3%) |
| solver input / output tokens | 2,348,667 / 599,018 | 2,816,479 / 523,196 | input +19.9%，output -12.7% |

题目级 closeout 的差异集中在两道题：两边都证明了 `imo2024_p1`、`uk2024_r1_p1`、`uk2024_r1_p2`；baseline 另外证明 `imo2023_p4` 和 `imo2024_p2`，treatment 没有新增 baseline 未证明的题。其余 7 题两边最终都是 `COMPILES_WITH_SORRY`。首个 proof 时间略快不能抵消后续 proof 数量减少，所以 treatment 的时间加权总分仍然更低。

### 机制与资源证据

| 指标 | baseline | treatment | 分母/解释 |
| --- | ---: | ---: | --- |
| checkpoint saved | 0 | 444 | final.json checkpoint evidence；不是 proof 数 |
| checkpoint published | 0 | 222 | 已发布 CPS piece；其中 89 次 publish skipped |
| checkpoint handoff | 0 | 92 | 下一 assignment 的候选交接 |
| checkpoint recovery prompt | 0 | 53 | recovery assignment 实际收到 continuation prompt |
| checkpoint save failures | 0 | 0 | 保存链路未报失败 |
| recovery succeeded | 4 | 14 | 全部 recovery attempt 的事件计数；不是题目证明数 |
| solver slot utilization | 0.99998246 | 0.99998600 | 32-slot capacity，均接近满载 |
| solver agent-seconds | 115197.979095 | 115198.387242 | 固定 horizon 下几乎相同 |

人工注入后的事件顺序也符合目标边界：baseline 记录 `AGENT_FAILURE` 后结束该 agent；treatment 记录 `checkpoint_saved`、`checkpoint_published`、`agent_recovery_scheduled`、`agent_recovery_started` 和 `checkpoint_recovery_prompt`。这证明 recovery 链路发生了，不证明恢复候选最终成为有效 proof。

### 稳定性、限制与下一步

- 只有一个配对 replicate，不能估计稳定的平均 treatment effect。gpt-6-astra provider、并发和题目分配仍有随机性。
- 预定的 1800 秒 injector 因 `awk` 语法错误失败；人工注入在两条 arm 分别于 UTC 09:06:58 和 09:07:03 完成。两边都确实收到了 SIGTERM，但这不是“严格同时刻”的注入。
- 人工注入选择的是各容器当时枚举到的第一个 Pi RPC 进程；baseline 当时属于 `imo2024_p5`，treatment 当时属于 `imo2024_p2`。被中断的逻辑题目不同，因此这轮不能把 `5/12` 对 `3/12` 的差异单独归因于 checkpoint。
- 两边 final status 都是 `DEGRADED`，各有 4 次 Judge probe infrastructure error；profiling audit 不是 clean：baseline 有 1840 个 dropped fields、1 个未闭合 span，treatment 有 1748 个 dropped fields、1895 个 unknown event 和 1 个未闭合 span。分数仍来自各自 final closeout，但健康度限制降低了外推强度。
- 本轮只证明了恢复状态被读取和交接，尚未建立“恢复后少重复路线、首次编辑更早、候选更容易被正式证明”的 adoption 因果链。后续 replicate 必须按题目记录恢复前后候选哈希、首次新编辑、首次 `judge_check`、重复路线和最终 authoritative verdict。
- 最小下一步是先修复 injector 并保留同一 12 题/Astra/3600 秒合同，运行至少 3 个配对 replicate；报告每个 replicate 的 score、nAUC、proved-task 集合、recovery adoption、tokens、slot-seconds 和 Judge/profiling health。若多轮仍无 score 改善但机制收益稳定，则按“可靠性改动而非性能优化”决策；若多轮 score 方向一致，再评估默认开启。

完整证据：[`real-ab-summary.json`](/home/ubuntu/workspace/.workspace/builds/CS-20260905-real-ab-20260905/evidence/real-ab-summary.json)；运行合同：[`batch-contract.json`](/home/ubuntu/workspace/.workspace/builds/CS-20260905-real-ab-20260905/batch-contract.json)；baseline 最终结果：[`final.json`](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/recovery-handoff-20260905/runs/checkpoint-gpt6-astra-real-ab-20260905/baseline/20260905T083503Z-f78defc4/final.json)；treatment 最终结果：[`final.json`](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/recovery-handoff-20260905/runs/checkpoint-gpt6-astra-real-ab-20260905/treatment/20260905T083503Z-2f823977/final.json)；人工注入证据：[`injection-baseline.txt`](/home/ubuntu/workspace/.workspace/builds/CS-20260905-real-ab-20260905/evidence/injection-baseline.txt)、[`injection-treatment.txt`](/home/ubuntu/workspace/.workspace/builds/CS-20260905-real-ab-20260905/evidence/injection-treatment.txt)；A/B 汇总：[`compare_runs.py`](/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/recovery-handoff-20260905/scripts/compare_runs.py)。
