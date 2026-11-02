# 06 · 工具层与证据契约

对应代码：`src/agents/tools/`（各工具模块）、`src/agents/tools/runtime.py`（ToolRuntime）、
`src/agents/tools/retrieval_executor.py`（批量检索执行器）、
`src/pipelines/document_rag/schemas.py::Evidence`。

---

## 1. 统一证据模型

```python
@dataclass
class Evidence:                    # document_rag/schemas.py:59
    id: str
    content: str
    source_name: str = ""
    source_type: str = "document"
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    backend: str = "ragflow"
    retriever: str = ""
```

`EvidenceEnvelope` 是其治理扩展（项目/基线/源版本/处理产物/文档角色/模块范围/修订/审批状态/
有效期/定位器/事实类型/确定性/权威策略/内容哈希/引用片段/血缘组/检索时间），
用于项目级检索；chat 主链只用 `Evidence`。

**约定**：

- `id` 必须全局可引用（citation 的锚点），各工具自行定义命名空间前缀：
  `xlsx-schema:`、`xlsx-sql:`、`wiki:{slug}`、`conv:{conversation_id}:{message_id}`、
  `circuit-*`（引擎内生成）；
- `metadata` 至少应有 `content_kind` / `processor_kind` / `locator`，
  便于评估（`required_evidence_types`）与 UI 定位（sheet!cell / 页码 / 设计/模块）；
- `score` 语义由各工具自定（见各节），**禁止跨工具比较分数**；
- 证据渲染进提示词时统一截断到 `MAX_EVIDENCE_CONTENT_CHARS = 1200` 字符
  （`runtime.py:22`）。

---

## 2. ToolRuntime（单请求状态容器，`tools/runtime.py`）

| 能力 | 说明 |
|---|---|
| 作用域 | `kb_name`、`ctx`、附件 refs、`source_scope`、`department_id`；构造后不可变 |
| 取消 | `should_cancel()` 回调；工具调用前后检查 |
| 事件 | `emit(event_type, payload)` 经 `src/contracts/agent_events.py` 校验；工具事件用 `tool_started/tool_result` 辅助方法，必须有稳定 `call_id` |
| 证据 | `add_evidence(evidence, admit_limit=...)`（去重 + 预算裁剪 + 可选单次准入上限）、`evidence` 列表、citation 编号分配 |
| 候选 | `candidate_mode` + `candidate_evidence`：检索命中默认先进入不可引用的候选索引，`evidence_read` 再按 id 提升为正式证据 |
| 预算 | `evidence_budget`（有附件时 `max(30, 80)`）、`budget_blocked` 标记与超限提示 |
| 诊断 | `diagnostics`（每工具 status/hit_count/top_k/duration/scoped） |
| 查询账本 | `queries`（每轮检索的 query 指纹/命中数） |
| 记忆 | `memory_context`（隐私投影后的记忆条目）、`add_memory_context()` |
| 参数 | `top_k`（deep 8 / fast 5）、`query_mode`、`wiki_assist` |
| 渲染 | `format_evidence_for_llm(...)`（1200 字符截断、编号、来源行）、`format_tool_result(...)`（**重复调用折叠** + 预算耗尽显式说明）、`empty_result_message(budget_blocked=...)` |

### 2.1 来源类型与来源预算（2026-10-22）

`src/services/evidence_sources.py` 是唯一来源类型判定点：`attachment`、
`knowledge_base(_document|_structured)`、`conversation`、`wiki`、`memory`。
历史 `source_type="chat_attachment"` 只做兼容归一化。缺失身份不再作为通配符；
附件证据不能把知识库义务置为 `admitted`，反之亦然。

`src/services/evidence_budget.py` 提供请求级 `EvidenceBudgetCoordinator`：
全局上限、附件/知识库切片、必要预留、按渲染 UTF-8 字节计量、来源级/全局
`source_budget_exhausted`/`global_budget_exhausted` 诊断。正式证据不淘汰；
候选池可以淘汰。运行时只在 `attachment_and_knowledge_base` + 开关
`EVIDENCE_SOURCE_AWARE_ENABLED=true` 时启用，工具不得自行复制预算状态。

