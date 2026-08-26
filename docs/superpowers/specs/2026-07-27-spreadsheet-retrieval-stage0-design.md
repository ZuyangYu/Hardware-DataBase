# Spreadsheet 检索接通（阶段 0）设计

> 日期：2026-07-27
> 分支：`feature/template-upload-governed-authoring`
> 上游方案：`docs/Hardware-DataBase_文档生成改进方案.md` §3 阶段 0
> 状态：待实施

## 1. 背景与目标

知识库受控写作（文档生成）链路在冻结来源时纳入了 `.xlsx`（`create_knowledge_base_document_work_order` 把 `list_file_infos` 返回的全部 readable 文档名冻结进 `KnowledgeBaseSourceSnapshot`），但检索闭包 `_knowledge_base_retriever` 只调用 `RAGFlowBackend.retrieve`，后者只认 `processor_kind == RAGFLOW` 的文档。结果：被冻结的 spreadsheet 来源在生产期**永远查不到**，需要 BOM / 用量 / 参数等表格事实的字段静默判 `missing`/`blocked`，而非报错。这是一个 fail-silent 正确性缺口（上游方案 P1）。

**目标（阶段 0）**：让被冻结进 source set 的 `.xlsx` 通过 spreadsheet 结构化索引真正产出证据，且不破坏 evidence 落域校验（`_validated_evidence`）、`HarnessPolicy` allowlist 与来源冻结机制。改动隔离在 KB 检索闭包内，可独立交付、可回滚。

**非目标**：查询改写、空结果重试、capability-aware 多检索器分发、rerank、检索可观测性 ledger、草稿质量、自适应恢复（分别属上游方案阶段 1–5）；project-scoped 工单的同构改造（`_project_retriever` 同样只调 RAGFlow）列入后续，不在本阶段。

## 2. 现状关键事实（代码核查结论）

| 事实 | 位置 |
|---|---|
| 冻结来源不区分 processor_kind，含 `.xlsx` | `src/core/app_pipeline.py:426`（`list_file_infos` -> `store.list_documents`，不过滤 processor_kind） |
| 检索只走 RAGFlow 文本路径 | `src/core/app_pipeline.py:515` `backend.retrieve` |
| RAGFlow 检索过滤掉非 RAGFLOW 记录 | `src/pipelines/document_rag/ragflow_backend.py:855`（`record.processor_kind == PROCESSOR_KIND_RAGFLOW`） |
| spreadsheet 工具 `filters` 仅读 `record_id`，`source_names` 被忽略 | `src/agents/tools/spreadsheet_tools.py:52`、`:131` |
| `spreadsheet_service` 当前只注入给 agent，文档生成路径未持有 | `src/core/app_pipeline.py:57`（仅 `MultiSourceAgentRunner(...)` 形参） |
| 落域封装对 evidence 做 duck-typing 盖章 + 冻结集校验 | `src/document_authoring/service.py:1174` `build_knowledge_base_retrieval_outcome` |
| harness 落域校验要求 `metadata["knowledge_base_name"]==kb_name` 且 `source_name ∈ snapshot.source_names` | `src/document_authoring/harness/graph.py:266` `_validated_evidence` |
| Evidence 类型两路一致（`src.agents.state.Evidence`） | `src/agents/state.py:64` |

> 注：`build_knowledge_base_retrieval_outcome` 在 `source_name not in frozen_source_names` 时**抛 PermissionError**。若不先在闭包层过滤冻结集外的 spreadsheet evidence，该异常会**终止整个 run**。这是阶段 0 的核心正确性约束。

## 3. 设计

### 3.1 架构与边界

```
_knowledge_base_retriever 闭包（唯一改动点）
  │
  ├─ RAGFlowBackend.retrieve(query, filters={source_names})      ← 现状保留
  │
  └─ [新增] 若 tabular_lookup ∈ required_capabilities 且 service 可用:
        ├─ SpreadsheetSemanticTool.run(query, kb_name, ctx, top_k, filters={})
        ├─ 闭包层过滤: e.source_name ∈ frozen_source_names      ← 核心正确性防线
        └─ 追加到 evidences 末尾
  │
  └─ build_knowledge_base_retrieval_outcome(合并后的 evidences)  ← 现状保留，二次冻结集校验作双保险
```

