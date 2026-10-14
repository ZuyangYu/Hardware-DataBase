# 24 · 主流与前沿方案对标、差距及验证建议

日期：2026-10-08。评审对象：`Hardware-DataBase-observability` 当前工作区，
`develop` / `62a85a7`，**包含已有未提交的可靠性改造**。

实施跟进：随后从 `a2396d6` 开始落地 P0（G01–G03），本页保留原始复现证据；
当前代码契约和验收状态见 [实施记录](../docs/superpowers/plans/2026-10-8-production-feedback-cost-observability-implementation.md)。

对应代码：`src/observability/`、`src/evaluation/`、`src/agents/`、
`src/core/chat_model_runtime.py`、`src/pipelines/document_rag/ragflow_backend.py`、
`deploy/observability/`、`.github/workflows/ci.yml`、`frontend/e2e/`。

## 1. 结论

**技术路线符合主流，已经具备部分前沿能力；主要差距在质量闭环、评测可信度、
观测链路自身可靠性、成本归因和完整生产验收。**

当前更准确的定位是：面向硬件资料的领域化 Agentic RAG 与受控文档生产平台，
具备较完善的业务可靠性机制和统一遥测基础。

| 维度 | 判断 | 依据 |
|---|---|---|
| 技术选型 | 与主流一致 | deepagents/LangGraph、FastAPI、PostgreSQL、Redis、OTel/OpenInference、RAGAS |
| 硬件领域能力 | 有明确差异化 | EDF 结构事实、表格只读 SQL、Datasheet 关联、项目/基线、不可变导出及工程文档放行 |
| 持久任务与接管 | 基础较强，已有实测 | claim/lease/fencing、attempt checkpoint、跨副本恢复、SSE 重放 |
| AI 可观测性 | 已有基础平台集成 | Collector → Tempo/Phoenix、Metrics → Prometheus、Logs → Loki/Grafana |
| 持续质量工程 | 部分完成 | 数据集、快照、QC、RAGAS、历史对比已有；发布门禁和生产反馈闭环仍不充分 |
| 大规模/长期生产能力 | 验证仍不完整 | 已有短时真实冒烟；四类混合业务、正式过载、24h 长稳态尚待验收 |

这里的对标是功能和工程实践对标。未在相同数据、模型、硬件和到达率下运行外部项目，
因此不据此宣称回答准确率、吞吐或延迟超过某产品。

## 2. 与代表方案对比

| 对标对象 | 已有对应能力 | 差距与可借鉴能力 |
|---|---|---|
| **RAGFlow / Dify**：企业知识与 Agent 应用 | RAGFlow 混合检索、元数据作用域、多工具 Agent、附件/文档/表格、电路领域工具 | 检索调参的可重复实验；解析结果定位；跨格式统一证据；上游新检索能力的显式适配与契约验证 |
| **LangGraph**：有状态 Agent 运行时 | checkpointer、attempt 隔离、fencing、恢复、流式输出、人工交互 | 将评测等仍使用本地线程的长任务纳入共享任务账本；扩展跨进程、跨版本和副作用重放验证 |
| **Phoenix**：开源 AI 工程平台 | 已部署 Phoenix 并转发 OpenInference traces | 把业务评测分数回写为 span annotations，串联生产 trace、人工复核、数据集版本和 experiments；Prompt 版本管理 |
| **Langfuse / LangSmith**：完整 AI 质量循环 | tokens、延迟、错误、工具轨迹、离线评测、历史报告 | 模型/Prompt 版本与成本归因、反馈队列、在线抽样评测、逐样本对比、发布质量回归 |
| **OpenTelemetry + Grafana/SRE**：通用生产观测 | 三信号、W3C context、低基数标签键、脱敏、15 条告警、通知接入模板 | Collector WAL、自监控、无数据/进程失联告警、基础设施指标、SLO error budget 与 burn-rate |
| **GraphRAG / RAGFlow 知识编译**：结构化知识与多跳检索 | 电路连通图、Wiki 蒸馏/版本/一跳扩展、跨源查询、项目来源冻结 | 领域实体之间的跨源关系；知识更新/删除后的派生产物失效；图增强对复杂问题的受控 A/B |
| **Docling 等结构化多模态解析** | 已有 OCR/附件处理、表格结构与电路解析，EvidenceEnvelope 含 locator/quote_span | 页码/bbox/图像裁片/表格坐标的统一证据定位；原理图图像与 EDF、Datasheet 曲线的联合验收 |

