# Agent 同期方向反馈：最终结论与真实实验数据

日期：2026-09-05（Asia/Shanghai）

实验对象：12 道 MathOlympiadBench 数学奥林匹克题，CPS，1 小时 horizon

文档性质：对最初任务目标、实现协议和各阶段真实实验的最终汇总；本文件不替代各轮原始审计产物。

## 给项目成员的快速导读

### 我们发现了什么问题

原版系统会让多个 Agent 在同一题上并发探索语义上相近的证明路线。它们不是有意制造冗余，而是启动时不知道同期 Agent 已经在做什么；因此即使每个 Agent 都在独立工作，也可能无意识地撞到同一个证明方向。原先尝试用完全相同的 `route_key` 做 uniqueness，但 key 只反映技术字符串，无法判断“不同写法是否其实是同一个数学思路”。本次改动要减少的是这种无意识重复，而不是禁止有价值的独立复核。

### 我们具体改了什么

我们把协调信息从“key 是否相同”改成“当前 Agent 用一句短自然语言说明自己正在做什么”，并由 runner 在真实 admission 后维护 active roster，把当时已经存在的同题 peer summaries 放进后续 Agent 的 prompt。Agent 仍然自己决定换方向、做互补工作，或者在有明确独立验证/细化/反例/new lemma 时继续重复；runner 不替它分配数学路线。`route_claim` 保留为 CPS 中的技术 lease、owner/episode/TTL 和生命周期工具，同时继续提供 route-first gate。最新代码还增加了 `advisory` 与 `strong` 两种 prompt 模式：前者只告知并交给 Agent 判断，后者明确要求默认优先选择不同证明族/子问题，并说明重复理由。

### 我们具体做了什么实验

我们没有用 mock 结果代替真实效果实验，而是分阶段保留了四类证据：先记录原版三次 baseline；再验证 route-claim 的原子竞争、admission 和收尾；随后做三组 feature-off vs advisory 的真实 paired run；最后在相同 source/image、两套相互隔离的 Judge/Prover/worker/CPS 环境中，并发跑了一组 advisory vs strong 的 12 题、每臂 1 小时真实 A/B（只有 NuRouter 按设计共享）。最终主指标只统计 admission 时确实看到 peer 列表的样本，再由后验语义复核判断是否重复。后文因此同时报告协议是否跑通、重复路线是否改变、数学得分是否改变，以及各自的健康/因果限制，避免把这些问题混成一个数字。

## 结论先行

这项任务真正要解决的不是“让 runner 用一个完全相同的 key 把路线去重”，而是：**在 Agent 决定首个探索方向时，把同题同期 Agent 已经声明的、简短的自然语言方向反馈给它，让 Agent 自己判断是否避开、互补或有理由重复。** runner 负责登记、广播和生命周期，不负责替 Agent 分配数学路线。

截至本次最新真实 A/B，可以得出四个层次不同的结论：

1. **协议层已可行。** active roster、自然语言 route summary、route claim、admission/lifecycle 收尾和 write/edit gate 已在真实 Pi/Judge/Lean/Prover workload 中闭环；summary 不是 mock 字段，A/B 两臂都是真实 Agent。
2. **exact-key uniqueness 不是正确的效果指标，也不是可靠的语义去重方案。** route key 只是技术句柄/租约身份；相同 key 可能只代表实现层碰撞，不同 key 也可能指向同一证明家族。
3. **强提示词对“已被告知存在同期方向”的样本，给出了明显的重复下降信号。** 最新一组 paired real run 的后验语义复核为：广义重复从 `46/50=92.0%` 降到 `27/53=50.9%`，无明确独立理由的严格重复从 `41/50=82.0%` 降到 `13/53=24.5%`。这支持“强提示应让 Agent 更倾向于先找不同方向”的判断。
4. **这还不是最终因果证明，也没有证明数学得分提高。** 最新只完成一个 paired replicate，且两臂最终健康状态都是 `DEGRADED`；强提示臂只多得到 `1/12` 个最终证明（`6/12` 对 `5/12`），只能作为描述性观察。应保留强提示策略作为候选默认，继续做多轮干净 paired replication，而不是把这一个分数差写成收益承诺。

