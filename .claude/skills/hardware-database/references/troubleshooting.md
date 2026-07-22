# 排错

## health 探测分流

`python3 scripts/hdb.py health` 的三种状态:

| `status` | 含义 | 处理 |
|---|---|---|
| `ok` (authed=true) | 服务在跑、已登录 | 直接检索 |
| `server_up` (authed=false) | 服务在跑、未登录/令牌过期 | `hardware-database login --user <u>` 或设 `HDB_TOKEN` |
| `server_down` | 服务没起 / 地址不对 | `uv run hardware-database-server`;确认 `HDB_API_URL` |

## 常见错误

### `未登录。请先运行 hardware-database login。`(退出码 2)
CLI 没拿到令牌。三选一:`login --user <u>` / `export HDB_TOKEN=<tok>` / `--token <tok>`。
注意:会话文件在 `~/.config/hardware-database/`,若 `HDB_CONFIG_DIR` 被改到别处,login 和查询要用同一个目录。

### `API 错误: ...`(401)
令牌失效。CLI 会附"令牌可能过期,请重新 login"。重新登录即可。

### `API 错误: ...`(403)
权限不足:
- 检索/列文件 403 -> 对该 KB 无 read 权限,换可访问的 KB(先 `kbs` 看权限)。
- 上传 403 -> 当前角色不是 dept_admin,需部门管理员。
- 删除 403 -> 当前角色不是 system_admin。
- 建库 403 -> 需 dept_admin。

### `API 错误: ...`(404)
KB 名或文件名不存在。`kbs` / `files --kb <name>` 确认实际名称(注意大小写)。

### `API 错误: ...`(5xx) / `error` SSE 事件
服务端异常。agent 管线是 fail-open 的:LLM 节点有确定性兜底,但 RAGFlow 侧故障会冒到 `error` 事件。看 `uv run hardware-database-server` 的日志(`storage/logs/`),确认 RAGFlow key / 网络可达。

### `query` 返回 `summary.status` 非 success
证据不足或部分失败(agent 仍可能给出 `partial_but_answerable` 的答案)。处理:
1. 读 `summary` 看哪些子问题没覆盖。
2. 用器件型号/全称/同义词换措辞重检索。
3. 仍不足则如实告诉用户"知识库里没找到充分证据",不要编造。

### `hardware-database CLI not found`(退出码 127)
未安装或不在 PATH。`uv sync`(仓库根目录)后,封装层会自动 fallback 到 `uv run --project <repo> hardware-database`;若直接用原生 CLI,需 `uv run hardware-database ...`。

### 上传被拒:`.xls` 不受支持
仅 `.xlsx` 走表格 pipeline,`.xls` 被拒。先转成 `.xlsx` 再传。

### 上传后查不到
RAGFlow 解析是远程异步,表格解析在 daemon worker 上,电路索引用同步。刚上传的文档可能还在解析,`files --kb <name>` 看 `status` 字段判断是否完成。

## 日志与状态位置(服务侧,gitignored)

```
storage/logs/                     # 应用日志
storage/pipeline_documents.db     # 文档/解析任务台账
storage/table_indexes/            # 表格索引
storage/circuits/                 # 电路索引
storage/auth.db                   # 账号/会话
```
