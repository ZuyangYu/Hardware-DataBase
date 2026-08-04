# 通用电路事实检索、文档预览反馈与 ICD 评估实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** 让模板驱动的文档生成能按字段语义从项目资料中检索权威电路事实，生成后提供只读预览与反馈门禁，并以实际 ICD 模板、EDF 和人工 ICD 进行可复现的差异评估。

**Architecture:** 保持解析器、CircuitStore、RetrieverRegistry 和文档编排的分层。电路查询仍由现有 CircuitIndexService/CircuitQueryTool 提供；在 EvidenceMapper 中保留完整、规范化且可追溯的管脚事实；由模板字段的通用语义补足检索能力；候选制品默认停留在审核态，预览和反馈为独立的只读/审阅操作。比较器只读取两份工作簿，输出结构化差异报告，不依赖项目名称、接插件位号或固定管脚数。

**Tech Stack:** Python 3.11、Pydantic、pytest、Streamlit、ZIP/XML（OOXML）、现有 EDF/CircuitStore/RAGFlow 适配层。

## 全局约束

- 不以 X1900、X1902、PGND、项目名称或人工 ICD 的固定行列作为业务规则。
- EDF/CircuitStore 的原始管脚事实是证据来源；展示层可去除 OrCAD 的 & 前缀，但必须保留原始名称和来源元数据。
- 没有网络的管脚必须保留为 NC（源文件未声明网络连接），不得因空网络被静默丢弃。
- PGND、安装脚、屏蔽脚等是否进入接口文档，应由模板字段和人工审核决定，不能在检索层排除。
- 自动生成只能创建候选制品，只有显式批准才允许发布。
- 预览解析必须是有上限、只读、容错的；不得在页面轮询时重复解析大文件。
- 新增代码先写失败测试；只提交本任务产生的文件，不覆盖工作区已有未提交改动。

## 实施步骤

### 任务 1：保留完整且可追溯的连接器管脚证据

**文件：**
- 修改：src/circuit/evidence_mapper.py
- 新增：tests/test_circuit_evidence_mapper.py
- 视需要修改：tests/test_circuit_index_service.py

**步骤：**

1. 为 pin_mapping 证据写失败测试：
   - 输入含 &1 -> CAN_H、&2 -> None、&3 -> PGND 的实例详情；
   - 断言正文使用规范化管脚号、未联网管脚的 NC 说明、PGND 未被排除；
   - 断言 metadata 保留 raw_pin_name、pin_name、net_name、连接状态和来源信息。
2. 运行 uv run python -m pytest -q tests/test_circuit_evidence_mapper.py，确认失败原因是当前映射器过滤了空网络且未保留结构化映射。
3. 在 CircuitEvidenceMapper 中建立统一的管脚规范化和呈现逻辑：
   - 仅移除语法性 & 前缀；
   - 逐个保留全部已知 pin；
   - 对无网络的项产生明确 NC 文案；
   - 追加受限大小的 pin_mappings 元数据，原始数据仍保留。
4. 重跑任务测试和相邻 CircuitStore 测试。
5. 创建原子提交：fix: preserve complete circuit pin evidence。

### 任务 2：按模板字段语义通用地启用电路检索

**文件：**
- 新增：src/document_authoring/circuit_capabilities.py
- 修改：src/document_authoring/harness/graph.py
- 修改：src/core/app_pipeline.py
- 修改：tests/test_knowledge_base_document_work_orders.py
- 修改：tests/test_project_retriever_dispatch.py
- 视需要修改：tests/test_retriever_registry.py
- 新增：tests/test_circuit_capabilities.py

**步骤：**

1. 写失败测试覆盖：
   - Pin Definition、引脚定义、连接器网络字段自动带 relationship_lookup；
   - 板端型号、器件料号字段自动带 entity_lookup；
   - 无电路语义字段不添加任何电路能力；
   - 显式能力与推断能力取并集，未知能力仍会被过滤；
   - KB 与项目范围下，冻结源/指定文档过滤后电路 evidence 能到达 registry。
2. 运行新增测试，确认当前图构建不会从字段语义推断 capability，或缺少相应回归保护。
3. 实现纯函数 enrich_circuit_capabilities：
   - 从 label、description、query_terms 归一化提取通用中英文关键词；
   - 对管脚、引脚、接插件、网络、连接等关系语义加入 relationship_lookup；
   - 对型号、料号、位号、器件等实体语义加入 entity_lookup；
   - 不引入项目/器件/管脚值特例。
4. 在 field requirement 创建点调用该函数后，再走现有允许能力过滤。
5. 审核现有 CircuitQueryTool 注册：
   - 复用已有 CircuitIndexService；
   - 确保知识库路径受 frozen source set 限制；
   - 确保项目路径只返回当前文档；
   - 为空服务、安全异常或无证据时维持默认检索退化行为。
6. 重跑任务测试及：
   uv run python -m pytest -q tests/test_knowledge_base_document_work_orders.py tests/test_project_retriever_dispatch.py tests/test_retriever_registry.py
7. 创建原子提交：feat: retrieve circuit facts from generic field semantics。

### 任务 3：候选制品预览、反馈与发布门禁

