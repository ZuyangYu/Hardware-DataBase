# 查询改写 + 空结果重试（阶段 1）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-27-query-rewrite-stage1-design.md`

**Goal:** success_empty 字段在 attempt 2 用 LLM 改写串重试，复用 `max_retrieval_attempts_per_unit=2` 预算；LLM 不可用降级原串。

**Architecture:** retrieve 签名加 `query_override` 第三参；`QueryRewriter` 仿 writer 注入 harness；`_retrieve_with_budget` 在 success_empty 时 `require_tool("rewrite_query")` 守门后调 rewriter，改写串经 query_override 传 retrieve。

**Tech Stack:** Python 3，pytest，unittest.mock；`LLMClient`、`InformationRequirement`、`HarnessPolicy`。

## Global Constraints

- 不修改 `InformationRequirement` 模型（跨 4 模块共用），改写串经 retrieve 签名第三参传递。
- 不修改 `_validated_evidence`、`build_knowledge_base_retrieval_outcome`、冻结机制。
- policy 守门在 graph 层；旧 policy（无 rewrite_query）不追溯，rewriter=None。
- LLM 失败/解析失败/空 -> rewriter 返回 None，降级原串。
- TDD：每任务先写失败测试再实现再提交。提交信息带 `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

### Task 1: QueryRewriter 类 + 单测

**Files:**
- Create: `src/document_authoring/writers/query_rewriter.py`
- Test: `tests/test_query_rewriter.py`

**Interfaces:**
- Consumes: `LLMClient`（`src/core/llm_client.py:194` `.chat(messages, **kwargs)`）、`InformationRequirement`（`src/agents/claim_evidence.py`）、`_strip_code_fences`（`src/document_authoring/writers/managed.py:287`）。
- Produces: `QueryRewriter` 类，`__init__(client: LLMClient | None = None)`、`rewrite(requirement: InformationRequirement) -> str | None`、`provider_id = "query_rewriter"`。

- [ ] **Step 1: Write the failing tests**

创建 `tests/test_query_rewriter.py`：

```python
from __future__ import annotations

from unittest.mock import Mock

from src.agents.claim_evidence import InformationRequirement
from src.document_authoring.writers.query_rewriter import QueryRewriter


def _requirement(subject: str = "额定电压") -> InformationRequirement:
    return InformationRequirement(
        requirement_id="req-1",
        semantic_unit_id="field:f1",
        claim_type="attribute",
        subject=subject,
        predicate="规格",
        required_capabilities=["entity_lookup"],
    )


def test_rewriter_returns_rewrite_string_from_json(monkeypatch):
    client = Mock()
    client.chat.return_value = '{"rewrite": "额定电压 规格参数 电源电压"}'
    rewriter = QueryRewriter(client=client)
    result = rewriter.rewrite(_requirement())
    assert result == "额定电压 规格参数 电源电压"
    assert client.chat.call_args.kwargs.get("usage_stage") == "query_rewrite"


def test_rewriter_returns_text_when_not_json(monkeypatch):
    client = Mock()
    client.chat.return_value = "电源电压 额定值 参数"
    rewriter = QueryRewriter(client=client)
    result = rewriter.rewrite(_requirement())
    assert result == "电源电压 额定值 参数"


def test_rewriter_strips_code_fences(monkeypatch):
    client = Mock()
    client.chat.return_value = "```json\n{\"rewrite\": \"重写串\"}\n```"
    rewriter = QueryRewriter(client=client)
    assert rewriter.rewrite(_requirement()) == "重写串"


def test_rewriter_returns_none_on_llm_exception():
    client = Mock()
    client.chat.side_effect = RuntimeError("llm down")
    rewriter = QueryRewriter(client=client)
    assert rewriter.rewrite(_requirement()) is None


def test_rewriter_returns_none_on_empty_response():
    client = Mock()
    client.chat.return_value = "   \n  "
    rewriter = QueryRewriter(client=client)
    assert rewriter.rewrite(_requirement()) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_query_rewriter.py -v`
Expected: FAIL（模块不存在，ImportError）。

- [ ] **Step 3: Write minimal implementation**

创建 `src/document_authoring/writers/query_rewriter.py`：

```python
"""LLM-backed query rewriter for harness retrieval.

Produces one rewritten query string per requirement to improve recall when
schema terminology differs from document terminology. Any LLM or parsing
failure degrades to ``None`` so the caller falls back to the original query.
"""

from __future__ import annotations

import json
import logging

from src.agents.claim_evidence import InformationRequirement
from src.core.llm_client import LLMClient
from src.document_authoring.writers.managed import _strip_code_fences


