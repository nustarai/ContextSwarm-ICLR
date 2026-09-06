# Agent 超时 recovery 策略的三轮三条件正式对比

## 背景和动机

原版 Agent 在单次运行达到 900 秒后会 recovery 一次，因此一个 Agent 可能持续占用资源约 1800 秒。历史运行记录显示，这类超时后的第二段运行大多仍然超时。要判断“减少无效超时 recovery”是否真的释放了有效探索能力，本批次同时比较原版、修改后的 900 秒不重试，以及修改后的 1800 秒不重试，并将三种条件各重复三轮。

本报告回答两个问题：第一，修改是否确实只对超时/主动停止取消 recovery、仍保留异常停止 recovery；第二，在相同任务、模型和总 horizon 下，修改后的结果质量、时间效率和运行健康是否优于原版。证据批次标识为 `THREEWAY-R3-20260906`；原始日志和逐事件审计未随本文发布，以下数字来自该批次的脱敏汇总。

## 具体改了什么

本次比较固定任务选择、CPS 容量、seed、Judge 合同、模型和总运行 horizon，只改变 Agent 的超时/recovery 条件：

- `baseline-original900`：原版 source；Agent 单次超时 900 秒，超时 recovery 开启一次。
- `treatment-modified900`：修改版 source；Agent 单次超时 900 秒，超时或主动取消不 recovery，异常停止仍 recovery。
- `treatment-modified1800`：同一修改版 source；Agent 单次超时 1800 秒，超时或主动取消不 recovery，异常停止仍 recovery。

Judge 的 300 秒设置、Judge 自身 retry、候选提交和路径探索逻辑均不属于本次 treatment。`modified900` 与 `modified1800` 还存在 Agent timeout 数值差异，因此两者的结果不能被解释为只隔离了一个变量；它们共同用于判断“删掉无效 recovery”在两个 timeout 边界下的表现。

## 具体的实验

这是 3 轮、每轮 3 个 arm 并发、轮与轮顺序执行的真实 formal MathOlympiadBench 实验，共 9 个独立 arm。每个 arm 使用独立 worktree、容器、Judge/Lean stack、数据库、运行目录和端口；上一轮的三个 broker 全部 drained 后才启动下一轮。

固定合同如下：

- 12 道数学奥林匹克题目；每轮每个 arm 的任务顺序相同。
- 总 horizon 3600 秒；CPS `max_parallel=32`、每题初始 Agent 数 2、episodes 2、seed 0。
- 模型 `gpt-6-astra`，thinking `max`；Judge timeout 300 秒；生命周期上限 4500 秒。
- 结果 cache disabled；每个 arm 的 transport preflight 均为 `ok`，最终 9/9 arm 正常退出，9/9 broker 均确认 drained 且无 unsettled job。
- 这是正式真实运行，不是 mock、replay 或 dry-run。健康状态仍单独报告：候选质量与基础设施健康不合并成一个指标。

## 结论

第一，策略修改按预期生效。原版三轮平均每轮约 53.3 次 recovery scheduling，其中约 52.7 次来自 timeout/cancel；两个修改版三轮均没有把 timeout/cancel 事件排入 recovery，recovery 只对应异常停止，平均分别为 3.7 次和 4.0 次。这直接回答了“是否仍在做无意义的超时重试”：在本批次中，修改版没有这样做。

第二，`modified900` 没有表现出质量改善：三轮平均最终得分 3.33/12，低于原版 4.33/12；normalized score-time AUC 平均 0.1795，也低于原版 0.1966。它虽然平均完成了更多尝试，但更多尝试没有转化成更高得分。

第三，`modified1800` 是本批次更有希望的方向：三轮平均最终得分 4.33/12，与原版持平；normalized score-time AUC 平均 0.2188，高于原版 0.1966；同时三轮 health 均为 healthy，而原版三轮都被标记 degraded。这个结果支持“让 Agent 连续运行到 1800 秒、同时不对 timeout 做 recovery”至少没有在本批次造成质量损失，但 n=3 且轮间波动很大，不能宣称统计显著或普遍提升。

因此当前决策建议是：不要把 `modified900` 作为默认优化直接上线；可以保留 `modified1800` 作为下一轮验证候选，但在做默认切换前应先修复/解释原版反复出现的 Judge probe/process health degradation，并用更多配对重复或预先登记的 seed 检验 1800 秒方向是否稳定。

## 支撑结论的数据和分析

### 逐轮最终结果

