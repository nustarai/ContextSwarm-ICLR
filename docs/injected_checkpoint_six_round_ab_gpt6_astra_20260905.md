# 超时前强制保存 checkpoint 对 MathOlympiadBench 的六轮 GPT-6-Astra A/B 结果

## 背景和动机

checkpoint 改动的目标是让 Agent 在 timeout、cancel、live RPC error 或进程被终止前已经产生的候选、尝试记录和有限证明上下文能够交给后续 assignment。此前三轮真实 A/B 的 score 差值为 `-2、-4、+2`，说明 treatment 的结果方向不稳定，三轮样本不足以判断这种延续是否改变整体性能。本报告在同一实验合同下补跑 r4–r6，把总配对 replicate 数扩展到六轮，回答两个问题：checkpoint 是否稳定保留局部成果，以及这种保留是否稳定改变最终数学成绩、时间加权成绩或成本。

## 具体改了什么

baseline 关闭 `checkpoint.enabled`、`checkpoint.transfer` 和 `checkpoint.publish`；treatment 打开这三个开关。treatment 在进程边界保存不可变候选快照、SHA-256、终止原因和有限上下文，并在后续 assignment 前提供 handoff/recovery prompt。checkpoint 始终标记为未验证，必须经过新的 Judge 检查才能进入有效证明。共享 CPS 知识发布和 checkpoint 交接仍然是不同机制，本实验没有把 CPS 消息或 checkpoint publication 当作证明采用。

## 具体的实验

六轮都是固定 12 题的配对 A/B；每轮 baseline 和 treatment 同时启动，轮间重新建立独立 Judge/Lean 栈。每个 arm 使用 3600 秒 solver horizon、32 个并发 slot、每题 2 个初始 Agent、每题 2 个 episode、profiling 开启、Judge result cache 关闭和同一任务顺序。精确模型字符串是 `openai-codex/gpt-6-astra`，两边使用同一源码 commit `587543f` 和同一镜像 digest `sha256:4f051f0c…`。r1–r3 使用此前已完成的证据；本批次新增 r4–r6。每轮在启动后约 1800 秒尝试对两边同一 `imo2023_p5` Pi 进程发送 SIGTERM，5 秒内未退出才会升级 SIGKILL；六轮结果中的 r2、r4、r5、r6 均严格命中两边同一题和同一秒，r1 命中题目不一致，r3 treatment 没有活跃目标题进程而按合同不替换题目。

## 结论

1. **局部成果的保存和交接是稳定发生的。** 六轮中 baseline 的 checkpoint save、handoff 和 recovery prompt 全部为零；treatment 每轮都保存 checkpoint，数量为 `444、405、433、412、412、468`，平均 429 次，并产生平均 75.8 次 handoff 和 57.0 次 recovery prompt。这证明进程边界上的局部状态被持久化并能到达后续 assignment，但这些计数不等价于候选被采用或最终得分。
2. **六轮没有显示稳定的成绩提升，也没有复现前三轮的持续负向趋势。** treatment 在六轮中赢 3 轮、输 3 轮，score 差值依次为 `-2、-4、+2、+2、-1、+2`。平均 score 是 baseline `5.33/12`、treatment `5.17/12`，差值 `-0.17` 分；平均 normalized score-time AUC 是 `0.27477` 对 `0.25501`，treatment 低 `0.01976`（约 7.2%）。这组数据支持“checkpoint recovery 目前不是稳定的性能优化”，也不支持“它必然让成绩变差”。结果方向随搜索轨迹变化，和路径依赖及随机性相符；本实验没有直接测量某个旧 checkpoint 是否语义正确，因此不能把每次差异归因为“恢复了错误状态”。
3. **成本增加是比成绩方向更一致的信号。** 六轮平均 solver total tokens 从 1.965M 增至 2.496M，增加 27.0%；input tokens 从 1.576M 增至 2.131M，增加 35.2%。平均 solver slot-seconds 几乎相同（115198.25 对 115197.90），attempt 数反而略少（125.3 对 119.5）。checkpoint 应被定位为可靠性和可恢复性层，而不是默认的性能或成本优化。
4. **建议保留持久化能力，但把“保存状态”和“唯一恢复路线”解耦。** 后续实现应继续保存并传递 checkpoint，同时为 recovery 保留 fresh exploration 分支，把 blocker 当作软证据，限制同一 task-latest 路线连续重复，并记录 checkpoint adoption、首次新编辑、首次新 `judge_check` 和重复路线率。只有在这些指标与最终 proof 建立可追溯关系后，才适合判断是否应改变 recovery 的候选优先级。

## 支撑结论的数据和分析

### 六轮逐轮结果

