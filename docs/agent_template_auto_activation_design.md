# Agent 自动拆解·判断·生成模板方案（替代人工 review）

> 状态：**待你审阅后定夺**。本文把“让 Agent 自动处理模板、不需要人工 review”的可行方案、改动清单与安全权衡写清楚，你确认力度后再落代码。

---

## 1. 问题

上传模板后，"新建生成任务"里选不到模板。根因：

- 模板分析常返回 **`requires_human`**（LLM 建议的字段绑定会覆盖模板里**已有内容**的单元格，安全规则判定为破坏性）。
- 前端只在 `ready_for_confirmation` 时显示"确认并启用模板"按钮 → `requires_human` **无任何人工入口**。
- 后端 `confirm_template_analysis` 硬性要求 `ready_for_confirmation`；review/correct 的 service 方法**未暴露成 HTTP 端点**，前端也没接线。
- 结果：模板卡在 `draft` → `list_templates(approved_only=True)` 返回空 → 新建任务无模板可选。

> 数据库实锤：`document_authoring.db` 里 `template_versions` 5 条全 `draft`、`document_schemas` 0 条、`document_work_orders` 0 条。5 个分析的 `reason_codes` 均为 `nonempty_target_not_placeholder` + `destructive_target_ratio`。

## 2. 现状机制（为什么 requires_human）

`analyze_uploaded_template` 流程（`src/document_authoring/service.py`）：

1. `template_suggester.suggest(analysis)` —— LLM 读 `analysis.model_dump_json()`（含每个单元的 `value_kind`、`structural_role_hint`、`value_preview`），输出字段绑定建议。
2. `decide_template_activation(analysis)`（`src/document_authoring/template_activation.py`）纯规则判定：
   - 目标单元 `value_kind != "blank"` 且 `structural_role_hint != "placeholder"`，且**不在** `human_confirmed_target_unit_ids` + `approved_overwrite_unit_ids` → `reject("nonempty_target_not_placeholder")`。
   - 非占位目标占比超 `max_target_ratio=0.20` 或非空覆盖占比超 `max_nonempty_overwrite_ratio=0.0` → `reject("destructive_target_ratio")`。
   - 默认 `max_nonempty_overwrite_ratio = 0.0`：**任何非空覆盖都必须人工批准**。
3. `status = auto_accepted ? ready_for_confirmation : requires_human`。

设计哲学（`template_activation.py` 注释原话）：*"Classify a model proposal without granting the model policy authority."* —— 故意不让模型有覆盖策略权，非空覆盖一律交人工。

## 3. 目标

**不做人工 review**，让 Agent 自动完成"拆解 → 判断 → 生成"，模板上传后直接可供文档生成使用。

## 4. 方案：LLM 显式覆盖判断 + 决策采信

LLM 在建议阶段**已经能看到**每个目标单元的 `value_preview`（现存值）。让它对每个目标单元显式判断"**该单元格现有内容是否为模板为生成预留的默认值/示例，生成时应被替换**"，把这类判断作为"自动批准的覆盖"写入 `analysis.approved_overwrite_unit_ids`，`decide_template_activation` 采信之 → 判定 `auto_accepted` → 模板可激活。

### 4.1 改动清单（文件级）

