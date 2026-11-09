# 2026-10-30 优化项汇报图

本目录是本周优化项的独立 SVG 图表，按主题分开画，便于逐页汇报或插入文档。

## 图表清单

| 文件 | 汇报主题 | 核心数字 |
|---|---|---|
| `01-retrieval-funnel.svg` | 检索链路：候选不再自动占用证据预算 | 候选 1,209 / 精读 66 次 / 正式证据 128；比例 10.6% |
| `02-retrieval-quality.svg` | 逐题检索质量 | 10/10 有引用；平均 12.8 条证据、11.2 个引用、6.6 次精读 |
| `03-acceptance-matrix.svg` | 端到端验收矩阵 | 16/16 项全部通过，0 失败 |
| `04-test-coverage.svg` | 测试覆盖与通过率 | 2,514 项通过；0 失败；相关回归 74 项 |
| `05-frontend-noise.svg` | 聊天区降噪 | 面板 3→0、禁用文案 6→0、代码少 417 行 |
| `06-code-slimming.svg` | 架构瘦身 | git 删除 136,401 行；55 个 API；7 个 task_* 模块 |
| `07-context-governance.svg` | 上下文治理 | 32k 模型窗口从 64k 压回 32,768；10 题 0 异常 |

建议汇报顺序：`01 → 02 → 03 → 04 → 05 → 06 → 07`。先讲检索收益，再讲质量/验收/测试，最后讲前端降噪、架构瘦身和上下文治理。

## 数据来源

- `/tmp/hdb-question-stress-converged.json`：2026-10-30 12:02–12:16，AAA 知识库，10 题，并发 4，10/10 完成。
- `/tmp/hdb-final-acceptance.json`：2026-10-30 12:29 的 16 项端到端验收。
- `/tmp/hdb-full-gap.log`：后端全量 pytest，2,280 passed / 189 skipped / 0 failed，644.64s。
- `/tmp/hdb-fe-converged.log`：前端 Vitest，31 个文件 / 161 tests passed，4.46s。
- `frontend/test-results/acceptance.json`：Playwright E2E，73 expected / 9 skipped / 0 unexpected。
- 本地 targeted pytest：74 passed in 5.95s（2026-10-30）。
- `git show --numstat 1ea0838`、`git show --numstat 1995eab`、`git diff --numstat`：代码增删统计。
- `tests/test_context_budget.py`：模型窗口解析用例（Qwen2.5 32k、DeepSeek、未知模型、关闭）。

## 口径与限制

1. **没有编造 before 数据。** 只有 git diff、前端面板/文案、模型窗口解析存在真实“前后”对照；检索质量、压测、验收、测试覆盖均为优化后实测值。
2. **候选数是累计条目，不是唯一文档数。** `candidate_count` 按每次 `tool_result` 累计，含重复命中；10.6% 是“正式证据 / 候选条目”的聚合比例，不是严格集合差。
3. **压测时延不作为本轮优化指标。** 10 题压测在并发 4、真实模型下运行，端到端 p50 约 266s（含排队），不能与单请求延迟直接比较，因此图中未画时延。
4. **代码统计为 tracked diff，且包含测试/文档。** 当前工作区 20 个新增文件的内容未计入 `+` 行；合计删除 136,401 行仅统计 tracked diff，行数包含测试、文档与迁移文件，不等同于生产代码行数。
5. **E2E 的 73 为 expected（通过），9 为 skipped。** 跳过项是环境/外部依赖条件不满足，不计为失败。E2E 全量结果是 2026-10-28 最近一次运行；10/30 的前端改动由 `hdb-final-acceptance.json` 中的 `frontend_meta_cards_removed` / `frontend_build_marker_updated` 两项覆盖。
6. **上下文压缩为 0 不是异常。** 10 题压测没有触发窗口压缩，说明本轮上下文没有逼近窗口上限；`compaction_event_typed` 验收仍通过。

## 重新生成

```bash
python3 docs/reports/2026-10-30-optimization/generate.py
```

只依赖 Python 标准库；SVG 可直接用浏览器、Inkscape、Illustrator 或 draw.io 打开/编辑。字体优先使用 `Droid Sans Fallback` / `Noto Sans SC`，缺失时回退到系统中文 sans-serif。