`src/services/evidence_policy.py` 把 `ConversationPlan.evidence_purpose`
（`qa/summarize/extract/compare/author/visual`）映射为候选预览模式与比较双侧义务。
`qa` 默认候选预览；`compare` 要求两侧来源义务都进入 `admitted` 才报告完整比较。
比较工具按 `evidence.compare` 能力开放，由模型自行选择来源和调用时机；
服务端不再根据正则/`purpose` 自动预检，也不会替模型预选附件。

### 2.2 指针优先检索（2026-10-30）

知识库对话默认开启 `ToolRuntime.candidate_mode`：

1. `document_search`、`document_search_batch`、`kb_grep`、`circuit_search`、
   `conversation_search`、`wiki_search`、表格行/单元格检索、附件检索等工具
   返回的全部命中先进入 `candidate_evidence`（非引用、非正式证据）；
2. **不再自动准入**：任何搜索命中都不会直接获得 `[n]`，也不会占用正式证据预算；
3. 命中以 `id / 来源 / 位置 / score / ≤180 字符摘要` 的候选索引返回；
4. 模型判断候选与问题直接相关后调用 `evidence_read(candidate_ids=[...])`，
   选中的候选才进入正式证据账本并获得引用编号；
5. 已提升的候选从候选索引移除，重复检索不会再次提升同一批候选。

`kb_grep` 是精确/正则/多模式指针检索：一次可传多个型号、位号或信号名，
只返回指针，不登记证据；适合 TC377、VCC3V3_EQ 这类精确关键词。

指针持久化在 `storage/pointer_index/<sha>.db`（SQLite FTS5 + `LIKE` 合并），
覆盖 docx/pdf chunk、xlsx 语义行和 EDF 网表/器件；索引按来源记录签名失效，
同一 KB 的重复检索不再重新解析全文。跨轮上下文由
`SessionContextMiddleware` 以紧凑块注入：上一轮问题/结论、未解决缺口、
可用旧证据指针。它不是硬相关性规则，也不注入正文；块内明确提示旧 `[n]`
不可复用，引用前必须 `evidence_read`。完全无关的新问题可以忽略该块，
按全新检索处理。最近 3 轮的已读证据和缺口仍保存在 `session_recall` 缓冲区，
模型也可以主动调用该工具取用。

候选较多时，前 12 条保留摘要，其余以 `id/来源/位置` 紧凑列出，仍可直接
`evidence_read`，不会出现“候选未展开但拿不到 id”的情况。回答收尾前会做
同轮缺口闭环：若草稿出现“未找到/未给出/证据不足”，先把草稿作为 narration
撤回，带着缺口和未读候选再检索一轮，补齐后重新输出最终回答；补齐轮最多
再执行 3 次工具调用、软上限 90 秒，且只在本轮仍有未读候选时触发；首轮已
超过 360 秒的慢轮次直接跳过，保证仍在 600 秒轮次窗口内结束。只有一轮
闭环仍无法补齐时才保留缺口。首轮另有 480 秒软上限：到点停止继续检索，
用已登记证据合成部分结果，避免触发布局层的 600 秒超时。每轮回答还会把缺口句写入
`summary.verification.answer_gaps`，下一轮作为“上一轮遗留缺口”注入；
`evidence_read` 会在问题中的位号或门限数值尚未出现在证据里时，提示改用
`kb_grep`/`circuit_search` 或表格查询。

候选索引受 `candidate_store_limit=60` 约束；模型只对真正要引用的正文付费。

关键函数：

```python
timed_tool_call(rt, name, fn, *, top_k=..., ...)
    # 取消检查 → next_invocation_id()=call_id → 计时 → tool_started →
    # governor tool permit → 执行 → 候选分流/证据准入 → tool_result；
    # 异常 → degraded + 空结果（fail-open）
    # 成功/失败事件共用同一 call_id，前端和持久化投影不再按工具名配对。
format_evidence_for_llm(rt, evidences)      # 注入 LLM 的文本形态
format_candidates_for_llm(rows)             # 候选索引（不含正式证据块）
make_evidence_read(rt)                      # 候选 → 正式证据
budget_message(rt)                          # 预算耗尽时的说明段
format_tool_result(...)                     # 返回给模型的工具结果文本
```

