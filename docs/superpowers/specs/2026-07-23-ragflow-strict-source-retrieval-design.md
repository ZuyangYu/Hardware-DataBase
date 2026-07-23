# P0.5-B：RAGFlow 严格选源检索设计

## 背景与目标

当前普通问答使用的 `RAGFlowBackend.retrieve()` 在带 metadata 条件的查询为空时，会移除条件后重试，并在应用层按本地映射过滤。这保留了问答兼容性，但不满足正式文档生成的 fail-closed 要求。

本子项目为 Document Harness 与确定性文档工作流增加仅限正式文档链路的 RAGFlow 严格选源适配器。它必须只使用 Work Order 冻结的 `SourceSetSnapshot`，且不能因远端不支持过滤、返回空结果或返回范围不可靠而扩大查询范围。

普通问答的 `RAGFlowBackend.retrieve()` 和其 metadata fallback 行为不在本子项目修改范围内。

## 决策与边界

采用独立的 `StrictRAGFlowRetrievalAdapter`，作为 `ProjectEvidenceRetrievalService.retrieve()` 的单来源回调实现，而不向普通 `RAGFlowBackend.retrieve()` 增加 `strict` 分支。

适配器的可信输入为：

- 当前认证的 `RequestContext`；
- `InformationRequirement`；
- `SourceSetSnapshot` 中的单一 `source_version_id` 与已冻结 `processing_artifact_id`；
- 该 ProcessingArtifact 的 RAGFlow `backend_locator`；
- 快照冻结且人工批准的 Region Policy 版本。

调用者、UI、Managed Writer 与外部 Agent 都不能传入任意 RAGFlow document ID、metadata 条件、文件路径或项目范围。

## 检索流程

1. `ProjectEvidenceRetrievalService` 继续逐一调用适配器，不允许一次提交可扩大范围的版本集合。
2. 适配器从 `ProcessingArtifact.backend_locator` 获取精确的 `dataset_id` 与 `document_id`。缺少、格式非法或与本地 Pipeline Record 不一致时，报告 `source_unavailable`。
3. 适配器发起只针对该冻结 document 的服务端检索。请求必须带 document-ID 或经过验证的等价 metadata 过滤；`top_k` 在该过滤之后生效。
4. 适配器不得调用没有该限制的检索接口，也不得在零命中或本地过滤后零命中时重试全局查询。
5. 返回的每个 chunk 必须可验证属于冻结 document，并具有可以映射到来源 Region Policy 的稳定 locator/quote span。范围不匹配、缺失定位或无法前置区域过滤时，报告 `filter_unsupported`。
6. 仅允许区域中的 chunk 被转换为 `EvidenceEnvelope`，由既有 `ProjectEvidenceRetrievalService` 再验证项目、版本和处理产物范围。

## 错误语义

| 情况 | Source outcome |
| --- | --- |
| 成功且存在允许 Evidence | `success_with_hits` |
| 成功、范围和区域均已验证但无命中 | `success_empty` |
| 冻结来源没有可用远端 locator，或远端服务不可用 | `source_unavailable` |
| 请求、响应解析或不可恢复的远端调用错误 | `retrieval_failed` |
| 远端不能强制过滤、返回越界 chunk、缺少稳定 locator，或无法在排序前执行 Region Policy | `filter_unsupported` |

`success_empty` 是唯一允许上层按缺失策略产生 TBD 的空结果。其余状态必须保留失败语义，不能被合并为 `missing` 或触发无范围 fallback。

## 实现位置

- `src/projects/`：严格适配器及其 RAGFlow locator/Region Policy 验证逻辑。
- `src/pipelines/document_rag/`：仅补充可复用的、受 document 限制的 RAGFlow client 调用与最小 chunk 转换，不改变普通问答入口的 fallback 策略。
- `src/document_authoring/`：以适配器构造 `RetrievalOutcome` 的正式文档检索入口。
- `tests/`：适配器单元测试、Document Harness 集成测试，以及普通问答 fallback 回归测试。
- `docs/`：P0.5-B 结果报告，包含真实后端 capability 结论与 go/no-go 状态。

## 验收

自动化测试必须证明：

1. 严格请求只包含单一冻结来源的服务端过滤；
2. 过滤后才应用 top-k；
3. 严格路径没有 metadata-free 或全局 fallback；
4. 越界 chunk、缺失 locator 和不允许区域均不能成为 Evidence；
5. 零结果、来源不可用、检索失败和过滤不支持被映射为不同的 `RetrievalOutcome`；
6. 普通问答原有 fallback 回归保持通过；
7. 真实 RAGFlow 冒烟测试在配置环境中验证服务端 document-ID/等价 metadata 过滤和 locator 行为。

真实后端不具备可验证的严格选源能力时，文档生成的 RAGFlow 适配器必须保持不可用，P0.5-B 结论为 no-go；不得用普通问答路径替代。

## 非目标

- 不修改普通问答的 RAGFlow 检索语义；
- 不实现 Spreadsheet 或 Circuit 的严格适配器；
- 不实现 DOCX/Markdown Renderer、MCP Adapter、ProjectFact 或 ProjectSnapshot；
- 不将真实 RAGFlow 环境冒烟测试伪装为离线单元测试。