**文件：**
- 新增：src/document_authoring/artifact_preview.py
- 修改：src/document_authoring/models.py
- 修改：src/document_authoring/service.py
- 修改：src/core/app_pipeline.py
- 修改：src/ui/document_generation_page.py
- 新增：tests/test_artifact_preview.py
- 新增：tests/test_document_generation_feedback.py
- 修改：tests/test_app_pipeline_document_authoring.py
- 视需要修改：tests/test_document_generation_ui.py

**步骤：**

1. 写失败测试覆盖：
   - xlsx 预览最多返回 3 个 sheet、每 sheet 最多 50 行和 12 列，超限标记 truncated；
   - docx 返回有界段落/表格文本；
   - 损坏或不支持的制品返回安全 warning；
   - 反馈事件包含当前 artifact 的文件哈希、版本快照与操作者；
   - 自动生成返回 review_candidate，不会调用审批/发布；
   - 只有显式 approve_document_artifact 才进入已发布状态。
2. 运行上述测试，确认当前自动生成会隐式批准且缺少预览/反馈服务。
3. 实现只读 preview_artifact：
   - 通过 ZIP/XML 读取 xlsx/xlsm 的共享字符串、工作表和有限单元格；
   - 读取 docx 的段落和表格；
   - 不执行宏、不写制品、无格式内容也可安全返回。
4. 将 feedback 加入可审计人工事件；新增服务和 pipeline 包装方法，非空反馈可记录但不能批准或发布。
5. 变更两条自动生成路径，使其停止在 waiting_human_approval/review_candidate，并在返回体中明确候选状态和下一步。
6. 在候选审核 UI 中增加“加载预览”和“提交反馈”动作；预览按需获取，批准按钮仍是唯一发布入口。
7. 运行任务测试、服务测试和 UI 测试。
8. 创建原子提交：feat: add artifact preview feedback and approval gate。

### 任务 4：建立非项目特化的 ICD 端到端回归

**文件：**
- 新增：tests/test_icd_template_regression.py
- 新增：tests/test_generic_circuit_authoring_flow.py
- 视需要修改：tests/conftest.py

**步骤：**

1. 使用测试夹具构建两套不同位号、不同 pin 数与不同网络的 circuit 数据：
   - 第一套含 & 前缀、空网络和 PGND；
   - 第二套不含任何 TCN2、X1900、X1902 或真实项目字面量。
2. 写失败测试：
   - 以模板字段 Pin Definition 形成 work unit 时，retriever 会请求关系事实；
   - 生成器通过 evidence metadata 产生逐 pin 行，而不是从 PDF 打乱文本或硬编码表行产生；
   - 两套数据均保留其完整 pin 集合、NC、型号/实体事实。
3. 用最小可控 writer 仅消费 WriterRequest.evidence 进行断言，避免把真实模型随机性当作回归判据。
4. 运行端到端测试，修复必要的适配问题，不为真实项目添加条件分支。
5. 创建原子提交：test: cover generic ICD circuit authoring flow。

### 任务 5：比较实际 ICD 并输出可复现评估

**文件：**
- 新增：scripts/compare_icd_artifacts.py
- 新增：tests/test_icd_artifact_comparison.py
- 新增：docs/evaluations/ 下本次生成的比较报告（仅在实跑后）
- 新增：output/ 下本次实际生成的 xlsx（仅在实跑后；不覆盖已有产物）

**步骤：**

1. 写失败测试验证比较器：
   - 从通用列头同义词识别 pin 编号和定义列；
   - 匹配、定义不一致、仅人工存在、仅生成存在分别输出；
   - 计算 pin 精确匹配率、覆盖率与 warning；
   - 没有可识别表头时以 warning 结束，而不是猜测或失败。
2. 实现只读 CLI：
   uv run python scripts/compare_icd_artifacts.py --reference … --generated … --output …
   使用项目现有 xlsx parser，生成稳定 JSON。
3. 运行相关测试：
   uv run python -m pytest -q tests/test_icd_artifact_comparison.py tests/test_icd_template_regression.py tests/test_generic_circuit_authoring_flow.py
4. 使用实际 825504380 EDF、ICD 模板和需求资料生成独立 xlsx；同时记录所用来源、模板字段和候选审核状态。若当前本地模型/索引服务不可用，采用同一正式 writer 输入的可控离线写入器，并在报告中明确限制。
5. 与人工 ICD 执行比较器，人工核验每个差异：
   - 将管脚事实错误、缺失、展示归一化差异分类；
   - 将功能描述、裁剪标记、组织模板通用文字等不可由 EDF 单独推出的项目资料覆盖问题单列；
   - 不用相似度掩盖未验证事实。
6. 将生成文件、JSON 差异、中文结论保存到不覆盖现有内容的新路径；输出准确率、覆盖率、主要差距和下一步建议。
7. 创建原子提交：test: add reproducible ICD artifact evaluation。

## 最终验证

1. 运行任务涉及的完整 pytest 集合，并保留命令与结果。
2. 运行格式/空白检查：git diff --check。
3. 检查工作树和 staged diff，确认未夹带用户原有改动。
4. 对变更进行独立代码审阅，重点检查：
   - 是否存在位号、网络或项目名特化；
   - 是否有隐式审批/发布旁路；
   - 预览是否可能越界读取或泄露完整制品；
   - evidence 是否可追溯至具体来源。
5. 仅在测试、实际 ICD 比较和审阅均有证据后，报告已完成的能力与仍存在的资料覆盖差距。

