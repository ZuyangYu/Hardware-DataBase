# MCP Gateway（参考部署，dev-l1 档）

独立的 MCP 网关：负责 MCP 传输/会话/路由与工具治理，**不控制 OCI 运行时**。
它请求 Sandbox Runner 启动/回收固定 service profile，然后通过回环 HTTP 与该服务
容器内的 stdio MCP 桥通信；HDB 只调用下面的受认证端点。

## 组成

```
deploy/mcp-gateway/
├─ gateway.py           # FastAPI 服务：attach/discover/invoke/disable/revoke
├─ deployments.json     # 已审核的 deployment_ref -> 镜像 ID/profile digest/工具 allowlist
├─ stdio_server.py      # 固定只读 stdio MCP 服务（stdlib，JSON-RPC over stdio）
├─ stdio_bridge.py      # 容器内 HTTP 桥：spawn stdio 子进程并转发 tools/list|call
├─ Dockerfile           # hdb-stdio-mcp:1.0（离线构建，非 root、无 HDB 代码）
└─ smoke.py        # Gateway 契约 + 真实 stdio MCP 调用验证（10 项）
```

## 启动

```bash
# 1) 构建 service 镜像（离线）
cd deploy/mcp-gateway && docker build -t hdb-stdio-mcp:1.0 .

# 2) HDB→Gateway 使用 HDB 的服务令牌；Gateway→Runner 使用独立的
#    secrets/runner-token（0600）；由 Runner 创建 --internal 网桥。
setsid nohup env SANDBOX_GATEWAY_PORT=8781 \
  uv run python deploy/mcp-gateway/gateway.py \
  >> storage/logs/mcp-gateway.log 2>&1 < /dev/null &

# 3) 验证
uv run python deploy/mcp-gateway/smoke.py
```

## 接口（`/v1`，Bearer + `X-Audience: sandbox-gateway`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/gateway/health` | 健康检查（`healthy`） |
| POST | `/gateway/attach` | 通过 Runner 启动 service profile 并等待桥就绪 |
| GET | `/gateway/{ref}/discover` | 返回 allowlist 内的工具（schema 以审核值为准） |
| POST | `/gateway/{ref}/invoke` | 转发 `tools/call`（拒绝运行时控制参数） |
| POST | `/gateway/{ref}/disable` | 停止服务并解除挂载 |
| POST | `/gateway/{ref}/revoke` | 同 disable（撤销语义） |

## HDB 侧配置

```env
SANDBOX_ENABLED=true
SANDBOX_BACKEND=runner_api
SANDBOX_GATEWAY_ENABLED=true
SANDBOX_GATEWAY_URL=http://127.0.0.1:8781
SANDBOX_RUNNER_URL=http://127.0.0.1:8780
SANDBOX_SERVICE_AUTH_REF=workload://hdb-sandbox-dev-l1
SANDBOX_SERVICE_TOKEN_FILE=<绝对路径>/deploy/sandbox-runner/secrets/token
SANDBOX_REQUIRE_RUNTIME_ATTESTATION=true
SANDBOX_RUNTIME_ATTESTATION_REF=attestation://dev-l1/2026-09-22
SANDBOX_RUNTIME_PROFILE_DIGEST=<runtime-policy.dev-l1.yaml 的 sha256>
SANDBOX_ARTIFACT_SCANNER_REF=scanner://builtin/basic-v1
SANDBOX_AUTHORIZED_CAPABILITIES=sandbox.execute,sandbox.write_artifact,external_tool.invoke
```

HDB 侧登记：管理员调用 `POST /api/v1/sandbox/gateways`（声明式，不含任何命令/环境字段）
→ `review` → `test`（真实健康检查）→ `enable` → `attach` → `discover` → `invoke`。

## 安全边界与生产替换项

- Gateway 不持有 OCI 控制权；service 容器由 Runner 以 dev-l1 加固档启动，使用
  `--internal` 网桥（无出网，仅宿主可达）。
- 工具 allowlist 与输入 schema 以 HDB/Gateway 的审核值为准，服务端无法通过
  discovery 扩宽调用面；运行时控制参数（command/env/volume 等）在两层都会被拒绝。
- stdio MCP 镜像 ID 和 profile digest 已固定在 Gateway 和 Runner 独立目录；
  重新构建后需更新目录并重新审核 HDB 部署。
- Gateway 逐次核对 Runner 的稳定 OCI 容器身份；HDB 停用/吊销先本地拒绝，
  再请求 Gateway 回收，失败标记 `cleanup_pending` 并支持重试。
- **生产替换项**：为固定镜像提供 SBOM/签名；升级运行时档位；
  用外部扫描器替换内置结构扫描；为 HDB↔Gateway↔Runner 引入 mTLS/短时身份。