值得保留的优势：确定性电路事实和只读表格计算适合硬件问题；项目基线、来源版本、
模板/产物治理比单纯聊天应用更贴近工程业务；已存在 Wiki 和图能力，后续应增强现有路径。

## 3. 本次可复现的具体问题

### G01 · P0：质量门禁可能把“无法评价”当成“通过”

位置：`src/evaluation/gates.py:28-90`、`src/evaluation/schemas.py:173-181`、
`src/evaluation/service.py:664-719`。

当前 `evaluate_gate()` 会跳过失败/缺失的分数，对低覆盖率指标也跳过阈值判断。
最后按 `not failures` 得出通过。评分诊断已能显示技术失败与覆盖不足，
但它们没有转化为发布门禁的失败/不可判定状态。

本轮直接调用真实门禁函数，配置 `faithfulness >= 0.75`、`fail_on_threshold=True`：

| 输入 | 实际输出 |
|---|---|
| 非 critical 样本的裁判全部失败 | `passed=true, exit_code=0` |
| 阈值指标完全缺失 | `passed=true, exit_code=0` |
| 10 个样本仅 1 个有效分数且为 0.1 | `passed=true, exit_code=0` |
| 分数为 NaN | `passed=true, exit_code=0` |

完善建议：

- 区分 `pass / fail / insufficient_evaluation`，发布模式中后两者都返回非零。
- 按**指标应适用的样本数**计算覆盖率；区分合法不适用、缺失、失败、取消和评分未完成。
- critical 样本必须有应需指标的有效结果；检查分数有限性及各指标自己的取值范围。
- 发布报告包含有效/应评数量、裁判失败率、采集失败率和实际使用的阈值。

必要测试：全部裁判失败、缺失阈值指标、critical 指标缺失、混合 cohort、合法 N/A、
低覆盖率、NaN/Infinity、暂停/取消/部分报告及退出码。

### G02 · P0（多副本运行评测时）：评测任务归属仍是进程内状态

位置：`src/evaluation/run_control.py:414-419,550-563,1123-1133`；
`src/api/routes/evaluation.py:472-493`。

API 内通过 `_threads` 防重复启动，并依靠这个线程表判断任务是否失联。
另一个进程拥有自己的线程表。在共享评测目录下，另一个 API 副本读取任务可能
将活任务判作 orphan 并持久化为 paused；启动保护也未跨进程共享。

本轮用两个 Controller 模拟两个独立线程表，A 的任务线程保持存活：

```text
owner_before=running
other_controller_read=paused
owner_thread_still_alive=true
persisted_status_after_read=paused
```

该复现使用临时目录和等待线程，未调用真实模型；验证的是状态归属缺陷。

完善建议：评测引入共享 job owner、lease、heartbeat、fencing/CAS，
由独立评测 worker 消费；状态读取为只读，失联恢复由持有权限的协调器执行。
现有快照与逐单元格续评分机制可以继续复用。

必要测试：A 执行/B 读取，双副本同时 start/resume，杀 owner 后接管，
旧 owner 写入被拒、最终报告单次提交、采集和评分的跨进程暂停/取消。

### G03 · P1：严格基线兼容性没有核对数据集内容哈希

位置：`src/evaluation/history.py:32-47,291-388`。

样本 cohort 指纹来自样本 ID；`compatibility_report()` 检查 KB、cohort、指标、
评估模型配置和快照归属，未比较已有的 `dataset_sha256` 或
`execution_dataset_sha256`。同一批 ID 的问题/参考答案发生变化，仍可能显示严格可比。