| 文件 | 改动 |
|---|---|
| `src/document_authoring/template_analysis.py` | `TemplateAnalysisSuggestion` 增加字段 `overwrite_unit_ids: list[str] = Field(default_factory=list)` |
| `src/document_authoring/template_suggester.py` | `_SUGGESTION_FIELDS` 加 `overwrite_unit_ids`；`_SYSTEM_PROMPT` 说明语义；`_parse_suggestion` 解析并校验 **`overwrite_unit_ids ⊆ target_unit_ids`**；`suggest()` 汇总各建议的 `overwrite_unit_ids` 写入 `analysis.approved_overwrite_unit_ids` |
| `src/document_authoring/template_activation.py` | ① `nonempty_target_not_placeholder` 检查：`unit_id in approved_overwrite_unit_ids` 即放行（不再强制 `human_confirmed`）；② `destructive_target_ratio` 的 risky 排除条件从 `human_confirmed_target_unit_ids` 扩为 `approved_overwrite_unit_ids` |
| `src/document_authoring/service.py` | `analyze_uploaded_template` 在 `suggest` 后、`decide` 前已由 suggester 注入 `approved_overwrite_unit_ids`（无需额外代码；若走 `analyze_and_activate_uploaded_template` 则上传即激活） |
| 前端（可选） | 若采用"全自动激活"，`/templates/analyze` 改走 `analyze_and_activate_uploaded_template`，分析成功即 `approved`，界面不再需要"确认并启用"按钮 |

### 4.2 关键判定变化

`decide_template_activation` 中：

```python
# 现状：非空覆盖必须 human_confirmed + approved_overwrite
if (unit.value_kind != "blank" and unit.structural_role_hint != "placeholder"
        and not (human_confirmed and unit_id in analysis.approved_overwrite_unit_ids)):
    reject("nonempty_target_not_placeholder")

# 方案：approved_overwrite_unit_ids（无论来源 human 还是 agent）即放行
if (unit.value_kind != "blank" and unit.structural_role_hint != "placeholder"
        and unit_id not in analysis.approved_overwrite_unit_ids):
    reject("nonempty_target_not_placeholder")
```

`destructive_target_ratio` 的 risky 排除同理，把 `approved_overwrite_unit_ids` 与 `human_confirmed_target_unit_ids` 一并排除。

## 5. 安全权衡（必须明确）

**本质：把"覆盖模板已有内容"的决定权从「人工」移交「LLM」。**

- 风险：LLM 误判，批准覆盖真正的静态内容（固定表头、公司名、公式）。
- 缓解设计：
  1. LLM 看得到 `value_preview`，prompt 明确限定"仅当该单元格是模板为生成预留的默认/示例且应被替换时才批准覆盖"。
  2. 强校验 `overwrite_unit_ids ⊆ target_unit_ids`，且只对 writable 单元生效。
  3. `approved_overwrite_unit_ids` 持久化进 `analysis`（可追溯每次覆盖了什么、谁批的）。
  4. 保留策略开关：默认开启自动覆盖，可一键回退到 `requires_human` 人工模式。

## 6. 两种力度（供你拍板）

- **梯度 1（推荐）**：LLM 显式判断每个目标单元"是否应为生成覆盖的默认值"，只自动批准这一类；LLM 不判为覆盖的单元仍走 `requires_human` 兜底。改动稍大，保留安全阀。
- **梯度 2（全信任）**：信任 LLM 建议的目标单元即可写字段，目标非空也一律自动覆盖，`requires_human` 基本不再出现。最省事，保护最弱。

## 7. 测试计划

- 新增（`tests/test_template_activation.py` 或新文件）：
  - "目标非空 + 在 `approved_overwrite_unit_ids` 中 → `auto_accepted`"。
  - "destructive ratio 超限但覆盖单元已批准 → 放行"。
  - suggester：`overwrite_unit_ids` 解析、`⊆ target_unit_ids` 校验、越界拒绝。
- 现有测试不破坏：`test_template_activation.py` 等均在不设 `approved_overwrite_unit_ids` 的假设下断言，基础约束（非空无批准 → `requires_human`）仍成立，流程保持。
- 跑全量 `uv run python -m pytest` + `uv run ruff check src/ tests/`。

## 8. 待你决策

1. **梯度**：1（LLM 判断可覆盖，其余兜底）还是 2（全信任）？
2. **激活方式**：前端保留"一键确认"（auto_accepted 后点一次确认）还是走 `analyze_and_activate` 全自动激活（上传即 approved）？
3. **策略开关**：是否需要可配置开关（默认开，可回退人工模式）？