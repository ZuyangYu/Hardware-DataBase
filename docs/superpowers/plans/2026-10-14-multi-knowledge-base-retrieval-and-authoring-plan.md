# 多知识库检索与跨项目文档生成改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.
>
> **编制日期：** 2026-10-14  
> **方案版本：** R2，按仓库核验与第二轮评审修订（2026-10-14）  
> **目标仓库：** Hardware-DataBase-observability  
> **核对基线：** feature/admin-directory-management / 3739b4b  
> **状态：** 方案与实施计划，尚未实施。本文列出的新增接口、模型、设置和测试均为拟新增内容。  
> **评审对象：** [用户提供的共享会话](https://chatgpt.com/share/6acf6c94-b8d0-83ea-aab8-65a074da7969)，重点是其中第八至十一节的改造建议。分享中后续 GitLab CI/CD 讨论不属于本次范围。

**Goal:** 让同部门、具有相应读取权限的用户，在一次对话或文档任务中显式选择多个逻辑知识库，综合检索并准确保留项目、文件和版本出处。

**Architecture:** 保留现有物理索引、解析器和单库检索边界，在应用层增加来源范围解析、受控检索调度和证据融合。对话采用固定来源集合，文档生成采用带文档身份和内容指纹的来源快照；授权在执行时复查，来源角色决定使用方式，不授予权限。

**Tech Stack:** Python 3.12+、FastAPI、Pydantic、现有 deepagents/LangGraph、RAGFlow、现有表格/电路/外部对话/Wiki 检索、SQLite/PostgreSQL、React/TypeScript、pytest、Vitest、Playwright。

**Spec:** 本文件的[设计约定](#design-contract)是实施依据；设计、任务和验收矩阵保持在同一文件，实施者须先读设计，再执行任务。

## Global Constraints

- 兼容现有单库请求、会话、工单和历史数据；不把所有旧会话批量转换为多库会话。
- 读取范围只包含当前用户可读取的本部门知识库；保持 system_admin 无内容访问权的现有规则。
- 保留单库 RAGFlowBackend.retrieve(kb_name, query, ...) 的职责；不通过取消知识库过滤实现跨库。
- 保留 source_scope 对附件/知识库的现有语义；多库选择使用独立的 knowledge_scope 字段。
- 用户未选择的库不参与检索；显式提交了不可读取的库时，整次请求拒绝，不悄悄删库后回答。
- 来源选择、来源内容、访问许可分别处理：选择快照不冻结权限，记录版本字段不自动获得历史版本读取能力。
- 每个正式发布阶段都必须完成 SQLite 与 PostgreSQL 的模型、持久化、迁移及 Worker 恢复验收。首个内部演示可先使用隔离 SQLite，但不算 P0 发布完成，也不能部署到 PostgreSQL 生产实例。
- 多库服务复用现有共享检索池与资源治理；旧单库批量检索仍由原开关决定执行分支。池初始化、关闭和兼容验证由独立 T0 承担，不把开启原全局开关作为多库的必需前置变更。
- 不在检索线程池的任务内再次阻塞等待同一个线程池；错误状态、权限失效和冻结来源变化不得降级成正常零命中。
- 业务文案使用中文；来源角色不能覆盖经过验证的版本、审批状态或正式规范。
- 本轮交付是本方案文件。业务实现、数据库迁移、服务重启和部署由后续实施任务执行。

---

## R2 修订记录

| 项目 | 本版决定 | 对应任务/验收 |
| --- | --- | --- |
| 文件哈希与片段哈希 | 明确 source_content_hash 与 evidence_text_hash 的不同来源；复合身份继续包含 chunk 定位 | T4/T5、A27 |
| 非 strict 证据元数据 | 所有通过台账验证的文档命中都补 local_record_id、真实库身份及两个哈希；P0 不因字段缺失而被迫切换为冻结检索 | T4、A28 |
| 检索回退 | P0 允许本地白名单校验下的回退；P2 首版拒绝远端冻结范围请求被拒后的宽检索 | §3.5.1、T4/T8、A29/A30 |
| 状态区别 | 新路径使用结构化结果，区分零命中、来源不可用、指纹变化与过滤不支持；旧 list 返回接口保持兼容 | T4/T8、A31 |
| 共享执行器 | 增加 T0 生命周期与兼容前置任务；多库使用池不等于翻转旧单库批量检索开关 | T0、A32 |
| 首个演示 | 增加 M1：2 库、answer、仅文档检索、隔离 SQLite 的纵向闭环；双后端仍是正式发布门禁 | §6.1/§6.2 |
| 来源变更与 409 | 保留草稿，引导新建会话；附件和模板绑定重新验证 | T6、A33 |
| 撤权后的历史 | 按消息/产物的直接及继承来源依赖控制正文显示，不能仅凭引用列表证明无依赖 | T3/T10、A34 |
| 检索质量 | Reranker 仍可选；相关性与必要来源覆盖成为 P0 的必过评测门禁 | T5/T11、A35 |
| 记忆恢复 | 列为范围独立、带明确验收条件的后续工作项 F1 | §3.4.1 |
| 排期 | 增加任务依赖关系和增量工程人日区间；M1 完成后重新校准 | §6.1/§6.2 |

R2 仅修订本方案文档。所有新增接口、测试、配置和里程碑仍是待实施契约；文档中的代码片段不代表业务代码已修改。

## 1. 原方案评审

### 1.1 评审结论

**方向合理，可以作为设计起点；补齐以下约束后适合实施。**

原方案正确识别了“单库范围限制贯穿 API、会话、工具运行时和文档生成”，建议保留共享物理索引、按逻辑库分别授权检索、在应用层融合结果。它也正确区分了普通 KB 文档生成与 Project 治理路径，没有把底层多 dataset 能力直接等同于用户可用的多库功能。

原方案属于架构建议，尚不是可直接照做的完整迁移计划。本修订重点解决持久化、历史上下文、版本真实性、异构检索、失败语义和兼容发布。

| 原建议 | 判断 | 本修订的决定 |
| --- | --- | --- |
| 保留物理索引，在单库检索之上增加多库编排 | 采纳 | 单库授权、文档台账和回读验证继续生效 |
| 用稳定 kb_id 选择来源 | 采纳并细化 | 服务端按 ID 解析数据库事实，验证部门、当前权限和 ID/名称对应关系 |
| 会话默认集合与每轮来源快照 | 需要补充 | 首轮提交后集合固定；改变集合、角色或用途创建新会话，避免旧历史和 checkpoint 带入已移除来源 |
| 多库逐一构造 RequestContext | 采纳 | 每库独立对象；不能复用并修改同一个 metadata 字典；执行前重新读取权限 |
| 使用 RRF 统一处理不同检索分数 | 有条件采纳 | RRF 是排名融合基线，不等价于跨库语义重排；加入来源覆盖约束，统一 reranker 作为后续可选优化 |
| 冻结来源后继续使用启动时版本 | 需要修正 | 只有实际保留并能定位旧处理产物时才能继续；当前阶段遇到版本/哈希漂移必须报告 source_changed，禁止换成最新版本 |
| 强烈复用 Project SourceSetSnapshot | 采纳概念，不强绑业务入口 | 共用来源身份和快照适配能力；普通多库问答/写作不要求用户先建 Project、审批基线 |
| 只修改 conversation.py 持久化 | 不完整 | 同步 PostgreSQL store、facade、固定 DDL、schema version、导入/迁移和 Worker 恢复 |
| 复用 retrieval_executor 处理超时 | 需要修正 | 当前 map_bounded 的 deadline 主要控制排队准入；新增运行预算/取消语义，传输超时单独约束 |
| 新增 multi_kb_document_search | 可行但非首选 | 优先保留 document_search/document_search_batch 工具名，由运行时注入范围；定向查询只接受服务端给出的来源引用 |
| 文档先跨库，结构化检索后补 | 采纳分阶段 | P0 明示只支持多库文档检索；P1 补齐结构化来源；P2 才作为跨库工程文档完整交付 |
| 来源角色 primary/reference | 采纳并细化 | 比较任务使用 peer；standard 仅表示用途，不能由客户端声明为“已批准规范” |
| 引用按完整身份去重 | 采纳 | 同名、相同 chunk_id、相同内容但不同项目/版本都不能丢失出处；前端按真实来源访问 |

### 1.2 两个容易被忽略的技术问题

**RRF 不能独自解决跨库相关性。** 对互不重叠的 A、B 两个结果列表，若每条证据只出现在一个列表，A 的第一名和 B 的第一名都会得到 1/(k+1)。这只能表达各库内部名次，不能证明两者与原问题同样相关。本文采用“每来源候选质量过滤 + 排名融合 + 必要来源覆盖”，后续以评测决定是否加入统一 reranker。

**旧快照不等于内容版本快照。** 当前 KnowledgeBaseSourceSnapshot 保存的是 knowledge_base_name、source_names 等字段；对该结构做哈希只能证明名单未变，不能证明同名文件内容未变。Project 路径也有按 document.title 搜索后附加 source_version_id 的适配逻辑。新方案要求检索前后验证实际文档/产物身份，不能只给新结果补一个旧版本标签。

### 1.3 第二轮评审采纳边界

第二轮评审对范围、迁移、执行器和前端交互的风险判断予以采纳，但两项推论作如下校正：

- 文件级哈希相同不会单独导致不同 chunk 的复合身份碰撞，因为身份还包含 chunk 定位。需要修订的是字段语义与赋值路径，不是认定 A09 无法测试。
- 远端省略 document_ids 不必然使应用输出越界，现有返回结果还经过本地文档台账验证。R2 为 P2 选择更严格的“远端冻结范围请求被拒则停止”策略，这是明确的首版产品/工程决策；不能反推原有本地白名单机制没有作用。

现有 strict 路径还会把记录不完整或版本戳不匹配表示为空列表。新多库路径必须修复这种状态信息丢失，否则 P2 无法可靠区分 source_changed 与 success_empty。

“多库召回后融合或重排”是可参考的产品模式；本文不将 Dify、LlamaIndex、LangChain 描述成统一使用 RRF(k=60)。k=60 是本方案的初始排名融合参数，相关性仍须本项目数据评测。

## 2. 已核对的仓库基础

以下路径相对仓库根目录，行号仅作本次基线定位，实施时以函数名定位。

| 现有文件/入口 | 已确认行为 | 对实施的影响 |
| --- | --- | --- |
| src/api/schemas.py：CreateSessionRequest、CreateTurnRequest | 会话只有一个 kb_name；source_scope 控制附件与 KB | 新增字段，保持旧字段含义 |
| frontend/src/pages/chat/useKbChat.ts：useKbChat | 按单库保存会话选择、加载历史 | 多库会话仍保留导航归属库，并展示真实检索来源 |
| src/api/context.py：build_context_for_user | 根据用户本部门和一个 KB 构造上下文 | 按库构造，不能依赖前端传部门 |
| src/core/conversation.py | SQLite 会话、轮次、幂等、摘要与队列 | 新范围必须随轮次原子持久化 |
| src/storage/control_plane/conversation_service.py、postgres.py | PostgreSQL facade/store 是独立路径 | 不能只改单机 SQLite |
| src/storage/control_plane/schema.py、schema_version.py | 固定 DDL 与 readiness 版本检查 | 需要显式迁移；应用启动不替代生产迁移 |
| src/agents/runner.py：stream、create_deep_agent | 单个 ToolRuntime；持久化 thread 会恢复历史 | 范围变化涉及 checkpoint 与摘要隔离 |
| src/agents/tools/runtime.py：add_evidence、finalize_evidence | 按证据 ID 去重、分配引用编号、控制预算 | 必须保留融合顺序与跨库身份 |
| src/agents/tools/retrieval_executor.py：map_bounded | 有共享池、准入队列、取消；排队 deadline 不等于任务硬超时 | 扁平调度，单独约束运行时间和迟到结果 |
| src/pipelines/document_rag/ragflow_backend.py：retrieve | 单库权限、台账范围、document_ids、返回结果校验；非 strict 默认不带 local_record_id | 补齐回读元数据；新增同库的结构化范围结果入口；旧接口保持兼容 |
| src/pipelines/document_rag/ragflow_backend.py：retrieve_chunks / _filter_chunks | document_ids 被拒时去掉远端预过滤重试，但仍有本地记录白名单 | 新路径显式选择 chat/frozen 回退策略，禁止以远端请求是否变宽直接判断输出越界 |
| src/agents/tools/document_rag_tool.py：document_search_batch | 原共享开关影响批量工具的本地线程池/共享池分支 | T0 独立验证这一分支与池生命周期；不声称所有单库查询都被该开关切换 |
| src/core/app_pipeline.py：_prepare_document_content_preview | 明确拒绝跨库预览 | 规划预览也要接入新范围，不能只改生成阶段 |
| src/document_authoring/models.py：KnowledgeBaseSourceSnapshot | 以库名和来源文件名构成快照 | 增加真实记录身份、内容哈希和产物定位 |
| src/document_authoring/planning/sources.py：FrozenSourceScope | 已有统一规划来源适配层 | 扩展适配，避免复制另一套规划器 |
| src/core/app_pipeline.py：_project_retriever | 能逐库调用，但部分定位依据仍是文件名 | 复用编排思想，补严格产物绑定 |
| src/document_authoring/pg_support.py、src/storage/bootstrap.py | 文档域支持独立 PG 存储初始化 | 新快照表同时覆盖文档域 PG |
| src/memory/service.py | 项目记忆绑定单库；有个人记忆与摘要 | 多库首版禁止将混合内容自动写入主库记忆 |
| frontend/src/pages/chat/components/MessageBubble.tsx | 引用面板显示文件和定位 | 增加库、角色和真实版本出处 |

本文沿用前一轮分析的针对性验证：范围隔离、RAGFlow 文档范围、项目检索分派、证据组合共 25 项测试通过。它们是旧能力的基线，不证明本方案已实现。

<a id="design-contract"></a>

## 3. 设计约定

### 3.1 用户行为与交付边界

1. 新建知识库会话时，选择一个导航归属库，再添加其他可访问库。默认仍为单库。
2. 单次最多选择 4 个库；这是首版建议配置值，可在验证负载后调整。
3. 提交首轮后，来源集合、角色和用途固定。调整时新建会话；不复制旧 assistant 内容、摘要、checkpoint 或附件绑定。
4. 同一集合在不同会话中可以独立使用；不跨用户或跨会话共享模型历史。
5. 来源选择支持三种用途：
   - answer：综合问答，导航归属库为 primary，其他为 reference 或 standard。
   - compare：项目对比，所有来源为 peer，至少 2 个；导航归属不带来事实优先级。
   - compose：为主库归属的项目编写文档，一个 primary，其余为 reference 或 standard。
6. KB ID 是检索范围身份，不能当作业务 Project ID。未维护 Project 元数据时只展示知识库名，不推断项目名称。
7. 多库读取不授予参考库写权限。生成工单、编辑模板、保存产物按目标库的现有写权限检查。

阶段边界：

| 阶段 | 用户可用能力 | 明确边界 |
| --- | --- | --- |
| P0 | 多库 Word/PDF/TXT/Markdown 文档问答，出处和部分失败展示 | 先完成 M1 内部双库 answer 演示，再完成 M2 正式发布门禁；多库会话暂不暴露结构化检索、Wiki、外部对话及工程文档生成工具；附件保留独立范围机制 |
| P1 | 加入表格、网表、外部对话、可选 Wiki 的多库检索 | 无法实施严格范围校验的适配器不注册，不能退回整个索引 |
| P2 | 跨库规划、预览、生成、恢复和导出；来源角色与版本冲突校验 | 只有 P2 验收完成，才宣称满足“两个项目共同用于模块说明文档”的完整需求 |

### 3.2 API：独立的 knowledge_scope

新增模型放在 src/services/knowledge_scope_models.py，并由 API schema 引用。协议示例：

~~~json
{
  "kb_name": "项目A",
  "title": "A与B的电源模块分析",
  "knowledge_scope": {
    "version": 1,
    "purpose": "answer",
    "sources": [
      {"kb_id": 101, "role": "primary"},
      {"kb_id": 205, "role": "reference"}
    ]
  }
}
~~~

这是拟扩展的 POST /api/v1/conversations 请求。Turn 请求可携带相同的 knowledge_scope 作为一致性断言；省略时沿用会话集合，不能覆盖已固定范围。返回 SessionView/TurnView 时提供服务端解析的名称、角色和 selection_hash，前端不能把这些响应字段当作下一次授权证明。

以下规则必须确定实现：

| 输入/状态 | 处理 |
| --- | --- |
| 旧请求完全不含 knowledge_scope | 保持当前单库行为 |
| 显式 sources 为空、重复 ID、角色不符合用途、数量超限 | 422，拒绝；空数组不表示搜索全部库 |
| 新多库请求包含未知、跨部门或无权限的 ID | 403 knowledge_scope_denied；返回通用说明，不泄露不可见库名称 |
| 导航归属库未包含在集合，或 primary 不匹配归属库 | 422 knowledge_scope_anchor_mismatch |
| 已有首轮后提交不同集合、角色或用途 | 409 knowledge_scope_changed；前端保留未提交文本与新选来源，展示“使用新来源创建对话”，完成新会话创建后由用户发送 |
| general chat 请求多 KB | 422；首版从具体 KB 入口创建多库会话，不改变普通聊天哨兵含义 |
| 多库开关关闭 | 新多库请求返回 409 feature_unavailable，不降级成单库 |
| 同一个新协议 client_request_id 对应不同范围/请求指纹 | 409 idempotency_conflict；同一请求重放返回原轮次 |
| attachment_only | 本轮不调用任何 KB 检索器；不得为了多库覆盖强行搜索 KB |
| 尚处 P0，用户要求多库生成工程文档 | 返回明确的功能阶段说明，不创建单库工单冒充完成 |

来源角色合并到每个 source 对象，避免 source_kb_ids 与 source_roles 两套字段不一致。

### 3.3 模型与持久化

新增的核心接口与职责：

~~~text
KnowledgeSelectionInput
    version: int
    purpose: Literal["answer", "compare", "compose"]
    sources: tuple[KnowledgeSourceInput, ...]

KnowledgeSourceInput
    kb_id: int
    role: Literal["primary", "peer", "reference", "standard"]

AuthorizedKnowledgeSource
    kb_id: int
    kb_name: str
    department_id: str
    role: str

KnowledgeScopeSnapshot
    version: int
    snapshot_id: str
    tenant_key: str
    actor_user_id: int
    anchor_kb_id: int
    selection_hash: str
    purpose: str
    sources: tuple[AuthorizedKnowledgeSource, ...]
    authorized_at: str
~~~

以上是字段契约，不是现有类。模型使用 Pydantic 的冻结配置和禁止额外字段；嵌套集合使用 tuple，序列化时输出 JSON 数组。来源名称、部门和身份仅由服务器填充。

selection_hash 的规范化输入包含协议版本、tenant_key、actor_user_id、anchor_kb_id、purpose、按 kb_id 排序后的 (kb_id, role)。不包含展示名称、授权时间和随机 snapshot_id。权限变更不会因 hash 未变化而跳过重查。

- tenant_key 使用现有认证上下文的 tenant_id；缺省采用现有系统约定的 default，不另建租户系统。
- 新多库请求中的 kb_id 必须为正整数、来自已注册且可见的库；旧单库历史缺少 kb_id 时继续兼容，不以名称伪造一个 ID。
- 普通问答每轮读取当时可用内容，范围快照只冻结选库；该轮实际证据的内容哈希、定位另行保存。
- 文档任务在 P2 冻结文档清单与指纹，不能把对话的选库快照直接当作版本快照。

数据库增量：

| Owner | 表 | 新字段/表 |
| --- | --- | --- |
| 会话控制面 | chat_sessions | knowledge_selection_json：SQLite TEXT / PG JSONB，默认空对象；knowledge_selection_hash：TEXT，默认空串 |
| 会话控制面 | chat_turns | knowledge_scope_json、knowledge_selection_hash、knowledge_request_fingerprint；默认空值兼容旧轮次 |
| 文档台账域 | pipeline_documents | index_revision：非负整数，旧数据默认 0；受管解析/索引更新开始前递增 |
| 文档域 | multi_knowledge_source_snapshots（新增） | snapshot_id、tenant_id、created_by、target_kb_id、selection_hash、content_hash、payload_json |
| 文档工单 payload | document_work_orders.payload_json | source_snapshot_kind、knowledge_selection_hash；source_set_snapshot_id 仍是唯一的来源快照指针 |

新字段必须覆盖写入、读回、列表投影、Worker claim/retry、迁移导入和终态落库。会话范围第一次绑定与首轮插入在同一事务内处理，并沿用现有锁顺序，避免并发首轮绑定不同集合。

旧轮次与工单不重写旧哈希。新可选字段未出现时，原有哈希算法继续排除它们；版本化模型根据 source_snapshot_kind 选择序列化与校验路径。

### 3.4 权限、历史和记忆

KnowledgeScopeResolver 的拟新增公共方法：

~~~text
resolve(
    actor: AuthUser,
    anchor_kb_name: str,
    selection: KnowledgeSelectionInput | None,
) -> KnowledgeScopeSnapshot

revalidate(
    actor_user_id: int,
    snapshot: KnowledgeScopeSnapshot,
) -> tuple[AuthorizedKnowledgeSource, ...]

context_for_source(
    actor_user_id: int,
    source: AuthorizedKnowledgeSource,
) -> RequestContext
~~~

resolver 放在服务层；不要让检索服务反向导入 FastAPI 路由。将按稳定 KB ID 构造上下文的共同逻辑放进 src/services/knowledge_scope.py，API context 只委托它。AuthService 增加公开的按 ID 读取身份方法，不能直接从业务层调用私有 _get_kb_row 绕过范围检查。

执行约束：

- API 接收时、Worker 执行前、每批检索开始、证据合入、模型调用及文档导出前复查权限。
- 不把完整 RequestContext 或权限 map 持久化为长期授权；只保存范围与审计事实。
- 任何库权限撤销使当前多库执行失败并停止后续调用，不能忽略该库继续给出“完整回答”。
- 当前 timed_tool_call 会把一般 Exception 降级为空结果。KnowledgeScopeDenied 必须独立处理，并设置 ToolRuntime.knowledge_scope_invalidated 终止标志；runner、模型调用和 SSE 发送边界检查此标志。不能只抛异常后依赖框架将它原样传出。普通 timeout/retrieval_failed 仍可按部分失败处理。
- SSE 每批发送前检查取消/权限失效标志；撤销在下一受控边界阻断，不能声称撤回已经送达的文本。
- 查看历史、引用、下载和导出时按消息/产物的完整来源依赖检查权限。含已撤销来源的混合回答整条显示“来源权限已变化”，不返回正文、引用预览或缓存摘要；其他能证明不依赖该来源的消息仍可显示，不按会话标题统一屏蔽全部历史。
- 来源依赖包含所有实际送入模型的 KB 证据，以及被使用的历史消息、摘要、缓存和产物所继承的依赖，不能只根据模型最终引用列表判断。依赖不完整的消息按会话选择集合保守检查。
- 在每个 turn 的 summary_json 中保存 source_dependencies：version=1、kb_ids、inherited_turn_ids、complete。多库依赖快照在首次调用模型前持久化，后续工具补证继续合并；终态落库补齐完整闭包。恢复到依赖不完整的中间状态时不得显示未经复查的部分回答。
- 历史展示的逐消息校验与新轮次执行的选库校验分别处理。B 撤权后，旧 A+B 会话不能继续生成；用户可查看被证明只依赖 A 的消息，并新建 A 会话。隐藏正文的提示不能成为把该正文重新送入模型的通道。
- 检查范围包含当前旧 session.kb_name 之外的所有来源；不能只验证导航归属库。

新多库会话的 checkpoint 使用包含会话 ID 与 selection_hash 的独立 thread key；不得接入旧单库 checkpoint。即使选库相同，不同会话也不得共享 thread。

P0–P2 采用保守的记忆策略：多库会话不加载长期个人/项目记忆，不触发长期记忆自动提取；同会话历史和滚动摘要只在范围完整且当前授权通过时使用。这样避免把参考库事实写入主库记忆，或者通过缺少来源标注的旧记忆绕过选库。该限制仅对新多库会话生效，前端给出简短说明。

### 3.4.1 后续工作 F1：多库长期记忆恢复

F1 不属于 P0–P2 的实施或估算范围；当前多库会话保持长期记忆不加载、不自动提取的约定。只有同时完成以下条件，才可另立实施计划恢复：

1. 记忆记录携带直接与继承的来源依赖、内容指纹和有效性版本；个人偏好与项目事实有明确分类。
2. 检索时依照本轮来源集合及当前权限裁剪；无法确认依赖的历史记忆不能进入多库模型上下文。
3. 参考库事实不能自动转成主库的已验证事实，写入或提升来源角色经过现有治理机制。
4. 来源撤权、文件更新和索引代次变化会使关联记忆/摘要/检索缓存失效，并在 Worker 重启后仍生效。
5. 验收覆盖跨库事实误写、撤权、同名文档更新、缓存命中和旧单库记忆回归。

该条目用于维护后续范围和恢复条件，不在本方案中保留未定义的代码接口或实现占位。

### 3.5 检索编排与并发

新增 MultiKnowledgeRetrievalService，主流程：

~~~text
已授权选库快照 + 查询列表 + 检索类型
  → 每库建立独立上下文
  → 展开 query × source × retriever 工作项
  → 一个共享执行器按总预算调度
  → 单库结构化范围结果与真实来源
  → 验证身份、权限、指纹
  → 融合、覆盖检查、证据准入
  → Agent / 文档生成消费证据与缺口
~~~

拟新增接口：

~~~text
search(
    *,
    snapshot: KnowledgeScopeSnapshot,
    queries: Sequence[str],
    retriever_kind: str,
    per_source_top_k: int,
    budget: RetrievalBudget,
    source_ref: str | None = None,
    document_contracts: Mapping[int, DocumentRetrievalContract] | None = None,
) -> MultiKnowledgeRetrievalOutcome
~~~

RetrievalBudget 放在同一新模块，持有 turn 共享的原子调用计数、monotonic 绝对截止时间和取消回调。MultiKnowledgeRetrievalOutcome 放在 knowledge_scope_models.py，包含 evidence、source_outcomes、coverage、selection_hash、partial；source_outcomes 覆盖实际调度的每个来源。

document_contracts 仅由服务端文档生成适配层提供。None 表示普通 chat 检索；非 None 时仅调度该映射列出的已选库，并使用相应 frozen 契约；空映射表示没有适用的冻结文档工作项，不能转换成 chat 全库搜索。映射中的未知 KB ID 拒绝。

调度原则：

1. 保留 document_search 和 document_search_batch 的工具名。单库运行时沿用原开关和旧分支，多库运行时委托新服务。
2. 批量查询只展开一次笛卡尔积，不在每个 query 的线程中阻塞等待另一次 map_bounded。
3. 子任务返回数据，不直接向主 ToolRuntime 添加证据或分配引用编号；主线程统一接收，超时/取消后的结果丢弃。
4. 每个 HTTP 重试、来源分组回退、追加查询都消耗同一轮调用预算，不能只统计顶层工具调用。
5. 多库逐库调用第 3.5.1 节的 retrieve_with_scope，而不是仅使用丢失错误状态的旧 list 接口；不做无条件多 dataset 全局搜索。
6. source_ref 只接受当前 catalog 给出的引用，例如 kb:101；不能执行模型任意给出的 KB 名称。
7. source_scope 允许知识库时才调度；附件沿用自己的 ACL、预算和证据身份。
8. 独立来源暂时失败可形成部分结果；权限失效终止整轮，全部无有效证据时停止事实生成。

#### 3.5.1 文档检索范围契约与回退策略

在 src/pipelines/document_rag/schemas.py 定义 DocumentRetrievalContract 和 ScopedDocumentRetrievalOutcome；按该层现有 dataclass 风格实现，不导入 API 或多库服务层。

~~~text
DocumentRetrievalContract
    mode: Literal["chat", "frozen"]
    allowed_record_ids: tuple[int, ...] | None
    expected_source_stamps: dict[int, DocumentSourceStamp]

DocumentSourceStamp
    kb_id: int
    local_record_id: int
    source_content_hash: str
    source_version_id: str | None
    revision: str | None
    processing_artifact_id: str | None
    processor_version: str | None
    remote_document_id: str
    index_revision: int

ScopedDocumentRetrievalOutcome
    status: Literal[
        "success_with_hits", "success_empty", "access_denied",
        "source_changed", "source_unavailable", "filter_unsupported",
        "retrieval_failed", "timeout", "cancelled", "budget_exhausted"
    ]
    evidence: list[Evidence]
    error_code: str | None
    diagnostics: dict[str, Any]
~~~

RAGFlowBackend 新增同库入口：

~~~text
retrieve_with_scope(
    kb_name: str,
    query: str,
    *,
    contract: DocumentRetrievalContract,
    top_k: int,
    ctx: RequestContext,
    filters: dict | None = None,
    should_cancel: Callable[[], bool] | None = None,
    before_request: Callable[[], None] | None = None,
    remaining_seconds: Callable[[], float] | None = None,
) -> ScopedDocumentRetrievalOutcome
~~~

before_request 和 remaining_seconds 是服务端预算回调：每次实际内容检索 HTTP 请求（包括传输重试、document_ids 回退）发出前执行一次预算/取消检查并取得剩余时间，不能只在顶层工具调用时计数；回调通过当前单库适配器传至 RAGFlowClient。

旧 retrieve(kb_name, query, ...) 继续返回 list[Evidence] 并维持原回退行为。两个入口复用内部台账查询、请求和回读代码；不能复制两套授权逻辑。新入口根据 contract 保留状态和策略，旧入口按原协议映射结果。

**P0 选择 chat 模式。** allowed_record_ids=None 表示在已授权的当前库中查询所有当前可用文档，仍由本地台账与库/部门校验约束。若调用方有显式文件选择，则传入对应记录 ID；允许返回的记录集合仍须精确限制。P0 不要求为了获得 local_record_id 而强制调用冻结模式，字段由回读阶段直接补齐。

**P2 选择 frozen 模式。** allowed_record_ids 必须为明确的非空冻结集合，expected_source_stamps 完整覆盖该集合。远端拒绝冻结 document_ids 请求时，首版结束本次取证，不进行去掉 document_ids 的宽检索。本决定比“宽检索后严格本地验证”更保守，目的是使首版行为和测试可确定；后续只有证明产物级验证等价、评测召回损失并独立修改契约后，才能放开。

| 场景 | chat / P0 | frozen / P2 |
| --- | --- | --- |
| document_ids 被拒 | 在剩余预算内允许重试，保留本地记录白名单和库/部门校验；记录 remote_scope_fallback=true | 不重试宽检索；范围能力不支持返回 filter_unsupported，文档/数据集映射漂移返回 source_unavailable |
| 元数据范围零命中 | 可按已有策略回退；本地库与显式文件集合不扩大 | 不取消冻结记录范围；正常无命中返回 success_empty |
| 远端返回非本地/非允许记录 | 丢弃，不交给模型；记录丢弃数和召回不足 | 丢弃；若响应无法证明按冻结范围执行，返回 filter_unsupported |
| 显式记录列表存在缺失项 | 返回 source_unavailable，不冒充 success_empty | 返回 source_unavailable，不检索剩余项冒充完整集合 |
| 源文件哈希/索引代次与冻结戳不同 | 普通当前内容问答不冻结旧戳；显式带戳时仍不得忽略 | 返回 source_changed，停止相关生成单元 |
| allowed_record_ids 为空 | 明确允许零记录，不变成全库；success_empty | source_unavailable / empty_frozen_source_set；该情况应在规划前被阻止 |
| 用户或来源权限失效 | access_denied，触发整轮失效标志 | 同左 |
| 过滤能力不足 | 不因 P0 远端预过滤失败放弃本地授权检查 | filter_unsupported，不按文件名或当前最新内容替代 |

旧 strict 路径中的“未知记录/版本戳错误 → []”不得原样进入新多库 outcome。新入口在共享验证逻辑内保留失败原因；新增预检与检索后复查也必须明确返回 source_changed/source_unavailable。只有完整范围有效而搜索确实没有匹配时，才使用 success_empty。

#### 3.5.2 共享执行器与建议预算

多库服务在其功能开启时使用现有 get_retrieval_executor()，不以 SHARED_RETRIEVAL_EXECUTOR_ENABLED=true 为条件。该原开关仍决定旧 document_search_batch 的分支。API/Worker 的池初始化、关闭、热配置和 readiness 由 T0 验证；池资源治理不可用时，多库功能不可用。

初始建议设置：

| 设置 | 默认值 | 含义 |
| --- | --- | --- |
| MULTI_KB_CHAT_ENABLED | false | P0 功能开关 |
| MULTI_KB_STRUCTURED_ENABLED | false | P1 开关，依赖 P0 |
| MULTI_KB_AUTHORING_ENABLED | false | P2 开关，依赖 P0、P1 |
| MULTI_KB_MAX_SOURCES | 4 | 正式 P0 来源上限；M1 内部演示限制为 2 |
| MULTI_KB_PER_SOURCE_TOP_K | 8 | 单次每来源候选上限 |
| MULTI_KB_MAX_CALLS_PER_TURN | 24 | 全轮后端调用总量，包含重试 |
| MULTI_KB_RETRIEVAL_DEADLINE_SECONDS | 30 | 全轮多库检索累计墙钟预算 |
| MULTI_KB_EVIDENCE_MAX_ITEMS | 24 | 证据条数上限，仍受更小运行时预算约束 |
| MULTI_KB_CONTEXT_MAX_TOKENS | 8000 | 检索上下文目标上限，不能超过模型/Agent 的更小剩余预算 |

并发量取 RETRIEVAL_PER_REQUEST_FANOUT 与剩余调用预算的较小值。T0 先验证开启多库、原共享开关仍为 false 时，旧单库批量分支继续使用其原逻辑，多库调用受共享池治理。

map_bounded 的 deadline 保留准入语义。新增 execution_deadline_at，在等待循环截止并停止接收结果；HTTP 层使用剩余时间限制请求超时和重试。Python 线程无法强制杀死，迟到任务在真实结束前仍占池容量，不无限创建替代线程。

### 3.6 证据身份、排序与覆盖

新增 src/services/evidence_fusion.py，依次完成真实出处验证、重复命中的排名融合、覆盖与 token 准入。

#### 3.6.1 哈希与定位字段

| 字段 | 计算/赋值位置 | 语义 |
| --- | --- | --- |
| source_content_hash | 通过台账验证的 PipelineDocumentRecord.content_hash | 源文件原始字节的入库哈希，识别源内容版本；不在查询时用 chunk 文本冒充 |
| evidence_text_hash | 原始检索片段 content 字符串编码为 UTF-8 后计算 SHA-256 | 实际证据文本的指纹；在转换为 HTML、格式化或模型摘要之前计算 |
| local_record_id | _filter_chunks 中命中 records_by_remote_id 的 record.id | 本地文档记录身份；正常及 strict 路径都提供 |
| remote_document_id | 远端 chunk.document_id 与台账 record.document_id 验证后的值 | RAGFlow 文档身份 |
| chunk_or_structured_locator | chunk_id，或表格/电路/页面的规范化稳定定位 | 区分同一文件内不同片段/结构化对象 |
| index_revision | 当前台账代次；P0 旧记录尚无新列时以兼容值 0 表示 | P2 接入受管重解析代次；0 不表示已保证历史产物可重现 |

计算示例：

~~~python
import hashlib

def evidence_text_hash(raw_content: str) -> str:
    return hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
~~~

不进行未定义的空白折叠、Unicode 重写或渲染后再哈希。结构化证据先由适配器生成确定性原始内容字符串及稳定定位，存入 evidence.content，再使用相同文本哈希规则。

台账已验证后统一补齐 local_record_id、kb_id、department_id、source_content_hash、evidence_text_hash 与远端定位；这些字段不能采信远端同名 metadata。kb_id 取服务端已验证的来源身份，台账中有正值时须一致；兼容的旧记录 kb_id=0 不覆盖实际已授权 ID。

新多库证据缺少文件哈希、记录身份或可靠定位时，返回 source_unavailable / evidence_identity_incomplete；不猜文件哈希、不把文本哈希当源文件哈希。旧 retrieve 返回协议保持兼容，元数据增加属于可接受的增量扩展。

若源内容有正式 source_version_id，则保留真实版本；没有正式版本时，用 source_content_hash 表达内容身份，不伪造 revision。旧快照、台账与 link_stamps 的 content_hash 字段继续保持文件级含义；新适配层在写入 link_stamps 时将 source_content_hash 映射到其旧键 content_hash。

#### 3.6.2 复合身份与融合准入

服务端身份使用规范化字段序列做稳定摘要，不直接拼接无分隔文本：

~~~text
tenant_key + department_id + kb_id + processor_kind
+ local_record_id + source_content_hash + processing_artifact_id + index_revision
+ remote_document_id + chunk_or_structured_locator + evidence_text_hash
~~~

同一文件不同 chunk 即使 source_content_hash 相同，也因定位不同具有不同身份；同一 chunk 定位的实际文本变化可由 evidence_text_hash 区分。文件级哈希和片段级哈希承担不同职责。

Evidence 保留原始 score；metadata 增加 raw_score、retriever_rank、fusion_score、fusion_rank、knowledge_source、selection_hash，以及上述身份字段。前端使用已授权 knowledge_source，模型不能声明新的来源身份。

融合规则：

- 同一完整身份在多个查询中的重复命中可以合并；不同库/版本不能丢失出处。
- 相同文本可共享展示内容以节省 token，但保留全部 provenance 和引用定位。
- 默认 RRF k=60，是初始排序参数；不同库第一名同分不证明两者语义同样相关。
- answer/compose 优先保证合格主库候选覆盖；参考库只补充。
- compare 对每个有合格结果的 peer 至少保留一条；不得填入低质量片段伪造覆盖。
- 区分无命中、超时、权限拒绝、指纹变化和预算拒绝。
- token 裁剪后重算 coverage；不能容纳比较所需来源时报告 incomplete。
- ToolRuntime.add_evidence 按 fusion_rank/来源预算准入，不能再用异构 raw_score 排序破坏结果。
- 引用编号在准入时分配，交给模型后保持稳定；finalize_evidence 不重新编号。

#### 3.6.3 P0 检索质量门禁

统一 reranker 仍是可选优化，质量验收是必需项。M2 前维护至少 20 条有人工标注的代表性问答，标注相关片段、必要来源、不可回答和冲突场景。R2 建议的初始门槛：

| 指标 | 初始验收门槛 | 口径 |
| --- | --- | --- |
| 来源范围正确率 | 100% | 所有交给模型的证据都在当前授权范围；负向用例零越界 |
| 必要来源覆盖率 | ≥95% | 对已确认各必要来源都有合格资料的可回答案例，统计最终上下文覆盖；不以弱相关填充凑数 |
| 上下文相关片段占比 | ≥75% | 人工标注为相关的准入片段数/全部准入片段数，在固定集上报告总体值与逐题结果 |
| 主库关键事实误用参考来源 | 0 个已知案例 | 检查规格、接口、验收阈值等关键字段；P2 继续复验 |

这些数值是本项目首版建议门槛，不是通用行业标准。M1 后可基于已标注集调整，并把理由写入验收报告；正式 M2 前锁定阈值，禁止只为让失败结果通过而降低标准。

未通过时，调整查询、候选质量过滤和配额，或者接入统一 reranker，再跑同一评测集。不能只把“RRF 相关性不足”写进发布说明后宣称验收完成。

### 3.7 失败语义

| 情形 | 行为 |
| --- | --- |
| 入参含未授权来源 | 拒绝整个请求，无检索调用 |
| 执行时权限撤销/用户停用 | access_denied，终止本轮，不输出尚未发送的内容 |
| 一个来源无命中 | success_empty，保留其他有效证据并明确覆盖缺口 |
| 一个来源超时/服务异常 | timeout/retrieval_failed；普通问答可给有范围声明的部分结果 |
| 全部来源失败或为空 | 返回证据不足，不能生成完整知识库事实回答 |
| 比较任务缺少一侧 | 标记 comparison_incomplete，不生成无依据的差异结论 |
| 主库缺少工程关键参数、只有参考库参数 | 在文档中记录缺口，不把参考参数填成主库事实 |
| 文档冻结来源指纹变化 | source_changed，阻止相关生成单元继续；由用户重新选择并创建新快照 |
| 检索器不支持冻结文档/产物过滤 | filter_unsupported；P2 不退化为宽检索或按同名文件搜索 |
| 冻结记录已删除、远端映射失效 | source_unavailable；不等同正常零命中 |
| 完整且有效的范围中没有搜索命中 | success_empty；不得由范围校验失败伪装而来 |
| 任务取消或执行代次失效 | 后续结果不可合入证据；SSE 与工单保持既有取消语义 |

部分来源失败是检索 outcome，不新增不兼容的 chat_turn.status 值。终态继续使用现有成功/失败/取消等状态，coverage 和错误细节写入 summary/metrics 的有版本字段。

## 4. 文档生成的来源与版本设计

### 4.1 不要求先建设新的项目管理平台

普通工作台继续从目标 KB 创建任务，模板与产物的归属保持现有机制；任务读取来源可以包含多个 KB。Project 路径继续承担项目基线治理，不把一个 SourceSetSnapshot.project_id 改成多个项目 ID。

新增 MultiKnowledgeSourceSnapshot，与现有两类快照并存。现有 PipelineDocumentRecord 未直接提供 processor_version/processing_artifact_id，不能假定这些字段已经存在：可解析的治理产物从 ProjectStore 读取；其余受应用管理的解析结果通过新增 index_revision 标识索引代次。source_snapshot_kind 使用：

- legacy_knowledge_base：原知识库快照。
- legacy_project：原 Project 快照。
- multi_knowledge_v1：新的多库来源快照。

WorkOrder.source_set_snapshot_id 是唯一来源快照引用。不能同时维护一个可变 source_names 列表和一个不同步的多库快照。需要兼容旧消费者时，从新快照生成只读投影。

### 4.2 每个冻结来源记录什么

新增 src/document_authoring/knowledge_sources.py，定义 FrozenKnowledgeDocument 与 MultiKnowledgeSourceSnapshot，至少包含：

| 字段 | 来源 |
| --- | --- |
| tenant_key、department_id、kb_id、kb_name_snapshot | 已验证的库身份 |
| role | 服务端规范化后的使用角色 |
| local_record_id、processor_kind、source_group | 应用文档台账 |
| original_file_name | 展示用途，不作为唯一身份 |
| source_content_hash | 台账 content_hash 的真实源文件哈希；旧快照和 link_stamps 的旧键含义保持不变 |
| source_version_id、revision、approval_status | 存在正式版本信息时复制真实值，否则为空 |
| processing_artifact_id、processor_version | 从真实治理产物或索引清单解析；不存在则为空，不伪造值 |
| index_revision | 本地解析台账的受管索引代次，至少能够检测同一文件内容未变但索引被重建 |
| dataset_id、remote_document_id | RAGFlow 文档定位 |
| structured_locator | 表格/电路/对话/Wiki 对应的稳定索引身份 |
| selected_at、snapshot_hash | 审计与快照完整性 |

MultiKnowledgeSourceSnapshot.documents 保存 tuple[FrozenKnowledgeDocument, ...]。knowledge_sources.py 提供 build_document_contracts(snapshot, *, record_ids=None) -> dict[int, DocumentRetrievalContract]，按已冻结库和记录构造 frozen 契约；record_ids 只能收窄快照，不能加入新记录，非 RAGFlow 来源由对应严格结构化适配器处理。

规划层 FrozenSourceRef.source_id 使用包含库和记录身份的稳定 ID；其现有 content_hash 字段保存文档/产物实际指纹，由 source_content_hash/真实产物指纹适配，不使用来源名单哈希或片段文本哈希替代。

### 4.3 如何检索冻结来源

- RAGFlow 使用 retrieve_with_scope(mode="frozen")；每库传明确 allowed_record_ids 和完整 DocumentSourceStamp，旧 link_stamps 所需字段由适配层映射。
- 正常与 strict 回读都具有真实记录 ID；P2 要求 strict 是为了冻结来源范围和指纹校验，不是为了获得 local_record_id。
- 远端拒绝冻结 document_ids 请求时按 §3.5.1 停止，不执行删除参数后的宽检索。能力不足用 filter_unsupported，映射/文档缺失用 source_unavailable；诊断保留原因类别但不泄露凭据或原文。
- 请求前、回读后、任务恢复和生成单元补证前验证源文件哈希、index_revision、真实版本及产物定位。任何已知漂移为 source_changed；不能回填旧版本标签到新文本。
- 记录列表缺失与指纹不符必须保持结构化失败状态；只有有效完整范围零命中才返回 success_empty。
- Excel 绑定本库记录 ID、表/工作表定位和指纹；电路绑定 design/asset 与稳定结构化定位。
- 外部对话/Wiki 同时满足 KB、记录/页面、版本约束；不支持该能力的来源不作为 P2 冻结证据。

index_revision 在受管重新解析、重新索引或原位替换远端文档开始前以台账事务递增；失败状态也使旧快照不可继续使用。历史记录以 0 为兼容起点，不触发全量重解析。

首版一致性保证覆盖本应用管理的写入。直接修改 RAGFlow 或底层索引不在这一保证内；发现远端修订/数量/片段异常时报告 source_changed/source_unavailable，同步台账后新建快照。严格检测所有带外修改需要远端不可变产物或内容清单校验，不以本地代次字段冒充。

**版本策略：** 固定身份与变化检测。来源更新或索引替换时阻止旧快照继续生成；实际保留且可定位旧产物后，才能另行支持历史版本重新检索。已保存的证据文本复用仍须验证授权及来源依赖，不意味着整个旧文档可重现。

### 4.4 规划、生成和输出必须接通同一个范围

改造范围包括：

- 资料选择与生成会话。
- _planning_source_snapshot 和资料数量/类型统计。
- _prepare_document_content_preview。
- OutputSpec / DocumentPlan 的来源哈希与确认。
- 工单创建、来源快照保存和幂等键。
- 知识检索 provider、能力分派、证据绑定与跨单元缓存。
- Worker 恢复、取消、重新规划与反馈修订。
- 来源引用、下载、导出与审核状态。

当前 _prepare_document_content_preview 的跨库拒绝只能在新范围校验完成后替换；不能直接删掉校验。旧单库路径继续使用原适配器。

文档按章节或信息需求取证。例如：

| 章节 | 主库依据 | 其他来源的用途 | 校验 |
| --- | --- | --- | --- |
| 功能概述 | A 需求说明 | B 架构经验 | 不用 B 的产品目标替换 A |
| 电气参数 | A 正式规格/BOM | B 器件经验 | 主库缺证据则标缺口 |
| 接口定义 | A 接口表、网表 | B 相似连接方案 | 绑定 A 的版本和连接器身份 |
| 设计注意事项 | A 约束 | B 历史问题、公共规范 | 区分约束与建议 |
| 测试验证 | A 验收条件 | B 测试方法 | 阈值和实测结果分别注明来源 |

角色约束要进入既有 claim-evidence / requirement-fit 校验及导出门禁，不能只写在系统提示词里。模型抽取的事实也必须保留证据 ID 和来源角色；角色不匹配或冲突未解决的关键字段不能作为已验证事实发布。

## 5. 文件结构与职责

### 5.1 新增模块

| 拟新增文件 | 单一职责 |
| --- | --- |
| src/services/knowledge_scope_models.py | 选择、已授权来源、范围快照、来源 outcome 的稳定数据契约 |
| src/services/knowledge_scope.py | 按当前用户与 KB ID 解析、复查范围、建立单库上下文 |
| src/services/multi_kb_retrieval.py | 查询工作项展开、共享预算、受控调度、结果状态汇总 |
| src/services/evidence_fusion.py | 证据稳定身份、排名融合、覆盖和 token 准入 |
| src/document_authoring/knowledge_sources.py | 多库文档快照和真实来源指纹 |
| frontend/src/pages/chat/components/KnowledgeSourcePicker.tsx | 选库、角色、用途和范围变更交互 |
| scripts/production_hardening/migrate_multi_kb_scope.py | 会话控制面、文档快照及解析台账版本字段增量迁移的检查/执行/验证入口 |

### 5.2 需要修改的现有模块

| 范围 | 文件 |
| --- | --- |
| 身份、协议 | src/core/auth.py、src/api/context.py、src/api/schemas.py |
| 会话/API/Worker | src/core/conversation.py、src/api/routes/conversations.py、src/api/routes/query.py、src/core/app_pipeline.py |
| PG 控制面 | src/storage/control_plane/base.py、sqlite.py、postgres.py、conversation_service.py、schema.py、schema_version.py、loader.py |
| Agent 与工具 | src/agents/runner.py、src/agents/tools/runtime.py、document_rag_tool.py、pipeline_catalog.py、spreadsheet_tools.py、circuit_tools.py、external_conversation_tools.py、wiki_tools.py、retrieval_executor.py |
| 能力与动作 | src/agents/orchestration/tool_assembly.py、src/agents/orchestration/capabilities.py、adapters.py、executor.py |
| 底层严格范围 | src/pipelines/document_rag/ragflow_backend.py、同文件中的 RAGFlowClient、表格/电路/外部对话/Wiki 适配器 |
| 记忆和历史 | src/memory/service.py、src/agents/tools/memory_tools.py、终态记忆入队路径 |
| 文档 | src/document_authoring/models.py、service.py、evidence.py、work_order_store.py、worker.py、pg_support.py、planning/sources.py、planning/models.py、planning/store.py |
| 文档 API 与导出 | src/api/routes/document_generation.py、src/api/routes/exports.py、src/result_exports/worker.py |
| 前端 | frontend/src/api/types.ts、frontend/src/pages/chat/useKbChat.ts、ChatPage.tsx、chatHelpers.tsx、components/Composer.tsx、components/MessageBubble.tsx、components/DocumentStatusCard.tsx、components/OutputSpecConfirmationCard.tsx、frontend/src/api/documentAuthoring.ts |
| 设置/运维/文档 | src/settings.py、.env.example、src/api/app.py、src/workers/main.py、src/observability/metrics.py、src/storage/bootstrap.py、deploy/postgres/README.md、wiki 对应章节 |

实施时只在现有入口增加委托与范围参数，不借机全面重写大型 app_pipeline.py 或 runner.py。RAGFlowClient 当前定义在 ragflow_backend.py 内；文档生成前端已收敛到 ChatPage、useKbChat 和文档卡片，不新增已不存在的独立 DocumentGenerationPage。

## 6. 实施任务

每个任务的独立可验证交付物单独提交；新增行为按“先写失败测试 → 最小实现 → 相关回归通过”执行。M1 先串联 T1–T6 的最小子集，后续完成各任务剩余契约，不把演示部分提交当作整项任务完成。下列代码块是接口/关键逻辑和测试契约，尚未进入业务代码；任务中的新类型均在前文或该任务定义。

### T0：共享检索池生命周期与旧单库兼容（独立前置任务）

**Files**

- Modify：src/api/app.py、src/workers/main.py、src/agents/tools/retrieval_executor.py、src/settings.py、.env.example。
- Test：tests/test_multi_kb_executor_lifecycle.py（新增）、tests/test_shared_retrieval_executor.py，以及 API/Worker 生命周期相关测试。

**Interfaces**

- Consumes：现有 get_retrieval_executor/reset_retrieval_executor、旧 SHARED_RETRIEVAL_EXECUTOR_ENABLED 分支。
- Produces：API/Worker 对多库检索池的初始化、可用性检查和关闭所有权；MULTI_KB_CHAT_ENABLED 默认 false。
- 不新增第二个线程池或翻转旧全局开关；同一进程的多库工作共用已有池。

- [ ] 写四组合行为测试，并记录真实使用的池/调用路径：

~~~text
旧共享开关 false + 多库关闭：旧单库批量逻辑不变，多库不可用
旧共享开关 false + 多库开启：旧单库批量仍为原分支，多库使用共享池
旧共享开关 true  + 多库关闭：旧批量使用共享池，多库不可用
旧共享开关 true  + 多库开启：两者使用同一共享池
~~~

- [ ] 在 API/Worker 生命周期中纳入多库功能对池的使用；验证初始化失败导致多库不可用，重启后恢复可用，关闭按真实任务结束/既有截止时间回收。
- [ ] 验证关闭多库开关阻止新多库任务；已运行任务按现有取消/预算边界结束，不能声称强制杀死 Python 线程。
- [ ] 单独运行共享执行器、批量文档检索、队列饱和、取消和 API/Worker 生命周期回归。若另行决定翻转旧共享开关，作为独立配置发布记录受影响单库分支与回归结果。
- [ ] 提交：fix: own multi-kb retrieval executor lifecycle。
- [ ] 该前置任务不等同 P0 可用；多库入口仍受后续范围与验收门禁控制。

### T1：范围契约与按 ID 授权（P0）

**Files**

- Create：src/services/knowledge_scope_models.py、src/services/knowledge_scope.py。
- Modify：src/core/auth.py、src/api/context.py。
- Test：tests/test_knowledge_scope.py（新增），tests/test_auth_kb_scope.py、tests/test_kb_scope.py。

**Interfaces**

- Consumes：AuthUser、AuthService、RequestContext、现有部门/KB 权限。
- Produces：第 3.3 节模型；selection_hash(selection, *, tenant_key, actor_user_id, anchor_kb_id)；第 3.4 节 resolver 方法；AuthService.get_knowledge_base_identity(kb_id)，返回 kb_id/name/department_id 或 None。
- 新异常 KnowledgeScopeDenied、KnowledgeScopeInvalid、KnowledgeScopeChanged 定义在 knowledge_scope.py；API 层映射状态码。

- [ ] 增加模型行为测试：空数组、重复 ID、非法角色、比较模式不足两库、排序不改变 hash、角色变化改变 hash。

~~~python
def test_selection_hash_ignores_input_order():
    from src.services.knowledge_scope_models import (
        KnowledgeSelectionInput,
        selection_hash,
    )
    left = KnowledgeSelectionInput.model_validate({
        "version": 1, "purpose": "compare",
        "sources": [
            {"kb_id": 101, "role": "peer"},
            {"kb_id": 205, "role": "peer"},
        ],
    })
    right = left.model_copy(update={"sources": tuple(reversed(left.sources))})
    assert selection_hash(
        left, tenant_key="default", actor_user_id=7, anchor_kb_id=101,
    ) == selection_hash(
        right, tenant_key="default", actor_user_id=7, anchor_kb_id=101,
    )
~~~

- [ ] 增加真实临时 AuthService 场景：A/B 属于用户部门，C 属于另一部门且与 A 同名；验证 C、私有他人库、停用用户、system_admin 全部拒绝。每库上下文的 metadata 和权限 map 必须独立。
- [ ] 实现按 ID 查询后验证部门与权限；先验证完整集合，再构造快照。不得让“部分授权成功”触发后端查询。
- [ ] 运行：uv run python -m pytest -q tests/test_knowledge_scope.py tests/test_auth_kb_scope.py tests/test_kb_scope.py。
- [ ] 提交：feat: add authorized knowledge scope contracts。

### T2：会话范围、轮次快照与双后端持久化（P0）

**Files**

- Modify：src/api/schemas.py、src/core/conversation.py、src/storage/control_plane/base.py、sqlite.py、postgres.py、conversation_service.py、schema.py、schema_version.py、loader.py。
- Create：scripts/production_hardening/migrate_multi_kb_scope.py。
- Test：tests/test_multi_kb_store_contract.py（新增）、tests/test_control_plane_store_contract.py、tests/test_control_plane_schema.py。

**Interfaces**

- Consumes：KnowledgeSelectionInput、KnowledgeScopeSnapshot。
- Produces：create_session(..., knowledge_selection=None)、create_turn(..., knowledge_scope=None) 的向后兼容关键字参数；ChatSession/ChatTurn 对应可选字段。
- 新协议 request_fingerprint 包含 query、knowledge selection hash、source_scope、attachment_ids 和规范化 document_context，不含授权时间/随机快照 ID。

- [ ] 为 SQLite/PG 共用契约增加 round-trip、旧行默认值、相同请求重放、不同范围重放冲突、并发首轮绑定、Worker claim 后字段保留测试。
- [ ] 实现会话与首轮范围绑定事务；历史范围已固定时采用以下逻辑：

~~~python
from src.services.knowledge_scope import KnowledgeScopeChanged

def require_same_selection(stored_hash: str, requested_hash: str) -> None:
    if stored_hash and stored_hash != requested_hash:
        raise KnowledgeScopeChanged("knowledge_scope_changed")
~~~

- [ ] 增量 DDL 按第 3.3 节添加字段。PostgreSQL 使用 JSONB，SQLite 使用 TEXT；loader 的 JSON 列清单、schema version 和现有记录投影一起更新。
- [ ] 迁移脚本支持 --check、--apply、--verify 三种互斥模式；未提供模式不写入。复用既有 DSN 获取和日志脱敏方式，不打印连接凭据。重复 --apply 不改数据。
- [ ] M1 可先在隔离 SQLite 打通首个纵向演示，但仅标为“SQLite 内部演示”；PG 持久化、迁移和恢复工作必须在 M2 正式 P0 前完成。
- [ ] 在配置 HDB_TEST_POSTGRES_DSN 的一次性数据库运行同一组 PG 契约和迁移前后回读。PG 未配置产生 skip 时不能宣称双后端验收完成。
- [ ] 提交：feat: persist knowledge scopes across conversation backends。

### T3：API、Worker、历史与记忆隔离（P0）

**Files**

- Modify：src/api/routes/conversations.py、src/api/routes/query.py、src/core/app_pipeline.py、src/agents/runner.py、src/agents/tools/runtime.py、src/memory/service.py、src/agents/tools/memory_tools.py。
- Modify：SQLite/PG 终态记忆入队与历史投影路径。
- Test：tests/test_multi_kb_turn_flow.py、tests/test_multi_kb_history_scope.py（新增），tests/test_conversation_wiring.py、tests/test_memory_security_contract.py。

**Interfaces**

- Consumes：T1 resolver、T2 持久化范围。
- Produces：AppPipeline.query / runner.stream 的可选 knowledge_scope 参数；ToolRuntime.knowledge_scope；新多库 thread key。
- 增加 multi_kb_thread_key(session_id, selection_hash) 纯函数；旧单库 thread_id 保持原逻辑。

- [ ] 写 API 测试验证第 3.2 节状态码，使用 fake retriever 计数确认非法范围产生零后端调用。
- [ ] 为 API 持久化 → 独立 Worker 读回 → runner 装配增加集成测试，防止只在 API 内存中携带来源。
- [ ] 实现新多库 thread key，并在任何历史/摘要恢复前复查所有来源：

~~~python
def multi_kb_thread_key(session_id: int, selection_hash: str) -> str:
    return f"session:{session_id}:knowledge:{selection_hash}"

def test_different_sessions_never_share_thread():
    assert multi_kb_thread_key(1, "same") != multi_kb_thread_key(2, "same")
~~~

- [ ] 增加“A+B 的旧回答后改成 A”“B 权限撤销后读取历史/恢复 Worker”“多库终态不产生长期记忆任务”测试。验证旧单库记忆仍正常工作。
- [ ] 在模型调用前保存 source_dependencies；验证 B 证据被送入模型但未被最终引用时仍算 B 依赖，历史/摘要继承 B 时依赖继续传播。
- [ ] 权限变化后逐消息显示：能证明仅依赖 A 的消息可读，混合/依赖未知消息隐藏正文；当前固定 A+B 会话的新执行仍拒绝，不从隐藏消息重建模型上下文。
- [ ] 保持附件原语义；新会话不能继承旧会话 attachment_ids，前端也不自动重放旧附件。
- [ ] 运行新增测试与 conversation wiring、memory security 回归。
- [ ] 提交：feat: enforce multi-kb scope across turn execution。

### T4：受控文档检索调度与真实截止时间（P0）

**Files**

- Create：src/services/multi_kb_retrieval.py。
- Modify：src/agents/tools/retrieval_executor.py、document_rag_tool.py、src/settings.py、.env.example。
- Modify：src/pipelines/document_rag/schemas.py、ragflow_backend.py 中回读元数据、结构化范围结果入口与 RAGFlowClient 超时适配。
- Test：tests/test_multi_kb_retrieval.py、tests/test_ragflow_scoped_outcome.py（新增）、tests/test_shared_retrieval_executor.py、tests/test_ragflow_document_scope.py。

**Interfaces**

- Consumes：KnowledgeScopeSnapshot、单库 retrieve_with_scope（内部复用现有单库检索逻辑）、共享 RetrievalExecutor。
- Produces：RetrievalBudget、MultiKnowledgeRetrievalService.search、MultiKnowledgeRetrievalOutcome，以及第 3.5.1 节的 DocumentRetrievalContract/DocumentSourceStamp/ScopedDocumentRetrievalOutcome 和 RAGFlowBackend.retrieve_with_scope。
- retrieve_with_scope 仍只接收一个库；chat/frozen 政策由服务端契约指定，客户端不能控制回退参数。
- budget.before_attempt() 检查取消/截止时间并原子预留一次内容检索调用；budget.remaining_seconds() 返回剩余时间。回调拒绝时映射为 cancelled/timeout/budget_exhausted；同一次请求不得在任务入队和 HTTP 发出时重复扣款。

- [ ] 写两个已授权库分别调用的测试：后端记录每次 kb_name、ctx.metadata.kb_id、department；返回同名文件以检验身份不混淆。
- [ ] 为普通和 strict 回读补 metadata：local_record_id=record.id、source_content_hash=record.content_hash、evidence_text_hash=SHA256(raw_content UTF-8)，并写入验证后的 kb_id、部门和远端定位。测试两条路径均有完整且可信的来源字段。
- [ ] 写 scoped outcome 对照测试：有效范围零命中为 success_empty；显式记录缺失为 source_unavailable；指纹不同为 source_changed；权限失效为 access_denied，禁止统一变成 []。
- [ ] 写 1 工作线程 + 多个 query × KB 的测试，证明没有嵌套等待死锁；记录实际请求数，包含回退，验证不超过总预算。
- [ ] 展开一次工作项后调用共享执行器。核心结构为：

~~~python
work_items = [
    (query, source)
    for query in normalized_queries
    for source in snapshot.sources
]
results = executor.map_bounded(
    retrieve_one,
    work_items,
    fanout=effective_fanout,
    deadline=queue_budget_seconds,
    execution_deadline_at=budget.deadline_at,
    cancel_check=budget.cancelled,
)
~~~

上述 retrieve_one 是 search 内部闭包：接收 (query, source)，复查权限并构建独立 ctx，再调用 retrieve_with_scope，传 before_request=budget.before_attempt、remaining_seconds=budget.remaining_seconds；结构化适配器在实际查询前做同样预算检查。保留结构化状态，只返回数据，不在子线程分配引用。

- [ ] P0 使用 chat 契约：document_ids 被拒后的重试仍以本地记录白名单筛选。桩返回允许记录和其他库/未选文件，验证只留下合法证据。
- [ ] P2 的 frozen 契约骨架在该任务定义，产物代次验证由 T8 补齐；先验证 frozen 远端范围拒绝时只发一次请求，不去掉参数重试。
- [ ] 旧 retrieve() 的返回类型和已存在回退测试保持兼容。新 scoped 入口保留原因，权限失败在根运行时设置失效标志。

- [ ] 扩展 map_bounded 的 execution_deadline_at，默认 None 保持老调用行为；验证队列截止、运行截止、取消、迟到结果分别处理，线程池槽位按真实任务结束释放。
- [ ] HTTP 超时取既有上限与剩余预算的较小值；若 deadline 已到不发请求。网络重试与文档路由重试都经过预算计数。
- [ ] 运行新增调度/scoped outcome 测试、共享执行器与文档范围回归；记录 P0 有限回退的召回/丢弃信息。
- [ ] 提交：feat: coordinate bounded multi-kb document retrieval。

### T5：证据融合、引用身份与准入预算（P0）

**Files**

- Create：src/services/evidence_fusion.py。
- Modify：src/agents/tools/runtime.py、src/agents/schemas.py、src/pipelines/document_rag/schemas.py、结果/事件序列化。
- Test：tests/test_multi_kb_evidence_fusion.py、tests/test_multi_kb_citations.py（新增）。

**Interfaces**

- Consumes：每源检索结果、用途、条数与 token 预算。
- Produces：evidence_text_hash(raw_content: str) -> str、evidence_identity(fields)、fuse_evidence(outcomes, purpose, max_items, token_budget)；返回稳定 ID、融合顺序和 coverage。
- fields 是第 3.6 节身份字段的字典；source_content_hash 来自台账，evidence_text_hash 来自原始证据文本。缺少完整身份返回 evidence_identity_incomplete，不在合并时猜测。
- 证据 hash 命名与旧文件级 content_hash 保持显式适配；不能把两种指纹混写一个字段。

- [ ] 增加同名文件、相同远端 chunk_id、跨库相同内容、同文件不同版本的用例。最小身份测试：

~~~python
def test_same_chunk_in_two_kbs_has_different_identity():
    import hashlib
    from src.services.evidence_fusion import evidence_identity, evidence_text_hash
    common = {
        "tenant_key": "default", "department_id": "3",
        "processor_kind": "ragflow", "local_record_id": 9,
        "source_content_hash": hashlib.sha256(b"file-bytes").hexdigest(),
        "evidence_text_hash": evidence_text_hash("retrieved evidence"),
        "processing_artifact_id": "", "index_revision": 0,
        "remote_document_id": "remote-1", "chunk_or_structured_locator": "chunk-1",
    }
    assert evidence_identity({**common, "kb_id": 101}) != evidence_identity(
        {**common, "kb_id": 205}
    )
~~~

- [ ] 增加同文件两 chunk 使用相同 source_content_hash 但不同定位的身份测试；同定位文本变化时 evidence_text_hash 不同，身份也不同。哈希测试使用真实 SHA-256，不能只用示例字符串证明计算方式。
- [ ] 实现完整身份去重、多查询排名融合和第 3.6 节覆盖约束；RRF 使用 sum(1/(60+rank))，保留 raw_score，不宣称分数跨库可比。
- [ ] 建立 §3.6.3 的人工标注样本和来源覆盖测试；质量不达门槛时修正排序/候选策略或引入统一 reranker，不能把“可选”理解为免于质量验收。
- [ ] 为 ToolRuntime 写预算耗尽用例：模拟一个库先返回大量证据，确认主线程融合后仍保留另一必要来源。工具调用完成后不接收迟到证据。
- [ ] 测试截断前后的 coverage、稳定引用编号、重新加载历史后的相同来源身份。
- [ ] 运行新增测试及已有 document evidence/runtime 相关测试。
- [ ] 提交：feat: preserve multi-kb evidence provenance and coverage。

### T6：前端来源选择与 P0 端到端（P0）

**Files**

- Create：frontend/src/pages/chat/components/KnowledgeSourcePicker.tsx。
- Modify：frontend/src/api/types.ts、frontend/src/pages/chat/useKbChat.ts、ChatPage.tsx、components/Composer.tsx、components/MessageBubble.tsx、chatHelpers.tsx。
- Test：KnowledgeSourcePicker.test.tsx、现有 MessageBubble.test.tsx、frontend/e2e/multi-kb-chat.spec.ts（新增；当前 Playwright testDir 为 ./e2e）。

**Interfaces**

- Consumes：可访问 KB 列表中的 kb_id、服务端解析后的 knowledge_scope、coverage/source_outcomes。
- Produces：新建会话的 knowledge_scope；每条消息展示真实来源和缺口。

- [ ] 组件测试覆盖选择两库、角色冲突、超上限、刷新恢复、切换用户清空状态、来源变更新建会话。
- [ ] 把来源调整识别为新会话操作：保留未提交文本和待用的新来源，展示“使用新来源创建对话”，新建成功后由用户发送；不自动丢弃输入，也不自动发送未经重新验证的附件。
- [ ] 对 409 knowledge_scope_changed 做相同恢复流程；其他 409 分别展示幂等冲突/功能不可用，不能统一重建会话。
- [ ] 附件和模板上下文需重新解析为新会话有效引用；失败时保留草稿并显示具体受限项，不复制旧 assistant、滚动摘要或 checkpoint。
- [ ] 增加 409 响应后的草稿保留、创建失败、创建成功、重复点击幂等和有效附件重新绑定组件/E2E 测试。
- [ ] 单库默认展示保持现有操作；多库显示“项目A + 1 个来源”，可展开查看。P0 明示“当前多库检索支持文档资料”。
- [ ] 引用标题格式采用“项目B · spec.pdf · 版本/哈希摘要 · 第4页”；点击按证据真实 KB/记录 ID 定位，不能使用当前导航库拼下载 URL。
- [ ] 对话范围设置不受纯展示 tab 缓存覆盖；已创建会话以服务端返回值为准。不同 selection_hash 的新会话不预填旧回答。
- [ ] 添加真实 API、stub 模型、stub 检索的端到端用例：A 中写唯一标记 A_ONLY，B 中 B_ONLY，未选择 C 中 C_FORBIDDEN；校验实际工具调用、证据和回答引用，不只断言模型文本出现两个库名。
- [ ] 运行 frontend 目录下 npm test 与 npm run build；运行对应 Playwright 场景。记录 P0 功能门禁结果。
- [ ] 提交：feat: expose authorized multi-kb chat sources。

### T7：表格、电路、外部对话与 Wiki 适配（P1）

**Files**

- Modify：src/services/multi_kb_retrieval.py、src/agents/tools/pipeline_catalog.py、spreadsheet_tools.py、circuit_tools.py、external_conversation_tools.py、wiki_tools.py、src/agents/runner.py、src/agents/orchestration/tool_assembly.py。
- Modify：相应索引服务的记录/设计/页面范围参数。
- Test：tests/test_multi_kb_structured_retrieval.py（新增）、已有 spreadsheet/circuit/conversation/wiki 工具测试。

**Interfaces**

- Consumes：同一 KnowledgeScopeSnapshot 与 RetrievalBudget。
- Produces：注册到新服务的单库适配器，统一返回 MultiKnowledgeRetrievalOutcome；catalog 返回 KB 身份和稳定 source_ref。
- 适配器能力明确声明 supports_record_filter、supports_version_check，不支持时在文档冻结场景返回 filter_unsupported。

- [ ] 写 A/B 同名表、相同 refdes/net_name、同名 Wiki 页的分离测试；SQL 查询必须绑定一个来源 DB，模型不能自行创建跨库 JOIN。
- [ ] 为每类工具复用单库查询方法，并在结果进入主运行时前验证真实来源。定向 lookup 使用已授权 source_ref：

~~~python
selected = {
    f"kb:{source.kb_id}": source
    for source in snapshot.sources
}
if source_ref is not None and source_ref not in selected:
    raise KnowledgeScopeDenied("knowledge_scope_denied")
~~~

- [ ] 验证文档与结构化查询共享同一轮调用预算；Wiki 仍受 wiki_assist 控制。
- [ ] 展开 catalog 时展示每源可用/不可用资料类型；不能把某库无结构化结果解释成该库无资料。
- [ ] 用跨库表格参数比较和 A 网表+B 说明书场景完成 P1 E2E。
- [ ] 提交：feat: extend multi-kb retrieval to structured sources。

### T8：多库文档快照与版本冲突检测（P2 基础）

**Files**

- Create：src/document_authoring/knowledge_sources.py。
- Modify：src/document_authoring/models.py、work_order_store.py、pg_support.py、planning/sources.py、src/storage/bootstrap.py、迁移脚本。
- Modify：src/pipelines/document_store_sqlite.py、src/pipelines/ingestion.py、src/pipelines/runtime.py 与 RAGFlow 重新解析入口的 index_revision 更新。
- Modify：RAGFlow 严格过滤与结构化适配器的真实产物定位。
- Test：tests/test_multi_kb_authoring_sources.py（新增）、tests/test_document_work_order_source_snapshots.py、tests/test_document_planning_sources.py。

**Interfaces**

- Consumes：已授权来源范围、各库文档台账、原有 SourceVersion/ProcessingArtifact（若存在）。
- Produces：FrozenKnowledgeDocument、MultiKnowledgeSourceSnapshot、freeze_sources、validate_source_stamp、build_document_contracts。
- build_document_contracts 的签名和范围收窄规则见 §4.2；新快照使用 source_content_hash，旧台账/快照/link_stamps 中 content_hash 保持文件级语义。
- validate_source_stamp(frozen, current) 接收规范化 DocumentSourceStamp，比较库/记录身份、source_content_hash、真实版本和产物代次；缺失为 SourceUnavailableError，不一致为 SourceChangedError，两类异常在该模块定义并适配为 scoped outcome。

- [ ] 写“同名文件替换”“同 record 内容更新”“产物重建”“任务启动后新增未选文档”“同名不同库”测试。
- [ ] 定义独立快照表并接入文档域 SQLite/PG；创建快照与工单引用在文档域现有事务边界完成。文档台账新增 index_revision，同步台账域 PG schema 与重解析/重索引路径；用相同 content_hash 的索引重建测试证明代次变化可检测。
- [ ] 快照包含实际文档指纹。关键校验如下，current 为当前已授权台账/产物视图：

~~~python
def validate_source_stamp(frozen, current) -> None:
    if current is None:
        raise SourceUnavailableError("source_unavailable")
    keys = (
        "kb_id", "local_record_id", "source_content_hash",
        "source_version_id", "processing_artifact_id", "processor_version",
        "remote_document_id", "index_revision",
    )
    if any(getattr(frozen, key) != getattr(current, key) for key in keys):
        raise SourceChangedError("source_changed")
~~~

- [ ] FrozenSourceScope 新增 from_multi_knowledge_snapshot 适配，source_id 由库+记录+产物构成。旧两类快照 round-trip 和旧哈希保持不变。
- [ ] 严格检索测试确认 allowed_record_ids 与版本戳生效。frozen 请求 document_ids 被拒时断言 HTTP 请求数为 1，返回具体 source_unavailable/filter_unsupported，不能触发旧宽检索回退。
- [ ] 对记录删除、哈希变化、相同文件内容的索引重建与真正零命中分别断言 source_unavailable、source_changed、source_changed、success_empty；对旧 retrieve() 验证返回协议仍兼容。
- [ ] 新快照里的 source_content_hash 映射到旧 link_stamps.content_hash；evidence_text_hash 不能传入文件版本比较。
- [ ] 运行快照/规划适配测试与文档域 PG round-trip。
- [ ] 提交：feat: freeze multi-kb authoring source identities。

### T9：打通规划、预览、工单与 Worker（P2）

**Files**

- Modify：src/core/app_pipeline.py 中 _planning_source_snapshot、_prepare_document_content_preview、create_knowledge_base_document_work_order、_knowledge_base_retriever 及恢复/修订入口。
- Modify：src/document_authoring/evidence.py、service.py、worker.py、planning/models.py、planning/store.py、src/api/routes/document_generation.py。
- Modify：frontend/src/api/documentAuthoring.ts、frontend/src/pages/chat/useKbChat.ts、ChatPage.tsx、components/DocumentStatusCard.tsx、components/OutputSpecConfirmationCard.tsx 的来源选择与摘要。
- Test：tests/test_multi_kb_authoring_flow.py（新增）、tests/test_document_generation_prepare.py、tests/test_document_plan_confirmation.py、tests/test_document_plan_submission_worker.py。

**Interfaces**

- Consumes：T8 不可变文档快照、T7 多类型适配器、现有文档任务/模板/输出规格。
- Produces：多库来源贯穿的同一工单与计划；预览/生成/恢复使用相同 snapshot_id 和 snapshot_hash。

- [ ] 写完整顺序用例：选择 A/B → 预览命中两库 → 确认计划 → 创建工单 → Worker 重启恢复 → 生成章节 → 导出。用 stub 模型验证工具接收到相同快照。
- [ ] 更新计划哈希、幂等键及预览来源统计，确保增加 B 会形成新范围，不能复用 A 的缓存或已确认计划。
- [ ] 通过 source_snapshot_kind 分派解析来源；原单库跨库拒绝仅被“新范围已授权且类型正确”的分支替代。未知 kind 拒绝。
- [ ] 新文档 provider 从 build_document_contracts 获取按库冻结契约，作为服务端 document_contracts 传入 search；空映射表示无该类来源，不能降级为 chat 检索。
- [ ] 缓存键至少包含 tenant/actor、selection_hash、source snapshot hash、query、retriever kind；缓存命中仍复查来源访问与指纹。
- [ ] 多库生成的目标库写权限与所有来源读权限分别检查；参考库无需写权限。旧单库工单不受新功能开关影响。
- [ ] 验证附件只按现有 attachment_refs 参与；P2 不自动支持原预览路径尚不支持的其他输入类型。
- [ ] 提交：feat: integrate multi-kb sources into document authoring。

### T10：章节证据规则、来源撤销与导出（P2）

**Files**

- Modify：src/agents/claim_evidence.py、src/document_authoring/writers/requirement_fit_checker.py 及其现有质量校验调用入口、src/document_authoring/service.py、src/api/routes/document_generation.py、src/api/routes/exports.py、src/result_exports/worker.py。
- Modify：来源引用与文档审核信息展示。
- Test：tests/test_multi_kb_authoring_roles.py、tests/test_multi_kb_export_authorization.py（新增）。

**Interfaces**

- Consumes：章节需求、来源角色、证据 ID、完整来源依赖。
- Produces：既有质量问题/缺口记录；导出允许或拒绝的明确原因。
- 增加 required_source_role(requirement, purpose) 与 role_fit(evidence_role, required_role) 纯策略函数；不在其中判定用户权限或文档真实性。

- [ ] 建立固定案例：A 电压 5V，B 电压 12V；给 A 写说明只能将 5V 作为主库事实，12V 可明确标为 B 的参考差异。
- [ ] 增加 A 无规格、B 有规格的用例：关键字段保持缺口；不能仅因 B 排名更高通过质量门。
- [ ] 比较场景允许并列展示两者及冲突，不强制 primary 优先；standard 只有在真实元数据支持时才能被称为正式规范。
- [ ] 文档、回答导出保存真实来源依赖与继承闭包。下载/历史重新打开前逐消息/产物复查；B 被撤销时，受其影响的内容不得经旧缓存、引用预览或导出取回；能证明只依赖 A 的独立消息仍可展示。
- [ ] 运行角色、冲突、撤销与导出测试，并回归既有文档 finalize/feedback。
- [ ] 提交：feat: enforce source roles and export access for multi-kb documents。

### T11：观测、迁移演练与分阶段发布

**Files**

- Modify：src/observability/metrics.py、src/settings.py、.env.example、deploy/postgres/README.md。
- Modify：wiki/04-conversation-turns.md、wiki/05-agent-runtime.md、wiki/06-tools-and-evidence.md、wiki/13-document-authoring.md、wiki/14-authoring-render-and-api.md、wiki/15-wiki-assets-projects.md。
- Create：evaluation/datasets/multi_kb_v1.jsonl；docs/superpowers/implementation/multi-kb-acceptance.md。
- Test：tests/test_multi_kb_settings.py、tests/test_multi_kb_observability.py（新增）。

**Interfaces**

- Consumes：所有阶段的 outcome、trace、coverage、存储 schema。
- Produces：按阶段启用的发布包、迁移验证记录、验收报告。

- [ ] 配置校验覆盖开关依赖、来源数/预算正值以及 API/Worker 配置一致性；关闭后新多库任务明确拒绝，不降级单库。
- [ ] 指标只使用低基数标签：phase、retriever_kind、outcome、purpose；KB ID/名称、用户、文件名放受控 trace，不作指标标签。
- [ ] 记录后端实际调用数、排队/执行时间、每来源命中/丢弃数、token 裁剪、覆盖缺口、迟到结果丢弃、source_changed 和拒绝原因。
- [ ] 记录 chat/frozen 模式与 remote_scope_fallback 状态，证明 P2 没有执行宽回退。
- [ ] 对 §3.6.3 的质量门槛记录逐题标注、总体指标、基线和失败案例；RRF 参数与是否启用 reranker 均写入验收报告。
- [ ] 以 1、2、4 个来源及并发多用户运行隔离环境负载验证，比较基线与新方案的 p50/p95、请求放大和池饱和；业务验收延迟阈值使用部署已有 SLO，不在缺乏基线时承诺固定性能。
- [ ] SQLite 旧数据、PG 旧 schema、API/Worker 重启、取消/重试、功能关闭、回滚均进行演练并记录实际命令、版本和结果。
- [ ] 按第 7 节验收矩阵逐条记录通过/失败/未运行。实际 PG 或真实 RAGFlow 未运行时，相关阶段不得标为完整验收。
- [ ] 发布说明明示：来源集合调整会保留草稿并创建新会话；撤权影响消息/产物及继承依赖；多库长期记忆禁用；RRF 为初始排序且已通过质量门槛；P2 仅保证受管写入下的变化检测。
- [ ] T11 在 M2/M3/M4 各阶段执行相关门禁，不能全部拖到最后；M5 完成整体联调和迁移/回滚演练。
- [ ] 提交：docs: verify and document multi-kb retrieval rollout。

### 6.1 依赖关系与首个纵向闭环

依赖关系按接口和正式发布门禁表示：

~~~text
T0 + T1 ─→ T4 ─→ T5
T1 ─→ T2 ─→ T3
T2 + T3 + T4 + T5 ─→ T6 ─→ M2：正式 P0
正式 P0 ─→ T7 ─→ M3：P1
T2 + T4 + T5 ─→ T8
T7 + T8 ─→ T9 ─→ T10 ─→ M4：P2
T11：随每阶段验证，最后完成 M5 总体验收
~~~

T8 的文档快照与指纹部分可在 T7 期间按已确定接口开展；P2 发布仍要求 P1 所需的严格结构化适配完成。

**M1 首个可演示闭环不是新增一套实现。** 将 T1–T6 中最小必要部分先按一条用户路径串联：

1. T0 完成多库池生命周期与原单库兼容；T1 完成两库授权与稳定范围。
2. T2 先完成隔离 SQLite 的会话/turn 范围持久化；T3 接通真实 API → Worker → runner，固定来源集合、独立 thread、取消及长期记忆禁用均必须有效。
3. T4 仅接 document_search 的一次查询，chat 模式逐库检索；T5 实现真实身份、基本合并与引用。
4. T6 提供两库 answer 的最小选择与消息引用展示，验证 A/B 命中、C 不参与、无权限整次拒绝和 Worker 重启恢复。
5. 使用 stub 模型与可控资料验证路径，再进行一次真实 RAGFlow 双库取证；结果标为内部演示。没有真实后端时，只能标为桩演示。

M1 不开放 compare、4 库、批量查询、结构化检索或跨库文档生成。它也不能跳过权限、历史隔离和来源身份以换取更早演示。M2 补齐 T1–T6 的完整范围、真实 PG 契约、回退/超时/409 场景与质量门禁后，才允许正式 P0 发布。

### 6.2 里程碑与初始工程人日估算

估算假设：1 名熟悉该仓库的开发者，现有测试与前端构建正常，可使用一次性 PostgreSQL 与 RAGFlow 测试实例。下表是每个里程碑的增量有效工程人日，M1 的成果在后续复用，不重复计算。外部环境等待、业务审批和带外历史索引建设不计入。

| 里程碑 | 交付与退出条件 | 增量人日初估 |
| --- | --- | --- |
| M0 / T0 | 池生命周期、旧单库分支兼容验证；多库入口仍关闭 | 1–2 |
| M1 | 上述两库 answer 文档纵向演示，隔离 SQLite；不计正式发布 | 4–7 |
| M2 / 正式 P0 | 完整 T1–T6，真实 PG/Worker 恢复、批量/预算/错误语义、409 体验与质量门槛 | 8–13 |
| M3 / P1 | T7 四类异构来源能力验证、范围隔离与混合场景 E2E | 6–10 |
| M4 / P2 | T8–T10 快照、索引代次、规划/生成/恢复、角色校验与导出 | 12–20 |
| M5 / 整体验收 | T11 的最终跨阶段回归、观测、负载及迁移回滚演练 | 4–7 |

合计约 35–59 个有效工程人日，属于排期初值，不是交付承诺。M1 后依据实际改动范围、PG 契约难度与回归耗时重估 M2–M5；P1 若任一适配器缺少严格记录/版本能力，或 P2 来源治理覆盖不足，记录具体工作量和阶段阻塞，不能把 filter_unsupported 当作该类型功能已交付。

## 7. 验收矩阵

| 编号 | 场景 | 必须满足的结果 | 阶段 |
| --- | --- | --- | --- |
| A01 | A/B 均授权、C 未选 | 实际调用仅涉及 A/B；证据来自两者且不含 C | P0 |
| A02 | A 授权、B 未授权 | 整体拒绝，后端调用数为 0 | P0 |
| A03 | 不同部门同名库 | 按 ID 与部门拒绝跨部门来源 | P0 |
| A04 | 旧客户端只传 kb_name | 保持单库行为、旧状态和历史可读 | P0 |
| A05 | 空列表/重复 ID/错误角色/超过 4 库 | 明确 422，不扩大或静默裁剪范围 | P0 |
| A06 | 同一 client_request_id 重放 | 同范围同请求返回原轮次；新协议冲突请求 409 | P0 |
| A07 | A+B 改为 A，或用途/角色变化 | 保留未提交文本后创建新会话，不加载旧 checkpoint/摘要/附件绑定 | P0 |
| A08 | Worker 重启/轮次重试 | 恢复同一范围并重新授权，不扩大选库 | P0 |
| A09 | 同名文件、同 chunk ID | 引用和证据身份仍区分库与文档 | P0 |
| A10 | A 命中/B 空 | 区分空与失败；回答说明覆盖范围 | P0 |
| A11 | B 超时/全部失败 | 局部缺口可见；全部失败不生成事实答案 | P0 |
| A12 | 多 query × 多库 + 单线程池 | 不死锁、不超预算、不接收迟到结果 | P0 |
| A13 | 已撤销 B 的权限 | 停止旧范围新执行；受影响消息/产物不可读，可证明无 B 依赖的独立消息可读 | P0 |
| A14 | attachment_only | 0 次 KB 检索，附件 ACL 保持 | P0 |
| A15 | 跨库重复正文、token 裁剪 | 保留出处；裁剪后 coverage 真实 | P0 |
| A16 | A/B 同名工作表与相同器件编号 | 结构化查询定位到各自记录/设计 | P1 |
| A17 | 不支持冻结记录/产物校验的适配器 | filter_unsupported；P2 不执行宽检索替代冻结范围 | P1/P2 |
| A18 | 多库混合内容与长期记忆 | 不加载/写入长期记忆；旧单库功能正常 | P0 |
| A19 | 预览、确认、生成、恢复 | snapshot_id/hash 一致，含两库来源 | P2 |
| A20 | 生成中替换同名文件/重新解析产物 | source_changed；不默默读取新版本 | P2 |
| A21 | A=5V/B=12V，为 A 写作 | A 事实和 B 参考明确区分，冲突可见 | P2 |
| A22 | A 缺关键证据/B 有数据 | 记录缺口，不以 B 值替代 A | P2 |
| A23 | 来源名单变更后重试旧计划 | 不能复用旧批准哈希/缓存，应创建新计划 | P2 |
| A24 | 源库读权限 + 目标库写权限 | 两类授权分别生效；不要求源库写权限 | P2 |
| A25 | SQLite 与真实 PG 相同场景 | 行为与 round-trip 一致，迁移幂等 | 各阶段 |
| A26 | P0/P1/P2 开关关闭或依赖缺失 | 明确不可用，无单库静默降级 | 各阶段 |
| A27 | 同文件两 chunk、同定位文本变化 | 源文件哈希相同仍由定位区分；文本变化产生不同 evidence_text_hash | P0 |
| A28 | 普通与 strict 文档回读 | 都有可信 local_record_id、源文件哈希、片段哈希和库身份 | P0 |
| A29 | P0 document_ids 被拒并返回其他库/文件 | 有限回退仅保留本地允许记录，记录回退和丢弃数 | P0 |
| A30 | P2 冻结 document_ids 被拒 | 一次请求后返回具体失败状态，不去掉参数发起宽回退 | P2 |
| A31 | 缺失记录/指纹变化/正常无命中 | source_unavailable、source_changed、success_empty 可区分 | P0/P2 |
| A32 | 原共享开关 false、多库开启 | 多库受共享池治理，旧单库批量分支不被切换，关闭回收正确 | T0/P0 |
| A33 | 来源变化 409 后继续编辑 | 草稿和新来源保留，其他 409 不误处理；附件/模板重新验证 | P0 |
| A34 | B 未被引用但进入上下文或由历史继承 | 仍记 B 依赖并在撤权时保护；仅 A 的独立消息可读 | P0/P2 |
| A35 | 固定多库相关性与覆盖评测 | 达到锁定质量门槛并保留逐题结果；未达标不能仅附风险说明发布 | P0/P2 |

建议的业务评测数据至少包括：双项目比较、主项目写作参考历史项目、项目+规范、同名不同版本、表格+网表+说明书、跨部门同名库、无命中、超时、来源更新、授权撤销。

测试须检查工具调用范围、持久化快照、证据身份与实际结果，不能只检查“回答中提到了 A 和 B”。

## 8. 发布、迁移与回滚

推荐顺序：

1. 在隔离环境完成模型/迁移和旧数据回读；新字段先以默认空值部署，功能开关关闭。
2. 先完成 T0 的池生命周期和旧单库兼容验证；不把翻转 SHARED_RETRIEVAL_EXECUTOR_ENABLED 作为多库上线条件。
3. 在隔离环境完成 M1，标记内部演示。随后部署认识新范围字段和 scoped outcome 的 API/Worker，确认版本一致；旧 Worker 不得领取多库任务。
4. M2 的真实 PG、质量指标与全部 P0 必要门禁通过后，开启 P0 小范围使用。
5. M3/P1 通过结构化范围测试后启用对应适配器；不支持的来源明确受限。
6. M4/P2 完成冻结回退禁令、版本/来源错误区别、规划生成 E2E 和导出复查后开启文档生成。
7. T11 按阶段记录门禁，M5 完成整体回归和演练；保留独立关闭新任务的能力。

回滚规则：

- 应用级回滚优先关闭新任务入口，取消或排空运行中的多库任务；已有多库任务不得交给不理解其范围的旧 Worker。
- 回滚版本必须拒绝不支持的快照/协议版本，而不是忽略字段继续按主库执行。
- 新数据库列和快照表采用加法迁移，紧急回滚时保留数据，不执行破坏性删列。
- 若恢复完全旧二进制，必须先隔离多库会话/任务的读取与执行入口；仅关闭新建按钮不充分。
- 已生成的历史产物与来源快照保留；访问时继续做来源权限校验。
- 普通单库请求和原快照依旧走原路径，不依赖多库开关。

## 9. 实施验证命令

下列命令用于后续实现阶段；本次仅生成方案，没有执行新增功能测试。

T0 生命周期与兼容验证（新增测试实施后）：

~~~bash
uv run python -m pytest -q tests/test_multi_kb_executor_lifecycle.py tests/test_shared_retrieval_executor.py
~~~

基础范围回归：

~~~bash
uv run python -m pytest -q tests/test_kb_scope.py tests/test_ragflow_document_scope.py tests/test_project_retriever_dispatch.py tests/test_document_evidence_providers.py
~~~

新增多库测试完成后：

~~~bash
uv run python -m pytest -q tests/test_knowledge_scope.py tests/test_multi_kb_store_contract.py tests/test_multi_kb_turn_flow.py tests/test_multi_kb_history_scope.py tests/test_multi_kb_retrieval.py tests/test_ragflow_scoped_outcome.py tests/test_multi_kb_evidence_fusion.py tests/test_multi_kb_citations.py
~~~

P1/P2：

~~~bash
uv run python -m pytest -q tests/test_multi_kb_structured_retrieval.py tests/test_multi_kb_authoring_sources.py tests/test_multi_kb_authoring_flow.py tests/test_multi_kb_authoring_roles.py tests/test_multi_kb_export_authorization.py
~~~

控制面 PG 契约（运行前由测试环境提供 HDB_TEST_POSTGRES_DSN，指向一次性数据库）：

~~~bash
uv run python -m pytest -q tests/test_control_plane_store_contract.py tests/test_control_plane_conversation_service_postgres.py tests/test_multi_kb_store_contract.py
~~~

前端在 frontend 目录执行：

~~~bash
npm test
npm run build
npm run test:e2e -- multi-kb-chat.spec.ts
~~~

实施结束后按改动范围运行 lint、完整必要回归和真实 RAGFlow/LLM 验收；不要把 stub E2E 或因缺少 PG 而 skip 的结果写成生产联调完成。

## 10. 参考与本次交付说明

- [原共享会话](https://chatgpt.com/share/6acf6c94-b8d0-83ea-aab8-65a074da7969)：本次评审的原方案来源；其中引用的远端 develop 与本地分支不完全等价，本计划以本地 3739b4b 为准。
- [Dify Knowledge Retrieval](https://docs.dify.ai/en/cloud/use-dify/nodes/knowledge-retrieval)：说明多个知识库同时检索、合并后节点级重排的模式。
- [RAGFlow HTTP API](https://ragflow.io/docs/http_api_reference#retrieve-chunks)：多 dataset、document_ids、元数据范围；使用当前部署实际支持的参数，不照搬新版 API 字段。
- [LlamaIndex Sub Question Query Engine](https://developers.llamaindex.ai/python/examples/query_engine/sub_question_query_engine/)：按来源拆分子问题再综合结果。
- [LangChain Retrieval](https://docs.langchain.com/oss/python/deepagents/retrieval)：将不同数据源作为受控检索工具的架构。

**本次已完成：** R2 纳入第二轮评审，明确两种哈希与回读字段、scoped outcome、P0/P2 回退边界、独立 T0、M1 纵向演示、任务依赖/初始估算、409 恢复、消息依赖保护与 F1 记忆恢复条件。本文共有 T0–T11 共 12 个实施任务和 A01–A35 共 35 条验收场景。

**本次未执行：** 多库业务实现、数据库迁移、服务配置变更与部署。本文中新增测试和验收案例均待后续实施，前述 25 项通过仅代表已验证的现有基线。

