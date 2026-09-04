# MathOlympiadBench：超时终止 checkpoint 对比实验

## 结论先行

本次改动的目标不是把未完成证明算成成功，而是把“已经写过的候选、最近的验证反馈、已记录的阻塞方向和可继续位置”从一次性 Agent 工作目录提升为下一次 assignment 可读取的、带哈希的未验证交接物。

当前实现已经完成并通过离线/mock 验收；三次真实一小时 A/B 运行尚未启动，因为本机没有本次任务授权的 Judge URL、Judge cache health URL、节点配置或匹配的容器镜像。没有这些输入时不能把 mock/replay 数字冒充真实得分结果，也不能启动旧的本地 Test Lab 或把远端长期环境当成本地测试环境。

因此本文件分开记录：

1. 原版三次运行中实际丢失的内容；
2. checkpoint 的实现合同和不改变的评分边界；
3. 已完成的离线/mock 结果；
4. 真实 A/B 所需的固定合同、指标和当前阻塞。

## 1. 原版三次运行的事实基线

纳入比较的原版是同一冻结合同下的三个 run：

| run | 得分 | logical assignments | solver timeout | cancelled | abnormal failure | CPS pieces / messages |
|---|---:|---:|---:|---:|---:|---:|
| `20260901T012227Z-8c90d3f0` | 5/12 | 82 | 62 | 17 | 2 | 67 / 439 |
| `20260902T075657Z-eda06caf` | 4/12 | 76 | 64 | 10 | 0 | 74 / 434 |
| `20260902T090313Z-ecee9c07` | 5/12 | 82 | 64 | 15 | 1 | 70 / 375 |
| 合计 | — | 240 | 190 | 42 | 3 | 211 / 1248 |

三次均使用 MathOlympiadBench 12 题、3600 秒 horizon、CPS/blackboard、32 slots、uniform allocation、`initial_agents_per_task=2`、Pi timeout 900 秒和同一模型合同。三次均为 `DEGRADED`；因此它们适合回答“状态有没有留下、后续能不能看到”，不适合单独证明得分因果。

### 原版到底留下了什么

逐个 terminal Agent 的 `result.lean` 与 benchmark baseline 做哈希比较，得到以下保守口径：

| run | 非成功 terminal 尝试 | 其中 result.lean 非 baseline | 包含成功尝试时的非 baseline 总数 |
|---|---:|---:|---:|
| `8c90d3f0` | 81 | 69 | 70 |
| `eda06caf` | 74 | 68 | 70 |
| `ecee9c07` | 80 | 69 | 71 |
| 合计 | 235 | 206 | 211 |

这 206 个非成功尝试的候选文件不是“证明”，但确实不是空 baseline。它们留在对应 `workers/<task>/agents/<agent>/result.lean` 临时目录里；原版流程随后把这些结果记成 `AGENT_FAILURE`、`CANCELLED` 或 timeout closeout，没有把文件、哈希和继续信息写成统一的下一 assignment 交接对象。文件存在本身也不表示 Agent 的数学结论正确。

原版的结构化 CPS 有价值但不完整：三次合计 197 条 Agent pieces、14 条 runner pieces、1248 条 messages。消息正文约为 pieces 正文的 2.21 倍；消息同时承载询问、代码/Lean 细节和反例，很多负面结论只停留在 message。所有 96 个首批并发启动 digest 都为空；因此第一批 Agent 没有可靠的 active-route/已做过什么视图。已有记录能证明发送、暴露或文件变化，不能证明后续 Agent 真正采用了某条路线。

### 可复核的具体缺口

这不是抽象的“日志少了一点”，而是信息落在了错误的生命周期层级：

| 观察 | 原版证据 | 为什么会影响下一次继续 |
|---|---|---|
| 明确反例只在短期 message | `imo2024_p2` 的具体数值反例、`usa2024_p2` 的假下界、`uk2024_r1_p1` 的 `rfl/decide` 失败都没有对应负面 piece | message 可能只进入特定 recipient 的 inbox；ack 或 recipient 结束后，后续 assignment 的可检索摘要不再稳定包含它 |
| 发给已结束/错误范围的对象 | 1170 条定向消息中 302 条发送时 recipient 已结束；另有 3 条跨 task、按 task 过滤后必然不可达 | “发送成功”不等于下一 Agent 收到，尤其不能作为 termination handoff |
| 第一批没有当前方向视图 | 96/96 个首批 startup digest 为空；后续 144 个 adaptive digest 多数非空但主要是历史 pieces/messages | 同一波 Agent 在第一次 edit 前没有可靠的 active-route 或“不要重复”屏障 |
| 候选文件与结论脱节 | 206 个非成功 terminal 尝试的 `result.lean` 与 baseline 不同，但没有统一 candidate hash、验证状态、阻塞和 next-step 指针 | 文件仍可离线找到，却不能保证当时下一 assignment 会读到、知道它未验证，或知道应从哪一步继续 |

