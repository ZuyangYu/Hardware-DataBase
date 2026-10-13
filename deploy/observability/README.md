# Hardware DataBase 可观测性本地栈

此目录提供方案文档中 P0/P1 的本地联调栈：OTel Collector、Prometheus、Tempo、Loki、Grafana、Phoenix 和 Phoenix PostgreSQL。所有镜像版本固定，升级时应在变更记录中说明兼容性。

启动：

```bash
export GRAFANA_ADMIN_PASSWORD='replace-with-a-strong-password'
export PHOENIX_DB_PASSWORD='replace-with-a-different-strong-password'
docker compose -f deploy/observability/docker-compose.yml up -d
```

单机 Docker 生产部署（`deploy/docker`）默认用覆盖文件，一条命令同网络起全套：

```bash
cd deploy/docker
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
# 或：bash deploy/docker/quickstart.sh
```

本目录的独立 compose 适合观测栈单独部署、或放在另一台机器上。

应用端将 `OTEL_EXPORTER_OTLP_ENDPOINT` 指向 `http://otel-collector:4317`（应用也接在同一容器网络）
或 `http://127.0.0.1:4317`（应用作为宿主机进程运行）。宿主机进程不能使用容器内的 `otel-collector` 域名。

`deploy/docker` 的生产编排通过覆盖文件把应用与观测栈放进同一个 Compose 网络，应用侧使用
`http://otel-collector:4317`。资源受限环境可以显式使用
`quickstart.sh --without-observability`，此时 API 不导出 OTLP。

```dotenv
OBS_ENABLED=true
OBS_CAPTURE_CONTENT=false
OBS_CAPTURE_QUERY=false
OBS_CAPTURE_EVIDENCE=false
OBS_CAPTURE_LLM_CONTENT=false
OBS_CONTENT_MAX_CHARS=50000
OBS_GRAFANA_BASE_URL=http://localhost:3000
OBS_PHOENIX_BASE_URL=http://localhost:6006
```

## FlClash TUN 绕过

如果宿主机启用了 FlClash TUN，Docker 发布端口的回包可能被透明代理截获，导致外部客户端握手超时。当前主机可执行：

```bash
sudo bash deploy/observability/bypass-flclash-ports.sh --persist
```

脚本只为 `3000` 和 `6006` 的新连接设置主路由标记，并保存当前 iptables 规则。执行后重新测试 `http://<服务器IP>:3000` 和 `http://<服务器IP>:6006`。

默认不采集问题、回答、prompt、completion 或证据正文。需要在 Phoenix 查看完整内容时，将上述四个 `OBS_CAPTURE_*` 开关按需设为 `true`；`OBS_CONTENT_MAX_CHARS` 控制单个内容字段的最大长度。采集内容可能包含用户问题、模型上下文和知识库正文，启用前必须完成脱敏、访问控制和保留周期评审。启动独立观测栈前必须设置 `GRAFANA_ADMIN_PASSWORD` 与 `PHOENIX_DB_PASSWORD`；编排不再提供默认口令。

## Collector 0.128.0 持久队列和自监控

独立栈和 `deploy/docker` 覆盖文件均挂载 `otel-collector-wal`，初始化服务设置目录 owner
10001:10001。Tempo/Phoenix/Loki 各有 10000 batches 的持久 sending queue，后端故障退避重试。
内部 `8888` 暴露 Collector 自监控，Prometheus 同时抓取应用 `9464` 与 self-metrics `8888`。

`quality-cost-durability` 看板区分 enqueue loss 和 send retry failures，同时展示费用写入、质量队列、
worker 角色与聊天 SLO。部署覆盖文件已给 evaluation/quality worker 接入 OTLP。

告警包括 Collector down、队列 80% 压力、发送失败、enqueue-failed、费用 dropped/corrupt/write_failures、
required role missing、quality worker 指标 absent，以及启用后的聊天多窗口 burn-rate。
版本化费用预算另有 known amount exceeded 与 unknown cost coverage 告警；预算无强制模型停用副作用。
默认 Alertmanager 接收器需按实际通知渠道配置；规则通过不等于生产通知已送达。

WAL 保护已入持久队列的遥测。实测 receiver/SDK 成功应答后，队列满和磁盘满仍会丢失 span，
必须看 `otelcol_exporter_enqueue_failed_spans_total`。应用 SDK 尚未导出的数据不在 WAL 保护范围。
磁盘规划按三个出口各自的 bytes/s × 可缓冲故障时间 × 安全系数计算；10000 batches 不是磁盘字节配额。
维护前备份 WAL 卷；不要通过删除该卷来解决积压。队列/磁盘满会触发不可重放丢失，需要容量处置。

可重现隔离故障与通知测试（每个脚本清理其创建的 container）：

```bash
uv run python scripts/production_hardening/collector_wal_acceptance.py
uv run python scripts/production_hardening/collector_pressure_acceptance.py
uv run python scripts/production_hardening/alert_delivery_acceptance.py
uv run python scripts/production_hardening/sdk_preexport_acceptance.py
HDB_TEST_POSTGRES_DSN=<fixture-dsn> uv run python scripts/production_hardening/cost_workflow_backup_acceptance.py
docker run --rm --entrypoint /bin/promtool \
  -v "$PWD/deploy/observability/prometheus:/etc/prometheus:ro" \
  prom/prometheus:v3.5.0 test rules /etc/prometheus/tests/rules.test.yml
```

证据保存在 `storage/evaluations/production-feedback-p2-20261008/`。SIGKILL 恢复 10/10；队列满损失
198/200、运行中磁盘满损失 179/200，均有相同数量的 enqueue-failed。通知测试对隔离 HTTP 接收端
验证 firing/resolved，生产接收端和正式容量/24h 长稳态另按部署窗口验收。
SDK 导出前 SIGKILL 的 10 条 span 未送达，正常关闭导出 10/10；这部分无法由 Collector WAL 恢复。

## 业务 SLO 首版

聊天接受时间到首 token/终态与 worker execution 分开。quality worker 根据权威 turn/事件对 task ID
去重，保存 deadline/首 token 的单调违约事实；早取消单列，晚成功或晚取消不抹去已发生的违约。
提供批准的 `HDB_CHAT_SLO_POLICY`（见 `.env.example`）后才启用 general/KB chat SLO。
无流量没有 burn-rate 数值；多窗口 14.4×/6× 规则要求最低事件数。多个副本 gauge 以 max 聚合。
authoring/attachment/export 的业务成功定义及 deadline、PG/Redis/主机/GPU exporter 继续接入。
