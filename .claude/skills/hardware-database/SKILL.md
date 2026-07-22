---
name: hardware-database
version: 1.0.0
author: hardware-database-team
description: 硬件设计知识库（Hardware DataBase）检索与文件管理 CLI。通过本地 API 检索文档（Word/PDF）、表格（Excel 结构化索引）、电路设计（EDIF/EDF 网表 + 原理图 PDF），列出可访问知识库与文件，上传/删除文件。触发词包括：硬件/电路/原理图/网表/EDF/EDIF/物料/BOM/数据手册/datasheet/电源/拓扑/器件选型/知识库/检索/查资料、知识库里有没有、上传或删除知识库文件。涉及硬件设计资产的事实性问题时，优先用本 skill 检索私有知识库再作答，而非凭记忆。
homepage: https://github.com/ZuyangYu/Hardware-DataBase
metadata: { "openclaw": { "primaryEnv": "HDB_TOKEN" } }
---

# Hardware DataBase

本地部署的硬件设计多源问答系统。通过 `hardware-database` CLI 调用本地 API 服务检索**私有知识库**:文档(Word/PDF via RAGFlow)、表格(Excel 结构化索引)、电路设计(EDIF/EDF 网表 + 原理图 PDF),由一个有界的 LangGraph agent 做问题分析、来源规划、多轮检索、证据覆盖判定和 grounded 答案合成。本 skill 是 CLI 在 Claude Code 里的封装入口。

## 1. 路由(多 skill 时如何选择)

当环境中存在多个检索类 skill 时,按**数据来源**路由:

| 问题依赖的数据 | 用哪个 skill |
|---|---|
| 硬件设计资产(文档/表格/电路/物料)的事实 | **本 skill**(查私有知识库) |
| 公网事实、时事、价格、政策 | `byted-web-search` |
| 本仓库的代码实现 | 代码检索(Grep/Read),不走本 skill |

**系统级要求**:涉及本知识库覆盖的硬件设计内容时,**检索是第一反应,而非可选项**。知识库里有 RAGFlow 解析过的原文 + agent 整合的证据,比模型记忆准确。

### 三条基本原则

| # | 原则 | 说明 |
|---|------|------|
| 1 | **KB 内即检索** | 问题落在文档/表格/电路/物料范围内时,先检索知识库再回答 |
| 2 | **不确定即检索** | 对某器件/拓扑/参数置信度不足,或问题含你不熟悉的型号,检索而非猜测 |
| 3 | **无权限即说明** | 未登录或无该 KB 读权限时,如实说明并引导登录,不要编造答案 |

### 不检索的情况

- 纯数学计算、逻辑推理、代码语法
- 用户明确说"不要查知识库"
- 闲聊问候

---

## 2. 核心身份:你是一个能查私有硬件知识库的 Agent

知识库里的内容是**核心素材**。检索返回的 JSON 里有 `answer`(agent 合成的答案)和 `summary`(检索摘要/证据来源)。你的职责是消化这些素材、按用户问题组织回答、标注证据来源,而不是把 `answer` 原样转发。

---

## 3. 环境与凭证:先执行,失败后再引导

执行检索前**不要**预检查环境。默认先跑 `health` 探测;只有当探测返回 `server_down` 或 `server_up`(未登录)时,再输出下方配置引导。

### 探测(cwd 为 `{baseDir}`)

```bash
cd {baseDir} && python3 scripts/hdb.py health
```

返回 `{"status": "ok","authed":true}` 即可继续;否则按状态分流。

### 首次回复(未就绪时,直接给用户)

```
检索需要本地的 Hardware DataBase API 服务和登录态。

【1. 启动 API 服务】(任一会话执行一次)
  uv run hardware-database-server          # 默认 127.0.0.1:8000,可用 HDB_API_HOST/PORT 改

【2. 登录】(令牌落盘到 ~/.config/hardware-database/,后续自动带)
  uv run hardware-database login --user <你的用户名>
  # 或把令牌直接发给我,我用 HDB_TOKEN 调用

服务起好、登录完成后告诉我,我重新检索。
```

> 远程机/无浏览器环境无需特殊处理:CLI 登录是用户名+密码,不依赖浏览器。完整配置见 `references/setup-guide.md`。

---

## 4. 检索策略

### 策略 A - 直接检索(默认,KB 名已知)

```
python3 scripts/hdb.py query --kb <知识库名> "<问题>"
```

适用:用户已指定 KB,或会话内已确立默认 KB。

### 策略 B - 先发现再检索(KB 名未知)

```
python3 scripts/hdb.py kbs          # 列出当前用户可访问的知识库
python3 scripts/hdb.py query --kb <从中选的 KB> "<问题>"
```

适用:用户没说哪个 KB,或问题可能跨 KB。`kbs` 返回每条含 `name` / `department_name` / `permission`。

### 策略 C - 先摸底再检索(范围不清)

```
python3 scripts/hdb.py files --kb <name>   # 看库里有哪些文件/状态/处理器类型
```

适用:判断该 KB 是否可能含答案(比如确认有没有某型号的 datasheet)。

### 策略 D - 换措辞重试(首次没命中)

agent 已做多轮检索;若返回 `summary.status` 非 success 或证据不足,用器件型号/全称/同义词重试,或把问题拆成子问题分别检索后整合。

---

