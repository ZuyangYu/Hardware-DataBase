# 知识库直连文档生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让登录用户自行选择其已授权知识库，直接创建、运行、查看和审批文档生成任务，而无需项目或项目基线。

**Architecture:** 为工作单引入显式的 `knowledge_base` 输入作用域和独立的、可哈希的知识库来源快照；保留项目作用域的旧工作单和旧接口。服务层根据工作单作用域执行知识库权限或项目权限校验，Pipeline 为知识库工作单构造仅查询冻结知识库及冻结来源名的检索器，页面只显示当前用户可读的知识库。

**Tech Stack:** Python 3、Pydantic、SQLite、Streamlit、现有 RAG backend、pytest、Ruff。

## Global Constraints

- 不创建隐藏项目，不要求项目或基线，且不迁移或删除已有项目型工作单。
- 任务只可使用 `RequestContext` 中具备 `read` 权限的知识库；权限撤销后禁止运行、查看状态、下载与审批。
- 知识库来源快照必须保存知识库名、创建人、创建时间、来源文档名和内容哈希；检索只能使用该快照范围。
- 继续使用已确认的安全模板、匹配的已批准 Document Schema、Harness 进度、产物净化校验和候选审批。
- 保留工作区中已有的未提交自动生成、LLM 与评估改动；仅在功能确实重叠时整合，不重置或覆盖它们。

---

## File structure

- `src/document_authoring/models.py`：定义工作单输入作用域与知识库来源快照模型。
- `src/document_authoring/work_order_store.py`：迁移并持久化作用域字段、知识库来源快照和按作用域列出的工作单。
- `src/document_authoring/service.py`：创建知识库工作单、统一来源快照解析、按作用域授权、执行与审批守卫。
- `src/core/app_pipeline.py`：公开已授权知识库的生成选项、知识库检索器和知识库作用域任务查询。
- `src/ui/document_generation_page.py`：以知识库选择器替换项目/基线选择器，并展示知识库作用域的任务列表。
- `tests/test_knowledge_base_document_work_orders.py`：覆盖快照、创建、权限撤销、作用域隔离与旧项目兼容。
- `tests/test_document_generation_page.py`：覆盖知识库选择页面和无授权知识库提示。
- `docs/Hardware-DataBase_Agentic-RAG改造方案.md`：记录知识库直连入口和不再需要项目管理的测试路径。

### Task 1: 定义并持久化知识库来源作用域

**Files:**
- Modify: `src/document_authoring/models.py:DocumentWorkOrder、AuthoringRunManifest`
- Modify: `src/document_authoring/work_order_store.py:_initialize_schema、create_work_order、find_work_order_by_idempotency、list_work_orders`
- Create: `tests/test_knowledge_base_document_work_orders.py`

**Interfaces:**
- Consumes: `DocumentWorkOrder` 与项目型 `SourceSetSnapshot` 的既有 `source_set_snapshot_id`/`content_hash` 契约。
- Produces: `KnowledgeBaseSourceSnapshot`、`DocumentWorkOrder.scope_type`、`DocumentWorkOrder.knowledge_base_name`、`WorkOrderStore.create_knowledge_base_source_snapshot()`、`WorkOrderStore.get_knowledge_base_source_snapshot()`、`WorkOrderStore.list_work_orders_for_knowledge_base()`。

- [ ] **Step 1: 写入失败测试，要求工作单可保存知识库作用域且项目字段为空**

```python
def test_store_round_trips_knowledge_base_work_order(tmp_path):
    store = WorkOrderStore(tmp_path / "authoring.db")
    snapshot = KnowledgeBaseSourceSnapshot.create(
        tenant_id="tenant-a", knowledge_base_name="hardware", source_names=["spec.pdf"], created_by="alice",
    )
    store.create_knowledge_base_source_snapshot(snapshot)
    order = make_work_order(
        scope_type="knowledge_base", knowledge_base_name="hardware", project_id=None,
        baseline_id=None, baseline_content_hash="", source_set_snapshot_id=snapshot.source_set_snapshot_id,
    )
    store.create_work_order(order)
    assert store.get_work_order(order.work_order_id).knowledge_base_name == "hardware"
    assert store.list_work_orders_for_knowledge_base("tenant-a", "hardware") == [order]
```