因此当前产品/实验决策是：**保留“默认优先选择 materially different 的 proof family/subproblem/experiment；只有明确独立验证、细化、反例或新 lemma 时才重复”的强软约束；不做 runner 级语义硬拒绝；继续让 Agent 自己做最终取舍。** 如果必须让第一批同时启动的 Agent 也互相看到提案，则另行实现 proposal barrier 或分阶段 admission；单靠 prompt 无法让 Agent 看到尚不存在的 peer。

## 1. 最初任务目标与正确指标

### 1.1 原始问题

原版运行中，大量 Agent 并发、同步地探索语义上相近的证明路线。重复不是有意增加冗余，而是 Agent 不知道其他 Agent 已经在做什么。原版 prompt 对所有 Agent 基本通用，具体方向由 Agent 自己从题目和上下文中选择；runner 没有预先给每个 Agent 分配数学路线。

目标不是强行阻止所有重复。一个 Agent 可能在看到同向路线后仍然选择重复，因为它要做独立验证、反例检查或更短证明；这在研究型搜索中是合理的。要解决的是**无意识重复**，并把决策所需的信息补给 Agent。

### 1.2 本任务的协议假设

理想的第一阶段时序是：

```text
读取题目和公开骨架
→ 查询当前 active routes
→ 原子声明自己的自然语言方向
→ 看见冲突/同期方向后，换方向或写明独立理由
→ early judge_check
→ search / inbox / send / publish
→ write / edit candidate
```

这个阶段只承诺“读题后、首次实质性写入前尽早协调”。它不承诺 Agent 在产生任何私有 reasoning 之前就完成协调；后者需要 proposal barrier，是第二阶段设计。

### 1.3 最终采用的效果指标

按照用户要求，分母只包含**admission 时 prompt 明确列出至少一个同题 peer 方向**的 actor-episode。没有 peer 的空 snapshot 从分母中剔除，因为它没有受到本次反馈信息的影响。

对这些曝光样本做后验语义标注：

- `repeat`：核心证明族/子问题/实验路线实质相同，且没有明确独立贡献；
- `repeat_justified`：有重叠，但声明了独立验证、细化、反例或新 lemma；
- `different`：核心证明族、子问题或实验对象明显不同；
- `uncertain`：信息不足时保留不确定，不偷偷算作 `different`。

本报告同时给出两个比例：

```text
广义重复率 = (repeat + repeat_justified) / prompt_exposed
严格重复率 = repeat / prompt_exposed
```

这两个比例是外部后验语义诊断，不是 Agent 自己填写的“我是否重复”，也不是 runner 的 key 过滤结果。严格重复率更接近“看到反馈后仍无明确理由地重复”，广义重复率则保守地把所有同族路线都算作重叠。

## 2. 实现内容与边界

最新强提示策略的代码提交为 `7ef68a2c6f7ec53791b7f269d2fd0ebe8c58b827`，变更仍位于实验专用 worktree/branch，未合并、未发布、未部署到持久 runtime。

| 层面 | 实现行为 | 是否替 Agent 决定路线 |
|---|---|---|
| active roster | runner 只在真实 admission 时注册 actor；未 admission 的未来 Agent 不预写进 active roster | 否 |
| route summary | Agent 提交一句短的自然语言方向描述；同题 active summaries 可注入后续 Agent prompt | 否 |
| route claim | SQLite 原子保存 claim、owner、episode、TTL、状态和独立验证理由；相同技术句柄的 primary 竞争可被检测 | 否，key 只做技术身份 |
| prompt advisory | 告知列表是参考信息，由 Agent 自行判断 | 否 |
| prompt strong | 默认优先选择不同证明族/子问题/实验；重复必须说明具体独立贡献 | 否，只改变偏好 |
| gate | 首次 Judge 前只允许 `cps_active_routes`/`cps_claim_route`；search/inbox/send/publish 仍受 Judge gate；无 lease 的 write/edit 被 treatment gate 阻止 | 否 |
| fail-open | broker/route 故障若允许继续，必须记录明确 `route_claim_bypass_reason` | 否，且可审计 |
| lifecycle | finish、cancel、timeout、recovery exhausted、solved-by-peer、TTL expiry 等路径释放 actor/claim | 否 |

