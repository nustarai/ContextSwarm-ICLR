# MathOlympiadBench 同期方向反馈提示策略：真实配对实验与决策结论

日期：2026-09-05（Asia/Shanghai）

报告修订：activity-feedback-pr-20260907

实验对象：12 道 MathOlympiadBench 数学奥林匹克竞赛题；CPS；每臂 1 小时

文档状态：按 `decision-oriented-experiment-report` 结构整理的最终主报告。本报告的主效果实验冻结在 source commit `7ef68a2c6f7ec53791b7f269d2fd0ebe8c58b827`；后续另行加入的 external-dedup/GPT-6 实验不并入本报告的 A/B 分母或分数。

## 背景和动机

原版运行中，我们观察到多个 Agent 会在同一题上并发探索语义上相近的证明路线。现有证据支持“它们在开始工作时缺少同期方向信息”，但不支持把这种情况描述成 Agent 的私有 reasoning 已被读取或永久丢失。问题的实际影响是：每个 Agent 都可能在独立工作，却无意识地把计算时间、Judge 次数和候选修改投入到同一个核心证明族。

最初曾尝试用完全相同的 `route_key` 做 uniqueness。这个 key 适合做技术层的 claim lease、owner 绑定和原子竞争，但不能回答数学语义问题：不同 key 可能表达同一个证明思路，相同 key 也可能只是实现层句柄碰撞。因此本任务不是让 runner 强行替每个 Agent 分配路线，也不是把所有重复都拒绝掉，而是要回答：

> 当 Agent 在选择方向时，确实看到同题同期 Agent 正在做什么，是否会更少地无理由重复，并把有价值的独立验证保留下来？

由此采用的主指标只统计 admission 时 prompt 中确实列出至少一个同题 peer 方向的 actor-episode。没有 peer 的空 snapshot 不进入分母，因为这些样本没有受到本次反馈信息的影响。语义复核使用四类标签：

- `repeat`：核心证明族、子问题或实验路线实质相同，且没有明确独立贡献；
- `repeat_justified`：有重叠，但声明了独立验证、细化、反例或新 lemma；
- `different`：核心证明族、子问题或实验对象明显不同；
- `uncertain`：信息不足时保留不确定，不强行算作不同。

主报表同时保留两个分母明确的比例：

```text
广义重复率 = (repeat + repeat_justified) / prompt_exposed
严格重复率 = repeat / prompt_exposed
```

这是外部后验语义诊断，不是 Agent 自己填写的“我是否重复”，也不是 runner 的 key 过滤结果。

## 具体改了什么

### 方向信息从技术 key 改成自然语言反馈

Agent 现在需要用一句短的自然语言 `summary` 说明“当前正在做什么或测试什么”。runner 在真实 admission 后维护 active roster，并把当时已经存在的同题 peer summaries 放进后续 Agent 的 task prompt。Agent 仍然自己决定：可以换方向、做互补子问题，也可以在有明确独立理由时继续重复；runner 不替 Agent 选择数学路线。

### route claim 保留为技术协议，而不是语义判定器

`route_claim` 在 CPS 中继续负责 claim lease、owner/actor、episode、TTL、状态、release 和原子技术竞争。相同 `route_key` 的 primary uniqueness 只保证协议层句柄不被无意复用；它不再被当作“数学方向唯一性”或重复率指标。自然语言 summary 才是给 Agent 和后验语义审查使用的方向信息。

### 增加两个可比较的 prompt 策略

- `advisory`：告诉 Agent peer 列表是参考信息，由 Agent 自行判断规避或重复；
- `strong`：明确要求默认优先选择 materially different 的 proof family、subproblem 或 experiment；若仍与 peer 重叠，应说明 independent verification、refinement、counterexample 或 new lemma。

两种模式都保留 Agent 的自主权，strong 只改变默认偏好和解释义务，不做 runner 级语义硬拒绝。

### 未改变的控制边界

