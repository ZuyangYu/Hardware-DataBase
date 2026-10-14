# 26 · 固定实验与调用费用账本

对应代码：`src/quality/{workflow,evaluation_bridge,experiments}.py`、`src/costs/`、
`src/api/routes/{quality_workflow,costs}.py`、`src/observability/slo.py`。

## 内部案例、回归、发布与回访 API

高级质量前端已移除，以下功能仅保留为内部接口和历史记录。日常用户通过反馈工单提交问题，
管理员联系处理并记录解决办法；工单关闭不依赖以下步骤。

1. 通过质量案例与复核 API 对有权访问的固定材料复核；confirmed 并提供参考答案。
2. 冻结接口记录材料、所选证据、rubric、split 和 review ID 的不可变来源 manifest。
   修订或撤销后重新读取时会拒绝使用失效材料；同一会话不能跨 repair/gold/holdout 分组。
3. 评分接口按该版本创建 offline run。原始 owner 可调用固定证据候选生成接口，
   只向业务模型提供问题与冻结检索证据；参考答案只交给 scorer，不提供给生成模型。
4. run 沿用共享租约 evaluation worker；在线采集完成后显式调用候选评分接口。
   授权详情：`GET /api/v1/quality/evaluation-runs/{run_id}`；原管理员评测入口不会开放正文。
5. 关联修改引用和不同的 baseline/candidate run。服务端校验固定输入内容指纹、cohort、scorer 和候选门禁。
   数值配对由代码计算，评分不足、输入改变、未完成报告不能计作验证通过。
6. 填 release revision 和实际部署时间；关闭案例还需部署后同场景验证 turn，代码 revision 一致，
   最新人工复核必须 `not_an_error / correctness=correct`。创建修改引用不会自动关闭问题。

首版 candidate 是 **固定证据上的回答 Prompt/模型实验**。检索、解析、Agent 与工具修复的完整实验
还需要冻结来源库与受控执行环境，不能以此实验替代完整 RAG 回归。

quality worker 每 30 秒清理已撤销/过期/删除材料的受治理 run 派生文件；活 worker 在下一 checkpoint
取消并由 fenced owner 清理。原始快照、授权副本及评测读取均保留服务端权限检查。

## 费用口径与受控价格

`/admin/costs` 展示共享调用明细、已知金额、未知调用和 coverage；支持时间/用途/受控模型/部门/项目过滤、
UTC 日趋势与分币种模型已知费用排行、任务全部尝试分页和 CSV。趋势/排行最多 10000 调用，超过时明确缩小范围。
系统状态页的旧 usage 明确为当前 API 进程，持久账本可跨副本/重启读取。

- 每次实际外层 provider 调用有独立 invocation ID；start/end 按 event ID 幂等写入。
- runtime 为 CHAIN、provider 为 LLM。费用仅从叶子事件算，不对父级累计 token 再计价。
- SDK 隐藏内部 HTTP retry 时标记 logical_only；外层 fallback、业务重试、judge re-ask 分别记录。
- absent usage/价格为 NULL；reported zero 才为 0。价格按真实 response model 与发生时间匹配。
- cache 从 input 扣出后分别计价，已含在 output 的 reasoning 不再加价；未支持的计费桶保留 unknown。
- Decimal/PG NUMERIC，币种分别合计。金额是应用 token 测算，供应商结算折扣/TCO/账单尚需对账。
- coverage 分母是已记录 invocation；queue/spool 丢失告警表示存在未记录事件，不能据此宣称全量覆盖。

开启费用采集：

```dotenv
COST_LEDGER_ENABLED=true
COST_MODEL_REGISTRY={}
```

注册别名，避免 URL/key 进入账本：

```bash
uv run python -m src.costs.cli --registry-key openai-compatible https://example.invalid/v1 requested-model
```

将输出键放入 `COST_MODEL_REGISTRY`：

```json
{"<lookup-key>": {"endpoint_alias": "cloud-primary", "model_alias": "chat-main"}}
```

