# benchmarks/ — 表格问答基准评测

本目录是 **Excel 表格问答（SQL 结构化查询路径）的基准评测**, 与 RAGAS 评估体系
(`evaluation/` + `src/evaluation/`) **相互独立、互不引用**。

| 目录 | 体系 | 数据集 | 用途 |
|---|---|---|---|
| `benchmarks/spreadsheet/` | 自建三层基准（解析保真/检索召回/端到端 A/B） | `golden_qa.json` 30 题 5 类 | 表格问答优化的回归门禁与前后对比 |
| `evaluation/` | RAGAS（LLM 评审 + 硬件领域规则） | `evaluation/datasets/hardware_qa_v1.jsonl` 25 题 | 通用 RAG 质量评估（文档/电路） |

## 使用

```bash
# 检索层/解析层评测(含冻结基线对比)
.venv/bin/python benchmarks/spreadsheet/run_eval.py

# 端到端 A/B(arm a=禁SQL, arm b=完整工具; 需要模型 API 配额)
.venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --arm a --workers 2
.venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --arm b --workers 2
.venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --report

# 18 条参考 SQL 回归
.venv/bin/python benchmarks/spreadsheet/verify_sql_path.py
```

结果写入 `storage/eval/spreadsheet/`(gitignored); 冻结基线: `baseline_report.json`。
