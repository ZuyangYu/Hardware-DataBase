# Checkpoint 分叉/恢复验证（P0-002b）

> 证据：`tests/test_checkpoint_fork_restore.py`（5 项，全部通过）
> 实现：`src/agents/checkpoints/fork.py`（仅使用官方 saver API）
> 结论：P0-003 可以按"attempt namespace + 指针 CAS"设计启用手持；pending writes 必须显式复制。

## 1. 实测行为

| 行为 | 结果 | 证据 |
|---|---|---|
| 同 thread 连续 invoke 持久化历史 | 通过；`get_state` 返回合并后的完整历史 | `test_same_thread_resume_persists_history` |
| `put` 复制 checkpoint 到新 thread/namespace | 通过；返回带新 `checkpoint_id` 的配置，状态与源一致 | `test_checkpoint_copy_is_isolated_from_source_namespace` |
| 复制后的 namespace 用新输入继续 | 通过；新执行在复制历史之上追加，而不是从空历史开始 | `test_copied_namespace_continues_history` |
| pending writes 与 checkpoint 分离 | 通过；`put` 不复制 pending writes，必须显式 `put_writes` | `test_pending_writes_require_explicit_copy` |
| pending write 结构 | 实测为三元组 `(task_id, channel, value)`；`task_path` 不在读回结构内 | 同上 |
| 删除被替代的 attempt namespace | 通过；`delete_thread` 只影响目标 namespace，不影响源与后继 attempt | `test_delete_superseded_attempt_keeps_new_attempt` |
| 复制后写入隔离 | 通过；对副本的继续执行不改变源 thread 的最新状态 | `test_checkpoint_copy_is_isolated_from_source_namespace` |

## 2. 对 P0-003 的约束

1. 接管流程：读取 canonical 指针 → 用 `copy_latest_checkpoint` 把最新 root checkpoint（含 pending writes）复制到 `thread_id::attempt{N+1}` → 在新 namespace 继续执行 → 业务提交后用权威事务 CAS 推进 canonical 指针。
2. 旧 attempt 即使继续写入，也只落在自己的 namespace，不会覆盖 canonical 指针指向的数据；最终回答/事件/指针提交必须校验同一 session generation/token。
3. 取消后下一轮从 committed 指针分叉；被替代的 attempt namespace 由保留期任务清理。
4. 复制的 `checkpoint_id` 由 saver 重新分配；恢复指针保存的是目标 checkpoint 配置，指针永远指向已持久化数据。
5. 不能通过"丢弃 pending writes"简化接管；若后续确认 pending writes 在中断场景可安全忽略，必须单独记录并重新验证。

## 3. 已知限制

- 只复制最新 root checkpoint，不克隆历史修订与嵌套子图 namespace。当前 general-purpose subagent 已禁用，agent 图为单层，该限制可接受；
- P3 启用 Reviewer/Research 子图后，如需接管运行中的子图，必须扩展为按 namespace 列表复制并重新验证；
- 本验证不证明"恰好一次"外部副作用；可能重放的领域写工具必须使用稳定幂等键。