- [ ] **Step 2: 运行测试，确认当前模型或存储不支持它**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base_document_work_orders.py::test_store_round_trips_knowledge_base_work_order -q`

Expected: FAIL，原因是 `KnowledgeBaseSourceSnapshot`、知识库作用域字段或存储方法不存在。

- [ ] **Step 3: 增加不可变模型和 SQLite 迁移**

```python
class KnowledgeBaseSourceSnapshot(BaseModel):
    source_set_snapshot_id: str
    tenant_id: str
    knowledge_base_name: str
    source_names: list[str]
    created_by: str
    created_at: datetime = Field(default_factory=utc_now)
    content_hash: str = ""

    @model_validator(mode="after")
    def bind_content_hash(self):
        expected = content_hash(self.model_dump(mode="json", exclude={"content_hash"}))
        if self.content_hash and self.content_hash != expected:
            raise ValueError("knowledge-base source snapshot hash does not match contents")
        self.content_hash = expected
        return self
```

把 `DocumentWorkOrder.project_id` 和 `baseline_id` 改为可空字段，增加 `scope_type: Literal["project", "knowledge_base"] = "project"` 与 `knowledge_base_name: str | None = None`，并在验证器中要求：项目作用域必须有项目和基线；知识库作用域必须有知识库名且不得有项目或基线。重建 `document_work_orders` 表以使用 `scope_key` 作为唯一幂等范围；复制旧行时填入 `scope_type='project'` 和 `scope_key='project:' || project_id`。新增 `knowledge_base_source_snapshots` 表，持久化完整 JSON 和哈希。

- [ ] **Step 4: 运行模型与存储测试**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base_document_work_orders.py tests/test_document_authoring_store.py -q`

Expected: PASS；旧项目型工作单与新知识库工作单均可读取。

- [ ] **Step 5: 提交作用域持久化变更**

```bash
git add src/document_authoring/models.py src/document_authoring/work_order_store.py tests/test_knowledge_base_document_work_orders.py
git commit -m "feat: persist knowledge-base document work orders"
```

### Task 2: 在服务层创建、解析和授权知识库工作单

**Files:**
- Modify: `src/document_authoring/service.py:create_document_work_order、_order、_harness_run_for_context、approve_document_artifact、download_document_artifact、_validate_retrieval_outcome`
- Modify: `src/document_authoring/harness/runtime.py:build_manifest`
- Modify: `src/document_authoring/harness/graph.py:状态初始化与检索结果校验`
- Modify: `tests/test_knowledge_base_document_work_orders.py`

**Interfaces:**
- Consumes: `KnowledgeBaseSourceSnapshot` 和 `RequestContext.has_kb_permission(kb_name, "read")`。
- Produces: `DocumentGenerationService.create_knowledge_base_work_order()`、`DocumentGenerationService.resolve_source_snapshot()`、`DocumentGenerationService.require_work_order_capability()`。

- [ ] **Step 1: 写入失败测试，要求服务无需项目即可创建任务，并在撤权后拒绝访问**

```python
def test_knowledge_base_work_order_requires_live_read_permission(service, ctx, approved_template, approved_schema):
    order = service.create_knowledge_base_work_order(
        ctx, knowledge_base_name="hardware", source_names=["spec.pdf"],
        template_version_id=approved_template.template_version_id,
        document_schema_id=approved_schema.document_schema_id,
        document_schema_version=approved_schema.version,
    )
    assert order.scope_type == "knowledge_base"
    ctx.kb_permissions.clear()
    with pytest.raises(PermissionError, match="knowledge base"):
        service.require_work_order_capability(ctx, order, "run_deterministic_work_order")
```

