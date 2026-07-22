# 配置指南

本 skill 依赖本地 Hardware DataBase API 服务 + 登录态。CLI 与服务端分离:RAGFlow key / `.env` / `auth.db` 只在服务侧,客户端(CLI / 本 skill)只持有令牌。

## 1. 安装

仓库根目录执行(本仓库用 `uv`):

```bash
uv sync                    # 装好依赖,含 hardware-database / hardware-database-server 两个 console script
```

确认 CLI 可用:

```bash
uv run hardware-database --help
```

## 2. 启动 API 服务

```bash
uv run hardware-database-server             # 默认 127.0.0.1:8000
```

地址可用环境变量改:

| 变量 | 默认 | 说明 |
|---|---|---|
| `HDB_API_HOST` | `127.0.0.1` | 监听地址 |
| `HDB_API_PORT` | `8000` | 监听端口 |

服务侧会读 `.env`(RAGFlow key、agent LLM provider 等)。`.env` 含真实 key,**不是模板**,不要外发;模板用 `.env.example`。

## 3. 登录

```bash
uv run hardware-database login --user <用户名>
# 提示输入密码;或非交互:--password <密码>
```

登录成功后令牌落盘:

```
~/.config/hardware-database/    # 会话文件(token / username / api_url)
```

之后所有 CLI 子命令自动带令牌,无需重复登录。可用 `HDB_CONFIG_DIR` 改这个目录(测试/多账号场景)。

### 不用 login,直接用环境变量

```bash
export HDB_API_URL=http://127.0.0.1:8000
export HDB_TOKEN=<令牌>
```

令牌优先级:`--token` 参数 > `HDB_TOKEN` > `login` 会话文件。
地址优先级:`--api-url` 参数 > `HDB_API_URL` > 会话文件 > `127.0.0.1:8000`。

## 4. 角色与权限

服务端 `auth.db` 三级角色(部门+KB scoped):

| 角色 | 能做什么 |
|---|---|
| `user`(普通用户) | 检索、列知识库、列文件 |
| `dept_admin`(部门管理员) | 上面 + 上传文件、建库 |
| `system_admin` | 上面 + 删除文件 |

权限复用 `RAGFlowBackend._check_kb_access` 与 `RequestContext.has_kb_permission`:普通用户只能检索,部门管理员才能上传/建库,删除需 system_admin。

## 5. 验证

```bash
cd .claude/skills/hardware-database && python3 scripts/hdb.py health
# 期望: {"status": "ok", "authed": true, ...}
```

或直接用 CLI:

```bash
uv run hardware-database whoami --json
uv run hardware-database list-kb --json
```

## 6. 也可以用 MCP(原生工具集成)

除了本 skill(Bash 调 `scripts/hdb.py`),还有 MCP 这条原生路径:`src/mcp/server.py`(`hardware-database-mcp`,stdio)把同一套 API 暴露成 MCP 工具,项目级 `.mcp.json` 已注册,Claude Code 自动发现并原生调用 `query`/`list_kbs` 等,不必走 Bash。两者共用同一套 API + 同一个 `login` 会话,二选一即可:

- 想让 Claude Code **原生工具调用** -> 用 MCP(起 `hardware-database-server` + `login` 后,在 Claude Code 里直接问)。
- 想在**任意 agent / 脚本**里调 -> 用本 skill 的 `scripts/hdb.py` 或裸 CLI。

MCP 工具:health / whoami / list_kbs / list_files / query / upload / delete。详见仓库 `CLAUDE.md` 的「API、CLI 与 MCP」段与 `src/mcp/server.py`。
