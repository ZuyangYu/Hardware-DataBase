# Hardware DataBase 评估数据集

`datasets/hardware_qa_v1.jsonl` 是首版 25 条硬件问答评估基线，来源于 `docs/joint_retrieval_test_cases.md` 中已核实的 ADAS 案例，并加入同义问法、噪声、权限隔离和缺失证据变体。

每行是一个独立 JSON 对象。新增样本时复制 `datasets/template.jsonl`，使用稳定且唯一的 `hw-v1-*` ID，并确保参考答案只包含可由指定知识库验证的事实。

主要字段：

- `reference_answer`：用于回答正确性评估。
- `reference_contexts`：可选的参考证据来源记录，便于人工审计；当前 RAGAS 0.4 `context_recall` 按官方 LLM 语义使用 `reference_answer` 与完整检索上下文评分，不要求该字段。
- `required_evidence_types`：期望实际命中的证据类型。
- `rubric.required_facts`：回答必须覆盖的器件、网络或结论。
- `rubric.forbidden_claims`：回答不得编造或泄露的声明。
- `must_disclose_missing` / `must_disclose_conflicts`：控制领域指标是否适用。
- `critical`：该样本任一适用核心指标低于阈值时使门禁失败。
- `expected_access`：可选值为 `allowed` / `denied`，用于权限隔离样本；缺省为 `allowed`。`denied` 样本只做结构化权限合规检查，不参与普通知识库检索和 RAGAS 上下文评分。

评估页面选择的知识库以服务端返回的 `kb_id` 为准；`kb_name` 只用于展示和一致性校验。数据集中的 `request_context` 不能扩大权限，服务端会把允许样本归一化到所选知识库的真实部门作用域，并丢弃其他知识库的权限声明。

运行校验：

```powershell
uv run hardware-database-eval validate --dataset evaluation/datasets/hardware_qa_v1.jsonl
```

## Streamlit 运行控制

系统管理员可在“RAGAS 评估”页面查看运行阶段、当前样本、完成/总数、成功/失败数和已耗时间。“暂停”和“取消”都是协作式操作：它们会等待正在执行的模型请求结束，并在下一个安全检查点生效。“取消”不会删除已保存的 `snapshot.jsonl`；选择“继续”后，系统会使用原始数据集和筛选条件恢复运行，并跳过已成功的样本。
系统管理员可以手动删除“已完成”“失败”或“已取消”的评估运行；删除会移除该运行目录下的输入副本、快照、状态和报告，且不可恢复。运行中、排队中、暂停中或请求态运行不能删除；运行目录外引用的离线快照和原始数据集不会被删除。

在线运行先采集回答和检索证据，完成后进入采集质检状态；管理员确认后点击“开始评分”才执行 RAGAS。评分依赖、裁判 LLM 和 Embedding 的预检均延后到真正进入评分阶段。离线重评使用已有 `snapshot.jsonl`，创建并启动后直接进入评分，因此仍需先执行 `uv sync --group eval` 并完成评估模型配置。

运行目录位于 `storage/evaluations/<run_id>/`，并始终包含 `run_state.json` 和规范化的 `execution_dataset.jsonl`。在线运行在至少持久化一条采集结果后，才会在该目录生成 `snapshot.jsonl`；离线运行则引用所提供的快照路径，该路径可能位于运行目录之外。新运行会写入 `snapshot.manifest.json`，记录知识库 ID、部门、样本集指纹和快照哈希，不包含密钥。仅在评分完整结束后，系统才会生成 `summary.json`、`results.jsonl`、`summary.csv` 和 `report.html`。这使暂停或取消的运行可以安全恢复，同时避免将不完整结果当作最终报告使用。

系统管理员页面创建评估前应先执行预检。预检会返回数据集总数、知识库匹配数、过滤数、正常/拒绝样本数、样本集指纹、错误和警告；创建与启动阶段还会重新校验知识库绑定、运行输入副本哈希和离线快照归属。

历史对比默认是严格模式，要求 `kb_id`、样本集指纹、指标集合和公开模型配置一致；不一致时接口返回冲突错误。需要查看不同配置的结果时，可明确选择“仅查看对比”，该模式会携带差异警告，不应作为回归基线结论。

评分结果解读：

- `评分任务进度`表示评分工作项已完成/总工作项，不等于有效评分数；请同时查看 `确认评分失败`、各指标的适用样本数和 `metric_failures`。
- `snapshot.jsonl` 中的 `retrieved_contexts` 是完整的原始检索结果；正式评分时 `metadata.ragas_scoring.scored_contexts` 与其一致，按原始顺序完整送入 RAGAS，不去重、不重排、不裁剪。
- 在线采集并评分、离线重评都会在后端执行依赖和配置前置检查。离线重评不会重新检索；如果快照本身缺少证据，必须先补充/重新索引资料，再重新在线采集。

性能与 RAGAS 调用方式：

- 评分阶段按“适用指标集合”批量提交样本，一次批量 RAGAS 执行复用同一组 LLM、Embedding 和指标实例；当前使用 RAGAS 0.4 的 collections 指标异步 `ascore()` 接口，进度回调只负责落盘，不会把任务拆成逐样本的模型初始化。
- 采集阶段按线程复用 `AppPipeline`；同一次采集中完全相同且权限上下文一致的问题会复用成功快照，不跨运行或跨权限范围缓存。
- `EVAL_MAX_WORKERS` 同时作为 RAGAS `RunConfig.max_workers` 和在线采集并发上限。提高它前应确认裁判模型和 Embedding 服务的限流配置。
- RAGAS 的 LLM 指标可能包含多次裁判请求；指标越多、上下文越长，耗时和费用越高。建议先用少量样本和 1～2 个指标做基准，再扩大评估范围。
- 报告中的 `scored_contexts` 是实际送入 RAGAS 各上下文指标的完整原始上下文；`retrieved_contexts` 同时保留该内容用于审计。若上游模型因上下文限制失败，系统记录该指标失败，不改用缩小窗口生成可能失真的分数。
