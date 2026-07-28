# Capability-aware 检索分发（阶段 2）设计

> 日期：2026-07-27
> 分支：`feature/template-upload-governed-authoring`
> 上游方案：`docs/Hardware-DataBase_文档生成改进方案.md` §3 阶段 2
> 状态：待实施

## 1. 背景与目标

阶段 0 在 KB 检索闭包内用一段硬编码 `if "tabular_lookup" in required_capabilities` 接通了 spreadsheet 检索，闭环了 P1。但这段分派是写死的单 capability 特例，未泛化：

- **P4（`required_capabilities` 形同虚设）**：除 `tabular_lookup` 外，`document_claim_lookup`/`entity_lookup`/`relationship_lookup`/`revision_lookup` 声明了却不影响检索器选择，全压给 RAGFlow 文本检索。`_project_retriever`（project-scoped 工单）甚至完全没有 spreadsheet 分派，存在与 P1 同构的表格不可达缺口（上游方案 §2 P1 注释明列其改造入阶段 2）。
- **P7（`preferred_source_roles` 忽略）**：schema 声明了来源角色偏好却从不参与排序/加分。
- **P8（字段间无证据复用）**：每字段独立检索，A 字段的命中无法辅助 B 字段；上游方案 §2 P8 注释明列"并入阶段 2 Coordinator 跨单元 evidence 缓存"。

**目标（阶段 2）**：用 `RetrieverRegistry` 把 `required_capabilities` 真正路由到对应检索器（泛化阶段 0 的硬编码），合并去重（按 content 哈希），落实 `preferred_source_roles` 加分，并引入跨单元 evidence 复用缓存；同时把 spreadsheet 分派补进 project 路径。改动仍在"冻结-校验-预算"框架内，不破坏 `_validated_evidence` 落域校验与 `HarnessPolicy` allowlist。

**非目标**：rerank 与检索可观测性 ledger（阶段 3）、草稿质量（阶段 4）、自适应恢复（阶段 5）；`revision_lookup` 的专用检索器（本阶段预留，仍走 RAGFlow 默认）。

## 2. 现状关键事实（代码核查结论）

| 事实 | 位置 |
|---|---|
| KB 闭包硬编码 tabular_lookup 分派 | `src/core/app_pipeline.py:541` |
| KB 闭包 query 串构造 | `src/core/app_pipeline.py:522` |
| project 闭包 `retrieve_one` 仅调 RAGFlow（无 spreadsheet） | `src/core/app_pipeline.py:576` |
| project 闭包包成 EvidenceEnvelope（带 document_role） | `src/core/app_pipeline.py:601` |
| project 检索的 per-version 落域校验（安全边界） | `src/projects/retrieval.py:70`（`project_id`/`source_version_id`/`processing_artifact_id` 三重校验） |
| Evidence 两型：KB 路径用 `src.agents.state.Evidence`（Pydantic，有 `content_kind`/`processor_kind`/`locator`） | `src/agents/state.py:64` |
| project 路径用 `EvidenceEnvelope`（dataclass，有 `document_role`/`content_hash`） | `src/pipelines/document_rag/schemas.py:72` |
| capability 受限集合 | `src/document_authoring/harness/graph.py:314` `_capabilities` |
| `InformationRequirement.required_capabilities` / `preferred_source_roles` | `src/agents/claim_evidence.py:67` |
| `build_knowledge_base_retrieval_outcome` 二次冻结集校验（抛 PermissionError） | `src/document_authoring/service.py:1187` |
| 闭包在一次 run 内复用（registry 跨 unit 持久化可行） | `src/core/app_pipeline.py:467`（闭包构造一次，传给 harness，per-unit 调用） |

> **关键约束**：project 路径的 `ProjectEvidenceRetrievalService.retrieve`（`retrieval.py:32`）对每条 evidence 强制 `project_id == snapshot.project_id` 且 `source_version_id == version_id` 且 `processing_artifact_id in artifacts`。这是安全边界，**不可绕过**。因此 project 路径的 spreadsheet 注入必须在 `retrieve_one` 内完成，并把 spreadsheet evidence 绑定到当前 version 的 `source_version_id` + `processing_artifact_id`，使其通过校验（与 RAGFlow evidence 同构构造）。

## 3. 设计

### 3.1 架构与边界

