# MathOlympiadBench 三轮原版实验的 message / piece 内容与协作时序审计

日期：2026-09-03

状态：只读分析完成；未修改运行逻辑，未启动新实验

按用户目标整理的实际实现任务、代码触点、测试和实验顺序见 [`matholympiad_message_piece_action_plan_20260903.md`](matholympiad_message_piece_action_plan_20260903.md)。

## 1. 结论

当前设计的基本分工方向是对的：`message` 适合短期询问、定向协作和快速状态同步，`piece` 适合可检索、可复用的证明策略、Lean 片段、反例和阻塞结论。三轮数据也表明 Agent 确实在这样使用它们：197 条 Agent-authored pieces 中有 86 条 `proof_strategy`、42 条 `lemma`、33 条 `blocker`；而 1248 条 messages 更短、更密集，主要用于询问、催要代码、交换当前进展和向多个同伴发送同一信息。

但当前实现还不能可靠解决“并发 Agent 在选方向前避免撞车”这个核心问题：

1. 三轮共有 96 个首批 assignment，它们的 startup digest 全部为空。每轮 12 道题中有 8 道题同时启动 3 个 Agent、4 道题同时启动 2 个 Agent；这些首批 Agent 在启动时完全不知道同题其他 Agent 将选什么方向。
2. Worker 必须先完成一次 terminal `judge_check` 才能使用任何 CPS 工具。之后虽有 232/240 个 Agent 在第一次 `edit` 前（包括全程没有 edit 的 Agent）调用过 `cps_search`，但首批 96 个 Agent 中只有 19 个在第一次 `edit` 前实际拿到任何同伴内容，只有 4 个拿到可识别的具体方向消息。
3. 检出的 69 组不重复“具体方向声明”中，只有 22 组在声明者第一次 `edit` 前发出，53 组最终被至少一个同伴读到，只有 17 组在至少一个接收者第一次 `edit` 前被读到。第一次 `edit` 已经是很宽松的上界；Agent 完全可能在更早的私有 reasoning 中已经选定方向。
4. 当前 roster 只有 `actor_id / task_id / episode`，没有 `active / finished`、当前方向、声明时间或 TTL。1170 条定向消息中有 302 条是在收件 Agent 已结束后才发出，506 条最终没有被指定收件人读到。
5. “正在运行者之间交换成果”实际上存在，而且很常见：被同伴看到的 170 条 Agent pieces 中，167 条至少有一次是在作者仍运行时被看到；被同伴看到的 738 条 messages 中，728 条如此。问题是接收者无法知道作者是否仍活跃，也没有原子性的方向声明或占位机制。因此这是实时数据流，不是可靠的实时方向协调。

结构化知识的质量总体有价值，但分类和收敛机制不足：

- 33 条显式 `blocker` 说明 Agent 会记录排除方向、语义一致性检查和失败路线；具体反例也确实被记录过。
- 仍有明确的 message-only 反例和失败结论，没有被提升为 piece。另有 83 组“负面 message 但未找到同作者后续相似负面 piece”的自动复核候选；它们不能全部算作确认遗漏，但足以说明边界不稳定。
- 33 条 blocker 中有 14 条是在作者已经读过同题旧 blocker 后再次发布的。部分是更强的独立证据，但当前没有 `refines / supersedes / confirms` 关系，摘要只能把相近结论并列堆叠。
- Piece 检索把 UUID 尾部当作所谓 recency tie-breaker；UUID 尾部并不是时间。代码注释与实际排序语义不一致，无法稳定保证较新的或验证更强的知识优先。

建议继续优化并做对比实验，但先优化可观测性，再评价分数：最优先增加 runner-owned 活跃 roster、结构化 route claim、逐接收者 delivery/ack、message-to-piece promotion 以及 piece 的验证状态和关系字段。核心实验应先比较“现状”与“强制在第一次实质 edit 前声明/检查活跃方向”，而不是同时改调度、提示词、知识 schema 和检索排序，否则无法知道收益来自哪里。

## 2. 审计范围与证据边界

本报告只分析以下三次相同合同下的原版一小时实验：

| 标签 | run id | score | assignments | pieces（Agent + runner） | messages | final status |
|---|---|---:|---:|---:|---:|---|
| baseline-prior | `20260901T012227Z-8c90d3f0` | 5/12 | 82 | 67（62 + 5） | 439 | `DEGRADED` |
| baseline-r1 | `20260902T075657Z-eda06caf` | 4/12 | 76 | 74（70 + 4） | 434 | `DEGRADED` |
| baseline-r2 | `20260902T090313Z-ecee9c07` | 5/12 | 82 | 70（65 + 5） | 375 | `DEGRADED` |
| 合计 | 3 runs | 14 个 proved task-runs | 240 | 211（197 + 14） | 1248 | 3 次均 `DEGRADED` |

