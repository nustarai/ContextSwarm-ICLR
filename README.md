# ContextSwarm ICLR Mini

这是一个与上游 `ContextSwarm` 隔离的、只保留 MathOlympiadBench latest12 的研究运行时。当前目录的代码不会修改 sibling 上游仓库。

运行形态固定为同一套 Docker + NuRouter/AISW + Pi backend：

- `mono`：一个 Pi session 顺序处理 12 个任务，作为单体 baseline；
- `parallel`：每个任务一个独立 Pi session，不共享 CPS；
- `cps`：弹性 agent pool；默认每道题先分配 2 个 agent，总并行槽位为 24。agent
  完成后，空闲槽位会继续分配给未完成题目；同题 agent 通过 SQLite WAL/CPS
  context 和 task-local best candidate 合作；
- `cps_direct` / `cps_hybrid`：用于通信机制 ablation。

## 先做本地 smoke

不需要 Docker、NuRouter、Pi 或 Lean 服务即可检查数据和运行闭环：

```bash
python3 -m contextswarm_mini.cli --config configs/smoke.toml validate --json
python3 -m contextswarm_mini.cli --config configs/smoke.toml plan --json
python3 -m contextswarm_mini.cli --config configs/smoke.toml run --mock-agent
# 等价入口：python3 main.py --config configs/smoke.toml run --mock-agent
```

最后一条命令会在 `runs/`（或 `--output` 指定目录）生成：

```text
run_meta.json
transport_preflight.json   # real NuRouter/Lean run
events.jsonl
scoreboard_history.jsonl
final.json
cps.sqlite3              # CPS 模式
communication_trace.jsonl # CPS 事件投影
elastic_assignments.jsonl  # CPS 动态 agent 分配
elastic_scheduler_state.json # CPS 调度器收尾状态
closeout_candidates.json   # 三种模式统一的冻结候选索引与 SHA-256
closeout_candidates/<task>/result.lean # feedback-free 最终评分快照
workers/<task>/result.lean # parallel
workers/<task>/agents/<actor>/result.lean # elastic CPS attempts
workers/<task>/best/result.lean # elastic CPS best candidate
workers/mono/tasks/<task>/result.lean # mono
```

`--mock-agent` 只验证编排和产物，不代表论文分数。

## Docker + NuRouter/AISW Pi

先构建镜像：

```bash
CONTEXTSWARM_MINI_PI_VERSION=0.84.2 scripts/build_image.sh
```

镜像同时固定 Codex compatibility binary（当前默认 `0.148.0`，可用
`CONTEXTSWARM_MINI_CODEX_VERSION` 覆盖）。

默认 manifest 使用标准 provider routing（`fast_mode = false`），这样可以
兼容没有 runtime-policy endpoint 的 NuRouter release；确认 coordinator 的
`/core/v1/runtime-policy` 返回 `allowCodexFastMode=true` 后，再在 operator-local
manifest 中打开 fast mode。

真实运行前需要在宿主机准备：

1. 可在 Linux 容器执行的 NuRouter/AISW release ELF（优先使用 `nurouter`）；
2. NuRouter node/coordinator 配置（默认读取 `~/.nurouter/node.toml`）；
3. 可访问的 MathOlympiadBench Lean router（默认 `http://127.0.0.1:18000`）。

如果使用同机的 `ContextSwarmJudge`，需要启动完整 formal stack（不要只启动
Lean-Eval slice），例如：

```bash
cd /path/to/ContextSwarmJudge
./scripts/start_formal_lean_stack.sh up
```

然后确认 `18000/healthz` 的 `accepted_lean_env_ids` 包含
`formal_matholympiadbench`。

默认运行 CPS：

```bash
scripts/run_docker.sh --config configs/cps.toml
```

正式启动前可以只做 transport 检查（不会启动 Pi session）：

```bash
scripts/run_docker.sh --config configs/cps.toml preflight
```

运行三种 paper-facing cells：

```bash
scripts/run_docker.sh --config configs/mono.toml
scripts/run_docker.sh --config configs/parallel.toml
scripts/run_docker.sh --config configs/cps.toml
```

CPS 的弹性调度字段位于 `[experiment]`：

```toml
max_parallel = 24           # 全局 agent 槽位
initial_agents_per_task = 2 # 每题的初始 agent 数
max_attempts_per_task = 0   # 0 = 直到 horizon；可设有限重试上限
cancel_on_proved = true     # 题目证明后取消同题仍运行的 agent
assignment_policy = "least_active"
```

每次尝试使用独立 workspace，完成后把较强 candidate 合并到
`workers/<task>/best/result.lean`；后续 agent 会先读取该文件和该题的 CPS
pieces/messages。Mono 和 Parallel 仍保持通信关闭、固定 baseline 语义。

Pi transport 设置由共享 `[pi]` / `[pi.retry]` manifest 明确控制，三种模式
不会隐式继承宿主机 `~/.pi`。默认 provider idle timeout 为 600 秒；一次
`agent_end` 只代表底层调用结束，runner 会继续等待 Pi 自动 retry/compaction，
直到收到 `agent_settled` 才结束该 agent。外层 experiment horizon 仍是硬截止。

每个 worker 的实际 Pi settings 写入其私有 `.pi/settings.json`；每次调用的原始
session 进一步隔离在该 worker 的 `.pi/sessions/<session-id>/`，避免 CPS 高并发
反复扫描其他 session，也不为 Mono/Parallel 增加共享通信面。它们位于 run 目录
内，因此容器使用 `--rm` 后仍会保留；`pi_session_index.jsonl` 记录对应相对路径。
session 可能包含完整 prompt、工具输出和 provider 错误，仅用于本地诊断，公开
artifact 或运行摘要前必须审查，不能提交到 Git。