```
新增 src/document_authoring/retriever_registry.py
  ├─ RetrieverRegistry
  │    ├─ default_retriever: Callable[[str, InformationRequirement], list[Evidence]]  (RAGFlow，始终调用)
  │    ├─ specialized: dict[capability, Callable[..., list[Evidence]]]                (tabular_lookup -> spreadsheet)
  │    ├─ cross_unit_cache: CrossUnitEvidenceCache                                    (P8)
  │    └─ retrieve(requirement, query) -> list[Evidence]
  │         1. 始终调 default_retriever
  │         2. 对 requirement.required_capabilities 中已注册的 specialized 逐一分派
  │         3. 合并 -> dedup_by_content(keep highest score) -> apply_role_boost(preferred_source_roles)
  │         4. 若结果为空：cross_unit_cache.offer(requirement, query) 回填复用证据（P8）
  │         5. cross_unit_cache.ingest(fresh) 累积供后续 unit 复用
  │
  └─ 共享后处理函数（KB 与 project 路径都用）
       ├─ dedup_by_content(evidences) -> list
       ├─ apply_role_boost(evidences, preferred_roles, factor) -> list
       └─ CrossUnitEvidenceCache（content_hash -> evidence，offer/ingest）

KB 路径：_knowledge_base_retriever 闭包构造 RetrieverRegistry（default=RAGFlow, tabular_lookup=spreadsheet+冻结集过滤），调用 registry.retrieve
project 路径：retrieve_one 内补 spreadsheet 分派（绑 version_id+artifact），闭包对 outcome 做 dedup+role_boost+cache 后处理
```

**边界**：不改 `AuthoringGraph`、`HarnessPolicy`、`HarnessToolPolicy`、`_validated_evidence`、`build_knowledge_base_retrieval_outcome`、`ProjectEvidenceRetrievalService.retrieve`、`SpreadsheetSemanticTool`。不改 `InformationRequirement` 模型。

### 3.2 capability -> 检索器映射

| capability | 检索器 | 说明 |
|---|---|---|
| `document_claim_lookup` / `entity_lookup` / `relationship_lookup` | RAGFlow（default） | 默认始终调用，覆盖文本类事实 |
| `tabular_lookup` | spreadsheet 工具（specialized） | 额外追加，仅当 `spreadsheet_service` 可用 |
| `revision_lookup` | 预留（走 default） | 本阶段无专用检索器，仍 RAGFlow |

**分派语义（关键决策）**：RAGFlow 作为 **default 始终调用**（保留阶段 0 行为、不回归文本召回），specialized 检索器按声明 capability **叠加**调用。即 `tabular_lookup` 字段 = RAGFlow + spreadsheet（与阶段 0 一致），`document_claim_lookup` 字段 = 仅 RAGFlow，`required_capabilities=[]` 字段 = 仅 RAGFlow。这满足验收"不同 capability 走不同检索器"（tabular 多一路 spreadsheet），且零回归。

### 3.3 合并去重（改动点 2，按 content 哈希）

- 去重键：`hashlib.sha256(evidence.content.strip().encode("utf-8")).hexdigest()`（`EvidenceEnvelope.content_hash` 已有同算法，但 KB 路径 `state.Evidence` 无此字段，统一用函数计算）。
- 冲突保留：`score` 高者；并列保留首现。
- 仅在单个 unit 的多检索器合并时去重（RAGFlow chunk 与 spreadsheet 行 content 不同，故实际碰撞少；为正确性而设）。

### 3.4 preferred_source_roles 加分（改动点 3，闭环 P7）

- 在合并去重后、返回前，对每条 evidence：若其来源角色命中 `requirement.preferred_source_roles`，`score *= PREFERRED_ROLE_BOOST_FACTOR`（默认 1.5），并打 `metadata["preferred_source_role_match"] = role`。
- 角色来源（duck-typing，兼容两型 Evidence）：`getattr(evidence, "document_role", None) or evidence.metadata.get("document_role")`。
  - KB 路径 `state.Evidence` 无 `document_role` -> 通常无角色信息 -> 加分为 no-op（无害）。
  - project 路径 `EvidenceEnvelope.document_role` 有值 -> 加分生效。
- 纯确定性加分，无 LLM/外部调用，不需 `allowed_tools` 守门。

### 3.5 跨单元 evidence 复用缓存（P8）