三轮均使用冻结的 12 道 MathOlympiadBench 题、3600 秒 horizon、CPS 32、blackboard communication、uniform allocation、相同模型与 source commit `33296b07634c708412326c2808d5782dab3f788e`。三轮都有较多 timeout/recovery 噪声且最终为 `DEGRADED`，所以本文可以回答“记录了什么、何时可见、是否送达”，不能把通信模式与 5/4/5 得分建立因果关系。

使用的证据层包括：

- `cps.sqlite3`：所有 piece/message 的持久行、作者、收件人、类型、标题、正文长度和时间；
- `communication_trace.jsonl`：piece 创建、message 发送和 ack 事件；
- `events.jsonl`：assignment、实际结束时间、initial/adaptive phase；
- `allocation_decisions.jsonl`：调度快照、策略、evidence IDs 和选题结果；
- `profiling.jsonl`：startup digest 的行数以及 CPS query/materialize 次数；
- `pi_session_index.jsonl` 和 240 个唯一 session 文件：工具调用顺序、startup prompt、`cps_search`/`cps_inbox` 返回的具体 item ID、第一次 `edit` 时间。

审计脚本逐个重建了 startup digest，并与 session 中的实际 prompt 比较。239/240 个 prompt 完全一致；剩余一个 prompt 报告 4 行上下文，但因同秒 ack/排序边界未做到字节一致，本报告对这 4 行不计曝光。因此所有“被看到”数字都是保守下界，误差上限集中在这一个 startup digest。240 个 prompt 都没有触发 6000 字符截断。

本文中的“看到”严格表示 item 被放入 startup prompt，或由 `cps_search`/`cps_inbox` 实际返回；它不表示 Agent 理解、采用或正确使用。共享 candidate 文件是另一条协作通道，不在 message/piece 曝光统计内。“第一次 edit 前”把全程没有 edit 的 Agent 也计入，并且 Agent 可能在 edit 前的私有 reasoning 中已经选定方向，因此它只能作为宽松上界。

内容类别中，piece 的 `kind` 是数据库真值；message 的“询问、方向、负面结论、Lean 细节”等是高精度但不完备的词法分类，可以用于比较分布，不能当作完整语义标注。对具体案例另做了人工核对和时间线复核。

## 3. 系统实际上在哪些阶段知道什么

需要把“调度器选择哪道题”和“Worker 选择哪条证明方向”分开。

| 阶段 | 当前可见信息 | 三轮中的实际行为 | 缺失信息 |
|---|---|---|---|
| Adaptive allocator 选题 | Snapshot 包含题目级 `active_agents`、attempt、score、recent pieces 等 | 144/144 次使用 `uniform`；`features` 全空，`evidence_piece_ids` 总计 0，理由均为 deterministic round-robin；144 次被选题当时都有 active Agent | 不读取 piece 内容，不知道子方向；这是本 baseline arm 的设计，不是运行异常 |
| Assignment 入场 | Runner 把 `{actor_id, task_id, episode}` append 到 `actors.json` | Roster 只增不减；201/240 个 Agent 调用过 `cps_actors` | 没有 active/finished、current route、heartbeat、claim time、TTL |
| 构造 startup prompt | 同题最多 8 条 ranked pieces + 最多 8 条对该 actor 可见且未 ack 的 messages | 首批 96/96 为空；adaptive 135/144 非空 | 首批无协调屏障；message 只按收件人过滤；无 active-route 专区 |
| Worker 初始检查 | 先读文件并完成 terminal `judge_check` | 240 个 Worker 的第一个工具都是 `read` 或 `ls`；239 个有 CPS 调用的 Worker 都在首次 Judge 调用之后才调用 CPS | 所有 CPS 操作在 Judge checkpoint 前都被 broker 拒绝 |
| Worker 选方向与实现 | 可自愿 `cps_search`、`cps_inbox`、`cps_actors`、`cps_send`、`cps_publish` | 239/240 用过 search，213/240 用过 inbox，212/240 发过 message，150/240 发布过 Agent piece | 没有必须先 claim route 的状态机，也没有“有人刚选择了同一路线”的主动通知 |
| 知识消费 | 工具结果中有 piece/message ID；message 可以 ack | 仅 47/1248 message rows 有 ack；piece 没有 adoption/feedback 记录 | 无法区分 `seen / used / refined / rejected / ignored` |

源码语义也支持上述结论：