自动词法审计另外筛出 93 条负面 message（合并为约 89 组正文），其中 83 组没有找到同作者后续相似负面 piece；这 83 组是待人工复核队列，不应直接当成 83 个已确认遗漏。上表三类人工核对案例已经足以证明：原版不是没有产出，而是没有在终止边界把“候选 + 结论 + 继续位置”绑定成一个可交接对象。

### 丢失边界

原版并非“所有信息都消失”：

- 已发布的 pieces/messages、Judge receipt、profiling 和每个 worker 文件仍可在原始 run 目录中离线审计；
- 但超时后没有一个稳定、原子、带 candidate hash 的 latest checkpoint 指针；
- 只有部分消息/结构化条目进入下一次 digest，且直接消息可能发给已结束的 recipient；
- 运行结束后再从原始目录人工重建，不能等同于当时下一 Agent 可见的状态；
- `output_tail` 在很多 timeout 中为空，不能依赖它代替候选和交接记录。

## 2. 历史候选重放：覆盖能力而非得分证据

为避免只凭单元测试推断“原版丢失边界已经被覆盖”，我对上面三个原始 run 做了一次只读重放：逐个读取 terminal Agent 的 `result.lean`，只挑选非成功且哈希不同于题目 baseline 的候选（206 个），交给本分支的 `CheckpointStore.save`，再用 `materialize_for_agent` 模拟下一次 assignment 的交接，并重新核对快照 SHA-256。没有调用模型、Judge 或评分器；候选正文没有写入本仓库。

| run | timeout / cancelled / failure | 非成功 changed 候选 | checkpoint 保存 | 下一 assignment 交接 | hash 核对 |
|---|---:|---:|---:|---:|---:|
| `8c90d3f0` | 62 / 17 / 2 | 69 | 69/69 | 69/69 | 69/69 |
| `eda06caf` | 64 / 10 / 0 | 68 | 68/68 | 68/68 | 68/68 |
| `ecee9c07` | 64 / 15 / 1 | 69 | 69/69 | 69/69 | 69/69 |
| 合计 | 190 / 42 / 3 | **206** | **206/206** | **206/206** | **206/206** |

机器可读结果保存在本次任务的 owner-only evidence 目录 `evidence/historical-replay.json`，schema 为 `contextswarm_checkpoint_historical_replay_v1`。`candidate_missing=0`、`baseline_mismatch=0`、`capture_rate=1.0`、`handoff_rate=1.0`。这说明对原版已实际产生的局部候选，新的保存/复制/哈希链路没有再把它留在临时 Agent 目录之外；它**不能**说明候选数学上正确，也不能反事实地证明当时下一 Agent 会因此得分更高。重放没有凭空补回隐藏 reasoning；`completed_work`、`ruled_out`、`next_step` 只有在原始 CPS/message 中已有可读证据时才有意义。

## 3. 改动合同

### 强制保存点

在 CPS runner 的每个 Pi `AgentResult` 返回边界调用 checkpoint sink：包括 inner recovery 的失败、最终失败、取消、horizon closeout 和正常返回。保存发生在 runner 释放 solver slot、执行最终 Judge 评估和记录 closeout 之前；对正常返回的候选，在 Judge receipt 之后再追加一个不可变快照，把最终验证状态/反馈补齐。保存异常是 fail-open，不会改变 recovery、Judge 或 score 生命周期。

每个任务的 `workers/<task>/checkpoints/` 包含：

- `000001/`, `000002/`, …：不可变的候选快照和 `checkpoint.json`；
- `latest.json`：原子更新的 latest 指针；
- `index.jsonl`：按保存顺序的 bounded ledger。

记录至少包含 candidate 文件状态/大小/SHA-256/来源（当前 workspace 或 carry-forward）、attempt/recovery 序号、timeout/cancel/horizon/process-failure 原因、是否仍待 retry、最近验证状态/反馈、有限的 CPS pieces 和 task-local messages、`completed_work`、`ruled_out`、`next_step`。所有文本经过长度限制和 endpoint/credential/path 脱敏；目录为 0700，文件为 0600。

