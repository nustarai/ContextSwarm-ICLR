# MathOlympiadBench 三轮原版实验的 message / piece 协作机制决策报告

日期：2026-09-05

状态：三轮原版实验的只读分析已完成；本报告未引入运行时 treatment，也未启动新实验。

## 背景和动机

在 12 道数学奥林匹克题的并行求解中，Agent 同时使用两种共享通道：

- `message`：短期、定向或广播的即时沟通；
- `piece`：应当能够被后续 Agent 检索和复用的结构化知识。

需要回答的不是“数据库里有多少条记录”，而是四个决策问题：

1. 这两种通道实际承载的内容是否符合各自的用途？
2. 同题的并发 Agent 在选择证明方向前，能否知道其他仍在运行的 Agent 正在做什么？
3. 反例、排除方向和失败结论是否停留在短消息或私有过程，没有进入可复用知识层？
4. 是否值得修改协议并做对比实验；如果值得，第一步应该改什么，而不是同时改动所有协作机制？

本报告区分四种状态：记录、送达、看到和采用。没有 piece 不等于没有产生知识；message 被写入不等于被 recipient 读到；被读到也不等于被采用或改善了最终证明。报告不会把这些状态混成一个“成功率”，也不会把私有 reasoning 中未出现的文字直接解释为永久丢失。

## 具体改了什么

本轮没有修改 ContextSwarm 运行代码，也没有把任何新协议放入三轮实验。分析对象是现有原版行为：

| 观察对象 | 原版行为 | 本轮是否改变 |
|---|---|---|
| startup context | 同题 piece search 加上当前 actor 可见的 message，限制在约 6000 字符 | 未改变；本轮只重建并核对实际暴露内容 |
| CPS 使用时机 | 首次 terminal `judge_check` 前，CPS 操作被 broker gate 拒绝 | 未改变 |
| Agent roster | 只有 `actor_id / task_id / episode` 的 append-only 列表，没有 active、finished 或 current route | 未改变 |
| route 协调 | 没有独立 route claim；方向只可能以自由文本 message/piece 出现 | 未改变 |
| message ack | `messages` 表使用单一全局 `acked_at`，不是逐接收者 receipt | 未改变 |
| allocator | uniform deterministic round-robin；不读取 CPS 方向或 evidence | 未改变 |

本轮实际增加的是分析层面的可见性重建：把 assignment、session、CPS 查询返回、startup prompt、第一次 `edit` 和结束时间对齐，区分首批/后续 assignment，并对“被 peer 看到”采用保守的逐项核对口径。

## 具体的实验

### 实验设置

| 项目 | 设置 |
|---|---|
| 实验问题 | 现有 message/piece 是否支持及时的方向协调和可复用知识传递 |
| 任务范围 | 同一批 12 道 MathOlympiadBench 题 |
| 运行时长 | 每次 3600 秒 |
| 重复次数 | 3 次原版 run |
| baseline / treatment | 没有 treatment；三次都是同一原版协作机制 |
| 模型与版本 | 三次使用相同的模型和冻结 source commit `33296b07634c708412326c2808d5782dab3f788e` |
| 共享设置 | CPS32、blackboard communication、uniform allocation |
| 真实性 | real workload，带 profiling；不是 mock、replay 或 canary |
| 健康状态 | 三次最终均为 `DEGRADED`，所以结果适合描述记录和机制，不适合作为通信因果性能结论 |

### 三次 run

| run | score | assignments | pieces（Agent + runner） | messages | final status |
|---|---:|---:|---:|---:|---|
| `20260901T012227Z-8c90d3f0` | 5/12 | 82 | 67（62 + 5） | 439 | `DEGRADED` |
| `20260902T075657Z-eda06caf` | 4/12 | 76 | 74（70 + 4） | 434 | `DEGRADED` |
| `20260902T090313Z-ecee9c07` | 5/12 | 82 | 70（65 + 5） | 375 | `DEGRADED` |
| 合计 | 14 个 proved task-runs | 240 | 211（197 + 14） | 1248 | 3 次均 `DEGRADED` |

