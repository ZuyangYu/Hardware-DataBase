# R3 发布复核与回滚操作顺序

本文是待部署环境复核的操作清单，不代表已经执行生产发布。业务审批资格仍由正式审批系统确定。

## 发布证据

| 范围 | 当前证据 | 仍需补齐 |
|---|---|---|
| 冲突与写作输入 | 固定合成集 22 例/45 条数值量纲标签，自动裁决开/关均通过；关联回归 123 项 | 独立人工复核、真实资料相关性/冲突标签与质量阈值 |
| 真实来源检索 | 既有真实 RAGFlow 双/四库 7/7 锚点冒烟 | 独立人工相关性；生产部署版本的完整回答/生成链路 |
| PG 与恢复 | 48 项真实 PG 回归；增量迁移、含决策备份恢复、重复迁移和幂等重放 15 项检查 | 实际旧 Worker 隔离及 API/Worker 切换、生产 RPO/RTO |
| 批准与正式导出 | 待批准候选保持 `approval_pending`；本地正式导出阻断 | 正式审批系统接口、资格、回调验证与审批撤销流程 |
| 持续运行 | 15 分钟隔离持续检索，635 请求、3,592 调用、0 失败、线程池排空 | 部署负责人确定长稳时长、工作负载及延迟/可靠性 SLO |

证据入口：`multi-kb-conflict-acceptance.md`、`2026-10-19-multi-kb-r3-migration-recovery.md`、`2026-10-19-multi-kb-r3-soak.md`，以及既有本地 `2026-10-16-multi-kb-r3-live-acceptance.md`。任何待验收项不得因其他局部门禁通过而自动视为完成。

## 迁移前

1. 记录 API、chat/authoring Worker 的实际 commit/image、配置版本、数据库后端和来源索引代次；确认服务处于同一兼容协议。
2. 冻结新任务准入，盘点在途任务、租约、`waiting_human` 与 `approval_pending` 事项；按既有运维入口排空/隔离，不能丢弃待决事项。
3. 使用一致性备份保存当前权威数据库、Authoring 数据库及关联产物/检查点。记录备份时间、哈希、恢复点及增量写入边界；先在新隔离库验证恢复。
4. 在同一受控环境通过 DSN 环境变量运行检查，避免将连接串写入命令参数或验收报告：

```bash
uv run python scripts/production_hardening/migrate_multi_kb_scope.py --backend postgres --check
uv run python scripts/production_hardening/migrate_multi_kb_conflict_review.py --backend postgres --check
```

scope 使用 `CONTROL_PLANE_DATABASE_URL`；冲突工具优先使用 `AUTHORING_STORE_DATABASE_URL`，为空时回落至控制面 DSN。检查模式不写库；未就绪返回 1。重复事项需要核对来源快照和历史，不能直接删行消除冲突。

## 迁移、启用与复核

维护窗口内，依次对两种工具执行 `--apply`、`--verify`，再次 apply 后核对行数、payload、快照/证据戳、版本和历史日志。异常即保持维护状态并使用明确恢复点处理。

迁移成功后先启动当前兼容 API/Worker，核对就绪、协议版本和旧消费者隔离，再按发布范围启用：

- `MULTI_KB_CHAT_ENABLED`：多库入口及执行器。
- `MULTI_KB_HUMAN_REVIEW_ENABLED`：多库文档人工审阅契约及相应 Worker 领取。
- `MULTI_KB_CONFLICT_AUTO_RESOLVE_ENABLED`：自动裁决；可继续关闭，让待审事项进入人工流程。
- `MULTI_KB_REVIEW_REQUIRE_APPROVAL_FOR_CRITICAL=true`：关键规格批准要求保持启用；该设置不创建审批资格。

这些是进程启动配置；不能以单个测试进程内改变变量代替 API/Worker 统一部署。先验证 1/2/4 库、来源撤权、版本变化、同幂等键重放、等待/恢复、候选审批状态与正式导出阻断，再按部署 SLO 放量。

## 回滚与恢复

1. 停止新准入，保存发布后新增的任务、决定、审计和产物；排空/隔离相关消费者。
2. 关闭多库入口/人工审阅的部署开关并重启相应兼容进程；保留所有 R3 数据和协议标记。关闭开关后的新操作应明确拒绝，等待事项保持可追溯。
3. 若需回退应用版本，先确认该版本不领取多库协议 turn、review 契约工单和计划提交。没有兼容证明时保留维护状态，不能直接启动旧 Worker。
4. 数据库故障恢复使用包含最新决定审计的一致恢复点；在新库 restore、verify、核对全部权威数据和幂等重放后再切换连接。不得删新列、清空决定日志或用迁移前快照覆盖发布后历史。
5. 恢复后逐项复核来源授权、冻结版本、待审批候选、正式导出资格以及旧单库兼容，记录实际恢复耗时、数据损失和负责人签署。

## 外部输入

正式发布仍需提供：独立人工标注/复核人，正式审批系统及接口/回调/身份资料，部署 SLO 和持续负载时长，实际维护窗口、API/Worker 版本清单及发布/恢复负责人。编码代理不能代签这些业务或运维结论。