本轮构造两个数据集哈希与执行数据集哈希都不同、其余条件相同的真实 Summary：

```text
dataset_hash_equal=false
execution_hash_equal=false
compatibility_report_compatible=true
```

完善建议：严格比较规范化执行样本的内容哈希，记录裁判/评分 Prompt 和参数版本；
同时记录业务模型、Agent Prompt、检索配置、源码指纹及来源/索引版本。
其中实验变量允许不同，但必须显式列出；输入数据与评判规则变化不能伪装为同一基线。
验收脚本已经记录源码 SHA-256，可将这一模式推广到常规质量评测。

必要测试：相同 ID 不同问题/参考答案、相同数据不同顺序、不同来源版本、
不同裁判 Prompt、旧报告缺元数据，以及允许的模型/Prompt 实验变量。

## 4. 与成熟方案相比仍需完善的能力

### G04 · P1：生产失败案例到评测集的闭环

当前 Phoenix 已接入 trace 接收，业务侧评测使用本地 JSONL/报告；已有历史对比与
数据集冻结，但没有查到业务侧统一的 trace annotation、用户回答反馈、复核队列、
生产样本入库和实验关联链路。

建议优先利用已经部署的 Phoenix：

```text
turn/trace → 反馈或抽样规则 → 人工复核 → versioned dataset
          → 固定裁判的 experiment → 回归门禁 → 发布版本
```

最小新增契约：`turn_id/trace_id/span_id`、反馈标签/理由、证据/来源版本、
模型/Prompt/检索版本、复核状态、dataset example/version、experiment/run。
将 RAGAS 结果映射为 annotation，保留原始快照作为业务证据。

### G05 · P1：成本与模型维度归因

位置：`src/core/chat_model_runtime.py:379-445,516-627`、
`src/observability/metrics.py:278,433-446`、`semantic.py:30-56`。

已有总 input/output tokens 和 usage 是否返回的标志；指标以 provider 聚合，
多个 OpenAI-compatible 端点归一成 `custom`。未查到完整的价格表/成本计算、
缓存与 reasoning token 拆分、业务模型版本维度。

建议：受控模型/端点别名、operation/profile 维度、cache/reasoning usage、
版本化价格表、每个成功业务任务成本和裁判成本；usage 缺失/价格未知标为 unknown。
只对叶子 LLM 调用计费，避免 parent/agent 的 token 汇总重复计费。
指标标签使用有限注册值，细粒度用户/项目成本保存在账本或 trace 中。

### G06 · P1：GenAI 语义兼容与可重放元数据

位置：`src/observability/tracing.py:149-169,221-226`、`semantic.py`。

目前 OpenInference 类型与 `llm.model_name/provider` 已有，`gen_ai.*` 主要是 usage。
对照上游当前规范，operation、provider、request/response model、tool/agent 等映射仍不完整。
模型 runtime 的 invoke 路径有输出采集，完整输入/调用参数的统一采集与版本指纹仍需补齐。

GenAI 规范仍处于 **Development**，2026 年已迁到独立仓库；应固定规范版本、
维护 OpenInference + OTel 的兼容映射及 exporter 契约测试。
正文关闭时保存安全的 Prompt/参数指纹；正文打开时遵守已有采集和脱敏开关。
`Observation.outcome()` 当前将用户取消设为 OTel ERROR，需明确它与业务错误率口径的关系。

### G07 · P1：观测链路自身可靠性和监控

位置：`deploy/observability/otel-collector/config.yaml`、`prometheus/prometheus.yml`、
`src/observability/bootstrap.py:141-153`。

Collector 已有 memory_limiter/batch，应用使用异步导出与 fail-open，这些做法正确。
仍缺少显式持久发送队列/WAL、队列/导出失败监控、Collector 内部指标抓取与相应告警。
当前 Prometheus 只抓取 Collector 暴露的应用指标端口 9464。