强提示的核心语义是“优先规避同族路线”，不是“禁止重复”。这样既保持 Agent 自主性，也让重复成为一个需要解释的主动选择。

## 3. 实验谱系：哪些结果能不能互相比较

以下阶段必须分开阅读。它们的 source、image、Judge 隔离或处理变量不同，不能把所有分数相加后当成一条因果曲线。

| 阶段 | 真实运行 | 主要数据 | 能回答什么 | 不能回答什么 |
|---|---|---|---|---|
| 原版 baseline | 3 次 feature-off：`5/12、4/12、5/12`，合计 `14/36` | 12 题、1 小时、CPS32；均 `DEGRADED` | 原版成绩和“没有早期方向反馈”的背景 | 新协议是否改善语义重复或得分 |
| route-claim 首次并发尝试 | 3 个 treatment horizon-credit：`5/12、2/12、3/12` | 三次共用一个 32-worker Judge，closeout 不完整，234 actors/272 claims | 共享 Judge 下的容量/失败现象 | 隔离后的成绩 A/B；该样本不作正式效果比较 |
| route-claim 顺序隔离机制运行 | 3 个 treatment final artifact：`6/12、6/12、6/12` | 每轮独立 Judge/CPS/workspace/端口，265 actors/319 claims | 真实生命周期、独立 stack 和 route 协议可闭环 | route claim 相对 control 的因果收益；没有 control |
| 弱提示词 activity-feedback paired | 3 个 control/treatment block：control `6/6/6`，advisory treatment `6/6/5` | 同源 feature-off vs advisory；treatment 375 claims；均 `DEGRADED` | 信息通道确实注入；Agent 仍会出现同族路线 | 语义重复率已下降；此前没有全量盲审分母 |
| 最新强提示词 paired | 1 个 advisory vs strong 真 A/B | 同源、独立 Judge、同一 image/source；prompt-exposed `50` vs `53` | 强措辞的方向性效果 | 最终因果效应、稳定数学收益 |

### 3.1 原版三次基线（用户最初的原始实验）

这三次是最初没有 activity feedback 的真实运行，作为背景基线保存：

| run | score | assignments | pieces | messages | final status |
|---|---:|---:|---:|---:|---|
| `20260901T012227Z-8c90d3f0` | `5/12` | 82 | 67 | 439 | `DEGRADED` |
| `20260902T075657Z-eda06caf` | `4/12` | 76 | 74 | 434 | `DEGRADED` |
| `20260902T090313Z-ecee9c07` | `5/12` | 82 | 70 | 375 | `DEGRADED` |
| **合计** | **14/36** | **240** | **211** | **1248** | 3 次均 `DEGRADED` |

这些运行没有“已被告知同期方向”的 prompt-exposed 分母，因此不能用它们直接计算本任务的新指标。它们证明的是原版存在早期协调缺口，而不是给出语义重复率的金标准。

### 3.2 早期 route-claim 运行为何不作成绩证据

在用户纠正“不要共享 Judge、要隔离运行”之前，曾有三次同步 treatment 使用共享 Judge；其 horizon-credit 为 `5/12、2/12、3/12`。这三次不纳入正式成绩比较，因为三臂共享队列、closeout 有不完整项，且运行之间存在资源混杂。