- `pieces` 只有 `id/task_id/author/kind/title/body/tags/created_at/active`；`messages` 只有 `sender/recipient/body/created_at/acked_at`。
- Startup digest 由同题 piece search 和 actor inbox 组成，渲染时 pieces 在前、messages 在后，总预算 6000 字符。
- Inbox 只返回同题、未 ack、广播或发给当前 actor 的最新 8 条消息；没有 push。
- Broker 在首次 terminal Judge checkpoint 前统一返回 `JUDGE_CHECK_REQUIRED`。
- Roster 的公开字段被硬限制为 `actor_id/task_id/episode`。
- `UniformAllocationPolicy` 明确“ignores CPS progress”，只在 eligible tasks 上做 deterministic round-robin。

对应实现入口是 [`contextswarm_mini/cps.py`](../contextswarm_mini/cps.py)、[`contextswarm_mini/prompts.py`](../contextswarm_mini/prompts.py)、[`contextswarm_mini/judge_broker.py`](../contextswarm_mini/judge_broker.py)、[`contextswarm_mini/runner.py`](../contextswarm_mini/runner.py) 和 [`contextswarm_mini/allocation.py`](../contextswarm_mini/allocation.py)。本报告按实验 source commit 的实现语义重建，而不是按后来分支推测。

## 4. Pieces：记录了什么，质量如何

### 4.1 类型分布

以下只统计 197 条 Agent-authored pieces；14 条 runner-authored `validation_result` 单独列出。

| kind | 数量 | Agent pieces 占比 | 典型内容 |
|---|---:|---:|---|
| `proof_strategy` | 86 | 43.65% | 数学化简、归纳/递推路线、上下界结构、正反向拆分 |
| `lemma` | 42 | 21.32% | 可复用 Lean helper、已经编译的局部引理、API 使用模式 |
| `blocker` | 33 | 16.75% | 反例、语义一致性、某条捷径不成立、自动化或 API 阻塞 |
| `handoff` | 29 | 14.72% | 当前 candidate 状态、剩余 sorry、下一步建议、请求接力 |
| `candidate` | 5 | 2.54% | 某个具体候选证明状态；仅 baseline-r1 使用了该 kind |
| `example` | 1 | 0.51% | 示例；仅 baseline-prior 出现 |
| `question` | 1 | 0.51% | 待解决问题；仅 baseline-r2 出现 |
| runner `validation_result` | 14 | — | Runner 写入的权威 `PROVED` 元数据 |

Piece 正文明显比 message 更适合持久知识：三轮正文中位数分别为 388、358、378 字符；197 条 Agent piece 中只有 3 条没有 tags，没有完全相同的重复 piece。240 个 assignment 中有 150 个至少发布一条 Agent piece，90 个没有发布；其中 20 个既没有发布 piece，也没有发送 message。后两个数字只能说明没有 CPS 输出，不能自动解释为遗漏，因为有些任务很快结束、有些 Agent 没得到可复用结论。

但 taxonomy 没有强约束。`kind` 实际上是任意字符串，只阻止 Agent 冒用 runner-only kind。因此相似语义可能被放进 `blocker`、`proof_strategy`、`handoff` 或 `candidate`；`candidate/example/question` 又只在个别 run 出现。当前类型更像写作提示，不是稳定的数据合同。

### 4.2 具体的高价值例子

下面只做必要的转述，不复制完整正文。

| piece ID | task | kind | 内容与评价 |
|---|---|---|---|
| `a2033e01…` | `imo2023_p4` | `proof_strategy` | 把目标变成 prefix-sum recurrence，记录 increment 至少为 1、等号条件及不能连续取等；信息足以让同伴继续形式化，是好的 durable strategy。该题随后得到 runner `PROVED` validation。 |
| `44084eda…` | `imo2023_p3` | `blocker` | 用 `k=2`、`P=X^2` 的具体模型排除“前提矛盾所以 ex-falso”的捷径，并记录 `simp/aesop/omega` 没有直接闭合；这是应当保留的反例型知识。 |
| `99403fcf…` | `usa2024_p2` | `blocker` | 核对 `nonempty_inter` 等定义，确认前提并非 parser/vacuity 漏洞；阻止后续 Agent 重复尝试伪捷径。 |
| `acf3e919…` | `uk2024_r1_p1` | `lemma` | 记录已经编译的 `extendPerm` permutation-extension helper；这是最典型的可复用 Lean artifact。 |
| `e510cd75…` | `usa2024_p2` | `proof_strategy` | 记录 alternating-quotient/inclusion-exclusion 型下界突破和后续形式化方向；正文较完整，属于值得进入摘要的核心策略。 |
| `a747bd5f…` | `imo2023_p3` | `lemma` | 标题声称 forward witness 已通过 monic/degree/coeff，但正文只有 64 字符，缺少适用条件、代码位置和剩余障碍；类型正确但复用信息不足。 |