- [ ] **Step 2: 运行测试，确认创建入口尚不存在**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base_document_work_orders.py::test_knowledge_base_work_order_requires_live_read_permission -q`

Expected: FAIL，原因是 `create_knowledge_base_work_order` 不存在。

- [ ] **Step 3: 实现作用域统一的服务帮助方法与创建入口**

```python
def require_work_order_capability(self, ctx: RequestContext, order: DocumentWorkOrder, capability: str) -> None:
    if order.scope_type == "knowledge_base":
        if not order.knowledge_base_name or not ctx.has_kb_permission(order.knowledge_base_name, "read"):
            raise PermissionError("knowledge base access is required for this work order")
        return
    self.projects.access.require(ctx, order.project_id, capability)

def create_knowledge_base_work_order(self, ctx: RequestContext, *, knowledge_base_name: str,
                                     source_names: list[str], template_version_id: str,
                                     document_schema_id: str, document_schema_version: str,
                                     idempotency_key: str | None = None) -> DocumentWorkOrder:
    if not ctx.has_kb_permission(knowledge_base_name, "read"):
        raise PermissionError("knowledge base read permission is required")
    snapshot = self._create_knowledge_base_source_snapshot(ctx, knowledge_base_name, source_names)
    return self._create_frozen_work_order(ctx, scope_type="knowledge_base", snapshot=snapshot,
        knowledge_base_name=knowledge_base_name, template_version_id=template_version_id,
        document_schema_id=document_schema_id, document_schema_version=document_schema_version,
        idempotency_key=idempotency_key)
```

让 Manifest、Harness Graph 和检索结果校验调用 `resolve_source_snapshot(order)`，返回统一的 `source_set_snapshot_id`、`content_hash` 与来源名。知识库作用域检索证据必须具有相同 `metadata["knowledge_base_name"]`，其 `source_name` 必须在冻结 `source_names` 内；项目作用域继续执行现有版本/区域策略校验。所有状态、Harness 控制、人工事件、批准和下载入口都改为先调用 `require_work_order_capability()`。

- [ ] **Step 4: 运行服务与 Harness 回归测试**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base_document_work_orders.py tests/test_document_authoring_integration.py tests/test_harness_runtime.py -q`

Expected: PASS；撤权、伪造知识库名与跨知识库证据会被拒绝，项目型回归保持通过。

- [ ] **Step 5: 提交服务层变更**

```bash
git add src/document_authoring/service.py src/document_authoring/harness/runtime.py src/document_authoring/harness/graph.py tests/test_knowledge_base_document_work_orders.py
git commit -m "feat: create governed knowledge-base work orders"
```

### Task 3: 提供知识库选项、快照和检索器的 Pipeline 入口

**Files:**
- Modify: `src/core/app_pipeline.py:list_document_generation_options、list_document_work_orders、auto_generate_document、get_document_run_status`
- Modify: `tests/test_knowledge_base_document_work_orders.py`

**Interfaces:**
- Consumes: `AppPipeline.list_knowledge_bases(ctx)`、`DocumentGenerationService.create_knowledge_base_work_order()`、`DocumentGenerationService.resolve_source_snapshot()`。
- Produces: `AppPipeline.list_knowledge_base_document_generation_options(ctx)`、`AppPipeline.create_knowledge_base_document_work_order(ctx, ...)`、`AppPipeline.list_knowledge_base_document_work_orders(ctx, knowledge_base_name)`、`AppPipeline.auto_generate_knowledge_base_document(ctx, ...)`、`DocumentGenerationService.build_knowledge_base_retrieval_outcome()`。

- [ ] **Step 1: 写入失败测试，要求检索器只调用选定知识库并过滤冻结来源名**