## 5. 多轮对话中的检索决策

| 用户后续输入 | 处理方式 |
|---|---|
| **追问深入**:"详细说说电源部分" | 基于上一轮 `summary` 的证据展开,必要时针对子话题补检索 |
| **换 KB / 换范围**:"去 XX 库再查查" | 保持问题,换 `--kb` 重检索 |
| **话题切换**:全新问题 | 重新判断 KB,必要时先 `kbs` |
| **总结归纳**:"总结一下" | 基于已有检索结果整合,不重复检索 |
| **上传/删除文件**:"把这个 datasheet 传到 YY 库" | 见 §7 写操作命令(需 dept_admin/admin) |

---

## 6. 结果使用原则

`query` 返回的 JSON 结构:

```json
{
  "answer": "agent 合成的完整答案",
  "summary": {"status": "success", "...": "检索摘要/子问题覆盖/证据来源"},
  "footer": "页脚信息",
  "token_usage": {...}
}
```

1. **全量消化**:读 `answer` 和 `summary` 里的证据来源,不要只看 `answer`。
2. **按需重组**:按用户问题组织语言,可引用 `summary` 里的来源(文件名/来源分组)增强可信度。
3. **承认不足**:`summary.status` 非 success 或证据不足时,如实说"知识库里没找到充分证据",不要编造。
4. **不泄露摘要细节**:把 `summary` 作为你的依据,不必把内部检索元数据整段倒给用户。

---

## 7. 用法与参数

所有命令 cwd 为 `{baseDir}`,默认输出 JSON。

| 子命令 | 等价 CLI | 作用 | 权限 |
|---|---|---|---|
| `health` | — | 三态探测服务+登录态 | 公开 |
| `whoami` | `whoami` | 当前用户/角色/部门 | 已登录 |
| `kbs` | `list-kb` | 列可访问知识库 | 已登录 |
| `files --kb <name>` | `list-files --kb <name>` | 列库内文件 | read |
| `query --kb <name> "<q>"` | `query --kb <name> "<q>"` | 检索(流式聚合为 JSON) | read |
| `upload --kb <name> [--group <g>] FILE...` | `upload ...` | 上传,`--group` 缺省自动分类 | dept_admin |
| `delete --kb <name> --file <f>` | `delete --kb <name> --file <f>` | 删文件 | system_admin |

### 自然语言 → 命令映射

| 用户说的 | 命令 |
|---|---|
| "查一下 XX 库里 Y 的选型" | `query --kb XX "Y 的选型"` |
| "我有哪些知识库" / "能查哪些库" | `kbs` |
| "XX 库里有没有 Y 的 datasheet" | `files --kb XX`(或直接 `query`) |
| "把这份原理图传到 XX 库" | `upload --kb XX path/to.pdf`(设计类自动归 `设计数据`) |
| "删掉 XX 库里的 Y" | `delete --kb XX --file Y` |

### 文件上传的来源分组(`--group`)

`--group` 缺省时自动按扩展名分类。手动指定可选 10 组之一:文档资料 / 物料数据 / 设计数据 / 网表数据 / 原理图数据 / 测试数据 / 项目管理数据 / 外部数据 / 人员与组织数据 / 未分类。**来源分组决定落哪个 RAGFlow 数据集,扩展名决定走哪条 pipeline**(`.doc/.docx/.pdf`→文档,`.xlsx`→表格,`.edf/.edif`→电路)。

### 鉴权与地址解析

- 令牌优先级:`HDB_TOKEN` 环境变量 > `login` 存的会话(`~/.config/hardware-database/`)。
- API 地址优先级:`HDB_API_URL` > 会话文件 > `http://127.0.0.1:8000`。
- 也可直接用底层 CLI:`uv run hardware-database query --kb <name> "<q>" --json`(见 `references/command-reference.md`)。

---

## 8. 故障

| 状态/错误 | 原因 | 解决 |
|---|---|---|
| `health` → `server_down` | API 服务没起 | `uv run hardware-database-server` |
| `health` → `server_up` (authed=false) | 未登录/令牌过期 | `hardware-database login --user <u>` 或设 `HDB_TOKEN` |
| `API 错误: ...` (401) | 令牌失效 | 重新 `login`;CLI 会提示"令牌可能过期" |
| `API 错误: ...` (403) | 无该 KB 的读/写权限 | 换有权限的 KB;上传需 dept_admin、删除需 system_admin |
| `query` 返回 `summary.status` 非 success | 证据不足/检索失败 | 按 §4 策略 D 换措辞重试,或说明证据不足 |
| `CLI not found` | 未安装 | `uv sync`(仓库根目录) |
| 连接慢/超时 | 首轮 RAGFlow 解析/agent 多轮 | `query` 已设长超时;耐心等 done 事件 |

> 完整命令参考见 `references/command-reference.md`,排错见 `references/troubleshooting.md`。

---

## 9. 注意事项

- **私有数据不出域**:检索走本地 API,RAGFlow key / `.env` / `auth.db` 只在服务侧,本 skill 与 CLI 不持有。
- **写操作要确认**:上传/删除是改库操作,执行前向用户确认 KB、文件、`--group`。
- **结果以 KB 为准**:知识库内容可能与模型记忆冲突,以检索结果为准并说明。
- **中文输出**:用户面向的回答用中文。