### 下一次 assignment 的交接

当 manifest 的 `checkpoint.transfer=true` 时，新的 workspace 收到 `checkpoint/checkpoint.json` 和经哈希核对的候选副本。普通 CPS（原版 `candidate_transfer=true`）仍把已验证 best candidate 放在活动 `result.lean`，checkpoint 候选放在独立目录并由 prompt 明确要求先检查；这样未验证内容不会静默覆盖已验证候选。新的 Agent 必须重新 `judge_check`，checkpoint 永远不计分、不直接 promote。

`checkpoint.publish=true` 时，runner 还向 task-local CPS 写入 `kind=checkpoint`、`runner_checkpoint/unverified/timeout_recovery` 标签的 bounded piece。该 piece 不是 proof，也不进入普通 strategy-piece 计数；它只让后续 assignment 能从 CPS 看到 latest 交接元数据。磁盘快照先于发布，发布在取消/整体 horizon 已关闭时可记录为 skipped；发布失败单独记为 `checkpoint_publish_failed`，不伪装成 candidate failure。

### 诚实边界

Runner 能强制保存已经存在于文件、CPS、Judge feedback 和可读 result/session tail 中的证据；如果整个 Python runner 被 SIGKILL，或模型从未把隐含 reasoning 写入文件/消息，事后无法凭空恢复语义证明。因而 checkpoint 的 `unverified=true` 和 `score_eligible=false` 是硬合同。若要保证模型主动写出“当前路线/阻塞/下一步”的语义摘要，还需要后续的 cooperative summary 协议；本轮不把推断当成事实。

## 4. 已完成的验收

实现分支：`feat/timeout-checkpoint-20260904`（基于原版 source `33296b07634c708412326c2808d5782dab3f788e`）。

已通过：

- checkpoint store 原子写入、不可变序号、hash 校验、路径/凭据脱敏和 0600/0700 权限测试；
- recovery sink 在失败尝试重试前收到记录，并在成功返回时收到最终记录；若后续尝试
  仍只留下已验证 best，则会 carry-forward 先前的 changed candidate，避免 latest 指针
  被空 baseline closeout 覆盖；
- message-only blocker 被加入 bounded ruled-out context，即使 direct message 已 ack；
- mock CPS runner：两次失败写入 `partial-1`/`partial-2`，新的 assignment 读到 `checkpoint/result.lean=partial-2`，Judge 只评估第三次新候选；checkpoint piece 从未成为 proof；
- 现有 recovery/CPS partial focused tests（含 slot refill）通过；checkpoint/recovery focused 合计 **30 个测试通过（30/30）**。

另做了一次同配置的短时 mock smoke（baseline 与 treatment 各一轮，`configs/smoke.toml`、1 题、30 秒合同）：

| arm | score | assignments | checkpoint saved / captured | handoff | published pieces |
|---|---:|---:|---:|---:|---:|
| baseline | 0/1 (`MOCK_SKIPPED`) | 2 | 0 / 0 | 0 | 0 |
| checkpoint treatment | 0/1 (`MOCK_SKIPPED`) | 2 | 4 / 4 | 1 | 2 |

完整摘要在 `evidence/mock-smoke3-comparison-1788493558.json`。treatment 每个正常 mock 返回先写一份“评估前”不可变快照，再在最终 closeout 写一份补齐验证边界的快照；两臂都没有真实证明，故该 smoke 只验证开关、写入、发布和 closeout 计数，不是质量或速度比较。

另外以相同的有限 mock 合同打开 profiling（2 秒 horizon、最多 2 次 assignment）核对新增成本边界：treatment 的 4 次保存 span 合计 14.473 ms、2 次发布 span 合计 3.919 ms；baseline 没有这些 span。该数字只表示本机小候选/SQLite 的编排开销，不能外推到真实 Pi、Judge 或 32-slot 一小时运行。摘要为 `evidence/profiled-smoke3-comparison-1788493576.json`，其中每个 span 同时保留 wall/CPU 字段。

建议的本地验证命令（输出目录应位于磁盘支持的 `.workspace/builds/CS-20260904/`）：

```bash
umask 0022
TMPDIR=/home/ubuntu/workspace/.workspace/builds/CS-20260904/tmp \
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
python3 -m unittest tests.test_checkpoint tests.test_cps_recovery_partial tests.test_agent_recovery
python3 -m compileall -q contextswarm_mini
PYTHONWARNINGS=ignore::ResourceWarning python3 -m unittest discover -s tests
```