```python
def test_pipeline_knowledge_base_retriever_is_scoped(pipeline, ctx):
    retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["spec.pdf"])
    retrieve(requirement("voltage"), 0)
    pipeline.backend.retrieve.assert_called_once_with(
        "hardware", "voltage", top_k=ANY, ctx=ctx, filters={"source_names": ["spec.pdf"]},
    )
```

- [ ] **Step 2: 运行测试，确认 Pipeline 入口尚不存在**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base_document_work_orders.py::test_pipeline_knowledge_base_retriever_is_scoped -q`

Expected: FAIL，原因是 `_knowledge_base_retriever` 不存在。

- [ ] **Step 3: 实现知识库作用域 Pipeline 方法**

```python
def list_knowledge_base_document_generation_options(self, ctx: RequestContext) -> dict[str, list]:
    return {
        "knowledge_bases": self.list_knowledge_bases(ctx),
        "templates": self.document_generation.store.list_templates(approved_only=True),
        "schemas": self.document_generation.store.list_document_schemas(approved_only=True),
    }

def _knowledge_base_retriever(self, ctx: RequestContext, kb_name: str, source_names: list[str]):
    if not ctx.has_kb_permission(kb_name, "read"):
        raise PermissionError("knowledge base read permission is required")
    def retrieve(requirement, _attempt):
        query = " ".join(value for value in (requirement.subject, requirement.predicate, requirement.object_hint) if value)
        evidences = self.backend.retrieve(kb_name, query, top_k=config.settings.FINAL_TOP_K, ctx=ctx,
                                          filters={"source_names": source_names})
        return self.document_generation.build_knowledge_base_retrieval_outcome(kb_name, source_names, evidences)
    return retrieve
```

创建任务前通过 `list_file_infos(kb_name, ctx)` 获取可读来源名并写入快照；空知识库返回可操作错误。自动生成从创建后的工作单读取其快照，而不是信任 UI 再次提交的文件列表。状态返回中增加安全的 `scope_type` 与 `knowledge_base_name`，不暴露原始来源内容。任务列表只返回调用者当前仍有读取权限的知识库范围。

- [ ] **Step 4: 运行 Pipeline 测试**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base_document_work_orders.py tests/test_document_auto_generation.py -q`

Expected: PASS；自动生成和普通任务均只检索所选知识库。

- [ ] **Step 5: 提交 Pipeline 变更**

```bash
git add src/core/app_pipeline.py tests/test_knowledge_base_document_work_orders.py
git commit -m "feat: retrieve document evidence from selected knowledge base"
```

### Task 4: 将 Streamlit 文档生成流程切换为知识库选择

**Files:**
- Modify: `src/ui/document_generation_page.py:render_document_generation_page、_render_work_order_creation、_render_durable_runs`
- Modify: `tests/test_document_generation_page.py`

**Interfaces:**
- Consumes: `AppPipeline.list_knowledge_base_document_generation_options()`、`create_knowledge_base_document_work_order()`、`auto_generate_knowledge_base_document()` 与 `list_knowledge_base_document_work_orders()`。
- Produces: “已授权知识库”选择器、无授权/空知识库提示、知识库作用域任务筛选与既有 Harness 状态显示。

- [ ] **Step 1: 写入失败页面测试，要求没有项目也能显示知识库选择器**

```python
def test_work_order_page_uses_authorized_knowledge_bases_not_projects(fake_st, pipeline, ctx):
    pipeline.list_knowledge_base_document_generation_options.return_value = {
        "knowledge_bases": ["hardware"], "templates": [approved_template], "schemas": [approved_schema],
    }
    _render_work_order_creation(fake_st, pipeline, ctx)
    assert ("已授权知识库", ["hardware"]) in fake_st.selectboxes
    pipeline.list_accessible_projects.assert_not_called()
```

- [ ] **Step 2: 运行测试，确认当前页面仍依赖项目列表**

