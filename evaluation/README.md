# Hardware DataBase 评估数据集

`datasets/hardware_qa_v1.jsonl` 是首版 25 条硬件问答评估基线，来源于 `docs/joint_retrieval_test_cases.md` 中已核实的 ADAS 案例，并加入同义问法、噪声、权限隔离和缺失证据变体。

每行是一个独立 JSON 对象。新增样本时复制 `datasets/template.jsonl`，使用稳定且唯一的 `hw-v1-*` ID，并确保参考答案只包含可由指定知识库验证的事实。

主要字段：

- `reference_answer`：用于回答正确性评估。
- `reference_contexts`：用于上下文召回评估。
- `required_evidence_types`：期望实际命中的证据类型。
- `rubric.required_facts`：回答必须覆盖的器件、网络或结论。
- `rubric.forbidden_claims`：回答不得编造或泄露的声明。
- `must_disclose_missing` / `must_disclose_conflicts`：控制领域指标是否适用。
- `critical`：该样本任一适用核心指标低于阈值时使门禁失败。

运行校验：

```powershell
uv run hardware-database-eval validate --dataset evaluation/datasets/hardware_qa_v1.jsonl
```
