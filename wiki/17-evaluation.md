# 17 · RAGAS 评估子系统

对应代码：`src/evaluation/`（9.4k 行，22 个模块）、`src/api/routes/evaluation.py`（1123 行）、
`frontend/src/pages/admin/EvaluationPage.tsx`；CLI `hardware-database-eval`（`src/evaluation/cli.py`）。

定位：管理员可用的**回答质量评估**。支持**在线采集**（真实跑业务管线）与**离线重评**
（对已保存快照换裁判模型重打分）；产出可审计、可对比、可门禁的报告。

---

## 1. 数据集格式

JSONL，每行一个 `EvaluationSample`（`extra="forbid"`）：

| 字段 | 类型/默认 | 说明 |
|---|---|---|
| `id` | str 必填非空 | 唯一；重复是校验错误 |
| `question` / `reference_answer` | str 必填非空 | — |
| `reference_contexts` | `list[str] = []` | 仅审计用（对应 RAGAS 0.4 的 LLM context_recall 参考） |
| `kb_name` | str 必填 | 必须与选中 KB 绑定一致 |
| `tags` | `list[str] = []` | `{direct, small-talk, permission, isolation}` 命中则归入**非检索队列（non_retrieval）** |
| `metrics` | `list[str] = []` | 逐样本指标提示（**不用于限制 RAGAS**） |
| `required_evidence_types` | `list[str] = []` | 词法 `evidence_consistency` 用 |
| `rubric` | `{required_facts[], forbidden_claims[], must_disclose_missing, must_disclose_conflicts}` | — |
| `request_context` | dict | 白名单键 `user_id, session_id, department_id, roles, allowed_kbs, kb_permissions`；未知键报错 |
| `critical` | bool = false | 失败/越阈值 → 门禁失败 |
| `expected_access` | `"allowed"` \| `"denied"` | denied 是负向授权对照 |

校验：`_parse_dataset` 读 UTF-8-sig、跳空行、聚合错误 `line N: …`、重复 id 报首次行号；
`validate_dataset` 返回错误表；`load_dataset` 抛 `DatasetValidationError("; ".join(errors))`。

内置数据集：`evaluation/datasets/{aaa_hard_eval_v1.jsonl(47 例, kb AAA, 作用域 1739:AAA),`
`hardware_qa_v1.jsonl(25 例), attachment_editing_v1.jsonl}`。

---

## 2. 核心 schema（`schemas.py`）

```python
MetricStatus = success | failed | not_applicable
SnapshotStatus = success | failed
ExpectedAccess = allowed | denied ;  ObservedAccess = allowed | denied | unknown
RunStatus = queued | running | pause_requested | paused | collected | cancel_requested | cancelled
          | completed | failed
RunStage  = idle | collecting | collected | scoring | reporting
```

| 类型 | 字段 |
|---|---|
| `AnswerSnapshot` | `sample_id, question, kb_name, response, scored_response, retrieved_contexts, evidence, retrieval_summary, status, error_stage, error_message, started_at, finished_at, duration_seconds, metadata, access_check`；`contexts_for_scoring()` 优先 `retrieved_contexts`，否则从 `evidence[*].content` 派生 |
| `DocumentGenerationSnapshot` | 字段级观测（`mapped_field_id, filled_value, evidence_sources, retrieved_evidence_sources,` 覆盖/越权/不支持填充计数, `auto_approved, typed_value_ok, writer_mode, writer_fallback, requires_human, llm_calls, duration_seconds, token_usage`） |
| `MetricResult` | `sample_id, metric_name, score: float\|None, status, reason, details` |
| `SampleResult` | `sample_id, question, reference_answer, response, scored_response, retrieved_contexts, critical, snapshot_status, metrics, metadata, access_check` |
| `GateResult` | `passed, exit_code, metric_scores, metric_counts, failures` |
| `EvaluationSummary` | 运行身份 + 计数 + 指标分数/计数/失败 + 评分进度 + `gate` + 元数据（`kb_id, kb_name, department_id, created_by, source_dataset_path, dataset_sha256, execution_dataset_sha256, dataset_*_counts, cohort_fingerprint, snapshot_sha256, snapshot_ownership_verified, validation_warnings, llm_model, embedding_model, evaluation_config`） |
| `EvaluationRunState` | 上述 + `dataset_path, snapshot_path, mode, sample_ids, tags, status, stage, total/completed/successful/failed_samples, scoring_completed_groups/total_groups, scoring_completed_items/total_items, current_sample_id/question, started_at, updated_at, finished_at, error_message, report_path`（`model_validator(mode="before")` 兼容删除 `score_enabled` 的旧状态） |

---

## 3. CLI（`hardware-database-eval`）

| 命令 | 必需 | 可选 | 行为 |
|---|---|---|---|
| `validate` | `--dataset PATH` | — | 打印全部错误到 stderr，非法退出 1 |
| `collect` | `--dataset`、`--output DIR` | `--no-resume`、`--sample-id`(可重复)、`--tag`(可重复) | 快照写 `output/<run_id>/snapshot.jsonl`；打印数量 |
| `score` | `--dataset`、`--snapshot PATH`、`--output DIR` | `--sample-id`、`--tag`、`--metric`、`--threshold name=value`、`--fail-on-threshold` | 新 run 目录；检查点 `run_dir/.score_checkpoint.json`；写报告；`run_state.json(mode=offline)`；成功后删检查点 |
| `run` | `--dataset`、`--output DIR` | 同上 | 采集+评分+报告；`mode=online` |

- `--metric none` → `[]`（跳过 LLM 指标）；默认 `DEFAULT_STANDARD_METRICS =`
  `[answer_correctness, faithfulness, answer_relevancy, context_precision, context_recall]`；
