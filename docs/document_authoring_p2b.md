# P2b：有界 Harness 与语义辅助

本阶段在 P2a 的项目、基线、来源快照和受控 XLSM/XLSX 渲染闭环之上，加入内部 `Document Harness`。它是独立于问答 Agent 的有限状态执行器；不需要 Claude Code、MCP 或外部模型服务即可运行测试和受控的离线 Writer。

## 执行边界

```text
approved internal_harness DocumentSchema
  + approved, version-pinned HarnessPolicy
  + immutable SourceSetSnapshot
  → InformationRequirement
  → strict RetrievalOutcome
  → validated Evidence Package
  → Managed Writer structured DocumentUnitDraft
  → assertion / contamination / consistency validation
  → validated WorkbookFillPlan
  → review_candidate + ValidationReport + Human Review Queue
```

- `src/document_authoring/harness/graph.py` 是独立 Authoring Graph；它不能访问 Shell、任意 SQL、任意路径、模板原件或问答 Agent 的状态。
- `HarnessPolicy` 固定允许工具、最大步骤、最大检索轮次、最大单元数、Writer Provider 和 Prompt 版本。达到上限会记录 `harness_budget_exceeded`，并进入 `waiting_human`，不会伪装为 TBD。
- 工作单会同时冻结 `harness_policy_id` 与 `harness_policy_version`。后续发布相同 Policy ID 的新版本不会改变已创建任务。
- `ManagedWriter` 只接收一个单元的 Schema 文本和已验证 Evidence Package；Provider 必须返回结构化 `DocumentUnitDraft`，不能返回或改写二进制文档。
- 运行清单记录来源快照 hash、基线 hash、来源版本、解析产物、区域策略、模板 hash、Schema/策略 hash、预算、Prompt 版本及本次使用的 Evidence 内容 hash。
- Writer 输出先校验 Evidence ID、断言 Evidence ID 和词法锚点；再检测登记的 `LegacyTemplateClaim`，并检查带 `consistency_key` 的跨单元冲突。仅 `supported + ready_to_render` 的 Draft 可生成 FillPlan。

## 调用方式

先注册并批准 `HarnessPolicy`，再创建 execution mode 为 `internal_harness` 的 Schema 工作单。服务端要求调用方提供一个 `(InformationRequirement, attempt) -> RetrievalOutcome` 回调；生产适配器应以 `ProjectEvidenceRetrievalService` 在冻结 Source Set 内构造该结果，不能使用问答链路的无范围 fallback。

```python
candidate = document_generation.run_internal_harness(
    ctx,
    work_order_id,
    retrieve=project_scoped_retrieve,
)
```

Streamlit 创建页会根据批准 Schema 的 `execution_mode` 显示 Harness Policy 选择；确定性 Schema 不显示该项。运行状态中会显示最新 HarnessRun 的节点、步骤和检索轮次。

## 已覆盖的安全回归

`tests/test_document_authoring_p2a.py` 覆盖：

- 冻结来源集的越界 Evidence fail-closed；
- 离线 deterministic Writer 只能使用经过验证的 Evidence；
- Policy 新版本不会影响已创建工作单；
- Legacy Template Claim 进入 Draft 时生成 `template_contamination` 并转人工；
- 超过 Harness 步骤预算时不发起检索、转人工并保留验证报告。

## 后续可靠性边界

P2c 的第一增量已经补充 SQLite 租约、heartbeat、fencing token、Checkpoint、Draft 节点回执以及暂停/取消/有限重试，见 [document_authoring_p2c.md](document_authoring_p2c.md)。多实例调度、交易 outbox、Artifact/人工事件回执和进程重启后的回调重建仍不属于当前实现。