建议：有界队列、重试、file_storage 持久化及容量规划，抓取自监控，
验证 Tempo/Phoenix/Loki 故障与 Collector 重启后的丢失率和恢复行为。
SDK 当前是 head sampling；默认采样率 1.0。降低采样率前先设计错误/慢请求保留策略；
tail sampling 无法挽回 SDK 已丢弃的 spans，且长评测/文档任务需要考虑决策窗口。

### G08 · P1：从阈值告警完善为 SLI/SLO 和基础设施观测

已有 15 条业务告警及比例口径回归，但 chat 10% 错误率、P95 8s 等固定阈值尚不等于
完整 SLO/error budget。未在当前编排查到 PG/Redis/主机/容器指标 exporter，
告警也缺少应用指标 absent、Collector 不可用、所需 Worker 角色失联等覆盖。

建议按 general/KB/authoring/export 分别定义有效请求、成功、用户取消、业务降级、
超时和延迟门槛；引入多窗口 burn-rate 和低流量合成探针。
补充连接池等待/耗尽、PG 锁、Redis 内存/延迟、磁盘空间、进程资源及遥测丢弃。
部署使用本地 GPU 时再接入推理服务/GPU 指标。
`HdbStaleAuthorityWrites` 监控的是**被拒绝的**陈旧写入，应与真正提交成功的非法写入区分。

### G09 · P1：真实质量和业务容量进入自动验证

位置：`.github/workflows/ci.yml:43-51,71-74`、`frontend/e2e/reliability.spec.ts`、
`wiki/23-reliability-improvement-plan.md:83-101,163-200`。

CI 已有后端/PG/Redis、前端单测/构建及告警规则。当前浏览器命令只执行 `--list`；
质量侧只校验 QA 数据集格式。已记录的真实浏览器和文件验收有价值，但不是每次发布自动执行。

建议增加隔离 staging 的 nightly/release 工作流，运行真实浏览器、真实文件和固定质量集，
保留 trace/截图/模型与数据指纹。常规提交继续运行无需外部模型的契约测试。

正式容量：预热 5min，聊天/解析/文档生成/导出混合 30min × 3，
2 倍到达率 5min 后回落，以及 24h 长稳态；到达率、比例和验收 SLA 由目标部署冻结。
已有 44 请求/0.2 req/s 的短时冒烟不能证明饱和容量，且其中没有文档生成。
原始报告的 chat E2E P95 为 **10.14s**；overall P95 **6.05s** 混合了不同业务，
应按 workload/phase 解读，不直接当作聊天延迟。

### G10 · P1/P2：检索调参与统一证据定位

位置：`ragflow_backend.py:648-693,1245-1390`、
`src/pipelines/document_rag/schemas.py:60-92`。

- 适配请求显式传入 similarity/vector weight/top_k/metadata；reranker、GraphRAG、
  page-index 等上游能力尚未在这一路径显式配置。RAGFlow 实例/版本的隐含默认需通过契约验证确认。
- 当前远端候选 top_k 与最终 top_k 共用，在共享物理 dataset + 本地 ACL/文件过滤时，
  可能出现外部候选挤占、过滤后召回不足。需要限定来源后召回或分开候选预算与最终预算。
- `record_rag_zero_hit()` 在远端 raw chunks 为空时记录；本地过滤后证据为空仅日志报告。
  应分别观测 remote hit、authorized hit、selected evidence 和 zero-result reason。
- 普通文档检索映射为基础 Evidence；未看到这一路径映射 chunk 顶层页码/位置/图像字段。
  已有 EvidenceEnvelope/locator/quote_span 可作为跨源统一定位契约。

建议以固定硬件问题集做 top_k、候选预算、阈值、rerank 和路由回退 A/B；
记录 Recall@k/MRR/nDCG、授权后召回、答案正确率、成本和延迟。
表格已经有专门 recall/MRR 基准，后续推广到文档/电路/跨源链路。

