# Capability-aware 检索分发（阶段 2）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-27-capability-dispatch-stage2-design.md`

**Goal:** `RetrieverRegistry` 按 `required_capabilities` 分派检索器（泛化阶段 0 硬编码），合并去重（content 哈希），落实 `preferred_source_roles` 加分，引入跨单元 evidence 复用缓存；project 路径补 spreadsheet 分派（绑 version_id+artifact 过落域校验）。

**Architecture:** 新增 `src/document_authoring/retriever_registry.py`（`RetrieverRegistry` + 共享后处理函数 `dedup_by_content`/`apply_role_boost`/`CrossUnitEvidenceCache`）。KB 闭包构造 registry 调 `registry.retrieve`；project 闭包在 `retrieve_one` 内补 spreadsheet 分派、对 outcome 做后处理。两路径共用后处理函数。

**Tech Stack:** Python 3，pytest，unittest.mock；`InformationRequirement`、`state.Evidence`、`EvidenceEnvelope`、`SpreadsheetSemanticTool`。

## Global Constraints

- 不修改 `InformationRequirement`、`AuthoringGraph`、`HarnessPolicy`、`HarnessToolPolicy`、`_validated_evidence`、`build_knowledge_base_retrieval_outcome`、`ProjectEvidenceRetrievalService.retrieve`、`SpreadsheetSemanticTool`。
- 无 HarnessPolicy / allowed_tools / 版本 / 预算变动（registry 是闭包内部检索优化，与阶段 0 spreadsheet 非守门接入同前例）。
- project 路径 spreadsheet evidence 必须绑 `source_version_id` + `processing_artifact_id`，镜像 RAGFlow envelope 构造，过 `retrieval.py:70` 落域校验。
- 跨单元复用仅在空结果时回填，带 `reused`/`reused_from_unit` 标，受 `max_reuse_per_unit` 约束。
- 两型 Evidence（`state.Evidence` / `EvidenceEnvelope`）duck-typing 取 role；去重用 content 函数计算。
- TDD：每任务先写失败测试再实现再提交。提交信息带 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- `docs/superpowers/` 被 gitignore，新文件需 `git add -f`。

---

### Task 1: RetrieverRegistry + 共享后处理 + 单测

**Files:**
- Create: `src/document_authoring/retriever_registry.py`
- Test: `tests/test_retriever_registry.py`

**Interfaces:**
- Consumes: `InformationRequirement`（`src/agents/claim_evidence.py:58`）、`state.Evidence`（`src/agents/state.py:64`）。
- Produces:
  - `RetrieverRegistry(default_retriever, specialized=None, cross_unit_cache=None, role_boost_factor=1.5, max_reuse_per_unit=5)`，`.retrieve(requirement, query) -> list[Evidence]`。
  - `dedup_by_content(evidences) -> list[Evidence]`（保留高分）。
  - `apply_role_boost(evidences, preferred_roles, factor=1.5) -> list[Evidence]`。
  - `CrossUnitEvidenceCache(max_reuse_per_unit=5)`，`.ingest(evidences, unit_id)` / `.offer(requirement, query, unit_id) -> list[Evidence]`。
  - `content_hash(evidence) -> str`。

- [ ] **Step 1: Write the failing tests**

创建 `tests/test_retriever_registry.py`，覆盖 spec §4.1 的 9 个用例。用 `types.SimpleNamespace` 或 `state.Evidence` 造 fake evidence，fake 检索器用闭包/`Mock`。

```python
from __future__ import annotations

from unittest.mock import Mock

from src.agents.claim_evidence import InformationRequirement
from src.agents.state import Evidence
from src.document_authoring.retriever_registry import (
    CrossUnitEvidenceCache,
    RetrieverRegistry,
    apply_role_boost,
    content_hash,
    dedup_by_content,
)


def _req(subject="X", caps=None, roles=None):
    return InformationRequirement(
        requirement_id="r", semantic_unit_id="field:f1", claim_type="attribute",
        subject=subject, required_capabilities=caps or [], preferred_source_roles=roles or [],
    )


def _ev(content="c", score=0.5, source_name="s.pdf", role=None):
    return Evidence(
        id=content, content=content, source_name=source_name,
        content_kind="document_text", processor_kind="ragflow",
        score=score, metadata={"document_role": role} if role else {},
    )
```

