# P2a：受控 XLSM/XLSX 文档生成

本实现对应《Hardware-DataBase_Agentic-RAG 改造方案》中的 P1、PR-04、PR-05 和 PR-06A 的最小闭环。它不依赖 Claude Code、Codex、MCP 或模型调用。

## 已实现的边界

```text
ProjectPrincipalBinding
  → ProjectBaseline（批准且不可变）
  → SourceSetSnapshot（版本、解析产物、区域策略冻结）
  → RetrievalOutcome（区分空结果与失败）
  → Evidence Matrix / DeterministicRuleSpec
  → review_candidate XLSM/XLSX
  → hash 绑定的审批事件
  → 字节不变 approved_release
```

- `src/projects/` 保存 Project、成员权限、业务文档版本、解析产物、项目来源绑定、基线和区域策略；`ProjectStore` 有独立的 SQLite migration ledger。
- `SourceSetSnapshot` 只会接受已批准/发布的基线和来源版本、状态为 `ready` 的解析产物，以及人工批准的 allow Region Policy。它冻结输入，但每次读取/执行仍重新校验当前项目成员权限。
- `ProjectEvidenceRetrievalService` 一次只向适配器传递一个冻结的版本和解析产物集合；适配器返回越界证据会得到 `filter_unsupported/retrieval_failed`，绝不会退化为全局检索。
- `TemplateVersion`、Workbook Region 和 Template Unit Binding 必须先登记、再批准。生成时不会重新猜测可写区域。
- XLSM/XLSX renderer 直接复制 OOXML 包，仅修改批准的 worksheet cell。它拒绝公式单元和公式形式文本，比较前后 Part Manifest/relationship hash，并拒绝变动宏、外部链接和嵌入对象等主动内容。带主动内容的模板还要求精确内容 hash 位于批准 Renderer Policy 的 allowlist。
- 审批哈希由 `candidate_content_hash + validation_report_hash + source_set_snapshot_hash` 组成；正式发布沿用候选字节，不能在审批后无感重渲染。

## 最小注册顺序

1. 创建项目并写入 `ProjectPrincipalBinding`；项目管理员、作者、审阅者和批准者均是独立角色。
2. 注册 SourceAsset、LogicalDocument、SourceVersion、ProcessingArtifact、ProjectSourceBinding 和 `allow` Region Policy。
3. 创建 `approved` 或 `released` ProjectBaseline。
4. 注册 RendererPolicy、DocumentSchema、DeterministicRuleSpec，以及受控模板的 Region/Binding；再批准模板。
5. 使用 `DocumentGenerationService.create_document_work_order` 创建工作单。这一步会在同一调用中生成不可变 SourceSetSnapshot。
6. 使用受控检索生成 `RetrievalOutcome` 后，调用 `run_deterministic_work_order` 或 `start_document_generation`。
7. 审阅 review candidate，提交人工事件，然后调用 `approve_document_artifact` 发布字节不变的 approved release。

Streamlit 的“文档生成”页只允许选择当前用户具有项目成员权限的 Project、批准基线、模板和 Schema；它不会使用聊天知识库或会话状态作为生成输入。

## 当前阶段限制

- P2a 只实现 XLSM/XLSX 的确定性检查项；P2b 已在独立 Harness 中加入 Managed Writer、断言—证据校验、模板污染和跨单元一致性检查，见 [document_authoring_p2b.md](document_authoring_p2b.md)。DOCX/Markdown 和外部 Agent Adapter 仍属于 P3。
- 后台执行器是单实例、单线程的 P2a 实现。租约、心跳、fencing token、checkpoint、暂停/恢复和多实例部署属于 P2c。
- 真实 RAGFlow/Spreadsheet/Circuit 的领域适配应通过 `ProjectEvidenceRetrievalService` 接入；现有问答链路保留兼容行为，不可将其无范围 fallback 用于正式文档生成。
- ProjectFact 与 ProjectSnapshot 尚未启用。文档工作单仍完全由批准 Schema、Baseline 和 SourceSetSnapshot 驱动。
