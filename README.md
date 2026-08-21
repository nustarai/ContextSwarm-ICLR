# ContextSwarm ICLR Mini

这是一个与上游 `ContextSwarm` 隔离的、只保留 MathOlympiadBench latest12 的研究运行时。当前目录的代码不会修改 sibling 上游仓库。

运行形态固定为同一套 Docker + NuRouter/AISW + Pi backend：

- `mono`：一个 Pi session 顺序处理 12 个任务，作为单体 baseline；
- `parallel`：每个任务一个独立 Pi session，不共享 CPS；
- `cps`：bounded episodes 重用任务上下文，并通过可替换通信策略写入 SQLite WAL；
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
workers/<task>/result.lean # parallel/CPS
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

用于短 canary 的 180 秒 manifest 已保留在 `configs/3min_*.toml`：

```bash
scripts/run_docker.sh --config configs/3min_mono.toml
scripts/run_docker.sh --config configs/3min_parallel.toml
scripts/run_docker.sh --config configs/3min_cps.toml
```

3 分钟 horizon 到达后，runner 会停止 Pi session，并跳过已经迟到的 Lean
提交/长轮询；这保证 container closeout 不会被 evaluator queue 无限拖延。
`[lean].max_concurrent_evaluations` 默认是 1，与本 canary 的单 Goedel-Prover
worker 对齐；提高它应当和 Judge worker/内存容量一起调整。

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
```

提交字段包括 candidate `code`、baseline `target_code`、`problem_id`、`lean_env_id` 和 `verification_profile`. 只有 canonical `PROVED` / `AC` verdict 计入 `final.json`。

## 数据来源

`benchmarks/matholympiadbench/` 只迁移 latest12 的 `problem.md`、`metadata.json` 和 `baseline/*.lean`。题目源 revision 记录在各任务 metadata 中；原仓库的生产 evaluator 和其它 benchmark 没有被复制。这里的 Lean evaluator 是精简的 HTTP adapter，不会绕过 judge 或改变 theorem contract。