### 4.3 记录了反例，但没有收敛成一条知识

33 条 blocker 中有 14 条是在作者已经通过 startup/search 看到同题旧 blocker 后发布的。最清楚的是 `imo2023_p2_v2`：

- baseline-prior 依次出现“baseline/aesop 后无立即矛盾”→“假设看起来一致”→“具体欧氏模型说明可实现”→“automation 失败且 premise 可实现”→“仍无矛盾”；后四条的作者都已经看过前一条。
- baseline-r2 又出现“初始 geometry 一致”→“early checks 无矛盾”→“angle/API 检查仍无矛盾”→“targeted checks 无 closure”→“详细 semantic audit 无矛盾”；同样形成链式重复。

这不代表后续 piece 没价值：具体坐标模型显然比“没有找到矛盾”更强。问题是 schema 无法表达“confirm/refine/supersede 旧结论”，检索也不会自动把五条合并成一条带证据层级的结论。最终 digest 消耗多个 slot，却仍不能告诉 Agent 哪条是当前最强版本。

### 4.4 Piece 的实际可见性

- 197 条 Agent pieces 中至少 170 条被另一 Agent 看到，覆盖率 86.29%；27 条在该 run 内未被同伴通过 CPS 看到。
- 170 条被看到的 piece 中，167 条至少有一次是在作者仍运行时被看到；所以 piece 并不只传播“已完成任务”的历史知识。
- 三轮中首次被同伴看到的中位延迟分别约为 116、103、116 秒。
- 14 条 runner validation 全在任务收尾/证明完成附近产生，没有被后续 Agent 看到，这符合任务很快停止或不再 refill 的生命周期，不应与 Agent piece 的复用率混算。

## 5. Messages：记录了什么，送达是否合理

### 5.1 总体分布

| 指标 | 数值 | 解读 |
|---|---:|---|
| message rows | 1248 | 是 Agent pieces 的 6.34 倍；平均每 assignment 5.2 条 |
| 定向 / 广播 | 1170 / 78 | 93.75% 是 point-to-point，只有 6.25% 是 task broadcast |
| 正文总字符 | 186,981 | 是全部 piece 正文 84,640 字符的 2.21 倍；大量信息停留在短期通道 |
| 正文中位数 | 每 run 118–127 字符 | 多数确实是短消息，但最大值达到 2763 字符 |
| 询问/协调类（词法，重叠） | 943 | 75.56%；大量为“有无思路/代码/进展”的 solicitation |
| 含 Lean/API/代码特征（词法，重叠） | 528 | 42.31%；message 并不只是状态，也承载技术正文 |
| 含负面/排除特征（词法，重叠） | 93 | 7.45%；其中包含应当晋升为 piece 的反例和失败结论 |
| 可识别当前工作声明 | 119 rows / 101 组不重复正文 | 不是协议字段，只是自然语言中碰巧出现 |
| 可识别具体 route 声明 | 76 rows / 69 组不重复正文 | 仅约占全部 message 的 6%；没有结构化 route ID |
| acked rows | 47 | 仅占 3.77%；不能据此推断其余都没用 |

完全相同的 message body 有 115 条额外重复行。核对后，这 115 条全部是同一 sender 把同样内容 fan-out 给不同 recipient；共有 55 个 fan-out group、170 个 message rows，最大一次发给 7 人。没有发现同一 sender 对同一 recipient 重复发送完全相同正文。因此这不是重试写重，而是 Agent 用多次 direct send 模拟 task broadcast，增加了写入和收件管理开销。

### 5.2 送达与生命周期

| 指标 | 数值 | 影响 |
|---|---:|---|
| 被任一同伴实际看到 | 至少 738/1248（59.13%） | 其余至少 510 条未形成 CPS 信息传递 |
| 定向消息被指定收件人看到 | 664/1170（56.75%） | 506 条定向消息未被指定收件人读取 |
| 发出时收件人已经结束 | 302/1170（25.81%） | Static roster 诱导 Agent 向 stale recipient 发送 |
| 已知 actor 但跨 task、必然不可达 | 3 | Sender 的 task_id 被写入 message；目标 actor 的 inbox 按自己的 task_id 过滤 |
| 被看到且当时 sender 仍运行 | 728/738（98.64%） | 当前工作产生的信息确实能够实时传播 |

`cps_send` 没有验证 recipient 是否存在、是否同题或是否仍 active；`cps_actors` 又只能返回 append-only roster。两者叠加，造成“格式上发送成功、语义上不会送达”的静默失败。