### 口径和限制

- 240 个 session 中，239 个 startup prompt 与离线重建逐项一致；一个同秒 ack/排序边界样本按保守口径不计入曝光。
- 240 个 prompt 均没有触发 6000 字符截断。
- “看到”表示 item 出现在 startup prompt，或确实由 `cps_search`/`cps_inbox` 返回；不表示理解、采用或正确使用。
- “首次 edit 前”是实现前的宽松上界；Agent 可能在私有 reasoning 中更早决定方向。
- message 的“方向、负面、协调”等类别是高精度词法分类，允许重叠，不是完整语义标注。
- 共享 candidate 文件是另一条协作通道，不计入 message/piece 曝光统计。

## 结论

### 1. 机制层：当前系统不能可靠解决并发方向撞车

首批并发 Agent 启动时没有 active-route 视图。后续 Agent 偶尔能看到历史或正在产生的 piece/message，但这依赖自愿发送和主动 polling，不是可靠的并发协商。

因此，当前系统更适合“后续 Agent 接续已有成果”，不适合“同一波 Agent 在开始证明前声明互斥方向”。

### 2. 内容层：pieces 总体有价值，但反例和失败路线经常停在 message

`proof_strategy`、`lemma` 和 `blocker` 已经承载了真实的数学、Lean 和排除信息；这部分能力应保留。与此同时，明确的 message-only 反例已经存在，说明“没有进入 piece”不是纯粹的理论担忧。

### 3. 交付层：发送、送达、看到和采用没有闭环

当前 roster 不知道 actor 是否仍在运行，direct message 可以发给已结束或跨题的对象，broadcast 的全局 ack 不能表达每个 peer 的阅读状态。即使一条 piece 被看到，系统也不能证明它被采用或改善了 candidate。

### 4. 结果层：本轮不能宣称通信机制提升或降低了数学得分

三次 run 都是同一原版机制且都为 `DEGRADED`；5/12、4/12、5/12 只能作为现状背景。它们支持协议缺口的描述，不支持通信设计的因果性能结论。

### 5. 决策：应优化，但先做一个最小、可归因的实现切片

建议的实际顺序是：

1. 在 `cps.py`、`runner.py`、`judge_broker.py`、`pi_agent.py`、`pi_solver_tools.mjs` 和 `prompts.py` 中实现 runner-owned active roster 与原子 route claim；
2. 在 `cps.py` 和 broker 中修正逐接收者 delivery/seen/ack；
3. 再增加 counterexample/blocker promotion、piece relation 和 exposure→adoption 归因；
4. 实现完成并通过 focused/mock 回归后，先运行只包含 route coordination 的 contemporaneous A/B。

在第一轮 A/B 之前，不应同时改变 allocator、检索排序、负面知识 promotion 和 prompt 大结构，否则无法归因。

## 支撑结论的数据和分析

### 1. Pieces 的内容和传播

197 条 Agent-authored pieces 的类型如下；14 条 runner validation 单独计算：

| kind | 数量 | Agent pieces 占比 | 典型信息 |
|---|---:|---:|---|
| `proof_strategy` | 86 | 43.65% | 递推、上下界、数学化简、证明分解 |
| `lemma` | 42 | 21.32% | Lean helper、局部引理、API 用法 |
| `blocker` | 33 | 16.75% | 反例、语义核对、失败捷径、自动化阻塞 |
| `handoff` | 29 | 14.72% | candidate 状态、剩余障碍、接力建议 |
| `candidate` | 5 | 2.54% | 具体候选状态 |
| `example` | 1 | 0.51% | 示例 |
| `question` | 1 | 0.51% | 待解决问题 |

其他传播数据：

