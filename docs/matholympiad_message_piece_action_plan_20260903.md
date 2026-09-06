# MathOlympiadBench message / piece 结论与下一步任务交接

日期：2026-09-03
状态：基于三轮原版一小时实验的只读交接稿；未修改运行逻辑，未启动新实验

这份交接稿是详细审计报告的执行摘要。它把当前目标拆成四件事：

1. 判断当前 `message` 和 `piece` 的分工、内容和传播是否合理；
2. 判断 Agent 在选方向前能否看到同题仍在运行的方向；
3. 区分已经确认的结构化漏记、送达失败和仍需人工复核的候选；
4. 将下一步改进拆成有明确产出、验收标准和重跑边界的任务。

详细证据、案例时间线和源码语义见 [`matholympiad_message_piece_audit_20260903.md`](matholympiad_message_piece_audit_20260903.md)。

## 1. 当前决策结论

### 1.1 现状判断

当前的概念分工是正确的：

- `message` 适合短期询问、定向协作、即时状态；
- `piece` 适合可检索、可复用的策略、lemma、反例和 blocker。

但实现层面存在三个相互独立的缺口：

| 缺口 | 已确认事实 | 对目标的影响 | 判断级别 |
|---|---|---|---|
| 方向时序 | 首批 96/96 个 startup digest 为空；具体 route 声明只有 17/69 组在至少一个 peer 首次 edit 前被读到 | 同一波并发 Agent 没有可靠的避重窗口 | 已确认 |
| 生命周期与送达 | 302/1170 条 direct message 发给已结束 recipient；664/1170 被指定 recipient 读到；存在 3 条跨题不可达消息 | “发送成功”不等于“活跃同伴收到” | 已确认 |
| 知识闭环 | 反例和失败路线会停留在 message；piece 没有 adoption、refines、supersedes 等关系 | 可复用排除结论容易消失、重复或无法归因 | 已确认有例，规模需复核 |

因此结论不是“停止使用 message/piece”，而是：**保留现有两层通道，增加独立的 active-route 状态、可靠 receipt，以及从 message 到结构化知识的受控 promotion。**

### 1.2 是否值得优化和做实验

值得，但第一轮应只验证“并发方向协调”这一机制，不应同时改 allocator、prompt、检索排序和知识 schema。否则即使得分变化，也无法知道收益来自哪一项。

现有三轮只能作为历史描述性 baseline：

| run | score | assignments | pieces（Agent+runner） | messages | 状态 |
|---|---:|---:|---:|---:|---|
| `20260901T012227Z-8c90d3f0` | 5/12 | 82 | 67（62+5） | 439 | `DEGRADED` |
| `20260902T075657Z-eda06caf` | 4/12 | 76 | 74（70+4） | 434 | `DEGRADED` |
| `20260902T090313Z-ecee9c07` | 5/12 | 82 | 70（65+5） | 375 | `DEGRADED` |
| 合计 | 14 个 proved task-runs | 240 | 211（197+14） | 1248 | 3 次均 `DEGRADED` |

这些运行足以回答记录内容、可见性、时序和 schema 问题；不足以证明通信模式造成了分数差异。

## 2. 证据矩阵：目标、结论、数据和边界

