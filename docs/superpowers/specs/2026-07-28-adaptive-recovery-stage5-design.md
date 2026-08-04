# 阶段 5：自适应恢复（Adaptive Recovery）设计 — 闭环 P3 极端情况

- 日期：2026-07-28
- 分支：`feature/template-upload-governed-authoring`（worktree）
- 前置：阶段 0–4 已实施并通过验收（163 测试绿）
- 关联改进方案：`docs/Hardware-DataBase_文档生成改进方案.md` §3 阶段 5

## 1. 问题（P3 极端情况）

阶段 1 已闭环 P3 主路径：`success_empty` 触发改写 + 重试。但用尽 `max_retrieval_attempts_per_unit` 次后，字段仍可能 `success_empty`。当前行为（`graph.py:152-154`）：

```
if not evidence:
    result.unit_statuses[unit_id] = _missing_status(unit_id, schema, outcome)
    continue                                   # ← 不产草稿
```

`_missing_status`（`graph.py:483-490`）对 `missing_policy == "block_section"` 的字段返回 `"blocked"`，其余返回 `"tbd"`/`"insufficient_evidence"`。该字段**不进入草稿环节**，人工审核页只看到一个空的 blocked 字段——没有任何候选证据可供人审判断。

**run 级影响**：`runtime.py:222` 中 `blocked` 已在 `waiting_human` 集合内，故 run 状态本就为 `waiting_human`（不"终止"run）。所谓"终止"指**字段级**终止：无草稿、无证据、直接 blocked。

**目标**：原本判 `blocked` 的字段，在策略允许时以低置信证据产草稿并进 `requires_human`，人工有据可审，而非空 blocked。

## 2. 代码走查结论（关键：方案文本与现状的 reconcile）

改进方案 §3 阶段 5 原文："drop source_group 硬过滤（退回 balanced route）再查一次"。走查发现需与现状对齐：

### 2.1 source_group 硬过滤在 harness 路径的两种形态

`RAGFlowBackend.retrieve`（`ragflow_backend.py:860-945`）对 source_group 有两层过滤：

1. **服务端 `metadata_condition`**（`_metadata_condition`，`:227-248`）：当 `route_source_groups(query).should_filter`（confidence ≥ 0.7）时，把 `source_group` 条件加入 RAGFlow 检索的 metadata 过滤。**两条 harness 路径都传 `source_names`**（KB：`frozen_source_names`，`app_pipeline.py:535`；project：`[document.title]`，`app_pipeline.py:624`），但 `source_names` 不影响 `metadata_condition` 里的 source_group 条件——`_metadata_condition` 总是把 `routed_source_groups` 加进去。**所以服务端 source_group 过滤在两条路径都生效。**

2. **本地 `_filter_chunks` 后过滤**（`:931-933`，`apply_routed_source_groups`）：`apply_routed_source_groups = bool(routed_source_groups) and not source_names`。harness 路径 `source_names` 非空 → **本地后过滤 = False（关闭）**。

### 2.2 现有 0-chunk fallback 的覆盖盲区

`ragflow_backend.py:882-895`：当带 `metadata_condition`（含 source_group）的首检返回 **0 raw chunks** 时，丢弃整个 `metadata_condition` 重查。这能恢复"source_group 过严致 0 chunk"的情形——**但仅在 raw chunks = 0 时触发**。

**真正的盲区**：当 `route_source_groups` 高置信路由到 group X（如 BOM 查询 → MATERIAL_GROUP），但冻结集内相关文档被标在**别的 group**（或 UNKNOWN_GROUP）时：
- 服务端 `metadata_condition` 限定 group X → RAGFlow 返回 group X 的 chunks（来自**非冻结集**的文档）。
- raw chunks > 0 → **0-chunk fallback 不触发**。
- 本地 `source_names` 过滤（`:943`）把这些非冻结集 chunks 全部丢弃 → `evidences=[]` → `success_empty`。
- 冻结集内 group Y 的相关文档**从未被查询**。

**结论**：方案"drop source_group 硬过滤（退回 balanced route）"在当前架构下有真实含义——指**服务端 `metadata_condition` 的 source_group 条件**。放宽它 = 让 RAGFlow 跨所有 group 检索，再用本地 `source_names`（冻结集）兜底。这正是"退回 balanced route……仍在冻结 source_names 内，不破坏落域校验"。

### 2.3 落域校验不变（正确性约束）

- `_validated_evidence`（`graph.py:418-474`）：KB 路径强校验 `evidence.source_name in snapshot.source_names`，project 路径强校验 `source_version_id in versions`。**恢复路径产出的证据仍必须通过该校验**——`balanced_route` 只 drop source_group，**不放宽 `source_names`/`source_version_ids`**。
- `build_knowledge_base_retrieval_outcome`（`service.py:1219-1222`）：再次校验 `evidence.source_name in frozen_source_names`，否则 `PermissionError`。双重守门。
- backend 本地 `_filter_chunks` 的 `source_names` 过滤（`:943`）在 `balanced_route` 下**保留**——只有 source_group 条件被 drop。

## 3. 设计