- 240 个 Agent 中 150 个发布过 Agent piece，90 个没有发布；20 个既没有 piece 也没有 message。
- 170/197 条 Agent pieces 被另一 Agent 看到；其中 167 条至少有一次是在作者仍运行时被看到。
- 27 条 Agent pieces 在该 run 内没有被 peer 通过 CPS 看到。
- 没有发现完全相同的 piece 重复行；但 33 条 blocker 中有 14 条是在作者已经看到同题旧 blocker 后再次发布，现有 schema 没有 `refines`、`confirms` 或 `supersedes` 关系。

这些数据支持“pieces 具有真实复用价值”，但不支持“被看到就被采用”。

### 2. Messages 的内容和传递

| 指标 | 数值 | 解释 |
|---|---:|---|
| message rows | 1248 | 是 Agent piece 的 6.34 倍 |
| direct / broadcast | 1170 / 78 | 93.75% 是定向消息 |
| 正文总字符 | 186,981 | 是 Agent piece 正文 84,640 字符的 2.21 倍 |
| 询问/协调类 | 943 | 词法分类，75.56%，与其他类别重叠 |
| Lean/API/代码特征 | 528 | 词法分类，42.31% |
| 负面/排除特征 | 93 | 词法分类，7.45% |
| 可识别当前方向声明 | 119 rows / 101 组 | 不是正式 schema |
| 可识别具体 route 声明 | 76 rows / 69 组 | 约占全部 message 的 6% |

传递和生命周期：

| 指标 | 数值 | 影响 |
|---|---:|---|
| 被任一 peer 看到 | 738/1248 | 至少 510 条没有形成 CPS peer exposure |
| direct 被指定 recipient 看到 | 664/1170 | 506 条没有被指定对象读到 |
| 发出时 recipient 已结束 | 302/1170 | 静态 roster 诱导 stale send |
| 跨 task、必然不可达 | 3 | 当前 inbox 按 task 过滤而静默丢失 |
| 完全相同正文的 fan-out 额外行 | 115 | Agent 用多次 direct send 模拟 broadcast |
| acked rows | 47 | 不足以代表 adoption |

因此 message 通道既承载了太多应进入 piece 的技术内容，也没有可靠的 recipient 生命周期和逐人 receipt。

### 3. 方向可见性和时序

每轮 32 个首批 assignment 中，有 8 个题组同时启动 3 个 Agent、4 个题组同时启动 2 个 Agent；三轮合计 96 个首批 Agent。

| 时点 | 首批 Agent | Adaptive Agent | 全部 Agent |
|---|---:|---:|---:|
| startup digest 非空 | 0/96 | 135/144 | 135/240 |
| 首次 edit 前看到任一 peer context | 19/96 | 138/144 | 157/240 |
| 首次 edit 前看到具体 route message | 4/96 | 28/144 | 32/240 |

69 组不重复具体 route 声明中：

- 22 组在 sender 首次 edit 前发送；
- 53 组最终被至少一个 peer 看到；
- 17 组在至少一个 peer 首次 edit 前被看到；
- 从 assignment 到发送的中位延迟约 282 秒，p90 约 1106 秒。

这解释了为什么当前机制可以产生 live reuse，却不能保证最初的方向避重。

一个时序失败案例是 `imo2023_p4`：Agent A 在自己的首次 edit 前发送了 prefix-recurrence/Finset 方向，但 Agent B 已经先 edit，几分钟后才读到该消息。一个成功案例是 `imo2024_p5`：piece `90288972…` 被仍在运行的 peer 搜索到，随后 message `cde1b59d…` 明确提到使用了该 helper。两者共同说明问题是协议保证缺失，而不是传播能力完全不存在。

### 4. 调度器与 CPS 的边界

144 次 adaptive decision 全部是 uniform round-robin：

- `features` 非空次数为 0；
- `evidence_piece_ids` 次数为 0；
- 144/144 次被选题目当时都有 active Agent。

调度器知道题目级 active count，但没有读取题内 route，也没有用 piece/message 决定子方向。因此不应把“allocator 知道某题有几个 Agent”解释成“Agent 之间已经完成方向协调”。