| 要回答的问题 | 当前结论 | 关键数据 | 证据边界 |
|---|---|---|---|
| pieces 是否包含复用价值？ | 是。策略、lemma、blocker 占主要部分，且不少在作者仍运行时被看到 | 197 条 Agent pieces：`proof_strategy` 86、`lemma` 42、`blocker` 33、`handoff` 29；170/197 被 peer 看到，其中 167 条在作者 active 时被看到 | “被看到”不表示被理解或采用 |
| messages 是否只是短状态？ | 不是。它们同时承载询问、代码、Lean 细节、路线和负面结论 | 1248 条；正文 186,981 字符，是 pieces 84,640 字符的 2.21 倍；528 条含 Lean/API/代码特征 | 分类是词法启发式，类别有重叠 |
| 首批并发是否知道彼此方向？ | 否 | startup digest 96/96 为空；每轮有 8 个三 Agent 题组、4 个双 Agent 题组 | 这是启动时事实，不代表后续不能互相搜索 |
| adaptive 是否能看到已有知识？ | 通常能，但主要是历史 piece/message，不是活跃 route ledger | 135/144 个 adaptive startup digest 非空；重建到 557 次 context item occurrence（429 pieces、128 messages） | 没有 active 标志，无法判断内容是否仍代表当前路线 |
| 运行中信息是否能传播？ | 能，但依赖主动 polling 和自愿发送 | 738/1248 messages 被 peer 看到；728 条在 sender active 时被看到；170/197 Agent pieces 被看到 | 没有 push、claim 或 adoption 记录 |
| 方向声明是否及时？ | 不可靠 | 69 组具体 route 声明中，22 组早于 sender 首次 edit，17 组早于至少一个 peer 首次 edit；发送延迟中位数约 282 秒、p90 约 1106 秒 | 首次 edit 只是宽松上界；private reasoning 可能更早选定方向 |
| 消息是否送达正确对象？ | 不可靠 | direct 1170 条中 664 条被指定 recipient 读到，302 条发送时 recipient 已结束；3 条跨 task 必然不可达 | 读到仍不等于采用；broadcast 的全局 ack 还会隐藏其他人的未读状态 |
| 反例/排除是否进入 piece？ | 至少有明确漏记 | 已确认 message-only 反例/失败结论 3 类；93 条负面 message（89 组正文），其中 83 组未找到同作者后续相似负面 piece | 83 组是 review queue，不是 83 条已确认遗漏 |
| 重复 blocker 是否有关系？ | 有重复/增强现象，但无法表达关系 | 33 条 blocker 中 14 条是在作者已看到同题旧 blocker 后发布 | 可能是独立验证，不应简单去重或禁止冗余 |
| allocator 是否利用了方向知识？ | 没有 | 144/144 adaptive decisions 为 uniform round-robin；`features` 为空，`evidence_piece_ids` 为 0 | scheduler 的题目级 active count 不等于方向级协调 |
| exposure 是否能归因到结果？ | 不能 | piece 无 adoption/feedback；只找到一处明确跨作者 piece 前缀引用 | 不能把 exposure 当 adoption，也不能把无引用当未使用 |

## 3. 应保留的正向能力与需要修正的混乱

### 应保留

- 允许 Agent 通过 piece 交接可复用的策略、Lean helper 和 blocker。
- 允许 message 做低延迟的询问和定向协作。
- 允许有理由的独立验证，不把所有同路线工作当作错误。
- 保留现有 Judge checkpoint 和 candidate 验证，不用通信优化替代数学验证。

### 应修正

- 把“当前正在做什么”从自然语言 message 中抽出为 runner-owned 状态。
- 把“已发送、已送达、已读、已采用”拆成不同状态。
- 把明确反例、错误 bound、不可行 Lean/API 路线从短期 message 晋升到可检索 piece。
- 给相近 piece 建立 `refines / confirms / supersedes / contradicts` 关系。
- 用真实 `created_at` 做时间排序，修正 UUID 尾部伪 recency。
- 保留 source message、exposure ID 和后续 candidate/Judge 结果，形成可归因闭环。

## 4. 分析已经完成；下面才是实际实现任务

前面的审计已经完成了本轮应做的离线分析。`83` 组负面 message 候选和 reasoning 扫描结果是证据边界，不应再被写成下一位执行者的前置分析任务；它们也不阻塞协议实现。后续任务应直接修改代码、增加测试，并在实现完成后进行正式对比实验。

### I1：增加可配置的通信 treatment 开关

**要改的代码**：

- `contextswarm_mini/config.py`：增加一个明确的 CPS feature 配置对象（至少包含 `route_claim_required`、`route_claim_ttl_seconds`、`per_recipient_receipts`、`knowledge_promotion`），并纳入 `public_dict()`、manifest hash 和 run metadata；
- `contextswarm_mini/pi_agent.py`：把 treatment flag 作为受控、显式的 session capability 传给 Pi；不从 worker 环境任意读取开关；
- `configs/formal_1h_cps32_profiled_clean.toml`：显式写出所有 feature 为关闭，保持历史 baseline 不变；
- 新增同合同 treatment manifest，例如 `configs/formal_1h_cps32_route_claim.toml`，只打开 route claim 和 active roster；
- `tests/test_selection_config.py` 或新增 `tests/test_cps_feature_config.py`：覆盖默认值、未知字段、继承 manifest 和 baseline/treatment identity 不可混淆。

