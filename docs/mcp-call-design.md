# HDB MCP 最终调用设计

本文件是最终设计，不再提供备选方案。

## 1. 总体结构

```text
OpenCode / Claude Code
        │  stdio MCP
        ▼
hdb MCP server（src/mcp_bridge/server.py）
        │  HTTP + Authorization: Bearer <per-user session token>
        ▼
HDB API（/api/v1）
```

- 一个用户一个 MCP 进程。
- 一个 MCP 进程只使用一个用户身份。
- 不共享 `aaa_admin` token，不做共享 stdio 桥。

## 2. 认证

- 认证方式：HDB Session Token。
- 获取方式：`POST /api/v1/login`，用户名 + 密码。
- 存储位置：`~/.config/hdb/mcp_token`，权限 `600`。
- 注入方式：MCP 包装器读取 token，设置 `HDB_API_TOKEN`。
- 过期处理：删除 token 文件，重新执行 setup 脚本。
- 不使用：`external-api-service` 分支的 API Key、OAuth、共享 token。

## 3. MCP Server

- 实现：`src/mcp_bridge/server.py`
- 传输：stdio
- 启动包装器：`~/.config/hdb/hdb-mcp`
- 环境变量：
  - `HDB_API_BASE`
  - `HDB_API_TOKEN`
  - `PYTHONPATH`

工具固定为 23 个：

```text
list_kbs
list_kb_files
list_directories
get_parse_task
retry_parse
read_file_chunks
search_knowledge_base
download_kb_file
list_tables
query_table
list_assets
get_asset
list_doc_assets
get_doc_asset
upload_file
create_directory_import
upload_directory_import_file
get_directory_import
delete_file
delete_directory
list_spreadsheets
list_circuit_designs
query_circuit
```

> 远程模式（streamable-http）复用同一工具集；Docker 部署见
> [`deploy/docker/README.md`](../deploy/docker/README.md) 的「内置 MCP Server」。

写入规则：

- `upload_file`：真实写入，要求 KB write 权限，平台审计并触发解析；单文件。
- `create_directory_import`：提交目录清单，创建可断点续传的导入批次（最多
  2000 文件 / 2 GiB），返回批次与每文件 ID。
- `upload_directory_import_file`：按批次 ID + 文件 ID 逐文件上传，可重试；
  单次受 `MCP_MAX_UPLOAD_BYTES`（默认 100 MiB）限制。
- `get_directory_import`：查询批次和每文件状态。
- `delete_file`：默认 dry-run，必须显式 `confirm=true`。
- `delete_directory`：默认 dry-run，必须显式 `confirm=true`；`recursive=true`
  才会删除目录下的文件和子目录，非递归模式只允许删除空目录。

## 4. OpenCode 配置

全局配置：`~/.config/opencode/opencode.json`

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "hdb": {
        "type": "local",
        "command": ["/home/<user>/.config/hdb/hdb-mcp"],
        "codemode": false
      }
    }
  }
}
```

Skill 通过软链接注册：

```text
~/.config/opencode/skills/hw-design-solution
  -> <repo>/.claude/hw-design-solution
```

工具在 OpenCode 中的名称：

```text
hdb_list_kbs
hdb_query_circuit
hdb_query_table
hdb_search_knowledge_base
...
```

Skill 在 OpenCode 中的 ID：

```text
hw-design-solution
```

## 5. 用户接入

**由用户本人执行，管理员不代跑。** Token 由平台在用户本人登录时生成，
管理员不签发、不保管、不转交。

每个用户在自己的机器上执行一条命令：

```bash
<repo>/scripts/hdb_mcp_user_setup.sh --username <自己的用户名>
```

脚本只是替代手工操作，不替代用户身份。脚本完成：

1. 登录并获取该用户自己的 Session Token。
2. 写入 `~/.config/hdb/mcp_token`（600）。
3. 生成 `~/.config/hdb/hdb-mcp`（700）。
4. 链接 `hw-design-solution` 到 OpenCode skills。
5. 执行 `opencode mcp add hdb --global`。

验证：

```bash
opencode mcp list
# ✓ hdb connected

opencode run --auto "调用 hdb_list_kbs，列出知识库名称。"
```

## 6. 使用方式

```text
使用 @hw-design-solution 按“模式 A：单文档执行”画 AAA TCN2 主板的②整体硬件方案框图。
先用 hdb_list_circuit_designs 和 hdb_query_circuit 查关键器件；
再用 hdb_search_knowledge_base + hdb_read_file_chunks 读原始约束与证据；
最后用 skill 的 scripts/checks 做六项自检。
```

## 7. 边界

- 本设计只覆盖同一台机器/同一仓库内的本地 stdio MCP。
- 不提供远程 MCP、多用户共享桥、API Key 管理。
- 需要远程多用户时，另立项目，不在本设计内。