- 首次 Judge 前，只有 `cps_active_routes` 和 `cps_claim_route` 可以作为协调入口；
- search、inbox、send、publish 仍受原有 Judge checkpoint gate；
- treatment 下没有有效 claim 的 write/edit 仍被 gate 阻止；
- route/CPS 故障若采用 fail-open，必须留下明确的 `route_claim_bypass_reason`，不能伪装成已经成功 claim；
- actor admission、finish、cancel、timeout、recovery exhausted、solved-by-peer 和 TTL expiry 都要完成生命周期收尾。

这项改动解决的是“已存在的同期信息没有传给 Agent”，不是“在第一批 Agent 产生任何私有 reasoning 之前就收集提案”。后一个更强的目标需要 proposal barrier 或分阶段 admission。

## 具体的实验

### 主实验合同

| 项目 | 设置 |
|---|---|
| 实验问题 | `strong` 相比 `advisory` 是否减少已看到 peer 方向后的语义重复 |
| 任务范围 | 12 道 MathOlympiadBench 题 |
| 运行时长 | 每臂 `3600 s` |
| 重复次数 | 1 个 paired replicate（advisory vs strong） |
| baseline 与 treatment | baseline=`advisory`，treatment=`strong`；两臂都启用 active roster、自然语言 summary 和 route claim，唯一设计变量是 prompt wording |
| 模型 | `openai-codex/gpt-5.6-sol` |
| source 与 image | source=`7ef68a2c6f7ec53791b7f269d2fd0ebe8c58b827`；同一 Docker image digest `sha256:5153ec9f5e56cf7d2d0c5ff960ea6a21ac1733b2f36ce651c9c618a15668ac75` |
| 固定并发条件 | `max_parallel=32`、每题初始 2 个 Agent、每题 2 episodes、相同题目、模型、horizon 与 CPS capacity |
| 真实性 | 两臂均为 real Pi/Judge/Lean/Prover/NuRouter workload；A/B 统计不包含 mock Agent、mock Judge 或 mock evaluator |
| 隔离 | 两臂使用独立 Judge、Prover、worker workspace、CPS SQLite 和 CPU/NUMA 分区；NuRouter 是按设计唯一共享的长期服务 |

主实验运行 ID：

- advisory：`20260904T212201Z-32116cd1`；
- strong：`20260904T212201Z-5a476777`。

两臂均在 horizon 后完成 drain/closeout，runner `rc=0`。本轮按操作要求旁路 quiet-host gate 以便同时启动，因此两臂之间的资源隔离成立，但宿主机其他 workload 没有被完全排除。

### 历史结果的使用方式

下面的历史运行用于说明实验问题如何逐步收敛，不把不同 source、Judge 合同或处理变量的分数合并成一个因果估计：

| 阶段 | 真实运行与结果 | 本报告中的用途 |
|---|---|---|
| 原版 baseline | 3 次 feature-off：`5/12、4/12、5/12`，合计 `14/36` | 原版背景；没有 prompt-exposed 分母 |
| 共享 Judge 的早期 route-claim 尝试 | horizon-credit `5/12、2/12、3/12` | 暴露共享队列/closeout 混杂；不作成绩比较 |
| 顺序、独立 Judge 的 route-claim treatment | final artifact `6/12、6/12、6/12` | 证明真实生命周期和隔离协议；没有 control，不作因果结论 |
| 弱提示词 activity-feedback paired | 3 blocks：control `6/6/6`，advisory `6/6/5` | 证明信息通道工作，但此前没有全量语义重复率 |
| 本报告主实验 | advisory vs strong，1 个 paired replicate | 判断强措辞是否改变已曝光样本的语义路线选择 |

当前分支在主实验之后还新增了一个 `external_dedup_mode=off/enforce` 的 GPT-6 Astra 请求标识实验。它把判断移到 runner/CPS，并改变了模型请求标识和处理机制；该实验没有触发 enforce 决策，单独记录在 [`external_route_dedup_experiment_gpt6_20260905.md`](external_route_dedup_experiment_gpt6_20260905.md)，不与本报告的 natural-language feedback A/B 合并。