- `--threshold` 解析 `name=value`（name 非空、value ∈ [0,1]）；
- `main()` 在 `--fail-on-threshold` 时返回 `summary.gate.exit_code`，否则 0；
  捕获 `DatasetValidationError, ValueError, OSError, RuntimeError` → `evaluation error: …` 退出 1。

---

## 4. `EvaluationService`

构造：`EvaluationService(*, ragas_adapter=None, pipeline_factory=AppPipeline, config=None)`。

### 4.1 `collect(samples, snapshot_path, *, resume=True, before_sample, after_sample)`

- `SnapshotStore(snapshot_path)`；`resume` 时跳过 `completed_ids()`（每个样本**最新一条
  `status=="success"`**，因此后续失败的重试不会让成功复活）；
- 并发 `EVAL_MAX_WORKERS`(4)；线程池用 `submit_with_current_context`（继承 trace 上下文）；
  调用内缓存键 `(question, kb_name, expected_access, json(request_context, sort_keys=True))`
  复用成功快照（**不跨运行/授权复用**）；
- `_collect_sample` → `AnswerRunner.collect`；**降级自愈**：若判为降级快照
  （`status != success` 或回答以 `系统错误`/`模型服务调用过于频繁` 开头）且
  `EVAL_COLLECT_RETRIES`(1) > 0 → 原地重试一次；
- `before_sample` 返回 false 即停止提交；
- `AnswerRunner`：每线程一个 `AppPipeline`（`threading.local`）；收集事件、
  `strip_narration_segments` 去计划叙述、`_SENSITIVE_KEYS` 深度 6 脱敏检索摘要、
  丢弃 summary 中重复的 evidence 副本；`extract_scored_response` 去掉行政段落
  （来源说明/证据来源/检索账本/证据覆盖度/检索诊断/证据质量 与「子问题 sqN 已完全覆盖」行），
  诊断落 `metadata.scored_response_filter`；
- `expected_access=="denied"` 的样本**不发给管线**，直接记合成拒绝快照
  （`metadata.collection_mode="access_check_only"`，`retrieval_summary.status="permission_denied"`）。

### 4.2 `score(samples, snapshots, ...)`

- 指标默认 `DEFAULT_STANDARD_METRICS`；
- 硬件域规则仅对 `expected_access=="allowed"` 的**成功**快照运行：
  `score_hardware_rules` → `forbidden_claims`、`evidence_consistency`；
- 种子/续跑：`completed_groups`、`seeded_metrics`；只有
  `status==success 且 score is not None 且 is_ragas_metric` 的单元格算已种；
- 无成功快照 → 指标置空 + `metadata["scoring_skipped"]="no_successful_snapshots"`；
- **队列**：`evaluation_cohort(sample)` = `non_retrieval`（tags 命中四个标签之一）否则 `retrieval`；
  非检索样本与 denied 样本的所有 RAGAS 指标置 `not_applicable`（原因明确）；
- 只有 `allowed` 的检索队列样本进 adapter；
  `adapter.prepare_snapshots_for_scoring(retrieval_snapshots, full_contexts=True)`；
  逐样本诊断存 `metadata["ragas_scoring"]`；
- 批量路径 `adapter.score_batched(..., on_result=..., skip_cells=...)`；
  `_ScoringCancelled` → 所有未上报单元格置 `not_applicable("scoring cancelled")`，保留已完成结果；
- 进度：`completed_groups/total_groups` 以**指标名**计，`completed_items/total_items` 以
  样本×指标计；回调返回 false → `stopped` → 结果 `in_progress`；
- `_build_summary`：`evaluate_gate(results, thresholds or DEFAULT_THRESHOLDS, fail_on_threshold,`
  `sample_cohorts)`；元数据含 `run_outcome{kind,completed_groups,total_groups}`、
  `scoring_diagnostics`、`judge_governance = model_gateway.governance_status()`
  （fail-soft；**bypassed 的结果不得用于生产容量/质量验收**）；
- `_build_scoring_diagnostics`：`status ∈ {ready, interpret_with_caution, insufficient_coverage,`
  `technical_failure}`，统计采集失败、有/无证据（仅正常检索）、检索部分失败、上下文截断、
  指标失败、逐指标 `{success,failed,not_applicable,zero_scores}`、`all_zero_metrics`，附中文告警；
- `run(...)`：加载数据集 → 过滤 → `run_id = YYYYMMDDTHHMMSSZ-<8hex>` → `collect → score →`
  `write_reports` → `write_manifest`。

预检：`preflight_online(samples)`、`preflight_scoring()`（注入/非原生 adapter 时跳过）。

---

## 5. 运行控制与状态机（`run_control.py`）

`RunStateStore(path=run_dir/run_state.json)`：进程内 RLock；原子写（临时文件 + `os.replace`，
对 `PermissionError` 重试延迟 `(0.05,0.1,0.2,0.4,0.8,1.0,1.0)`）；`finally` 清理临时文件。

