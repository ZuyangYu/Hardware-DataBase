# draw.io MCP（远程桥接服务）

把官方 [`@drawio/mcp`](https://www.npmjs.com/package/@drawio/mcp) stdio 服务包装成
HDB 可登记的 **Streamable HTTP MCP**。官方包负责把 XML / CSV / Mermaid 压缩成
draw.io `#create` 链接；本桥负责 HTTP 传输、工具过滤，并额外提供原生
`.drawio` 文件产物。

> HDB 的远程 MCP 通道不会启动 stdio 或本地第三方进程，所以这里必须由一个独立
> 服务承载 MCP 传输；HDB 只出站连接 `http://<host>:8091/mcp`。

## 暴露的工具

| 工具 | 类型 | 用途 |
|---|---|---|
| `create_drawio_file` | 写工具 | 接收 `mxGraphModel` XML，返回可下载的 `.drawio` 候选产物；需用户在写入提案中确认 |
| `open_drawio_xml` | 只读 | 用 draw.io/mxGraph XML 打开编辑器链接 |
| `open_drawio_csv` | 只读 | 用 draw.io CSV 格式生成编辑器链接 |
| `open_drawio_mermaid` | 只读 | 用 Mermaid 语法生成编辑器链接 |
| `search_shapes` | 只读 | 搜索 draw.io 图形库，返回可用 style |

`create_drawio_file` 走 HDB 的受控写通道：模型只创建提案，用户确认后平台才调用
本服务并保存候选文件；文件通过 `/plugins/mcp/write-proposals/<id>/artifact` 下载。

**刻意不暴露** 官方 stdio 服务的 `list_pages` / `get_page` / `set_page`：这些工具
接收宿主文件路径，远程服务不应让平台 Agent 读写服务器本地文件。

## 部署

### 方式一：Docker（推荐）

```bash
cd deploy/drawio-mcp
docker build -t hdb-drawio-mcp:1.0 .
docker run -d --name hdb-drawio-mcp \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  -p 127.0.0.1:8091:8091 \
  hdb-drawio-mcp:1.0
# 服务地址：http://127.0.0.1:8091/mcp
```

自建 draw.io 实例时追加：

```bash
-e DRAWIO_BASE_URL=https://drawio.example.com/
```

### 方式二：直接运行（本机联调）

```bash
cd deploy/drawio-mcp
npm ci --ignore-scripts
DRAWIO_MCP_PORT=8091 node server.mjs
```

本仓库的 `scripts/hdb.sh` 也支持纳管这个进程。先安装依赖，再在 `.env` 打开：

```env
HDB_DRAWIO_MCP_ENABLED=true
HDB_DRAWIO_MCP_PORT=8091
```

之后 `bash scripts/hdb.sh start/stop/status/logs` 会一并管理 draw.io MCP。

联调：

```bash
node smoke.mjs
# 或：DRAWIO_MCP_URL=http://127.0.0.1:8091/mcp node smoke.mjs
```

冒烟脚本会列出工具、调用 `open_drawio_mermaid` 检查编辑器 URL，并调用
`create_drawio_file` 检查候选产物字段。

## 在 HDB 中登记

HDB 默认只允许 HTTPS 和公网地址；本机回环地址需要一次性放开（仅建议开发机）：

```env
MCP_ENABLED=true
MCP_ALLOW_PRIVATE_HOSTS=true
MCP_ALLOW_INSECURE_HTTP=true
```

然后以系统管理员登录，在「系统配置 → 扩展插件」登记：

| 字段 | 值 |
|---|---|
| 名称 | draw.io 图表 |
| slug | `drawio` |
| 传输 | `streamable_http` |
| URL | `http://127.0.0.1:8091/mcp` |
| 部门范围 | `all`（或指定部门） |
| 标签 | `diagram,drawio` |

点击「测试连接」→「启用」。若要让 Agent 生成可下载文件，还需要：

1. `.env` 打开 `MCP_WRITE_ENABLED=true`；
2. 把 `create_drawio_file` 加入该服务的写工具白名单；
3. 员工在「扩展」页订阅时勾选写入同意。

员工侧订阅后，Agent 才会在对话中装配 `mcp__drawio__*` 工具。

等价的 API 调用：

```bash
TOKEN=<系统管理员 token>
BASE=http://127.0.0.1:8002/api/v1/plugins/mcp
curl -X POST $BASE -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"slug":"drawio","name":"draw.io 图表","transport":"streamable_http",
       "url":"http://127.0.0.1:8091/mcp","department_scope":"all",
       "tags":["diagram","drawio"]}'
curl -X POST $BASE/drawio/test -H "Authorization: Bearer $TOKEN"
curl -X PUT  $BASE/drawio/status -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"enabled":true}'
curl -X PUT  $BASE/drawio/write-tools -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"write_tools":["create_drawio_file"]}'
```

员工订阅后，可直接说「画一张登录流程图并生成 login-flow.drawio 文件」，Agent 会
调用 `mcp__drawio__create_drawio_file`；确认写入提案后即可下载。只想要在线预览时，
仍可让 Agent 调用 `open_drawio_mermaid` 等只读工具返回编辑链接。

## 安全边界

- 不暴露文件系统工具，不接受任意路径；
- 只返回链接或候选 `.drawio` 字节，不上传/写入 HDB 原始数据；
- `.drawio` 走写提案确认流程，确认前不会调用外部服务；
- 对外只监听 8091，建议只绑定回环或内网地址；
- 生产环境建议用反向代理终止 TLS，并把 `MCP_ALLOW_INSECURE_HTTP` 保持为 `false`；
- `MCP_ALLOW_PRIVATE_HOSTS=true` 会放宽所有 MCP 服务的地址校验，只在受控内网使用。
