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

为让尚未完成 mandatory early Judge checkpoint 的 session 仍能在 closeout 回合使用
`cps_search`/`cps_publish`，treatment 的 worker system prompt 还包含一条只在
`RUNNER-REQUESTED TERMINATION CLOSEOUT` 消息出现时才生效的窄例外；baseline 的历史
prompt bytes 不变。它不会在正常结束时触发额外回合，但这点固定输入差异必须计入
正式 A/B 的 token/成本开销，不能把 treatment 宣称为完全零 prompt 差异。

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

## 真实受控 canary：匹配的 baseline / treatment（非 mock）

在用户要求“先做一次真实测试”后，补做了一组同条件的单题配对。这里的“真实”是指
容器内启动真实 `pi`（`mocked=false`），并通过任务自有的 compat proxy 访问真实
`contextswarm-lean-service`/Lean 工具链；proxy 只做 HTTP API 适配，不返回预置的
Judge 结果。两次都没有传 `--mock-agent`、`--mock-proved` 或 `--dry-run`，也没有使用
远端账号。两次都通过了 transport preflight（`status=ok`、Lean
`workspace_ready=true`、Judge result-cache backend `disabled`、NuRouter enabled，Pi
0.84.3），并在后端日志中留下真实 `POST /api/lean/jobs`（202）及轮询记录。

为隔离恢复因素，两次都只运行 `imo2024_p1` 一个逻辑 assignment，120 秒 horizon，
`pi.timeout_seconds=900`，`[pi.recovery].enabled=false`；唯一实验变量是
`termination_summary`。两次产生的 candidate SHA-256 都是
`cb34b023e37f808e1fa9cb9b2f2f47541e1e63122457a37f2b4bc8836cb0955f`，即没有把证明编辑
差异误当成发布收益。

| arm | artifact（相对本 worktree） | Agent / Judge 事实 | timeout | closeout request / sent / completed / published / missing | CPS pieces / messages | score |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| baseline | `runs/termination_summary_real_canary/real-progress-baseline-r3-20260904/20260904T105626Z-4e1ff7ea` | `mocked=false`；1 次 accepted `COMPILES_WITH_SORRY`，`proved=false` | 1 | 0 / 0 / 0 / 0 / 0 | 0 / 0 | 0 |
| treatment | `runs/termination_summary_real_canary/real-progress-treatment-current-20260904/20260904T105921Z-d3241762` | `mocked=false`；1 次 accepted `COMPILES_WITH_SORRY`，`proved=false` | 1 | **1 / 1 / 1 / 1 / 0** | **1 / 0** | 0 |

treatment 的 `final.json` 还记录了 `returncode=124`、`timed_out=true`、
`termination_summary_publish_verified=true`；这表示任务确实因 timeout 终止，但在终止前
完成了同一 Pi session 的 closeout，而不是正常完成或偷偷延长 horizon。CPS sqlite 中
唯一的一条 `termination_summary` piece 是 Agent 自己写入的，内容留下了可继续使用的
数学信息：

- 用 floor-sum 的平均值界推出 `Sₙ = n⌊(n+1)α/2⌋`，再相邻相减得到递推；
- 说明“取一个合适的奇数 n 让和落在 `(0,n)`”并不总可用（给出了 `β=0.9` 的反例）；
- 明确记录 skeleton 仍是 `by sorry`、没有已验证 proof，并给出下一步要形式化的
  `Finset.sum_le_sum`、floor 界和 Archimedean 取 n 的工作清单。

匹配 baseline 在超时前也完成了同一个真实 early `judge_check`，但由于没有终止总结命令，
其 CPS sqlite 是 0 pieces / 0 messages；运行产物里没有一条结构化的“已尝试路线、排除的
捷径、验证反馈、下一步”记录。因而这组配对已经直接复现了本改动要解决的丢失：不是让
baseline 得分变低，而是让同一份未完成工作在结束边界是否变成可检索共享知识。

这仍然不是正式 12 题 A/B，也不能从 score=0 或一条 piece 推断证明率、后续 adoption
或默认开启的必要性；它只证明真实运行链路和 closeout 回收目标在一题上成立。后续正式
实验应在相同的真实 Judge/Lean 合同下扩大到预注册的 12 题、多次独立 replicate，并
同时报告 publish capture、后续 Agent 是否采用、Judge 质量和额外时间/token 成本。

### 真实 canary 的故障/参数迭代记录

配对前的真实探针也保留在同一 run 根目录，不能被隐藏掉：