| 方法 | 允许的起始态 | 结果 |
|---|---|---|
| `mark_running(stage)` | `queued` | `running` + stage；首次设置 `started_at` |
| `begin_sample(sample)` | `running`（若已请求暂停/取消 → 结束） | `stage="collecting"` + 当前样本 |
| `request_pause` | `queued`/`running`（幂等） | `pause_requested` |
| `mark_paused` | `pause_requested` | `paused`，stage `idle` |
| `request_cancel` | `collected` → 直接 `cancelled`；`queued/running/pause_requested` → `cancel_requested`；`paused` → 直接 `cancelled`；已 `cancel_requested` 幂等 | — |
| `mark_cancelled` | `cancel_requested`/`paused`/`collected` | `cancelled` |
| `mark_collected(qc_verdict)` | `running`/`pause_requested`/`cancel_requested` | `collected` + `evaluation_config["collection_qc_verdict"]` |
| `request_scoring` | `collected`/`failed` | `queued`，清 `finished_at`/错误 |
| `mark_completed(report_path)` | `queued`/`running` | `completed` |
| `mark_failed(error)` | `queued`/`running`/`pause_requested`/`cancel_requested` | `failed` |
| `complete_report_or_handle_control(report_path)` | 最终发布门 | 完成 / 移除产物后 paused / 移除产物后 cancelled |
| `publish_partial_report(report_path, error)` | 控制请求胜出 | paused / cancelled / failed（带部分报告） |
| `mark_orphaned_as_paused` | 非终态 | `paused`（显示期恢复） |

`_REPORT_ARTIFACT_NAMES = ("summary.json","results.jsonl","summary.csv","report.html","report_complete.json")`；
`remove_report_artifacts()` 同时删 `.tmp` 变体。

`EvaluationRunController(service_factory, state_root)`：

- `create_online_run` / `create_offline_run`：`_freeze_execution_dataset` 把规范化样本写
  `execution_dataset.jsonl` 并返回 `(execution_path, sha256(source), sha256(execution))`；
  记录 counts、cohort 指纹、`snapshot_ownership_verified` 等；
- `start(run_id)`：每 run 一个活线程（`_threads` 注册表，重复启动抛 `RuntimeError`），
  守护 + 上下文传播，命名 `evaluation-<run_id[:16]>`；
- `pause` / `cancel` / `resume(mode)` / `delete` / `start_scoring(force)` / `load_collection_qc` /
  `load_for_display`；
- `resume(run_id, mode)`：`mode ∈ {continue, restart}`；仅从 `paused`/`cancelled`；
  **`cancelled` 不允许 continue**（提示用 `restart`）；`restart` 删 `.checkpoint`；
  设置 `evaluation_config["scoring_resume"]=mode`；清报告与错误 → `queued`；
- `delete(run_id)`：拒绝路径穿越（`run_dir.parent != state_root`）、拒绝活线程、
  只允许 `{failed,cancelled,completed,collected}`，然后 rmtree；
  外部离线快照/数据集**不受影响**；
- `load_for_display`：线程存活则原样返回（**不会**顺带暂停）；否则 `mark_orphaned_as_paused`；
  无 `run_state.json` 时从 `summary.json` 合成 `completed`（两者都缺 → 报错）。

### 5.1 `_execute_impl(run_id)` 流程

```
1. 从快照刷新进度；queued → mark_running(stage="collecting")；否则必须处于 running/pause/cancel_requested
2. 从「执行数据集」加载样本（已在创建时过滤），再套用保存的 sample_ids/tags
3. 记录评分器公共配置（llm_model/embedding_model/evaluation_config）——采集前后各一次
4. online: preflight_online（失败 → mark_failed）→ service.collect(resume=True, 进度回调)
   offline: SnapshotStore.load_all()
5. 写 snapshot.manifest.json，记录 snapshot_sha256 + ownership_verified
6. checkpoint：pause_requested → mark_paused 并停；cancel_requested → mark_cancelled 并停
7. 在线采集/评分分离：除非 evaluation_config["scoring_requested"]，
   跑 run_collection_qc → 写 collection_qc.json → mark_collected → 返回（等待显式 score）
8. 评分预检（service.preflight_scoring）失败 → mark_failed
9. 预置进度总数（retrieval_count × 5 指标）；stage="scoring"
10. progress_callback=checkpoint（写 .checkpoint/summary.json|results.jsonl）
11. item_progress_callback=item_checkpoint（每完成/每 5 项/每 2.0s/暂停取消时写）
12. scoring_resume=="continue" → _load_checkpoint_metrics 种子已成功单元格
    （别名归一：context_precision_with_reference / llm_context_precision_with_reference → context_precision）
13. service.score(..., run_id, progress_callback, item_progress_callback, seeded_metrics,
    should_stop=lambda: status in {pause_requested, cancel_requested})
14. 若暂停/取消胜出 → 写部分报告（kind=partial_paused|partial_cancelled）→ publish_partial_report
15. 否则 stage="reporting" → 写最终报告（kind="completed"）→ 复查控制 → complete_report_or_handle_control
16. 异常：有检查点 → publish_partial_report(partial_failed)；
    否则移除产物 → mark_failed("evaluation worker failed; see application logs")
```

### 5.2 `start_scoring(run_id, force=False)`

允许从 `collected`；从 `failed` 仅当采集完整
（`total_samples>0 and completed_samples==total_samples and successful_samples>0`），
否则 `ValueError("cannot start scoring from failed: collection is incomplete; rerun the evaluation instead")`；
QC `fail` 且未 force → `ValueError("collection QC failed: …")`；
随后清检查点、置 `scoring_requested=True`、`request_scoring()`、`start()`。

---

## 6. 快照存储与 sidecar 清单

`SnapshotStore(path)`（JSONL，UTF-8-sig）：

- `load_all()` 严格：非法行 → `ValueError("invalid snapshot at line N: …")`；
- `completed_ids()`：每样本最新一条成功行；
- `append(snapshot)`：加锁读-改-写整文件（临时文件 + `os.replace`，**原子全量重写，不是 O_APPEND**）。

`snapshot_manifest.py`：`snapshot_sha256(path)`（1 MiB 分块）；
`write_snapshot_manifest(...)` → `snapshot.manifest.json`（`schema_version:1, kb_id, kb_name,`
`department_id, cohort_fingerprint, snapshot_sha256, snapshot_path, ownership_verified`）；
`validate_snapshot_manifest(...)` 校验 ownership 类型、kb identity、cohort、文件哈希，返回中文错误列表。