工具工厂统一签名：`make_<x>_tool(rt, <service>, ...) -> LangChainTool`（闭包捕获 rt）。

---

## 3. 工具逐个说明

### 3.1 `document_search`（`tools/document_rag_tool.py`）

| 项 | 值 |
|---|---|
| 参数 | `query`、`top_k`（clamp 到 `rt.top_k` 上限）、可选 `filters` |
| 服务 | `RAGBackend.retrieve(kb, query, top_k, ctx, filters, should_cancel)` |
| 证据 | 直接来自 RAGFlow（`backend="ragflow"`, `retriever="ragflow_retrieval"`） |
| 特殊 | **零命中 fail-open 重试**：当无 `source_names` 且无 `record_id` 过滤时，第二次带 `filters["balanced_route"]=True`（丢 source_group 硬过滤，保留冻结来源），发 `stage="balanced_route_retry"` |
| 已知 | `document_store` 参数实际未使用（`_backend_filters` 在无 `record_id` 时提前返回） |

### 3.2 `circuit_search`（`tools/circuit_tools.py:63`）

| 项 | 值 |
|---|---|
| 参数 | `query`、`top_k`（clamp 1..10）、`design_id`/`query_operation` 等（透传） |
| 服务 | `CircuitIndexService.query` / `typed_query`（见 [10](10-circuit.md)） |
| 作用域 | `KnowledgeBaseCircuitScopeResolver`：department+kb 强制；附件场景用 `AttachmentCircuitScopeResolver`（读 manifest `circuit_root`，多于一个 root 则拒绝） |
| 证据 | `CircuitEvidenceMapper` 产出结构化行（score 0.80~0.99 直接行、0.88 图关系、≤0.69 语义补充） |

### 3.3 表格工具（`tools/spreadsheet_tools.py`）

| 工具 | 作用 |
|---|---|
| `spreadsheet_schema_lookup(query="", top_k=8)` | 浏览表/列/类型/样例；输出按 ≤1100 字符分块（`xlsx-schema:summary:{chunk}`，score 1.0） |
| `spreadsheet_sql_query(sql)` | **LLM 生成只读 SQL**：sqlglot 解析 → 单条 SELECT → 表白名单 → LIMIT 收敛到 200 → 只读连接执行（`file:...?mode=ro` + `PRAGMA query_only=ON`）→ 结果 ≤200 行/2000 字符；证据 id 内容派生 `xlsx-sql:{kind}:{sha1(sql)[:12]}` |
| `spreadsheet_row_search` / `spreadsheet_cell_lookup` | 词法回退：`LOWER(col) LIKE ? ESCAPE '\'` 于 `table_semantic_rows` / `table_cells`；类形态（`SpreadsheetSemanticTool`/`SpreadsheetCellTool`）按 token 命中数重排，过采样 `min(200, max(40, top_k*8))` |

细节见 [09-spreadsheet.md](09-spreadsheet.md)。

### 3.4 `conversation_search`（`tools/external_conversation_tools.py:91`）

| 项 | 值 |
|---|---|
| 参数 | `query`、`top_k`（clamp 1..20） |
| 服务 | `ExternalConversationQueryEngine.search_by_scope` + 可选向量补充 |
| 证据 | `conv:{conversation_id}:{message_id}`；score = `1.0 + 0.1*matched_tokens − 0.05*is_vector + 0.001*position` |
| metadata | `role/origin/source_group/department_id/title` + locator（turn_index/start_offset） |

### 3.5 `wiki_search`（`tools/wiki_tools.py:56`）

| 项 | 值 |
|---|---|
| 参数 | `query`、`top_k`、`include_linked`（可用 `HDB_WIKI_SEARCH_EXPAND` 环境变量强制） |
| 服务 | `WikiService.search_pages`（title/slug/summary/aliases/content 加权 + 一跳链接扩展） |
| 证据 | `Evidence(id="wiki:{slug}", content_kind="wiki_page", processor_kind="wiki", score, locator={slug,hop,via}, metadata={page_type,summary,out_links,version})` |
| 边界 | Wiki 是**蒸馏知识线索**，不是引文权威（回答引用仍指向 `source_refs`/`chunk_refs`） |