使用实际供应商批准的价目创建 JSON；字段是 `price_version`、`endpoint_alias`、`provider`、
`requested_model`、`response_model`、UTC `effective_from`/可空 `effective_to`、`currency`、
`input_per_million`、`cached_input_per_million`、`output_per_million`。单价采用十进制字符串。

```bash
uv run python -m src.storage.bootstrap
uv run python -m src.costs.cli --price-file approved-price.json
```

管理员也可用 `POST /api/v1/costs/prices` 注册。版本不可修改、区间不重叠；历史调用不因导入新价目重新计价。
首版不做无审计的历史重算；供应商账单调整/结算差异记录继续扩展。

管理员在费用页或 `POST /api/v1/costs/budgets` 注册固定窗口预算版本。字段为 budget_version、
currency、十进制 amount_limit、UTC effective_from/effective_to，可选 department_id/purpose/model_alias。
版本不可改，同范围/币种的窗口不能重叠；员工仅可查看本部门策略，后台采用该策略固定时间窗口聚合。
budget 为观测/告警口径，不自动停用模型。已知金额达到限额可确认超限；未知调用或丢失时不能声称精确预算内。
策略低于限额显示“已知金额低于限额”，无数据单列；不输出精确花费预测。

## 费用写入故障与重放

模型路径只向 1024 容量队列 `put_nowait`；后台先写原子 spool，再写权威 DB。
单进程 spool 默认 64 MiB。DB 失联时保留已落盘事件，Linux 以文件锁跨重启接管同目标范围内的旧目录。
目录为 `cost-spool/<destination-hash>/<pid-uuid>/`；sink/单个 invocation 固定写入目标，配置重载不会把旧事件写入新库。
非 Linux 环境需停消费者后显式重放；损坏/冲突文件隔离为 `.corrupt`。

```bash
uv run python -m src.costs.cli --replay-spool
```

更换 DSN 或接管旧扁平目录时，在停消费者后显式选择源目录，并配置其正确目标 DB：

```bash
uv run python -m src.costs.cli --replay-spool --spool-directory storage/cost-spool/<source-directory>
```

SDK 调用和内存队列之间、队列与 fsync 之间仍可能在 SIGKILL 时丢失。看板和告警需同时检查
`hdb_cost_sink_events_total{status="dropped"}`、`corrupt`、`write_failures`、`spool_full`。
spool 各进程配额之和及 Collector 的真实出口速率、故障缓冲磁盘容量需在正式负载下规划。

## 统计报告与聊天 SLO

质量页“生成统计报告”先由代码冻结范围内的 all-time 案例/费用统计，再给出 proposed 规则建议。
报告保存输入 hash，可重复按请求键返回；没有读取 Phoenix、没有 LLM 自行推测数值。
当前最多 10000 条案例；时间窗口、跨源诊断、定时报告与 LLM 说明继续扩展。

聊天 SLO 仅在显式提供 `HDB_CHAT_SLO_POLICY` 后启用；需要批准的 version/effective_from、
deadline_seconds、first_token_seconds、availability_target 和 minimum_events。
该策略为 task 固定，不覆盖旧任务；业务事实按 turn ID 去重，迟到成功/取消不能删除违约。
窗口按 accepted_at 归组，有等待 deadline 成熟的滞后；无流量显示 insufficient_traffic。
authoring/attachment/export 的成功标准及 deadline 需单独确认和接入。

已在当前配置模型上验证两次真实调用，reported usage 均持久保存；请求别名与同步响应模型不同，
流式模型版本缺失，因此无正式价目时仍为 unknown_price。`real-provider-usage.json` 只保留用量/身份元数据。
该合成提示测试不作为生产领域质量/供应商账单对账结论。

Collector WAL/self-metrics、队列/磁盘满实测、burn-rate、required-role 和通知验收见
[部署运行手册](../deploy/observability/README.md)与
[实施记录](../docs/superpowers/plans/2026-10-8-production-feedback-cost-observability-implementation.md)。