---

## 7. 采集 QC 门（`collection_qc.py`）

`run_collection_qc(samples, snapshots)`（**永不抛异常**）：

- 取每样本最新快照；逐样本标志：
  - `missing_snapshot`（`allowed` 样本算问题，并全局记「快照数量不足：N 个样本未采集」）；
  - `collection_failed`（`status != success`，问题「采集失败（stage）」）；
  - `degraded_answer`（回答以 `系统错误`/`模型服务调用过于频繁` 开头，问题「降级答案…，无评分价值」）；
  - `no_evidence`（非 denied 且上下文字符数为 0，仅**告警**）；
- 裁决：有任何问题 → `fail`；仅有告警 → `pass_with_warnings`；否则 `pass`；
- 输出：`{verdict, checked_at, totals{samples,snapshots,failed,degraded,zero_evidence,expected_denied},`
  `issues, warnings, samples[{sample_id,status,evidence_count,duration_seconds,flags}]}`；
  持久化到 `run_dir/collection_qc.json`。

---

## 8. 预检（`preflight.py`）

`EvaluationPreflight(pipeline_factory).validate(samples, scan_sources=True)`：

- `expected_access=="denied"`：声明 `required_evidence_types` 或 `metrics` 是错误；
  上下文必须**没有**该 KB 的读权限；
- `allowed` + `required_evidence_types`：必须有读权限；`scan_sources` 时
  `pipeline.scan_kb_sources(...)` 必须返回 ≥1 来源（按 `(kb_name, department_id)` 缓存；
  失败按作用域只报一次）。

`validate_scoring(config=None)`：

- 模块可导入：`ragas`、`openai`、`langchain_openai`（缺 → 「评分依赖缺失：…；请运行 uv sync --group eval」）；
- `EvaluationConfig.from_environment()` 错误 → 「评估配置无效：…」；
- `_ping_endpoints`：LLM `POST {base}/chat/completions`（`max_tokens:1`，20s）+ 
  embedding `POST {base}/embeddings`（`input:["ping"]`）；非 200/不可达 → 明确中文错误（含状态码/模型/URL）。

---

## 9. `RagasAdapter`

### 9.1 指标注册

```python
STANDARD_METRICS = {answer_correctness, faithfulness, answer_relevancy, context_precision, context_recall}
RAGAS_RESULT_KEYS = {…, context_precision → llm_context_precision_with_reference, …}
CONTEXT_METRICS = {faithfulness, context_precision, context_recall}
RAW_CONTEXT_METRICS = {context_precision, context_recall}
```

适用性 `_metric_is_applicable`：`context_recall` 需参考答案非空且快照有上下文；
`faithfulness`/`context_precision` 需上下文非空；其它恒真。

字段映射：

| 指标 | user_input | response | reference | retrieved_contexts |
|---|---|---|---|---|
| answer_correctness | ✓ | ✓ | ✓ | — |
| answer_relevancy | ✓ | ✓ | — | — |
| faithfulness | ✓ | ✓ | — | ✓ |
| context_precision | ✓ | — | ✓ | ✓ |
| context_recall | ✓ | — | ✓ | ✓ |

`response` 恒用 `scored_response or response`。

### 9.2 评分与重试阶梯

`score(samples, snapshots, metric_names, *, snapshots_prepared=False, on_result=None)`：

- 快照缺失/失败 → 每个指标 `failed`（「answer snapshot is missing or failed」）；
  不适用 → `not_applicable`（「required contexts are unavailable」）；
- 按适用指标元组分组；后端 = 注入或 `_NativeRagasBackend(config)`；
- 有 `on_result` → 逐样本评（实时进度）；结果按 `(样本序 × 指标序)` 排序；
- 无 `on_result` → 按指标组 `backend.score(records,[metric])` 包裹
  `observe.evaluator("hdb.evaluation.metric", metric=…, stage="score", sample_count=…)`；
- 重试阶梯（每单元格）：
  1. 异常 + CONTEXT_METRICS + 可重试上下文错误（TimeoutError/ConnectionError/HTTP 400）且非 full-context
     → `_retry_with_smaller_context`；
  2. NaN → 最多 `config.max_retries` 次 + `_backoff_sleep`；
  3. 异常 + CONTEXT_METRICS + 可重试上下文错误且未重试过 → 缩小上下文重试；
  4. 异常 + 非上下文 + `_is_retryable_evaluator_error` → `_retry_transient_metric`；
- `_is_retryable_evaluator_error`：TimeoutError/ConnectionError、HTTP 408/429/≥500，
  或类名 ∈ `{APITimeoutError, APIConnectionError, ConnectTimeout, NetworkError, ReadTimeout,`
  `RemoteProtocolError, TimeoutException}`；
- `_backoff_sleep`：优先 Retry-After，否则 `min(30, 2**attempt) * (0.5 + random())`，上限 60s；
- 失败结果：`status="failed"`，`details.evaluator_diagnostic`（kind/attempts/上下文统计/最终上下文）；
  成功结果可带 recovered 重试诊断。

**取消**：`set_stop_check(fn)` + 每次裁判调用前 `_raise_if_stopped()` → 内部 `_ScoringCancelled`
→ 服务层转 `not_applicable("scoring cancelled")`。

### 9.3 批量路径 `score_batched(...)`（生产路径）

- 先按指标集合分组，再按**上下文模式**拆执行组：
  `("raw", RAW_CONTEXT_METRICS)` 用 `snapshot.metadata["_raw_retrieved_contexts"]`；
  `("curated", 其它指标)` 用处理后的上下文；