| probe | 关键结果 | 解释 |
| --- | --- | --- |
| `real-20260904/20260904T101953Z-e6fc4a35`（grace 10s） | 2 次 request/sent，0 completed，0 published，2 missing | 窗口不足以让真实 Pi 完成 cooperative closeout；不是功能成功 |
| `real-grace45-20260904/20260904T103218Z-971e5210`（旧代码） | 1 published，但 0 completed | 真实多 turn closeout 暴露了 lifecycle receipt 被 continuation `turn_start` 清零的 bug |
| `real-grace45-r2-20260904/20260904T104235Z-1f1dcb82`（修复后） | 2/2 completed，2/2 published | 修复后 receipt 与实际 CPS 写入一致；该 run 因旧 manifest 仍启用一次 recovery |

因此当前建议的下一步不是把 mock 数字写进论文，而是先固定真实 canary 的 grace/窗口
参数，再执行正式 baseline/treatment 12 题配对；任何 hard kill、Judge 不可用或窗口不足
都应按 `missing/unavailable` 计数，不能由 runner 事后猜正文。

## 真实正式 treatment：12 题、1 小时、CPS32（2026-09-04）

这次是用户要求的第一轮正式真实运行，已经实际执行完毕，不是 mock、离线回放或
canary。使用的 manifest 是
[`formal_1h_cps32_profiled_termination_summary.toml`](../configs/formal_1h_cps32_profiled_termination_summary.toml)，
运行目录为
[`20260904T144148Z-f8b61b1b`](../runs/formal_1h_cps32_profiled_termination_summary/20260904T114903Z/20260904T144148Z-f8b61b1b)。
安全汇总副本位于任务的 owner-only build evidence 目录：
[`formal_summary.json`](../../../../builds/timeout-checkpoint-20260904/formal-treatment/evidence/20260904T114903Z/formal_summary.json)。

运行合同在启动前冻结并写入 evidence：固定 MathOlympiadBench 的 12 题、3600 秒
horizon、CPS32、每题初始 2 个 Agent、每题 2 个 episode、Pi 900 秒 invocation
上限和 profiling；使用真实 Pi 与真实 `contextswarm-lean-service`/Lean Judge，
Judge result cache 关闭，`mock_flags=none`。唯一 treatment 是 timeout/cancel/error
时预留 45 秒向同一 session 发起 cooperative closeout；checkpoint 的
`enabled/transfer/publish` 均为 false。这里要特别区分：这不等于把原有的 Pi 进程级
recovery 关闭；为了保持与 baseline 的既有合同一致，本 manifest 从 `base.toml` 继承了
`[pi.recovery] enabled=true, max_restarts=1`。本轮没有新增或改变 recovery/checkpoint
逻辑，只有 termination-summary 是 treatment 变量。启动前通过真实 Judge/Lean preflight，结束后
独立检查确认任务端口、容器和 stack 均已清理。

### 运行结果

| 项目 | 结果 |
| --- | ---: |
| 运行时段（UTC） | 14:41:47–15:44:03（含收尾） |
| 最终状态 | `DEGRADED` |
| Judge 得分 | **6/12**（6 `PROVED`、3 `COMPILES_WITH_SORRY`、3 `VERIFY_FAIL`） |
| logical assignment / finished | 109 / 109 |
| solver timeout / cancel | 81 / 23 |
| Judge probe infrastructure error | 14 |
| unexpected solver process error | 2 |
| solver model sessions / tokens | 109 / 3,629,175 |

`DEGRADED` 及上述 Judge/进程错误意味着这次的分数只能作为该轮运行结果，不能和
历史原版分数直接做因果归因；它不表示 closeout 本身造成了得分变化。

### 为什么会出现几十次 retry

这里的 retry 不是 termination-summary 自己重复发送，也不是 checkpoint 恢复实验。
正式 arm 保留了原版已有的两层进程策略：

1. `[pi.retry]` 是单个 Pi invocation 内部的 provider 重试（最多 10 次）；它不增加
   `process_attempt`，也不单独产生一条终止总结事件。
2. `[pi.recovery]` 是 invocation 进程级的外层重启。本轮从 `base.toml` 继承
   `enabled=true`、`max_restarts=1`、1 秒 backoff。一个进程以非零码退出、尚未触及
   一小时 horizon、没有被 cancel，runner 才把它判为可恢复，并用**同一逻辑
   `actor_id/task/episode/session/workspace`**再启动一次；这条判定完全不读取
   termination-summary 的内容。

本轮的精确分解如下：

| 层次 | 数量 | 含义 |
| --- | ---: | --- |
| logical assignment | 109 | 调度器创建的逻辑 Agent；其中初始池 32 个，horizon 内释放槽位后又自适应补了 77 个，这 77 个是新的 assignment，不是进程 retry |
| first process attempt 可恢复 | 81 | 79 个 900 秒 Pi timeout、2 个 transport failure；都在外层 horizon 仍有余量时安排一次 restart |
| second process attempt 启动 | 81 | 每个 logical actor 最多一次，符合 `max_restarts=1` |
| second attempt 成功 | 3 | 恢复后的同一 session 正常返回，不产生 terminal closeout |
| second attempt 仍失败/被终止 | 78 | 48 次再次 timeout、2 次 transport、13 次到达外层 horizon、15 次 cancel/进程终止 |

