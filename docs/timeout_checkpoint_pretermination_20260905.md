# 超时前强制保存 checkpoint：实现与对比记录

本记录针对 MathOlympiadBench 的恢复场景：Agent 在一轮证明尚未完成时因
timeout、取消或 live session error 结束，后续 assignment 仍应看到已经写入的候选、最近的
验证边界和可继续位置。checkpoint 是恢复证据，不是证明；它不会绕过 Judge，也不会进入
score 或 best-candidate promotion。

## 先前实现的缺口

原有 `CheckpointStore` 只在 `PiAgent.run()` 返回后由 runner 调用。正常返回、软失败和
进程 drain 都能留下文件，但“deadline 已到、进程即将被 SIGTERM”这个窗口没有保存动作。
因此，正好在超时前写入 `result.lean` 的局部成果可能仍只存在于临时 Agent 工作目录中。
cooperative termination summary 只能让同一 session 主动发布 CPS 文本，也不能替代候选文件
的原子复制和哈希核对。

## 本次改动

`PiAgent.run()` 新增可选的 `on_termination_checkpoint(reason)` 回调。回调只在本次调用
仍然拥有 live workspace 时触发，并且每个 process attempt 最多触发一次：

| 触发点 | reason | 保存时机 |
| --- | --- | --- |
| cancellation event 被观察到 | `cancelled` | 向 broker 撤销/关闭进程之前 |
| soft timeout 或 hard deadline 被观察到 | `timeout` | 发送 closeout 或 SIGTERM 之前 |
| live RPC/IO exception | `error` | drain 之前 |

runner 在这个回调里构造一个仅含边界状态的 `AgentResult`，同步调用已有
`CheckpointStore.save()`，并写入 `checkpoint_pretermination_requested` 与带
`phase=pretermination` 的 `checkpoint_saved` 事件。候选通过 `O_NOFOLLOW`、大小上限、SHA-256
和 owner-only 目录保护；上下文只来自已经持久化的 CPS/Judge 投影，经过长度和敏感值过滤。
返回后的原有 result hook 仍会追加一次 attempt snapshot；Judge 返回后还会追加
`phase=final_validation`，把 validation status/feedback 补齐。保存失败是 fail-open：记录
`checkpoint_save_failed`，不改变 recovery、Judge 或 score 生命周期。

如果 Python runner 或 Pi 被外部 `SIGKILL`，或候选尚未写入文件/CPS，任何回调都无法凭空
恢复隐藏 reasoning；这类情况必须在报告中记为 unavailable/missing。checkpoint 的记录始终
带 `unverified=true`、`score_eligible=false`。当前快照边界是题目的 candidate 文件和已经
持久化的 CPS/Judge 投影，不会复制整个 workspace、Pi session 或未写入 CPS 的对话内容；
其他辅助文件的未持久化改动仍属于 missing，不能从 candidate 快照推断出来。

## 可复现的中断/延续对比

自然的一小时实验很少会在完全相同的题目阶段发生失败，直接比较自然失败数量不能回答
“保存是否成功”。因此先运行一个确定性 continuation harness，再运行真实 12 题 A/B：

1. harness 固定 1 题、1 个 slot、2 次 assignment、`pi_recovery_max_restarts=0`；第一进程
   写入 `partial-before-timeout` 后报告 timeout，第二进程是 scheduler fresh refill。
2. baseline 关闭 checkpoint；第二进程没有 `checkpoint/` handoff。
3. treatment 只打开 `checkpoint.enabled/transfer/publish`；第一进程在返回 timeout 前调用
   回调，第二进程从 `checkpoint/checkpoint.json` 和 `checkpoint/result.lean` 读取候选，重新
   计算 SHA-256，并继续写入新的候选。
4. 两边都使用同一 synthetic evaluator，`MOCK_SKIPPED` 不产生证明。这个 harness 只测
   生命周期、可见性和完整性，不提供数学质量证据。

本次在磁盘支持的 evidence 目录运行的结果为
`/home/ubuntu/workspace/.workspace/builds/CS-20260905-checkpoint-review/pretermination-ab/comparison.json`：