- `CrossUnitEvidenceCache`：`content_hash -> evidence`，在闭包内**一次 run 一个实例**（闭包跨 unit 持久化）。
- `ingest(evidences)`：把当前 unit 的命中证据按 content_hash 累积。
- `offer(requirement, query)`：仅当当前 unit 新检索**结果为空**时调用；扫描缓存，返回 content 与 query 有词项重叠的证据，逐条打 `metadata["reused_from_unit"] = <unit_id>`、`metadata["reused"] = True`，数量受 `max_reuse_per_unit`（默认 5）约束。
- **仅在空结果时回填**：fresh 命中时不引入复用证据，避免噪声；空结果时复用是把"判 missing/blocked"降级为"低置信命中交人工"，与阶段 5 自适应恢复衔接（复用证据带 `reused` 标，阶段 4 的 `requirement_fit_check` 可据此加严）。
- 复用证据已在前序 unit 通过落域校验（同 run、同冻结集、同 kb_name），不破坏落域正确性。
- **无需 policy 守门**：复用是检索内部优化，使用同冻结集内已校验证据，不引入新外部工具/LLM/数据源；与阶段 0 spreadsheet 非守门接入同前例。旧工单同样受益（修复性质，非新能力）。

### 3.6 KB 路径改造（泛化阶段 0）

`_knowledge_base_retriever` 闭包：
- 构造时实例化 `RetrieverRegistry`：
  - `default_retriever = lambda q, req: self.backend.retrieve(kb_name, q, top_k=FINAL_TOP_K, ctx=ctx, filters={"source_names": frozen_source_names})`
  - 若 `spreadsheet_service` 可用：`specialized["tabular_lookup"] = lambda q, req: [e for e in SpreadsheetSemanticTool(self.spreadsheet_service).run(q, kb_name, ctx, top_k=FINAL_TOP_K, filters=None) if e.source_name in frozen_source_names]`（冻结集过滤在 specialized 内完成，不进 `build_...`）
  - cross_unit_cache 实例一个
- `retrieve(requirement, _attempt, query_override=None)`：`query = query_override or _join(req)`；`evidences = registry.retrieve(requirement, query)`；`return build_knowledge_base_retrieval_outcome(kb_name, frozen, evidences, ...)`。
- 阶段 0 的 5 个 KB retriever 测试必须保持绿（行为不变：tabular_lookup 加 spreadsheet、无 tabular_lookup 不调、service 缺失降级、冻结集外丢弃、query_override）。

### 3.7 project 路径改造（闭环同构缺口）

`_project_retriever` 闭包：
- `retrieve_one(version_id, artifact_ids, region_policies)` 内，RAGFlow 检索之后，若 `"tabular_lookup" in requirement.required_capabilities` 且 `self.spreadsheet_service is not None`：
  - `sp = SpreadsheetSemanticTool(self.spreadsheet_service).run(query, source_kb_name, ctx, top_k=FINAL_TOP_K, filters=None)`
  - 过滤 `e.source_name == document.title`（与 RAGFlow 的 `source_names=[document.title]` 同口径，保证只归属当前 version）
  - 逐条包成 `EvidenceEnvelope`，**镜像**现有 RAGFlow envelope 构造（`project_id`/`source_version_id=version_id`/`processing_artifact_id=artifact_ids[0] if len==1 else None`/`document_role=document.document_role`/`revision`/`approval_status`），使其通过 `retrieval.py:70` 落域校验。
  - `result.extend(...)`。
- 闭包对 `project_retrieval.retrieve(...)` 返回的 outcome 做**后处理**（共享函数）：`dedup_by_content` + `apply_role_boost(requirement.preferred_source_roles)` + `cross_unit_cache`（offer on empty、ingest on hits）。后处理只动 `outcome.evidences`（已校验证据），不改 `source_outcomes`/`applied_*`。

### 3.8 降级

- `spreadsheet_service is None`：specialized 不注册 / 不分派 -> 仅 RAGFlow（现状）。
- spreadsheet 工具返回 `[]` 或异常（其自身 try/except 吞掉返 `[]`）：不影响，仅 RAGFlow。
- 跨单元缓存 offer 无重叠：返回 `[]`，unit 仍判空（与无缓存一致）。
- project 路径 `document.title` 与 xlsx 文件名不一致：spreadsheet 过滤为空 -> 该 version 无 spreadsheet 证据（不报错，与 RAGFlow 查不到同语义）。

## 4. 测试策略