用例要点：
1. `test_default_always_invoked`：`caps=[]`，default Mock 被调一次，返回其 evidence。
2. `test_specialized_dispatched_for_tabular`：`caps=["tabular_lookup"]`，default + specialized 都被调，结果合并。
3. `test_dedup_keeps_highest_score`：default、specialized 返回同 content 不同 score -> 去重后 1 条、高分。
4. `test_role_boost_applies_to_matching_role`：`preferred_source_roles=["spec"]`，evidence `document_role="spec"` -> score*1.5 + metadata 标记。
5. `test_role_boost_noop_without_role`：无 role -> 不变。
6. `test_cross_unit_cache_offers_on_empty`：unit A ingest 命中、unit B fresh 空 + query 词项重叠 -> offer 返回带 `reused_from_unit` 的证据。
7. `test_cross_unit_cache_no_offer_when_fresh_hits`：B 有命中 -> offer 不被调（registry 内部，fresh 非空时跳过）。
8. `test_cross_unit_cache_caps_reuse`：缓存多条匹配 -> offer 受 `max_reuse_per_unit` 限制。
9. `test_revision_lookup_falls_back_to_default`：`caps=["revision_lookup"]` 无 specialized -> 仅 default。

- [ ] **Step 2: Implement `retriever_registry.py`**

实现 `content_hash`、`dedup_by_content`（按 hash 分组保留 max score）、`apply_role_boost`（duck-typing role）、`CrossUnitEvidenceCache`（`ingest`/`offer`，offer 用 query 词项与 content 的集合交集非空判定，逐条打标）、`RetrieverRegistry.retrieve`（default 始终调 + specialized 按 caps 调 -> 合并 -> dedup -> boost -> 空则 offer -> ingest）。

词项切分：`set(query.lower().split())` 与 `set(content.lower().split())` 交集非空（中文不分词，按空白切；足够防噪声，阶段 3 rerank 再精排）。

- [ ] **Step 3: Run tests, commit**

`pytest tests/test_retriever_registry.py -q` 全绿后提交：`feat: add RetrieverRegistry for capability-aware retrieval dispatch`。

---

### Task 2: 接入 KB 闭包（泛化阶段 0）

**Files:**
- Modify: `src/core/app_pipeline.py`（`_knowledge_base_retriever`）
- Test: `tests/test_knowledge_base_document_work_orders.py`

**Interfaces:**
- `_knowledge_base_retriever` 内构造 `RetrieverRegistry`：default=RAGFlow lambda、specialized[tabular_lookup]=spreadsheet+冻结集过滤 lambda、cross_unit_cache 一个实例。
- `retrieve` 闭包：`query = query_override or _join(...)`；`evidences = registry.retrieve(requirement, query)`；`return build_knowledge_base_retrieval_outcome(...)`。

- [ ] **Step 1: Write/extend failing tests**

扩展 `tests/test_knowledge_base_document_work_orders.py`：
- 确认阶段 0 的 5 个 retriever 测试（`:919`/`:936`/`:951`/`:968`/`:990`）仍绿（行为不变）。
- 新增 `test_kb_retriever_dedups_across_ragflow_and_spreadsheet`：backend.retrieve 与 spreadsheet_tool 都返回 content="BOM row" -> outcome.evidences 去重为 1 条。
- 新增 `test_kb_retriever_cross_unit_reuse_on_empty`：同一 retrieve 闭包两次调用；第一次 tabular_lookup 命中、第二次（不同 subject、空 fresh + query 词项重叠）回填复用证据带 `reused_from_unit`。

- [ ] **Step 2: Refactor `_knowledge_base_retriever`**

把 `:541` 的硬编码 `if tabular_lookup` 替换为构造 `RetrieverRegistry` 并调 `registry.retrieve`。保留 `build_knowledge_base_retrieval_outcome` 二次校验。spreadsheet 工具实例化时机不变（闭包构造时一次）。