## 结论

1. **机制层：已验证可行。** active roster、自然语言 summary、后续 prompt 注入、route claim 生命周期和 write/edit gate 已在真实 workload 中运行闭环。summary 不是 mock 字段，且 runner 没有替 Agent 指定数学路线。

2. **方向选择层：支持采用更强的软提示。** 在真正看到同期方向的样本中，主实验观察到 strong 的语义重复率低于 advisory；同时，剩余重叠中带明确独立验证、细化、反例或 new lemma 理由的比例更高。详细分子、分母和标签见“支撑结论的数据和分析”。这支持“默认先找不同方向，但允许有理由地重复”的产品决策。

3. **结果质量层：尚未证明数学得分或搜索效率提高。** 主实验 strong 最终分数高 1 个 task，但只有一个 replicate，两臂都是 `DEGRADED`；首个 proof 和 score-time AUC 没有形成一致优势。原版、弱提示词 paired 和主实验使用的 source/runtime/健康状态也不完全相同，不能把它们拼成总体收益。

4. **成本与可靠性层：协议开销可以观测，但当前 profiling 与 health 还不够干净。** 两臂均有 Judge 与 prover 错误，profiling 有 dropped fields 和 open spans；因此当前数据适合回答方向选择是否改变，不适合发布无损性能基准。

5. **决策：保留 strong 软策略，不做语义硬过滤。** 下一轮默认候选应是 strong prompt + 自然语言 summary + Agent 自主判断；`route_key` 继续作为技术 lease，不作为数学去重器。只有在多轮干净 paired replication 和盲审语义指标稳定后，才考虑扩大默认启用范围。

## 支撑结论的数据和分析

### 1. 主实验：prompt-exposed 样本的语义重复

这里的分母严格是 admission 时 prompt 中有至少一个同题 peer summary 的 actor-episode。主实验两臂最初同步启动的 32 个 Agent 都是 `0/32` exposure，因此被排除在这张表之外。

本轮共有 `103` 个 prompt-exposed 样本，均完成了语义标注，没有 `uncertain` 样本。

| 后验语义标签 | advisory | strong | strong − advisory |
|---|---:|---:|---:|
| `repeat`（无明确独立理由） | 41/50 = **82.0%** | 13/53 = **24.5%** | **−57.5 pp** |
| `repeat_justified` | 5/50 = 10.0% | 14/53 = 26.4% | +16.4 pp |
| `different` | 4/50 = 8.0% | 26/53 = 49.1% | +41.1 pp |
| **广义重复 = repeat + repeat_justified** | **46/50 = 92.0%** | **27/53 = 50.9%** | **−41.1 pp** |
| 广义重复中有明确理由 | 5/46 = 10.9% | 14/27 = 51.9% | +41.0 pp |

这一个 replicate 的描述性风险比为：广义重复约 `0.554`（相对下降约 `44.6%`），严格重复约 `0.299`（相对下降约 `70.1%`）。这些不是总体因果效应量，因为分母不同、标签是单审阅者的后验语义判断，而且只观察到一轮。

词面相似度只作辅助诊断：与某个 peer 的最大 summary Jaccard `>=0.25` 为 advisory `24/50=48.0%`、strong `8/53=15.1%`；`>=0.50` 两臂均为 `0`。这说明词面相似、技术 key 和数学语义是三个不同层次，不能互相替代。

### 2. 主实验的运行真实性、可见性和协议闭环

