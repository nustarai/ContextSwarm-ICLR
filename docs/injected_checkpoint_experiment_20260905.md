# Astra 注入式中断与 checkpoint 对比记录（2026-09-05）

## 背景和决策问题

本实验要回答的是一个生命周期问题：Agent 在一小时任务尚未完成证明时遇到
timeout、cancel 或 live session error，下一次 assignment 能否拿到已经写入的候选、
已排除的方向和继续位置。数学得分是第二层问题，不能用 checkpoint 文件存在来替代
Judge 证明。

本次实验使用恢复交接分支上的实现，并把正式合同的模型改为
`openai-codex/gpt-6-astra`。由于本 shell 没有注入正式 Judge/cache-health 能力，且本机
已有两条 32-slot Astra formal run 正在使用 provider 容量，本次没有启动第三方或其他
Agent 的运行时，也没有伪造 12 题数学分数。

## 具体变更

- 新增 [`formal_1h_cps32_profiled_gpt6_astra.toml`](../configs/formal_1h_cps32_profiled_gpt6_astra.toml)，作为 checkpoint off 的 baseline。
- 新增 [`formal_1h_cps32_profiled_checkpoint_gpt6_astra.toml`](../configs/formal_1h_cps32_profiled_checkpoint_gpt6_astra.toml)，作为 checkpoint on 的 treatment。
- 两个 manifest 都固定 12 题、3600 秒、CPS32、每题初始 2 个 Agent、Agent/Pi 900 秒超时和 profiling；唯一 treatment surface 是 `checkpoint.enabled/transfer/publish`。
- Astra 字符串采用 Pi/NuRouter 的 provider-qualified 形式 `openai-codex/gpt-6-astra`。两个 manifest 的解析核对均通过。

## 注入式实验

实验在独立的 ZFS evidence 目录运行，使用 deterministic synthetic Pi/evaluator。每个
cause 和 arm 各运行 1 个独立 task attempt；故每个表格单元的分母是 `n=1`，合作式
中断合计是每 arm `n=3`。候选中预先写入一条结构化 `refuted` finding，内容包含
“modular route ruled out”、counterexample 和 next action，用于观察 typed negative
context 是否保持可见。

为避免等待完整一小时，adapter 的实际 wall-clock deadline 是 3 秒，但所有实验元数据
仍保持正式合同的 `logical_horizon_seconds=3600`。这只验证生命周期，不产生 Astra
模型请求；结果文件中的 model 字段用于证明两臂使用同一个预定合同。

注入原因有四类：

1. `timeout`：回调在 timeout closeout 前触发；
2. `cancelled`：回调在 cancellation drain 前触发；
3. `error`：回调在 live RPC/IO error drain 前触发；
4. `external-kill-control`：故意不调用回调的控制组。它模拟“进程没有机会通知
   runner”，但 adapter 仍返回一个失败对象，因此 treatment 还能通过正常 result hook
   做 post-return 保存。这不是把整个 runner 发送 `SIGKILL` 的证明；真正的父进程
   `SIGKILL` 无法由进程内 callback 恢复。

每个 treatment replacement 都检查了四件事：是否读到同一候选、候选 SHA-256 是否匹配、
是否收到 checkpoint continuation prompt、以及 typed ruled-out route 是否仍在 prompt/CPS
上下文中。baseline 也保留了同一共享 CPS finding，因此 route 可见性本身不是 treatment
的独立增益；新增能力是私有候选、hash、terminal reason 和 continuation handoff。

## 结果

原始结果在 [`comparison.json`](/home/ubuntu/workspace/.workspace/builds/CS-20260905-injected-checkpoint/results/comparison.json)，固定合同在 [`experiment_contract.json`](/home/ubuntu/workspace/.workspace/builds/CS-20260905-injected-checkpoint/results/experiment_contract.json)，独立 driver 保存在 [`run_experiment.py`](/home/ubuntu/workspace/.workspace/builds/CS-20260905-injected-checkpoint/run_experiment.py)。

