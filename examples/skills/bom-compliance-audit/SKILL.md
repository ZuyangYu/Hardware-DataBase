---
name: bom-compliance-audit
description: 审查硬件 BOM 的位号、器件型号、数量和电压范围，输出规范化 BOM、校验报告与摘要。
metadata:
  version: 1.0.0
  intent_examples:
    - 审查 BOM 完整性
    - 检查物料表位号、数量和电压
    - 找出重复位号和缺失器件
  input_kinds: [json, csv]
  output_kinds: [json, md]
  interaction_modes: [self_contained, needs_user_input]
  side_effects: []
  risk: low
---
# BOM Compliance Audit

本 Skill 对硬件 BOM 做确定性的结构检查，不访问网络，不修改原始输入。

## 输入

优先读取参数 `bom_rows`（对象数组）；如果没有，则读取输入 Artifact 中第一个 `.csv` 或 `.json` 文件。

每一行建议包含：

- `reference`：位号，例如 `R1`
- `part_number`：器件型号
- `quantity`：数量
- `voltage_v`、`min_voltage_v`、`max_voltage_v`：可选电压信息

## 输出

- 正常：`normalized-bom.json`、`summary.md`、`result.json`
- 校验失败：`validation-report.json`、`normalized-bom.json`、`result.json`
- 缺少输入：`result.json`，`result_status=needs_user_input`

## 边界

该 Skill 只报告结构性问题，不替代硬件工程师的电气设计评审；所有产物均为不可信结果，不自动进入知识库证据链。
