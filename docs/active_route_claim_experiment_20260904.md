# Active roster + route claim 第一阶段实现与实验记录

日期：2026-09-04

> 更新说明（2026-09-05）：本文保留第一阶段实现、早期共享 Judge 尝试和顺序隔离运行的历史记录。最新的“弱提示词 vs 强提示词”真实 A/B、按 prompt-exposed 分母计算的语义重复指标，以及最终决策见 [`activity_feedback_final_conclusion_20260905.md`](activity_feedback_final_conclusion_20260905.md)。不要把本文早期的“尚未证明语义重复下降”段落当作最新实验结论。

状态：第一阶段协议已实现并通过离线机制验证；同源 control/treatment 正式配置已冻结。此前曾误用共享 Judge 做过三次并行 treatment（历史记录保留在第 7 节，不能用于成绩比较）；按用户纠正后的协议，已于 2026-09-03 依次完成三次真实、严格顺序、每轮独立 Judge stack/workspace 的 12 题 × 1 小时 treatment，详见第 13 节。三次新 run 均 `runner rc=0`、最终记录为 `6/12` 且运行健康状态为 `DEGRADED`；它们是有效的隔离机制/基础设施观测，但仍不是 control/treatment 因果 A/B。

实现基线（历史报告中的实现锚点）：`33296b07634c708412326c2808d5782dab3f788e`

此前并行尝试快照：`8d8c864e4ce718e6266e0e0d849e04b06647cf36`（Docker image `sha256:5ab20c1a2131942408a9da5b7c5c6d53b8031bb3a2f6ebfe250f36675cea74fa`）。本次顺序隔离三轮的精确 source/image 为 `943b0eecebcfd59a648c6d58d99efa12c4ae2290` / `sha256:4451e6aac33fc1fe5dfa822a00afc3ec618f20c9a6e149f21ed165785e2b0e6b`。

## 1. 结论

结论先说：此前三次同时跑的样本确实不能回答“得分是否提高”，而且不符合隔离实验要求；本次已经按纠正后的要求重新顺序跑完三次，每轮使用独立 Judge stack、CPS/worker workspace 和端口，下一轮只在上一轮 closeout/teardown 完成后启动。新三轮都记录为 `6/12`、`DEGRADED`，因此它们证明了隔离运行协议和 route claim 生命周期可以真实闭环，但仍不能单独回答 route claim 是否带来数学成绩提升。

这套方案作为第一阶段在协议层可行，但当前证据仍不足以支持“数学得分提高”或“语义重复探索已经下降”的结论。

已经成立的部分是：

1. Runner 只在真实 admission 时登记 Agent；未 admission 的未来 Agent 不再提前出现在 roster 中。正常结束、取消、超时、recovery exhausted 和 solved-by-peer 等收尾路径都会尝试结束 actor，并在同一 CPS 事务中释放其活跃 route claim。
2. 同一题、同一 `route_key` 的 primary claim 由 SQLite 原子约束保证最多一个；冲突者会看到已有 owner，可换方向，也可以附带明确的 `independent_verification_reason` 取得非 primary 的独立验证 claim。
3. `cps_active_routes` 和 `cps_claim_route` 是仅有的首次 terminal Judge checkpoint 前 CPS 能力；原有 search、inbox、send、publish 以及 route update/release 仍受 Judge gate 约束。
4. Treatment 下，Pi 的 write/edit 在拿到有效 route lease 前被阻止；只有 broker 明确发出的 fail-open 结果才能绕过，并且必须携带规范化的 `route_claim_bypass_reason`，不能伪装成成功 claim。
5. 三方同时竞争同一 route 的离线协议 smoke 得到 1 个 primary 和 2 个 conflict；独立验证、finish、TTL expiry、blocked、显式 release、admission-only roster 和 pre-Judge gate 均通过自检。
6. 修复后的最终 clean 快照又运行了一对同源 3 题 mock control/treatment：两臂均完成 6/6 assignment，`health.ok=true`，profiling 审计通过且 `dropped_fields.total=0`。但 mock Agent 不调用 route tools，也不产生数学结果；它只能验证 runner lifecycle、配置身份和产物闭环。
7. 此前并行尝试共登记 234 个 runner-owned actor、持久化 272 个 primary claim；这些数字只属于历史共享 Judge 样本。用户纠正后的三轮顺序隔离样本另有 265 个 actor、319 个 claim，详见第 13 节，不能混合合计。
8. 新顺序样本没有出现 `edit/write` 早于 claim，也没有 fail-open bypass 或历史/active primary duplicate group；但 run2 仍出现 2 个 episode 先做 early Judge、1 个先做受 gate 约束的 CPS 操作，说明“route-first”目前主要依赖 Agent 遵循 prompt，尚未成为不可绕过的 broker barrier。新样本的 peer-visible 比例不作为本轮主结论，因为其脱敏汇总没有保留语义 route 内容，且仍不能替代 blinded semantic audit。

尚未成立的部分是：

- 此前并行样本的 Judge closeout 未完成，只有 horizon 内 provisional proof、route/生命周期和 profiling 观测；新顺序样本则都有 `drained=true` 的 closeout receipt，但仍因 health `ok=false`、profiling audit 不完整和 run3 event/final 计数差异，不能升级成无争议的 benchmark authoritative score。
- 新顺序样本的每一轮 preflight 都满足 Judge endpoint/health、受控 NuRouter runtime 和 operator launch contract；declaration-index 仍未配置。与旧并行样本不同，新三轮各自使用独立的 32-worker Judge stack，没有共享 queue。
- 历史三轮都来自旧 source 且均为 `DEGRADED`。把历史 5/4/5 分直接与新实现的一次 treatment 比较，会同时混入 source、运行顺序、基础设施和故障状态差异，不构成因果对比。

因此当前决策应是：保留第一阶段实现，但先把 Judge 隔离/容量和不可绕过的 claim barrier 修好，再在同一精确 source/image 上做成对、重复的 control/treatment 正式实验；本轮不声称方案改善了最终成绩。

## 2. 问题、假设与证据层级

本实验要验证的假设是：如果 Agent 在第一次实质性修改候选文件前，必须先看到同题活跃方向并原子声明自己的 route，那么无意识的并发重复探索会下降；节省的 slot/token 时间可能进一步改善证明覆盖率或 time-to-proof。

需要把四层证据分开：

| 层级 | 本轮状态 | 能回答的问题 | 不能回答的问题 |
|---|---|---|---|
| 历史观测 | 已完成 | 原版系统是否缺少早期方向协调、重复风险有多大 | 新协议是否带来因果改善 |
| 单元/协议机制 | 已完成 | 原子竞争、身份绑定、gate、TTL、release、fail-open 是否按合同工作 | Agent 是否会生成稳定且语义正确的 route key |
| 同源 mock 编排 | 已完成 | treatment 是否能完成 admission/finish、配置/产物/profiling 是否闭环 | 数学效果、真实调用开销、冲突后行为 |
| 真实 treatment（此前 3-way parallel，历史无效样本） | 已执行但三次均 `DEGRADED` | 共享 Judge 下的失败/容量观测 | 隔离后的成绩、无混杂的 control/treatment 因果效果 |
| 真实 treatment（本次 3-way sequential isolated） | 已执行，三次均 `runner rc=0`、最终 `DEGRADED` | 独立 stack 下的真实 route/lifecycle、closeout、provisional/final run record | route 对数学成绩的因果效果；语义重复率 |