**验收标准**：baseline 的 config/source identity 不变；treatment 的差异只有 feature flag，不能靠环境变量悄悄改变实验合同。

**是否需要正式重跑**：不需要；先做 config focused tests。

### I2：实现 runner-owned active roster 和原子 route claim（第一个核心代码切片）

**要改的代码**：

- `contextswarm_mini/cps.py`：在 SQLite 中增加 `actors` 和 `route_claims` 表；实现 `register_actor`、`finish_actor`、`heartbeat_actor`、`list_active_actors`、`claim_route`、`update_route_claim`、`release_route_claim`；同一 `task_id + route_key` 的 claim 必须在一个写事务中检查和创建；
- `contextswarm_mini/runner.py`：在 `record_assignment()` 和两条 worker 执行路径（`_run_elastic_cps`、`_run_task_workers`）的实际 admission 时注册 actor，在所有正常结束、取消、超时、recovery exhausted 和 solved-by-peer 分支统一收尾；不得像当前 `task_worker` 路径那样预先把未来 assignment 全部写进 `actors.json`；`actors.json` 可继续作为审计 projection，但不再是活跃状态的权威源；
- `contextswarm_mini/judge_broker.py`：扩展 `_SessionClaim`、`JudgeBroker.session()` 和 `_cps_operation_locked()`，增加 `cps_active_routes`、`cps_claim_route`、`cps_update_route`、`cps_release_route` capability；从 runner-owned store 查询 active actor，并拒绝跨题或已结束 recipient；
- `contextswarm_mini/pi_agent.py`：把 route-claim-required capability 绑定到 Pi session，确保 recovery/new process 不能绕过 broker 状态；
- `contextswarm_mini/pi_solver_tools.mjs`：注册对应 broker tools；在现有 `installPathGuard` 的 `write/edit` 分支增加 treatment-only pre-edit claim gate；
- `contextswarm_mini/prompts.py`：明确顺序为“读取公开题目/骨架 → 读取 active routes → claim 或明确 independent verification → 早期 Judge checkpoint → 其余 CPS/写 candidate”。

**claim 最小字段**：

```text
claim_id, task_id, actor_id, episode, route_key, summary,
status(active|blocked|released|done), created_at, updated_at,
expires_at, released_at, independent_verification_reason
```

**行为要求**：

- `cps_active_routes` 和 `cps_claim_route` 是唯一允许在首次 Judge checkpoint 之前调用的 CPS 操作；search/inbox/send/publish 仍保持现有 Judge gate；
- `write/edit` 在 claim 成功或明确独立验证豁免之前被 treatment gate 阻止；
- 已有同 route claim 时返回冲突和现有 owner，但不强制禁止独立验证；
- heartbeat/TTL 自动使 stale claim 失效；
- actor 结束时 claim 自动转为 `released` 或 `done`；
- route claim 的冲突、创建、更新、释放和暴露都写入 bounded event，不能只依赖自由文本。
- claim store/broker 故障时必须显式记录 `route_claim_bypass_reason=unavailable`；可以按 fail-open 继续数学任务，但不能伪装成已完成 claim。

**必须新增的 focused tests**：

- 三个并发 worker 原子竞争同一个 route，只能有一个 primary claim；
- 第二个 worker 明确提供 independent-verification reason 时可以继续；
- actor finished、取消、TTL 到期后 route 可重新 claim；
- 新 assignment 只能看到已经 admission 的 actors，不看到预注册的未来 actors；
- 首次 Judge 之前只能读取 active routes/创建 claim，不能借此读取历史 pieces/messages；
- `write/edit` 在 claim 前被 treatment gate 阻止，baseline flag 下行为完全不变；
- claim store 错误或 broker 暂时不可用时按约定 fail-open，不破坏候选和 Judge 路径。

**是否需要正式重跑**：不需要。先完成单元、并发和 mock broker 测试。

