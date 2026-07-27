# 查询改写 + 空结果重试（阶段 1）设计

> 日期：2026-07-27
> 分支：`feature/template-upload-governed-authoring`
> 上游方案：`docs/Hardware-DataBase_文档生成改进方案.md` §3 阶段 1
> 前置：阶段 0（spreadsheet 检索接通）已实施
> 状态：待实施

## 1. 背景与目标

文档生成 harness 的检索是静态单串查询：`query = "{subject} {predicate} {object_hint}"`（`app_pipeline.py:511`）。schema 术语 ≠ 文档术语时召回崩塌，无同义词/重写/分解（上游方案 P2）。同时 `_retrieve_with_budget`（`graph.py:186`）只在 `retrieval_failed/source_unavailable/access_denied/partial_failure` 四种失败状态时重试，`success_empty` 直接判 missing/blocked，`max_retrieval_attempts_per_unit=2` 预算被浪费（P3）。

**目标（阶段 1）**：把静态单串查询升级为"原串 + LLM 改写"，并在 `success_empty` 时用改写串重试 attempt 2，复用已存在的 `max_retrieval_attempts_per_unit=2` 预算。

**非目标**：capability-aware 多检索器分发（阶段 2）、rerank 与可观测性 ledger 持久化（阶段 3）、草稿质量（阶段 4）、自适应恢复（阶段 5）。

## 2. 现状关键事实（代码核查结论）

| 事实 | 位置 |
|---|---|
| retrieve 注入链：AppPipeline 闭包 -> `service.run_internal_harness(*, retrieve)` -> `runtime.execute(*, retrieve)` -> `graph.run(*, retrieve)` -> `_retrieve_with_budget` 调 `retrieve(requirement, attempt)` | `app_pipeline.py:522`、`service.py:756/779`、`runtime.py:101/190`、`graph.py:88/198` |
| `RetrievalProvider` 类型 | `graph.py:47` `Callable[[InformationRequirement, int], RetrievalOutcome]` |
| `_retrieve_with_budget` 仅对 4 种失败状态重试 | `graph.py:200-201` |
| writer 注入模式（rewriter 仿此） | `service.py:767` `writer = writer or self._writer_for_policy(policy)`；`service.py:1097` `_writer_for_policy` |
| `HarnessPolicy.allowed_tools` 默认列表 | `models.py:335`（含 5 项，不含 rewrite_query） |
| 自动 policy 生成 `_schema_harness_policy` | `service.py:1011`；`max_steps = 2 + unit_count*(attempts+3)`（`:1019`） |
| `LLMClient.chat(messages, **kwargs)`，`usage_stage` 经 kwargs | `llm_client.py:194` |
| `InformationRequirement` 跨 4 模块共用（agents/projects/document_authoring） | `claim_evidence.py`、`graph.py`、`projects/retrieval.py`、`models.py` |
| 测试中 retrieve mock 约 10 处 | `test_template_authoring_integration.py`、`test_knowledge_base_document_work_orders.py`、`test_document_authoring_p2a.py`、`test_full_generation_flow.py`、`test_document_auto_generation.py` |

> 关键约束：`InformationRequirement` 跨模块共用，**不得加字段**承载改写串。改写串经 retrieve 签名的第三参 `query_override` 传递。

## 3. 设计

### 3.1 架构

```
service.run_internal_harness
  ├─ policy = load HarnessPolicy（allowed_tools 含 rewrite_query）
  ├─ rewriter = self._rewriter_for_policy(policy)     ← 仿 _writer_for_policy
  └─ runtime.execute(..., retrieve, rewriter)
        └─ graph.run(..., retrieve, rewriter)
              └─ _retrieve_with_budget(req, retrieve):
                    attempt 1: retrieve(req, 1, None)              ← 原串
                    if success_empty and rewriter is not None:
                        require_tool("rewrite_query") + _step
                        rewritten = rewriter.rewrite(req)          ← LLM 改写
                        attempt 2: retrieve(req, 2, rewritten)     ← 改写串（None 则原串）
                    elif 失败状态:
                        attempt 2: retrieve(req, 2, None)          ← 现有重试
                    else:
                        return outcome                             ← success_with_hits 直接返回
```

policy 守门在 graph 层（持有 HarnessToolPolicy）；LLMClient 在 service 层可用；rewriter 仿 writer 模式注入。

### 3.2 改动清单

1. **新文件 `src/document_authoring/writers/query_rewriter.py`**：`QueryRewriter` 类。
   - `__init__(self, client: LLMClient | None = None)`：`self._client = client or LLMClient()`。
   - `rewrite(self, requirement: InformationRequirement) -> str | None`：构造 prompt（subject/predicate/required_capabilities/label），调 `self._client.chat(messages, usage_stage="query_rewrite")`。解析返回：剥 code fence（复用 `managed.py` 的 `_strip_code_fences` 模式）-> 先尝试 `json.loads`，若为 dict 取 `rewrite` 键，若为 str 直接用 -> JSON 解析失败则取去除 code fence 后的整段文本 strip -> 去空 -> 返回串或 None。
   - 任何异常（LLM 抛错、解析失败、空返回）-> 记 log，返回 None（降级）。
   - `provider_id = "query_rewriter"`。

2. **`HarnessPolicy`（`models.py:324`）**：
   - `allowed_tools` 默认列表追加 `"rewrite_query"`（6 项）。
   - 新增 `max_query_rewrite_rounds: int = 1`。
   - `validate_budget`（`:343`）追加 `max_query_rewrite_rounds` 进 `min(...)` 下界校验。