这个边界很重要：机制通过是启动真实实验的前提，不是实验效果本身。

## 3. 历史原版三轮基线

下表来自同一冻结合同下的三次 MathOlympiadBench 运行；详细 message/piece 内容与时序审计在另一个只读分析产物中，本报告只保留与 route claim 假设直接相关的分母。

| 标签 | run id | score | assignments | pieces（Agent + runner） | messages | final status |
|---|---|---:|---:|---:|---:|---|
| baseline-prior | `20260901T012227Z-8c90d3f0` | 5/12 | 82 | 67（62 + 5） | 439 | `DEGRADED` |
| baseline-r1 | `20260902T075657Z-eda06caf` | 4/12 | 76 | 74（70 + 4） | 434 | `DEGRADED` |
| baseline-r2 | `20260902T090313Z-ecee9c07` | 5/12 | 82 | 70（65 + 5） | 375 | `DEGRADED` |
| 合计 | 3 runs | 14 个 proved task-runs | 240 | 211（197 + 14） | 1248 | 3 次均 `DEGRADED` |

合同共同点：12 道相同任务、3600 秒 horizon、CPS32、blackboard communication、uniform allocation、相同模型和 source commit `33296b07634c708412326c2808d5782dab3f788e`。

还要明确 runtime 差异：历史三轮的 NuRouter 0.2.2 binary SHA-256 为 `3fd4c9fc04e35c668c9d1bf0bfa3ac6460d210ab708c3b1fac1558ee5d79851b`、image 为 `sha256:e4f79e6c525fa0e1921e5ab925192ec5203ec3e26ab50aced8924a55eb550bb5`；本次 treatment 使用的是第 9 节记录的另一 NuRouter binary/image。也就是说，即使两边都标成 0.2.2，runtime 资产并非同一份，历史 5/4/5 只能作为非 contemporaneous 背景。

与“选方向前去重”最直接相关的历史结果：

| 指标 | 结果 | 含义 |
|---|---:|---|
| 首批 Agent startup digest | 96/96 为空 | 同一波并发 Agent 启动时没有其他人的方向信息 |
| 第一次 edit 前看到具体 route message | 32/240 | 即使采用宽松的 first-edit 上界，覆盖仍很低 |
| 其中首批 Agent | 4/96 | 核心首批碰撞窗口几乎没有协调 |
| 可识别的不重复具体 route 声明 | 69 组 | route 只存在于自然语言，不是协议状态 |
| 声明者 first edit 前发出 | 22/69 | 大部分声明本身已经偏晚 |
| 至少一个接收者 first edit 前读到 | 17/69 | 即使发送了，也不保证在 peer 选路前送达 |
| 定向消息发出时收件人已结束 | 302/1170 | append-only roster 会把 stale actor 当成可协作对象 |
| 定向消息被指定收件人实际读到 | 664/1170 | “发送成功”不等于形成信息传递 |

这些数据证明原系统存在结构性缺口，但不直接量化“有多少次语义重复”。`first edit` 也只是宽松上界，因为 Agent 可能更早在私有 reasoning 中确定方向。

## 4. 第一阶段协议与代码实现

### 4.1 Agent 实际时序

Treatment 的目标时序是：

```text
读取公开题目和骨架
→ cps_active_routes
→ cps_claim_route
→ 若冲突，换 route 或说明独立验证理由
→ early judge_check
→ search / inbox / send / publish
→ 修改 candidate
→ update / release route
```

本阶段协调点位于“读题之后、write/edit 之前”。它不能严格保证在 Agent 产生任何私有 reasoning 之前协调；要达到那个更强语义，需要 proposal barrier，由 runner 先收集所有提案再统一 admission，属于第二阶段设计。

### 4.2 模块责任

| 模块 | 主要变化 |
|---|---|
| [`contextswarm_mini/config.py`](../contextswarm_mini/config.py) | 增加严格类型的 `cps_features`；`route_claim_required`、TTL 和实验身份进入 canonical public config；required 只允许 CPS + 非 `none` communication |
| [`contextswarm_mini/cps.py`](../contextswarm_mini/cps.py) | SQLite 增加 runner-owned `actors`、`route_claims`；实现 register/heartbeat/finish、active roster、claim/update/release、TTL expiry 和原子 primary uniqueness |
| [`contextswarm_mini/runner.py`](../contextswarm_mini/runner.py) | admission 时登记、所有主要 terminal 路径收尾、动态生成 `actors.json` 投影、向 Pi 注入 route contract，并记录显式 bypass |
| [`contextswarm_mini/judge_broker.py`](../contextswarm_mini/judge_broker.py) | pre-Judge allowlist、actor/task/episode/claim 绑定、route API、语义 negative 与 transport outage 分离、broker-issued fail-open |
| [`contextswarm_mini/pi_agent.py`](../contextswarm_mini/pi_agent.py) | 把受控 route capability、episode、TTL 与 system prompt 注入 Pi session |
| [`contextswarm_mini/pi_solver_tools.mjs`](../contextswarm_mini/pi_solver_tools.mjs) | 注册四个 route tools；treatment-only write/edit gate；只接受绑定完整的 lease 或明确 bypass；清除 stale/blocked lease |
| [`contextswarm_mini/prompts.py`](../contextswarm_mini/prompts.py) | 明确 route-first 操作顺序、冲突处理和 fail-open 解释 |
| [`contextswarm_mini/profiling.py`](../contextswarm_mini/profiling.py) | `run.start` 与 `run.configuration` 直接记录三个 treatment identity 布尔字段，避免分析时只能跨文件推断实验臂 |

### 4.3 持久状态和身份合同

`route_claims` 至少持久化：

```text
claim_id, task_id, actor_id, episode, route_key, summary, status,
created_at, updated_at, expires_at, released_at,
independent_verification_reason
```

另外保留 `is_primary` 与 bounded `release_reason`，用于区分唯一 primary、独立验证 secondary 和收尾原因。

关键约束：

- primary uniqueness 的键是 `(task_id, route_key)`；`active`/`blocked` primary 受 SQLite partial unique index 保护。
- 只有 runner 已 admission 且 episode 精确匹配的 live actor 能 claim。
- actor 重注册到新 episode 时，旧 episode 的活跃 claim 在同一事务中释放；terminal actor 不能被同 episode 静默复活。
- `blocked` 对 peer 仍是可见占位，但不是可写 lease：更新返回 `ok=true`，同时 `acquired=false`、`claimed=false`。
- actor finish 与其活跃 claim release 在同一写事务中完成；TTL 查询也会清理过期占位。
- broker-facing update/release 必须精确绑定当前 task、actor、episode 和 claim id；底层 CPS 的无 actor 参数形式只属于受信任的 runner/direct-store 边界。

