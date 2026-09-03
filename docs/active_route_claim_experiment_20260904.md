# Active roster + route claim 第一阶段实现与实验记录

日期：2026-09-04

状态：第一阶段协议已实现并通过离线机制验证；同源 control/treatment 正式配置已冻结；真实 12 题 × 1 小时效果实验尚未执行

实现基线：`33296b07634c708412326c2808d5782dab3f788e`

最终实现与自检快照：`171ddda14e0495cba55a94ec0c2458741bd4d4d2`

## 1. 结论

这套方案作为第一阶段是可行的，但当前证据只足以支持“协议机制成立”，还不足以支持“数学得分提高”或“语义重复探索已经下降”的结论。

已经成立的部分是：

1. Runner 只在真实 admission 时登记 Agent；未 admission 的未来 Agent 不再提前出现在 roster 中。正常结束、取消、超时、recovery exhausted 和 solved-by-peer 等收尾路径都会尝试结束 actor，并在同一 CPS 事务中释放其活跃 route claim。
2. 同一题、同一 `route_key` 的 primary claim 由 SQLite 原子约束保证最多一个；冲突者会看到已有 owner，可换方向，也可以附带明确的 `independent_verification_reason` 取得非 primary 的独立验证 claim。
3. `cps_active_routes` 和 `cps_claim_route` 是仅有的首次 terminal Judge checkpoint 前 CPS 能力；原有 search、inbox、send、publish 以及 route update/release 仍受 Judge gate 约束。
4. Treatment 下，Pi 的 write/edit 在拿到有效 route lease 前被阻止；只有 broker 明确发出的 fail-open 结果才能绕过，并且必须携带规范化的 `route_claim_bypass_reason`，不能伪装成成功 claim。
5. 三方同时竞争同一 route 的离线协议 smoke 得到 1 个 primary 和 2 个 conflict；独立验证、finish、TTL expiry、blocked、显式 release、admission-only roster 和 pre-Judge gate 均通过自检。
6. 修复后的最终 clean 快照又运行了一对同源 3 题 mock control/treatment：两臂均完成 6/6 assignment，`health.ok=true`，profiling 审计通过且 `dropped_fields.total=0`。但 mock Agent 不调用 route tools，也不产生数学结果；它只能验证 runner lifecycle、配置身份和产物闭环。

尚未成立的部分是：

- 没有真实 Pi/Judge session，因此没有 route 选择、冲突后换路、语义重复率、得分、time-to-proof 或 token/cost 的 treatment 观测。
- 当前环境没有本轮所需的 Judge endpoint/health、同 revision declaration index、受控 NuRouter runtime 输入和 operator launch contract；因此没有把离线 mock 冒充成一小时正式实验。
- 历史三轮都来自旧 source 且均为 `DEGRADED`。把历史 5/4/5 分直接与新实现的一次 treatment 比较，会同时混入 source、运行顺序、基础设施和故障状态差异，不构成因果对比。

因此当前决策应是：保留第一阶段实现，下一步在同一精确 source/image 上做成对、重复的 control/treatment 正式实验；在该数据到位前，不声称方案改善了最终成绩。

## 2. 问题、假设与证据层级

本实验要验证的假设是：如果 Agent 在第一次实质性修改候选文件前，必须先看到同题活跃方向并原子声明自己的 route，那么无意识的并发重复探索会下降；节省的 slot/token 时间可能进一步改善证明覆盖率或 time-to-proof。

需要把四层证据分开：

| 层级 | 本轮状态 | 能回答的问题 | 不能回答的问题 |
|---|---|---|---|
| 历史观测 | 已完成 | 原版系统是否缺少早期方向协调、重复风险有多大 | 新协议是否带来因果改善 |
| 单元/协议机制 | 已完成 | 原子竞争、身份绑定、gate、TTL、release、fail-open 是否按合同工作 | Agent 是否会生成稳定且语义正确的 route key |
| 同源 mock 编排 | 已完成 | treatment 是否能完成 admission/finish、配置/产物/profiling 是否闭环 | 数学效果、真实调用开销、冲突后行为 |
| 真实重复 A/B | 未执行 | 重复率、得分、时间、成本和稳定性变化 | — |

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

## 7. 正式一小时 A/B 配置

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

## 8. 验证与独立审查

最终实现快照 `171ddda` 已完成：

- route core（CPS / broker / runner）单元测试：72/72 通过。
- profiling/config 专项：39/39 通过。
- protocol smoke：9/9 自检项通过，进程退出码 0。
- control/treatment formal config：两份 `validate`、两份 `plan` 均成功，task count 均为 12。
- Python compileall、Node `--check`、`git diff --check`：通过。

独立 exact-head review 在前一冻结点 `ef2f5f4` 发现一处中等严重度合同漏洞：direct CPS 对同一 actor 的 blocked claim 进行幂等重试时，错误返回 `acquired=true` / `claimed=true`。Broker/MJS 主路径会把 blocked 再归一为不可写，因而没有在现有主路径打开 write gate；但底层接口仍与“blocked 可见、不可写”的合同冲突。`171ddda` 已把幂等返回改为只有真实 `status=active` 才取得 write lease，并增加 direct CPS 回归测试；修复后的 72 项 route core 和 9 项协议 smoke 全部通过。

完整测试在精确实现快照 `157f1cb59569cba5a152922d04cb8ba4f7dee33c` 上运行 811 项，结果为 2 failures、1 skipped；没有 route-focused failure：

