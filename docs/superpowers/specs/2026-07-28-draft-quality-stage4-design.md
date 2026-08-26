# 草稿质量（阶段 4）设计

> 日期：2026-07-28
> 分支：`feature/template-upload-governed-authoring`
> 上游方案：`docs/Hardware-DataBase_文档生成改进方案.md` §3 阶段 4
> 状态：待实施

## 1. 背景与目标

阶段 3 让送 writer 的证据更相关、可观测。但草稿层仍有两个缺口：

- **P5（`DeterministicEvidenceWriter` 是占位）**：`_deterministic_draft` 取 `request.evidence[0]` 逐字复制（`managed.py:52`），忽略其余证据。多证据字段下草稿=首个 chunk，非综合答案。该函数同时是 `LLMManagedWriter` 持续失败后的 fallback（`managed.py:130`），故非 LLM 部署与 LLM 失败兜底都受影响。
- **P10（缺 draft-requirement 语义契合校验）**：`DocumentValidator.validate_unit_draft` 只做确定性 lexical anchor + evidence 归属校验（`validator.py:48`），不判定"草稿是否真的回答了该字段需求"；不契合的草稿仍可 `supported` 直接送渲染。

**目标（阶段 4）**：
1. `DeterministicEvidenceWriter` 升级：多证据时**结构化汇总全部证据**（枚举拼接，不捏造，带多证据引用）；单证据保持原样（兼容现有断言）。同时改善 LLM fallback 兜底草稿。
2. 回归锁定 `LLMManagedWriter._build_user_prompt` 已传**全部** evidence（`managed.py:235`）-- 现状即满足，加回归测试锁定。
3. 新增 `RequirementFitChecker`（LLM-as-judge，仿 `EvidenceReranker`）：判定草稿是否回答 requirement，未通过置 `requires_human`。受 `allowed_tools` 守门，旧 policy/LLM 不可用降级 pass。

**非目标**：自适应恢复（阶段 5）；改 `LLMManagedWriter` 的 LLM 主路径（已用全部 evidence）；改 `DocumentValidator` 的确定性校验逻辑（fit check 是独立的 LLM 质量门，不混入确定性 validator）。

**对上游方案 .md 的偏离说明**：上游方案 §3 阶段 4 改动点 3 写"validator 增加 `requirement_fit_check`"。本设计改为独立 `RequirementFitChecker` 注入 graph（仿 Stage 1/3 的 `QueryRewriter`/`EvidenceReranker`）。理由：`DocumentValidator` 是纯确定性、无 LLM、无 policy 感知对象；fit check 是 LLM 门控质量门，与 rerank 同型，独立注入 + allowlist 守门 + 失败降级更一致，且不污染确定性 validator 的构造与语义。spec §7 会回写此修正。

## 2. 现状关键事实（代码核查结论）

| 事实 | 位置 |
|---|---|
| `_deterministic_draft` 取 `evidence[0]` 逐字复制 | `src/document_authoring/writers/managed.py:49-71` |
| 同函数是 `LLMManagedWriter` 持续失败 fallback | `src/document_authoring/writers/managed.py:130` |
| `LLMManagedWriter._build_user_prompt` 传 `request.model_dump_json()`（含全部 evidence） | `src/document_authoring/writers/managed.py:230-250` |
| `validate_unit_draft` 确定性 lexical anchor + evidence 归属 | `src/document_authoring/validator.py:48-78` |
| graph 校验流：validate_unit_draft -> detect_template_contamination -> (supported?) ready_to_render | `src/document_authoring/harness/graph.py:146-164` |
| `validate_cross_unit_consistency` 按 `consistency_key` 跨 draft 聚合 value，多值即 conflict | `src/document_authoring/validator.py:96-109` |
| `WriterRequest` 不携带 `InformationRequirement`（graph 持有，loop 变量 `requirement`） | `src/document_authoring/writers/provider.py` + `graph.py:118` |
| Stage 1/3 LLM 辅助器范式：独立类 + `_xxx_for_policy` + runtime 透传 + graph 持有 + allowlist 守门 | `query_rewriter.py` / `evidence_reranker.py` + `service.py:1110/1118` + `runtime.py:106/107` |
| 现有断言锁定单证据原样：`draft.content == "Project Summary confirms STM32H743."`、`evidence_ids == ["evidence-1"]` | `tests/test_template_upload_service.py:725-727` |
| `LLMClient.chat` `usage_stage` 自由 kwarg | `src/core/llm_client.py:194` |

> **关键约束**：fit check 在 graph 层、确定性校验**之后**（supported & 无 contamination）执行；只读 draft + requirement，不改 evidence/落域；未通过置 `requires_human`（不抛错）。`_deterministic_draft` 升级不捏造内容、不动 `validation_status`（保持 `pending`，由 graph 校验定状态）。