之后三次严格顺序、独立 Judge stack 的 treatment 最终 artifact 都是 `6/12`。这验证了独立 stack 和 claim lifecycle，但没有 contemporaneous control，不能把 `18/36` 与原版 `14/36` 解读成 route claim 的收益。

### 3.3 弱提示词 activity-feedback paired 的作用和结论

在强提示词之前，曾经完成三组同源 feature-off control vs advisory treatment 的真实 paired run。三组 control 最终分数为 `6/12、6/12、6/12`，advisory treatment 为 `6/12、6/12、5/12`，合计 `18/36` 对 `17/36`；六个 run 都是 `DEGRADED`。这组结果没有稳定数学收益，因此没有把 advisory 作为“已经有效”的结论。

但它证明了信息通道本身真实存在：三组 treatment 共 `375` 条 claim，summary 非空 `375/375`，最终 `371 released + 4 done`，active/blocked 残留为 `0`，`independent_verification_reason` 出现 `307/375` 次；没有 route conflict 或 fail-open bypass。初始同步波次第一次查询看到同题 peer 为 `24/96=25.0%`，全部 admission 合计为 `191/263=72.6%`，说明先后 admission 时能传递信息，但不能覆盖 simultaneity race。

这组历史 paired 的词面复核只在 `1382` 个同题 summary pair 中发现 `1` 个 Jaccard `>=0.50`，没有做今天这种全量 prompt-exposed 语义比例审计。因此它能支持“通道工作、exact key 不等于语义去重”，不能支持“弱提示已经降低重复率”。

## 4. 最新强提示词真实 A/B（主结果）

### 4.1 实验合同

| 项目 | 值 |
|---|---|
| dataset | 12 道 MathOlympiadBench 题 |
| horizon | 每臂 3600 秒 |
| max parallel | 32 |
| initial agents / task | 2 |
| episodes / task | 2 |
| model | `openai-codex/gpt-5.6-sol` |
| source commit | `7ef68a2c6f7ec53791b7f269d2fd0ebe8c58b827` |
| Docker image digest | `sha256:5153ec9f5e56cf7d2d0c5ff960ea6a21ac1733b2f36ce651c9c618a15668ac75` |
| control/advisory run | `20260904T212201Z-32116cd1` |
| treatment/strong run | `20260904T212201Z-5a476777` |
| 唯一设计处理变量 | peer-direction prompt wording：`advisory` vs `strong` |

两臂都启用 active roster、自然语言 summary 和 route claim；两臂都使用独立 Judge/worker/CPS stack。A/B 中没有使用 Mock Agent、Mock Judge 或 Mock evaluator：

| 真实性审计 | advisory | strong |
|---|---:|---:|
| result rows | 86 | 85 |
| Pi agent starts | 86 | 85 |
| `mocked=false` | 86/86 | 85/85 |
| runner return code | 0 | 0 |

单独存在的 mock/contract smoke 只验证编排 plumbing，未进入下面任何实验统计。

### 4.2 用户要求的“被告知”分母

| admission 统计 | advisory | strong |
|---|---:|---:|
| admitted actors | 86 | 85 |
| prompt 中确实有同题 peer 列表（主分母） | **50** | **53** |
| 没有 peer 列表、从主分母剔除 | 36 | 32 |
| 最初同步 32 个 actor 的 prompt exposure | **0/32** | **0/32** |
| 第一次 `cps_active_routes` 查询看到 peer（协议诊断） | 57/83 = 68.7% | 59/85 = 69.4% |

最初同步波次的 `0/32` 是协议的已知 simultaneity race：32 个 Agent 同时 admission 时，彼此都还没有已存在的 summary 可注入。这些样本不应被当作“强 prompt 没有效果”的证据，也不应被漏进重复率分母。

### 4.3 语义重复主指标