### 4.4 Gate 与故障语义

| 情况 | 行为 |
|---|---|
| 首次 Judge 前调用 `cps_active_routes` / `cps_claim_route` | 允许 |
| 首次 Judge 前调用 search/inbox/send/publish/update/release | `JUDGE_CHECK_REQUIRED` |
| Treatment 下没有有效 claim 就 write/edit | Pi extension 阻止 |
| 同 route 已有 primary，未说明独立验证 | conflict，返回 bounded owner/claim 信息，不开 write gate |
| 提供独立验证理由且响应绑定完整 | 获得 non-primary claim，可继续 |
| malformed/unknown adapter 响应或 route 依赖故障 | broker 显式 fail-open，并记录规范化 `route_claim_bypass_reason` |
| 普通 input/identity/ownership/terminal negative | fail-closed；不能伪装成 outage bypass |

Fail-open 是可用性选择，不是去重保证：route 服务故障期间 Agent 可以继续工作，因此可能重新出现重复探索；区别在于这次会留下明确可统计的 bypass 证据。

## 5. 离线协议 smoke

自检脚本是 [`scripts/route_claim_protocol_smoke.py`](../scripts/route_claim_protocol_smoke.py)。它每次创建全新的 SQLite 数据库和 loopback broker，使用 synthetic actor，不连接 Pi、NuRouter、Judge 或外网；成功后删除临时工作目录，只保留 bounded JSON 汇总。

精确自检快照：`171ddda14e0495cba55a94ec0c2458741bd4d4d2`。

| 验收项 | 观测 | 结果 |
|---|---|---|
| admission-only roster | 首个 admission 后只看到 `actor-a`；未来 `actor-b/c` 不可见 | 通过 |
| 三方原子竞争 | 3 attempts → 1 primary + 2 conflicts；结果总数守恒 | 通过 |
| 独立验证 | reason 被持久化；acquired=true；is_primary=false | 通过 |
| primary finish | primary 结束后只剩 1 个 independent route | 通过 |
| 全部 finish | active routes 归零 | 通过 |
| TTL | 过期 claim 从 active view 消失；新 actor 重新取得 primary | 通过 |
| blocked | peer view 仍可见；update accepted；acquired/claimed 均为 false | 通过 |
| explicit release | 状态为 `released`，active view 中不存在 | 通过 |
| pre-Judge gate | active-routes 成功；search 返回 `JUDGE_CHECK_REQUIRED` | 通过 |
| profiling envelope | 7 类 actor/route transaction 均有稳定 operation label，non-ok commit 为 0 | 通过 |

该次自检的 transaction 汇总如下；这是一次微型 synthetic 描述值，不是 CPS32 性能基准：

| operation | count | wall total (s) | lock wait total (s) | lock hold total (s) | rows written |
|---|---:|---:|---:|---:|---:|
| `actor.register` | 8 | 0.011292 | 0.000080 | 0.007231 | 16 |
| `actor.list_active` | 2 | 0.001754 | 0.000040 | 0.000414 | 0 |
| `actor.finish` | 3 | 0.004468 | 0.000033 | 0.003041 | 10 |
| `route.claim` | 7 | 0.017697 | 0.004286 | 0.006885 | 12 |
| `route.list_active` | 6 | 0.004311 | 0.000070 | 0.001427 | 2 |
| `route.update` | 1 | 0.001438 | 0.000010 | 0.000964 | 2 |
| `route.release` | 1 | 0.001467 | 0.000011 | 0.000998 | 2 |

多个并发事务的 wall totals 可以重叠，不能相加后当作整个 run 的额外时长。

## 6. 同源 control/treatment mock 对比

两份 mock 配置除了实验名和 `route_claim_required` 外保持一致：

- Control：[`configs/route_claim_smoke_control.toml`](../configs/route_claim_smoke_control.toml)
- Treatment：[`configs/route_claim_smoke.toml`](../configs/route_claim_smoke.toml)

共同合同：3 道题、`max_parallel=3`、每题 2 episodes、3 秒 horizon、profiling 开启。下面这对命令在最终 clean worktree HEAD `96a0eb5903143f6f9175ef475e53cd166a51130f`（代码修复父提交 `171ddda14e0495cba55a94ec0c2458741bd4d4d2`）上执行；单元测试逐字段证明 canonical public config 只在 name 和 route flag 上不同。

需要单列 provenance 限制：这两个 test-only mock artifact 的 `run_meta.runtime_provenance` 按设计只记录 `test-only-mock-source` / `test-only-mock-image`，`source_git_commit` 为空。因此 `96a0eb`/`171ddda` 是本次 operator 命令和 clean worktree 的执行证据，不是 artifact 自身绑定的 Git SHA。正式 Docker A/B 必须把精确 source commit 与 image ID/digest 写入 artifact，不能沿用这组 mock provenance 做正式归因。

| 指标 | Control | Treatment |
|---|---:|---:|
| run id | `20260903T164057Z-9173033c` | `20260903T164058Z-5edbe0e9` |
| final status | `COMPLETED` | `COMPLETED` |
| health | `ok=true` | `ok=true` |
| assigned / attempted / finished | 6 / 6 / 6 | 6 / 6 / 6 |
| verdict | 6 × `MOCK_SKIPPED` | 6 × `MOCK_SKIPPED` |
| profiling rows | 266 | 272 |
| profiling dropped fields | 0 | 0 |
| profiler elapsed | 0.319033s | 0.374645s |
| actor rows at closeout | 0 | 6，全部 `finished` |
| route claim rows | 0 | 0（mock Agent 不调用 route tools） |
| CPS lifecycle events | 0 | 12（6 register + 6 finish） |

本次样本 treatment 比 control 多 0.055612 秒，约 17.43%。这只是一次亚秒、顺序执行、非随机化 mock；前一对同类样本甚至只有约 0.27% 差异，说明这种尺度主要受调度/文件系统噪声支配。不能据此声称真实开销是 17.43%，也不能声称无开销。原始 run 与审计报告分别保存在 `runs-paired-96a0eb5/`、`evidence/paired_control_audit_96a0eb5.json` 和 `evidence/paired_treatment_audit_96a0eb5.json`。

Treatment 中 6 次 `actor.register` 的 transaction wall total 为 0.013157s，6 次 `actor.finish` 为 0.050704s；合计 0.063861s。由于多个 Agent 并发，这些 transaction wall totals 彼此可能重叠，也不能与两臂 run elapsed 直接相减做因果归因。

更关键的限制是：mock solver 不加载 Pi extension，不会执行 active-routes、claim、conflict/reroute 或 write gate。因此这组数据验证的是 lifecycle/profiling plumbing，而不是 route 去重效果。

## 7. 正式一小时 A/B 配置与此前并行尝试（历史）

> 本节 7.1–7.4 保留的是用户纠正前的“三次同时启动、共享一个 Judge”尝试。它们不是本次要求的顺序隔离样本；尤其不要把本节的 `5/2/3`、234 actors、272 claims 或共享队列数字与第 13 节的新三轮数据相加或作成绩 A/B。

