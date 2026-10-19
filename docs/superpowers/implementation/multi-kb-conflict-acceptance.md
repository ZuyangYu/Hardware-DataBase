# R3 固定冲突评测与验收边界

## 固定集与执行范围

固定集：`evaluation/datasets/multi_kb_conflicts_v1.jsonl`，共 22 个合成工程案例、45 条预期声明。
执行器：`scripts/production_hardening/multi_kb_conflict_eval.py`。

每例保存原始声明文本、KB ID、来源角色、审批状态、生效范围/修订和版本，以及手工写定的归一化数值/量纲、抽取数量、事项成员、状态、阻断要求和写作输入。执行时按原文计算来源哈希；受控抽取器生成声明和精确引用跨度，再调用真实文档服务，将冲突事项写入临时 SQLite，并读取持久状态和写作上下文评分。项目库、工单库和产物目录均显式放在临时目录中；不调用模型、检索服务或部署数据库，不修改功能开关。

`expected_claims` 按证据 ID 和字段绑定规范数值/量纲；例如 5V 与 5000mV 均标为 `normalized_value="5"`、`normalized_unit="voltage"`。标签不由被测归一化器生成。报告 schema_version=2 会逐条比较这些值，防止分组、出处和数量均正确却把 5V 错写成 50V；初版报告仍保留在原始运行目录。

这些是编码代理编写的回归标签，全部保留 `review.status=pending`，不能声称为独立人工标注或真实资料相关性验收。

| 覆盖项 | 固定案例 |
|---|---|
| 单位、范围和限值等价 | 5V/5000mV、200uA/0.2mA、9–36V、温度范围 |
| 条件与对象区分 | 输入/输出、温度、CAN/正常工作、器件和电源轨 |
| 生效范围 | 不同修订、不同项目 |
| 关键阻断 | 同库已批准资料矛盾、电流限值矛盾、独立关键字段 |
| 目标事实与参考资料 | 主库已批准事实、缺主库依据、草稿/已批准规范 |
| 多源归组 | 四库、三种值、一个事项、完整出处 |
| 对比与注入 | peer 并列差异、文本伪造批准指令不生效 |
| 抽取能力边界 | 未支持属性不凭空生成声明 |

## 运行与本地门禁

```bash
HDB_ENV_PATH=/dev/null OBS_ENABLED=false MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED=false \
  uv run python scripts/production_hardening/multi_kb_conflict_eval.py \
  --output storage/evaluations/multi-kb-r3-conflicts/manual-resolution.json

HDB_ENV_PATH=/dev/null OBS_ENABLED=false MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED=true \
  uv run python scripts/production_hardening/multi_kb_conflict_eval.py \
  --output storage/evaluations/multi-kb-r3-conflicts/automatic-resolution.json

HDB_ENV_PATH=/dev/null OBS_ENABLED=false \
  uv run python scripts/production_hardening/multi_kb_conflict_eval.py \
  --require-human-review \
  --output storage/evaluations/multi-kb-r3-conflicts/human-review-gate.json
```

最后一条命令在标签待复核时应返回 **3**。自动裁决开关仅作用于评测进程，不修改 `.env` 或运行服务。两个模式的预期写作输入分别保存，关闭自动裁决时主库/参考差异保留人工审阅；开启时只允许真实已批准主库事实自动确定。

返回码：0 为本地回归通过；2 为案例失败或输入无效；3 为启用人工标签门禁后仍缺少人工复核。空数据集、重复案例 ID、未选择来源和无复核人身份的“已复核”标签均拒绝。执行错误保留逐例失败，不跳过后宣称成功。

本地要求 22 例的 45 条声明数值/量纲、抽取数量、出处、事项归组/状态/动作、选中证据和最终写作输入全部匹配；任何差异都会失败。此要求用于回归，不是由真实样本锁定的生产质量阈值。

报告包含数据集原始字节及规范化案例集哈希、执行代码哈希、抽取器/策略版本、实际开关状态、逐例预期与实测、原始证据定位和失败原因。关键阻断漏放按事项成员与字段计数，不能用同一案例的另一个已阻断字段掩盖遗漏。报告中的 `release_ready` 始终为 false，因为本执行器不验证完整发布条件。

## 独立人工复核与后续验收

独立工程人员应对照原文、适用条件、真实审批状态、证据身份和预期动作逐例核对，修正标签后再填写 `review.status=approved` 与真实 `reviewer`。该字段记录外部复核声明；脚本不能认证复核人身份。修改后数据集哈希会变化，需要重新保留两个开关模式的报告。

真实资料相关性/来源覆盖继续使用独立来源绑定标注；已有 `multi_kb_r3_live_anchors.jsonl` 及真实四库报告只能证明锚点召回和冒烟边界，不能替代人工相关性精度。文件/索引变化、撤权、API 状态码、Worker 释放/恢复和正式导出资格由关联集成测试与真实部署演练验证，不计入本固定集的覆盖结论。

隔离 PG 迁移及含审计历史的备份恢复已另行完成，见 `2026-10-19-multi-kb-r3-migration-recovery.md`。仍需保留独立证据：真实样本的人工相关性/冲突标注及质量阈值签署、持续负载与部署 SLO、实际 API/Worker 切换和生产回滚、正式审批系统接入。这些工作未因本地回归通过而自动完成。
