# 电路查询质量与 LangGraph 复合编排设计

更新日期：2026-07-10

## 目标

完成电路查询增量接入的后续工作：将旧分支中可复用的结构化电路查询质量能力迁入 `CircuitIndexService`，并将旧 `CompositeQueryAgent` 的有效多源行为翻译为当前 `src/agents/graph.py` 的 LangGraph 规划与检索逻辑。

## 架构约束

- `src/pipelines/*` 仍是唯一摄入与生命周期入口。
- `src/agents/*` 和 LangGraph 仍是唯一顶层查询编排入口。
- 电路能力只通过 `CircuitIndexService` 和 `CircuitQueryTool` 向上暴露。
- 不恢复 `src/query_router/*`、`src/core/rag_pipeline.py` 或旧 Streamlit router。
- 不迁移 PDF/图像解析、UI、旧 session context 或旧独立 LLM query agent。
- `.edf/.edif` 仍不上传到 RAGFlow。

## 方案

### 电路查询服务

`CircuitIndexService.query()` 从当前的简单 OR 子串匹配升级为以下固定流程：

1. 根据 `kb_name`、部门 scope 和 source/record filter 选择允许查询的 design。
2. 从问题中识别精确实例位号、网络名、模块名和器件描述词。
3. 调用 `CircuitQueryEngine` 执行实例、网络、模块和连接关系检索。
4. 将结构化结果转为稳定、可引用的 `Evidence`，包含 circuit id、record id、实体类型、实体 id 与来源文件。
5. 按精确实体命中、连接关系命中、词项/可选语义命中进行确定性排序和去重。

`CircuitQueryTool.run()` 的签名和返回类型保持不变：

```python
run(query, kb_name, ctx, top_k=5, filters=None) -> list[Evidence]
```

### 可迁移模块边界

允许纳入的依赖：

- `src/circuit/query_engine.py`
- `src/circuit/entity_resolver.py` 中不依赖 session/旧 tool 的纯解析逻辑
- `src/circuit/query_evidence.py` 中可转为 `Evidence` 的纯格式化逻辑
- `src/circuit/relations/*`
- `src/circuit/vector_index.py`，仅作为无 embedding 配置时 fail-soft 的可选召回增强

不纳入：

- `src/circuit/query_agent.py`
- `src/circuit/query_tool.py`
- `src/circuit/query_planner.py`
- `src/circuit/session_context_store.py`
- `src/circuit/recovery_manager.py`
- `src/circuit/llm_controlled_planner.py`
- `src/query_router/*`
- PDF/图像解析、`src/ui/*`

### LangGraph 复合行为

旧 `CompositeQueryAgent` 的行为映射为现有 LangGraph 节点责任：

| 原行为 | develop 中的承载位置 |
| --- | --- |
| 识别电路事实需求 | `_expected_evidence()` 与 question analysis |
| 选择电路数据源 | `plan_source_selection()` |
| 电路与文档/BOM 并行检索 | `retrieve_evidence()` 的多 tool calls |
| 发现缺口后继续查询 | `judge_sufficiency()` 与 `plan_next_retrieval()` |
| 相关性/来源优先级 | Evidence score、coverage matrix 与 retrieval ledger |
| 最终多源答案 | `compose_answer()` |

对以下问题，planner 必须生成电路 tool call：

- 实际连接、pin、net、位号、模块、拓扑、电源路径。

对以下复合问题，planner 必须并行生成对应 tool calls：

- 电路连接 + 设计说明：`circuit_query` + `document_rag`
- 电路器件 + BOM 用量/替代料：`circuit_query` + `spreadsheet_semantic`/`spreadsheet_cell`

## Evidence 规范

每条电路 Evidence 必须为一个可独立核验的结构化事实，不将 LLM 推断写入事实内容。

```python
Evidence(
    id="circuit:<record_id>:<entity_type>:<entity_id>",
    content="<可读的连接、实例、模块或电源事实>",
    source_name="<原始 EDF/EDIF 文件名>",
    content_kind="circuit_design",
    processor_kind="circuit_design",
    score=<float>,
    locator={"record_id": ..., "circuit_id": ..., "entity_type": ..., "entity_id": ...},
    metadata={"kb_name": ..., "department_id": ..., "source_group": "circuit_design"},
)
```

## 错误处理与兼容性

- 无匹配、source filter 不匹配、无 indexed circuit 时返回 `[]`。
- 解析/查询实现异常向上抛给 `retrieve_evidence()`，保留其 failed diagnostics。
- 可选 vector index 或 embedding 不可用时，退化为结构化精确查询，不影响上传或查询主路径。
- 只允许 `status == "indexed"` 的 circuit 记录进入 planner。
- 带部门 scope 的查询必须严格匹配 circuit metadata 的 department。

## 测试与验收

新增或扩展 unittest 覆盖：

- 精确实例、网络和模块检索返回正确 Evidence。
- 同一查询优先返回精确位号/网络连接而非宽泛描述匹配。
- source/record filter 与部门隔离保持有效。
- 电源路径或关系检索在可用结构化数据上产生 grounded Evidence。
- 单电路问题只生成 `circuit_query`；电路+BOM、 电路+文档问题生成多个 tool calls。
- 失败的 circuit record 不进入 source plan。
- `python -m unittest discover -s tests -v` 通过。

## 非目标

- PDF 原理图视觉理解、多模态模块截图、UI 浏览器。
- 旧独立 circuit chat/session 行为。
- 恢复旧 query router 或让其与 LangGraph 并存。