- Control：[`configs/formal_1h_cps32_profiled_route_control.toml`](../configs/formal_1h_cps32_profiled_route_control.toml)
- Treatment：[`configs/formal_1h_cps32_profiled_route_claim.toml`](../configs/formal_1h_cps32_profiled_route_claim.toml)

两者都继承历史 `formal_1h_cps32_profiled_clean.toml`。`validate` 与 `plan` 均通过；canonical public config 的差异被测试锁定为实验名和 `route_claim_required`，其余核心合同一致：

| 字段 | 两臂共同值 |
|---|---|
| tasks | 冻结的 MathOlympiadBench latest12 |
| horizon | 3600 秒 |
| mode / communication | `cps` / `blackboard` |
| max parallel / NuRouter in-flight / Judge concurrency | 32 / 32 / 32 |
| initial agents / episodes | 每题 2 / 2 |
| allocation | `uniform`，agent timeout 900 秒 |
| model / thinking | `openai-codex/gpt-5.6-sol` / `max` |
| route TTL | 3600 秒 |
| profiling | 继承同一 profiled contract |

Treatment 的三个身份字段会同时写入 `run_meta.json`、ordinary `run_started`、profiling `run.start` 和 `run.configuration`；因此单独拿到 profiling stream 也能辨认实验臂，不需要根据是否碰巧出现 route write 反推。

### 7.1 三次并行真实运行结果（历史、无效作答样本）

三次命令在同一秒启动，使用同一 source/image/config 和同一个隔离 Judge backend。下表的 `horizon proofs` 是 horizon 内 `judge_proof_credited` 事件数，只是 provisional 观测；它不是 closeout 后的 authoritative score。

| run | run id | horizon proofs | assignments / actor rows | claim rows | messages / pieces | scoreboard（PROVED / AGENT_FAILURE / CANCELLED / TIME_LIMIT） | 结束方式 |
|---|---|---:|---:|---:|---:|---|---|
| treatment-1 | `20260903T173005Z-a8c1e6e5` | 5/12 | 85 / 85 | 101 | 414 / 66 | 5 / 35 / 13 / 32 | `rc=143`；horizon 后 closeout 长时间等待，操作员有界停止 |
| treatment-2 | `20260903T173005Z-fbb1688d` | 2/12 | 75 / 75 | 86 | 364 / 68 | 2 / 36 / 5 / 32 | `rc=143`；horizon 后 closeout 长时间等待，操作员有界停止 |
| treatment-3 | `20260903T173005Z-c9906c53` | 3/12 | 74 / 74 | 85 | 443 / 70 | 3 / 33 / 6 / 32 | `rc=2`；`remote_settlement_unconfirmed` 后 `run_error` |

三次 horizon 都正常走到约 3600 秒；run1/run2 的 `143` 是外部停止 closeout 卡住的进程，不是 solver 语义失败码，run3 则由 Judge broker closeout drain deadline 自然报错。三次合计为 10 个 horizon-credit proof、234 个 admission、272 个持久 claim、1221 条 message 和 204 个 piece。由于三次共享一个 32-worker Judge，不能把 5/2/3 与历史 5/4/5 当成同条件分数差异。

若只做 descriptive scoreboard（不是 A/B 结论）：历史三轮为 5+4+5=14/36，本次三次 horizon-credit 为 5+2+3=10/36，表面差异为 −4 个 proof。这个差异同时受到新旧 source/image、NuRouter binary、三 run 共享 Judge 队列、closeout 不完整和 declaration-index 缺失影响；因此不能解释为 route claim 使成绩下降，也不能解释为它没有收益。

### 7.2 真实 route 协议观测（历史并行样本）

| 指标 | treatment-1 | treatment-2 | treatment-3 |
|---|---:|---:|---:|
| 首批 32 个 initial actor 的第一次 active-routes 查询看到同题 peer | 10/32 | 5/32 | 7/32 |
| 首次 edit 前任何一次 active-routes 查询看到同题 peer（首批） | 12/32 | 6/32 | 11/32 |
| 观测到 edit/write 早于 claim 的 actor | 0 | 0 | 0 |
| 观测到 first Judge 早于 claim 的 actor | 0 | 2 | 0 |
| 观测到受 Judge gate 约束的 CPS 操作早于 claim 的 actor | 0 | 1 | 0 |
| `route_claim_conflict` 事件 | 0 | 0 | 1 |
| 持久 claim（全部 `primary=1`） | 101 | 86 | 85 |
| claim 中带 independent-verification reason | 79 | 61 | 62 |
| 最大同时 active claim 行数 | 39 | 38 | 38 |
| 同一 actor 的最大同时 active claim 行数 | 2 | 2 | 2 |
| fail-open `route_claim_bypass_reason` | 0 | 0 | 0 |

三个 run 的 claim 最终状态都是全量 `released`（101/101、86/86、85/85），active/unreleased 残留均为 0；三份 CPS SQLite 的 `integrity_check` 也均为 `ok`。run3 的 1 次 conflict 没有 independent-verification reason，也没有形成持久 secondary row；因此“primary uniqueness”确实在真实并发中被触发，但这不是 1 次 secondary 独立验证成功。另一个需要保留的信号是 recovery 期间同一 actor 最多同时持有 2 行 claim：run1/2/3 分别有 16/12/12 个 actor 出现多行历史 claim（额外行 16/12/12）。这提示后续应增加 single-active-lease 或明确 supersede 语义，而不能只依赖 actor 重启流程。

整体看，首批第一次查询的 peer 可见率为 22/96（22.9%），把第一次 edit 前的重复查询纳入后为 29/96（30.2%）。这比历史“首批 first-edit 前读到具体 route message”4/96 的观测更有协调覆盖，但两者不是同一分母/同一信息载体，不能直接换算成语义重复率。真实 route key 的精确重复只反映协议层字符串冲突，不能替代 blinded semantic audit。表中“restricted CPS”只表示 search/inbox/send/publish 这一组原本受 Judge gate 管理的工具；run2 的那 1 个例子是在 early Judge 已完成后 search 才被正常允许，暴露的是 route-first prompt 不可强制，而不是把 Judge gate 误判为失效。

### 7.3 CPS route 事务与 token 诊断（历史并行样本）

`cps.write.commit` 的 route 事务没有 non-ok commit；各 run 的 `route.claim` 汇总如下。wall total 会因并发重叠，不能相加当作额外运行时。

| run | claim commit 次数 | wall total (s) | lock wait total (s) | 最大单次 lock wait (ms) |
|---|---:|---:|---:|---:|
| treatment-1 | 135 | 0.303193 | 0.008804 | 1.306 |
| treatment-2 | 117 | 0.264034 | 0.010692 | 3.732 |
| treatment-3 | 119 | 0.268799 | 0.006647 | 1.293 |