### I3：修正 message delivery 和 per-recipient receipt（第二个核心代码切片）

**要改的代码**：

- `contextswarm_mini/cps.py`：增加 `message_receipts(message_id, actor_id, delivered_at, seen_at, acked_at)`；保留旧 `messages.acked_at` 只作历史兼容，不再作为 broadcast 的全局可见性条件；
- `inbox()`：按 actor 过滤自己的未 ack 消息，并记录逐接收者 delivery/seen；broadcast 对每个 active peer 独立维护 receipt；
- `ack_message()`：改为 `(message_id, actor_id)` 维度更新，并验证该 actor 确实可见；
- `contextswarm_mini/judge_broker.py`：`cps_send` 在写入前验证 recipient 是否存在、同题且 active；对 finished/cross-task recipient 返回稳定的非成功状态；
- `contextswarm_mini/pi_solver_tools.mjs`：更新 tool descriptions，区分 `send`、`delivered`、`seen` 和 `ack`；
- `tests/test_judge_broker.py`、`tests/test_judge_broker_capabilities.py`、`tests/test_cps_transactions.py`：增加 broadcast 多接收者、stale recipient、cross-task rejection 和 receipt race 测试。

**验收标准**：一个 peer ack 后，其他未读 peer 仍能读到 broadcast；发送给已结束或跨题 recipient 不再出现“写入成功但永远不可达”；每个 delivery/read/ack 都能按 actor 重建。

**是否需要正式重跑**：不需要；先完成 mock/replay 和 focused tests。

### I4：把可复用负面结论提升为结构化 piece

**要改的代码**：

- `contextswarm_mini/cps.py`：为 piece 增加结构化 metadata 或配套表，至少保存 `status`、`claim`、`evidence_or_counterexample`、`preconditions`、`next_action`、`source_message_id`、`route_claim_id`；新增 piece relation 表；
- `contextswarm_mini/judge_broker.py`：扩展 `cps_publish` 参数；增加 `cps_promote_message(message_id, kind, ...)`，保留原 message，不做 destructive move；
- `contextswarm_mini/pi_solver_tools.mjs`：增加 promotion tool schema 和 relation 字段；
- `contextswarm_mini/prompts.py`：当 message 包含具体反例、错误 bound、明确失败的 Lean/API 路线或可编译 helper 时，提示 promotion；
- 修复 `CPSStore.search()` 的 recency tie-breaker，使用真实 `created_at`，并加入 `status`、relation 和验证强度的排序规则；
- `tests/test_cps_transactions.py`、新增 `tests/test_cps_knowledge.py`、`tests/test_prompts.py`：覆盖 promotion 幂等性、source 保留、relation、搜索排序和临时猜测不自动晋升。

**建议的关系**：`refines`、`confirms`、`supersedes`、`contradicts`。不把同 route 自动视为 duplicate；独立验证必须能表达。

**验收标准**：类似 `9a93bb88…`、`1896639b…`、`948a91e0…` 的 message 可以在不丢失原文的情况下晋升为可检索 piece；后续 Agent 能看到状态、证据和适用范围，而不是只看到一段无类型正文。

**是否需要正式重跑**：实现和离线 replay 不需要；如果改变 startup digest 内容，随 I7 的正式 treatment 一起验证，不另开无法归因的实验。

### I5：记录 exposure 到 adoption 的使用链

**要改的代码**：

- `contextswarm_mini/cps.py`：增加 bounded exposure/feedback 表或事件类型，关联 `actor_id`、`episode`、item ID、surface（startup/search/inbox）、时间；
- `contextswarm_mini/judge_broker.py`：让 search/inbox/digest 返回 exposure ID；增加知识反馈 capability，至少支持 `used`、`refined`、`rejected`、`duplicate`、`stale`、`misleading`、`not_used`；
- `contextswarm_mini/runner.py`：将 exposure、candidate revision、Judge verdict 和 route claim 关联到同一 attempt；
- `contextswarm_mini/pi_solver_tools.mjs`：提供 bounded feedback tool；不得传输 raw prompt、完整 transcript 或敏感候选内容。

