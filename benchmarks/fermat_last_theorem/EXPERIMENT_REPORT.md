FLT 一小时 CPS 实验复查（2026-09-06）

实验确实跑满一小时，但没有证明 FLT，最终状态为 DEGRADED，得分 0/1。前一次“运行稳定”的反馈仅覆盖最初几分钟，不能代表全程。此次报告依据完整日志、全部提交源代码和单独的最终候选复验。

1. 实际运行和证明进展

- 正式计时：北京时间 20:57:11–21:57:11，收尾完成于 21:57:13。
- 32 个并发 agent 槽位，累计 34 个 session（含 2 次补位）；模型 gpt-5.6-sol，thinking=max。
- 环境为 Lean 4.33.1 + 固定 Mathlib；输入是完整 FLT 的陈述，不提供外部 FLT 仓库的完成证明及证明依赖。
- 619 条 judge_check 记录不是 619 份证明：64 条 COMPILES_WITH_SORRY、116 条 VERIFY_FAIL、408 条 SESSION_PROBE_IN_FLIGHT、28 条 OUT_OF_HORIZON、3 条 TASK_CANCELLED。
- 因此只有 180 条正常候选反馈，其中 91 条使用客户端已有结果缓存；181 条调用获准进入 evaluator（另 1 条最终被取消）。
- 115 个不同候选源代码哈希已从 session 中逐字还原并匹配；20 个不同候选获得过 COMPILES_WITH_SORRY。
- 检查这 20 个候选：17 个主体仍含显式 sorry，另外 3 个是 apply? / intro h; apply?，反馈仍明确 is_valid_no_sorry=false。apply? 只提出带剩余子目标的建议，不能计算为已证明。
- 没有新证明的关键引理、指数特例或 FLT 核心环节。实际成果主要是发现 Mathlib 的 FermatLastTheorem / FermatLastTheoremFor / FermatLastTheoremWith 定义，以及查明若干猜测的定理名称不存在。
- CPS 有 52 条共享 piece、374 条 message；内容主要重复定义、猜测可能存在的完整证明实例，以及报告 Judge 等待。通信数量不是数学进度。

2. 剩余缺口

最终保存的候选尝试从 inferInstance、Fact FermatLastTheorem 或 Nonempty (FermatLastTheoremWith ℕ n) 直接获得整个定理，再把 a>0 转成 a≠0 后套用。它没有构造这些证明。

这相当于“如果库里已经有 FLT 的证明，就使用它”，并没有把 FLT 化简成一个更容易且已经完成大半的数学问题。库中存在定义 FermatLastTheorem : Prop，不等于已有该命题的证明。

保留的候选在原运行中曾得到 VERIFY_FAIL。原运行的最终复验又因配置冲突未完成。此次对冻结的同一份源代码，以 120 秒单命令预算、900 秒生命周期上限单独复验，7.539 秒返回 VERIFY_FAIL，Lean 执行本身 6.491 秒，错误是无法合成 Nonempty (FermatLastTheoremWith ℕ n) 实例。没有命中服务缓存。源代码哈希 b5e87f0b6354ff57bb8b6f7a9061c5d33a6370d479fd9f813f7ab4a22e7801a9。

这次补充复验独立保存在 frozen_candidate_recheck.json，不计入原小时成绩；原 final.json 仍是 DEGRADED。

无法给出有意义的“完成百分之几”或剩余小时数。以关键数学里程碑衡量，仍基本在起点；整个一般情形的核心证明尚未建立。这不意味着 FLT 原理上无法从 Mathlib 继续发展证明，也不能把一次受运行问题影响的零分结果当作模型能力上限。

3. 已定位的运行问题

A. 长搜索占用单并发验证通道（本轮新发现，尚未修复）

客户端允许同时执行的 evaluator 请求只有 1 个。4 个含 #find 的重搜索整单分别耗时 649.131、641.147、638.191、633.855 秒，合计约 42.71 分钟；第 5 个重搜索占用 201.582 秒后在小时结束时取消。五者合计 46.07 分钟，约占一小时的 76.8%。这是实验搜索请求的耗时，不是验证一份完成的 FLT 证明需要 46 分钟。

正常请求的后台执行中位数约 6.54 秒、P95 约 17.57 秒；但返回正常候选反馈的调用，在客户端排队的中位数约 95 秒、P95 约 33.7 分钟。28 个调用到实验结束还未进入 evaluator。