| 轮次 | 条件 | 得分 / 12 | normalized AUC | 首次证明时间 (s) | attempts | solver timeout | health | recovery（总 / timeout-or-cancel / abnormal） |
|---|---|---:|---:|---:|---:|---:|---|---:|
| R1 | baseline-original900 | 4 | 0.1865 | 201.1 | 146 | 45 | degraded | 50 / 49 / 1 |
| R1 | treatment-modified900 | 2 | 0.0941 | 296.8 | 174 | 105 | healthy | 2 / 0 / 2 |
| R1 | treatment-modified1800 | 6 | 0.3548 | 69.4 | 172 | 44 | healthy | 3 / 0 / 3 |
| R2 | baseline-original900 | 3 | 0.1280 | 155.2 | 128 | 51 | degraded | 53 / 53 / 0 |
| R2 | treatment-modified900 | 3 | 0.2061 | 168.5 | 180 | 104 | healthy | 3 / 0 / 3 |
| R2 | treatment-modified1800 | 5 | 0.1994 | 276.5 | 140 | 55 | healthy | 0 / 0 / 0 |
| R3 | baseline-original900 | 6 | 0.2752 | 208.6 | 121 | 58 | degraded | 57 / 56 / 1 |
| R3 | treatment-modified900 | 5 | 0.2382 | 300.1 | 163 | 106 | healthy | 6 / 0 / 6 |
| R3 | treatment-modified1800 | 2 | 0.1021 | 212.3 | 103 | 58 | healthy | 9 / 0 / 9 |

`recovery` 的三个数分别是所有 scheduled recovery、其中关联到 timeout/cancel 的数量、以及关联到异常停止的数量；它们不是任务数，也不是证明数。所有 9 个 arm 的 broker closeout 都为 `drained=true, active_handlers=0, remote_unsettled_jobs=0`。

### 三轮聚合（n=3，方向性证据）

| 条件 | score 三轮值，均值 | normalized AUC 三轮值，均值 | attempts 均值 | solver timeout 均值 | healthy 轮数 |
|---|---|---|---:|---:|---:|
| baseline-original900 | 4, 3, 6；**4.33** | 0.1865, 0.1280, 0.2752；**0.1966** | 131.7 | 51.3 | 0/3 |
| treatment-modified900 | 2, 3, 5；**3.33** | 0.0941, 0.2061, 0.2382；**0.1795** | 172.3 | 105.0 | 3/3 |
| treatment-modified1800 | 6, 5, 2；**4.33** | 0.3548, 0.1994, 0.1021；**0.2188** | 138.3 | 52.3 | 3/3 |

得分均值的分母是每轮 12 题，AUC 是同一 3600 秒 horizon 内的时间加权指标。`modified1800` 在 R1/R2 高于原版、在 R3 低于原版；`modified900` 的三轮也不是单调变化。这种轮间波动说明三次重复足以揭示方向和异常，但不足以给出稳定的显著性结论。

### Recovery 语义审计

| 条件 | timeout/cancel recovery 三轮值，均值 | abnormal-only recovery 三轮值，均值 | 解释 |
|---|---:|---:|---|
| baseline-original900 | 49, 53, 56；**52.7** | 1, 0, 1；**0.7** | 原版主要把 Agent timeout 当作 recovery 触发 |
| treatment-modified900 | 0, 0, 0；**0** | 2, 3, 6；**3.7** | timeout/cancel 被直接终止，异常仍可 recovery |
| treatment-modified1800 | 0, 0, 0；**0** | 3, 0, 9；**4.0** | 同样没有 timeout/cancel recovery |

这组事件证据支持修改的控制边界，而不是只凭最终分数推测实现行为。两个修改版所有三轮的 timeout/cancel recovery 都为零，剩余 recovery 均关联到 transport 等异常类别；因此报告中的核心判断只使用“timeout/cancel 为零”这一稳定结论。

### 健康与有效性边界

原版三轮都包含 `judge_probe_infrastructure_error`，R1/R3 还各包含 `solver_process_error`，因此原版的 3/3 degraded 不能被忽略。与此同时，三轮原版仍完成了全部候选评估、没有 OOM，且 broker 都 drained；这意味着它们不是未结算的无效 run，但 baseline 与 treatment 的基础设施健康并不完全对称。修改版三轮均 healthy，transport preflight 九次均通过。因而 `modified1800` 的“得分不低于原版且健康更好”是有决策价值的观察，但不是在完全相同健康状态下得到的无偏因果估计。

还发现一个需要单独标记的证据差异：启动器向 9 个 arm 都请求了 profiling，但只有原版 run 产出了 `profiling.jsonl`；两个修改版的 run metadata 和输出目录都没有 profiling stream。故本报告不使用 profiling 事件作条件间的性能结论，也不能声称三种条件具有完全相同的 profiling 开销。若下一步要做 profiling 归因，应先从同一 profiling-enabled base 重建修改版镜像，再重复配对实验；本批次的 final score、AUC、events、transport preflight 和 broker closeout 仍可作为真实 formal outcome 证据。

### 与更早实验的关系

更早的不同模型或单轮批次曾出现相反排序；它们不能与本批次直接合并成一个均值。它们只说明随机性和环境因素确实重要，因此本报告只把本次固定合同下的三轮结果作为当前决策依据，不声称跨批次显著性。

原始逐事件审计、每个 run 的 `final.json`/`run_meta.json`、transport preflight、Judge closeout 和聚合 JSON 以逻辑证据 ID `THREEWAY-R3-20260906` 关联；本文不嵌入机器路径、私有 endpoint、PID 或凭据。
