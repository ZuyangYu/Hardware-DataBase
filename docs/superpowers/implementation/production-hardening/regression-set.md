# 固定回归集（regression-set）

> 基线：`feature/fix-module-matching` commit `01c807e`（2026-09-15，PR #49 合并）
> 固定口径：同一数据集、同一判分脚本、同一模型配置、同一测试命令。
> 运行入口：`uv run python scripts/production_hardening/regression_runner.py --output docs/superpowers/implementation/production-hardening/baselines/`
> 该脚本会记录 commit、数据集 hash、测试结果与耗时，生成可比较的 `baseline_id`。

## 1. 确定性单元/集成回归（每次合入必须全绿）

| 套件 | 覆盖 |
|---|---|
| `tests/test_document_search_batch.py` | 文档批量检索、并发、取消、证据 |
| `tests/test_conversation_action_executor.py` | Action DAG 依赖、能力、禁止动作 |
| `tests/test_worker_fairness.py` | Worker 公平性与队列 |
| `tests/test_document_authoring_job_store.py` | 文档任务 lease/幂等/重试/死信 |
| `tests/test_chat_attachments.py` | 附件解析、等待 turn、权限 |
| `tests/evaluation/test_hardware_metrics.py` | 领域指标与判分 |
| `tests/test_chat_turn_sequence.py` | turn_sequence 分配/幂等/回填/约束 |
| `tests/test_chat_turn_claim_fencing.py` | 同会话顺序、lease、fencing 拒写、结构化领取结果 |
| `tests/test_waiting_turn_timeout.py` | 附件等待超时、条件迁移、可见状态 |
| `tests/test_chat_queue_deadline.py` | 聊天排队 deadline 超时失败与事件 |
| `tests/test_coordinator_lease.py` | 附件协调单所有者 lease 与接管 |
| `tests/test_worker_roles.py` | Worker 角色路由与队列归属 |
| `tests/test_checkpoint_fork_restore.py` | Checkpoint 分叉/恢复/pending writes |
| `tests/test_agent_checkpoint_attempts.py` | attempt 命名空间与 canonical 指针 CAS |
| `tests/test_resource_governor.py` | 资源配额、预留容量、uncertain |
| `tests/test_governor_recovery.py` | uncertain 升级、证据释放、审计、store 身份 |
| `tests/test_shared_retrieval_executor.py` | 有界提交、取消、上下文、迟到证据拒收 |
| `tests/test_action_parallel_executor.py` | 受控并行、依赖条件、稳定合并 |
| `tests/test_action_receipt_store.py` | 持久化 receipt、复用/重放/待协调、稳定幂等键 |
| `tests/test_recoverable_checkpoint_pointer.py` | 可恢复指针 CAS、观察器过滤、按 ref 分叉 |
| `tests/test_multiprocess_sqlite_contention.py` | 真实多进程竞争：turn 顺序/fencing、协调选主、配额互斥 |
| `tests/test_reviewer_eval_harness.py` | Reviewer 确定性评测 harness（语义冒烟分层，互不影响门禁） |
| `tests/test_migration_validation.py` | 迁移校验：哈希多重集/类型归一/FTS 跳过 |
| `tests/test_admission_control.py` | 入口准入限流与 429 |
| `tests/test_provider_config_guard.py` | 未配置 URL 的 postgres provider 拒绝保存/启动 |
| `tests/test_checkpoint_provider.py` | Checkpoint provider 协议（postgres 需 DSN） |
| `tests/test_subagents.py` | Reviewer/Research（flag gated） |

## 2. 业务数据集（冻结 hash，变更必须升版本）

| 数据集 | 样本数 | 用途 |
|---|---|---|
| `evaluation/datasets/hardware_qa_v1.jsonl` | 25 | 首版硬件问答基线（README 引用；2026-09-15 从既有上传副本还原并 `validate` 通过） |
| `evaluation/datasets/aaa_hard_eval_v1.jsonl` | 47 | 硬件问答（含权限/zero-hit） |
| `evaluation/datasets/attachment_editing_v1.jsonl` | 50 | 附件编辑与对话 |
| `evaluation/datasets/document_generation_v1.jsonl` | 21 | 文档生成与评审（fixture hash 见 manifest） |

