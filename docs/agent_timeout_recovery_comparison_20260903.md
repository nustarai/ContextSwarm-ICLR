# Agent 超时恢复语义与 1800 秒连续运行对比

**实验日期：** 2026-09-03（报告整理：2026-09-04）
**工作负载：** MathOlympiadBench 的固定 12 题、CPS、1 小时 horizon
**样本数：** 每个 treatment 3 次；表中的 `±` 是样本标准差（`n - 1`），不是置信区间

这份报告记录本次 recovery 语义修改、1800 秒连续运行对照，以及对此前“去掉
timeout recovery 后结果变差”现象的归因。报告只包含聚合后的计数、时间和分数；
prompt、response、candidate、账号、端点和原始 profiling/event 内容保留在操作员的
本地证据目录，没有加入仓库。

## 结论先行

1. **代码语义已经和目标一致。** Pi/Agent 任务超时（`AgentResult.timed_out=True`）
   和 runner 主动取消会结束当前 logical actor，不再对同一个 actor/session 做 outer
   recovery；异常的、非 timeout、非主动取消的进程或调用失败仍然在固定 horizon 内做
   有界 recovery。CPS 释放 slot 后可以接纳新的 assignment，这不是把同一条路径全盘
   否定，也不是对已超时 actor 的 recovery。
2. **1800 秒不中断运行给出了支持预期的结果。** 三次得分为 `7, 7, 5 / 12`，均值
   `6.333 ± 1.155`；旧版 `900 + recovery` 为 `4.667 ± 0.577`，新版 `900、无
   timeout recovery` 为 `3.333 ± 1.155`。nAUC 的均值分别为 `0.322684`、
   `0.231024` 和 `0.164602`。
3. **此前把旧 retry 判定为“几乎没有正向作用”是不完整的。** 旧版 retry 的终态
   `recovery_succeeded` 只表示第二个 Pi 进程最后是否以正常 return code 结束，并不
   表示该进程中途是否提交过并通过过 proof。旧版三次运行有 9 个实时
   `judge_proof_credited`，其中 6 个发生在 attempt 0 timeout、attempt 1 已启动之后。
   新版 900 在 900 秒处结束 actor，确实切掉了这些同一 session 的第二段机会；1800
   连续运行则在不重启进程的情况下恢复了这类机会。
4. 因此这组数据支持的工程方向是：**保留“timeout/cancel 不做 outer recovery”的
   语义，同时给单次 Agent 足够的连续预算（本实验为 1800 秒）**。仅把 timeout 从
   900 秒提前终止、但不增加连续预算（新版 900）不是等价的优化，也不应作为最终
   treatment。样本仍只有 3 次，1800 秒的绝对收益需要更多重复确认。

## 固定实验合同

下表是三组比较中应保持不变的运行合同。旧版和新版 900 的运行是此前已完成的
三次重复；新版 1800 是本次新跑的三次重复。

| 项目 | 固定值或说明 |
| --- | --- |
| 数据集 | MathOlympiadBench，固定 12 题 |
| 外层 horizon | 3600 秒 |
| CPS 并发 | `max_parallel = 32`，`initial_agents_per_task = 2` |
| 分配策略 | `uniform`；任务与 episode 合同不变 |
| 模型 | `openai-codex/gpt-5.6-sol`，thinking `max` |
| seed | `0` |
| Judge/Lean timeout | 300 秒；本实验没有修改 Judge 的 retry 或 timeout |
| allocator decision timeout | 900 秒；这是调度器决定期限，不是 Pi 进程 wall-time ceiling |
| Pi 内层 retry | 配置不变，由 Pi live session 自己处理 provider 请求 |
| outer recovery | 仅异常、非 timeout、非主动取消的进程/调用失败；最多一次，退避计入 horizon |
| 本次 treatment 差异 | Pi 单次连续 wall-time：新版 900 为 900 秒，新版 1800 为 1800 秒 |