| 指标 | baseline | treatment |
| --- | ---: | ---: |
| process calls | 2 | 2 |
| pretermination callback | 0 | 1 |
| pretermination checkpoint saved | 0 | 1 |
| fresh assignment checkpoint handoff | 0 | 1 |
| checkpoint CPS pieces | 0 | 3 |
| handoff candidate SHA match | — | true |
| result statuses | `AGENT_FAILURE`, `MOCK_SKIPPED` | `AGENT_FAILURE`, `MOCK_SKIPPED` |

Treatment 的 3 条 checkpoint CPS piece 中，两条保存超时进程的同一局部 candidate（分别是
pretermination 和 drain 后的 attempt-result），另一条来自 fresh assignment；runner 的
`final_validation` 事件仍会记录 Judge 边界，但在这个 harness 中没有再增加一条可见 piece。
这些记录都保持 unverified，不会增加 score。baseline 与 treatment 的 assignment 数和结果
状态相同，差异只出现在保存、发布和下一 assignment 可见性。

## 正式 MathOlympiadBench A/B 方案

历史原版三次 1 小时记录中，12 题共 240 个 logical assignment，190 timeout、42 cancel、3
abnormal failure；206 个非成功尝试的 `result.lean` 与题目 baseline 不同。这些数字说明
局部成果真实存在，但不能证明当时的后续 Agent 读到了它们。

正式比较应采用同一 commit、同一 immutable image、同一题目顺序和 12 题/3600 秒/CPS32/
initial 2 per task/Pi timeout 900 秒/model/thinking/Judge/Lean/profiling 合同；串行运行、每轮
Judge drain 后再启动下一轮。唯一 treatment 是 `[checkpoint] enabled=true, transfer=true,
publish=true`，baseline 三项均为 false。termination summary、selector、recovery 次数和
其他 prompt 行为固定，不要把两种机制混成一个变量。

如果问题要直接回答“对话中的总结和排除方向是否也被下一进程采用”，应在完成这个单变量
比较后另做一个 2×2：checkpoint off/on × termination-summary off/on。四个 arm 仍固定
同一模型、题目、容量、Judge 和 horizon；先用 checkpoint-only 对比确认文件交接，再用
2×2 分离 candidate 持久化和语义总结的增益，不能把 summary 的额外 prompt/token 成本
归到 checkpoint 上。

每轮至少记录以下分母和护栏：

| 层级 | 指标 |
| --- | --- |
| process attempt | timeout/cancel/error、`checkpoint_pretermination_requested`、save failure |
| candidate | captured、changed-from-baseline、bytes、SHA mismatch |
| logical assignment | fresh handoff、读取 marker、继续后的首次编辑/首次 Judge |
| semantic continuation | 重复路线率、已排除方向再次出现率、`next_step`/`ruled_out` 可见率 |
| quality/cost | score/12、nAUC、time-to-first-proof、solver tokens、checkpoint bytes/latency |
| lifecycle | final status、Judge probe errors、unsettled jobs、broker drain、profiling audit |

“保存成功”只由完整 candidate/hash 证据确认；“后续采用”必须有下一 Agent 的显式读取或
行为证据，不能用 CPS exposure、ack 或文件存在推断。若真实 A/B 的 score/nAUC 下降而
capture/handoff 改善，应把它解释为保存成本或轨迹差异，不能宣称 checkpoint 提升解题能力。

## 当前判断

确定性 harness 已证明新增回调发生在进程 drain 前，并能把 changed candidate 原子传给
fresh assignment；它也证明 baseline 不会产生这条 handoff。现有历史重放的 206/206 保存和
旧的真实 termination-summary 运行可以作为背景，但不能替代同 commit 的正式 12 题
baseline/treatment。是否默认开启，应等待至少三轮健康度可接受的真实 A/B，且同时满足：

- timeout/cancel 的 changed candidate capture 和 hash-checked handoff 可重复；
- checkpoint 从未被当成 Judge verdict 或计分候选；
- save/publish 失败不会杀死 run；
- continuation 的重复路线率确实下降，或至少能证明后续 Agent 读到了有效边界；
- score/nAUC 没有无法解释的明显回归，额外 wall/token/CPS 成本在可接受范围。

在获得匹配的真实 Judge/Lean runtime 输入前，不启动已停止的本地 Test Lab，不把远端
`48.3:29089` 端口当作已验证 Coordinator/Judge，也不将 mock harness 数字当成数学结果。
