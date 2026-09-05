# ContextSwarm checkpoint recovery handoff 改进与验证记录（2026-09-05）

## 背景和动机

此前的 checkpoint 实现已经能在 Pi 的 timeout、cancel 或 live error 边界保存候选和有限
CPS/Judge 摘要，但同一 actor 的 replacement process 仍然收到完全相同的初始 prompt。共享
工作区里可能还留有文件，下一进程却不知道上一进程已经做过什么、哪些方向有 blocker、应当
从哪里继续。并行 actor 交错写入一个 task-level `latest_checkpoint` 时，还存在把 peer 的
episode/context 当成当前进程历史的风险。

这次工作的决策问题是：在不把隐藏推理或未经 Judge 验证的内容伪装成证明的前提下，能否让
后续进程准确获得“已有候选、证据边界、排除方向和下一步”，并且在并行任务中保持恢复归属
清晰。参考资料中的完整负面晋升和路线声明实现属于独立实验变量，本次只吸收其中与恢复
交接直接相关的稳定语义。

## 具体改了什么

改动基于 pre-termination checkpoint 提交 `a6521fb`，位于独立 worktree 和分支
`fix/recovery-handoff-20260905`，最终提交的短 hash 以该分支交付时的 HEAD 为准。

- `CheckpointStore.load_latest(...)` 扫描不可变快照目录，按 task、可选 actor、episode
  过滤；metadata 和候选都使用有界、拒绝 symlink 的读取，并重新验证候选 SHA-256。对同一
  scope，优先返回仍存在且相对 baseline 有改动的候选，避免空 closeout 覆盖有效 partial。
  入口见 [`checkpoint.py`](../contextswarm_mini/checkpoint.py#L58) 和
  [`checkpoint.py`](../contextswarm_mini/checkpoint.py#L511)。
- `_ElasticTaskState` 增加 `(actor_id, episode)` 到 checkpoint 的映射。task-level
  `latest_checkpoint` 仍用于新的 assignment 选择同题最佳候选，但同 actor recovery 不再
  使用这个模糊指针。新的 `checkpoint_handoff` 事件记录 donor actor/episode、handoff scope
  和是否同 actor。入口见 [`runner.py`](../contextswarm_mini/runner.py#L993) 和
  [`runner.py`](../contextswarm_mini/runner.py#L5471)。
- recovery attempt 大于 0 时，runner 在调用 Pi 前动态追加 continuation prompt，明确这是
  同一 actor/episode 的替代进程，并写出快照序号、候选 hash、terminal reason、bounded
  completed/ruled-out evidence 和 next step。保存发生在原 workspace 准备之后时，runner
  会先把精确快照 materialize 到 `workdir/checkpoint/`，再启动 replacement。入口见
  [`runner.py`](../contextswarm_mini/runner.py#L2044) 和
  [`runner.py`](../contextswarm_mini/runner.py#L6313)。
- 吸收 negative-piece 资料的最小部分：如果已有 CPS piece/message body 是 JSON，checkpoint
  context 会提取 `status`、`claim`、`evidence`、`preconditions`、`next_action`、
  `source_message_id`、`route_claim_id` 和 relation，并沿用原有清洗和长度限制；
  `refuted`/`superseded` 进入 ruled-out。没有合并完整的 CPS schema、promotion tool 或
  route-claims allocator。
- 吸收 activity-feedback 资料的边界原则：fresh assignment 可以看到 task-level peer
  材料，但 donor provenance 明确；没有加入路线语义硬拒绝，也没有把 active roster 当成
  私有 recovery state。termination summary 仍与 checkpoint 分开，前者发布共享知识，后者
  保存私有恢复证据。

## 具体的实验

| 项目 | 设置 |
| --- | --- |
| 实验问题 | 中断后的 replacement 是否得到同 scope checkpoint、明确 continuation prompt 和可验证候选 |
| 真实题目范围 | 本次没有启动正式 12 题 MathOlympiadBench A/B；使用既有 smoke 题和 deterministic mock adapter |
| 生命周期窗口 | 测试注入 process failure/timeout；mock smoke 使用 `configs/smoke.toml` |
| 重复次数 | checkpoint/recovery/termination 聚焦组 60 tests；mock smoke 1 次；正式数学 A/B 为 0 次 |
| baseline / treatment | 测试同时覆盖 checkpoint off、旧 task-level handoff、pre-termination save 和新的 scoped recovery prompt；同一测试固定其它参数 |
| 模型/Judge | mock adapter；没有真实 Pi、Judge、Lean 或数学 score 证据 |
| 真实性 | synthetic/mock lifecycle validation，不是数学效果实验 |
| 关键固定条件 | 候选文件名、SHA-256、CPS bounded context、`unverified=true`、`score_eligible=false` 和 fresh `judge_check` 边界保持不变 |

正式 12 题实验仍应使用相同题目、模型、Judge/Lean、CPS32、初始并发、agent horizon、总
horizon 和 profiling 合同，至少 3 个随机化 repeat/arm。若自然运行很少产生失败，应在固定
候选编辑点注入 timeout 或 SIGTERM；注入只用于测 recovery 机制，不应把 mock score 当作正式
分数。

## 结论

1. **机制层：** 支持。新的 replacement process 会收到同 actor/episode 的 checkpoint
   continuation prompt；若 callback 在 workspace 准备后才保存，快照也会在下一次 Pi 调用前
   materialize。task-level fresh handoff 与 actor-private recovery 已有清晰身份边界。
2. **结果层：** 尚无数学 score、nAUC 或 Judge proof 提升证据。本次 synthetic/mock 验证只
   证明保存、scope、hash、materialize 和 prompt 链路。
3. **成本与可靠性：** checkpoint enabled 时增加一次有界 ledger scan、hash 校验和
   materialize；checkpoint disabled 的 callback fast path 保持关闭。clean review checkout
   的 recovery 聚焦测试通过；完整 discovery 的 timing 限制单独列出，没有把资源或 Judge
   错误混成数学失败。
4. **决策：** 保留这组 recovery 改动作为候选实现，暂不据此把功能宣布为数学性能提升或
   default-on。是否长期保留，应由固定合同的真实中断对比补足 continuation coverage、重复
   工作和最终 score 数据后决定。

## 支撑结论的数据和分析

### 结果对比

本次没有真实 baseline/treatment 分数表，正式数学 A/B 运行次数为 0。已有 pre-termination
deterministic harness 的历史结果可作为生命周期参照：baseline 的 callback/save/handoff
为 `0/0/0`，treatment 为 `1/1/1`，产生 3 条 checkpoint CPS piece，候选 hash match 为
`true`；该 harness 使用 synthetic evaluator，不能解释数学得分。原始汇总见
[`comparison.json`](/home/ubuntu/workspace/.workspace/builds/CS-20260905-checkpoint-review/pretermination-ab/comparison.json)
和旧说明 [`timeout_checkpoint_pretermination_20260905.md`](timeout_checkpoint_pretermination_20260905.md)。

本分支新增的 scope/recovery 断言覆盖：

| 机制指标 | 观测 | 分母/解释 |
| --- | ---: | --- |
| 聚焦测试 | 60 / 60 passed | checkpoint、recovery、partial CPS、termination summary 四组测试 |
| actor/episode 过滤 | 2 个 actor、2 个 episode | 同 scope 找回自身 changed candidate；peer 快照不混入 |
| 同 actor replacement prompt | 1 个失败 attempt 后的 replacement | prompt 含 `same actor and episode`、上一候选 hash 和 continuation 指令 |
| pre-termination materialize | 1 个注入 timeout 场景 | 下一次 Pi 调用前 `workdir/checkpoint/` 可读且 hash 一致 |
| structured negative handoff | 1 条 `refuted` JSON finding | status/evidence/next_action/source ID 被保存并归入 ruled-out |

这些数字回答的是“链路有没有把状态交给后续进程”，不回答“Agent 是否采纳了方向”或“候选
是否通过 Judge”。CPS piece/message 的发送、可见、ack、采用和证明成功仍是不同身份层，不能
合并成一个 success rate。

### 机制与成本证据

最新提交的 clean review checkout 执行：

```text
python3 -m unittest discover -s tests
Ran 768 tests in 91.189s
FAILED (3 timing-sensitive tests, skipped=1)

python3 -m unittest tests.test_checkpoint tests.test_agent_recovery \
  tests.test_cps_recovery_partial tests.test_termination_summary
Ran 60 tests ... OK

python3 -m compileall -q contextswarm_mini
configs/smoke.toml --mock-agent -> COMPLETED, mock score 0.0
git diff --check -> OK
```

完整 discovery 的 3 个失败都是已有的极短 horizon/timing 断言：evaluator backpressure 等待
   事件在 50ms 窗口内未必产生，另外两个 horizon admission 用例在单独重跑时通过；它们不
   触及本次 checkpoint 路径。测试证据和 clean review checkout 保存在任务专用的 ZFS build root
`/home/ubuntu/workspace/.workspace/builds/CS-20260905-recovery-handoff`，未使用 tmpfs；实现
worktree 干净。新增扫描和 hash 校验只在 checkpoint enabled 的路径发生，未修改 baseline 的
checkpoint-disabled fast path。

### 稳定性、限制与下一步

- checkpoint 只保存候选文件、scalar result、有限 CPS/Judge 摘要和来源标识；隐藏推理、完整
  transcript、未持久化的内存对象和进程已被 `SIGKILL` 直接切断的内容仍不可恢复。
- JSON typed metadata 只有在上游已经提供结构化 body 时才会出现；本分支没有偷偷把普通文本
  猜成 `refuted`，也没有自动晋升旧 message。完整 negative promotion 和 route-claims 仍应
  作为单独 treatment 评估。
- fresh assignment 有意接收 task-level peer candidate；事件里的 donor actor/episode 是
  provenance，不等于该 peer 的 reasoning 已被新 Agent 采用。
- 正式判断建议使用三组可解释 arms：B=`checkpoint off`；C=`checkpoint + actor-scoped
  recovery`、纯文本 CPS；D=在 C 基础上仅对已有 typed JSON 启用结构化 handoff。若运行时不能
  独立关闭 C/D，应事后分层，不要为获得显著结果而改变模型、并发或 horizon。
- 主要指标按 task 和 process-attempt 报告：continuation coverage（同 scope + hash 成功）、
  candidate retention、ruled-out route rework、首次新 `judge_check` 的时间、score/time/cost，
  以及 save/load/publish/receipt failure。只有 operator 提供匹配 Judge/Lean 输入并完成固定
  合同的真实重复实验后，才可以判断该改动是否值得长期保留。
