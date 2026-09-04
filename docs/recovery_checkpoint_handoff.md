# Recovery / checkpoint 交接说明（独立于终止总结实验）

本文件交给后续负责 recover 的 Agent。它不是本次“终止时成果总结与回收发布”实验的实现说明。

## 边界

两条链路必须保持分离：

| 链路 | 目的 | 所有者 | 是否在本次 treatment 开启 |
| --- | --- | --- | --- |
| termination summary | Agent 即将被 timeout/cancel 时，停止当前方向，检查自己的对话，把尚未发布的可复用知识发布到 task-local CPS | 当前任务 | 是 |
| recover/checkpoint | Agent 进程异常退出后，在有限次数内恢复同一 session/workspace，或把私有候选快照交给后续尝试 | 后续 recover Agent | 否 |

终止总结发布的是共享知识（由 Agent 通过 `cps_publish` 写入）；checkpoint 是私有恢复证据（候选文件、session/workspace 元数据）。本次 treatment 不把 checkpoint 注入新 Agent 的 prompt，也不通过 `latest_checkpoint` 选择或传递 Agent 状态。既有 formal CPS 的 best-candidate 复制是独立的 baseline 行为，后续实现不能把它与 checkpoint 混为一谈。

## 当前代码位置

- `contextswarm_mini/agent_recovery.py`
  - `run_with_recovery`：同一个 `actor_id/episode/workdir/session-id` 的有限重启边界。
  - `RecoveryResultSink` / `on_result`：保留的诊断 hook；它不应被用来从 runner 侧猜测或生成语义总结。
  - `is_recoverable_agent_failure`：保持既有进程失败/固定 deadline 判定；不读取
    termination-summary 字段。两条策略必须独立，避免本 treatment 偷改 recover
    次数；后续 recover Agent 再设计显式的摘要后恢复规则。
- `contextswarm_mini/checkpoint.py`
  - `CheckpointStore`：owner-only、不可变、限大小的私有快照存储；快照永远不是 Judge verdict，也不能直接计分。
- `contextswarm_mini/runner.py`
  - `prepare_workspace` 中的 checkpoint materialize/candidate-transfer 分支仍是独立能力，只有 manifest 显式启用 `[checkpoint]` 时才会运行。
  - `_checkpoint_context` 是旧的 runner 侧证据投影；它可能读取 task-local 的已发布 piece/message，但不等价于 Agent 自己的完整对话，不能冒充 termination summary。
- `tests/test_agent_recovery.py`、`tests/test_checkpoint.py`
  - 覆盖同 session/workspace 恢复、快照边界、hash/权限和 fail-open 行为；新增或修改 recover 行为时先保持这些测试的独立语义。

## 现有恢复合同（供后续设计复核）

1. 仅对进程/session 层异常做有限恢复；Judge 返回的 `VERIFY_FAIL`、反例、编译错误等是候选反馈，不应被当成进程恢复。
2. 恢复不延长外层实验 horizon；backoff 和新进程启动都计入原 deadline。
3. 恢复尝试保持同一逻辑 actor/task/episode、Pi session identity 和工作目录。不要把另一个 Agent 的完整对话拼接进来。
4. 私有候选快照必须经过大小限制、SHA-256 和路径/权限校验；未验证候选不能覆盖已验证 best candidate，也不能绕过 `judge_check`。
5. 多个 Agent 并发时，不能用一个模糊的 task-level “最新 checkpoint”推断应该恢复哪一个 Agent。需要明确的 `(task, actor, episode, process-attempt)` 归属和选择规则；若无法确定，应 fail closed 并交给人工/调度策略。
6. 如果整个 runner/Pi 被 `SIGKILL`，或模型从未把隐含 reasoning 写入文件、CPS 或 session 记录，事后无法凭空恢复语义证明。checkpoint 只能保存已经存在的字节和受限元数据。

## 与 termination summary 的交接点

终止总结由 `PiAgent.run` 在硬等待边界前，或在仍存活的 session 报告可识别
provider/assistant error 后，发送同一 RPC session 的 closeout 命令，不是由
`run_with_recovery` 生成：原 turn 仍运行时是 `steer`，已经 `agent_settled` 的 idle
session 使用普通 `prompt` 真正开启总结回合。
总结请求一旦发出，当前 Agent 是“结束中的 Agent”；本任务不改变既有 recover 判定，
因此是否重试仍由原有进程失败/deadline 合同决定。若 closeout command 期间进程崩溃，runner 只能记录
`termination_summary_missing` 和诊断，不应由 runner 读取其他 Agent 的内容来补写一份伪总结。

后续 recover 实现若需要利用总结，应只读取已发布的 task-local CPS piece，并在自己的协议中明确：

- 读取范围是哪些 task/actor/piece，而不是整个对话；
- 哪些 piece 已经被消费、哪些方向明确不要重复；
- 如何重新 `judge_check` 未验证候选；
- 恢复失败、重复 piece、过期 piece 和跨 task 污染如何处理。

这些问题属于 recover 的后续任务，不应反向扩大本次 termination-summary treatment 的范围。

## 建议的后续验证

- 用独立 fake Pi 验证：同 session/workspace 重启、候选快照恢复、恢复次数和 deadline 不漂移。
- 用两个并发 Agent 验证：各自的 checkpoint 不互相覆盖，task-level 查询不会误选另一 actor 的快照。
- 验证 termination summary 不会改写既有恢复次数；若原有 recover 合同决定重试，
  总结 piece 的作者仍必须是产生它的结束中 Agent。
- 把“恢复状态”和“共享知识发布”分别写入独立的 JSONL/指标字段，报告时不要合并成一个 checkpoint 成功率。