新版 1800 的可复现实验 manifest 是
[`configs/formal_1h_cps32_profiled_agent1800.toml`](../configs/formal_1h_cps32_profiled_agent1800.toml)。
它以当前 `formal_1h_bridge.toml` 为基线，并显式写出 CPS32、allocator 900 秒、Judge
契约和 Pi 1800 秒，避免依赖只存在于历史实验 worktree 的配置文件。

## 三组结果

### 聚合指标

| 指标 | 旧版 900 → recovery | 新版 900、timeout 不 recovery | 新版 1800、连续运行 | 1800 − 旧版 | 1800 − 新版 900 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 得分 / 12 | 4.667 ± 0.577 | 3.333 ± 1.155 | **6.333 ± 1.155** | +1.667 | +3.000 |
| nAUC | 0.231024 ± 0.039587 | 0.164602 ± 0.035874 | **0.322684 ± 0.005440** | +0.091660 | +0.158082 |
| raw AUC | 9,980.2 ± 1,710.1 | 7,110.8 ± 1,549.8 | **13,940.0 ± 235.0** | +3,959.7 | +6,829.2 |
| 首次 proof 秒数 | 140.317 ± 40.911 | 123.257 ± 4.279 | 182.904 ± 39.677 | +42.587 | +59.647 |
| assignment 数 | 80.000 ± 3.464 | **137.667 ± 3.512** | 86.667 ± 6.658 | +6.667 | −51.000 |
| adaptive assignment 数 | 48.000 ± 3.464 | **105.667 ± 3.512** | 54.667 ± 6.658 | +6.667 | −51.000 |
| solver timeout 数 | 63.333 ± 1.155 | **126.667 ± 1.155** | 61.333 ± 0.577 | −2.000 | −65.333 |
| solver cancelled 数 | 14.000 ± 3.606 | 10.333 ± 4.041 | 24.000 ± 6.083 | +10.000 | +13.667 |
| outer recovery scheduled 数 | **64.667 ± 0.577** | 3.000 ± 1.732 | 6.000 ± 1.732 | −58.667 | +3.000 |
| outer recovery succeeded 数 | 1.667 ± 0.577 | 0.333 ± 0.577 | 0.000 | −1.667 | −0.333 |
| solver slot utilization | 0.999976 | 0.999942 | **0.999983** | — | — |
| run wall 秒数 | 3,732.6 ± 147.8 | 3,752.2 ± 158.2 | **3,674.7 ± 38.9** | −57.8 | −77.5 |
| CPS pieces 数 | 70.333 ± 3.512 | 55.667 ± 4.619 | **84.667 ± 13.868** | +14.333 | +29.000 |
| CPS messages 数 | 416.000 ± 35.595 | 517.667 ± 37.421 | **575.667 ± 5.859** | +159.667 | +58.000 |
| Judge probe infrastructure error 数 | 20.000 ± 1.732 | 8.333 ± 1.528 | 14.000 ± 1.000 | −6.000 | +5.667 |
| failure observation 数 | 143.000 ± 3.606 | 140.333 ± 5.132 | 92.000 ± 5.568 | −51.000 | −48.333 |

三个 treatment 的 slot utilization 都约等于 1，所以 1800 的收益不能解释成“900
版本有大量空闲 slot 没有利用”。1800 的 assignment 反而少于新版 900；它是在更少的
进程/session 重建下，让单个 Agent 保持更长的连续探索。

### 新版 1800 的逐轮结果

| run | 得分 | nAUC | 首个 proof 秒数 | verified proof 数 | 整场时钟 1800 秒后的 proof 数 | assignment | timeout | abnormal recovery | Judge probe error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `20260903T104820Z-d67daadf` | 7 | 0.328913 | 161.250 | 7 | 1 | 90 | 62 | 4 | 13 |
| `20260903T120650Z-f6ef8a8a` | 7 | 0.318865 | 228.697 | 7 | 2 | 91 | 61 | 7 | 14 |
| `20260903T131006Z-7c6668fd` | 5 | 0.320274 | 158.767 | 5 | 0 | 79 | 61 | 7 | 15 |