| 指标 | advisory | strong | 解释 |
|---|---:|---:|---|
| admitted actors 与 result rows | 86 / 86 | 85 / 85 | 逻辑 Agent 与结果记录 |
| Pi Agent starts | 86 | 85 | 每条 A/B 结果都有真实 Pi 启动 |
| `mocked=false` | 86/86 | 85/85 | mock smoke 未混入主统计 |
| prompt-exposed actor-episodes | 50 | 53 | 主重复率分母 |
| initial 32-agent prompt exposure | 0/32 | 0/32 | 同步 admission race |
| 首次 active-route 查询看到 peer | 57/83 = 68.7% | 59/85 = 69.4% | 可见性诊断，不是重复率 |
| route claim rows | 125 | 119 | 技术 claim 数，不等于数学方向数 |
| summary 非空 | 125/125 | 119/119 | 方向反馈字段真实写入 |
| `independent_verification_reason` 有值 | 99 | 98 | Agent 声明，不是语义金标准 |
| 最终释放 | 125 released | 118 released + 1 done | lifecycle 收尾 |
| active 残留 | 0 | 0 | closeout 后无 active claim |
| route conflict 与 bypass event | 0 / 0 | 0 / 0 | 本轮未触发技术冲突或 fail-open |

最新两臂各有一个重复技术 route handle 的额外行，但没有把它计作语义重复。相反，summary 的后验语义标签能识别“不同 key 但同一证明家族”的情况。这是保留 key 作为技术身份、把语义判断交给 Agent/分析流程的直接依据。

### 3. 数学结果和历史对照

#### 3.1 主实验结果

| 指标 | advisory | strong |
|---|---:|---:|
| final score | **5/12** | **6/12** |
| final status | `DEGRADED` | `DEGRADED` |
| `PROVED` | 5 | 6 |
| `COMPILES_WITH_SORRY` | 7 | 6 |
| messages / pieces | 461 / 69 | 496 / 99 |
| first proof | 124.257 s | 188.239 s |
| 最后一个 `time_to_k_proofs` | 第 5 个：2577.663 s | 第 6 个：3327.662 s |
| normalized score-time AUC | 0.25649859 | 0.25286041 |

strong 的最终分数多 `1/12`，但首个 proof 更晚，AUC 略低；在一个 degraded replicate 中不能把 +1 解读成 prompt 带来的稳定数学收益。

#### 3.2 与原版和弱提示词 paired 的分数对照

| 实验家族 | control 或 baseline | treatment | 合计或差值 | 解释 |
|---|---:|---:|---:|---|
| 原版三次 baseline | `5/12、4/12、5/12` | — | `14/36` | 历史背景，无 peer-feedback 分母 |
| 弱提示词三组 paired | `6/12、6/12、6/12` | advisory `6/12、6/12、5/12` | `18/36` vs `17/36` | 通道已工作，但无稳定得分优势 |
| 主实验单个 paired replicate | advisory `5/12` | strong `6/12` | `+1/12` | 描述性观察，不是因果证明 |
| 顺序隔离 route-claim treatment | — | `6/12、6/12、6/12` | `18/36` | 无 control，只证明真实协议/隔离运行 |

原版 `14/36`、弱提示词 `18/36 vs 17/36`、主实验 `5/12 vs 6/12` 的 source/image/runtime 和健康条件并不完全相同，不能用简单相减得出“改动提高/降低了数学能力”。

### 4. 健康、成本和 profiling

主实验健康状态：

- advisory：Judge probes `1383`，infrastructure errors `17`，solver timeout `68`；
- strong：Judge probes `1544`，infrastructure errors `27`，solver timeout `62`；
- 两臂均为 `DEGRADED`，因此 score、AUC 和 wall-time 只能作描述性结果。

主实验 profiling：

| profiling 项目 | advisory | strong |
|---|---:|---:|
| realness | `real` | `real` |
| profile rows | 89,468 | 91,331 |
| schema/sequence/termination | 有效 | 有效 |
| dropped fields total | 164 | 161 |
| open spans | 20 | 27 |
| audit `ok` | false | false |

作为成本背景，早期三组弱提示词 paired 的聚合 CPS 观测为 control/treatment：SQLite connects `5411/8054`、`cps.write.commit` `2254/5372`、lock-wait total `0.282s/4.378s`。这是另一组 source/runtime 下的本机 workload 观测，不是主实验的开销估计；它提示自然语言/claim 通道会增加 CPS 事务量，后续需要在健康稳定后单独评估。