### 5. 已确认的 message-only 负面知识

以下不是“完全没有记录”，而是“已经记录在 message，却没有进入可检索的结构化知识层”：

| message | 结论 | 为什么值得 promotion |
|---|---|---|
| `9a93bb88…`、`2b6a6f55…`（`imo2024_p2`） | 用 `a=3,b=6` 等具体值反驳 naive gcd/normalization 等价式 | 是可复用反例，可排除一整类证明方向；sender 没有发布 piece |
| `1896639b…`（`usa2024_p2`） | 具体计算表明候选 `h_U` 下界为假 | 后续正向策略没有保留被否定的 bound |
| `948a91e0…`（`uk2024_r1_p1`） | direct `rfl/decide` 路线失败或资源表现不适用 | 后续 recurrence piece 没有包含该 API/计算排除信息 |

自动扫描还得到 93 条负面 message（89 组正文），其中 83 组没有找到同一 sender 后续词汇相似的负面 piece。这 83 组是待回归和质量评估样本，不是 83 条已经证实的遗漏；私有 reasoning 中没有后续 CPS 输出的情况同样不能直接解释为永久丢失。

### 6. 实际实现任务和第一轮对比实验

#### 6.1 代码实现顺序

第一切片是 active roster + route claim：

- `cps.py`：增加 `actors`、`route_claims`，提供注册、heartbeat、结束、TTL、原子 claim/release；
- `runner.py`：在真实 admission/finish 边界更新状态，不再预注册未来 actor；
- `judge_broker.py`：增加 active-route 查询和 claim/update/release capability，拒绝 stale/cross-task recipient；
- `pi_agent.py`、`pi_solver_tools.mjs`：把 treatment flag 绑定到 session，并在 `write/edit` 前执行 claim gate；
- `prompts.py`：明确“读题目 → 查 active route → claim/独立验证 → early Judge → 其余 CPS/编辑”的顺序。

其中 `cps_active_routes` 和 `cps_claim_route` 是唯一可以在首次 Judge 前调用的 CPS 操作；历史 pieces/messages 的 search/inbox/send/publish 仍保留原有 Judge gate。这样能在不开放全部通信权限的情况下解决最初方向竞争。

第二切片是 message receipts：增加 `(message_id, actor_id, delivered_at, seen_at, acked_at)`，修正 broadcast 全局 ack，并在发送前校验 recipient 生命周期。

第三切片才是 negative promotion、piece relations 和 exposure→adoption 归因。它们可以保留原 message，不应阻塞第一轮 route-coordination 实验。

#### 6.2 第一轮正式 A/B

| arm | 唯一变化 |
|---|---|
| A | 当前原版 baseline，所有新 feature 关闭 |
| B | active roster + pre-Judge route claim + pre-edit gate + active-route delta |

两臂保持 12 题、3600 秒、CPS32、模型、Judge、uniform allocation 和 runtime limits 相同；建议至少 3 repeats/arm、AB/BA 顺序随机化、串行运行。第一轮不打开 negative promotion、adoption telemetry 或 allocator treatment。

第一轮应优先判读机制指标：首批首次 edit 前 route 可见率、route collision、独立验证率、每题前 5 分钟 distinct routes、claim 延迟和 stale rate；score、AUC、Judge、token/cost、timeout/recovery 作为结果与成本护栏。

在 route claim 的机制指标确实改变之前，不应声称默认开启、性能提升或减少了数学重复劳动。mock 和 focused tests 是实现验收，不是正式实验结果；正式结果必须来自新的 real workload arm。

完整的实现任务卡见 [`matholympiad_message_piece_action_plan_20260903.md`](matholympiad_message_piece_action_plan_20260903.md)。

原始机器可读摘要和只读分析脚本未随 PR 提供；关键 aggregate 已在本报告列出。详细案例审计见 [`matholympiad_message_piece_audit_20260903.md`](matholympiad_message_piece_audit_20260903.md)。