“整场时钟 1800 秒后的 proof”只是从整个一小时 run 的起点计时，不能用来判断某个
Agent 是否连续运行了 1800 秒。按每个 Pi 进程自己的启动时钟，三次运行的 17 个
实时 `judge_proof_credited` 中有 11 个是在同一、没有重启过的进程运行超过 900 秒后
产生的，逐轮为 `5/6、4/6、2/5`。

### 逐题结果

下表的“1800 三次得分”按 run 顺序列出；`证明次数`是三次中得到 proof 的次数。

| 题目 | 旧版均值 | 新版 900 均值 | 1800 三次得分 | 1800 证明次数 |
| --- | ---: | ---: | ---: | ---: |
| `imo2024_p1` | 1.000 | 0.333 | 1, 1, 1 | 3/3 |
| `imo2024_p2` | 0.333 | 1.000 | 1, 1, 1 | 3/3 |
| `imo2024_p3` | 0.000 | 0.000 | 0, 0, 0 | 0/3 |
| `imo2024_p5` | 0.000 | 0.000 | 0, 0, 0 | 0/3 |
| `imo2024_p6` | 0.333 | 0.000 | 1, 1, 0 | 2/3 |
| `uk2024_r1_p1` | 1.000 | 0.333 | 1, 1, 1 | 3/3 |
| `uk2024_r1_p2` | 1.000 | 1.000 | 1, 1, 1 | 3/3 |
| `usa2024_p2` | 0.000 | 0.000 | 0, 0, 0 | 0/3 |
| `imo2023_p2_v2` | 0.000 | 0.000 | 0, 0, 0 | 0/3 |
| `imo2023_p3` | 0.000 | 0.000 | 0, 0, 0 | 0/3 |
| `imo2023_p4` | 1.000 | 0.667 | 1, 1, 1 | 3/3 |
| `imo2023_p5` | 0.000 | 0.000 | 1, 1, 0 | 2/3 |

1800 没有牺牲旧版已经稳定的题（`imo2024_p1`、两道 UK 题、`imo2023_p4`），并且
在 `imo2024_p2` 三次全中；它对 `imo2024_p6` 和 `imo2023_p5` 各增加了可见的后段
机会。前三次 1800 的首次 proof 反而比另外两组慢，因此收益主要来自后续覆盖和
持续探索深度，而不是首次成功的偶然提前。

## Proof 与 recovery 的事件级归因

### 计数定义

- `judge_proof_credited`：运行期间 evaluator/Judge 对提交 candidate 的实时、可归因
  proof credit。最终分数仍以 run 的 authoritative final result 为准。
- `recovery_succeeded`：outer recovery 的第二个进程最终以正常 return code 返回。它
  **不是**“第二段执行期间没有产生任何有效结果”的计数。
- 本节只计实时 proof credit，不把 closeout 时重复确认的最终结果当成新的探索事件。

### 观察结果

| 组别 | 实时 proof credit | 其中发生在 timeout recovery 已启动之后 | 解释 |
| --- | ---: | ---: | --- |
| 旧版 900 → recovery（三次合计） | 9 | **6** | attempt 0 timeout 后，attempt 1 仍可能提交并通过 proof；随后进程再 timeout/cancel 不会抹掉已产生的共享状态 |
| 新版 1800 连续（三次合计） | 17 | — | 其中 **11** 个在同一个 Pi 进程连续运行超过 900 秒后产生；没有 proof 前的进程重启 |

旧版的典型序列是：`attempt 0 timed_out` → `recovery started` → `judge_proof_credited`
→ attempt 1 后续结束。因而“旧版 recovery succeeded 只有约 5/194”不能推出“旧 retry
没有正向作用”；它只说明绝大多数第二段进程没有正常结束。新 900 版本切掉了这 6 个
事件对应的连续机会，虽然 scheduler 仍会在释放 slot 后接纳新的 assignment，但这些
新的 assignment 多而浅，不能保留同一 session 内的流式上下文。

### 生命周期语义

```text
Pi timeout / runner intentional cancel
    -> 当前 logical actor terminal
    -> 保留 task 的 best candidate 与 CPS 状态
    -> 释放该 actor 的 slot
    -> CPS scheduler 可接纳新的 assignment（不是同 actor recovery）

异常、非 timeout、非主动取消的进程/调用失败
    -> 在 horizon 仍有预算时
    -> 同 actor / task / episode / session / workspace 有界 outer recovery
```