### 3.6 `memory_search`（`tools/memory_tools.py:18`）

```python
memory_search(query: str, scope: str = "all", status: str = "all", top_k: int = 5) -> str
```

- `top_k` clamp 1..20；使用 `rt.ctx` 与 `rt.kb_name`（**不接受调用方传身份**）；
- 返回 JSON `{"results":[记忆投影...], "context": 格式化上下文}`；
- 错误：`MemoryAuthorizationError → {"results":[], "error":"当前请求没有可用的长期记忆范围"}`；
  其它异常 → `{"results":[], "error":"长期记忆暂时不可用"}`；两者都发 `tool_result failed`；
- 在 `_ALWAYS_AVAILABLE` 中：通用对话也会挂载；
- 记忆内容只是「历史线索」，提示词明确要求忽略其中的操作性指令。

### 3.7 `list_kb_sources`（`tools/pipeline_catalog.py:116`）

非检索工具：列出当前 KB 里可用的文档/表格/电路/外部对话来源（含可用性与过滤）。
用途：让模型知道「有哪些材料可查」，避免盲目检索。底层 `scan_kb_sources` 汇总：
`PipelineDocumentStore` 记录 + `SpreadsheetIndexService.rank_document_matches`
（token 化硬件查询词，最多 8 个）、`CircuitIndexService` 设计清单、外部对话列表。

### 3.8 附件工具族（`tools/attachment_tools.py`）

`build_attachment_tools(rt)`：作用域不允许或无 refs → 返回 `[]`。

| 工具 | 契约 |
|---|---|
| `attachment_list()` | 列出冻结 refs 的文件名/解析态/降级原因/候选修订与放行状态；每次重新校验 ACL |
| `attachment_search(query, top_k=6)` | RRF 混合检索（FTS5 + trigram + exact/normalized scan + 可选 dense），top_k clamp 1..12；只接受属于冻结 refs 的 asset |
| `attachment_read(query="", ordinal_from=0, limit=4)` | 读存储分块（跳过空/图片分块），limit clamp 1..10；越界时明确提示改用 `attachment_search` |
| `attachment_table_query(query="", sql="", top_k=8)` | 仅 `.xlsx/.xlsm`；附件私有表索引；复用 SQL 只读校验链；top_k clamp 1..20 |
| `attachment_circuit_search(query, top_k=5)` | 仅 `.edf/.edif`；附件私有 CircuitStore（`ctx=None`）；top_k clamp 1..10 |
| `attachment_visual_analyze(question, page=0, pages="")` | 视觉分析（需双开关）；支持 `a-b` 范围（≤20/段）、默认第 1 页、继承历史 locator |
| `attachment_edit(...)` | 创建修订候选（永不改原件）；含引用文本护栏、嵌入物删除护栏、目标解析与血缘追溯 |
| `request_attachment_release(revision_id="", attachment_id="", comment="")` | 仅登记放行请求，不执行放行 |
| `attachment_revision_history(attachment_id="")` | 按版本倒序列出修订（操作/前后文件名/放行状态/变更摘要） |

`attachment_edit` 的护栏细节（fail-closed）：

- `rt.attachment_edit_enabled`（来自 `allow_document_write = document_flow is not False`）；
- 引文护栏：用户问题里出现的引号片段必须与 `before` 一致（`attachment_target_not_explicit`）；
- 嵌入物删除护栏：`preserve_embedded=false` 需要问题里出现显式删除措辞；
- 目标解析：显式 revision id → 文件名/asset 别名（唯一时）→ `latest/最新候选` 选择器 →
  “继续/最新”问题强制最新血缘候选（否则 `attachment_version_mismatch`）；
- 血缘遍历最多 200 条 session 修订；
- 成功事件：`attachment_revision` + `document_card`（含下载 URL）。