Profiling 还记录了 model request/token 汇总（仅供成本诊断，不含 prompt/response）：run1/run2/run3 的 request 数为 5294/5231/5584，input token 为 11.27M/11.24M/11.55M，output token 为 3.44M/3.27M/3.38M，cache-read token 为 170.80M/177.68M/195.63M。由于没有 contemporaneous control，这些数不能单独说明 route treatment 的成本增量。

### 7.4 共享 Judge 饱和与 closeout 影响（历史并行样本）

隔离 Judge preflight 时为 32 configured/32 ready worker，result cache 明确为 disabled；但三次 run 共用这一服务。按 horizon 开始到最终精确关闭服务的窗口汇总：

| 观察 | 数值 |
|---|---:|
| `job_submitted` / `job_started` / `job_finished` | 4327 / 4174 / 4327 |
| finished status：succeeded / failed / cancelled / timed_out | 1318 / 2831 / 170 / 8 |
| `repl_recycled`（全部 request-limit） | 210 |
| submitted queue depth p50 / p95 / max | 1 / 57 / 111 |
| queue wait p50 / p95 / max | 185 ms / 225.2 s / 1933.6 s |

Horizon 结束后 worker 已无法及时消化 closeout 队列；run3 的 closeout receipt 明确为 `active_handlers=0`、`drained=false`、`remote_unsettled_jobs=1`，run1/run2 则在相同等待条件下由操作员停止。CPS route lock wait 仍只有毫秒级，因此本轮分数/吞吐的主导混杂是共享 Judge 容量，不是 route claim SQLite 事务。

## 8. 验证与独立审查

实现与单元验证沿用 `171ddda` 的 route-core 修复；第 7–9 节所述历史并行 image 从 clean HEAD `8d8c864` 构建。第 13 节顺序隔离 image 的 provenance 另列于 13.1。已有验证结果为：

- route core（CPS / broker / runner）单元测试：72/72 通过。
- profiling/config 专项：39/39 通过。
- protocol smoke：9/9 自检项通过，进程退出码 0。
- control/treatment formal config：两份 `validate`、两份 `plan` 均成功，task count 均为 12。
- Python compileall、Node `--check`、`git diff --check`：通过。

此前并行三次 run 的 `scripts/audit_profiling.py` 结果必须单独标为不完整（顺序隔离三轮的独立 audit 见 13.5）：

| run | audit exit | dropped fields | span missing end | terminal/profile 状态 |
|---|---:|---:|---:|---|
| treatment-1 | 1 | 159（53 rows） | 5 | `profile_terminal_missing`；外部停止后未正常结束 |
| treatment-2 | 1 | 129（43 rows） | 5 | `profile_terminal_missing`；外部停止后未正常结束 |
| treatment-3 | 1 | 126（42 rows） | 3 | `profile.end` 存在，但 Judge coverage `missing_required`，随后 drain timeout/run error |

这些历史 audit 失败来自 closeout/强制停止造成的生命周期缺口，而不是 profiling 文件无法解析；CPS、agent-wrapper、max-parallel coverage 在三次均存在。因而历史 profiling 可用于诊断，不应被写成“审计通过”或完整性能基准。

独立 exact-head review 在前一冻结点 `ef2f5f4` 发现一处中等严重度合同漏洞：direct CPS 对同一 actor 的 blocked claim 进行幂等重试时，错误返回 `acquired=true` / `claimed=true`。Broker/MJS 主路径会把 blocked 再归一为不可写，因而没有在现有主路径打开 write gate；但底层接口仍与“blocked 可见、不可写”的合同冲突。`171ddda` 已把幂等返回改为只有真实 `status=active` 才取得 write lease，并增加 direct CPS 回归测试；修复后的 72 项 route core 和 9 项协议 smoke 全部通过。

完整测试在精确实现快照 `157f1cb59569cba5a152922d04cb8ba4f7dee33c` 上运行 811 项，结果为 2 failures、1 skipped；没有 route-focused failure：

1. `test_unlimited_fast_agents_have_bounded_evaluator_backlog` 是 50ms horizon 的调度时序断言。精确实现串行 10 次失败 8 次；未改动的历史基线 `33296b0` 串行 20 次也失败 4 次，另一次 5 次样本失败 3 次。它不是 route-only failure，但当前快照中的触发频率不能视为已解决的兼容性风险。
2. `test_llm_admission_deadline_summary_is_selectable` 同样依赖跨 50ms horizon 的调度时序。精确实现串行 10 次失败 1 次，另一次串行 5/5 通过；历史基线串行 20/20 通过。它在两次 full discovery 中都曾失败，当前证据更符合负载敏感时序，但不能宣称已全绿。

中间快照 full discovery 还曾出现 `test_cli_plan_and_validate_all_four_arms` 因未关闭资源产生的 `ResourceWarning` 污染 stderr；它隔离串行 3/3 通过，并在本次精确快照 full discovery 中通过。

独立审查在 `ef2f5f4` 上又运行一次 full discovery：811 项中 1 failure、1 skipped，失败仍是上述 backpressure 断言；两项时序测试随后在同一候选上各重复 5 次，均 5/5 通过。未改基线 `33296b0` 的可审计重跑为 backpressure 16/20 通过、LLM deadline 20/20 通过。

修复后 clean `d297275e08e5a7341daa0e8c774e04de341342f9` 又运行了完整 discovery：812 项中 1 failure、1 skipped，唯一失败变为 `test_llm_admission_deadline_summary_is_selectable`（日志：`evidence/full_unittest_d297275.log`），没有 route-focused failure。当前候选的串行 10 次重复为 backpressure 5/10 通过、LLM deadline 10/10 通过（日志：`evidence/final_serial_repeat10_backpressure_d297275.log`、`evidence/final_serial_repeat10_llm_deadline_d297275.log`）；同一交替 block 的基线/候选 backpressure 分别为 13/20 与 6/20（日志：`evidence/paired_repeat20_backpressure_33296b0_vs_d297275.log`）。这些测试都把 horizon 压到 50ms，结果受调度和机器负载高度影响；它们只能标记 disabled-path/时序兼容风险，不能当作生产吞吐或 A/B 效果指标。

这些结果说明失败位于既有的极短 horizon 时序边界，且核心 route 测试全绿；报告不会把 full suite 写成全绿，也不会把不同时间、不同系统负载下的串行频率当作严格 A/B。正式实验前应把该 50ms 测试的候选/基线差异作为单独兼容性调查项，而不是忽略它或把它归因于 route 去重收益。

## 9. 此前并行尝试的 operator/runtime 边界（历史）

此前并行尝试实际完成了真实 Pi/Judge workload，但只使用隔离的一次性环境；本节的“本轮”均指该历史尝试：

- image/source 已冻结为 `contextswarm-iclr:route-claims-8d8c864-real3`、source `8d8c864`，运行产物的 `runtime_provenance` 写入同一 image digest 和 manifest SHA-256。
- Judge 使用任务专属的 loopback stack（32 worker、result cache disabled）和兼容 health proxy；三次旧 run 共享该 stack，未触碰既有 28100/28149/28201 listener。新顺序三轮的独立 stack、端口和 teardown 记录见第 13 节。
- preflight 成功确认 NuRouter `0.2.2`（binary SHA-256 `cbfb7bb4543f3e4c4840e735f6070c3ea54c4ba811a9e991c485beeacdccc05b`）、Pi `0.84.3`、模型 `gpt-5.6-sol` 可见、32/32 Judge worker ready、requested Lean env accepted、result cache disabled。declaration-index 未配置，且没有把它伪装成已验证的 revision match。
- 每个 run 使用独立 output/CPS SQLite/worker workspace；没有复制持久 Agent home、账号、Admin key、production 数据或远端长期测试环境。