## 3. 设计

### 3.1 架构与边界

```
改 src/document_authoring/writers/managed.py
  └─ _deterministic_draft：单证据原样；多证据结构化汇总（枚举拼接 + 一条引用全部证据的 summary assertion）

新增 src/document_authoring/writers/requirement_fit_checker.py
  └─ RequirementFitChecker（仿 EvidenceReranker）
       ├─ __init__(client: LLMClient | None = None)
       └─ check(draft, requirement) -> dict{fit: bool, reason: str}
            1. 构 prompt（requirement subject/predicate + draft content）
            2. LLMClient.chat(..., usage_stage="requirement_fit_check")，要 JSON {fit, reason}
            3. 解析：strip_code_fences -> json.loads -> dict{fit:bool, reason:str}
            4. 任何异常/解析失败：返回 {fit: True, reason: "fit check unavailable"}（降级 pass，不阻断）

改 src/document_authoring/harness/graph.py
  ├─ AuthoringGraph.__init__ 增 fit_checker: "RequirementFitChecker | None" = None
  └─ run() supported 分支：fit_checker 非 None 时调 check，未通过置 requires_human + issue

改 src/document_authoring/models.py
  └─ HarnessPolicy.allowed_tools 默认追加 "requirement_fit_check"

改 src/document_authoring/service.py
  ├─ 新增 _fit_checker_for_policy(policy)（仿 _reranker_for_policy）
  └─ run_internal_harness / resume_internal_harness 构造 fit_checker 透传 runtime

改 src/document_authoring/harness/runtime.py
  └─ execute() 增 fit_checker 形参，透传 AuthoringGraph(..., fit_checker=fit_checker)
```

**边界（不改）**：`DocumentValidator`（确定性校验逻辑不变）、`validate_unit_draft`/`detect_template_contamination`/`validate_cross_unit_consistency`、`LLMManagedWriter` 主路径（`_build_user_prompt`/`_parse_and_validate`）、`WriterRequest`/`InformationRequirement` 模型、`_validated_evidence`、落域校验。

### 3.2 DeterministicEvidenceWriter 升级（改动点 1，闭环 P5）

`_deterministic_draft` 改为：
- **单证据**：`body = evidence[0].content`，`evidence_ids=[id]`，一条 assertion（text=content, evidence_ids=[id]）--与 v1 完全一致，兼容 `test_template_upload_service.py:725-727`。
- **多证据**：`body = "\n".join(f"[{i+1}] {text}" for ...)`（枚举全部证据内容，不捏造），`evidence_ids=all`，**一条 summary assertion**（text=body, evidence_ids=all, consistency_key=unit_id）。
  - 为何一条而非每证据一条：`validate_cross_unit_consistency` 按 `consistency_key` 聚合 value，同一 unit 多条同 key 异值会误报 cross_unit_conflict；一条 summary assertion 引用全部证据即满足"多证据引用"，且 body 含每条 content -> `validate_unit_draft` 的 `any(lexical_anchor)` 对全部 cited evidence 成立。
- `proposed_value = body`、`generated_by="managed_writer"`、`proposed_status="draft"`，**不设 `validation_status`**（默认 `pending`，由 graph 定）。
- 抽 `_build_assertion(request, evidence_ids, text)` 辅助，单/多证据共用。

### 3.3 LLM 主路径回归锁定（改动点 2）

`_build_user_prompt` 已 `request.model_dump_json()`（含全部 evidence）。新增回归测试：多证据 request -> prompt 文本含**每条** evidence 的 id 与 content（断言不止 evidence[0]）。锁定"使用全部 evidence"不被未来改动回退。

### 3.4 RequirementFitChecker（改动点 3，闭环 P10）

- **位置**：`src/document_authoring/writers/requirement_fit_checker.py`，与 `evidence_reranker.py` 同包同构。
- **接口**：`check(draft: DocumentUnitDraft, requirement: InformationRequirement) -> dict[str, Any]`，返回 `{"fit": bool, "reason": str}`。
- **LLM 契约**：system prompt 要求返回 `{"fit": <bool>, "reason": "<short>"}`，fit=true 表示草稿回答了 requirement；user prompt 给 `requirement.subject`/`predicate` + `draft.content`（截断防超长）。
- **解析**：`_strip_code_fences` -> `json.loads` -> dict，取 `bool(payload.get("fit"))` 与 `str(payload.get("reason") or "")`；非 dict/缺字段/异常 -> `{"fit": True, "reason": "fit check unavailable"}`（**降级 pass**，不因 LLM 不可用误判 requires_human）。
- **usage_stage**：`"requirement_fit_check"`；**provider_id**：`"requirement_fit_checker"`。