Scaling sweep manifests：

```text
configs/scale_1h_mono.toml
configs/scale_1h_parallel.toml
configs/scale_1h_cps24.toml
configs/scale_1h_cps48.toml
configs/scale_1h_cps96.toml
```

其中 Parallel 保持每题一个 baseline agent；CPS24/48/96 分别从每题 2/4/8
个 agent 起步，总槽位分别为 24/48/96。

用于短 canary 的 180 秒 manifest 已保留在 `configs/3min_*.toml`：

```bash
scripts/run_docker.sh --config configs/3min_mono.toml
scripts/run_docker.sh --config configs/3min_parallel.toml
scripts/run_docker.sh --config configs/3min_cps.toml
```

3 分钟 horizon 只限制 solver 与 CPS 通信：到点后 runner 停止 Pi session、拒绝
新的 CPS 写入，并按各模式定义冻结每题一个候选。随后 Mono、Parallel、CPS
统一进入 feedback-free closeout；此阶段不再改变候选，也不把 Judge 结果反馈给
agent。这样，各模式在 horizon 收口时选中并冻结的候选，不会因为最终 Judge
排队或执行跨过截止点而漏分。

paper-facing manifest 统一将 `[lean].max_concurrent_evaluations` 设为 4；建议同时给
独立 Goedel-Prover Judge 配置至少 4 个 worker。该值应当始终和 Judge worker/
内存容量一起调整。`[lean].timeout_seconds`（默认 300 秒）是 Judge 单个后端
命令的执行预算，不是提交到终态的总 wall time：合法 job lifecycle 还可能包含
queue、冷 REPL header/body 以及 formal finalization。它也不是 solver horizon 或
整个 closeout 的总预算。`[lean].max_lifecycle_seconds`（paper manifest 为 3600）
是客户端防御畸形 receipt 的显式安全上界，不会缩短 Judge 正常公布的预算。

如果 AISW binary 或 node config 不在默认路径：

```bash
CONTEXTSWARM_NUROUTER_BINARY=/path/to/aisw-linux-x86_64 \
CONTEXTSWARM_AISW_NODE_CONFIG=/path/to/node.toml \
CONTEXTSWARM_CODEX_HOME=$HOME/.codex \
scripts/run_docker.sh --config configs/cps.toml
```

脚本通过 `--network host` 让容器访问宿主机上的 AISW coordinator 和 Lean router；AISW binary 与 node config 只读挂载，不会被复制进仓库或镜像。若环境不允许 host network，请把 manifest 中的服务地址改成容器可达地址。

`run_docker.sh` 会同时发现相邻的 `.nurouter-pi-launcher.json`（或旧
`.aisw-pi-launcher.json`），并在容器内重写 `real_pi`/`real_codex` 到镜像内
二进制；不要手工只挂载 ELF，否则 NuRouter 的 owner-only launcher 校验会失败。

Fast-mode 使用单独 manifest，并且必须先通过 transport preflight：

```bash
scripts/run_docker.sh --config configs/cps_fast.toml preflight
```

## CPS 接口

CPS worker 在工作目录中获得 `./context_piece`：

```bash
./context_piece search --query inequality
./context_piece create --kind proof_strategy --title 'route' --body '...'
./context_piece message send --to worker-imo2024_p1-e2 --body '...'
./context_piece message inbox
./context_piece actor list
```

`communication = none` 的 Mono/Parallel workspace 不会创建共享数据库或 helper，避免 baseline 意外获得通信能力。CPS 的实现集中在 `contextswarm_mini/cps.py`，后续可以只替换 policy、ranking 或 digest，而不改 NuRouter/Pi transport。

## Lean evaluator contract

`contextswarm_mini/evaluator.py` 使用 ContextSwarmJudge 的公开 Lean router：

```text
GET  /healthz
POST /api/lean/jobs
GET  /api/lean/jobs/<job_id>?wait_ms=1000
DELETE /api/lean/jobs/<job_id>  # 客户端放弃未收口 job 时取消并对账
```

提交字段包括 candidate `code`、baseline `target_code`、`problem_id`、`lean_env_id`
和 `verification_profile`。客户端优先采用 Judge receipt 公布的 whole-job
`lifecycle_deadline_ms`；兼容旧 Judge 时，会根据 receipt 的 queue deadline 和
formal pipeline 上界保守推导。只有整个合法 lifecycle 加 terminal settlement
窗口都结束后，才会请求取消并再次有界对账；畸形或超过客户端安全上界的
lifecycle receipt 会 fail closed，不会造成无限轮询。客户端主动取消不会被记成普通
`CANCELLED` 零分，而会标记为 degraded evaluator timeout。`final.json` 不会把
`queued` / `running` 当作最终 verdict；无法确认终态会明确记录为
`EVALUATOR_TIMEOUT`。Judge 明确返回的 pre-admission overload 会在 30 秒
admission budget 内有界重试；已经排队后才返回的 terminal、retryable
`rejected_overloaded` 至多重交一次 whole job。结果不明的 socket/proxy 失败不会
盲目重交，以免复制仍在运行的 job。Judge 的 `error_kind`、
`terminal_reason`、queue/execution timing 会保留在安全摘要中，以区分证明错误、
执行超时、资源限制、过载和基础设施故障。只有 canonical `PROVED` / `AC`
verdict 计入分数。

## 数据来源

`benchmarks/matholympiadbench/` 只迁移 latest12 的 `problem.md`、`metadata.json` 和 `baseline/*.lean`。题目源 revision 记录在各任务 metadata 中；原仓库的生产 evaluator 和其它 benchmark 没有被复制。这里的 Lean evaluator 是精简的 HTTP adapter，不会绕过 judge 或改变 theorem contract。