**边界**：不改 `SpreadsheetSemanticTool`/`SpreadsheetCellTool`（agentic 问答路径共用，避免跨路径风险）；不改 `AuthoringGraph`、`HarnessPolicy`、`HarnessToolPolicy`、`KnowledgeBaseSourceSnapshot`、`build_knowledge_base_retrieval_outcome`、`_validated_evidence`。

### 3.2 改动清单

1. **`AppPipeline.__init__`**：新增 `self.spreadsheet_service = getattr(self.backend, "spreadsheet_indexes", None)`。当前 `self.backend.spreadsheet_indexes` 仅以形参传给 `self.agent`，文档生成路径未持有，需存为实例属性才能在闭包引用。

2. **`_knowledge_base_retriever` 闭包**：在 `backend.retrieve(...)` 之后，追加一段有条件 spreadsheet 检索：
   - 触发：`"tabular_lookup" in (requirement.required_capabilities or [])` **且** `self.spreadsheet_service is not None`。
   - 执行：构造 `SpreadsheetSemanticTool(self.spreadsheet_service)`，调用 `.run(query, kb_name, ctx, top_k=config.settings.FINAL_TOP_K, filters=None)`。
   - 过滤：`sp_evidences = [e for e in sp_evidences if e.source_name in frozen_source_names]` —— 冻结集外 evidence 在此剔除，不进入落域封装。
   - 合并：`evidences = ragflow_evidences + sp_evidences`（RAGFlow 在前；id 命名空间不同：RAGFlow chunk id vs `xlsx:<record>:<sheet>:<row>:semantic`，无需去重）。
   - 工具实例化时机：在 retriever 构造时（`_knowledge_base_retriever` 被调用、返回 `retrieve` 闭包之前）实例化一次，多个 unit 的 `retrieve` 调用复用同一工具实例，避免每 unit 重建。

3. **`SpreadsheetSemanticTool` import**：在 `app_pipeline.py` 顶部新增导入。

### 3.3 触发条件：仅 `tabular_lookup`

仅当 unit 的 `required_capabilities` 含 `tabular_lookup` 时追加 spreadsheet 检索。语义清晰、避免无谓 sqlite 查询，并与上游阶段 2 的 capability-aware 分发自然衔接。代价：schema 未标注 `tabular_lookup` 却需要表格事实的字段仍会漏 —— 由阶段 2 的 capability 推断 / schema 治理解决，不在本阶段。

### 3.4 source_names 过滤：闭包层兜底（不改共享工具）

不修改共享 spreadsheet 工具的 `filters` 语义。过滤责任在闭包层：
- 工具 `.run(filters=None)` 返回部门作用域内全部命中行；
- 闭包按 `frozen_source_names` 过滤后再合并；
- `build_knowledge_base_retrieval_outcome` 内部仍做冻结集校验，构成双保险。

**为何不改工具**：工具被 agentic 问答路径共用，改 `filters` 会扩大回滚边界；阶段 0 原则是"不动 policy/冻结机制、最小起步"。工具层的 `source_names` 支持留待阶段 2（RetrieverRegistry）一并处理。

### 3.5 降级

- `self.spreadsheet_service is None`（backend 未配置 spreadsheet 索引）→ 跳过 spreadsheet 检索，仅 RAGFlow（即现状行为）。
- 工具返回 `[]`（无命中或 sqlite 文件不存在）→ 不影响，仅 RAGFlow evidence。
- 工具内部异常被其自身 try/except 吞掉返回 `[]`（见 `spreadsheet_tools.py:69-79`）→ 同上，不向上抛。

### 3.6 query / top_k

- query 复用 RAGFlow 串：`" ".join(v for v in (subject, predicate, object_hint) if v)`。
- top_k 复用 `config.settings.FINAL_TOP_K`。

## 4. 测试策略

扩展 `tests/test_knowledge_base_document_work_orders.py`，新增用例：