| replicate | baseline score | treatment score | Δ T−B | baseline nAUC | treatment nAUC | Δ nAUC | 首个 proof（B/T 秒） | solver total tokens（B/T） | checkpoint saved（B/T） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| r1 | 5 | 3 | -2 | 0.21986297 | 0.12814466 | -0.09171831 | 81.20 / 79.70 | 2,947,685 / 3,339,675 | 0 / 444 |
| r2 | 7 | 3 | -4 | 0.25225792 | 0.18629280 | -0.06596512 | 117.01 / 114.33 | 2,262,245 / 2,903,804 | 0 / 405 |
| r3 | 5 | 7 | +2 | 0.33822051 | 0.36642946 | +0.02820895 | 89.05 / 75.90 | 1,460,581 / 2,096,782 | 0 / 433 |
| r4 | 4 | 6 | +2 | 0.21246700 | 0.28529134 | +0.07282434 | 100.54 / 536.38 | 1,393,622 / 1,791,616 | 0 / 412 |
| r5 | 6 | 5 | -1 | 0.30057433 | 0.17251839 | -0.12805594 | 95.24 / 104.16 | 1,356,969 / 2,123,979 | 0 / 412 |
| r6 | 5 | 7 | +2 | 0.32525074 | 0.39137041 | +0.06611967 | 172.46 / 89.36 | 2,369,453 / 2,717,836 | 0 / 468 |
| **六轮平均** | **5.33** | **5.17** | **-0.17** | **0.27477224** | **0.25500784** | **-0.01976440** | **109.25 / 166.64** | **1.965M / 2.496M** | **0 / 429** |

r4 的 treatment 首个 proof 较晚，拉高了六轮 treatment 平均首个 proof；r6 则 treatment 较早，说明“先保留局部状态”并不决定早期 proof 时间方向。六轮 score 的正负差值各出现三次，不能用前三轮的负向均值外推稳定劣化。

### 机制、结果、成本和健康分层

| 指标 | baseline 六轮 | treatment 六轮 | 解释 |
| --- | ---: | ---: | --- |
| checkpoint saved（每轮 B/T） | 0 / 0 / 0 / 0 / 0 / 0 | 444 / 405 / 433 / 412 / 412 / 468 | runner 保存事件，不是证明数 |
| checkpoint handoff（每轮 B/T） | 0 | 92 / 69 / 71 / 66 / 65 / 92 | 后续 assignment 的交接次数 |
| recovery prompt（每轮 B/T） | 0 | 53 / 60 / 58 / 59 / 60 / 52 | 实际注入 continuation prompt 的 assignment 数 |
| checkpoint published（每轮 B/T） | 0 | 222 / 207 / 195 / 198 / 203 / 199 | CPS piece 发布，不等于候选采用 |
| solver attempts 平均 | 125.3 | 119.5 | treatment 低 4.7% |
| solver input tokens 平均 | 1.576M | 2.131M | treatment 高 35.2% |
| solver total tokens 平均 | 1.965M | 2.496M | treatment 高 27.0% |
| solver slot-seconds 平均 | 115198.25 | 115197.90 | 两边基本相同 |

六个 baseline 和六个 treatment arm 的最终状态均为 `DEGRADED`。新 r4–r6 的 Judge probe infrastructure error 计数分别为 baseline/treatment `1/0、0/3、3/3`；没有 OOM 或 exit 137。新三轮 profiling audit 都发现每个 arm 一个未闭合 span；dropped-field 总量为 r4 `1368/1412`、r5 `1348/1416`、r6 `1952/1804`，treatment 另有 unknown event 分别为 `1773、1782、1936`。这些 profiling 问题不改变最终 Judge 分数，但降低了对细粒度生命周期计数的外推强度。

六轮严格中断子集只能取 r2、r4、r5、r6，因为这四轮两边都在同一目标题上同一秒收到 SIGTERM；对应 score 差值为 `-4、+2、-1、+2`，平均 `-0.25`，仍然两正两负。r1 和 r3 的注入不满足同题双边配对，所以没有把它们假装合并到严格中断因果估计中。

新增批次有两次未进入 workload 的 prelaunch 事件：一次是启动编排路径命名检查失败，另一次是一个 Judge 端口已被无关进程占用。两次都在 preflight/启动门禁阶段停止，没有混用现有进程，也没有把失败尝试算进六轮 score；修正后 r4–r6 的六个正式 arm 均真实 preflight `ok`、runner 返回码为 0、Judge 栈已清理。原始日志、逐 arm `final.json`、profiling audit、注入证据和汇总 JSON 由内部 evidence ID `checkpoint-gpt6-astra-real-ab-20260905`、`checkpoint-gpt6-astra-real-ab-r2-r3-20260905`、`checkpoint-gpt6-astra-real-ab-r4-r6-20260905` 和 `six-round-summary.json` 标识，未把机器路径、端口或凭据写入本报告。

样本量是六个配对 replicate，足以显示前三轮与后三轮方向变化，但仍不足以声称统计显著或建立 checkpoint adoption 到最终 proof 的因果链。六轮运行都受到不同程度的 Judge/provider/solver process 健康问题影响，因此最终决策应把 checkpoint 视为可靠性改动，是否改变搜索优先级仍需更干净的固定目标注入实验。