3. **`_schema_harness_policy`（`service.py:1011`）**：
   - `max_steps` 从 `2 + unit_count*(attempts+3)` 改为 `2 + unit_count*(attempts+4)`（每 unit 预留 1 改写 step）。
   - `version` 字符串从 `f"units-{unit_count}-attempts-{attempts}"` 改为 `f"units-{unit_count}-attempts-{attempts}-rewrite"`。

4. **`service._rewriter_for_policy(policy)`（新方法，仿 `_writer_for_policy`）**：
   - `if "rewrite_query" in policy.allowed_tools: return QueryRewriter()`，否则 `return None`。

5. **`run_internal_harness`（`service.py:751`）/ `resume_internal_harness`（`service.py:807`）/ `runtime.execute`（`runtime.py:90`）/ `graph.run`（`graph.py:79`）/ `AuthoringGraph.__init__`**：新增 `rewriter: "QueryRewriter | None" = None` 参数逐层透传。`__init__` 存 `self.rewriter`。两个 service 入口（run/resume）均需构造 `rewriter = self._rewriter_for_policy(policy)` 并透传。

6. **`RetrievalProvider`（`graph.py:47`）**：改为 `Callable[[InformationRequirement, int, "str | None"], RetrievalOutcome]`。

7. **`graph._retrieve_with_budget`（`graph.py:186`）**：核心逻辑见 §3.1 架构图。改写前 `self.policy.require_tool("rewrite_query")` + `self._step(state, "rewrite_query")`；改写结果 append 到 `state["retrieval_ledger"]`（dict：`{unit_id, original_query, rewrite, attempt}`，阶段 3 持久化，阶段 1 仅写入 state）。

8. **app_pipeline 两个 retrieve 闭包（`app_pipeline.py:522` KB / `:575` project）**：签名 `def retrieve(requirement, _attempt, query_override=None)`；query 构造改为 `query = query_override or " ".join(...)`。

### 3.3 policy 版本治理

`allowed_tools` 默认值变更影响**新**生成的 policy。已在途工单冻结的旧 policy 版本不含 `rewrite_query`，不追溯：旧工单 `rewriter=None`，`_retrieve_with_budget` 不进入改写分支，行为同现状。新 schema 自动生成的 policy 含 `rewrite_query`，`rewriter` 注入。

### 3.4 降级链

- `rewriter is None`（policy 不允许）-> 不改写，attempt 2 原串重试（现状）。
- `rewriter.rewrite(...)` 返回 None（LLM 失败/解析失败/空）-> attempt 2 原串重试。
- `rewritten` 非空 -> attempt 2 用改写串。
- LLM 不可用环境（`LLMClient` 构造或 chat 抛异常）-> `rewrite` 内部捕获返回 None -> 同上。

### 3.5 预算与守门

- `max_query_rewrite_rounds=1`：声明性字段，每 unit 最多 1 次改写（attempt 2）。阶段 1 不实现多轮。
- `max_steps` 预留：`unit_count*(attempts+4)` 覆盖每 unit 1 改写 step。
- `require_tool("rewrite_query")` 在改写前守门；policy 不含该 tool 时抛 PermissionError（测试覆盖）。

## 4. 测试策略

- **QueryRewriter 单测**（新 `tests/test_query_rewriter.py`）：① LLM mock 返回合法 JSON -> 返回改写串；② LLM 抛异常 -> 返回 None；③ LLM 返回非法 JSON 但有文本 -> 取文本；④ LLM 返回空 -> None。
- **`_retrieve_with_budget` 单测**（扩展 `test_document_authoring_p2a.py` 或新文件）：① `success_empty` + rewriter mock 返回串 -> attempt 2 retrieve 收到 `query_override=串`；② rewriter=None -> attempt 2 `query_override=None`；③ rewriter.rewrite 返回 None -> attempt 2 `query_override=None`；④ 非 success_empty 失败 -> 现有重试 `query_override=None`；⑤ `success_with_hits` -> 不重试；⑥ allowed_tools 不含 rewrite_query + rewriter 注入 -> 改写步骤抛 PermissionError。
- **retrieve 闭包测试**（`test_knowledge_base_document_work_orders.py`）：`query_override` 非 None 时优先使用、为 None 时用原串。
- **mock retrieve 签名更新**：5 文件约 10 处 `def retrieve(requirement, attempt)` -> `(requirement, attempt, query_override=None)`。
- **集成**（`test_template_authoring_integration.py`）：success_empty 字段 attempt 2 命中（retrieve mock 第 1 次空、第 2 次有命中，rewriter mock 返回改写串）。

## 5. 验收标准

- `success_empty` 字段在 attempt 2 用改写串重试（mock 验证改写串传入 retrieve）。
- LLM 不可用时降级为原串重试，run 不失败。
- 旧 policy（无 rewrite_query）工单行为不变（rewriter=None）。
- `HarnessPolicy` 默认 allowed_tools 含 rewrite_query；`max_query_rewrite_rounds` 校验生效。
- 全部测试通过。

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| LLM 改写增加延迟/成本 | 生成变慢、调用增多 | `max_query_rewrite_rounds=1` + step 预算约束；降级保证不阻断 |
| retrieve 签名变更影响 10 处 mock | 测试机械更新 | 回归套件覆盖；签名参数有默认值，旧调用兼容 |
| 改写串质量差 | attempt 2 仍空 | 阶段 3 rerank 兜底；阶段 1 不解决 |
| 旧工单 policy 无 rewrite_query | 改写不可用 | 不追溯；旧工单 rewriter=None 行为同现状 |