**验收标准**：报告可以分别计算 `seen`、`used`、`refined` 和 `rejected`；ack 不再被当成 adoption；candidate 改善能至少关联到一个可审计 exposure/claim，无法关联时明确记为 unknown。

**是否需要正式重跑**：先做 mock/replay；正式数据在 I6/I7 中采集。

### I6：先做协议层 mock/regression，再做正式 A/B

这不是新的分析任务，而是对已实现代码的验收。

**mock/regression 必须先通过**：

- 三个并发 Agent 的 claim race；
- active roster admission/finish/TTL；
- stale/cross-task message rejection；
- per-recipient broadcast receipt；
- promotion/relation/search ranking；
- exposure feedback 与 candidate/Judge 关联；
- profiling disabled fast path 不增加无关 clock/serialization 工作；
- digest 仍不触发 6000 字符截断。

**是否需要正式重跑**：只运行 focused tests、mock-agent 和 deterministic replay，不需要一小时 workload。

### I7：做唯一的第一轮正式实现实验

**实验目标**：只检验 active route coordination 是否减少无意识重复路线。

| arm | 改动 | 其他条件 |
|---|---|---|
| A | 当前 baseline，所有新 feature off | 12 题、3600s、CPS32、模型、Judge、uniform allocation 不变 |
| B | I2 的 active roster + pre-Judge route claim + pre-edit gate + route delta | 其余完全相同 |

**实验要求**：

- 至少 3 repeats/arm；
- AB/BA 顺序随机化，串行执行；
- 每个 run 固定 task hash、manifest hash、source commit、model、runtime limits；
- 保留 profiling，并单独报告 degraded/health 状态；
- 不在这一轮打开 I4 promotion、I5 adoption 或 allocator treatment。

**首要验收指标**：

1. 首批 Agent 首次 edit 前看到 active route 的比例（当前参考值 4/96）；
2. route-key collision rate、独立验证率和每题前 5 分钟 distinct routes；
3. claim 创建/暴露/释放延迟及 stale rate；
4. direct delivery/read rate 和 finished-recipient send rate；
5. score、normalized score-time AUC、time-to-first-proof、proved rate；
6. Judge calls、token/cost、assignment、timeout/recovery 作为护栏。

**是否需要正式重跑**：需要。这是实现之后才执行的第一轮真实一小时实验。

### I8：在 I7 之后决定是否做 knowledge-promotion 2×2

只有当 I7 证明 route claim 的机制指标确实改变，才做以下拆分：

| arm | active route claim | negative promotion |
|---|---|---|
| A | off | off |
| B | on | off |
| C | off | on |
| D | on | on |

I8 不改变 allocator。allocator 是否读取和利用 piece 是另一个独立研究问题。

## 5. 明确不应作为下一步的事项

- 不再把“重新做一轮离线分析”“先人工整理所有候选”写成实现前置任务；本轮分析已完成，候选只作为实现后的回归样本和可选质量审查。
- 不只通过 prompt 增加“请多发消息”或“请多写 piece”。
- 不把所有重复路线硬禁止；独立验证和不同 Lean API 路线可能有价值。
- 不按 message 数量或 piece 数量优化。
- 不把“被看到”当成“被采用”。
- 不从这三轮 `DEGRADED` 运行直接声称 score 因果或统计显著。
- 不在没有当前用户授权的情况下启动新的真实账号、远端服务或长期运行环境。

## 6. 修正后的交接完成定义

本阶段的分析/交接已经完成。后续实现阶段的完成定义是：

1. I1 的 baseline/treatment feature identity 和配置通过 focused tests；
2. I2 产生可重建的 active roster、route claim、冲突和释放记录；
3. I3 产生逐接收者 delivery/seen/ack 记录，并拒绝 stale/cross-task recipient；
4. I4 能保留 source message、结构化负面知识和 piece relations；
5. I5 能区分 exposure 与 adoption，并在未知时明确记 unknown；
6. I6 的协议和 mock regression 通过；
7. I7 的 A/B 同时报告机制指标、数学结果、成本和 health/degraded 边界；
8. 只有在需要拆分机制时才执行 I8。

当前已完成的是本轮分析和这份交接；尚未实现 I1–I5，也尚未启动 I7 正式实验。