| 指标（仅在 prompt-exposed 分母内） | advisory | strong | strong − advisory |
|---|---:|---:|---:|
| `repeat` | 41/50 = 82.0% | 13/53 = 24.5% | **−57.5 pp** |
| `repeat_justified` | 5/50 = 10.0% | 14/53 = 26.4% | +16.4 pp |
| `different` | 4/50 = 8.0% | 26/53 = 49.1% | +41.1 pp |
| **广义重复 = repeat + repeat_justified** | **46/50 = 92.0%** | **27/53 = 50.9%** | **−41.1 pp** |
| 明确理由占广义重复 | 5/46 = 10.9% | 14/27 = 51.9% | +41.0 pp |

若只把这一个 replicate 当作描述性风险比：广义重复约为 advisory 的 `0.554`（相对下降约 `44.6%`），严格重复约为 `0.299`（相对下降约 `70.1%`）。由于分母不同、标注是后验人工判断且只有一个 replicate，这些不是已确认的总体效应量。

### 4.4 词面指标（仅作辅助）

与某个 peer 的最大 summary Jaccard：

| 阈值 | advisory | strong |
|---|---:|---:|
| `>= 0.25` | 24/50 = 48.0% | 8/53 = 15.1% |
| `>= 0.50` | 0/50 | 0/53 |

词面相似度明显低于语义复核，说明不能用 exact key 或 Jaccard 代替数学语义判断。`route_key` 在两臂各出现一个重复技术 handle 行，但没有把它误算成语义重复；这正是 key uniqueness 不能承担主指标的直接证据。

### 4.5 协议闭环数据

| route/actor 审计 | advisory | strong |
|---|---:|---:|
| claim rows | 125 | 119 |
| summary 非空 | 125/125 | 119/119 |
| `independent_verification_reason` 有值 | 99 | 98 |
| 最终 released | 125 | 118 |
| terminal `done` | 0 | 1 |
| active 残留 | 0 | 0 |
| `route_claim_conflict` event | 0 | 0 |
| `route_claim_bypass_reason` event | 0 | 0 |

这组数据说明“把方向写成一句话并反馈给后续 Agent”的链路在真实运行中确实工作；它不说明 Agent 一定会规避，也不说明技术 claim 冲突等价于语义冲突。

### 4.6 数学结果与健康状态

| 结果 | advisory | strong |
|---|---:|---:|
| final score | **5/12** | **6/12** |
| final status | `DEGRADED` | `DEGRADED` |
| `PROVED` | 5 | 6 |
| `COMPILES_WITH_SORRY` | 7 | 6 |
| result rows | 86 | 85 |
| messages | 461 | 496 |
| pieces | 69 | 99 |
| first proof | 124.257 s | 188.239 s |
| last `time_to_k_proofs` entry | 第 5 个：2577.663 s | 第 6 个：3327.662 s |
| normalized score-time AUC | 0.25649859 | 0.25286041 |

健康与基础设施观测：

- advisory：Judge probes `1383`，其中 infrastructure errors `17`；solver timeout `68`；
- strong：Judge probes `1544`，其中 infrastructure errors `27`；solver timeout `62`；
- 两臂都有 Judge/prover 错误，最终均为 `DEGRADED`；
- strong 的 `6/12` 相比 advisory 的 `5/12` 是单轮 +1，但 first proof 更晚、score-time AUC 略低，不能写成“强提示提升了搜索效率”；
- 本轮按照用户要求旁路 quiet-host gate 以便并发启动，CPU/NUMA 保证两臂互不争抢，但宿主机其他 workload 没有被排除。

### 4.7 Profiling 状态

| profiling 审计 | advisory | strong |
|---|---:|---:|
| realness | `real` | `real` |
| profile rows | 89,468 | 91,331 |
| schema/sequence/termination | 有效 | 有效 |
| dropped fields total | 164 | 161 |
| open spans | 20 | 27 |
| audit `ok` | false | false |

profiling 可以用于确认覆盖、阶段和资源轨迹，但由于 dropped fields/open spans，本轮不能被当作无损性能基准。

## 5. 对最初问题的逐项回答