Broadcast 的 ack 还有一个 schema 问题：`messages` 表只有一个全局 `acked_at`。任一 Agent ack 广播后，这条消息就从所有人的 inbox 消失。三轮有 10 条广播被 ack；按 ack 时仍活跃的同题 Agent 重建，至少有 3 个尚未读到的活跃 peer 会因首个全局 ack 而失去该消息。当前观测影响不大，但语义不正确，应改为 `(message_id, actor_id)` receipt。

## 6. 核心问题：选方向前能否看到其他正在做的方向

### 6.1 直接答案

当前答案是：**偶尔可以，但没有可靠保证，也无法识别“当前正在跑”这一状态。**

- 对首批并发 Agent：启动时一定看不到，因为 96/96 startup digest 为空。
- 对 adaptive Agent：通常能看到已有知识，135/144 startup digest 非空；重建出的 557 个 startup item occurrences 中，429 个是 pieces、128 个是 messages。这里主要是已形成的技术知识，而不是活跃方向声明。
- 对运行中的 peer：Agent 可以在 Judge checkpoint 后主动搜索/拉取，且实际有 167 条 piece、728 条 message 在作者仍活跃时被同伴看到；但内容没有 active 标志。
- 对方向避重：69 组具体 route 声明中，只有 22 组在 sender 第一次 `edit` 前发出，只有 17 组在至少一个 peer 第一次 `edit` 前被读到。Route 声明从 assignment 到发送的 message-row 中位延迟约 282 秒，p90 约 1106 秒，通常已经晚于“刚开始选择方向”的窗口。
- 从接收者看，240 个 Agent 中有 157 个在第一次 `edit` 前拿到任一 peer 内容；但首批只有 19/96，adaptive 则有 138/144。第一次 `edit` 前拿到具体 route message 的是 32/240，其中首批只有 4/96。

因此当前机制更适合“后续 Agent 接续前人工作”，不适合“同一波并发 Agent 在最初几十秒协商互斥方向”。

### 6.2 一个接近用户所述场景、但时序失败的例子

baseline-prior 的 `imo2023_p4`：

1. `agent-imo2023_p4-1` 在 message `28a68834…` 中同步了 prefix recurrence / Finset formalization 方向；message 于 `01:26:10.236Z` 发送。
2. 发送者自己的第一次 edit 是 `01:26:42.272Z`，所以这条声明确实早于发送者实施代码。
3. 收件人 `agent-imo2023_p4-2` 的第一次 edit 已发生在 `01:25:03.974Z`，到 `01:30:48.153Z` 才通过 inbox 看到这条 message。

这说明自然语言同步本身可以做到“A 先说自己方向”，但 B 的 polling 时机没有保证，无法在 B 开始前阻止方向重叠。

### 6.3 机制也有成功工作的时候

同一 run 的 `imo2024_p5` 提供了明确的 live reuse 证据：

1. `agent-imo2024_p5-5` 在 `02:12:45Z` 发布 piece `90288972…`，内容是可复用的 vertical-path helper chain。
2. `agent-imo2024_p5-7` 的 `cps_search` 在 `02:14:45.733Z` 返回该 piece。
3. 5 秒后，它在 message `cde1b59d…` 中明确提到从 CPS search 找到并使用了 `902889…` helper。
4. 两个 Agent 都到 `02:21` 以后才结束，所以这是正在运行者之间的真实检索和复用，不是只读历史 handoff。

另一个较好的 direction-broadcast 例子是 message `98044988…`：它同步了 upper/lower-bound 的具体工作路线，最终被 6 个 peer 看到，其中 5 个在各自第一次 edit 前看到。问题仍在于这是 Agent 自发行为，不是每个并发组都必须经过的协议步骤。

### 6.4 调度器有没有参考这些方向

没有。这三轮使用的是刻意保持简单的 `uniform` baseline：144 次 adaptive decision 的 `features` 都为空、`evidence_piece_ids` 总计为 0，全部按 eligible task 的固定 round-robin 选题。调度 snapshot 中虽有题目级 `active_agents` 和 recent pieces，但 uniform policy 明确忽略 CPS progress；它也没有 route 级状态可用。

这一区分很重要：当前 scheduler 解决的是“给哪道题补一个 slot”，CPS message/piece 解决的是“该题内部如何协作”。不能把题目级 active count 当成方向级去重。

## 7. 是否存在没有进入结构化知识的反例或结论

答案是肯定的，至少存在以下人工确认的例子。

### 7.1 已确认的 message-only 负面知识