Run: `.venv/bin/python -m pytest tests/test_document_generation_page.py::test_work_order_page_uses_authorized_knowledge_bases_not_projects -q`

Expected: FAIL，原因是页面调用 `list_accessible_projects()`。

- [ ] **Step 3: 替换新建与任务列表的项目控件**

```python
options = pipeline.list_knowledge_base_document_generation_options(ctx)
kb_name = st.selectbox("已授权知识库", options["knowledge_bases"], key="document-generation-kb")

if st.button("创建生成任务", type="primary", key="create-document-work-order"):
    order = pipeline.create_knowledge_base_document_work_order(
        ctx, knowledge_base_name=kb_name, template_version_id=template_id,
        document_schema_id=schema.document_schema_id, document_schema_version=schema.version,
        idempotency_key=f"streamlit-kb-{uuid.uuid4().hex}",
    )
```

保留模板、Schema 匹配、自动生成、进度时间线、暂停、取消、下载和批准控件；删除页面中的项目和配置基线下拉框及“冻结前的项目来源目录”文字。无可读知识库时显示“当前账号没有可用于文档生成的知识库，请联系管理员授权知识库。”；任务与下载页以同一知识库选择器过滤。

- [ ] **Step 4: 运行页面测试**

Run: `.venv/bin/python -m pytest tests/test_document_generation_page.py -q`

Expected: PASS；知识库选择、空态、创建和任务筛选均覆盖。

- [ ] **Step 5: 提交 UI 变更**

```bash
git add src/ui/document_generation_page.py tests/test_document_generation_page.py
git commit -m "feat: create document tasks from authorized knowledge bases"
```

### Task 5: 文档、全量验证与回归检查

**Files:**
- Modify: `docs/Hardware-DataBase_Agentic-RAG改造方案.md`
- Modify: `tests/test_knowledge_base_document_work_orders.py`

**Interfaces:**
- Consumes: 前四项的知识库直连入口和现有安全模板工作流。
- Produces: 用户可按页面完成测试的操作说明和完整验证记录。

- [ ] **Step 1: 增加端到端失败测试，覆盖无项目的用户直接自动生成**

```python
def test_user_with_only_kb_permission_can_auto_generate_without_project(pipeline, ctx):
    result = pipeline.auto_generate_knowledge_base_document(
        ctx, knowledge_base_name="hardware", template_version_id="template-1",
        document_schema_id="schema-1", document_schema_version="1",
    )
    assert result.stage in {"approved_release", "review_candidate"}
```

- [ ] **Step 2: 运行端到端测试，确认需求被覆盖**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base_document_work_orders.py::test_user_with_only_kb_permission_can_auto_generate_without_project -q`

Expected: PASS；调用过程中不创建或读取项目。

- [ ] **Step 3: 更新用户操作说明**

在改造方案的文档生成章节加入：登录项目成员账号后进入“文档生成”，在“已授权知识库”选择来源、上传并启用安全模板、选择匹配 Schema、创建或自动生成任务；明确项目管理不是该流程的前置条件，并说明撤销知识库权限会停止后续访问。

- [ ] **Step 4: 执行完整验证**

Run: `.venv/bin/python -m pytest tests/test_knowledge_base_document_work_orders.py tests/test_document_generation_page.py tests/test_template_sanitizer.py tests/test_template_upload_service.py tests/test_template_authoring_integration.py tests/test_document_auto_generation.py -q`

Expected: 全部通过。

Run: `.venv/bin/ruff check src/document_authoring src/core/app_pipeline.py src/ui/document_generation_page.py tests/test_knowledge_base_document_work_orders.py tests/test_document_generation_page.py`

Expected: `All checks passed!`。

- [ ] **Step 5: 提交说明与验证测试**

```bash
git add docs/Hardware-DataBase_Agentic-RAG改造方案.md tests/test_knowledge_base_document_work_orders.py
git commit -m "docs: describe knowledge-base document generation workflow"
```