启动阶段有两次命令形态错误（把脚本子命令写成不接受的显式 `run`，以及把绝对 host output 传入容器导致只读路径错误）；它们都在产生 run id 前退出，不计入旧并行三次样本。旧并行有效样本是第 7.1 节列出的三个 run id；新顺序有效样本见第 13.2 节。

实验结束后按隔离 stack 自带的 `status/down` 精确关闭 28301/28349，并按 pid-file 身份校验停止 28350 兼容代理；复核显示这三个端口和任务进程均已消失，共享 listener 仍在。没有访问远端长期环境、没有部署、账号导入、生产写入或外部发送。

## 10. 下一轮正式实验设计

### 10.1 最小可信设计

建议至少 3 个 paired blocks，每个 block 同时包含 control 和 treatment，共至少 6 次一小时 run。每个 block：

1. 使用同一 exact source commit、同一 image ID/digest、同一任务顺序、模型、Judge、declaration-index、并发、horizon 和 profiling 合同。
2. 仅切换 `route_claim_required`；不要同时引入 receipt、knowledge promotion、allocator 或检索排序改动。
3. 交替或随机化顺序，例如 AB / BA / AB；记录 warm/cold Judge 状态和 block id。
4. 每次先做 preflight，再运行，再做 profiling audit、closeout audit 和外部资源 sampler 对账。
5. 任一 run `DEGRADED` 时保留数据但单独标记；不要把 infra-degraded 与 clean run 无条件汇总。

历史三轮可作为问题发现和量级背景，不能替代新的 contemporaneous control。

旧并行样本和新顺序样本共同把下一轮的几项硬门槛暴露出来：

1. 新顺序样本已经做到每个 run 独立 Judge；正式 paired block 仍应为每个 arm/block 保持独立 Judge capacity pool，并记录 warm/cold 状态，不能回到旧的三 run 共享 stack。
2. 把 route-first 从 prompt 约定提升为 broker 可验证的状态门槛：在 actor 的首次 Judge 之前，没有 `active` claim 或明确 independent-verification declaration 就拒绝 Judge；在首次 claim 之前继续拒绝 search/inbox/send/publish。run2 的 2 个 judge-before-claim 和 1 个 restricted-CPS-before-claim episode 说明仅靠 prompt 不够。
3. 对 recovery/resume 增加每 actor 单 active lease 或显式 supersede：新顺序 run1/run2/run3 分别有 16/16/21 个 actor 出现多行历史 claim，单 actor 历史行数上限分别为 2/3/3；虽然没有同时的同 key primary overlap，仍会放大 active roster 噪声。
4. closeout 必须有独立的 bounded drain 结果和 authoritative settlement receipt；若超时，应把 run 标成 `DEGRADED` 并停止新增 closeout poll，而不是让操作员长时间等待后再 SIGTERM。

### 10.2 预注册指标

主要机制指标应分别报告 initial/adaptive Agent 分母：

- admission → first active-routes、first claim、first edit 的延迟；
- first edit 前完成有效 claim 的比例；
- primary conflict 数、冲突后换 route 数、独立验证 claim 数及理由覆盖率；
- `route_claim_bypass_reason` 次数和占 assignments 比例；
- finish/release/TTL 后残留 claim 数；
- 结构化相同 `route_key` 的冲突率。

结构化 key 只能测协议层重复，还应对 session reasoning/summary 做 blinded semantic audit，区分：真正重复、互补子问题、刻意独立验证、相同名称但不同证明路径。

结果与成本指标：

- proved tasks / 12、time-to-first-proof、closeout authoritative proof；
- assignments、Agent occupied-slot seconds、delivered tokens、cache read/write、调用成本；
- runner/CPS/Judge CPU、RSS/PSS、SQLite lock wait/hold、WAL 增长；
- timeout、recovery、invalid output、bypass、`DEGRADED` 原因。

判断可行性时，不应只看分数。若 treatment 明显降低语义重复、bypass/ghost claim 接近零、成本没有不可接受增加，即使小样本分数不变，也说明机制值得继续；反之，如果 route key 大量同义不同写、Agent 为抢 claim 生成过粗 key，或 CPS32 lock wait/协调时间吞掉收益，则应先改 route proposal/normalization，再扩大实验。

## 11. 已知限制与后续风险

1. 第一阶段不是 pre-reasoning proposal barrier；它只保证 claim 在 write/edit 前。
2. Route key 由 Agent 生成，缺少语义规范化；不同字符串可能表示同一路线，同一字符串也可能覆盖不同子路线。
3. 正常收尾会立即 release；旧并行样本和新顺序样本都没有 active claim 残留，但新顺序 run3 保留 1 行已终态的 `done` claim，不能把它静默改写成 `released`。若持久 store 在 runner 重试后仍故障，claim 仍只能等 TTL 清理。
4. 当前 formal TTL 为 3600 秒，worker heartbeat 不续租；硬崩溃可能留下最长一个 horizon 的 ghost route。
5. `blocked` 为 peer-visible 占位而非写 lease；分析时必须区分 row 的 visibility `active` 与 envelope 的 authorization `acquired`。
6. Fail-open 保证进度但牺牲 outage 期间的去重；必须按 bypass 分层分析结果。
7. 尚无每 session 的 route/event quota；恶意或失控 Agent 的高频 update 需要后续限流。
8. 新表在新 run 中自动创建，没有把历史 append-only `actors.json` 回填成可信 lifecycle。
9. 历史并行样本的 SQLite route claim lock wait 为每 run 0.0066–0.0107 秒总量、最大单次 1.293–3.732ms；新顺序样本的 `route.claim` lock wait 为 0.00495–0.00681 秒总量、最大单次 0.139–1.370ms。两组都只是本 workload 的观测，不能外推到更高并发或不同磁盘。
10. 旧并行三次 treatment 共用 Judge 且 closeout 不完整；新顺序三次已隔离 Judge 并完成 drained closeout，但仍均为 `DEGRADED`，不能用 5/4/5 与新顺序 6/6/6 做简单平均或因果结论。
11. 本轮 run2 仍观测到 prompt-level route-first 违规（2 个 episode 先 Judge、1 个先 restricted CPS），虽没有 edit/write 早于 claim；必须补 broker barrier 后再扩大样本。

## 12. 复现命令边界

以下命令覆盖离线验证；此前并行尝试使用同一配置和相对 output（每个 run 独立目录），三条命令在同一秒并行启动。该命令段只用于复盘错误实验协议：