| message ID | task | 被记录的结论 | 为什么应晋升为 piece |
|---|---|---|---|
| `9a93bb88…`、`2b6a6f55…` | `imo2024_p2` | 用 `a=3, b=6` 等具体值说明一个 naive gcd/normalization 等价式为假；同一 sender 分别发给两个 peer | 是明确反例，可排除一整类归一化证明；该 sender 没有发布任何 piece |
| `1896639b…` | `usa2024_p2` | 给出具体计算，指出一个候选 `h_U` 下界为假 | Sender 后来发布了正向突破 piece，但没有把被否定的下界写进结构化负面知识；后续 Agent 仍可能重试 |
| `948a91e0…` | `uk2024_r1_p1` | 记录 direct `rfl/decide` 路线的失败/资源表现 | Sender 后来发布的是 recurrence strategy，不包含这条 API/计算排除结论 |

这几条都已持久化在 message 表，所以不是“完全丢失”；但 message 不能由 `cps_search` 检索，最多进入特定 recipient 的未 ack inbox，ack 后还会从后续 inbox/digest 消失。对于反例、否定某个 lemma、确认 parser/semantic shortcut 不成立这类有跨 Agent 复用价值的信息，留在 message 等同于放错了生命周期层级。

### 7.2 已记录但不可达

baseline-prior 的 message `fdaa4030…` 从 `imo2023_p2_v2` actor 发给 `imo2023_p3` actor。写入时 message 的 task_id 固定为 sender task，而 recipient inbox 只读取自己的 task_id，因此目标永远看不到。三轮共发现 3 条这种跨 task 定向消息，全部在 baseline-prior。

### 7.3 自动筛出的待人工复核集合

- 词法检测到 93 条含负面/排除语义的 messages，合并相同正文后为 89 组。
- 其中 83 组没有找到同一 sender 后续词汇相似的负面 piece，形成 message-only review queue。
- 对 240 个 session 的 reasoning block 做高精度强负面短语扫描，82 个 actor-run 出现过“具体反例/某 claim、bound、route 为假或在某例失败”的候选；其中 49 个在该时间后没有任何负面 message 或 piece。

这些数字不是“83 条或 49 个已确认遗漏”：reasoning 中的想法可能很快被自己推翻，message 与 piece 也可能用完全不同措辞，或由其他 Agent 记录。它们的正确用途是建立 blinded manual-audit sample，而不是直接当 KPI。人工确认的三类例子已经足以证明结构化漏记确实存在。

### 7.4 记录和采用之间仍有证据空洞

本轮可以准确重建 exposure，却不能准确重建 adoption：

- message ack 只有 47 条，而且 ack 只说明“消费/清理”，不说明采用；
- piece 没有 ack 或 feedback；
- 在全部 CPS 文本中，只找到一处明确的跨作者 piece 前缀引用，即 `cde1b59d…` 对 `902889…` 的引用；这不能说明只有一次复用，只说明其余复用没有稳定 attribution；
- 后续 candidate 变好可能来自 startup piece、manual search、direct message、共享 candidate 文件或 Agent 自己推导，现有 schema 无法归因。

因此不能把“170 条 piece 被看到”表述成“170 条被采用”，也不能从没有显式引用推断没有使用。

## 8. 当前设计合理与不合理之处

| 维度 | 判断 | 依据 |
|---|---|---|
| Piece 内容价值 | 基本合理 | 有完整 strategy、可编译 lemma、具体 blocker 和 runner validation；86.29% Agent pieces 至少被一个 peer 看见 |
| Message 的短期用途 | 部分合理 | 高频询问和快速 handoff 符合短消息定位；运行中 sender 的信息确实可以很快被读取 |
| Message / piece 边界 | 不稳定 | 明确反例、false bound、长 Lean snippet 仍留在 message；message 正文总量是 piece 的 2.21 倍 |
| 首批方向协调 | 不合理 | 96/96 initial digest 空；无 barrier/claim；首批只有 4/96 在 first edit 前看到具体 route |
| Active 状态 | 缺失 | Roster 无 lifecycle；25.81% direct message 发给已经结束的 recipient |
| 送达语义 | 不可靠 | 43.25% direct message 未被指定 recipient 读到；3 条跨 task 静默不可达；broadcast ack 是全局的 |
| 知识收敛 | 不足 | 14/33 blocker 是在看过旧 blocker 后再发，但无 confirms/refines/supersedes |
| 检索排序 | 有实现缺陷 | 所谓 recency tie-breaker 使用 UUID 尾部，而不是 `created_at`；top-8 不稳定 |
| 使用归因 | 缺失 | 能测 exposure，不能测 adopted/refined/rejected；piece 没有 feedback 事件 |
| 与得分关系 | 当前不能判断 | 三轮均 `DEGRADED`、n=3；高通信题多为难题，通信量与成功率高度混杂 |

逐题分布也说明不能用“消息越多越好”解释结果：