### 3.5 graph 接入 fit check

`run()` 校验流改为（在原 `else` 分支扩展）：

```python
if contamination:
    validated = validated.model_copy(update={"validation_status": "requires_human", ...})
    result.issues.extend(contamination); result.unit_statuses[unit_id] = "requires_human"
elif validated.validation_status != "supported":
    result.unit_statuses[unit_id] = "requires_human"
elif self.fit_checker is not None:
    self._step(state, "requirement_fit_check")
    self.policy.require_tool("requirement_fit_check")
    verdict = self.fit_checker.check(validated, requirement)
    if not verdict["fit"]:
        validated = validated.model_copy(update={
            "validation_status": "requires_human",
            "validation_notes": [*validated.validation_notes, f"requirement fit check: {verdict['reason']}"],
        })
        result.issues.append({"kind": "requirement_fit_failed", "unit_id": unit_id, "reason": verdict["reason"]})
        result.unit_statuses[unit_id] = "requires_human"
    else:
        result.unit_statuses[unit_id] = "ready_to_render"
else:
    result.unit_statuses[unit_id] = "ready_to_render"
result.drafts.append(validated)
```

- **守门**：`fit_checker` 由 `_fit_checker_for_policy` 仅在 `"requirement_fit_check" in allowed_tools` 时注入（旧 policy -> None -> 跳过，零回归）。`require_tool` 双保险（仿 rerank）。
- **仅 supported & 无 contamination 时执行**：已判 requires_human 的 unit 不再 fit check。
- **降级 pass**：LLM 不可用 -> `check` 返回 fit=True -> ready_to_render（与无 fit_checker 等价）。
- **step 预算**：1 step/supported-unit，运行时 max_steps 充裕。

### 3.6 policy / 注入接线（仿 Stage 3，但 opt-in）

- `requirement_fit_check` **不进默认 `allowed_tools`**（opt-in）。与 Stage 1/3 的 `rewrite_query`/`rerank_evidence`（status-preserving，失败降级 no-op）不同，fit check 是首个 **status-changing** LLM 门控（unfit -> `requires_human`）；放默认会让所有 run（含测试）受 LLM fit 判定影响（flaky/延迟），故部署方显式启用以先验证 LLM 判定质量。
- `service.py._fit_checker_for_policy(policy)`：`if "requirement_fit_check" in policy.allowed_tools: return RequirementFitChecker()` else None（与 `_reranker_for_policy` 同构）。
- `run_internal_harness`/`resume_internal_harness`：`fit_checker = self._fit_checker_for_policy(policy)`，透传 `runtime.execute(..., fit_checker=fit_checker)`。
- `runtime.py execute()` 增 `fit_checker` 形参，`AuthoringGraph(..., fit_checker=fit_checker)`。
- 冻结版本语义：部署方在 policy 显式列 `requirement_fit_check` 即启用；未列（含所有旧工单与默认 policy）-> fit_checker=None -> 跳过（零回归）。
- 不改 `prompt_version`、不加新预算字段。

### 3.7 降级

- 旧 policy 无 `requirement_fit_check`：fit_checker=None -> 跳过 fit check（现状）。
- LLM 不可用/解析失败：`check` 返回 fit=True -> 不阻断。
- `_deterministic_draft` 单证据：与 v1 一致；多证据：枚举汇总。
- `LLMManagedWriter` 主路径不变；fallback 用升级后的 `_deterministic_draft`。

## 4. 测试策略

### 4.1 扩展 `tests/test_template_upload_service.py` 或新建 `tests/test_deterministic_writer.py`（Task 1）
1. `deterministic_single_evidence_verbatim`：1 条 evidence -> content=evidence content、evidence_ids=[id]、1 assertion（锁定兼容）。
2. `deterministic_multi_evidence_summarizes_all`：2 条 evidence -> content 含两条 content、evidence_ids=两 id、1 summary assertion 引用两 id。
3. `deterministic_multi_evidence_passes_validation`：多证据 draft 过 `validate_unit_draft`（lexical anchor + 归属）且不触发 `validate_cross_unit_consistency` 单元内冲突。
4. `deterministic_no_evidence_raises`：空 evidence -> ValueError。
5. `llm_build_user_prompt_includes_all_evidence`（回归锁定改动点 2）：多证据 -> `_build_user_prompt` 文本含每条 id+content。