### 3.1 触发点：`_retrieve_with_budget` 内的恢复检索

`_retrieve_with_budget`（`graph.py:233-263`）用尽 attempts 后，若满足条件，做一次 **balanced 恢复检索**：

```
# 伪代码（在 for-attempt 循环之后）
if ("adaptive_recovery" in allowed_tools
        and last.status == "success_empty"
        and recovery_attempts < self.policy.policy.max_adaptive_recovery_rounds):
    self._step(state, "adaptive_recovery")
    self.policy.require_tool("adaptive_recovery")          # 治理守门
    recovery = retrieve(requirement, attempt + 1, None, relaxed=True)
    recovery_attempts += 1
    if recovery.status == "success_with_hits" and recovery.evidences:
        _tag_low_confidence(recovery)                       # 见 3.3
        last = recovery
```

- **触发条件**：`adaptive_recovery` 在 allowlist（治理开关）+ `max_adaptive_recovery_rounds ≥ 1`（预算）+ 终态 `success_empty`。**hard-fail 状态（`retrieval_failed`/`source_unavailable`/`access_denied`/`partial_failure`）不触发**——这些是源不可用，balanced 路由无益；恢复只解召回（empty hits）。
- **预算独立**：恢复检索**不调用 `require_retrieval_round`**（避免与 `max_retrieval_rounds` 冲突：2 次 attempt 已耗尽 round 预算），也不递增 `state["retrieval_round_count"]`。它走自己的 `max_adaptive_recovery_rounds` 预算，仅 `require_step` + `require_tool`。
- **一次为准**：v1 `max_adaptive_recovery_rounds=1`，balanced 查询不变，多次无益；预算字段留扩展位。

### 3.2 RetrievalProvider 扩展 `relaxed` 关键字

`RetrievalProvider`（`graph.py:52`）签名不变（兼容旧 fake），但 graph 在恢复分支以 `relaxed=True` 调用：

- KB 闭包 `retrieve(requirement, _attempt, query_override=None, relaxed=False)`（`app_pipeline.py:571`）：`relaxed=True` → RAGFlow `filters` 追加 `"balanced_route": True`（仍含 `source_names`）。
- project 闭包 `retrieve`（`app_pipeline.py:601`）内 `retrieve_one`（`:619`）：同上。
- **specialized 检索器（spreadsheet）不受 `relaxed` 影响**——它不走 source_group 路由；恢复仍会查 spreadsheet（若 capability 声明），与正常路径一致。
- 旧 fake `lambda req, attempt, query_override=None` 不受影响：恢复仅在 `adaptive_recovery` allowlist + `max_adaptive_recovery_rounds ≥ 1` 时触发，现有测试 policy 不含此项 → 旧 fake 永不被 `relaxed=True` 调用。

### 3.3 RAGFlow `balanced_route`：drop source_group

`RAGFlowBackend.retrieve` 读 `filters.get("balanced_route")`：为真时 `routed_source_groups = ()`（强制 balanced）——`_metadata_condition` 不加 source_group 条件、本地 `apply_routed_source_groups` 仍为 False。**保留** kb_name/department/source_names 条件。等价于"退回 balanced route，仍在冻结 source_names 内"。

恢复检索的 evidence metadata 仍带 `query_route_reason="balanced query"`、`query_route_confidence=0.0`（`route_source_groups` balanced 分支自然产出），可供 ledger 展示。

### 3.4 low_confidence 标记 + 路由 requires_human

恢复命中后，graph 给 `recovery.evidences[*].metadata["low_confidence"] = True` 打标（`outcome.evidences` 是 `build_knowledge_base_retrieval_outcome` 的 copy，可安全 mutate；project 路径 `EvidenceEnvelope.metadata` 同样可 mutate）。检测信号即此标记：

```
recovery_triggered = any(_evidence_low_confidence(e) for e in outcome.evidences)
```

`_evidence_low_confidence` 复用现有 `_evidence_fallback`（`graph.py:409-415`）的模式（`getattr(e, "metadata", {})`）。loop 在 `_validated_evidence` 之前从 `outcome.evidences` 读标记（此时 metadata 完整），故 KB/project 两路径都能检测。

随后正常走 rerank → ledger → draft → validate，**但**：

- **ledger row** 增加 `recovery_triggered: bool`（+ `recovery_query` 复用 original_query）。
- **若 `recovery_triggered` 且 evidence 非空**：强制 `unit_statuses[unit_id] = "requires_human"`，`result.issues.append({"kind": "low_confidence_recovery", "unit_id": unit_id, "reason": "evidence recovered via balanced-route retry"})`，**覆盖**正常 ready_to_render / supported 判定。即低置信草稿**必进人审**，即便 validator 判 supported。
- KB 路径 `_validated_evidence` 把 `metadata`（含 `low_confidence`）拷进 dict（`graph.py:447`），人审 UI 可见；project 路径 dict 不带 metadata，但 issue + ledger 已记录，足够人审定位。

### 3.5 策略开关：opt-in