| 注入原因 | arm | pretermination callback/save | checkpoint 总保存 | fresh handoff | replacement 读到候选 | SHA-256 match | continuation prompt | CPS checkpoint pieces |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| timeout | baseline | 0/0 | 0 | 0 | 否 | — | 否 | 0 |
| timeout | treatment | 1/1 | 5 | 1 | 是 | 是 | 是 | 3 |
| cancelled | baseline | 0/0 | 0 | 0 | 否 | — | 否 | 0 |
| cancelled | treatment | 1/1 | 5 | 1 | 是 | 是 | 是 | 3 |
| error | baseline | 0/0 | 0 | 0 | 否 | — | 否 | 0 |
| error | treatment | 1/1 | 5 | 1 | 是 | 是 | 是 | 3 |
| external-kill-control | baseline | 0/0 | 0 | 0 | 否 | — | 否 | 0 |
| external-kill-control | treatment | 0/0 | 4 | 1 | 是 | 是 | 是 | 2 |

在合作式中断的 `3 × 2` 个单元中，baseline 的 callback、pretermination save、fresh
handoff 和 hash-checked candidate coverage 都是 `0/3`；treatment 分别是 `3/3`、
`3/3`、`3/3` 和 `3/3`。四个 treatment 的 replacement 都看到了结构化 ruled-out route，
但该 finding 同时存在于两臂的共享 CPS 中，因此不能把 `4/4` 解释为 checkpoint 独有
的语义采用率。

所有单元的 evaluator 状态都是 `AGENT_FAILURE` 后接 `MOCK_SKIPPED`；没有 Lean verdict、
证明或 score/12。候选内容是 synthetic marker，SHA-256 只证明保存与物化没有改字节。

## 结论和分析

机制层证据支持保留这次改动：在 timeout、cancel 和 live error 三个承诺的 cooperative
边界，treatment 都在 drain 前保存了一次不可变 snapshot，下一 assignment 都读到了同一
候选和 hash，并获得了带 terminal reason、candidate provenance 和 continuation 指令的
prompt。baseline 的相同 assignment 数和失败状态没有这些 handoff。

这次数据不能证明 Astra 的数学能力提升，也不能证明真实 Agent 会减少重复路线。synthetic
adapter 会根据 checkpoint 文件是否存在选择“继续”或“从头开始”，所以它证明的是编排
链路，不是模型的行为因果。共享 CPS 中的 typed negative finding 在两臂都可见，后续应在
真实 run 中记录 replacement 的首次编辑、首次 `judge_check`、重复 route claim 和
`ruled_out` 重复率，不能只看 prompt 文本。

`external-kill-control` 给出了边界：如果失败对象仍能回到 runner，普通 result hook
仍可能捕获候选；如果整个 runner 在回调前被外部 `SIGKILL`，本实现和任何进程内保存器都
不能凭空恢复未写入磁盘/CPS 的内容。这类样本应单独计为 `unavailable`，不能混入
pretermination capture rate。

## 正式 Astra 12 题运行状态

本节原本记录的是 synthetic 实验完成时的历史状态。随后已经完成了一轮真实的 12 题
`gpt-6-astra` baseline/treatment，并在两条 arm 各注入了一个真实 Pi 中断；正式结果、
题目级 verdict、token、slot、健康度和限制统一见
[`injected_checkpoint_real_ab_gpt6_astra_20260905.md`](injected_checkpoint_real_ab_gpt6_astra_20260905.md)。
不要再把本节早先的“正式运行次数为 0”作为当前结论。

## 决策与下一步

当前决策是：保留恢复实现和两份 Astra manifest 作为候选，不能仅凭 synthetic 结果宣布
checkpoint 提升数学性能或默认开启。

正式实验具备运行条件后，应使用相同 source/image、12 题顺序、CPS32、3600 秒、profiling
和 Astra selector，先做 baseline/treatment 配对，再在同一 logical assignment 注入固定
的 timeout/cancel/error。每次注入至少记录：候选是否在中断前写入、pretermination save
和 hash、replacement 是否读取 checkpoint、首次新编辑/`judge_check` 的时间、重复
`ruled_out` route、最终 score/12、nAUC、solver tokens、save/load/publish latency，以及
Judge/broker drain 健康度。建议至少 3 个配对 repeat；父进程真实 `SIGKILL` 作为单独的
不可恢复边界样本统计。