### G11 · P2：硬件领域质量与多模态能力

确定性电路连通图、Wiki 和来源版本治理已经存在。进一步价值在于：

- BOM 料号 ↔ Datasheet 型号/引脚 ↔ EDF 位号/网络 ↔ 测试记录/需求的受控实体关联。
- 页码/bbox/图片裁片和表格单元格能点击回溯原文；原理图图像与网表事实的一致性。
- 单位、量纲、容差、工作条件、典型/最大值、绝对最大额定值的确定性校验。
- Wiki/图/索引在来源删除、版本升级、权限撤销后的失效/重建与一致性验证。
- 业务问题上的 claim 事实支持、缺证据拒答、冲突披露和任务完成率。

当前 `hardware_metrics.py` 的 QA 规则是禁词与证据类型审计，
`runner.py:394-411` 的引用验证是词法诊断。语义事实支持由裁判/人工评估，
不能将编号有效或证据类型齐全解释成工程结论已经被证明。

## 5. 优先级与验收产物

| 顺序 | 工作 | 完成标准 |
|---|---|---|
| 1 | G01 门禁可信度 | 无有效评分、评分不足、非有限值、critical 缺失不能放行；明确 N/A 分母与退出码 |
| 2 | G02 多副本评测任务 | 跨副本读取不改变活任务；共享 owner/lease/fencing；接管和报告唯一 |
| 3 | G03 实验可比性 | 数据内容/裁判规则变化被识别；业务模型、Prompt、检索与来源版本可追溯 |
| 4 | G04 + G09 质量闭环 | 一个生产 bad case 可完整流转为复核、固定样本、实验和发布 gate |
| 5 | G05–G08 生产观测 | 成本可归因、遥测故障可观测、SLI/SLO 按业务区分、规则和通知端实测 |
| 6 | G09 正式容量 | 四类混合、真实过载、恢复与 24h 报告；一致性条件全部通过 |
| 7 | G10–G11 领域增强 | 固定基准证明确有质量收益，并报告成本/延迟、原文定位与版本一致性 |

建议先完善质量与验证基础，再扩展复杂检索策略。已有 Phoenix 和电路图/Wiki 可以直接复用。

## 6. 建议补充的测试矩阵

| 测试 | 关键场景与判据 |
|---|---|
| 评测门禁 | 见 G01；评分异常与业务低分分开，发布结果不可含糊 |
| 评测多副本 | 见 G02；双 API 同时 start/pause/resume/读状态，旧 owner 无权提交 |
| 实验复现 | 同 ID 不同数据内容、模型/Prompt/检索/来源版本；可比性和实验变量一致 |
| OTel 端到端 | API → PG durable turn → Worker → tool → LLM → Collector → Tempo/Phoenix；日志关联和采样连续性 |
| 遥测故障 | 后端 503、Collector SIGKILL/重启、队列满、磁盘满；业务延迟增量、丢失与恢复可计量 |
| 流式 usage/成本 | 末尾 usage、增量/累计 usage、取消、429 重试、cache/reasoning、缺失价格；无重复计费 |
| 检索隔离与召回 | 跨部门高分干扰、元数据缺失、严格来源筛选、fallback；权限不放宽，授权后 recall 可测 |
| 硬件金标 | 料号近似、版本冲突、单位转换、器件额定值/工况、网表别名、多跳、无证据、缺失图像 |
| 真实浏览器/文件 | 持续断线、运行时撤权、真实 PDF/DOCX/XLSX、长多轮修订、放行和五类产物；按当前已有 3 个浏览器用例扩展 |
| 容量/恢复 | 四类业务、长稳态、PG/Redis/存储故障、重复消费、部署升级、恢复演练；single owner/终态/产物唯一 |

建议将质量金标扩展为分层的 200–500 个业务确认样本，关键类别至少有独立结果，
保留盲测集；重复运行报告方差/置信区间，校准 LLM 裁判与领域专家一致性。
数量是规划建议，最终按领域覆盖与置信度确定。