`HarnessPolicy`：
- `allowed_tools` 默认**不含** `adaptive_recovery`（与 `requirement_fit_check` 同前例）。
- 新增 `max_adaptive_recovery_rounds: int = 0`（默认 0 = 关；镜像 `max_query_rewrite_rounds` 的"allowlist 门 + 预算字段"双开关前例）。`validate_budget` 校验 `≥ 0`。部署方启用需同时 `allowed_tools += ["adaptive_recovery"]` 且 `max_adaptive_recovery_rounds = 1`。

**opt-in 理由**（与 fit_check 一致的经验）：
1. 恢复是 **status-changing**（blocked → requires_human + 多一份草稿），虽方向安全（更多人审），但改变了默认行为。
2. 恢复做一次**真实 backend 检索**，默认开启会扰动集成测试（即便 run 级 status 不变——`blocked` 本就在 `waiting_human` 集合——unit 级断言可能变）。
3. 方案明确"需策略开关"。opt-in 即开关；后续验证无回归后可默认开启（与 fit_check 同路径）。

**零回归保证**：旧 policy 无 `adaptive_recovery` → graph 恢复分支不进 → 行为与阶段 4 完全一致。

## 4. 约束遵守（CLAUDE.md）

- **fail-closed**：恢复证据仍过 `_validated_evidence` + `build_knowledge_base_retrieval_outcome` 双重落域校验；`balanced_route` 只 drop source_group，不放宽冻结 `source_names`/`source_version_ids`。
- **不扩 writer/validator/suggester 输入面**：恢复只作用于检索层（graph `_retrieve_with_budget` + backend retrieve）；writer/validator 接口的 evidence 不变，只是多带 `low_confidence` metadata + unit 强制 `requires_human`。无"为让它工作而放宽写作者输入面"。
- **content-hash 寻址不变**：草稿仍由现有 writer 产出，artifact 寻址不受恢复影响；恢复不 mutate 已提交 artifact。
- **不破坏确定性**：`DocumentValidator` 逻辑不动；`low_confidence` → `requires_human` 是 graph 层 status 覆盖，不改 validator。

## 5. 任务分解（TDD，4 任务，每任务 red→green→commit）

| # | 任务 | 红→绿 测试 | 主要文件 |
|---|------|-----------|---------|
| 1 | Policy 字段 + tool 守门 | `tests/test_harness_policy.py`：`adaptive_recovery` 不在默认 allowlist；`max_adaptive_recovery_rounds` 默认 0；负值校验 | `models.py` |
| 2 | RAGFlow `balanced_route`（drop source_group） | `tests/test_ragflow_balanced_route.py`（仿 `test_ragflow_metadata_fallback.py` 的 `_Client`/`_Store` fake）：`balanced_route=True` 时 `metadata_condition` 不含 source_group、跨 group chunk 保留、`source_names` 仍过滤 | `ragflow_backend.py` |
| 3 | Graph 自适应恢复 + low_confidence 路由 | `tests/test_authoring_graph_adaptive_recovery.py`（仿 `test_retrieve_with_budget.py`/`test_authoring_graph_rerank_ledger.py`）：恢复触发条件、`relaxed=True` 传递、命中→`requires_human`+issue+ledger、未命中→原 blocked、opt-out→零触发；`_retrieve_with_budget` 返回签名**不变**（用 outcome.evidences metadata 传信号） | `harness/graph.py` |
| 4 | 闭包 wiring（KB+project `relaxed`）+ 集成回归 + 文档 | 集成回归（`test_full_generation_flow.py` 等默认 policy 不触发恢复，绿）；改进方案 §3 阶段 5 改写 + §7 状态表 | `app_pipeline.py`、改进方案 `.md` |

每任务提交信息以 `Co-Authored-By: Claude <noreply@anthropic.com>` 结尾。

## 6. 验收

- 默认 policy（无 `adaptive_recovery`）：行为与阶段 4 byte-identical（零回归，现有 163 测试全绿）。
- opt-in 启用后：原本 `success_empty` → `blocked` 的 `block_section` 字段，若 balanced 恢复命中冻结集内证据，则产出低置信草稿、`unit_status="requires_human"`、`issue.kind="low_confidence_recovery"`、ledger `recovery_triggered=True`；人审页可见证据与草稿。
- balanced 恢复未命中：维持原 `blocked`/`tbd`（无副作用）。
- 恢复证据全部在冻结 `source_names` 内（落域校验不触发 `PermissionError`）。
- `balanced_route` 不 drop `source_names`/kb/department，仅 drop source_group。

## 7. 不在范围内

- 多次恢复 / 恢复时改写查询（v1 一次 balanced）。
- 跨字段证据复用在恢复路径的额外加成（`CrossUnitEvidenceCache` 已在正常路径工作，恢复路径复用同一 registry，不特殊处理）。
- `low_confidence` 在 project 路径 validated dict 的 metadata 透传（project 路径 `_validated_evidence` 本就不带 metadata；issue + ledger 已足够人审，KB 路径 metadata 完整透传）。
- 恢复触发的 run 级 status 变化（`blocked` 与 `requires_human` 都映射 `waiting_human`，run 级不变）。
