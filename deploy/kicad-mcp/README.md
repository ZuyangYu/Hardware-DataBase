# 内网 KiCad MCP 参考服务

MCP 集成方案 §4.12 的可部署示例：一个独立的远程 MCP 服务，只暴露两个受治理工具，
不接收 shell 文本、任意命令或宿主路径；写工具只返回候选产物引用，永不修改原件。

| 工具 | 注解 | 用途 |
|---|---|---|
| `kicad_analyze_schematic` | `readOnlyHint=True` | 只读分析，返回文本摘要 |
| `kicad_create_schematic` | `readOnlyHint=False` | 生成候选 `.kicad_sch`，返回产物引用 |

写工具返回契约（HDB 侧逐项校验）：

```json
{
  "filename": "candidate.kicad_sch",
  "content_base64": "<base64>",
  "sha256": "<content sha256>",
  "media_type": "application/x-kicad-schematic"
}
```

- `filename` 扩展名必须在 `MCP_ARTIFACT_ALLOWED_EXTENSIONS` 内；
- 内容不得超过 `MCP_MAX_ARTIFACT_BYTES`；
- `sha256` 与内容不一致会被拒绝（`artifact_hash`）；
- HDB 只把校验通过的字节写入 `MCP_ARTIFACT_STORAGE_DIR`，原始设计文件不受影响。

## 部署

### 方式一：容器（推荐）

```bash
cd deploy/kicad-mcp
docker build -t hdb-kicad-mcp:1.0 .
docker run -d --name hdb-kicad-mcp \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  -p 10.0.0.8:8080:8080 \
  hdb-kicad-mcp:1.0
# 服务地址：http://10.0.0.8:8080/mcp
```

生产部署建议：只加入内网网段、由反向代理终止 TLS 并校验调用方、限制出网
（默认拒绝），并把服务放在与 HDB 不同的主机或容器边界内。

### 方式二：直接运行（联调/本机验证）

```bash
pip install -r deploy/kicad-mcp/requirements.txt
KICAD_MCP_HOST=127.0.0.1 KICAD_MCP_PORT=8765 python deploy/kicad-mcp/server.py
# 服务地址：http://127.0.0.1:8765/mcp
```

## 在 HDB 中登记与启用

前置（一次性，需重启或通过「系统配置」热加载）：

```env
MCP_ENABLED=true
MCP_ALLOW_PRIVATE_HOSTS=true      # 内网地址必须
MCP_ALLOW_INSECURE_HTTP=true      # http:// 必须；有 TLS 则不需要
MCP_WRITE_ENABLED=true            # 仅当要使用写工具
```

管理页「系统配置 → 扩展插件」：

1. 登记：slug `kicad`、传输 `streamable_http`、URL `http://10.0.0.8:8080/mcp`、
   部门范围按需（`all` 或指定部门）。
2. 测试连接 → 启用。
3. 「工具」面板把 `kicad_create_schematic` 加入 `write_tools` 白名单。

员工侧：侧边栏「扩展」→ 订阅（使用写工具需勾选写入同意）。之后对话中即可调用
`mcp__kicad__kicad_analyze_schematic`；写工具只创建提案，用户在聊天卡片或扩展页
确认后由 worker 生成候选产物，可下载并核对 SHA-256。

## 联调冒烟脚本

`smoke.py` 用管理员/员工 API 走完“登记 → 测试 → 启用 → 白名单 → 订阅 →
真实回合调用 → 写提案确认 → 产物下载”的完整链路（需要本机 API 可达、服务已启动）：

```bash
uv run python deploy/kicad-mcp/smoke.py \
  --base http://127.0.0.1:8001 \
  --mcp-url http://127.0.0.1:8765/mcp \
  --admin-password "$AUTH_DEFAULT_ADMIN_PASSWORD" \
  --employee-password '<已存在的员工密码>'
```

脚本只打印状态与哈希，不打印密码或产物正文。

## 安全边界

- 不接收/执行 shell 文本、任意命令、任意 URL；没有 `subprocess` 调用；
- 容器以非 root、只读 rootfs、`--cap-drop ALL` 运行；
- HDB 侧仍有四道门：管理员白名单、`MCP_WRITE_ENABLED`、员工写入同意、
  `external_tool.write` 能力位；
- 外部结果不进入 `rt.evidence`、不获得引用编号；
- 如需真实 `kicad-cli` 渲染/校验，请在生产镜像中以固定 argv 调用
  （`subprocess.run([...], shell=False)`），并继续遵守“只生成候选产物”的约束。