```bash
python3 -m contextswarm_mini.cli \
  --config configs/formal_1h_cps32_profiled_route_control.toml validate --json
python3 -m contextswarm_mini.cli \
  --config configs/formal_1h_cps32_profiled_route_claim.toml validate --json
python3 scripts/route_claim_protocol_smoke.py --output "$EVIDENCE_DIR/route_protocol.json"
CONTEXTSWARM_PROFILE=1 python3 -m contextswarm_mini.cli \
  --config configs/route_claim_smoke_control.toml run --mock-agent --output "$CONTROL_RUNS"
CONTEXTSWARM_PROFILE=1 python3 -m contextswarm_mini.cli \
  --config configs/route_claim_smoke.toml run --mock-agent --output "$TREATMENT_RUNS"
```

真实三次的实际入口（在隔离 Docker runner 内）为：

```bash
umask 0022
scripts/run_docker.sh --config configs/formal_1h_cps32_profiled_route_claim.toml \
  --output runs/route-claim-real3/run1
scripts/run_docker.sh --config configs/formal_1h_cps32_profiled_route_claim.toml \
  --output runs/route-claim-real3/run2
scripts/run_docker.sh --config configs/formal_1h_cps32_profiled_route_claim.toml \
  --output runs/route-claim-real3/run3
```

三条真实命令由 supervisor 同时后台启动；这里省略了只存在于 operator 环境的 Judge loopback 注入和 NuRouter capability 参数，避免把机器路径/凭据写入文档。变量和 output 必须指向 owner-only、磁盘支持的目录；这段命令不能复现第 13 节的隔离协议。

历史并行尝试的脱敏汇总产物以逻辑证据 ID `route-claim-real3-summary` 标识；每个 run 的原始 profiling、Judge、CPS 文件保留在受限实验归档中，不在报告中展开 prompt、response、route summary 或凭据。

## 13. 用户纠正后的严格顺序、独立 Judge 三轮真实运行

### 13.1 实验协议（本节是本次有效样本）

用户指出此前“三次并行”把 Judge 竞争混进了实验，要求改为“隔离开、顺序跑三次”。本节记录按该要求重新执行的三轮 treatment；它取代第 7.1–7.4 节作为本次实验问题的有效运行记录，第 7 节只保留为错误协议的历史复盘。

每一轮都满足以下边界：

- 只启动一个 12 题、3600 秒 horizon 的 treatment；上一轮 runner 返回、closeout 完成、Judge stack teardown 后，才启动下一轮。
- 每轮拥有独立的 32-worker Judge stack、Lean workspace、兼容 proxy、router/backend 端口和 CPS SQLite；没有共享 Judge queue、result cache 或 worker workspace。
- 三轮使用完全相同的 source commit、Docker image、manifest 和 workload；只改变 run 的隔离目录/端口，不改变实验臂。
- 运行前后只使用一次性 owner-only 磁盘目录；没有使用持久 Agent home、远端长期环境、生产账号、Admin key 或生产数据。
- 运行结束后按 stack 自带的 down/status 流程关闭，并复核任务端口和进程消失；未触碰不在本任务范围内的 28100/28149/28201 listener。

本次有效样本的精确 provenance：

| 项目 | 值 |
|---|---|
| source commit | `943b0eecebcfd59a648c6d58d99efa12c4ae2290` |
| Docker image | `sha256:4451e6aac33fc1fe5dfa822a00afc3ec618f20c9a6e149f21ed165785e2b0e6b` |
| manifest | `configs/formal_1h_cps32_profiled_route_claim.toml` |
| manifest SHA-256 | `73fe890b2a1b89c2e96c3a74ef99275d845856b7271a070463e9960880a4cff5` |
| Judge 配置 | 每轮 32 configured workers，result cache disabled |
| runner | 三轮 `runner rc=0`；horizon 为 3600 秒 |

服务窗口和轮次间隔由脱敏汇总中的 Judge service event log 直接计算，窗口没有重叠：

| run | run id | 独立端口（router/backend/proxy） | Judge service window (UTC) | 下一轮前间隔 |
|---|---|---|---|---:|
| 1 | `20260903T201950Z-7ba44f46` | `28401/28449/28450` | `20:19:44.472–21:23:22.306` | 270.569 s |
| 2 | `20260903T212758Z-1a7dbf9a` | `28501/28549/28550` | `21:27:52.875–22:29:18.067` | 20.066 s |
| 3 | `20260903T222943Z-ec622eae` | `28601/28649/28650` | `22:29:38.133–23:30:29.659` | — |

因此，这三轮不存在“前一轮和后一轮同时把 judge job 打进同一个服务”的竞争；轮间仍保留了可审计的 teardown gap，而不是依靠“看起来应该已经结束”的时间假设。

### 13.2 每轮成绩、生命周期和 CPS 结果

下表的 score/`PROVED` 来自各 run 的 `final.json`（不是把旧并行样本的 horizon event 数拼进来）。`DEGRADED` 是运行健康状态，表示本轮虽然产出了完整 run record 和 closeout receipt，但基础设施健康检查仍不满足 `ok=true`。

| run | runner elapsed | final status | score (`PROVED` / max) | `COMPILES_WITH_SORRY` | admitted actors | route claims | messages / pieces | final claim status |
|---|---:|---|---:|---:|---:|---:|---:|---|
| 1 | 3813 s | `DEGRADED` | 6 / 12 | 6 | 88 | 104 | 747 / 132 | `released` 104 |
| 2 | 3682 s | `DEGRADED` | 6 / 12 | 6 | 92 | 109 | 666 / 89 | `released` 109 |
| 3 | 3647 s | `DEGRADED` | 6 / 12 | 6 | 85 | 106 | 675 / 88 | `released` 105；`done` 1 |
| **合计/均值** | — | — | **18 / 36；均值 6 / 12** | **18** | **265** | **319** | **2088 / 309** | **无 active/blocked 残留** |

三轮 `final.json` 的 verdict 分布都是 `PROVED=6`、`COMPILES_WITH_SORRY=6`。run3 的 `events.jsonl` 中 `judge_proof_credited` 计数为 5，而同一 run 的 closeout/final verdict 计数为 6；这是一个需要后续修复或解释的 event-to-final reconciliation 差异。本报告把 6/12 明确标为“run final artifact score”，不把它升级成已经完全无争议的 benchmark authoritative score。

run3 唯一的非 `released` 行是一个已经终态化的 `done` claim（`uk2024_r1_p1`、episode 1，release reason `updated`），没有 active/blocked claim、未结 remote job 或可继续写入的 lease；它仍作为生命周期审计的边界例外保留，不能静默改写成全量 `released`。

### 13.3 独立 Judge 是否真的消除了跨 run 竞争

每轮 Judge event log 都显示 `job_submitted = job_started = job_finished`，且提交队列近似最大值为 1。关键 queue wait 指标如下；它们是每轮独立 stack 内的排队，不是三轮共享队列的混合值：

