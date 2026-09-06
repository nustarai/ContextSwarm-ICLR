# 反例与失败路线结构化传播实验报告

**报告修订：** `r4-portable-20260907`。本报告把历史审计、真实模型重复实验和本次代码快照放在同一个可复核的决策记录中；原始 profiling、trace 和运行目录不随 PR 分发。

## 背景和动机

最初的 MathOlympiadBench 原版实验已经显示，Agent 会发现对后续探索有复用价值的反例、经过实际测试并排除的路线和明确的 blocker，但这些负向结论有时只停留在 CPS `message` 中，没有进入持久、可搜索的 `piece`。后续 Agent 使用 `cps_search` 时主要检索 piece，因此可能看不到早已失败的路线并重复尝试。

这里要解决的不是删除 message，也不是把每条 message 自动转换成 piece，而是让已经验证、对整个任务仍有复用价值的负向结论，像正向 lemma 或 strategy 一样进入共享知识流。

历史原版三次运行的分数为 `5/12`、`4/12`、`5/12`，合计 240 个 assignments、211 个 pieces 和 1,248 条 messages。审计还发现 96 个空的 startup digest、3 个经过人工复核但只存在于 message 的可复用负向发现，以及 93 个需要进一步复核的“负向 message 但没有匹配负向 piece”候选；93 是审计队列，不是已确认的遗漏次数。

## 具体改了什么

本改动只增加 prompt 约束和一个可审计的 manifest 开关，复用现有 CPS `cps_publish` 路径，不改变 CPS 存储、评测器、allocator 或消息协议。

| 信息类型 | `cps_send` message | `cps_publish` piece |
| --- | ---: | ---: |
| 即时问题、提醒、请求或状态同步 | 是 | 否 |
| 已充分核验、可被后续复用的结论（正向或负向） | 可选 | 是 |
| 既可复用、又需要当前活跃 Agent 立即处理的结论 | 是 | 是 |

当 `experiment.negative_piece_prompt = true` 时，worker prompt 明确要求：可复现的 counterexample、经过测试并排除的路线、以及可复用的负向 blocker，必须像正向结论一样 publish；piece 应包含 claim、证据或反例、适用范围/前提、后果或下一步，并把猜测标成 tentative。临时协调仍使用 `cps_send`，同时满足即时性和长期复用性的发现使用两个频道。

实现复用已有的字符串字段 `pieces.kind`，建议使用 `counterexample`、`failed_route` 或 `negative_finding`。没有新增数据库表、relation/metadata storage、message-promotion endpoint、外部 reviewer、allocator policy 或 evaluator 行为；默认值为 `false`，所以未开启时历史 prompt 保持不变。

## 具体的实验

实验问题是：在 12 道 MathOlympiadBench 题目上，明确要求 Agent 发布负向结论，是否能提高负向知识的持久化和后续可见性，并观察是否出现解题结果信号。

| 项目 | 固定设置 |
| --- | --- |
| 真实重复 | 3 组 historical-original / direct-child treatment 配对 |
| 单次 horizon | 3,600 秒（1 小时） |
| 任务与容量 | 12 题、32 个 solver slots、seed `0`、uniform allocation |
| 模型 | `openai-codex/gpt-5.6-sol`，`thinking=max` |
| Judge 与传输 | 真实 NuRouter/Pi/LMM/Judge；Judge result cache 关闭 |
| 真实性 | 完成的 Agent 记录均为 `mocked=false`；没有用 mock 或 offline replay 代替真实对照 |
| arm 差异 | 仅 prompt 开关和对应的配置/输出标识；任务、模型、horizon、容量和 Judge contract 固定 |

权威严格对照使用 historical original source commit `33296b07634c708412326c2808d5782dab3f788e`，以及其直接子提交 treatment `468551c5cfb64b96015d89ec1922fe70769caaf7`。两边分别使用 immutable image `sha256:e4f79e6c525fa0e1921e5ab925192ec5203ec3e26ab50aced8924a55eb550bb5` 和 `sha256:ae403637314032fd1f616cca1d771476778d5a1f9be32d3916352d98da6dd5d0`，固定 NuRouter binary SHA-256 为 `cbfb7bb4543f3e4c4840e735f6070c3ea54c4ba811a9e991c485beeacdccc05b`。每个 arm 的 Judge/Prover、CPS store、runtime/home 和会话彼此隔离；公开报告不记录私有 endpoint、端口或本机路径。

此外保留一次确定性 CPS 机制回放作为 wiring 检查：两边收到完全相同的 12 条 messages 和 9 个正向 pieces，treatment 额外通过普通 `cps_publish` 写入 3 个 `negative_finding` pieces。回放只验证数据路径，不连接模型或 Judge，也不用于质量结论。

## 结论

机制结论是明确的：三组严格重复中，historical original 产生 0 个明确负向 piece，treatment 产生 42 个；3 个 treatment arm 都产生负向 piece，3 个 original arm 都没有。改动确实把反例和失败路线带入了普通、可搜索的 piece 流。

解题结果出现积极且重复的 horizon 信号：三组分别为 original `[4, 3, 2]`、treatment `[6, 5, 4]`，每组都是 `+2`，均值从 `3.0/12` 到 `5.0/12`。但所有严格 arm 都是 `DEGRADED`，两个追加 original arm 的 final artifact 还受到收尾恢复介入影响，因此不能把这个信号当作健康条件下的因果 score 提升。