### 4.2 新增 `tests/test_requirement_fit_checker.py`（Task 2，fake LLMClient）
1. `check_returns_fit_true_when_llm_says_fit`：LLM 返回 `{"fit": true, "reason": "ok"}` -> {fit:True, reason:"ok"}。
2. `check_returns_fit_false_when_llm_says_unfit`：LLM 返回 `{"fit": false, "reason": "missing spec"}` -> {fit:False}。
3. `check_degrades_to_pass_on_llm_failure`：LLM 抛异常 -> {fit:True}。
4. `check_degrades_to_pass_on_parse_failure`：LLM 返回非 JSON / 非 dict -> {fit:True}。
5. `check_uses_requirement_fit_check_usage_stage`：断言 `usage_stage="requirement_fit_check"`。

### 4.3 新增/扩展 `tests/test_authoring_graph_rerank_ledger.py` 或新建 `tests/test_authoring_graph_fit_check.py`（Task 2）
1. `fit_check_marks_requires_human_when_unfit`：fit_checker 返回 fit=False -> unit_status requires_human、issue kind=requirement_fit_failed、validation_status requires_human。
2. `fit_check_passes_when_fit`：fit=True -> ready_to_render。
3. `fit_check_skipped_when_fit_checker_none`：fit_checker=None -> ready_to_render（无 require_tool）。
4. `fit_check_skipped_for_unsupported_unit`：validate_unit_draft unsupported -> requires_human，fit_checker 不调。
5. `fit_check_step_gated_by_require_tool`：policy 无 `requirement_fit_check` + 强注入 -> PermissionError。

### 4.4 扩展 `tests/test_harness_policy.py`（Task 3）
- `default_allowed_tools_includes_requirement_fit_check`。
- `_fit_checker_for_policy` 随 allowed_tools 切换（None / RequirementFitChecker）。

### 4.5 回归（Task 4）
- 阶段 0/1/2/3 全部测试保持绿（单证据 deterministic 行为不变；fit_checker 在旧 policy/无 LLM 时 pass）。
- `test_template_upload_service.py:725-727`（单证据 fallback）保持绿。
- `test_full_generation_flow.py`、`test_document_authoring_p2a.py`、`test_template_authoring_integration.py` 等保持绿。

## 5. 验收标准

- 多证据字段下 deterministic 草稿含全部证据内容、evidence_ids 全部、过确定性校验且无单元内 cross-unit 冲突；单证据与现状一致。
- `_build_user_prompt` 含全部 evidence（回归测试锁定）。
- 注入 fit_checker 且 LLM 判 unfit 时，unit 置 requires_human + issue；fit 时 ready_to_render。
- fit_checker=None 或旧 policy：跳过 fit check，零回归。
- LLM 不可用：fit check 降级 pass，不误判 requires_human。
- `requirement_fit_check` 受 allowlist 守门；阶段 0–3 全部回归绿；无 `prompt_version`/预算字段变动。

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 多证据 summary assertion 触发 cross-unit 冲突 | unit 误判 conflicting | 用一条 summary assertion（同 consistency_key 单值）；单测 4.1.3 专测 |
| deterministic 升级破坏单证据断言 | 测试红 | 单证据保持 v1 原样；单测 4.1.1 锁定；`test_template_upload_service.py:725-727` 回归 |
| fit check LLM 误判 unfit | 字段误判 requires_human | 降级 pass（LLM 失败不阻断）；unfit 只置 requires_human（人工兜底，不丢证据）；阶段 5 可再放宽 |
| fit check 增延迟/成本 | 每 supported unit 一次 LLM | allowlist 可关；失败 pass；usage_stage 计费观测 |
| 旧 policy 工单无 fit check | 不享受 | fit_checker=None 跳过；与 Stage 1/3 同前例 |
| fit check 在测试环境触发真实 LLM | 集成测试不稳 | LLM 不可用 -> ValueError -> 降级 pass；与 reranker 同机制，回归验证 |

## 7. 对上游改进方案 .md 的修正

1. §3 阶段 4 改动点 1 补"单证据保持原样、多证据枚举汇总 + 一条 summary assertion 避免单元内 cross-unit 冲突"。
2. §3 阶段 4 改动点 3 把"validator 增加 requirement_fit_check"修正为"独立 `RequirementFitChecker` 注入 graph（仿 Stage 1/3 LLM 辅助器），受 `requirement_fit_check` allowlist 守门（**opt-in，不进默认 allowlist**，因 fit check 是 status-changing LLM 门控），LLM 失败降级 pass"，并说明偏离理由（validator 纯确定性、fit check 是 LLM 门控质量门）。
3. §7 实施状态表标记阶段 4 已实施（拆"阶段 4–5 | 未实施"为"阶段 4 | 已实施" + "阶段 5 | 未实施"）。
