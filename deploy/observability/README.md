# Hardware DataBase 可观测性本地栈

此目录提供方案文档中 P0/P1 的本地联调栈：OTel Collector、Prometheus、Tempo、Loki、Grafana、Phoenix 和 Phoenix PostgreSQL。所有镜像版本固定，升级时应在变更记录中说明兼容性。

启动：

```bash
docker compose -f deploy/observability/docker-compose.yml up -d
```

应用端将 `OTEL_EXPORTER_OTLP_ENDPOINT` 指向 `http://otel-collector:4317`（应用也在容器网络）或 `http://127.0.0.1:4317`（应用作为宿主机进程运行）。宿主机进程不能使用容器内的 `otel-collector` 域名。

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

默认不采集问题、回答、prompt、completion 或证据正文。需要在 Phoenix 查看完整内容时，将上述四个 `OBS_CAPTURE_*` 开关按需设为 `true`；`OBS_CONTENT_MAX_CHARS` 控制单个内容字段的最大长度。采集内容可能包含用户问题、模型上下文和知识库正文，启用前必须完成脱敏、访问控制和保留周期评审。Grafana 初始密码仅用于本地开发，请通过环境变量覆盖默认值。