决策是保留这项 prompt 改动作为后续实验候选，而不是现在就默认开启或宣传一般性的解题率提升。下一轮应先消除 Judge/closeout 的 arm 级健康不对称，完成双方健康的重复对照，并直接记录从 exposure 到 retrieval/adoption 的证据。

## 支撑结论的数据和分析

### 严格重复的分数与可比性

| 配对 | original horizon | treatment horizon | original final | treatment final | 解释 |
| --- | ---: | ---: | ---: | ---: | --- |
| `NP-STRICT-R1` | 4/12 | 6/12 | 4/12 | 6/12 | 两边均 `DEGRADED`，可记录但不是健康运行 |
| `NP-STRICT-R2` | 3/12 | 5/12 | 0*/12 | 4/12 | original final 受收尾恢复污染 |
| `NP-STRICT-R3` | 2/12 | 4/12 | 0*/12 | 4/12 | original final 受收尾恢复污染 |

按 horizon 计算，三组 treatment 都胜出且每组差值为 `+2`。按 final 计算的原始数组是 original `[4,0,0]` 对 treatment `[6,4,4]`，只有首组 final pair 可以直接比较；追加两组的 `0*` 不应当作为正常原版分数参与平均。

三组的 normalized score-time AUC 均值为 original `0.1753800933`、treatment `0.2856690133`，差值 `+0.11028892`。AUC 是分数轨迹指标，不能绕过 degraded health、收尾污染和不等 attempts 的限制被解释成速度或质量因果证据。

### 负向知识传播

| 指标（3 个 arm 合计） | historical original | treatment | 解释 |
| --- | ---: | ---: | --- |
| 持久化 pieces | 163 | 159 | 总量相近，主要变化是知识类型和持久性 |
| 明确负向 pieces | 0 | 42 | `failed_route` + `negative_finding`；严格重复中未出现字面 `counterexample` kind |
| 含负向 piece 的 arm | 0/3 | 3/3 | 每个 treatment arm 都有实际负向发布 |
| 含负向 piece 的 task-arm 单元 | 0/36 | 19/36 | 单元计数允许不同 arm 的同题重复出现 |
| `cps_publish` 开始/完成 | 154/154 | 144/144 | 两边普通 publish 路径都闭合 |
| `cps_search` 负向记录后的 exposure starts | 0 | 203 | 涉及 75 个 actor；是机会计数，不等于 adoption |
| solver attempts | 229 | 245 | attempts 不相等，限制 score 因果解释 |

负向 piece 的出现与总 piece 数没有同步大幅增加，支持“表达和持久化方式改变”这一机制解释，而不是简单增加所有输出。`cps_publish` 的完成数与开始数相等，且实现没有引入 promotion message 或额外存储关系。

### 历史审计和较早对照的定位

历史原版三次运行的 `5/4/5` 说明在相同题集和一小时设置下，3 到 5 题的波动本来就存在；严格 original 的 `4/12` 与这一分布一致，不能据此认为之前的 3 到 4 题是误跑了改版代码。

较早的 flag-off/on 配对用于发现 control 不是 historical original 的问题，属于描述性机制探索，不是权威 A/B：它们的 source 不是逐位相同的历史 binary。后续严格记录 `NP-STRICT-R1`、`NP-STRICT-R2`、`NP-STRICT-R3` 才采用 historical original 与 direct-child treatment 的固定比较合同。

### 健康、profiling 和证据边界

三组严格 arm 都保留了 `DEGRADED` 状态；追加 original 的 final artifact 受到收尾恢复介入，treatment arm 的 profiling/closeout 也存在 dropped-field、missing-span 或 Judge probe warning。两边没有因此被改写成 healthy，也没有把 warning 当成 mock 证据；这些问题只说明 score 结论需要更保守。

profiling、token、资源和逐事件 trace 已在运行时做了隐私过滤并保留在任务私有归档中，本 PR 只分发上面的聚合计数、commit/image checksum 和逻辑 evidence ID。未分发的原始证据不能通过本报告中的本机路径或私有 URL 访问。

## 验证

历史 treatment source 已通过 `python3 -m compileall -q contextswarm_mini`、prompt/config focused tests 和两份 mock-agent smoke wiring 检查；mock smoke 只证明 orchestration 和开关传播，不代表 LMM 结果。真实三组严格重复使用 `mocked=false`，并通过普通 CPS publish 完成闭合。

本 PR 另从当前远端 `main` 建立干净 head，再把同一 prompt/config 语义移植到当前 runner；因此 PR head 与历史真实实验 commit 不相同，PR 元数据会把比较角色标记为 `tested-revisions-differ`，并把历史 score 结论标为 stale/需要再次实验。当前 head 的 focused tests、compileall 和 mock smoke 是代码回归证据，不是新的真实模型实验。

## 报告修订记录

| 日期 | 修订 | 内容 | 决策影响 |
| --- | --- | --- | --- |
| 2026-09-04 | `r1-exploratory` | 记录首组 flag-off/on 真实对照和负向 piece 机制 | 发现机制信号，但 control 不是历史原版 |
| 2026-09-05 | `r2-strict` | 改用 historical original 与 direct-child treatment | 形成可复核的严格比较合同 |
| 2026-09-05 | `r3-strict-repeats` | 增加两轮严格重复并补充健康/收尾限制 | horizon 信号重复，但 score 因果仍未确认 |
| 2026-09-07 | `r4-portable-20260907` | 迁移到当前 `main` 的 PR head，统一中文说明并脱敏 | 代码可审查，历史结果标为 stale，需健康重复 |