| task | assignments | Agent pieces | messages | messages 未被 peer 看到 | proved runs / 3 |
|---|---:|---:|---:|---:|---:|
| `imo2024_p1` | 12 | 9 | 20 | 16 | 3 |
| `imo2024_p2` | 23 | 12 | 116 | 40 | 1 |
| `imo2024_p3` | 27 | 15 | 158 | 57 | 0 |
| `imo2024_p5` | 27 | 21 | 183 | 75 | 0 |
| `imo2024_p6` | 22 | 17 | 115 | 47 | 1 |
| `uk2024_r1_p1` | 19 | 19 | 84 | 48 | 3 |
| `uk2024_r1_p2` | 9 | 0 | 0 | 0 | 3 |
| `usa2024_p2` | 27 | 24 | 172 | 61 | 0 |
| `imo2023_p2_v2` | 23 | 16 | 118 | 41 | 0 |
| `imo2023_p3` | 23 | 40 | 172 | 79 | 0 |
| `imo2023_p4` | 6 | 8 | 3 | 1 | 3 |
| `imo2023_p5` | 22 | 16 | 107 | 45 | 0 |

`uk2024_r1_p2` 三次都很快证明，完全不需要 Agent communication；`imo2023_p4` 仅 3 条 message 也三次成功。相反，`imo2024_p5`、`imo2023_p3`、`usa2024_p2` 的消息和 pieces 很多但未证明。最合理的解释是题目难度和运行时长同时驱动通信量，而不是通信本身导致失败。

## 9. 建议的调整

### P0：先补“活跃方向”这一独立状态，不要塞进普通 message

新增 runner-owned `route_claim`，至少包含：

```text
claim_id, task_id, actor_id, route_key, short_summary,
status(active|blocked|released|done), created_at, expires_at,
related_piece_ids, independent_verification_reason
```

建议流程：Worker 读取题目并完成现有 mandatory Judge checkpoint 后，在第一次实质 edit 前必须先读取 active claims，再原子性创建或加入一个 claim。若 route_key 已被占用，系统给出软冲突提示，但允许 Agent 明确选择“独立验证同一路线”。Claim 使用 TTL/heartbeat，在 Agent 结束、阻塞或切换方向时自动释放，不能依赖 Agent 手工清理。

如果目标真的是“任何工作开始前”协调，而不仅是“第一次 edit 前”，需要二阶段启动：先让同一波 Agent 只产出短 route proposal，runner 汇总/去重后再进入 proof construction。单纯在现有 prompt 里增加一句“请先发消息”不够，因为首批并发启动和 polling race 仍存在。

### P0：让 roster 表示当前生命周期

`cps_actors` 应从 runner 的实时 assignment state 生成，返回 `admitted/running/closing/finished`、当前 claim、最后 heartbeat 和 finish time；默认 recipient discovery 只返回同题 active Agent。向跨题或已结束 recipient 发送时应显式拒绝或要求 `archive_only=true`，不能“写成功但永远不可达”。

### P0：修正 delivery/receipt

- 用 task channel/broadcast 替代同一正文多次 direct fan-out；
- receipt 表使用 `(message_id, actor_id, delivered_at, seen_at, acked_at)`；
- broadcast 的一个 ack 不应隐藏其他人的消息；
- 在下一次模型 turn 前注入轻量 unread/active-route delta，减少完全依赖 Agent 自觉 polling；
- 对 direct message 返回 recipient lifecycle 和可达性结果。

### P1：给可复用负面知识稳定 schema

Piece 至少增加以下语义字段，而不是只依赖自由正文：

```text
kind(strategy|lemma|counterexample|blocker|implementation_note|handoff)
status(proposed|tested|judge_checked|refuted|superseded)
claim
evidence_or_counterexample
preconditions_and_scope
next_action
route_claim_id
related_ids / refines / supersedes / confirms
```

具体的 promotion 规则可以很简单：message 中出现具体反例、某 lemma/bound 为假、某 Lean/API 路径确定失败、可编译 helper、或同一正文需要发送给多个 recipient 时，工具提示 sender 同步 `cps_publish`，或提供 `promote_message(message_id, kind, ...)`。不要完全自动晋升，因为临时猜测和已撤回结论会污染知识库。

### P1：修正检索与摘要预算

- 用真实 `created_at`，不要用 UUID 尾部模拟 recency；
- 排序优先考虑 task/route match、验证状态、是否被 supersede、证据强度、novelty 和真实时间；
- Startup digest 固定预留不同槽位，例如 2 个 active route claims、2 个最强 counterexample/blocker、4 个 verified/most-relevant pieces；不要让五条相近 blocker 挤掉其他方向；
- Message 不进入通用知识 search，但被 promotion 后的 piece 必须可检索，并保留 `source_message_id`。

### P1：补 exposure 到 adoption 的闭环