缓存查询也发生在同一客户端 gate 之后，已知结果仍可能排队；缓存命中的调用最长等了 2746 秒。Judge healthz 看不到客户端这层等待，因此“服务队列为 0”不能说明 agent 获得了及时反馈。服务曾扩到 2 个 worker，但客户端单并发 gate 仍限制吞吐。

B. HTTP 空闲超时和 Judge 生命周期不一致（本轮新发现，已复现，尚未修复）

45 次原始 judge_check 工具返回错误，耗时全部约 601 秒，提示 The controlled experiment broker call failed。后端请求却仍在等待或执行，之后同一 session 的重试产生 408 条 SESSION_PROBE_IN_FLIGHT。

run_meta 的 pi_http_idle_timeout_ms=600000。原镜像中的 Pi configureHttpDispatcher 把 headersTimeout/bodyTimeout 设置在全局 fetch dispatcher 上；Judge 扩展使用同一个 fetch，虽然 AbortController 允许等到一小时 deadline，仍会被更短的全局 HTTP timeout 中断。

已在原不可变镜像中做缩短时间的复现：HTTP timeout=50ms、模拟 broker 2500ms 后返回、外层 deadline=10s。原 judge_check 扩展约 1011ms 报同样错误，broker 当时尚未结束、随后正常结束。复现文件 broker_timeout_reproduction.json。缩短复现用于确认机制，不冒充又等待了原始 601 秒。

C. 最终复验配置不相容（本轮新发现，诊断复验已绕开，原配置尚未修复）

FLT config 同时设置 timeout_seconds=3600 与 max_lifecycle_seconds=3600。服务将多阶段执行/重试预算计入整个 job 生命周期，返回 10820 秒；客户端在提交后发现 10820 > 3600，立即取消 job，最终 EVALUATOR_ERROR，health issues 为 closeout_incomplete/evaluator_infrastructure_error。这是确定的配置契约问题，不是证明失败，也不是缓存重新构建。

D. 上游模型服务错误（本轮实际发生，外部可用性问题）

只计 message_end（避免把 message_end/turn_end/retry 三份事件重复相加）：370 次 provider_5xx，涉及 17 个 agent；另有 21 次 other 类模型错误、1 次 rate_limit。大量为上游 overloaded。自动重试消耗了原一小时预算。没有 OOM/exit137 或 runner/worker 崩溃的记录；32 个 slot 的几乎 100% 占用不能等同于 32 个 agent 全程有效推理。

E. 启动调试期已修复的问题

前两轮调试发现客户端将 succeeded+is_valid_with_sorry 以及 failed+verification_failed 误分为 EVALUATOR_ERROR，分别通过 722ee9a、4adae38 修复。报告中的一小时运行使用包含两项修复的 4adae38 镜像，运行期 judge_probe_infrastructure_error_count=0。该指标仍不涵盖工具层断开、客户端排队或上游模型错误，故不能据此称全链路健康。

4. 对后续实验的判断

先修生命周期配置及工具超时/状态恢复；将重搜索与短验证的预算、排队和缓存处理理顺，验收时观察 agent 等待时间而非只看 healthz 服务队列。单纯继续原配置的 32 并发不会提供干净的加速测量。

数学任务需要可检验的中间里程碑/引理或固定的已有依赖。若要比较“原先 11 天缩短为 1 天”，起始数学材料和完成标准必须一致；当前从陈述和 Mathlib 开始攻克整个 FLT，没有测得该加速效果。

证据文件（相对于本实验目录）：

- final.json：原始得分、DEGRADED、收尾错误。
- judge_checks.jsonl：调用计数、缓存、客户端 gate 等待。
- pi_events.jsonl 及 workers/*/agents/*/.pi/sessions/*/*.jsonl：模型错误和工具往返。
- closeout_candidates/fermat_last_theorem/result.lean：冻结的最终候选。
- review/metrics.json：复查统计及五个重请求的后台结束记录。
- review/source_audit.json、review/candidate_sources.json：115 个哈希匹配源代码及编译候选主体。
- review/broker_tool_errors.json：45 次约 601 秒工具错误。
- review/broker_timeout_reproduction.json：原镜像中缩短时间复现。
- review/frozen_candidate_recheck.json：独立真实 Lean 复验及后台错误。