logger = logging.getLogger(__name__)


class QueryRewriter:
    """Rewrite a retrieval query using the shared LLM client."""

    provider_id = "query_rewriter"

    def __init__(self, client: LLMClient | None = None):
        self._client = client or LLMClient()

    def rewrite(self, requirement: InformationRequirement) -> str | None:
        prompt = _build_prompt(requirement)
        try:
            response = self._client.chat(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                usage_stage="query_rewrite",
            )
        except Exception as exc:
            logger.warning("QueryRewriter LLM call failed for %s: %s", requirement.requirement_id, exc)
            return None
        return _parse_rewrite(response)


_SYSTEM_PROMPT = """You rewrite a retrieval query to improve recall in a hardware knowledge base.

Return ONE rewritten query string that covers synonyms, entity aliases, and
alternative phrasings of the requirement. Output ONLY the query text, or a
JSON object {"rewrite": "<query>"}. No explanation, no markdown fences."""


def _build_prompt(requirement: InformationRequirement) -> str:
    parts = [f"subject: {requirement.subject or ''}"]
    if requirement.predicate:
        parts.append(f"predicate: {requirement.predicate}")
    if requirement.required_capabilities:
        parts.append(f"capabilities: {', '.join(requirement.required_capabilities)}")
    parts.append("Return one rewritten query string.")
    return "\n".join(parts)


def _parse_rewrite(response: str) -> str | None:
    if not response:
        return None
    cleaned = _strip_code_fences(response).strip()
    if not cleaned:
        return None
    # Try JSON first: {"rewrite": "..."} or a bare JSON string.
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return cleaned or None
    if isinstance(parsed, dict):
        value = str(parsed.get("rewrite") or "").strip()
        return value or None
    if isinstance(parsed, str):
        return parsed.strip() or None
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_query_rewriter.py -v`
Expected: PASS（5 用例）。

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/writers/query_rewriter.py tests/test_query_rewriter.py
git commit -m "feat: add QueryRewriter for harness retrieval

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: HarnessPolicy 加 rewrite_query + max_query_rewrite_rounds

**Files:**
- Modify: `src/document_authoring/models.py:324-357`（`HarnessPolicy`）
- Test: `tests/test_document_authoring_p2a.py`（或新建 `tests/test_harness_policy.py`）

**Interfaces:**
- Produces: `HarnessPolicy.allowed_tools` 默认含 `"rewrite_query"`；`HarnessPolicy.max_query_rewrite_rounds: int = 1`；`validate_budget` 校验新字段。

- [ ] **Step 1: Write the failing tests**

新建 `tests/test_harness_policy.py`：

```python
from __future__ import annotations

import pytest

from src.document_authoring.models import HarnessPolicy


def _make_policy(**overrides) -> HarnessPolicy:
    base = dict(
        harness_policy_id="p1", version="1", status="approved",
    )
    base.update(overrides)
    return HarnessPolicy(**base)


def test_default_allowed_tools_include_rewrite_query():
    policy = _make_policy()
    assert "rewrite_query" in policy.allowed_tools


def test_default_max_query_rewrite_rounds_is_one():
    policy = _make_policy()
    assert policy.max_query_rewrite_rounds == 1