- 按记录（去掉 `sample_id`）的规范 JSON 去重，映射回所有位置；
- 原生后端流式回调只上报干净浮点（跳过 NaN/异常），最终统一走重试与失败 emit；
- `skip_cells`（恢复运行）既不发给裁判也不重复上报。

### 9.4 上下文准备 `prepare_snapshots_for_scoring`

- **生产用 `full_contexts=True`**：`bounded_contexts = raw_contexts`，诊断记录原始/评分条数与字符数、
  `contexts_truncated: False`、`context_selection: "raw_original_order"`、
  `diagnostic["scored_contexts"] = bounded_contexts`（**送入 RAGAS 的精确字符串**）；
  元数据标 `_raw_retrieved_contexts` / `_full_contexts_for_scoring`；
- `full_contexts=False` 时（`_scoring_contexts` + `_bounded_contexts`）：
  - 证据相关性 = 问题 token 与内容重叠 + 样板检测（填写说明/模板说明/封面/资料源目录/模板变更历史）；
  - 排序：非样板优先 → 问题相关性 → `evidence_quality` 分 → 证据 id；
  - claim 覆盖（`claim_coverage` 的 `supported|partial|conflicting`）按 `content_kind`
    选最佳证据（保证联合硬件问题的电路+文档双重证据）；
  - `context_selection ∈ {claim_coverage+evidence_quality, evidence_quality, claim_coverage, original_order}`；
  - 上限：`EVAL_MAX_CONTEXTS_PER_SAMPLE=4`、`EVAL_MAX_CONTEXT_CHARS=4000`、
    `EVAL_MAX_CONTEXT_CHARS_PER_ITEM=1200`；按全文与归一化 120 字符前缀（≥24）去重；
    HTML 先 `strip_markup`；诊断含 `contexts_truncated`。

### 9.5 原生后端（`_NativeRagasBackend`）

- LLM：`AsyncOpenAI(api_key or "not-required", base_url, timeout, default_headers=opencode_extra_headers)`
  + `ragas.llms.llm_factory(model, client=..., max_tokens=...)` → **`govern_ragas_llm(llm)`**
  （包裹 `generate/agenerate` 为 batch 槽位并记录用量；绕过会记
  `judge_governance.bypassed=True` + `hdb.eval.governance_bypassed`）；
  包裹必须在 LLM 对象上（RAGAS 的 instructor 适配器做 `isinstance(..., AsyncOpenAI)` 检查）；
- Embeddings：legacy `langchain_openai.OpenAIEmbeddings(check_embedding_ctx_length=False,`
  `dimensions=EVAL_EMBEDDING_DIMS)`；现代路径是 `ragas.embeddings.OpenAIEmbeddings` 子类注入 `dimensions`；
- `RunConfig(timeout, max_workers, max_retries)`；
- **生产指标**（`ragas.metrics.collections`）：

```python
answer_correctness → AnswerCorrectness(llm=llm, embeddings=embeddings)
answer_relevancy   → AnswerRelevancy(llm=llm, embeddings=embeddings)
faithfulness       → Faithfulness(llm=llm)
context_precision  → ContextPrecisionWithReference(llm=llm)
context_recall     → ContextRecall(llm=llm)
metric.max_retries = config.max_retries     # 仅诊断用
```

按 `(metric_names, id(llm))` 缓存；
`_score_modern_async`：每个 `(record, metric)` 一个 asyncio 任务，`Semaphore(max_workers)`，
`asyncio.wait_for(..., timeout=max(timeout,1)*2)`，`as_completed` 流式回调；
`_run_async` 兼容「已有事件循环」宿主（专用线程 `hdb-ragas-loop`）；
依赖缺失 → `RuntimeError("RAGAS evaluation dependencies are missing; run 'uv sync --group eval'")`；
- **回退裁判** `_fill_with_fallback`：配置了 `EVAL_LLM_FALLBACK_BASE_URL` + `_MODEL` 时，
  只对失败单元格（None/异常/NaN）用回退 LLM 重打分，**不动健康单元格**。

**为什么锁定 `ragas==0.4.3`**：pyproject 注释——「锁定适配器使用并测试过的 collections API 版本，
避免指标提示词/语义在部署间漂移」；`tests/evaluation/test_ragas_runtime.py` 断言
`ragas.__version__.startswith("0.4.")` 且 `ragas.metrics.collections.Faithfulness` 存在。

---

## 10. 硬件域指标（`hardware_metrics.py`）

- `_EVIDENCE_TYPE_ALIASES`（存储 kind → 数据集词表）：`document_text→document`；
  `spreadsheet_table|spreadsheet_schema|spreadsheet_sql_result|table→spreadsheet`；
  `circuit→circuit_design`；`catalog→catalog`；
- 归一化 `NFKC + casefold`，子串匹配 + 折叠空白回退；
- `score_hardware_rules(sample, snapshot)`：
  - `forbidden_claims`：命中任一禁止断言 → 0.0，否则 1.0；rubric 为空 → `not_applicable`；
  - `evidence_consistency`：`(期望类型数 − 缺失数)/期望类型数`；无期望 → `not_applicable`；
  - 明确只做词法（语义质量交给 RAGAS）；
---

## 11. 门禁（`gates.py`）

```python
DEFAULT_THRESHOLDS = {
  answer_correctness: 0.75, faithfulness: 0.75, evidence_consistency: 0.75,
  answer_relevancy: 0.70, context_precision: 0.70, context_recall: 0.70,
  forbidden_claims: 1.0,
}
DOCUMENT_GENERATION_HARD_ZERO_METRICS = {
  fixed_content_overwrite_rate, source_scope_violation_count, unsupported_required_field_fill_count,
}
```