对 startup/search/inbox 暴露的 item 记录统一 exposure ID，并允许 Worker 在后续标记：

```text
used, refined, rejected, duplicate, stale, misleading,
not_used, route_attempted, route_improving
```

Candidate improvement、Judge verdict 和 route change 应能关联 exposure/claim。这样才能回答“哪类 piece 真正帮助了证明”，而不只是“数据库里有多少行”。

## 10. 建议的对比实验

### 10.1 先做核心 A/B，再决定是否做 2×2

核心问题应先只改变方向协调：

- A：当前 baseline，保留现有 message/piece 行为；
- B：增加实时 lifecycle roster + mandatory pre-edit route claim + active-route startup/delta；其他任务、模型、horizon、CPS 32、Judge、allocation 和 runtime limits 完全不变。

现有三轮可以作为历史描述性 baseline，但不应是新 treatment 的唯一 control：三轮均 `DEGRADED`，provider/runtime 时间漂移也可能影响结果。建议做同时期、顺序执行且随机化 AB/BA 顺序的至少 3 个 repeat/arm；仍保持每个 arm 12 题、1 小时、32 slots，不通过并跑多个 arm 改变外部容量合同。

如果 B 明显改善方向时序，再做 2×2 以拆分两个机制：

| arm | active route claim | negative message promotion |
|---|---|---|
| A | off | off |
| B | on | off |
| C | off | on |
| D | on | on |

### 10.2 预先注册的主要指标

方向协调指标：

1. Initial Agent 在第一次实质 edit 前看到至少一个 active route 的比例；当前参考值是 4/96 看到具体 route message。
2. 同题并发 Agent 的 route-key collision rate，以及冲突后明确选择 independent verification 的比例。
3. Route claim 从 assignment 到发布的延迟、从发布到 peer exposure 的延迟、claim stale/TTL rate。
4. 每个题目前 5 分钟的 distinct active routes 数，而不是 message 数。

知识质量指标：

1. 人工盲审确认的 reusable negative findings 中，结束前形成 structured piece 的比例。
2. `counterexample/blocker` 的 `refines/confirms/supersedes` 覆盖率和未合并重复率；当前 14/33 blocker 是作者看过旧 blocker 后再次发布。
3. Piece exposure → used/refined/rejected 的归因覆盖率；当前没有可靠基线。
4. 错误或误导 piece 的比例，防止为追求 capture rate 过度发布。

Delivery 与系统指标：

1. 发给已结束 recipient 的比例；当前为 302/1170。
2. Direct delivery/read rate；当前为 664/1170。
3. Fan-out 重复写入、broadcast per-recipient receipt 正确率。
4. CPS lock/query overhead、startup digest 字符数、truncation rate；当前 truncation 为 0/240，优化不应破坏这一点。

Outcome 指标：

1. score/12、normalized score-time AUC、time-to-first-proof 和 per-task proved rate；
2. Judge calls、token/cost、assignment 数和 timeout/recovery；
3. 对未解题，记录 verified progress，而不是只看最终 0/1。

### 10.3 判读原则

- 当前 n=3 且运行 degraded，不把小幅 score 波动当成显著结论；先看预注册的机制指标是否按预期变化。
- 去重不应变成禁止冗余。独立验证、备份实现和不同 Lean API 路线有价值，系统只要求显式说明为何重复。
- 不在同一 treatment 中同时改 allocator。Uniform policy 是否利用 piece 是另一个实验问题；本次先验证题内方向协调。
- 在正式一小时 arm 前，先用 mock/deterministic replay 验证 lifecycle、atomic claim、cross-task rejection、per-recipient ack 和 fail-open 行为；mock 只能验证协议，不能替代正式 workload。

## 11. 最终判断

需要优化，也值得做对比实验。当前最主要的问题不是“Agent 完全没有记录有价值知识”：大量 strategy、lemma、blocker 已经正确进入 pieces，而且多数在作者仍运行时被同伴看到。真正的缺口有三个：

1. **时序缺口**：首批并发 Agent 在选择方向前没有共同的 active-route view；
2. **生命周期缺口**：message 的 recipient 和 ack 语义不知道谁还活跃、谁已经读过；
3. **知识闭环缺口**：反例常停留在 message/reasoning，重复 blocker 没有关系，exposure 也没有 adoption 证据。

所以建议不要只调 prompt 让 Agent “多发消息”或“多写 piece”。应先把方向声明、活跃状态、负面知识 promotion 和使用归因变成明确协议，再用 contemporaneous A/B 验证它是否减少重复路线，同时不损害得分、成本和知识质量。

本次已检查可复用知识沉淀需求；本报告本身即为新增沉淀，无需修改其他现有文档。
