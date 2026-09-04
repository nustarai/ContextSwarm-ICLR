# MathOlympiadBench：timeout/cancel 终止总结与 CPS 发布对比

## 结论先行

本实验验证的不是 checkpoint 恢复，而是一个更窄的 closeout 保证：Agent 正常结束时仍按原 prompt 自主发布；只有在 live Pi session 即将 timeout、收到 cancel，或以可识别的 provider/assistant error 结束时，runner 才向同一 session 发送一次有界的 closeout command（活动 turn 用 `steer`，已 settled 的 idle session 用 `prompt`）。Agent 必须从自己的当前对话中找出尚未发布、可复用的部分成果，并通过 `cps_publish(kind="termination_summary")` 写入共享 task-local CPS。

runner 不读取或拼接整个对话，不替 Agent 编造总结，也不因本 treatment 新增
candidate/workspace 或私有 checkpoint 传递。baseline 已有的 task-local best-candidate
复制保持不变，不能把它误称为本次 checkpoint treatment。后续 Agent 仍然只通过既有
CPS 共享知识；recover/checkpoint 由独立工作流负责，见
[`recovery_checkpoint_handoff.md`](recovery_checkpoint_handoff.md)。

## 为什么要做这项对比

原版三次一小时、12 题、带 profiling 的历史记录显示，运行中确实产生过“应该留下、但没有成为结构化 CPS piece”的信息：

- 部分 Agent 只在输出/消息中留下反例、失败路线或下一步，结束前没有稳定的发布动作；
- 历史消息审计中可以看到发送给已经结束 recipient 的消息，消息“发送成功”不等于后续 Agent 一定能消费；
- 运行结束前没有一个统一的、由 runner 强制触发的“回看本次对话并发布遗漏成果”边界。

这些证据只能说明可见性/采用边界存在损失，不能反推出隐藏 reasoning，也不能把一条消息当成已验证 proof。新的 treatment 只测试能否在进程仍活着时把这部分语义成果回收进 CPS。

### 原版三次运行的可复核基线

下面的数字来自原版三次带 profiling 的 1 小时记录；`assignment` 是逻辑尝试，不是
Pi 进程启动数。三次 run 都是 `DEGRADED`，所以这些数字用于描述“哪里可能漏发”，不
用于单独证明任何得分因果：

| run | score | logical assignments | timeout | cancel | abnormal | CPS pieces / messages |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `20260901T012227Z-8c90d3f0` | 5/12 | 82 | 62 | 17 | 2 | 67 / 439 |
| `20260902T075657Z-eda06caf` | 4/12 | 76 | 64 | 10 | 0 | 74 / 434 |
| `20260902T090313Z-ecee9c07` | 5/12 | 82 | 64 | 15 | 1 | 70 / 375 |
| 合计 | — | **240** | **190** | **42** | **3** | **211 / 1248** |

对 terminal Agent 的候选文件做只读哈希核对后，235 个非成功 logical attempt 中有
206 个 `result.lean` 与题目 baseline 不同（69、68、69）。它们是实实在在产生过的
局部文件，但原版没有在结束边界把候选、反例/阻塞、验证反馈和下一步绑定成一条可检索
的共享记录。消息审计还显示 1170 条定向消息中有 302 条是在 recipient 已结束后发送，
96/96 个首批 startup digest 为空；因此“文件存在”或“消息发送成功”都不能替代
termination closeout。词法审计筛出的 93 条负面消息（约 89 组，83 组没有同作者相似
负面 piece）只是人工复核队列，不应被夸大为 83 个已确认遗漏。

人工复核的典型缺口包括 `imo2024_p2` 的具体反例、`usa2024_p2` 的假下界，以及
`uk2024_r1_p1` 的 `rfl/decide` 失败：原版能在短期消息或输出中看到它们，却没有
稳定的终止时刻 CPS 摘要。新协议的目标正是把这类已有语义成果交给同一 Agent 自己
回看并发布，而不是让 runner 根据日志猜测结论。