- [ ] **Step 3: Run tests, commit**

`pytest tests/test_knowledge_base_document_work_orders.py -q` 全绿后提交：`feat: route KB retrieval through RetrieverRegistry`。

---

### Task 3: 接入 project 路径（闭环同构缺口）

**Files:**
- Modify: `src/core/app_pipeline.py`（`_project_retriever`）
- Test: 找到/新建 project retriever 测试文件

**Interfaces:**
- `retrieve_one` 内 RAGFlow 之后：若 `tabular_lookup in requirement.required_capabilities` 且 `self.spreadsheet_service` -> 调 `SpreadsheetSemanticTool(self.spreadsheet_service).run(query, source_kb_name, ctx, top_k=FINAL_TOP_K, filters=None)`，过滤 `e.source_name == document.title`，镜像 RAGFlow envelope 构造（`source_version_id=version_id`/`processing_artifact_id=artifact_ids[0] if len==1 else None`/`document_role`/`revision`/`approval_status`），`result.extend`。
- 闭包对 `project_retrieval.retrieve(...)` 返回 outcome 做后处理：`dedup_by_content` + `apply_role_boost(requirement.preferred_source_roles)` + `CrossUnitEvidenceCache`（offer on empty、ingest on hits）。cross_unit_cache 实例在闭包内一个。

- [ ] **Step 1: Locate project retriever tests**

`grep -rn "_project_retriever\|project_retrieval.retrieve" tests/`，确认现有 project retriever 测试位置与 mock 模式。

- [ ] **Step 2: Write failing tests**

新增：
- `test_project_retriever_adds_spreadsheet_for_tabular_lookup`：xlsx source version + tabular_lookup -> outcome 含 spreadsheet envelope，`source_version_id`/`processing_artifact_id` 正确，`source_outcomes` 无 `filter_unsupported`。
- `test_project_retriever_role_boost_for_preferred_source_roles`：preferred_source_roles 命中 `document_role` -> score 提升。
- `test_project_retriever_skips_spreadsheet_without_tabular_lookup`：无 tabular_lookup -> spreadsheet 工具不调。
- 现有 project retriever 回归不破坏。

- [ ] **Step 3: Implement `_project_retriever` changes**

在 `retrieve_one` 内补 spreadsheet 分派（镜像 envelope 构造）；闭包加后处理。注意 `EvidenceEnvelope` 是 dataclass，`apply_role_boost`/`dedup_by_content` 需 duck-typing 兼容（`.content`/`.score`/`.metadata`/`getattr(., 'document_role', None)`）。

- [ ] **Step 4: Run tests, commit**

`pytest <project retriever tests> -q` 全绿后提交：`feat: add spreadsheet dispatch to project retriever path`。

---

### Task 4: 更新改进方案 .md + 最终回归

**Files:**
- Modify: `docs/Hardware-DataBase_文档生成改进方案.md`

- [ ] **Step 1: Apply doc corrections (spec §7)**

1. §3 阶段 2 改动点 1 补"default 始终调用、specialized 叠加"分派语义。
2. §3 阶段 2 补 project 路径落域约束（绑 version_id+artifact 过 `retrieval.py:70`）。
3. §3 阶段 2 改动点 3 补"KB 路径无 document_role 时加分为 no-op"。
4. §7 实施状态表标记阶段 2 已实施。

- [ ] **Step 2: Final regression**

`pytest tests/test_retriever_registry.py tests/test_knowledge_base_document_work_orders.py tests/test_retrieve_with_budget.py tests/test_query_rewriter.py tests/test_harness_policy.py tests/test_document_authoring_p2a.py tests/test_full_generation_flow.py <project retriever tests> -q` 全绿。

- [ ] **Step 3: Commit doc**

`git add -f` spec/plan；提交：`docs: mark stage 2 (capability dispatch) as implemented`。

---

## Rollback

每任务独立提交，可逐 commit 回滚。Task 1 是纯新增模块（无副作用）；Task 2/3 改闭包，回滚后恢复阶段 0/1 行为（KB 仍走硬编码 spreadsheet、project 仅 RAGFlow）。