因此终止总结事件才会是 `109 + 78 = 187`：每个 logical assignment 的第一次结束
各贡献一条 attempt-0 closeout；只有那 81 个 restart 中最终失败的 78 个才再贡献
attempt-1 closeout。3 个成功 recovery 不会再触发 terminal closeout。换句话说，187
不是 187 个不同 Agent，也不是一个 Agent 被重试了 187 次；它是 109 个逻辑 assignment
对应的 187 个“以终止为结局”的 process-attempt 事件。实际启动过的 process attempt
总数是 190（109 个首次尝试 + 81 个 recovery 尝试，其中 3 个 recovery 正常成功，
所以没有终止总结事件）。

closeout 与 recovery 的顺序也不是反过来的：`run_with_recovery` 在安排 replacement
之前先调用结果 hook，所以第一次 timeout 会先获得一次 termination-summary 机会，随后
才判断是否启动 recovery。例如 `imo2023_p2_v2` 的一个 actor 在 14:56:27 首次 timeout，
同一 session 的 summary 已完成并发布；1 秒后 recovery attempt-1 启动，15:11:28
再次 timeout，第二次 summary 才记录为 missing，随后因 `restart_limit` 结束。这个
例子说明 retry 不是 closeout 造成的，而是原有进程恢复合同独立触发的。

### 终止总结实际捕获情况

runner 的终止事件是 **attempt-level** 计数：

| 指标 | 数量 | 说明 |
| --- | ---: | --- |
| eligible termination | 187 | timeout 160、cancel 23、error 4 |
| request / request sent | 187 / 187 | 每次都成功写入同一 session 的 closeout 命令 |
| closeout completed | 101 | 45 秒窗口内收到完成生命周期证据（54.0%） |
| publication events | 109 | runner 观测到 `termination_summary_published`（58.3%） |
| missing at runner audit boundary | 78 | 事件边界当时未看到完成/发布（41.7%） |
| communication unavailable / audit failure | 0 / 0 | 没有被归入失联或审计失败 |

这里不能把 187 或 78 当成 187/78 个不同 Agent：109 个 logical assignment 中有 78 个
经历了第二次 process attempt，retry 复用了同一个 `closeout_id`。因此正式报告同时
保留 attempt-level 指标和 logical-closeout reconciliation，而不把 retry 产生的事件
重复算成丢失的 Agent。

在 run 结束后重新读取 task-local CPS sqlite，得到 113 条
`kind=termination_summary` piece，覆盖全部 12 题；每条正文都包含
`new_findings`、`counterexamples_or_ruled_out`、`validation_feedback`、`next_step`
四个强制段落，说明确实产生了可检索的共享知识，而不是只记录“命令已发送”。抽样
可见的语义包括部分递推/代数路线、被排除的捷径、Lean 验证反馈和可以继续形式化的
下一步；正文仍是 Agent 自己的未验证总结，不能当成 proof 或 Judge 结论。

这次还暴露了一个必须修复的证据边界问题：113 条 CPS row 中只有 83 个
`closeout_id` 能与 109 个 logical closeout 精确匹配（76.1%），另有 1 条 tag 有一位
字符不一致，3 条合法 summary 在 runner 的 publication 事件之后才写入 sqlite。因此
“78 missing”是 runner 终止审计时的事件计数，不是“78 份知识永久丢失”；终局 sqlite
重读发现了晚到/错绑 row，但当前 schema 没有把它们自动回补到原事件。后续应先修复
closeout-id 绑定和终局 drain/reconciliation，再把 publish capture 作为正式 A/B 的主
指标。

### 这轮实验能、不能说明什么

它已经在真实 12 题、真实 Judge 的负载下证明：终止边界确实可以让同一 Agent 把原本
只停留在对话/文件中的局部成果写入共享 CPS；同时也量化了 45 秒 cooperative window
仍有相当比例未完成的事实。它**还不能**证明证明率、后续 Agent adoption 或默认开启
的必要性，因为目前只有 treatment 一轮，尚未有同 commit、同资源合同的正式 clean
baseline；此外本轮 health 为 `DEGRADED`，且上述 event/CPS race 会污染简单的
“published/missing”比较。下一步应在同一真实 Judge 合同下串行执行匹配 baseline，
再按预注册 replicate 比较 publish capture、后续路线重复率/采用率、Judge 质量以及
额外 wall/token/CPS 成本，而不是拿本轮 6/12 对历史 4/12 或 5/12 做结论。

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