`evaluate_gate(results, thresholds=None, *, fail_on_threshold=False, sample_cohorts=None, min_coverage=0.8)`：

- 跳过 non_retrieval 队列的 RAGAS 指标；
- `critical` + `status=="failed"` + 有阈值 → 失败 `critical sample {id}: {metric} evaluation failed`；
- `critical` + 分数 < 阈值 → 失败；
- 覆盖率：`needed = ceil(0.8 * sample_total)`；评分数不足的指标**跳过阈值检查**；
- 逐指标取成功分数的均值；越阈值 → `{metric}: {score:.3f} < {threshold:.3f}`；
- `passed = not failures`；`exit_code = 2 if fail_on_threshold and not passed else 0`。

---

## 12. 报告（`reporters.py`）

`write_reports(run_dir, summary, results, *, metadata=None)`：逐文件原子写；
先删 `report_complete.json`，最后写它作为**完成标记**。

| 文件 | 内容 |
|---|---|
| `summary.json` | `EvaluationSummary.model_dump_json(indent=2)`（合并 metadata）；**自动化消费的权威汇总** |
| `results.jsonl` | 每行一个 `SampleResult` |
| `summary.csv` | **UTF-8 with BOM**；列：sample_id、snapshot_status、question、reference_answer、response、scored_response、retrieved_contexts(JSON)、scored_contexts(JSON)、ragas_scoring(JSON) + 每指标一列 |
| `report.html` | 标题 + 计数 + `run_outcome.kind` + 指标表；每样本后跟整行详情（问题/参考答案/实际回答/评分正文/原始上下文 + 「评分上下文：N/M 段，X/Y 字符；已裁剪：是/否」+ 实际送入评分的上下文），全部 HTML 转义 |
| `report_complete.json` | `{"run_id": …}` |

`write_manifest(run_dir, manifest)` → `manifest.json`。

---

## 13. 历史、队列与导入

### 13.1 队列（`cohorts.py`）

`RAGAS_METRICS`、`is_ragas_metric`；`NON_RETRIEVAL_TAGS = {direct, small-talk, permission, isolation}`；
`evaluation_cohort(sample) -> "retrieval"|"non_retrieval"`。

### 13.2 历史（`history.py`）

- `cohort_fingerprint(sample_ids)`：排序/去重/修剪后规范 JSON（无空格）的 SHA-256；
- `load_history_run(run_dir)`：必须有 `summary.json`；`results.jsonl` 严格校验；
  可选 `import_manifest.json` 判定 origin（`local`/`imported`）；合并去重告警；
- `compatible_baselines(selected, candidates)`：同一非空指纹，排除自身；
- `compatibility_report(current, baseline)` 五项严格检查：`kb_id`、`cohort_fingerprint`、
  `metrics`、`model_config`（公共评估配置 + llm/embedding 模型）、
  `snapshot_ownership`（kb_id + cohort + `snapshot_ownership_verified`）；
  缺失元数据即视为不匹配（「旧报告仍可查看，但不能被当作可复现基线」）。

### 13.3 导入（`history_import.py`，库级；未接 API 路由）

- 强制工件 `summary.json` + `results.jsonl`；可选 `summary.csv` + `report.html`；
  `IMPORT_MANIFEST_SCHEMA_VERSION=1`；`RENAME_NOREPLACE=1`；
- `discover_imports(source_root, target_root) -> ImportPlan`：只读；拒绝重叠根（路径与
  device/inode 身份）；逐组件 `O_NOFOLLOW|O_DIRECTORY` 打开；拒绝 symlink run 目录；
  校验 JSON 与样本 id；计算逐文件 sha256 / 样本 id / cohort；分类
  `copy|skip_equal|conflict|invalid`；
- `apply_import_plan(plan)`：复核根身份与目标根安全（属主 euid、非组/世界可写）；
  复制到私有 staging（0700，随机名）并逐文件校验哈希；写 `import_manifest.json` + `report_complete.json`；
  fsync；用 **`renameat2(RENAME_NOREPLACE)`** 发布（无安全回退，不可用即 `ImportApplyError`）；
  清理用隔离重命名；错误类 `ImportApplyError` / `ImportPublishedUncertainError`。

### 13.4 展示（`presentation.py`）

指标中文标签（禁词检查/证据一致性/答案正确性/忠实性/答案相关性/上下文精确率/上下文召回率）；
`classify_sample_result` 序列（采集失败 → 评分失败 → 关键样本待复核 → 无检索证据 → 正常完成）；
`build_credibility_summary`（`存在技术失败`/`评分覆盖不足`/`结果可解读`）；
`build_comparison`、`build_current_metric_chart_rows`（附阈值与达标标记）、
`build_baseline_chart_rows`、`build_sample_rows`。

---

## 14. E2E 运行器（`e2e_runner.py`，opt-in）

- 门控 `HDB_E2E=1` 或 `--force`（会真实调用模型与服务）；脚本 `hardware-database-e2e`；
- 数据集：JSONL（允许 `#` 注释），`E2ECase{case_id, query, fixture{kind∈{txt,md,docx}}, expect,`
  `description, followup_queries, source_scope="attachment_only"}`；
  `expect` 严格白名单（turn_status、工具名单、blocked、revision/释放状态、文本包含等）；
- `evaluate_case_expectations(expect, observation)` 纯离线可测；blocked 用例要求存在
  `status="blocked"` 的 `stage` 事件与预期 error_code 且无候选修订；`chain_versions`
  要求每修订的 `parent_attachment_id == 上一 child_attachment_id`；
- 现场驱动 `E2EClient`：登录 → 建会话 → 传附件 → 建 turn + start → 5s 轮询终态 →
  从 SQLite 读 `chat_turn_events` → 收集修订/下载子件/已放行件 → 评估；