## 协议合同

### 正常结束

不发送额外总结。原有 worker prompt 已要求在有 meaningful discovery 时使用 `cps_publish`，因此正常完成路径保持不变，避免重复 piece 和额外 token 成本。

### timeout

`[termination_summary].grace_seconds` 是从每个 Pi invocation 的硬等待上限中预留的 bounded window（默认 45 秒，允许 1–300 秒）。达到 `hard_deadline - grace_seconds` 时，runner 向同一 RPC session 发送一次 `type="steer"`。Pi 的 RPC 合同是：`steer` 在当前 assistant turn 的工具调用完成后、下一次 LLM call 之前交付；因此它是 cooperative closeout，而不是对任意正在运行的外部工具作硬杀。详见[Pi RPC 文档](https://pi.dev/docs/latest/rpc)。收到后执行：

1. 停止当前证明/程序方向，不开启新路线；
2. 检查当前 Agent 对话和 task-local CPS，避免重复发布；
3. 发布一条带 `kind="termination_summary"`、`forced_closeout:<id>` tag 的紧凑摘要；
4. 没有新成果时也发布 `no_new_publishable_finding`，明确说明原因；
5. 回复 `TERMINATION_SUMMARY_COMPLETE` 作为诊断标记。

宽限期结束后无论是否完成，runner 都按 timeout terminalize。总结完成不会变成 proof，也不会触发候选评分；既有 `[pi.recovery]` 是否重试仍按原进程失败/deadline 合同决定，本 treatment 不改它。

### cancel

收到 cancel 后立即发送同样的 `steer`（若 `on_cancel=true`）。取消事件在总结窗口内通过两阶段 adapter 对 broker 的 CPS 写 guard 做短暂 mask，保证 Agent 仍能完成一次 CPS 写；窗口结束后恢复硬取消。若任务已经在 Pi 启动前被取消，则不会启动新的 Agent，也不会伪造总结。

### 可合作的异常结束

如果 Pi 报告 assistant/provider error 但 RPC 进程仍然存活，runner 会在观察到
`agent_settled` 后以 `reason="error"` 向同一 session 发送普通 `type="prompt"`，让已经
idle 的 session 真正开始 closeout turn；对仍在运行的 timeout/cancel 则使用
`type="steer"`。两种命令都不会创建新 Agent 或新 session。这样覆盖“会话已经失败、
但对话里可能仍有反例或验证反馈”的情况。干净的 `agent_settled` 不触发额外消息，避免
正常路径重复发布。进程已退出、SIGKILL、stdin/stdout 不可用或命令被拒绝时仍只能记
`termination_summary_missing/unavailable`，不能由 runner 猜测正文。

### 进程崩溃/硬杀

如果 Pi 已经崩溃、被 `SIGKILL` 或 stdin/stdout 不可用，就没有同一 Agent 可供 closeout command。此时只记录诊断和 `termination_summary_missing`；runner 不从其他 Agent 或 task-wide recent message 自动合成一条伪摘要。这是 cooperative 协议的明确能力边界。

## 实现与可审计字段

- `contextswarm_mini/pi_agent.py`：原生 Pi RPC `steer`/idle-session `prompt`、软窗口轮询、timeout/cancel/error 触发及 `termination_summary_requested/completed/reason`。
- `contextswarm_mini/runner.py`：按 `(task_id, actor_id, episode, process_attempt)` 记录每次请求；在 CPS 中只查询该 actor 的 `termination_summary` piece id 差集，验证真实写入条数，不信任模型回复文本。
- `contextswarm_mini/cps.py`：`piece_headers_by_actor` 是 bounded、task/actor-scoped、
  不读取正文的只读审计查询；`pieces_by_actor` 仅供需要受限正文的诊断调用。
- `contextswarm_mini/models.py`：AgentResult 增加请求、实际写入 request、完成、发布条数/发布验证字段；写入失败不会被误报为已送达。
- `final.json.termination_summary`：请求数、实际送达数、完成数、实际发布 piece 数、missing/unavailable 数及事件计数。
- `final.json.checkpoint`：本 arm 应保持 disabled/zero；若出现 checkpoint handoff，说明 manifest 或实现边界漂移，应停止比较。

Agent 发布正文受 CPS 既有长度、标签和 task-scope 约束；closeout prompt 明确禁止
凭据、原始 endpoint、绝对主机路径和完整 transcript，runner 的诊断输出另行做脱敏。
持久化 summary 正文不是由 runner 代写或通用重写，内容由 Agent 自己负责，runner 只负责
触发和核验“是否确实写进 CPS”，因此正式运行前仍应按公开 artifact 规则审查正文。
closeout mask 期间 controlled `judge_check`/`evaluate_local`/`formal_query` 也会被拒绝，
避免总结窗口悄悄开启新的验证路线；本地文件工具仍由同一 Pi session 的 prompt 合同约束。

## A/B 运行设计

Treatment manifest：[`configs/formal_1h_cps32_profiled_termination_summary.toml`](../configs/formal_1h_cps32_profiled_termination_summary.toml)

配置字段的含义固定如下：`enabled` 打开整个协议；`grace_seconds` 是有界总结窗口；
`on_timeout`、`on_cancel` 与 `on_error` 分别控制三种终止触发；`max_prompt_chars` 限制 Agent
发布的 CPS body 大小。timeout/cancel closeout 使用 `steer`；已经 `agent_settled` 的 live error 使用
idle-session `prompt`。若某个触发开关显式关闭，runner 记录 `termination_summary_unavailable`
及原因，但不会伪造一条 summary piece。

与历史 baseline [`formal_1h_cps32_profiled_clean.toml`](../configs/formal_1h_cps32_profiled_clean.toml) 固定：

- 同一 MathOlympiadBench 12 题及顺序；
- 一小时外层 horizon、CPS32、initial 2/task、`least_active`、episodes、模型/thinking、Judge/Lean profile、profiling 开关和资源上限；
- checkpoint 全部关闭；baseline 已有的 candidate-transfer、workspace 和 recover 行为
  保持不变，不作为本 treatment；本实验只增加 termination-summary command/CPS publish；
- baseline 不发送 closeout command；treatment 仅启用 `termination_summary`。

建议各 arm 串行运行 3 次，使用独立 run directory，并在每轮 Judge broker drain 完成后再开始下一轮。当前分支只提供协议、mock/focused 验证和记录格式；没有在未获授权的情况下启动真实账号/远端 1 小时实验。

## 已执行的离线证据（不是数学效果结果）

为了先验证生命周期和记录格式，执行了一组相同任务数、相同容量和相同 10 秒上限的
离线配对。两边都使用 `--mock-agent`，因此 Agent 会正常返回 `MOCK_SKIPPED`，不会触发
timeout/cancel，也不会调用 Pi、Judge 或真实账号：

| arm | run | status / score | assignments | solver timeout / cancel | summary enabled / requests / sent / completed / published | checkpoint saved / published |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| baseline | `baseline-mock-bounded/20260904T064226Z-a4e36a98` | `COMPLETED`, 0/2 (`MOCK_SKIPPED`) | 2 | 0 / 0 | false / 0 / 0 / 0 / 0 | 0 / 0 |
| treatment | `treatment-mock-bounded-2/20260904T065809Z-cf8c2dae` | `COMPLETED`, 0/2 (`MOCK_SKIPPED`) | 2 | 0 / 0 | true / 0 / 0 / 0 / 0 | 0 / 0 |

该配对只证明 treatment manifest 能在不改变正常结束路径的情况下写出同样的 run
health/final schema，并且正常结束不会额外发送 closeout command；它没有覆盖真正的终止回收能力，
不能用于声称 publish capture、后续 adoption、得分或速度提升。真正的 closeout 路径由
22 个 focused tests 覆盖，其中 fake Pi 的超时、取消、live-provider-error、extension-error、初始 prompt rejection，以及“先发布再进入既有 recover replacement”场景验证了：请求发给同一个 session、
`request_sent` 只有在对应的 `steer`/idle-session `prompt` 写入并 flush 后才为真、必须看到匹配的 user-message/settlement
生命周期，runner 只按同一 `(task, actor, episode)` 的 CPS piece id 差集核验发布。

另外执行了一组更小但真正触发终止边界的 loopback 配对（同一 fake Pi、同一题、1 秒
Pi 上限、0.5 秒 closeout 窗口、关闭 recovery；仍不调用真实 Judge/账号）：

| arm | artifact | result | closeout request / sent / completed | verified summary pieces |
| --- | --- | --- | ---: | ---: |
| baseline | `builds/timeout-checkpoint-20260904/forced-closeout-pair/baseline/` | timeout, no closeout | 0 / 0 / 0 | 0 |
| treatment | `builds/timeout-checkpoint-20260904/forced-closeout-pair/treatment/` | timeout, same process | 1 / 1 / 1 | 1 |

这组结果只证明 runner 能在同一 RPC session 的 soft boundary 发出 `steer`，并由 Agent
通过本地 broker 实际写入带 `forced_closeout:<id>` 的 CPS piece；它没有证明模型能从真实
数学对话中提炼出高质量知识。artifact 位于本任务 owner-only build 根目录，不属于提交的
benchmark 数据；正式 1 小时实验仍需授权的 Judge/Lean/runtime 输入。

一次早期手工命令误用了正式 1 小时 manifest 与无界 mock refill，产生了约 730 MiB 的
无效中间产物；该进程已停止，目录不纳入任何结果。后续运行改用上表两个有界 manifest，
以避免把调度器的无限补充行为误认为实验数据。

## 主要指标

| 指标 | 定义 | 解释 |
| --- | --- | --- |
| closeout request rate | 触发 timeout/cancel/error 的 Agent 中收到 closeout command 的比例 | runner 是否真正覆盖终止边界 |
| summary completion rate | `termination_summary_completed / requested` | Agent 是否在窗口内结束自己的 closeout turn |
| publish capture rate | 有新增 actor-scoped `termination_summary` piece 的请求比例 | 共享知识是否实际落库；不把回复文本当 receipt |
| no-new rate | 明确发布 `no_new_publishable_finding` 的比例 | 区分“确实没有新知识”和“漏发/失联” |
| missing/unavailable | 进程崩溃、CPS 禁用、closeout command 写入失败等 | 协议能力边界和故障成本 |
| score / proved rate / first-proof time | 与 baseline 相同的 Judge 指标 | 是否改善后续采用；不能只看发布量 |
| token/time overhead | closeout command 的额外 wall/token/Judge/CPS 写开销 | 判断是否值得默认开启 |
| duplicate/adoption | 后续 Agent 是否重复已标记 blocker、是否采用新 piece | 验证“共享知识”而非仅增加日志 |

所有比例都同时报告 logical assignment、timeout/cancel assignment 和 task 三个分母；不要把 AgentResult、CPS piece、消息条数混成一个分母。

## 判定标准

只有同时满足以下条件，才能说“有必要”：

1. treatment 的新增 summary piece 能显著覆盖原版历史中 message/output-only 的可复用发现；
2. 后续 Agent 在 CPS 中可见并实际减少重复路线或更快找到可验证 continuation；
3. Judge score/proved-rate/first-proof 等质量指标不恶化，且没有把未验证文本当成 proof；
4. 额外 closeout command/token/CPS 写入成本和 closeout 延迟在预设上限内；
5. missing/unavailable 仍被诚实记录，不能用 runner 事后推断补齐。

如果只观察到 piece 数增加、没有 adoption 或质量收益，应结论为“可见性改善但尚不足以默认开启”。如果 summary completion 很高但 publish capture 很低，应优先修复 Agent/CPS closeout 合同，而不是引入 checkpoint 传递。