carry-forward 修正后的 clean-tree 全套 discovery 在
`PYTHONWARNINGS=ignore::ResourceWarning` 下实际运行了 **736/736** 个测试，结果为
`OK (skipped=1)`；checkpoint 相关聚焦集合为 30/30，`compileall` 通过。为保留
未抑制 warning 的真实边界，同一提交再运行一次未设置 `PYTHONWARNINGS` 的全套，
得到 736 个测试、1 个 Figure-4 的 0.05 秒边界时序失败（无 checkpoint 相关失败）。
更早的 clean-tree 运行还出现过 evaluator-backpressure 与 scheduler non-admission
两个同类短 horizon 失败；把那两个测试放在冻结原版 worktree 上重跑时也复现了，
因此都归类为环境/时序波动，不能当作本次功能回归。脏树时出现的 tracked-files
门禁失败已在 clean-tree 重跑中消失。

## 5. 真实 A/B 对照合同

真实实验应以原版三次为 baseline，新增三次 treatment，顺序串行并在每轮 drain 后关闭 Judge stack。除 checkpoint treatment 外固定：12 题及顺序、3600 秒、CPS32、`uniform`/`least_active`、initial 2/task、Pi timeout 900 秒、模型/thinking、Judge/Lean profile、cache-disabled 要求、profiling 开关和资源上限。治疗 manifest 是 `configs/formal_1h_cps32_profiled_checkpoint.toml`，仅打开：

```toml
[checkpoint]
enabled = true
transfer = true
publish = true
```

每轮需记录 source commit、immutable image ID、manifest closure hash、Judge/Lean capability 结果和 run status；不得用不同服务/账号/端口或运行顺序掩盖合同差异。原版三次不是随机配对，新增三次也只能给方向性证据。

### 主要指标

| 维度 | 指标 | 目标判定 |
|---|---|---|
| 保存覆盖 | `checkpoint_saved`、candidate captured、changed-from-baseline | timeout/cancel 的非空局部候选不再只停留在临时 agent 目录 |
| 可见性 | `checkpoint_handoff`、新 Agent 是否读取 `checkpoint/checkpoint.json`、CPS checkpoint piece | 后续 assignment 能知道候选 hash、最近反馈和已记录 blocker |
| 语义交接 | `completed_work`/`ruled_out`/`next_step` 非空率、message-only blocker 捕获率 | 不把“文件存在”误报为“路线已理解” |
| 质量护栏 | score/12、nAUC、首次证明、每题 proved rate | 不因 checkpoint 把未验证候选直接计分 |
| 成本 | assignment 数、timeout/cancel、slot utilization、checkpoint 写入耗时/bytes、CPS pieces/messages | 判断强制 fsync 和发布的开销是否可接受 |
| 完整性 | hash mismatch、save/publish failure、profiling audit、Judge drain | 任何不完整 run 单独标记，不混入算法结论 |

### 采用门槛

只有同时满足以下条件，才建议保留默认 treatment：

1. timeout/cancel 的 changed candidate capture 和下一 assignment handoff 有可重复的正向证据；
2. 未验证 checkpoint 从未绕过 Judge、score 或 best-candidate promotion；
3. checkpoint 写入失败不会杀死 run，且额外成本相对于可见性收益可接受；
4. 三次真实重复的 score/nAUC 不出现明显回归，或回归能明确归因于可接受的保存成本；
5. 若语义 summary 捕获率很低，不能声称已经实现“准确知道做过什么”，应把 cooperative summary 作为下一项工作。

在真实 A/B 尚未获得授权输入前，当前结论只能是：**保留这条 fail-open、不可计分的 checkpoint 基础设施值得做；是否改善数学得分或是否应默认开启，尚无真实三次 treatment 证据。**

## 6. 运行阻塞与下一步

只读预检显示本机没有 `contextswarm-iclr-mini:latest`、Judge URL/cache-health 环境变量或本机节点配置；根据 workspace contract，不能启动已停止的旧 Test Lab，也不能从 `48.3:29089` 端口推断可用的 Coordinator/Judge。要完成三次真实 treatment，需要用户/部署者提供当次 Judge/Lean 访问边界、匹配节点和可复核的 runtime 输入；收到后先做只读 preflight，再按上面的固定合同串行运行。
