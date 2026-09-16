# 基线报告（baseline-report）

> 状态：模板 + 确定性回归基线。容量/延迟业务基线必须在目标硬件上按 §4.3 采集后填写，禁止用未经运行的估计值冒充。
> 每次运行以 `baseline_id` 归档到本目录 `baselines/`，质量/容量结论必须引用对应 `baseline_id`。

## 1. 运行标识

| 字段 | 值 |
|---|---|
| baseline_id | `20260916T114508Z-d7898b8-g1-clean-baseline`（干净提交基线，G1；此前 `20260915T112403Z-01c807e-p0a-p2-final`，实施后复核 `20260915T102004Z-01c807e-p0a-01c807e-verified`；初始基线 `20260915T084230Z-01c807e-p0-baseline-01c807e`） |
| git_commit | `01c807e`（PR #49 合并后的 `feature/fix-module-matching`） |
| 采集时间（UTC） | 2026-09-15T10:20:04Z（本机确定性回归） |
| 模型配置摘要（provider/model/agent 参数） | 未调用模型；确定性 pytest 集 |
| RAGFlow 版本 / 数据库版本 | 未连接下游；SQLite（本机） |
| 部署形态（API 进程数 / Worker 角色与进程数 / 主机规格） | 单进程开发机；未做容量采集 |
| 测试命令 | `uv run python scripts/production_hardening/regression_runner.py --label p0a-01c807e-verified` |

## 2. 确定性回归结果（必须全绿）

| 套件 | 结果 | 耗时 |
|---|---|---|
| 固定 pytest 集（见 `regression-set.md` §1） | 全绿（回归子集 100 passed；全量 2657 passed / 0 failed（含 PostgreSQL 集成；既有失败已修复/清理，见 §6）） | 31.3s（子集） |
| 数据集 hash（sha256，记录于基线 JSON） | 见 baselines/20260916T114508Z-d7898b8-g1-clean-baseline.json |  |
| 三次连续运行波动（P0-001） | run1 35.53s / run2 35.31s / run3 36.47s，极差 1.16s（约 3.3%），结果全绿 | `baselines/*-p0a-fluctuation-run{1,2,3}.json` |

## 3. 质量基线（Phase 0）

| 指标 | 基线 B | 容差 | 备注 |
|---|---|---|---|
| 附件编辑端到端（文件+审计链判定） | **50/50 = 100%**（gate≥90% 通过） | ≥ 基线 −1pp | 2026-09-16 当前提交实测，产物 `attachment-e2e-baseline-20260916.json`（e2e runner 已适配两级角色模型） |
| RAGAS（ADAS_new 小样本，n=3） | `faithfulness` **0.905**、`answer_correctness` **0.538**、`evidence_consistency` **1.0**（逐样本：电路 1.0/0.506、文档 0.944/0.504、联合 0.769/0.603），`judge_governance.assembled=true` | 全量门禁需 ≥ 本基线 −1pp | 2026-09-16 实测，产物 `ragas-small-sample-adas-new-20260916.json`；前置修复见 `adas-new-eval-prep-20260916.json`（5 远端文档补本地部门映射 + EDF 入 ADAS_new 电路索引）。全量 25×6 判分约 3.6 分钟/指标项，需专门预算窗口，暂未执行 |
| Document Eval（Template/Numeric） | 未采集 | — | 需 document_generation 运行快照；确定性指标脚本就绪 |
| Agent routing / Tool 指标 | 未采集 | — | 需上述 KB 内容与模型预算 |

## 4. 延迟与吞吐（`load_probe.py` 固定到达率，本机开发环境实测）

> 口径：单 API 进程 + 单机 SQLite；`__general__` 通用问答（无 RAGFlow 检索）；模型 `ark-code-latest`（火山方舟）。
> 这些数字是**本机开发基线**，用于回归比较；绝对产品 SLO 与目标硬件容量仍需在部署环境登记。

| 口径 | P50 | P95 | P99 | 错误率 | 完成率 | 归档 |
|---|---|---|---|---|---|---|
| `/health` 5 rps / 20s（75 探测） | 0.108s | 0.133s | 0.162s | 0% | 100% | `load-health-01c807e-20260915.json` |
| 聊天端到端（同会话，未开启串行，4 完成） | 22.1s | 27.3s | 27.3s | 0% | 100% | `load-turn-01c807e-20260915.json` |
| 聊天端到端（同会话串行，5 完成，含排队） | 26.8s | 34.6s | 34.6s | 0% | 100% | `load-turn-serialized-01c807e-20260915.json` |
| 跨会话端到端（4 会话、串行开启、10 完成） | 39.8s | 92.3s | 92.3s | 0% | 100% | `load-turn-cross-session-01c807e-20260915.json` |
| 首 token（2 会话、串行开启、5 完成） | 21.4s | 31.1s | 31.1s | 0%（无 TTFT 缺失） | 100% | `load-turn-ttft-01c807e-20260915.json` |
| Heavy Job queue wait | 未采集（需文档/导出负载与审查环境） | — | — | — | — | — |

采集要求：目标流量预热 5 分钟、测量 ≥30 分钟、重复 3 次；再以 2 倍到达率施压 5 分钟后恢复；分别记录提交成功率、接受后完成率、拒绝率与队列增长趋势。本轮为开发机短时探测，满足“可重复 + 有归档”，不满足生产容量验收时长。

## 5. 容量拐点与降级

| 项 | 结论 |
|---|---|
| LLM 槽位拐点 |  |
| RAG 槽位拐点 |  |
| Worker/DB 拐点 |  |
| 队列与过载行为 |  |
| 降级/拒绝策略 |  |

## 6. 已知限制

- `evaluation/datasets/hardware_qa_v1.jsonl` 已于 2026-09-15 从既有上传副本还原（25 条，`validate` 通过），README 引用不再悬空；质量基线仍待模型预算跑分；
- 回归集当前 143 条样本（25+47+50+21），未达 §9.4 的 200 条目标，缺口仍需业务标注（不得用模型生成答案填充参考事实）；
- 容量数字与 SLO 绝对值必须在目标部署环境登记，本模板不预设秒数；
- 基线 `01c807e` 上原有 6 个失败（附件权限 4 例为旧逐库授权模型残留测试，2 例为已删除 `deploy/ragflow/embedding_model.py` 的过期回归）已在 2026-09-15 处理：附件用例改按两级角色模型断言（同部门员工隐式 admin；无权限调用方仍 403 且先于存在性检查），过期 RAGFlow 覆盖测试删除；
- RAGAS 全量基线（25 样本 × 6 指标）尚未执行：判分约 3.6 分钟/指标项，单轮估时 ~9 小时；当前以 ADAS_new 小样本（n=3）作为资源受限基线，正式门禁前需补数据/预算并跑三次；
- 功能开关默认关闭（`CONVERSATION_SERIALIZE_SAME_THREAD` / `RESOURCE_GOVERNOR_ENABLED` / `SHARED_RETRIEVAL_EXECUTOR_ENABLED` = false），需在阶段验收后按 §10.2 灰度打开；P0a 正确性验收必须在开关开启的测试环境执行；
- 评测入口范围说明：RAGAS judge LLM 已由 `govern_ragas_llm` 接入进程 batch 槽；`EVAL_EMBEDDING_*`（embedding，非 LLM）**未接入**部署级 governor，属显式范围外（见 `entry-inventory.md` §4），纳入前不得宣称评测入口资源治理全覆盖。
