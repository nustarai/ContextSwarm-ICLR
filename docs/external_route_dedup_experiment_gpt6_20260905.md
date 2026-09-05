# 外部路线去重：GPT-6 Astra ID 请求的一小时 paired 实验记录

> 运行流程完成，但 provider 没有对实际模型身份给出正向回执；因此本文不能被当作已确认的 GPT-6 Astra 结果。

本实验只评估“运行器在 Agent 外部观察路线并主动阻止重复路线”这一层。超时前 checkpoint、终止时总结以及向 CPS 发布恢复信息属于其他任务，本记录不把它们的效果混入去重结论。

## 要回答的问题

原有实验在不同 Agent 之间出现重复探索。这里把判断移到 CPS/runner：Agent 先提交短路线摘要，外部控制器在允许其取得路线 claim 前，对同一题目的其他 Agent 路线做有界比较；高置信度重叠时要求换方向，明确声明独立验证时才放行。

这次采用两 arm：

| arm | `external_dedup_mode` | 行为 |
| --- | --- | --- |
| control | `off` | 保留 route claim 和活动摘要，但不做外部重叠判断，不阻止 claim |
| treatment | `enforce` | lexical overlap 达到阈值时返回 `semantic_conflict`/`switch_required=true`；给出独立验证理由可以继续 |

固定参数为 `external_dedup_similarity_threshold=0.78`、`external_dedup_min_shared_tokens=3`。摘要先做隐私安全的 token 化、停用词过滤和有限词形归一化，再用 containment/Jaccard 组合分数。摘要太短、太泛或共享词不足时保留 unknown 并继续。当前实现是在 claim 写入前仲裁；它不会异步杀掉一个已经运行的 Agent，也不会替 Agent 选择数学路线。

实现提交为 `e4353b44c07515fc9e23dca9668340fa42a7678a`（`feat: add external route dedup controller`）。主要代码和测试见 [`route_dedup.py`](../contextswarm_mini/route_dedup.py)、[`cps.py`](../contextswarm_mini/cps.py)、[`test_route_dedup.py`](../tests/test_route_dedup.py) 和 [`test_cps_route_claims.py`](../tests/test_cps_route_claims.py)。

## 固定实验合同

- 数据集：12 道 MathOlympiadBench 题目，顺序和 seed 两 arm 相同。
- 时限：`3600 s`；并行上限：32；每题初始 2 个 Agent；实际运行到 drain/closeout 后才结束。
- control run：`20260905T071908Z-df7b2db9`。
- treatment run：`20260905T071908Z-e6b3eb73`。
- 两 arm 使用独立 CPS/数据库、Judge、worker workspace、CPU/NUMA 分区和 container stack；两 arm 的 transport preflight 均为 `status=ok`，Judge 为 32 个 ready worker，result cache 为 disabled。
- 构建镜像固定在 image digest `sha256:976aac1b3b4056b57b4e47dde16fec329c0804149a24659ea09fdf88f2b53426`，镜像 label/source commit 均回读为上述 `e4353b4`。
- 配置要求的模型字符串为 `openai-codex/gpt-6-astra`，两个 `run_meta.json` 和每个 Agent 的启动命令都记录了该值。

本次运行还记录到 Pi 警告：`Model "gpt-6-astra" not found for provider "openai-codex". Using custom model id.` control 有 30 个、treatment 有 29 个 Agent error tail 出现该警告。因此，“请求的是 GPT-6 Astra 标识”已验证，但 provider 端是否把它映射到正式模型的运行时身份没有被这次实验独立证明；后续正式比较前应先修正/验证 provider registry。

## 运行结果

两边 runner 均以 `rc=0` 结束，且都完成了 3600 秒 horizon、drain 和 closeout。运行状态都是 `DEGRADED`，不是健康的 `OK`：

| 指标 | control | treatment (`enforce`) |
| --- | ---: | ---: |
| 最终得分 / 12 | 6 | 4 |
| `score_time.normalized_score_time_auc` | 0.209250 | 0.192996 |
| 首个 verified proof（秒） | 175.86 | 235.65 |
| verified proofs | 6 | 4 |
| `COMPILES_WITH_SORRY` | 5 | 6 |
| `VERIFY_FAIL` | 1 | 2 |
| Agent attempts | 151 | 135 |
| Agent timeout count | 51 | 52 |
| CPS route claims | 179 | 156 |
| CPS pieces | 271 | 263 |
| CPS messages | 541 | 471 |