### 4.1 新增 `tests/test_retriever_registry.py`（Task 1，fake 检索器，无 backend）
1. `default_always_invoked`：空 capabilities 仍调 default。
2. `specialized_dispatched_for_tabular`：tabular_lookup 触发 specialized + default。
3. `dedup_by_content_keeps_highest_score`：两检索器返回同 content 不同 score -> 保留高分。
4. `role_boost_applies_to_matching_role`：preferred_source_roles 命中 -> score 提升 + metadata 标记。
5. `role_boost_noop_without_role`：无角色信息 -> 不变。
6. `cross_unit_cache_offers_on_empty`：unit A 命中、unit B 空 + query 词项重叠 -> B 回填复用证据带 `reused_from_unit`。
7. `cross_unit_cache_no_offer_when_fresh_hits`：unit B 有命中 -> 不引入复用。
8. `cross_unit_cache_caps_reuse`：复用数受 `max_reuse_per_unit` 限制。
9. `revision_lookup_falls_back_to_default`：revision_lookup 无 specialized -> 仅 default。

### 4.2 扩展 `tests/test_knowledge_base_document_work_orders.py`（Task 2）
- 阶段 0 的 5 个 retriever 测试保持绿（行为不变）。
- 新增：`test_kb_retriever_dedups_across_ragflow_and_spreadsheet`（两路同 content -> 去重）。
- 新增：`test_kb_retriever_cross_unit_reuse_on_empty`（两 unit 跨 retrieve 调用，第二 unit 空时回填）。

### 4.3 扩展 project retriever 测试（Task 3）
- 新增：`test_project_retriever_adds_spreadsheet_for_tabular_lookup`（xlsx source version + tabular_lookup -> outcome 含 spreadsheet envelope，`source_version_id`/`processing_artifact_id` 正确，落域校验通过）。
- 新增：`test_project_retriever_role_boost_for_preferred_source_roles`（preferred_source_roles 命中 document_role -> score 提升）。
- 新增：`test_project_retriever_skips_spreadsheet_without_tabular_lookup`（无 tabular_lookup -> spreadsheet 工具不调）。
- 现有 project retriever 回归不破坏。

## 5. 验收标准

- 不同 capability 的字段走不同检索器：`tabular_lookup` 字段命中 spreadsheet 证据（KB 与 project 路径均成立）；非 tabular 字段不调 spreadsheet。
- 同 content 证据跨检索器去重，保留高分。
- `preferred_source_roles` 命中来源角色时该证据 score 提升（project 路径可验证）。
- 跨单元：A 字段命中、B 字段空且查询相关时，B 回填 A 的证据（带 `reused` 标）；B 有命中时不引入复用。
- project 路径 spreadsheet 证据通过 `retrieval.py:70` 落域校验（`source_version_id`/`processing_artifact_id` 绑定正确），不触发 `filter_unsupported`。
- 阶段 0/1 全部回归测试保持绿；无 HarnessPolicy/版本/预算变动。

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| project 路径 spreadsheet 绕过落域校验 | 安全边界破坏 | 在 `retrieve_one` 内绑定 version_id+artifact，镜像 RAGFlow envelope 构造；测试 4.3 专测落域通过 |
| 跨单元复用引入噪声 | B 字段得到不相关证据 | 仅空结果时回填 + 词项重叠门槛 + `max_reuse_per_unit` 上限 + `reused` 标供阶段 4 校验加严 |
| role boost 因子破坏既有排序 | 排序变化 | 仅对命中角色加分、纯确定性；KB 路径无角色信息时 no-op；阶段 3 rerank 会重排，本阶段加分仅为轻量偏好 |
| 两型 Evidence 字段差异 | AttributeError | duck-typing 取 role；去重用 content 函数计算，不依赖 `content_hash` 字段 |
| project 路径 per-version 重复调 spreadsheet 工具 | 轻微延迟 | sqlite LIKE 快、仅 tabular_lookup unit、规模受 `max_units_per_run`+version 数约束；可接受 |

## 7. 对上游改进方案 .md 的修正

实施时同步修正 `docs/Hardware-DataBase_文档生成改进方案.md`：
1. §3 阶段 2 改动点 1 补"RAGFlow 作为 default 始终调用、specialized 叠加"的分派语义（避免被读成"tabular_lookup 仅走 spreadsheet 不走 RAGFlow"）。
2. §3 阶段 2 补 project 路径改造的落域约束说明（spreadsheet evidence 须绑 version_id+artifact 过 `retrieval.py:70` 校验）。
3. §3 阶段 2 改动点 3 补"KB 路径无 document_role 时加分为 no-op"。
4. §7 实施状态表标记阶段 2 已实施。
