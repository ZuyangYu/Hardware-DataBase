# 命令参考

本 skill 的 `scripts/hdb.py` 是 `hardware-database` CLI 的薄封装(强制 `--json` + `health` 探测)。底下任何命令都可换成原生 CLI 直接调用。

## 封装层(cwd 为 skill 根目录)

```bash
cd {baseDir} && python3 scripts/hdb.py <subcommand> [args...]
```

| 封装子命令 | 转发的 CLI | 说明 |
|---|---|---|
| `health` | (无) | 三态探测:server_down / server_up(未登录) / ok |
| `whoami` | `whoami` | 当前用户 |
| `kbs` | `list-kb` | 可访问知识库 |
| `files --kb <name>` | `list-files --kb <name>` | 库内文件 |
| `query --kb <name> "<q>"` | `query --kb <name> "<q>"` | 检索 |
| `upload --kb <name> [--group <g>] FILE...` | `upload ...` | 上传 |
| `delete --kb <name> --file <f>` | `delete --kb <name> --file <f>` | 删除 |

封装层固定追加 `--api-url $HDB_API_URL`(默认 `127.0.0.1:8000`)与 `--json`,其余参数原样透传。令牌由 CLI 自行从 `HDB_TOKEN` 或会话文件解析。

## 原生 CLI(仓库根目录)

```bash
uv run hardware-database [全局选项] <子命令> [子命令选项]
```

### 全局选项

| 选项 | 默认 | 说明 |
|---|---|---|
| `--api-url <url>` | `HDB_API_URL` 或会话或 `127.0.0.1:8000` | API 地址 |
| `--token <tok>` | `HDB_TOKEN` 或会话 | 访问令牌 |
| `--json` | off | 结构化输出(机器消费) |

### 子命令

#### `login --user <u> [--password <p>]`
登录并存会话。`--password` 缺省则交互提示。

#### `whoami`
当前用户。`--json` 输出 `{username, role, department_name?, ...}`。

#### `list-kb`
可访问知识库。`--json` 输出 `[{name, department_name, permission}, ...]`。

#### `query --kb <name> "<问题>"`
- 默认:流式打印答案 delta 到 stdout。
- `--json`:聚合成一个对象 `{answer, summary, footer, token_usage}`。
  - `answer`:agent 合成的完整答案。
  - `summary`:检索摘要,含 `status`(`success` / 其他)、子问题覆盖、证据来源。
  - `footer` / `token_usage`:观测信息。

SSE 事件:`delta`(增量文本)、`done`(收尾摘要)、`error`(失败)。

#### `upload --kb <name> [--group <g>] FILE...`
上传(需 dept_admin)。`--group` 缺省自动按扩展名分类。`--json` 输出 `{status, success_count, total_count, messages}`。
- 扩展名决定 pipeline:`.doc/.docx/.pdf`->文档,`.xlsx`->表格,`.edf/.edif`->电路,`.xls` 被拒。
- 来源分组决定 RAGFlow 数据集(治理 / 设计)。

#### `list-files --kb <name>`
库内文件。`--json` 输出 `[{name, status, processor_kind}, ...]`。

#### `delete --kb <name> --file <filename>`
删除文件(需 system_admin)。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | API 错误(4xx/5xx)、检索失败 |
| 2 | 未登录(需 `login`)、参数错误 |
| 127 | CLI 未安装(跑 `uv sync`) |