1. `test_unlimited_fast_agents_have_bounded_evaluator_backlog` 是 50ms horizon 的调度时序断言。精确实现串行 10 次失败 8 次；未改动的历史基线 `33296b0` 串行 20 次也失败 4 次，另一次 5 次样本失败 3 次。它不是 route-only failure，但当前快照中的触发频率不能视为已解决的兼容性风险。
2. `test_llm_admission_deadline_summary_is_selectable` 同样依赖跨 50ms horizon 的调度时序。精确实现串行 10 次失败 1 次，另一次串行 5/5 通过；历史基线串行 20/20 通过。它在两次 full discovery 中都曾失败，当前证据更符合负载敏感时序，但不能宣称已全绿。

中间快照 full discovery 还曾出现 `test_cli_plan_and_validate_all_four_arms` 因未关闭资源产生的 `ResourceWarning` 污染 stderr；它隔离串行 3/3 通过，并在本次精确快照 full discovery 中通过。

独立审查在 `ef2f5f4` 上又运行一次 full discovery：811 项中 1 failure、1 skipped，失败仍是上述 backpressure 断言；两项时序测试随后在同一候选上各重复 5 次，均 5/5 通过。未改基线 `33296b0` 的可审计重跑为 backpressure 16/20 通过、LLM deadline 20/20 通过。

修复后 clean `d297275e08e5a7341daa0e8c774e04de341342f9` 又运行了完整 discovery：812 项中 1 failure、1 skipped，唯一失败变为 `test_llm_admission_deadline_summary_is_selectable`（日志：`evidence/full_unittest_d297275.log`），没有 route-focused failure。当前候选的串行 10 次重复为 backpressure 5/10 通过、LLM deadline 10/10 通过（日志：`evidence/final_serial_repeat10_backpressure_d297275.log`、`evidence/final_serial_repeat10_llm_deadline_d297275.log`）；同一交替 block 的基线/候选 backpressure 分别为 13/20 与 6/20（日志：`evidence/paired_repeat20_backpressure_33296b0_vs_d297275.log`）。这些测试都把 horizon 压到 50ms，结果受调度和机器负载高度影响；它们只能标记 disabled-path/时序兼容风险，不能当作生产吞吐或 A/B 效果指标。

这些结果说明失败位于既有的极短 horizon 时序边界，且核心 route 测试全绿；报告不会把 full suite 写成全绿，也不会把不同时间、不同系统负载下的串行频率当作严格 A/B。正式实验前应把该 50ms 测试的候选/基线差异作为单独兼容性调查项，而不是忽略它或把它归因于 route 去重收益。

## 9. 为什么本轮没有启动真实 12 题一小时 treatment

真实 run 的代码入口已经准备好，但当前会话缺少以下 operator/runtime 输入：

- 可访问且 health/revision 匹配的 MathOlympiadBench Judge；
- 与 Judge Mathlib revision 一致的 declaration-index；
- 当次受控 NuRouter binary、node/coordinator config 和允许的模型容量；
- 固定 source/image digest、外部资源采样 PID 边界和正式输出位置。

这些信息既不能从端口或历史路径推断，也不应从其他运行复制。没有它们时启动只会得到不可解释的 infra failure，或者越过用户的 runtime/credential 边界。因此本轮只执行了离线协议 smoke 和 mock orchestration；没有访问远端环境、没有真实模型请求、没有部署、没有账号或数据库操作。

## 10. 下一轮正式实验设计

### 10.1 最小可信设计

建议至少 3 个 paired blocks，每个 block 同时包含 control 和 treatment，共至少 6 次一小时 run。每个 block：

1. 使用同一 exact source commit、同一 image ID/digest、同一任务顺序、模型、Judge、declaration-index、并发、horizon 和 profiling 合同。
2. 仅切换 `route_claim_required`；不要同时引入 receipt、knowledge promotion、allocator 或检索排序改动。
3. 交替或随机化顺序，例如 AB / BA / AB；记录 warm/cold Judge 状态和 block id。
4. 每次先做 preflight，再运行，再做 profiling audit、closeout audit 和外部资源 sampler 对账。
5. 任一 run `DEGRADED` 时保留数据但单独标记；不要把 infra-degraded 与 clean run 无条件汇总。

历史三轮可作为问题发现和量级背景，不能替代新的 contemporaneous control。

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
3. 正常收尾会立即 release，但持久 store 在 runner 三次 closeout 重试后仍故障时，claim 只能等 TTL 清理。
4. 当前 formal TTL 为 3600 秒，worker heartbeat 不续租；硬崩溃可能留下最长一个 horizon 的 ghost route。
5. `blocked` 为 peer-visible 占位而非写 lease；分析时必须区分 row 的 visibility `active` 与 envelope 的 authorization `acquired`。
6. Fail-open 保证进度但牺牲 outage 期间的去重；必须按 bypass 分层分析结果。
7. 尚无每 session 的 route/event quota；恶意或失控 Agent 的高频 update 需要后续限流。
8. 新表在新 run 中自动创建，没有把历史 append-only `actors.json` 回填成可信 lifecycle。
9. SQLite CPS32 下的实际 lock contention 尚未测量；synthetic micro-timing不能外推。
10. 历史三轮均 `DEGRADED`，不能用 5/4/5 的简单平均充当稳定对照分数。

## 12. 复现命令边界

以下命令只覆盖离线验证：

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

变量必须指向 owner-only、磁盘支持的目录；这些命令不授权真实 workload。正式一小时 run 仍需 operator 先提供并冻结第 9 节的运行合同。