## 4. 批量检索执行器（`tools/retrieval_executor.py`，278 行）

用途：批量检索的**单元级并行执行器**。

| 配置 | 默认 | 说明 |
|---|---|---|
| 共享检索执行器 | 代码内常量 `true` | 内置共享线程池；测试可覆盖，不是部署开关 |
| `RETRIEVAL_EXECUTOR_MAX_WORKERS` | `8` | 池大小 |
| `RETRIEVAL_QUEUE_MAX_DEPTH` | `256` | 队列深度 |
| `RETRIEVAL_PER_REQUEST_FANOUT` | `4` | 单请求扇出 |
| `RETRIEVAL_QUEUE_MAX_WAIT_SECONDS` | `30` | 入队等待上限 |
| `RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS` | `10` | 关停排空期限 |

行为：每个检索项返回状态 `ok | failed | cancelled | saturated`；
饱和时**明确上报**而不是无限等待；关停时最多排空到 deadline，未确认的调用按「已租用但未完成」处理；
指标 `hdb.retrieval.executor.attempts` / `…queue_wait`。

---

## 5. 证据生命周期（从工具到回答）

```
工具返回 Evidence[]
   ↓ candidate_mode：全部命中进入 candidate_evidence（不可引用、不占预算）
   ↓ 搜索不自动准入；候选保持指针状态
   ↓ evidence_read(candidate_ids)（模型按需提升候选）
ToolRuntime.evidence 列表（预算内；超限标记 budget_blocked）
   ↓ format_evidence_for_llm（≤1200 字符/条 + 编号 + 来源行 + 定位器）
注入模型上下文（下一轮工具结果 / 最终回答前的证据段落）
   ↓ 回答中的 [n] 引用
done 事件的 summary.evidence  → 前端引用面板（可展开、可定位）
   ↓ 导出时
ResultSnapshot（citation 列表：index/title/locator/excerpt/source_type/evidence_id）
   ↓ 评估时
ragas 的 retrieved_contexts / evidence_consistency 指标
```

**引用编号规则**：`metadata["citation_number"]` 由 ToolRuntime 分配，回答中的 `[n]` 与之对应；
`_build_claim_coverage` 依赖这个映射生成 citation 级覆盖（词法近似）。

### 5.x 模型自行比较，无专用比较工具（2026-10-26）

Agent 工具面不再提供 `document_compare` / `comparison_expand`，整篇文档比较
服务及其只读报告 API 也已删除。比较是模型的行为：模型用普通 `document_search`、
`attachment_search`、`attachment_read` 等工具读取两侧内容并自行对比，不再由
路由或预检自动触发。

---

## 6. Fail-open / Fail-closed 清单（工具层）

| Fail-open（吞错、降级、空结果） | Fail-closed（拒绝执行） |
|---|---|
| 所有检索工具异常（`degraded{stage:"tool_<name>"}`） | 作用域解析缺 department/kb（`PermissionError`） |
| `memory_search` 授权失败/不可用 | 附件 refs 越权/失效（`attachment_unavailable`） |
| 附件检索/视觉失败 | `attachment_edit` 的各种护栏 |
| wiki 检索异常 | SQL 非只读/多语句/表不在白名单 |
| 图表/表格证据为空 | 引用精确匹配失败（`before` 不唯一 → 拒绝而不是猜） |
| 共享检索池饱和（上报 `saturated`） | 文档工具未授权（`PermissionError`） |

---

## 7. 扩展一个新工具的最小步骤

1. 在 `src/agents/tools/` 新建 `make_<name>_tool(rt, service)`，用 `timed_tool_call` 包裹实现；
2. 在 `runner.py` 注册（条件挂载）；
3. 在 `tool_assembly.TOOL_CAPABILITIES` 登记能力名，否则统一模式下会被过滤；
4. 若是检索类：产出 `Evidence`（id/metadata/locator 齐全），并在 `TOOL_LABELS` 加中文名；
5. 补测试：工具契约 + 作用域拒绝路径 + 降级路径；参考
   `tests/test_conversation_search_tool.py`、`tests/test_spreadsheet_tools*.py`。