| run | Judge jobs | queue wait p50 | queue wait p95 | queue wait max | execution p95 | REPL recycle（均为 request-limit） |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 2003 | 117 ms | 166 ms | 333 ms | 10.253 s | 84 |
| 2 | 1724 | 165 ms | 183 ms | 367 ms | 10.167 s | 80 |
| 3 | 1983 | 111 ms | 133 ms | 210 ms | 12.644 s | 88 |

这和旧并行样本的共享 queue p95 225.2 秒、max 1933.6 秒是不同实验条件下的观测；新样本没有复现那种跨 run 共享 Judge 堆积。隔离并不等于 Judge 完全健康：run2 teardown 时 ready workers 为 25/32，run3 为 19/32，二者都记录 `restore_min_workers_blocked_worker_slot_limit`；三轮 health `ok` 都为 false，且分别有 22、19、23 个 Judge probe infrastructure errors。也就是说，用户指出的“相互竞争”已被实验设计移除，但单轮内部仍有 worker recycle、probe error 和 solver failure，需要作为独立基础设施问题处理。

所有三轮 `judge_broker_closeout.json` 都满足：`active_handlers=0`、`drained=true`、`fifo_depth=0`、`remote_unsettled_jobs=0`。这正是本次顺序协议与此前共享 Judge 尝试最重要的可验证区别：后一轮不是在前一轮残留 job 上继续启动。

### 13.4 Route claim 协议审计

三份 CPS 的脱敏审计均为 `PRAGMA integrity_check = ok`；历史/active primary duplicate group 均为 0；fail-open `route_claim_bypass_reason` 均为 0；所有持久 claim 都是 primary（`primary=1`）。独立验证理由字段分别出现 78、77、83 次，但这些仍是 primary claim 的明确理由，不应误读为已经形成 secondary claim。

| 指标 | run1 | run2 | run3 | 三轮解释 |
|---|---:|---:|---:|---|
| admitted episode | 88 | 92 | 85 | 只统计真实 admission，不含预注册未来 Agent |
| `active_routes` presence | 88 | 92 | 84 | run3 有 1 个 episode 没有完整 presence 记录 |
| `claim` presence | 88 | 92 | 84 | 与上行 episode 对齐 |
| `claim_before_edit` | 86 | 90 | 80 | 观测到的 edit/write 均未早于 claim |
| `edit/write` 早于 claim | 0 | 0 | 0 | 关键 write gate 没有被绕过 |
| `judge_check` 早于 claim | 0 | 2 | 0 | run2 暴露 prompt-level route-first 漏洞 |
| restricted CPS 早于 claim | 0 | 1 | 0 | 同样是 broker barrier 尚未完全强制 |
| historical/active duplicate primary groups | 0 | 0 | 0 | SQLite 唯一约束和 lifecycle 结果一致 |
| fail-open bypass | 0 | 0 | 0 | 没有伪装成成功 claim |

每轮 route transaction 的锁等待仍是毫秒级：`route.claim` 总 lock wait 为 6.810/4.950/5.118 ms，最大单次为 1.370/0.139/0.163 ms（run1/run2/run3）。这说明在本 workload 下 CPS 原子 claim 不是主要延迟来源，但不能外推到更高并发或不同存储。

这组结果支持一个窄结论：route claim 的“先查、再原子 claim、再允许 edit/write”主路径在真实 Agent session 中有可观测效果，且没有持久 duplicate primary 或 bypass。它不支持更强的“语义重复探索下降了多少”结论，因为 route key 是 Agent 生成的字符串，本报告没有对私有 reasoning 或 route summary 做 blinded semantic audit。

### 13.5 Profiling、健康和证据完整性

三轮 profiling 文件都可解析，且 agent-wrapper/CPS/Judge/max-parallel coverage 存在，`profile_started`、`profile.end`、`run.end` 的终止记录有效；但审计命令均以退出码 1 结束，不能写成“profiling audit 通过”：

| run | audit exit | dropped-field rows / total | span missing end | profile termination |
|---|---:|---:|---:|---|
| 1 | 1 | 56 / 168 | 1 | `valid=true`，但 audit 不完整 |
| 2 | 1 | 60 / 180 | 1 | `valid=true`，但 audit 不完整 |
| 3 | 1 | 53 / 159 | 1 | `valid=true`，但 audit 不完整 |

run1 的 final Judge health snapshot 因首次 harness 的收尾实现缺陷没有单独落盘；该轮仍有完整 Judge service event log、run final、CPS integrity 和 closeout receipt，因而纳入结果，但不冒充拥有与 run2/run3 相同的 health snapshot。这个缺口已经在后续顺序 harness 中修正，run2/run3 均有 bounded health snapshot。

### 13.6 与原版基线的可比性和下一步判断

原版此前三轮记录为 5/4/5，即 14/36；本次顺序隔离 treatment 三轮为 6/6/6，即 18/36。这个差值只可以作为描述性观察，不能解释成 route claim 的因果收益，原因包括：

- 原版与本次 source/image、NuRouter/Pi runtime、运行时间和 Judge 状态不同；
- 原版历史记录和本次 treatment 不是同一轮的 contemporaneous control；
- 本次三轮全是 treatment，没有 control arm，也没有随机化题目顺序或 AB/BA 配对；
- 本次三轮虽然隔离了跨 run Judge 竞争，但每轮仍有 `DEGRADED` health、probe infrastructure error、solver failure 和 REPL recycle；
- `final.json` 与 run3 event proof credit 存在 1 个计数差异，必须在正式 benchmark 前解释。

因此本次运行的正确结论是：

1. 用户指出的实验设计问题已经纠正；三轮之间没有共享 Judge 竞争，且每轮可独立复盘、收尾和清理。
2. active roster + route claim 的真实生命周期、SQLite 唯一性、write/edit gate 和 fail-open 记录得到三轮真实 workload 的支持。
3. 不能据此宣布数学得分提升，也不能宣布语义重复探索已下降；18/36 只是隔离 treatment 的描述性结果。
4. 下一轮正式效果实验应采用同一精确 source/image 的 paired control/treatment，至少 AB/BA/AB 或随机化顺序；每个 arm 都使用独立 Judge stack，并把 broker-level “首次 Judge 前必须有 active claim 或明确 independent verification”做成不可绕过的状态门槛。

### 13.7 脱敏产物与复现边界

本次顺序三轮的脱敏汇总以逻辑证据 ID `sequential-isolated-summary` 标识（schema `contextswarm_sequential_isolated_experiment_v1`）。其中只保存 provenance、时间窗口、计数、健康摘要、协议顺序和 profiling audit 摘要，不保存原始 prompt、model response、route summary、账号、token 或节点配置。

逐轮原始证据位于：

对应的原始 run 目录属于受限实验归档，不随 PR 分发。

operator 侧的顺序 harness 和 bounded evidence 位于：

对应的实验归档属于 owner-only 存储，不随 PR 分发。

真实入口脚本是 `run_sequential_isolated.sh`；它的关键合同是“一个 run 完成后才 teardown，再启动下一个”，不是把三条 runner 命令放到后台并行。该目录位于 workspace ZFS 磁盘支持的 owner-only build root；实验结束时任务端口已释放，未执行远端部署、生产写入或不可逆清理。
