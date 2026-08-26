# Hardware DataBase 评估数据集

`datasets/hardware_qa_v1.jsonl` 是首版 25 条硬件问答评估基线，来源于 `docs/joint_retrieval_test_cases.md` 中已核实的 ADAS 案例，并加入同义问法、噪声、权限隔离和缺失证据变体。

每行是一个独立 JSON 对象。新增样本时复制 `datasets/template.jsonl`，使用稳定且唯一的 `hw-v1-*` ID，并确保参考答案只包含可由指定知识库验证的事实。

主要字段：

- `reference_answer`：用于回答正确性评估。
- `reference_contexts`：记录参考证据的来源说明，并用于判断上下文召回指标是否适用。当前 RAGAS `context_recall` 实际使用 `reference_answer` 与检索上下文评分，因此这里不能替代可核验的参考答案。
- `required_evidence_types`：期望实际命中的证据类型。
- `rubric.required_facts`：回答必须覆盖的器件、网络或结论。
- `rubric.forbidden_claims`：回答不得编造或泄露的声明。
- `must_disclose_missing` / `must_disclose_conflicts`：控制领域指标是否适用。
- `critical`：该样本任一适用核心指标低于阈值时使门禁失败。

运行校验：

```powershell
uv run hardware-database-eval validate --dataset evaluation/datasets/hardware_qa_v1.jsonl
```

## Streamlit 运行控制

系统管理员可在“RAGAS 评估”页面查看运行阶段、当前样本、完成/总数、成功/失败数和已耗时间。“暂停”和“取消”都是协作式操作：它们会等待正在执行的模型请求结束，并在下一个安全检查点生效。“取消”不会删除已保存的 `snapshot.jsonl`；选择“继续”后，系统会使用原始数据集和筛选条件恢复运行，并跳过已成功的样本。
系统管理员可以手动删除“失败”或“已取消”的评估运行；删除会移除该运行目录下的快照、状态和报告，且不可恢复。运行中、排队中或已完成的运行不能删除。

在线运行可取消勾选“执行 RAGAS 评分”。此时系统只采集回答和检索证据，不需要安装 `eval` 依赖，也不需要配置裁判 LLM 或 Embedding。离线评分和勾选评分的在线运行仍需先执行 `uv sync --group eval`，并完成评估模型配置。

运行目录位于 `storage/evaluations/<run_id>/`，并始终包含 `run_state.json`。在线运行在至少持久化一条采集结果后，才会在该目录生成 `snapshot.jsonl`；离线运行则引用所提供的快照路径，该路径可能位于运行目录之外。仅在评分完整结束后，系统才会生成 `summary.json`、`results.jsonl`、`summary.csv` 和 `report.html`。这使暂停或取消的运行可以安全恢复，同时避免将不完整结果当作最终报告使用。

评分结果解读：

- `评分任务进度`表示评分工作项已完成/总工作项，不等于有效评分数；请同时查看 `确认评分失败`、各指标的适用样本数和 `metric_failures`。
- `snapshot.jsonl` 中的 `retrieved_contexts` 是完整的原始检索结果；报告结果的 `metadata.ragas_scoring.scored_contexts` 是实际送入 RAGAS 的上下文窗口。两者数量不同表示发生了去重、相关性排序或预算裁剪，不应混为一谈。
- 在线采集并评分、离线重评都会在后端执行依赖和配置前置检查。离线重评不会重新检索；如果快照本身缺少证据，必须先补充/重新索引资料，再重新在线采集。