def test_policy_rejects_negative_rewrite_rounds():
    with pytest.raises(ValueError):
        _make_policy(max_query_rewrite_rounds=-1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_harness_policy.py -v`
Expected: FAIL（`rewrite_query` 不在默认列表；`max_query_rewrite_rounds` 属性不存在）。

- [ ] **Step 3: Write minimal implementation**

修改 `src/document_authoring/models.py` 的 `HarnessPolicy`：

3a. `allowed_tools` 默认列表追加 `"rewrite_query"`：

```python
    allowed_tools: list[str] = Field(default_factory=lambda: [
        "retrieve_evidence", "draft_ready_unit", "validate_unit_draft",
        "detect_template_contamination", "validate_cross_unit", "rewrite_query",
    ])
```

3b. 在 `lease_seconds` 字段后新增：

```python
    max_query_rewrite_rounds: int = 1
```

3c. `validate_budget` 的 `min(...)` 加入 `max_query_rewrite_rounds`：

```python
        if min(
            self.max_steps,
            self.max_retrieval_rounds,
            self.max_retrieval_attempts_per_unit,
            self.max_units_per_run,
            self.lease_seconds,
            self.max_query_rewrite_rounds,
        ) < 1:
            raise ValueError("harness policy budgets must be positive")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_harness_policy.py -v`
Expected: PASS。

- [ ] **Step 5: Run regressions（默认 allowed_tools 变更可能影响断言）**

Run: `pytest tests/test_document_authoring_p2a.py tests/test_knowledge_base_document_work_orders.py -v`
Expected: PASS。若有测试硬断言 allowed_tools 长度/内容，更新为含 rewrite_query。

- [ ] **Step 6: Commit**

```bash
git add src/document_authoring/models.py tests/test_harness_policy.py
git commit -m "feat: add rewrite_query to HarnessPolicy allowlist

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: _schema_harness_policy 预留改写 step 预算

**Files:**
- Modify: `src/document_authoring/service.py:1011-1044`（`_schema_harness_policy`）

**Interfaces:**
- Produces: 自动生成 policy 的 `max_steps = 2 + unit_count*(attempts+4)`；`version` 含 `-rewrite`。

- [ ] **Step 1: Write the failing test**

在 `tests/test_harness_policy.py` 追加（需 import service 构造；或改在已有 service 测试里）。简单起见用直接调用 `_schema_harness_policy` 的 staticmethod 形式不可行（它写库）。改为通过 `service` fixture 调用一个 schema。先检查是否有现成 helper 构造 schema。

实际采用：在 `tests/test_document_authoring_p2a.py` 末尾追加，复用其 `service` fixture 与已有 schema 构造 helper。先 Read 该文件确认 helper 名。

> 实施时先 Read `tests/test_document_authoring_p2a.py` 找到构造 approved schema 的 helper（如 `_approved_schema`），新增测试断言生成 policy 的 `allowed_tools` 含 `rewrite_query` 且 `max_steps >= 2 + unit_count*(2+4)`。

- [ ] **Step 2: Run test to verify it fails**

Run: 对应测试
Expected: FAIL（`max_steps` 仍是 `+3` 公式）。

- [ ] **Step 3: Write minimal implementation**

修改 `src/document_authoring/service.py:1019`：

```python
        max_steps = 2 + unit_count * (attempts + 4)
```

修改 `:1032` version：

```python
            version=f"units-{unit_count}-attempts-{attempts}-rewrite",
```

- [ ] **Step 4: Run test to verify it passes**

Run: 对应测试
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/service.py tests/test_document_authoring_p2a.py
git commit -m "feat: reserve rewrite step budget in auto harness policy

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: retrieve 签名加 query_override（RetrievalProvider + 闭包）

**Files:**
- Modify: `src/document_authoring/harness/graph.py:47`（`RetrievalProvider`）
- Modify: `src/core/app_pipeline.py:522`（KB 闭包）、`:575`（project 闭包）
- Modify: `src/document_authoring/service.py:756`、`:812`（retrieve 类型注解）
- Test: `tests/test_knowledge_base_document_work_orders.py`

**Interfaces:**
- Produces: `RetrievalProvider = Callable[[InformationRequirement, int, "str | None"], RetrievalOutcome]`；两个闭包签名 `def retrieve(requirement, _attempt, query_override=None)`，query 优先用 `query_override`。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_knowledge_base_document_work_orders.py` 追加：

```python
def test_kb_retriever_uses_query_override_when_provided(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    pipeline.spreadsheet_service = None
    retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["spec.pdf"])
    retrieve(requirement_with_capabilities("描述", ["entity_lookup"]), 0, query_override="override query")

    # backend.retrieve was called with the override query, not the subject.
    called_query = pipeline.backend.retrieve.call_args.args[1]
    assert called_query == "override query"


def test_kb_retriever_falls_back_to_subject_query_without_override(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    pipeline.spreadsheet_service = None
    retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["spec.pdf"])
    retrieve(requirement("voltage"), 0)  # no query_override

    called_query = pipeline.backend.retrieve.call_args.args[1]
    assert "voltage" in called_query
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_knowledge_base_document_work_orders.py -k query_override -v`
Expected: FAIL（闭包签名不接受 query_override -> TypeError）。

- [ ] **Step 3: Write minimal implementation**

3a. `src/document_authoring/harness/graph.py:47`：

```python
RetrievalProvider = Callable[[InformationRequirement, int, "str | None"], RetrievalOutcome]
```

3b. `src/core/app_pipeline.py` KB 闭包（`:522`）签名 + query 构造：

```python
        def retrieve(requirement, _attempt, query_override=None):
            query = query_override or " ".join(
                value
                for value in (
                    requirement.subject,
                    requirement.predicate,
                    requirement.object_hint,
                )
                if value
            )
            evidences = list(
                self.backend.retrieve(
                    kb_name,
                    query,
                    top_k=config.settings.FINAL_TOP_K,
                    ctx=ctx,
                    filters={"source_names": frozen_source_names},
                )
            )
            # ... spreadsheet 段保持不变（用同一 query）
```

3c. project 闭包（`:575`）同样加 `query_override=None` 与 `query = query_override or ...`。

3d. `src/document_authoring/service.py:756` 与 `:812` 类型注解：

```python
        retrieve: Callable[[Any, int, "str | None"], RetrievalOutcome],
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_knowledge_base_document_work_orders.py -k "query_override or scoped or tabular" -v`
Expected: PASS。

- [ ] **Step 5: Run regression on retrieve mock tests（签名兼容性）**

Run: `pytest tests/test_document_authoring_p2a.py tests/test_template_authoring_integration.py tests/test_full_generation_flow.py tests/test_document_auto_generation.py -v`
Expected: 部分可能 FAIL（mock `def retrieve(requirement, attempt)` 收到 3 参）。记录失败点，Task 5 统一更新 mock 签名。

> 注：本步可能暴露签名不匹配。Task 5 会修。若失败过多影响信心，可在此步顺带把 5 个文件的 `def retrieve(requirement, attempt)` 改为 `(requirement, attempt, query_override=None)`，但正式更新放 Task 5。

- [ ] **Step 6: Commit**

```bash
git add src/document_authoring/harness/graph.py src/core/app_pipeline.py src/document_authoring/service.py tests/test_knowledge_base_document_work_orders.py
git commit -m "feat: add query_override param to retrieve provider

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: graph rewriter 注入 + _retrieve_with_budget 改写重试

**Files:**
- Modify: `src/document_authoring/harness/graph.py:64-89`（`__init__`/`run` 签名）、`:186-203`（`_retrieve_with_budget`）
- Modify: `src/document_authoring/harness/runtime.py:90-102`、`:180-191`（透传）
- Modify: `src/document_authoring/service.py:751-785`、`:807-845`（构造 rewriter + 透传）
- Modify: `src/document_authoring/service.py:1097`（新增 `_rewriter_for_policy`）
- Test: `tests/test_document_authoring_p2a.py`、`tests/test_template_authoring_integration.py`（mock 签名更新 + 集成测试）

**Interfaces:**
- Consumes: `QueryRewriter`（Task 1）、`HarnessPolicy.allowed_tools`（Task 2）、`query_override` retrieve 签名（Task 4）。
- Produces: `_retrieve_with_budget` 在 success_empty + rewriter 时用改写串 attempt 2；`require_tool("rewrite_query")` 守门。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_document_authoring_p2a.py` 追加（复用其 service fixture 与 schema helper）。先 Read 确认 helper。

测试 A：success_empty + rewriter -> attempt 2 用改写串。

```python
def test_retrieve_with_budget_uses_rewrite_on_success_empty(...):
    # 构造 schema/work order，retrieve mock 第 1 次 success_empty、第 2 次 success_with_hits
    # rewriter mock 返回 "rewrite-query"
    # 断言 retrieve 第 2 次收到 query_override="rewrite-query"
```

测试 B：rewriter=None -> attempt 2 query_override=None。

测试 C：allowed_tools 不含 rewrite_query + rewriter 注入 -> 抛 PermissionError。

具体代码实施时按 helper 现状填充。

- [ ] **Step 2: Run tests to verify they fail**

Run: 对应测试
Expected: FAIL（rewriter 未注入，attempt 2 仍 query_override=None）。

- [ ] **Step 3: Write minimal implementation**

3a. `graph.py` `AuthoringGraph.__init__` 加 `rewriter: "QueryRewriter | None" = None`，存 `self.rewriter`。import `QueryRewriter`（TYPE_CHECKING 下避免循环）。

3b. `graph.py` `run` 签名加 `rewriter: "QueryRewriter | None" = None`，传给 `AuthoringGraph` 构造（或在 `__init__` 已存）。实际：`runtime.execute` 构造 `AuthoringGraph` 时传 rewriter。

3c. `graph.py` `_retrieve_with_budget` 改写：

```python
    def _retrieve_with_budget(
        self,
        state: DocumentAuthoringState,
        requirement: InformationRequirement,
        retrieve: RetrievalProvider,
    ) -> RetrievalOutcome:
        self.policy.require_tool("retrieve_evidence")
        original_query = _query_string(requirement)
        last: RetrievalOutcome | None = None
        for attempt in range(1, self.policy.policy.max_retrieval_attempts_per_unit + 1):
            self._step(state, "retrieve_requirement_evidence")
            state["retrieval_round_count"] += 1
            self.policy.require_retrieval_round(state["retrieval_round_count"])
            if attempt == 1:
                outcome = retrieve(requirement, attempt, None)
            else:
                override = self._rewrite_for_retry(state, requirement, original_query)
                outcome = retrieve(requirement, attempt, override)
            last = outcome
            if outcome.status not in {"retrieval_failed", "source_unavailable", "access_denied", "partial_failure", "success_empty"}:
                return outcome
        assert last is not None
        return last

    def _rewrite_for_retry(self, state, requirement, original_query):
        if self.rewriter is None:
            return None
        self.policy.require_tool("rewrite_query")
        self._step(state, "rewrite_query")
        try:
            rewritten = self.rewriter.rewrite(requirement)
        except Exception:
            rewritten = None
        state.setdefault("retrieval_ledger", []).append({
            "unit_id": requirement.semantic_unit_id,
            "original_query": original_query,
            "rewrite": rewritten,
            "attempt": 2,
        })
        return rewritten
```

> 注意：`success_empty` 现在也进入下一 attempt（加入重试集合）。attempt 1 若 success_with_hits 直接返回。

3d. 新增模块级 helper `_query_string(requirement)` 返回 `" ".join(...)`。

3e. `runtime.py` `execute` 签名加 `rewriter`，构造 `AuthoringGraph(..., rewriter=rewriter)`。

3f. `service.py` 新增 `_rewriter_for_policy`：

```python
    @staticmethod
    def _rewriter_for_policy(policy: HarnessPolicy):
        if "rewrite_query" in policy.allowed_tools:
            from src.document_authoring.writers.query_rewriter import QueryRewriter
            return QueryRewriter()
        return None
```

3g. `service.py` `run_internal_harness` 与 `resume_internal_harness`：在 `writer = writer or self._writer_for_policy(policy)` 后加 `rewriter = self._rewriter_for_policy(policy)`，传给 `harness_runtime.execute(..., rewriter=rewriter)`。

- [ ] **Step 4: Update retrieve mock signatures across test files**

5 文件约 10 处 `def retrieve(requirement, attempt)` -> `def retrieve(requirement, attempt, query_override=None)`：
- `tests/test_template_authoring_integration.py:103`、`:115`(retrieve_one 不变)、`:194`等
- `tests/test_knowledge_base_document_work_orders.py:687`
- `tests/test_document_authoring_p2a.py:328`、`:416`、`:477`
- `tests/test_full_generation_flow.py:246`
- `tests/test_document_auto_generation.py`（`retrieve=Mock()` 自动兼容，无需改）

- [ ] **Step 5: Run all tests to verify they pass**

Run: `pytest tests/test_query_rewriter.py tests/test_harness_policy.py tests/test_document_authoring_p2a.py tests/test_knowledge_base_document_work_orders.py tests/test_template_authoring_integration.py tests/test_full_generation_flow.py tests/test_document_auto_generation.py -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add src/document_authoring/harness/graph.py src/document_authoring/harness/runtime.py src/document_authoring/service.py tests/
git commit -m "feat: rewrite query on success_empty retry in harness

_retrieve_with_budget now rewrites the query via QueryRewriter when
attempt 1 returns success_empty, gated by require_tool('rewrite_query').
LLM failure degrades to the original query. Mock retrieve signatures
updated for the new query_override param.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- §3.2 改动点 1 QueryRewriter -> Task 1 ✓
- §3.2 改动点 2 HarnessPolicy -> Task 2 ✓
- §3.2 改动点 3 _schema_harness_policy -> Task 3 ✓
- §3.2 改动点 4 _rewriter_for_policy -> Task 5 ✓
- §3.2 改动点 5 透传 -> Task 5 ✓
- §3.2 改动点 6 RetrievalProvider -> Task 4 ✓
- §3.2 改动点 7 _retrieve_with_budget -> Task 5 ✓
- §3.2 改动点 8 闭包 query_override -> Task 4 ✓
- §3.3 policy 版本治理 -> Task 2/3（默认含 + version 标记）✓
- §3.4 降级链 -> Task 1（None）+ Task 5（rewriter=None/rewrite=None）✓
- §4 测试策略 -> Task 1/2/4/5 覆盖 ✓

**2. Placeholder scan:** Task 3/5 的测试代码标注"按 helper 现状填充" -- 实施时先 Read 确认 helper，属可执行的具体步骤（已指明 Read 目标）。其余无 TBD。

**3. Type consistency:** `QueryRewriter.rewrite(requirement) -> str | None` 全链一致；`query_override: str | None` 在 RetrievalProvider/闭包/`_retrieve_with_budget` 一致；`rewriter: QueryRewriter | None` 在 graph/runtime/service 一致。

无缺口。