- CLI 参数（`--dataset` 默认 `evaluation/datasets/attachment_editing_v1.jsonl`、`--base-url`、
  `--kb`、`--department-id`、`--output`、`--case`、`--limit`、`--min-pass-rate` 默认 0.9、
  `--keep`、`--force`）；退出码 0 仅当全部通过且达通过率门；
- 清理：删除会话并停用本次创建的用户（除非 `--keep`）。

---

## 15. 访问/授权（`access.py`）

- `_PERMISSION_LEVELS = {"":0, read:1, write:2, admin:3}`；拒绝状态
  `{access_denied, permission_denied, forbidden, unauthorized, denied}`；
  允许状态 `{access_granted, allowed, authorized}`；
- `resolve_knowledge_base(summaries, kb_id=..., kb_name=...)`：只用**已注册且有稳定 kb_id** 的行；
  id 权威；仅名字且唯一时允许；id+name 不匹配 → `ValueError("kb_id 与 kb_name 不匹配")`；
  名字歧义 → `ValueError("知识库名称不唯一，请指定 kb_id")`；
- `normalize_sample_for_binding`：**数据集作用域只能收窄不能授予**——运行身份恒为
  `user_id="evaluation"`、`roles=["employee"]`、`department_id=binding.department_id`，
  且仅当数据集 `request_context` 允许时才有 `allowed_kbs/[scope]` 与 `kb_permissions{scope:"read"}`；
  `expected_access=="denied"` → 清空允许列表；
- `build_evaluation_context` → `RequestContext`（`session_id` 来自样本或 `eval-{id}`，
  `metadata` 带 `department_id` 与建议性的 `declared_user`）；
- `select_evaluation_samples`：先按 `kb_name == binding.kb_name` 过滤，再按 ids/tags；
  统计 dataset/matched/filtered/normal/expected_denied 与 cohort 指纹；
- `assess_access(...)`：**刻意忽略回答文本**；若在无读权限的上下文下返回了证据 →
  `observed="allowed"` 且原因 `evidence was returned despite a context without read permission`
  （泄漏检测）；支持结构化决策字段与布尔回退。

---

## 16. API（`routes/evaluation.py`）

**全部端点要求 `require_system_admin`**；`/preflight` 与 `POST /runs` 额外 `enforce_admission(...,`
`resource="evaluation")`。