健康状态中的共同问题包括 4 次 `judge_probe_infrastructure_error`；control 还有 1 次、treatment 还有 2 次 `unexpected_process_error`。这解释了 `DEGRADED`，也意味着单个 paired run 的得分差不能直接归因于去重。

### 去重信号

本次 treatment 的 CPS 中没有 `external_route_dedup_decision` 事件，也没有 `semantic_conflict` 或 `switch_required` 记录；control 也没有这类事件（control 本来就是 off）。两边收尾时 active/blocked claim 都为 0，route claim 最终分别为 `done/released = 61/118` 和 `61/95`。

为了区分“没有触发”和“没有重复”，对每个 run 的全部 route claim 做了同题目、不同 Agent 的离线 lexical 复核：

| 离线复核 | control | treatment |
| --- | ---: | ---: |
| 跨 Agent claim pairs | 1382 | 998 |
| score ≥ 0.60 | 1（最高 0.703） | 0 |
| score ≥ 0.78（本实验阈值） | 0 | 0 |
| score ≥ 0.82（same_route 判定带） | 0 | 0 |

因此本次实验没有观察到达到固定 lexical 阈值的候选重叠。这个结果不能证明数学语义上没有重复：当前比较器只看路线摘要的有限 lexical 信号，而且本版本只查询 claim 时仍处于 `active`/`blocked` 的 bounded peer projection；一个 Agent 结束后，后续 Agent 再重复同一路线不会被这一版的在线查询捕获。后一个边界是下一轮实验前必须明确修复或接受的设计缺口。

## profiling 与证据质量

两边的 profiling 文件均为 `real`，序列无 gap/duplicate/out-of-order，termination 事件显示 `profile/run/drain/horizon` 均结束，主要 coverage（agent wrapper、CPS、Judge、max_parallel）均为 covered。但精确审计返回 `exit_code=1`：

| 审计 | rows | dropped fields | span_missing_end | `ok` |
| --- | ---: | ---: | ---: | --- |
| control | 94,497 | 120 rows / 362 fields | 1 | false |
| treatment | 87,988 | 104 rows / 314 fields | 1 | false |

所以这些 profiling 可以用于规模和时序分析，但不能作为“profiling 审计完全通过”的证据。两边 runner log 还各有 closeout 阶段的 `BrokenPipeError`；最终 runner rc 仍为 0，Judge broker closeout 显示 `drained=true`、`remote_unsettled_jobs=0`，但该 closeout 噪声应在后续修复。

## 结论和下一步

这次实现验证了外部去重仲裁的接口和安全边界：判断在 CPS/runner 完成，Agent 只提交观察信号；enforce 可以在路线 claim 前要求换方向，并保留独立验证的放行通道。单元测试 81 项通过，配置校验、broker envelope 和 Pi tool surface 均覆盖。

这一个 GPT-6 配置的 1 小时 paired run 没有触发任何 enforce 决策，且两 arm 都是 degraded；因此不能据此宣布改动必要、有效或有害。当前最直接的工程结论是：先把在线比较从“只看 active/blocked”扩展到本次 run 内有界的历史 `released/done` claims，并为比较窗口/peer 状态写审计字段；同时验证 `openai-codex/gpt-6-astra` 的真实 provider 映射。完成后固定阈值重新跑至少 3 个 paired seeds，报告每次 claim 的 overlap rate、blocked/switch rate、独立验证放行率、重复路线的人工抽样 precision/recall、proof score/time 和 degraded/error rate。只有在这些数据稳定后，才决定是否把 enforce 作为默认策略；在此之前建议保留 advisory 或显式 opt-in enforce。

精确运行产物位于：

- control：`/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/external-dedup-control-20260905/runs/strong-activity-paired-20260905/control/20260905T071908Z-df7b2db9/`
- treatment：`/home/ubuntu/workspace/.workspace/worktrees/ContextSwarm-ICLR/strong-activity-feedback-20260905/runs/strong-activity-paired-20260905/treatment/20260905T071908Z-e6b3eb73/`
- exact profiling audits：`/home/ubuntu/workspace/.workspace/builds/external-dedup-gpt6-r3/evidence/control-profiling-audit-exact.json` 和 `treatment-profiling-audit-exact.json`

paired-run 脚本的 treatment `run-id.txt` 曾从 worktree 中误选一个旧 run；上述 treatment 路径和本报告所有数字均按 `run_meta.json` 的精确 ID 手工复核，旧 run 没有被用于结果统计。