### 5.1 “Agent 在被告知有重复后，重复概率是否真的降低？”

**最新强提示 A/B 给出是的、而且幅度明显的方向性答案。** 在明确看到同期方向的样本里：

- 广义语义重复：`92.0% → 50.9%`，下降 `41.1` 个百分点；
- 无明确独立理由的严格重复：`82.0% → 24.5%`，下降 `57.5` 个百分点；
- 同族重复里带独立验证/细化/反例/new lemma 理由的比例：`10.9% → 51.9%`。

这比“只看相同 key”更接近用户真正关心的影响：Agent 已经知道别人做什么之后，是否仍选择同一核心路线。

### 5.2 “数学得分是否提高？”

**目前不能下这个结论。** 最新一轮 strong 是 `6/12`、advisory 是 `5/12`，但只有一个 replicate，且两臂 `DEGRADED`；此前三组弱提示 paired 的成绩为 control `18/36`、advisory `17/36`，也没有稳定优势。最初原版 `14/36` 与这些运行的 source/image/runtime 不同，不能直接相减。

当前可以说“强提示显著改变了方向选择的描述性分布”，不能说“已经提高最终数学解题能力”。

### 5.3 “方向到底由谁决定？”

仍然是：**通用题目/工作 prompt 提供任务，Agent 自己决定具体方向；runner 只提供同期 Agent 的自然语言活动列表和协议约束。** 强 prompt 改变的是优先级和解释义务，不是把某条路线分配给某个 Agent。Agent 若认为重复有价值，仍可重复，但应给出具体理由。

### 5.4 “exact key uniqueness 是否继续作为主方案？”

**不建议。** key 应保留为：

- claim lease 的技术身份；
- SQLite 原子竞争和幂等操作的句柄；
- 生命周期、TTL、release 和 owner 绑定的索引。

它不应承担“数学语义唯一性”或“重复率”判定。语义分类应交给 Agent 的判断加上外部预注册/盲审的分析流程。

### 5.5 “最开始同时启动的一批怎么办？”

当前实现无法在初始同时 admission 的瞬间注入尚不存在的 peer 方向；最新两臂都是 `0/32` prompt exposure。若该窗口必须覆盖，需要：

1. 第一阶段 proposal barrier：先收集每题短 proposal，再把 bounded proposal 列表发给下一批 Agent；或
2. 分阶段 admission：先启动一小批、收集 summary，再启动下一批。

这属于协议第二阶段，不应通过 runner 猜测或硬编码数学路线来解决。

## 6. 证据边界与残余风险

以下限制决定了为什么本文使用“方向性支持”而不是“已证明因果效果”：

1. 最新 strong-vs-advisory 只有一个 paired replicate；模型采样、题目顺序和宿主时变负载仍可能造成随机差异。
2. 语义标签是一次有界、单审阅者的后验复核；还没有预注册指南下的双人盲审/一致性统计。
3. 两臂都 `DEGRADED`，Judge infrastructure errors 和 solver timeouts 会影响完成数与得分。
4. quiet gate 按用户要求旁路；两臂 CPU/NUMA、Judge、worker、CPS 彼此隔离，但不能声称整台主机无其他 workload。
5. profiling 有 dropped fields 和 open spans；可作诊断，不能作无损性能基线。
6. 同步首批 Agent 的 peer exposure 是 0/32；当前主指标只针对真正被告知的样本，不能外推到首批未曝光 Agent。
7. 运行期没有记录 `route_claim_conflict` 或 bypass；这说明本轮没有技术层冲突/故障样本，不说明语义上没有重复。
8. 早期历史样本包含共享 Judge、顺序 treatment、不同 source/image 等多种合同；它们保留用于复盘实验设计，不用于拼接效果量。

## 7. 最终建议与下一轮验收标准

### 7.1 现在应保留的方案