当前数据集实际数量：硬件 QA **25**、AAA hard **47**、附件修订 **50**、文档生成 **21**。
附件真实验收当前记录为 6 个用例，不能解释为整个 50 例全部完成。
denied QA 样本当前通过 access-check-only 路径生成拒绝快照；真实入口的负向授权
应另外通过 API/工具集成测试和运行中撤权测试验证。

测试卫生补充：`tests/evaluation/test_gates.py:99-136` 的两个函数位于 `__main__`
块内，pytest 不会收集；`test_optimizations.py` 已覆盖部分相关逻辑，仍应整理测试位置。

## 7. 本轮实际验证与证据范围

本轮执行：

```bash
uv run --no-sync python -m pytest -q \
  tests/test_observability_privacy.py tests/test_reliability_regressions.py \
  tests/test_backup_restore.py tests/test_checkpoint_attempt_backends.py \
  tests/test_checkpoint_initialization_failure.py tests/test_http_events.py \
  tests/test_mixed_load.py tests/evaluation --disable-warnings -ra
```

结果：**302 passed、5 skipped、24 subtests passed**，45.64 秒。
5 个跳过项来自未配置 `HDB_TEST_POSTGRES_DSN` 的 PG checkpoint 验证。
G01/G02/G03 另以真实 Python 函数、合成输入和临时目录复现，未修改业务实现。

核对了既有报告 `storage/evaluations/reliability-live-20261008-6/backend-tests.xml`：
XML 记载 3157 个测试/子测试条目、0 failures、0 errors、7 skipped，
与 [23](23-reliability-improvement-plan.md) 的 3107 passed + 43 subtests 记录一致。
全量、真实模型/浏览器/双副本/故障结果属于既有执行记录，本轮未重新执行。
当前测试文件实际为 342 个 `test_*.py`，Wiki 18 的规模数字已有漂移。

## 8. 外部资料

查阅日期：2026-10-08。产品能力以官方文档为依据，部署版本/产品档位需分别确认。

1. [OpenTelemetry GenAI conventions](https://github.com/open-telemetry/semantic-conventions-genai)：当前 GenAI Span/Metric 约定，状态仍为 Development。
2. [OTel Collector resiliency](https://opentelemetry.io/docs/collector/resiliency/)：sending queue、重试与 file_storage/WAL。
3. [Google SRE：Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/)：多窗口 burn-rate、低流量处理。
4. [Phoenix](https://arize.com/docs/phoenix)、[datasets](https://arize.com/docs/phoenix/datasets-and-experiments/concepts-datasets)：trace、annotations、版本化数据集、experiments、Prompt。
5. [Langfuse observability](https://langfuse.com/docs/observability/overview)、[Prompt version control](https://langfuse.com/docs/prompt-management/features/prompt-version-control)：成本、质量、Prompt 与实验关联。
6. [LangSmith observability](https://www.langchain.com/langsmith/observability)、[evaluation](https://www.langchain.com/langsmith/evaluation)：online/offline eval、人工反馈、模型/Prompt 比较。
7. [RAGFlow retrieval test](https://ragflow.io/docs/run_retrieval_test)、[2026-08 RAGFlow 0.27](https://ragflow.io/blog/ragflow-0.27-knowledge-compilation-and-agentic-retrieval)：混合/rerank、知识编译、Agentic Retrieval。
8. [Dify Knowledge Retrieval](https://docs.dify.ai/en/self-host/use-dify/nodes/knowledge-retrieval)：元数据过滤、rerank、多模态检索与引用。
9. [GraphRAG query overview](https://microsoft.github.io/graphrag/query/overview/)：local/global/DRIFT 查询。
10. [Docling document model](https://docling-project.github.io/docling/reference/docling_document/)：页面、表格、图像与 bbox/provenance。
11. [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)：持久状态、幂等副作用与恢复。