1. **正向命中**：unit `required_capabilities=["tabular_lookup"]`，KB 仅含 `.xlsx`、spreadsheet 工具 mock 返回命中行 → 闭包产出 evidence，`source_name ∈ frozen`，`build_...` 不抛异常，outcome `success_with_hits`。
2. **冻结集过滤**：spreadsheet 工具 mock 返回 `source_name` 不在冻结集的 evidence → 被闭包剔除，不进 `build_...`，run 不炸。
3. **不触发**：unit 不含 `tabular_lookup` → spreadsheet 工具不被调用（mock 断言 `assert_not_called`）。
4. **降级**：`spreadsheet_service=None` → 闭包仅走 RAGFlow，行为同现状。
5. **现有回归**：`test_pipeline_knowledge_base_retriever_is_scoped` 不破坏。

测试通过 mock `pipeline._knowledge_base_retriever` 的既有模式注入（见 `test_knowledge_base_document_work_orders.py:222`），spreadsheet 工具以 `Mock` 替换或 monkeypatch `SpreadsheetSemanticTool`。

## 5. 验收标准

- 含 `tabular_lookup` 的字段在仅有 `.xlsx` 的 KB 里能命中证据，unit 状态从 `blocked` 变 `ready_to_render`。
- 冻结后 KB 新增 `.xlsx`（不在冻结集）→ 其 evidence 被过滤，不触发 PermissionError，run 正常完成。
- 不需要表格事实的字段（无 `tabular_lookup`）行为与现状一致。
- `spreadsheet_service` 未配置时行为与现状一致。
- 全部新增 / 回归测试通过。

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| spreadsheet token LIKE 召回弱 | 阶段 0 命中仍受限 | 阶段 1 查询改写串同时喂给 spreadsheet 工具缓解；本阶段不解决 |
| 每个 `tabular_lookup` 字段多一次 per-dept sqlite 查询 | 轻微延迟 | 规模有限（受 `max_units_per_run` 约束）；可接受 |
| 闭包层过滤遗漏 → 冻结集外证据进 `build_...` | run 被 PermissionError 终止 | `build_...` 内部二次校验作双保险；测试用例 2 专门覆盖 |
| 共享工具行为变更波及 agentic 路径 | 跨路径回归 | 本阶段不改共享工具，零跨路径风险 |

## 7. 对上游改进方案 .md 的修正

本阶段实施同时修正 `docs/Hardware-DataBase_文档生成改进方案.md` 中经核查发现的问题（详见 §8 修正清单）。这些修正是"实施"的一部分，与代码改动一并提交。

## 8. 上游方案 .md 修正清单

1. 阶段 0 改动点 1：`filters={"source_names": frozen}` 对 spreadsheet 工具无效 → 改为"闭包层合并前按冻结集过滤"。
2. 阶段 0 改动点 3：AppPipeline 注入前提修正 —— `spreadsheet_service` 当前只注入给 `self.agent`，文档生成路径未持有，需新增 `self.spreadsheet_service`。
3. P1 行号 `ragflow_backend.py:851` → `855`。
4. `_validated_evidence` 行号统一为 `graph.py:266`（附录 §3 阶段 0 引 283 有误，283 是函数体内校验行）。
5. lease 备注：`lease_seconds=60` 只是 `models.py` 默认值；实际 schema 自动策略由 `_schema_harness_policy` 推导为 `max(300, unit_count*120)`（`service.py:1042`）。结论（非缺陷）不变。
6. P8（字段间无证据复用）显式标注"阶段 0–5 暂不闭环，并入阶段 2 Coordinator 跨单元 evidence 缓存"。
7. project 路径同构缺口标注：`_project_retriever` 同样只调 RAGFlow，同构改造列入阶段 2 / 后续。
8. 阶段 1 补 policy 版本治理：`allowed_tools` 增加 `rewrite_query` 需注册并审批新 policy 版本（工单冻结版本，旧工单不追溯）。
9. 阶段 3 补现成锚点：`graph.py:39` 预留的 `retrieval_ledger` 字段（从未写入）与 `graph.py:124` matrix row 的 per-source `diagnostics` 可直接复用。
10. 阶段 4.2 降级：`LLMManagedWriter._build_user_prompt` 已把含全部 evidence 的 request 传给 LLM（`managed.py:235`），"确认使用全部 evidence"现状即满足，改为回归锁定项。