路径安全：`output_root` 必须解析为 `STORAGE_DIR/evaluations`（否则 400「output_root 只能使用 storage/evaluations」）；
数据集必须在 `BASE_DIR/evaluation/datasets` 或 `STORAGE_DIR/evaluations/uploads`；
快照在 `STORAGE_DIR/evaluations` 下；`run_id` 必须是单一路径段。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/evaluation/knowledge-bases` | 仅有稳定 kb_id 的注册行；不含内容 |
| GET | `/evaluation/datasets` | 扫描内置目录 + uploads（同名 uploads 优先），每个 ≤20 MiB；返回样本数/畸形行/kb 绑定/tags/denied/critical 计数 |
| POST | `/evaluation/datasets` | multipart `.jsonl`；写入 uploads；非法 → 删文件 + 400 汇总错误 |
| POST | `/evaluation/preflight` | 只读；`scan_sources=(mode=="online")`，`check_scoring=False` |
| POST | `/evaluation/runs` | `scan_sources=False, check_scoring=False`（worker 启动时再查）；离线必须给 `snapshot_path`；审计 `evaluation_run_authorize_kb` |
| POST | `/evaluation/runs/{id}/start` | 启动前强校验：kb 绑定（缺 → 409）、KB/部门未变、执行数据集存在、哈希未变（409「运行输入副本哈希已变化」）、样本重载与 cohort 指纹一致；离线再校验快照哈希与 manifest |
| POST | `/evaluation/runs/{id}/pause` | 协作式 `pause_requested` |
| POST | `/evaluation/runs/{id}/resume?mode=continue\|restart` | `continue` 只对 paused 种检查点；`restart` 清检查点（paused/cancelled 均可）；随后重启线程 |
| POST | `/evaluation/runs/{id}/score?force=false` | 需 `collected`（或采集完整的 `failed`）；QC fail 未 force → 拒绝 |
| POST | `/evaluation/runs/{id}/cancel` | 协作式；部分报告保留 |
| DELETE | `/evaluation/runs/{id}` | 仅 `failed/cancelled/completed/collected`；404/409/500；审计 `delete_evaluation_run` |
| GET | `/evaluation/runs/{id}` | 状态 + summary + sample_results + collection_qc；评分中回落到 `.checkpoint/*`；样本诊断失败非致命（`sample_results_error`）；状态损坏 → 500；旧运行合成 completed |
| GET | `/evaluation/runs/{id}/compare?baseline=…&strict=…` | 两边都要 `summary.json`；严格不可比 → **409** `{message, warnings, compatibility}`；非严格 → 返回 `compatible` + 告警（含「仅查看对比：结果不满足严格基线条件，不得据此得出回归结论」） |

`_summary_dict` 会剥掉旧 summary 中缺失的运行元数据（不伪造 provenance）；
`list_evaluation_runs` 收录有 `summary.json` **或** `run_state.json` 的目录，按时间倒序。

### 前端契约（`EvaluationPage.tsx`）

- `ACTIVE_STATUSES = {queued, running, pause_requested, cancel_requested}` → **2s 静默轮询**；
- 按钮可用性：`canPause = queued|running`；`canResume = paused`；
  `canCancel = queued|running|pause_requested|paused|collected`；
  `canScore = collected`，或 `failed` 且采集完整；QC fail 时 score 按钮禁用；
- 预检在数据集/KB/模式/过滤器/快照变化时 600ms 去抖自动运行；任何配置变化使旧结果失效；
- `CollectionQcPanel` 展示裁决/计数/问题/告警；评分进度用
  `scoring_completed_items/scoring_total_items`；基线选择键为 cohort 指纹 + KB 身份。

---

## 17. 配置（`EvaluationConfig.from_environment()`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `EVAL_LLM_PROVIDER` | 回落 `AGENT_LLM_PROVIDER`(ollama) | 必须 `ollama|custom` |
| `EVAL_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | — | 显式指定时生效 |
| `EVAL_EMBEDDING_BASE_URL` / `_MODEL` | **必填** | 缺 → 配置错误 |
| `EVAL_EMBEDDING_API_KEY` / `_DIMS` | — | dims 为正整数 |
| `EVAL_LLM_MAX_TOKENS` | 4096 | 正 |
| `EVAL_MAX_CONTEXTS_PER_SAMPLE` / `_MAX_CONTEXT_CHARS` / `_MAX_CONTEXT_CHARS_PER_ITEM` | 4 / 4000 / 1200 | `full_contexts=False` 时生效 |
| `EVAL_SCORING_MAX_BUDGET_ATTEMPTS` / `_CONTEXT_SHRINK_FACTOR` | 3 / 0.5 | 缩小上下文重试 |
| `EVAL_TIMEOUT_SECONDS` / `EVAL_MAX_WORKERS` / `EVAL_MAX_RETRIES` | 60 / 4 / 2 | — |
| `EVAL_OUTPUT_ROOT` | `storage/evaluations` | — |
| `EVAL_LLM_FALLBACK_BASE_URL/_API_KEY/_MODEL` | — | `fallback_ready = bool(base_url and model)` |
| `EVAL_COLLECT_RETRIES` | 1 | 直接 `os.getenv` 读取（不在 dataclass 内） |

`public_metadata()` 只返回非敏感字段（无 API key）。

---

## 18. 测试与不变量

`tests/evaluation/`：`test_eval_config.py`、`dataset_loader.py`、`snapshot_store.py`（最新成功优先）、
`snapshot_manifest.py`、`preflight.py`、`answer_runner.py`、`hardware_metrics.py`、
`gates.py`（仅带标志才 exit 2；critical；低覆盖跳过；doc-gen 硬零）、`reporters.py`、
`ragas_adapter.py`（上下文收缩、NaN 恢复、瞬时重试、取消）、`ragas_runtime.py`（0.4 pin + collections）、
`service.py`（分组/批量评分、增量进度、采集缓存复用）、`run_control.py`（状态往返、原子写与重试、
每个转换守卫、QC 后显式评分、resume 模式、cancel 拒绝 continue、检查点种子归一、冻结输入 + 绑定元数据、
item 检查点、删除规则/穿越、孤儿→paused 恢复、部分报告、旧状态）、`history.py`、`history_import.py`
（不覆盖、冲突、哈希复验、symlink/身份攻击）、`cohorts.py`、`presentation.py`、`cli.py`、
`optimizations.py`（叙述剥离、近重复上下文折叠、预算前剥离 HTML、单样本覆盖）。
另有 `tests/test_evaluation_api.py`、`test_e2e_runner.py`、
`test_reviewer_eval_harness.py`。

核心不变量：

1. **运行输入不可变**：规范化 `execution_dataset.jsonl` + sha256，每次 start 前复验；
2. **报告只在评分完成时产出**；部分运行只发布 `.checkpoint/*` 或运行级部分报告，
   绝不冒充最终（`report_complete.json` 是完成标记）；
3. **暂停/取消是协作式的**，只在下个安全检查点生效；被取消的评分单元格 → `not_applicable("scoring cancelled")`；
4. **恢复不重评成功单元格**（seeded/skip_cells），失败与不适用会重试；
5. **denied 样本永不触达检索**；非检索队列样本永不获得 RAGAS 分数；
6. **裁判失败永远不是 0 分**（`failed`，不计入均值，`metric_failures` 如实上报）；
7. **治理绕过会被记录**并作废生产验收资格。

---

## 19. 一次在线运行的完整时序

```
POST /evaluation/runs (online)
  → _prepare_evaluation（数据集校验、按 kb_id 绑定 KB、样本选择、上下文/负向预检）
  → create_online_run：冻结 execution_dataset.jsonl；run_state.json = queued
POST /evaluation/runs/{id}/start
  → _validate_stored_run_before_start（KB、数据集哈希、cohort）
  → 线程：mark_running(collecting)
       preflight_online
       collect（EVAL_MAX_WORKERS 并发、断点续采、降级重试）
       写 snapshot.manifest.json
       run_collection_qc → collection_qc.json → status=collected   [停下等待]
POST /evaluation/runs/{id}/score [force]
  → start_scoring：QC 门 → request_scoring → queued → 线程
       preflight_scoring（依赖 + 端点 ping）
       stage=scoring → score_batched（raw + curated 两组，流式）
       .checkpoint/summary.json|results.jsonl（每指标组/5 项/2 秒）
       stage=reporting → summary.json / results.jsonl / summary.csv / report.html / report_complete.json
       complete_report_or_handle_control → completed
```

磁盘（`storage/evaluations/<run_id>/`）：`run_state.json`、`execution_dataset.jsonl`、
`snapshot.jsonl`、`snapshot.manifest.json`、`collection_qc.json`、`.checkpoint/`、
`manifest.json`（CLI `run` 写）、最终四个报告文件 + `report_complete.json`；
`uploads/` 存上传数据集，`e2e/` 存 E2E 摘要。
离线快照可位于运行目录之外（`storage/evaluations` 内任意路径），删运行**不会**动它。