因此“timeout 不 retry”不会把任务、题目或路径全盘否定；它只禁止对已经超时的
logical actor 做第二次相同的 outer recovery。正在流式生成但尚未持久化的进程内状态在
被杀时仍会丢失，这是 900 终止与 1800 连续运行之间的关键差别。

## 代码变更范围

- [`contextswarm_mini/agent_recovery.py`](../contextswarm_mini/agent_recovery.py)：
  `is_recoverable_agent_failure` 将 `timed_out`、主动取消和 horizon closeout 视为
  terminal；异常非 timeout 仍可在预算内恢复，并对 `subprocess.TimeoutExpired` 做
  明确归类。
- [`contextswarm_mini/runner.py`](../contextswarm_mini/runner.py)：CPS 的 fresh
  assignment/refill 与同 actor outer recovery 分开；timeout/cancel 不进入同 actor
  refill，Judge 的候选 verdict 也不被误判为进程故障。
- [`configs/formal_1h_cps32_profiled_agent1800.toml`](../configs/formal_1h_cps32_profiled_agent1800.toml)：
  提供固定 CPS32、allocator 900 秒、Judge 300 秒和 Pi 1800 秒的可验证 treatment
  manifest。
- `AGENTS.md`、`README.md`：同步 recovery contract 与事件语义，明确 timeout/cancel
  terminal 不等于 task 终止。

## 完整性、健康度与限制

- 三次 1800 run 均正常写出唯一的 `final.json` 与 `run_meta.json`，外层返回码为 0，
  没有 OOM、runner/worker 崩溃或未结算的 Judge job。第二、三轮各有一次
  `Coordinator response failed` 的 solver process error；它们被按异常停止保留在
  recovery 范围内，不与 timeout 混淆。
- 每轮有 13–15 次 Judge probe infrastructure error，因此运行健康标签为
  degraded。该标签与 candidate 分数、proof credit 分开报告；它是外部容量/探针问题
  的限制，不是把分数直接判为无效的依据。
- profiling audit 中仍有已知 dropped-field/open-span 诊断限制；事件顺序和终止校验
  有效，但 profiling 文件不能作为生产负载或资源独占的证明。
- 1800 三次运行是串行的；每轮本地 Judge backend、proxy、runtime、HOME、cache、
  TMPDIR、端口和输出目录隔离。NuRouter/provider capacity 按授权共享，所以不能把
  外部容量称作每轮完全独占。
- 每个 treatment 只有 3 次重复，且随机性和 provider 状态仍可能影响题目级结果。当前
  结论是强方向性证据，不是统计显著性声明；如需发表级估计，应继续做同合同的
  1800 重复，或做“同一新版源码下 900 无 recovery vs 1800 连续”的预注册 A/B。

## Provenance 与验证

本次三轮 1800 使用同一个源码提交
`74998a53e43e62246ce0af2553b7193976e67b39` 和同一个构建镜像摘要
`sha256:3225811d8ee880d8918547b2a9f08859039940ec45c1c730015596b63998af6d`。
历史对照的源码提交分别为旧版
`33296b07634c708412326c2808d5782dab3f788e` 与新版 900
`fefb7644ca10f27541d52434c5e0d1a20428de61`。原始运行 ID 已在逐轮表中列出；原始
profiling/event 证据不进入仓库，仅以脱敏聚合支撑本报告。

交付前在本分支执行以下检查，并在 PR 描述中回填实际结果：

```text
python3 -m compileall -q contextswarm_mini
python3 -m unittest discover -s tests
python3 -m contextswarm_mini.cli --config configs/smoke.toml run --mock-agent
```

这份报告与代码只构成 source/experiment provenance 交付；本次没有部署、修改远端
Coordinator/Judge、导入账号或合并 PR。后续若接受该方向，应单独决定是否把 1800 秒
作为正式默认值，并在更多重复后再做发布层面的判断。
