# G4 执行包：质量、容量与观测验收

> 依据：后续优化实施方案 v1.0 §9（含 §9.0 预算）、完整实施方案 §4.3/§9.3/§9.4。
> 状态：**配方与模板就绪；执行需目标环境、模型预算与业务标注**。RAGAS 按用户要求暂停执行（仅保留小样本历史结果）。

## 1. 容量测试配方（目标环境）

```bash
# 0) 环境：显式开启强制项并分角色起进程
CONVERSATION_SERIALIZE_SAME_THREAD=true RESOURCE_GOVERNOR_ENABLED=true \
RESOURCE_GOVERNOR_SCOPE=deployment SHARED_RETRIEVAL_EXECUTOR_ENABLED=true \
HDB_API_SPAWN_WORKER=0 uv run hardware-database-server
uv run python -m src.workers.main --role chat &   # x2
uv run python -m src.workers.main --role authoring &
uv run python -m src.workers.main --role parse &

# 1) 预热 5 分钟 + 固定混合负载 30 分钟，重复 3 次
uv run python scripts/production_hardening/load_probe.py --mode turn --ttft \
  --rate <目标到达率> --duration 1800 --warmup 300 --sessions <N> \
  --kb '__general__' --username <u> --password <p> --output run-<i>.json

# 2) 2 倍到达率过载 5 分钟后恢复
uv run python scripts/production_hardening/load_probe.py --mode turn --rate <2x> --duration 300 ...
uv run python scripts/production_hardening/load_probe.py --mode turn --rate <目标> --duration 300 ...

# 3) 健康探测（API 可用性，可与上并行）
uv run python scripts/production_hardening/load_probe.py --mode health --rate 5 --duration 1800 ...
```

混合负载比例（Phase 0 冻结）：60% 普通问答 / 20% 多源检索 / 10% SQL·表格 / 5% 文档生成 / 5% 导出·OCR。
分层口径：同一 session 单 turn、同一 session 多 turn 并发、跨 session 并发分别记录。

### 采集指标

| 类别 | 指标 |
|---|---|
| 延迟 | e2e P50/P95/P99、TTFT P95、同会话 queue wait、Heavy Job queue wait |
| 可靠性 | 提交成功率、接受后完成率、内部失败率、用户取消、主动拒绝（429/503） |
| 资源 | `hdb.resource.active/queue.waiting/uncertain`、LLM/RAG 在途、`hdb.retrieval.executor.*` |
| 正确性 | `hdb.conversation.stale_write_rejected`、重复终态事件/Artifact、越权计数 |

## 2. 发布门槛（相对 + 绝对同时通过）

| 维度 | 门槛 |
|---|---|
| 跨会话 Chat e2e P95 / TTFT P95 | ≤ 1.10 × 冻结基线 B，同时 ≤ 已登记绝对 SLO |
| 同会话后序 turn queue wait | 单独报告（含附件等待），不使用 1.10×B |
| 接受后内部失败率 | ≤1%，且较基线上升 ≤0.2pp |
| 质量 | routing/citation/正确率下降 ≤1pp；Unsupported claim 上升 ≤1pp |
| 零容忍 | 重复 Work Order/Artifact/终态事件、有效 stale write、越权 = 0 |
| 过载 | 准入等待不超 deadline；恢复负载后 5 分钟回到过载前队列深度 |
| Worker 接管 | 同角色接管不晚于 lease 过期后两个轮询周期；仅新 attempt 可提交 |

绝对 SLO 登记模板（`baseline-report.md` §4/§9.4.1）：`profile_id`、环境/模型/版本、并发分层、目标到达率、`chat_e2e_p95_limit_s`、`chat_ttft_p95_limit_s`、`chat_queue_p95_limit_s`、`heavy_queue_limit_s_by_kind`、`accepted_failure_rate_limit`、`llm_timeout_rate_limit`、`rag_timeout_rate_limit`、审批角色与冻结日期。

## 3. 质量集扩充模板（143 → 200+）

缺口 57 条由业务标注，**不得用模型生成答案填充参考事实**。每条 JSONL 字段（与 `EvaluationSample` 对齐）：

```json
{"id": "<唯一ID>", "question": "<中文/英文问题>", "reference_answer": "<人工确认答案>",
 "reference_contexts": ["<来源片段或定位>"], "kb_name": "<KB>", "tags": ["<类别>"],
 "metrics": [], "required_evidence_types": [], "rubric": {"required_facts": [], "forbidden_claims": []},
 "request_context": {"user_id": "eval_dept_<n>", "department_id": <n>, "roles": ["employee"],
   "allowed_kbs": ["<n>:<KB>"], "kb_permissions": {"<n>:<KB>": "read"}},
 "critical": false, "expected_access": "allowed"}
```

覆盖要求：普通问答、多源检索与 zero-hit、来源路由降级、Spreadsheet/SQL、Circuit/BOM、附件（解析中/失败/权限变化）、文档生成（修订/取消/重试/Gate）、Action routing（依赖环/未解决引用）、Citation/Numeric/Unsupported。

## 4. 质量执行命令（需 eval 组与模型预算）

```bash
uv sync --group eval
uv run hardware-database-eval validate --dataset evaluation/datasets/hardware_qa_v1.jsonl
# 全量 25×6×3（当前判分模型约 3.6 分钟/指标项，单轮 ~9h，三轮 ~27h；需预算窗口）
uv run hardware-database-eval run --dataset evaluation/datasets/hardware_qa_v1.jsonl --output storage/evaluations
uv run hardware-database-eval score --dataset <ds> --snapshot <snap> --output storage/evaluations ...
```

已归档的质量证据（历史）：
- 附件编辑 e2e 50/50（`attachment-e2e-baseline-20260916.json`）、ADAS_new 小样本 RAGAS（`ragas-small-sample-adas-new-20260916.json`）、Reviewer 确定性 1.0（`reviewer-eval-20260915.json`）；
- **确定性规则层（无裁判模型、可复现）**：对 `hardware_qa_v1` 25 条历史快照（`storage/evaluations/20260916T025402Z-8f3cbdec/snapshot.jsonl`）执行 `hardware-database-eval score --metric none`，`evidence_consistency=1.0`（21 条有证据类型断言，对应 Agent routing 审计）、`forbidden_claims=1.0`（6 条有禁词断言，对应 Unsupported Claim）、gate passed；产物 `g4-deterministic-rules-20260916.json` / `-results.jsonl`；
- Citation coverage 与 Document Eval 的完整报告仍需裁判/文档夹具（预算窗口内执行）。

## 5. 资源预算与责任（执行前提，方案 §9.0）

| 项 | 估算/要求 | 角色 |
|---|---|---|
| RAGAS 全量 25×6，单轮 | ~9 小时；三轮 ~27 小时 | 测试 + 模型预算审批 |
| 200 条人工确认样本 | 现有 143，补 57+ | 后端 + 业务标注人 |
| 30 分钟×3 + 2× 过载 + 恢复 | 至少一个完整业务高峰窗口 | 运维 + 测试 |
| 绝对 SLO | 业务/服务负责人登记 | 业务负责人 |

缺少预算或标注责任人时，G4 仅标记“确定性回归通过，质量/容量未验收”。

## 6. 观测验收

- Prometheus 已加载 15 条 Hdb 告警（含 `HdbResourceUncertainEscalated`、`HdbStaleAuthorityWrites`、`HdbAttachmentWaitTimeouts`）；
- Grafana `Hardware DataBase / Production Hardening` 看板已加载；指标链路应用→OTel→Prometheus 已验证；
- 压测期间需确认队列、锁、额度、下游、Worker 与 stale write 面板可定位。