### 5. 早期同步窗口和 exact-key 限制

历史弱提示词 paired 的初始波次第一次查询看到 peer 为 `24/96=25.0%`，全部后续 admission 合计为 `191/263=72.6%`。主实验进一步显示两臂最初 32 个 Agent 都是 `0/32`。因此当前方案能在 admission 有先后时传递信息，但不能让同时启动的 Agent 看到尚不存在的 summary。

如果必须覆盖第一批 Agent，需要 proposal barrier 或分阶段 admission；不能通过 runner 猜测路线，也不能把技术 key uniqueness 当作替代方案。

### 6. 限制、下一步和成功判据

当前结论的限制：

1. strong-vs-advisory 只有一个 paired replicate；模型采样、题目顺序和宿主时变负载仍可能影响结果。
2. 语义标签是一次有界、单审阅者的后验复核；尚未有预注册规则下的双人盲审和一致性统计。
3. 两臂均 `DEGRADED`，Judge/prover 错误会影响完成数和分数。
4. quiet-host gate 按操作要求旁路；两臂彼此隔离，不等于宿主机完全空闲。
5. profiling 有 dropped fields/open spans，不能作为无损性能基准。
6. `prompt_exposed` 只覆盖真正收到反馈的样本，不能外推到初始 `0/32` 波次。
7. GPT-6 external-dedup 分支改变了 treatment 和模型请求标识，单独报告，不与本报告合并。

下一轮真实实验应：

- 在同一精确 source/image/model/horizon 下再做至少 3 个 paired replicate，并随机化 AB/BA 顺序；
- 保持每臂独立 Judge、Prover、workspace 和 CPS，只共享按合同允许的 NuRouter；
- 预注册语义标注指南，双人盲审并报告 `repeat`、`repeat_justified`、`different` 和一致性；
- 在两臂都记录统一的决策字段，例如 `saw_peer_and_avoided`、`saw_peer_and_repeated_intentionally`、`changed_after_peer`；
- 修复 Judge health、profiling dropped/open-span 和 event-to-final reconciliation 后，再评价 score/AUC/成本；
- 若要评价第一波 Agent，先实现 proposal barrier/分阶段 admission，再重新定义曝光分母；
- solved-by-peer 的内部占位改成明确的 `skipped_before_start`/`cancelled_by_peer` schema，避免以后严格真实性审计出现 mock marker 歧义。

只有在多个 paired replicate 中重复率方向一致下降、`repeat_justified` 没有被误删、score/time/token/CPS 没有系统性恶化，并且 health/profiling 达到预设门槛后，才适合把 strong 策略写成稳定默认收益。

### 7. 证据索引与代码验证

本次主实验的详细报告和脱敏证据：

主实验的原始 run 元数据、逐事件 activity audit、协议 audit、两臂 profiling audit、CPU 与 NUMA 隔离审计，以及历史三组运行报告保存在 owner-only 实验归档中，没有随 PR 分发。为避免把本机路径或原始运行数据写入公共描述，本文保留决策所需的聚合指标、运行 ID 和审计限制；对应逻辑证据 ID 为 `strong-prompt-run-report`、`strong-prompt-activity-audit`、`strong-prompt-protocol-audit`、`strong-prompt-control-profile`、`strong-prompt-treatment-profile`、`strong-prompt-cpu-numa` 和 `activity-feedback-history`。

仓库内可随 PR 复核的相关附录：

- [active route claim 实验记录](active_route_claim_experiment_20260904.md)
- [GPT-6 external-dedup 实验记录](external_route_dedup_experiment_gpt6_20260905.md)

代码与文档验证：

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

本次报告重排和历史文档的 supersede 说明已提交在实验分支；没有修改主 worktree、没有 merge/release/deploy，也没有把 mock smoke 当作真实效果证据。