- 保留 active roster + natural-language summary + route claim lifecycle；
- 将 strong prompt 作为下一轮候选默认策略；
- 保留 Agent 的自主重复权，但要求说明 independent verification/refinement/counterexample/new lemma；
- 不在 runner 中实现语义硬拒绝，不再把 exact key 当作重复率；
- 将“被告知分母、广义重复、严格重复、合理重复占比”作为固定报表字段。

### 7.2 下一轮真实实验必须补齐的条件

- 至少再做 3 个 paired replicate（建议随机化 AB/BA 顺序），同一精确 source/image/model/horizon；
- 每臂独立 Judge、Prover、workspace、CPS 和端口，NuRouter 仅按设计共享；
- Judge health 达到稳定门槛，profiling dropped/open-span 问题单独修复或明确排除性能结论；
- 预先冻结语义标注指南，双人盲审并报告一致性；
- 两臂都记录统一的决策字段，例如 `saw_peer_and_avoided`、`saw_peer_and_repeated_intentionally`、`changed_after_peer`，但不让 runner 替 Agent 判定语义；
- 将 solved-by-peer 的内部占位从 `_mock_result` 改成明确的 `skipped_before_start`/`cancelled_by_peer` schema，避免以后严格真实性审计出现 mock 标记歧义；
- 如果要评价第一波 Agent，先实现 proposal barrier/分阶段 admission，再重新定义曝光分母。

### 7.3 成功判据

下一轮只有在以下数据同时成立时，才适合声称“方案有效”：

1. 多个 paired replicate 中，prompt-exposed 分母内的广义/严格语义重复率方向一致下降；
2. `repeat_justified` 占比上升而不是简单把有价值的独立复核误删；
3. score、score-time AUC、token/CPS overhead 和 first-proof time 没有因协调成本而系统性恶化；
4. Judge health、profiling 和 artifact reconciliation 达到预设质量门槛。

## 8. 可复核产物与验证

### 8.1 本次最新 A/B

详细报告和脱敏机器可读证据位于本次 owner-only build root：

- `STRONG_ACTIVITY_PROMPT_REPORT.md`：本轮完整叙述；
- `evidence/activity-audit.json`：admission、prompt exposure、词面诊断和语义标签汇总；
- `semantic-labels.json`：仅稳定 case id → label，不含 raw summary；
- `evidence/protocol-audit.json`：真实 Agent、actor/claim lifecycle 和 run 级审计；
- `evidence/control-profiling-audit.json`、`evidence/treatment-profiling-audit.json`：profiling 审计；
- `evidence/cpu-numa-partition.json`、`evidence/container-cpu-numa-partition.json`、`evidence/launch-skew.json`：隔离与并发启动证据；
- `evidence/quiet-gate-bypass.txt`：按用户要求并发启动的边界记录。

### 8.2 历史三组弱提示词 paired

完整报告：`REAL_ACTIVITY_FEEDBACK_REPORT.md`。其中明确区分了原版 baseline、共享 Judge 的无效样本、顺序隔离 treatment 和三组 feature-off vs advisory paired 结果；不应把这些层次混合。

### 8.3 代码验证

本次实验结束后的 focused gate：

```text
python3 -m unittest \
  tests.test_cps_feature_config \
  tests.test_activity_feedback \
  tests.test_tool_capability_gates \
  tests.test_runner_route_claims
→ 38 tests OK

python3 -m compileall -q contextswarm_mini
→ passed

git diff --check
→ passed
```

此前完整测试套件为 `820/822 passed, 1 skipped`，剩余两个是宿主负载下的 timing-sensitive failure；它们不被冒充成实验效果证据。

## 最终一句话

**第一阶段“把同期 Agent 正在做什么用短句反馈给其他 Agent”的方向是对的，强提示词在真正收到反馈的样本中已经显示出重复路线下降的明显信号；但当前仍只能批准它作为保留 Agent 自主判断的候选策略，不能把一次 `6/12 对 5/12` 或一次人工复核直接升级为最终数学收益结论。**