判分命令（需要 eval 组与模型配置）：

```bash
uv sync --group eval
uv run hardware-database-eval validate --dataset evaluation/datasets/aaa_hard_eval_v1.jsonl
uv run hardware-database-eval run --dataset evaluation/datasets/aaa_hard_eval_v1.jsonl --output storage/evaluations
```

> 数据集分型：`aaa_hard_eval_v1.jsonl` 是 RAGAS `EvaluationSample` 格式（`validate` 适用）；
> `attachment_editing_v1.jsonl` 与 `document_generation_v1.jsonl` 使用各自的扩展 schema，
> 分别由 `src/evaluation/e2e_runner.py`、`tests/test_document_generation_metrics.py` /
> `test_document_generation_baseline.py` 判分，不能用 RAGAS validate 校验。
> PostgreSQL 迁移分组演练：`uv run python scripts/production_hardening/migrate_sqlite_to_postgres.py --group <g> --dsn ...`
> （2026-09-15 演练：authority 45 表 / attachments 22 表 / pipeline 4 表 / projection 4 表全部计数+哈希一致；
> FTS/trigram 影子表标记 derived_skipped；产物 `postgres-migration-drill-20260915.json`）。
> ADAS_new RAGAS 前置修复（幂等）：`uv run python scripts/production_hardening/prepare_adas_new_eval.py`
> （登记 5 个远端文档的本地部门映射 + 将 EDF 索引进 ADAS_new 电路索引；产物 `adas-new-eval-prep-20260916.json`）。
> RAGAS 小样本判分：`EVAL_MAX_WORKERS=2 LLM_BATCH_MAX_CONCURRENCY=2 uv run hardware-database-eval score ...`
> （n=3、faithfulness/answer_correctness，产物 `ragas-small-sample-adas-new-20260916.json`）。
> 附件编辑端到端基线（真实 API+模型，需 HDB_E2E=1 与运行中的服务）：
> `HDB_E2E=1 uv run hardware-database-e2e --base-url <api> --output storage/evaluations/e2e-current`
> （2026-09-16 实测 50/50 通过，产物 `attachment-e2e-baseline-20260916.json`）。
> 多用户多并发 A/B 对比：`uv run python scripts/production_hardening/concurrency_compare_drill.py`
> （隔离临时库：跨用户并行、跨用户隔离 404、同会话突发顺序、低 RPM 429；产物
> `multi-user-concurrency-ab-20260916.json` / `.md`，ok=true）。
> 故障隔离演练：`uv run python scripts/production_hardening/fault_isolation_drill.py`
> （authoring/parse Worker 被杀后通用聊天仍完成、健康检查 200；RAGFlow 不可达的副本仍可服务通用聊天；
> 产物 `fault-isolation-drill-20260916.json`，ok=true）。
> 双副本 HTTP 接管演练：`uv run python scripts/production_hardening/multi_instance_drill.py`
> （session 跨副本可见、并发 start 单所有者、恰好一条 turn_end；产物 `multi-instance-http-drill-20260915.json`）。
> Reviewer 确定性层离线评测：`uv run python scripts/production_hardening/reviewer_eval.py`
> （fixture `evaluation/fixtures/reviewer_cases.jsonl`，10 例，状态准确率 1.0、四类确定性
> code precision/recall 1.0；语义对比需模型预算，报告中标记未评测）。

## 3. 质量指标口径（Phase 0 冻结）

- Retrieval：Recall / Precision / MRR / Zero-hit / Source Routing Accuracy；
- Answer：Groundedness / Correctness / Citation Coverage / Unsupported Claim Rate；
- Agent：Tool Routing Accuracy / Repeated Tool Call Rate / Step Efficiency / Action Plan Validity；
- Document：Template Compliance / Section Completeness / Evidence Coverage / Numeric Consistency / Release Readiness。

## 4. 门禁

- 权限、会话顺序、取消后有效写入、幂等冲突：确定性测试全部通过，越权与重复发布为 0；
- 质量下降（routing/citation/正确率）：不超过 1 个百分点；Unsupported claim rate 上升不超过 1 个百分点；
- 回归集样本目标 ≥ 200；当前 118 条，缺口在实施过程中补齐（不得用模型生成答案填充参考事实）。
