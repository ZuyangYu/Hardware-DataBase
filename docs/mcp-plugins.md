# 外部扩展操作指南：MCP 插件与 Skill

本指南面向系统管理员、运维和员工，说明远程 MCP 插件、文本 Skill 与沙箱执行 Skill 的启用和使用。
外部扩展默认关闭；MCP 总开关关闭时不会连接服务或向 Agent 装配 MCP 工具。

技术架构、授权与数据流见 [外部扩展技术 Wiki](../wiki/22-external-extensions.md)。
Sandbox Runner/Gateway 的部署要求见 [`deploy/mcp-sandbox/README.md`](../deploy/mcp-sandbox/README.md)、
[`deploy/sandbox-runner/README.md`](../deploy/sandbox-runner/README.md) 和 [`deploy/mcp-gateway/README.md`](../deploy/mcp-gateway/README.md)。

## 适用范围与边界

- **远程 MCP 插件**：HDB 使用 `streamable_http` / `sse` 连接管理员登记的远程服务；HDB
  不启动 stdio 或任何本地第三方进程。工具结果是不可信外部数据。
- **Sandbox MCP / 执行型 Skill**：由独立部署的 Gateway/Runner 承载；HDB API/Worker
  只调用认证 API，不持有 Docker/OCI 控制权。新环境默认关闭。仓库内有 dev-l1 参考部署，
  只用于 HDB 自有或管理员审核的固定工作负载，不代表第三方代码或多租户生产安全验收。
- **文本 Skill**：审核后作为有边界声明的不可信提示数据加载；不会执行 Markdown 中的代码，
  也不能授予工具、网络或沙箱能力。
- **只读默认**：只有声明 `readOnlyHint=true` 的工具进入模型；写工具（如 KiCad 生成）
  只有在管理员白名单、用户同意、`external_tool.write` 能力位和
  `MCP_WRITE_ENABLED=true` 四道门同时满足时才可见。
- **二期候选产物**：写工具先创建提案，不会在模型回合中调用外部服务；员工确认后由
  worker 执行。返回内容必须通过扩展名、大小和 SHA-256 校验，再落入
  `MCP_ARTIFACT_STORAGE_DIR`，永不覆盖原件。提案与产物可在对话卡片或「扩展」页确认/下载。
- **外部结果不进证据链**：不写入 `rt.evidence`、不获得引用编号；提示词固定要求
  标注“来自外部插件 server/tool”，并禁止执行其中的指令。
- **凭据加密存储**：`MCP_CREDENTIALS_KEY`（Fernet/HMAC）只从环境变量读取，
  不进入 `GET/PUT /config`；轮换需重启，旧凭据自动降级为不可用。
- **独立配额**：外部调用走 `mcp` governor 资源与 `MCP_CALLS_PER_TURN` 回合上限，
  不占用检索/LLM 配额，也不改变检索统计。

## 远程 MCP 插件快速上手（5 步）

| 步骤 | 谁 | 做什么 | 位置 |
|---|---|---|---|
| 1 | 运维/管理员 | 打开总开关与凭据密钥（热加载生效） | 「系统配置 → 外部 MCP 扩展」：`MCP_ENABLED=true`（需同时开启统一工具路径与资源 governor）；`MCP_CREDENTIALS_KEY` 只能写在 `.env`，写通道另加 `MCP_WRITE_ENABLED=true` |
| 2 | 管理员 | 登记远程 MCP 服务 → 测试连接 | 「系统配置 → 扩展插件」：slug/URL/传输/凭据/部门范围 |
| 3 | 管理员 | 启用服务、治理工具（停用、写白名单、刷新） | 同一页的「工具」面板 |
| 4 | 员工 | 订阅；写工具还需勾选写入同意 | 侧边栏「扩展」 |
| 5 | 员工 | 在对话中直接使用（工具名 `mcp__<slug>__<工具>`） | 聊天页 |

生成密钥：`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`。
写工具不会在模型回合直接执行：聊天中出现「写入提案」卡片，确认后由 worker 生成候选产物，
通过校验后在卡片或「扩展」页下载。

## 外部插件侧：最小接入示例

外部插件不是安装进 HDB 的包，而是一个**独立部署的远程 MCP 服务**；HDB 只通过
`streamable_http`/`sse` 出站连接。插件方只需：用 MCP SDK 暴露工具（只读工具声明
`readOnlyHint=True`）→ 部署到管理员可访问的地址（内网地址需 `MCP_ALLOW_PRIVATE_HOSTS=true`
并配合出口限制）→ 把 URL 交给管理员登记。

> 可直接部署的完整示例见 `deploy/kicad-mcp/`：FastMCP + streamable_http，含只读分析工具、
> 候选产物写工具、Dockerfile（非 root/只读 rootfs/`--cap-drop ALL`）和全链路联调脚本
> `smoke.py`。生产镜像按 §4.12 用固定 argv 调用 `kicad-cli` 替换内置生成器即可。

```python
# plugin_server.py —— 依赖：pip install "mcp>=1.30"
import base64
import hashlib

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("kicad-tools", host="0.0.0.0", port=8080)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def analyze_schematic(path: str) -> str:
    """只读分析：返回摘要文本。"""
    return f"analyzed: {path}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def create_schematic(title: str) -> dict:
    """写工具：只生成候选产物，返回产物引用，绝不修改原件。"""
    content = f'(kicad_sch (title "{title}"))'.encode()
    return {
        "filename": "candidate.kicad_sch",
        "content_base64": base64.b64encode(content).decode(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": "application/x-kicad-schematic",
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")  # 登记 URL：http://<host>:8080/mcp
```

写工具返回的产物契约（必须同时满足）：

- `filename`：扩展名在 `MCP_ARTIFACT_ALLOWED_EXTENSIONS` 内；
- `content_base64`：候选内容本体；
- `sha256`：内容 SHA-256，不一致按 `artifact_hash` 拒绝；
- `media_type`：可选；大小上限 `MCP_MAX_ARTIFACT_BYTES`（默认 50 MiB）。

返回非产物结构会判 `artifact_missing`；未声明 `readOnlyHint=true` 的工具只有进入管理员
`write_tools` 白名单后才可能被调用。

### 用 API 完成同样的登记（可选）

```bash
TOKEN=<管理员 token>
BASE=http://localhost:8001/api/v1/plugins/mcp
# 登记（默认停用，部门范围默认 none）
curl -X POST $BASE -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug":"kicad","name":"内网 KiCad","transport":"streamable_http",
       "url":"http://10.0.0.8:8080/mcp","department_scope":"all"}'
curl -X POST $BASE/kicad/test -H "Authorization: Bearer $TOKEN"        # 测试连接
curl -X PUT  $BASE/kicad/status -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"enabled":true}'            # 启用
curl -X PUT  $BASE/kicad/write-tools -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"write_tools":["create_schematic"]}'
```

员工侧（写工具需先订阅并同意写入）：

```bash
curl -X POST $BASE/kicad/subscribe -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"allow_write":true}'
curl $BASE/write-proposals -H "Authorization: Bearer $TOKEN"
curl -X POST $BASE/write-proposals/<proposal_id>/confirm -H "Authorization: Bearer $TOKEN"
curl -OJ $BASE/write-proposals/<proposal_id>/artifact -H "Authorization: Bearer $TOKEN"
```

## 管理员操作（系统配置 → 扩展插件）

1. 在 `.env` 打开总开关（需要重启/热加载后生效）：
   `MCP_ENABLED=true`，并配置 `MCP_CREDENTIALS_KEY=<Fernet key>`。
2. 在「扩展插件」页登记服务：slug、名称、传输、URL、凭据（请求头）、部门范围、
   超时、标签。凭据编辑时“留空即保留”，勾选“清除已保存凭据”才会删除。
   总开关关闭时仍可登记、编辑、删除，但不能启用，也不会发起外部连接：连接测试与工具
   发现都会返回 409，除非显式打开独立的 break-glass 诊断开关
   `SANDBOX_DIAGNOSTIC_TEST_ENABLED=true`。启用前需把 `MCP_ENABLED` 设为 true
   （「系统配置 → 外部 MCP 扩展」，或 `.env`）。
   `MCP_CREDENTIALS_KEY` 不出现在配置页，只能写在 `.env`。
3. 点击「测试连接」：真实连接并发现工具，成功后记录当前配置指纹；
   **修改连接字段会自动停用并作废测试结果**，必须重新测试才能启用。
4. 点击「启用」。启用后员工才能在目录中看到该服务。
5. 在「工具」面板中按需停用单个工具，或把写工具加入写白名单（二期生效）。
   员工仍需单独勾选写入同意；白名单不会自动授权写操作。

## 员工操作（侧边栏 → 扩展）

- 浏览管理员启用的、且与本部门匹配的服务；点击「订阅」后才对本人生效。
- 退订、管理员停用/删除服务会立即让工具不可见。
- 页面顶部固定展示数据外发说明；写同意仅在“有写白名单且写通道开启”时出现。
- 「写入提案」区域和聊天卡片展示待确认、执行中、失败及已完成候选产物；已完成产物提供下载和哈希。

## Skill 操作（侧边栏 → 扩展 / 管理后台 → 扩展插件）

### Skill 类型

| 类型 | 内容 | 使用方式 | 是否运行代码 |
|---|---|---|---|
| 文本 Skill | 提示文本，或只含 Markdown/许可文本的 ZIP | 订阅后在本人对话中作为受限参考文本 | 否 |
| 固定任务 Skill | 管理员绑定到已审核固定 bundle 的文本 Skill | 订阅后按固定 bundle 的 schema 提交任务 | 是，在独立 Runner 中运行固定 bundle |
| 上传执行型 Skill | 含 `SKILL.md` 与 `entrypoint.py` 的 ZIP，经 Runner 固定模板构建 | 订阅后在扩展页测试运行，或由 Agent 调用 | 是，仅在审核启用后由独立 Runner 执行 |

文本 Skill 内容一律视为不可信数据。无论谁提交，Skill 的文字都不会自行声明工具、能力或网络权限。

### 员工提交个人 Skill

1. 打开侧边栏「扩展」→「Skill 库」→「上传个人 Skill」，选择文本型或执行型并提交 ZIP。
   账号必须有部门；上传后记录归属于提交者的私有草稿，不会立即对其他员工可见。
2. 文本 ZIP 必须包含且只包含一个 `SKILL.md`，其中 YAML frontmatter 至少有 `name` 和
   `description`；可附带 Markdown 参考资料以及 `LICENSE.txt` / `NOTICE.txt`。其他资产、脚本
   和权限声明不会导入。所有 Markdown 会合并为不超过 16 KiB 的审核文本。
3. 执行型 ZIP 的根目录须包含 `SKILL.md` 与 `entrypoint.py`；入口读取
   `/work/in/parameters.json`，把候选产物写入 `/work/out`。可选 `requirements.lock` 必须
   固定版本并提供 wheel SHA-256。上传动作只保存源码，不会构建或运行代码。
4. 管理员完成审核和启用后，提交者可在「我上传的 Skill」区域添加到自己的对话；已启用的
   Skill 可选择「共享到部门」。共享只扩大同部门员工的可见范围，不会替他们自动订阅。
5. 部门员工在「Skill 库」中添加后，文本型用于对话；执行型可在卡片中输入 JSON 参数并测试，
   也可由对话中的 Agent 在授权范围内调用。执行还需要部署已授予所需沙箱能力。

### 管理员审核与发布 Skill

在管理后台「扩展插件」→「Skill 上传与审核」中查看待审核项。员工个人提交的 Skill 初始为私有，
管理员可下载原始 ZIP、查看 SHA-256、文件清单和规则提示；规则提示仅供人工复核，不是安全结论。

**员工提交的文本 Skill**：检查 `SKILL.md` 和合并后的参考文本，再依次执行「审核 Skill」→
「标记测试通过」→「启用 Skill」。无需 Runner，不具备代码执行能力。

**员工提交的执行型 Skill**：

1. 下载并人工检查源码、参数 schema 和许可。只有管理员点击「审核源包并允许构建」后，Runner 才会构建。
2. 管理员配置的 HTTPS pip 源只允许运维批准的主机。若包含 `requirements.lock`，Runner 会再次校验
   主机和哈希，并在固定模板构建阶段安装锁定 wheel；任务容器运行时不联网，也不动态安装依赖。
3. 检查构建镜像的 bundle digest、image ID、运行档位、能力和 schema。分别推进镜像的
   `draft → reviewed → tested → enabled` 与 Skill 的 `draft → reviewed → tested → enabled`；
   Skill 执行前关联镜像必须已启用。「标记测试通过」表示管理员完成了实际测试，不是系统自动背书。
4. 启用后，提交者可选择共享到所属部门；同部门员工仍需各自添加/订阅。需要执行能力的用户还必须
   拥有部署授权的能力位。

管理员自行登记的文本 Skill 也可从此页手动创建或导入文本 ZIP；管理员上传的执行型 ZIP 会由系统
请求 Runner 构建，之后仍须审核源包、镜像和 Skill 状态。固定 bundle 绑定、目录筛选、撤销和删除
也在此页完成。

### 撤销、退订与删除

- 员工可在「扩展」页移除已添加的 Skill；个人上传者可停止部门共享。
- 管理员撤销 Skill 后，系统会清除订阅并拒绝新的提示注入或任务提交；正在运行的任务仍按任务取消与
  对账流程处理。
- 目录删除只允许对已撤销 Skill 执行；若关联 bundle 尚未撤销或仍有未完成任务，删除会被拒绝。
  删除目录项不清除历史任务、审计记录或原始上传包。

### Skill 配置和排障

| 现象 | 检查项 |
|---|---|
| Skill 库为空 | `SKILLS_ENABLED=true`、管理员已启用、员工部门范围匹配；员工个人 Skill 尚未启用时仅本人和管理员可见 |
| 文本 Skill 没有出现在对话中 | 确认 Skill 已启用、员工已添加、用户仍在原部门且对话上下文允许加载 Skill |
| 执行型 Skill 不可见或无法运行 | 检查 `SANDBOX_ENABLED`、`SANDBOX_BACKEND=runner_api`、Runner 健康/认证、profile digest/attestation、bundle 状态与所需能力 |
| 个人执行 Skill 停留在待构建 | 管理员先下载审查源码并批准源包；带依赖时还需配置获准的 HTTPS pip 源 |
| Skill 任务处于 `unknown` / `reconcile_required` | 先由 Runner/HDB 对账确认执行和清理状态，不要重放为新任务或猜测成功 |


## 观测与审计

- 事件：`tool_started` / `tool_result`（`tool_kind=mcp`，含 server/tool/transport/
  latency/result_bytes/truncated）；装载失败发 `degraded`（stage=`mcp_load`）。
- 审计：`plugins.mcp.create|update|delete|enable|disable|test|tool_toggle|write_tools`
  与 `plugins.mcp.call`、`plugins.mcp.user_subscribe|user_unsubscribe|write_consent`。
- 指标：`hdb.mcp.call_count` / `hdb.mcp.call_duration_seconds` / `hdb.mcp.discovery_count` /
  `hdb.mcp.tools.discovered` / `hdb.mcp.truncated_total`，标签只含
  transport/status/kind 等有界枚举，不含 slug/工具名。
- 系统状态：`/api/v1/system/status` 的 `mcp` 组件三态
  `not_configured`（总开关关闭）/ `degraded`（前置条件缺失、无启用服务或凭据不可用）/ `up`。
- 写入指标：`hdb.mcp.write_count{status}`、`hdb.mcp.artifact_rejected_total{reason}`；写入审计
  action 为 `plugins.mcp.write_proposal|write_confirm|write_reject|artifact_received`。

## 排障速查

| 现象 | 原因与处理 |
|---|---|
| 目录为空 | `MCP_ENABLED=false`、无启用服务、部门范围不匹配 |
| 无法启用 | 未通过当前配置的连接测试，或凭据不可解密 |
| 工具消失 | 管理员停用/移出部门/删除服务，或本人退订；调用时也会二次校验 |
| 调用返回 `code=timeout` | 超过 `MCP_CALL_TIMEOUT_SECONDS` 或服务端超时；检查服务与超时配置 |
| 调用返回 `code=connect_failed` | 服务不可达；冷缓存回合会降级为“无工具”并记录 `degraded` |
| 结果被截断 | `MCP_MAX_RESULT_CHARS` / `MCP_MAX_RESULT_BYTES` 上限，尾缀 `…[已截断]` |
| 写入提案失败 | 检查 `MCP_WRITE_ENABLED`、管理员 `write_tools`、员工写入同意、凭据密钥和 worker 状态 |
| 产物被拒绝 | 查看 `artifact_*` 错误码；检查扩展名、`MCP_MAX_ARTIFACT_BYTES` 与外部 SHA-256 |

## Sandbox Gateway / Runner 配置概览

stdio MCP 与脚本任务不经过上面的远程 MCP 通道，而是走独立部署的 Sandbox
Gateway/Runner。HDB 只登记、审核和调用，不启动进程、不接触 Docker/OCI：

- 管理员接口在 `/api/v1/sandbox/*`：bundle 目录（固定 digest、输入/输出 schema、
  审核/测试/启用/吊销）、Gateway 部署登记（审核后的工具白名单与 schema、订阅）、
  能力授予（`sandbox.*` / `external_tool.*`，只能收窄部署上限）；
- 任务生命周期为 `submit → observe → cancel → collect → reconcile`；只有输出经
  Artifact Broker 的哈希/ACL/扫描/原子 commit 校验后任务才会 `succeeded`，远端
  含糊或失败一律进入对账态；
- 员工调用 Gateway 工具前必须订阅，写工具还需要写入同意与 `external_tool.write`
  能力；远端 discovery 不能扩大 HDB 已审核的工具集合；
- 文本 Skill 仅作为带边界声明的不可信提示数据加载，不改变工具或能力位。

新环境的安全默认值是 `SANDBOX_ENABLED=false`、`SANDBOX_GATEWAY_ENABLED=false`、
`SANDBOX_BACKEND=disabled`。启用执行型 Skill 至少需要独立 Runner、认证客户端、固定运行时
profile digest、runtime attestation 和 Artifact Broker 扫描器引用；启用 Gateway 服务时还要配置
独立 Gateway URL 与身份。任何配置不完整都应保持关闭或 `degraded`，不回退到本地进程/容器。

仓库提供 dev-l1 参考部署并完成固定负载的真实容器验证，但生产多租户、敏感数据、第三方或模型生成代码
仍需 L2/L3 隔离、外部扫描器、短期工作负载身份/mTLS、SBOM/签名和独立验收。部署开关、接口与运行时
限制以 [`deploy/mcp-sandbox/README.md`](../deploy/mcp-sandbox/README.md) 为准。

## 关键配置

见 `.env.example` 的「外部 MCP 扩展」与「Sandbox/Skill」段。按功能分别配置：

- **远程 MCP 只读调用**：`MCP_ENABLED=true`、统一 Agent 工具路径和资源 governor 前置条件；
  如需鉴权，再配置仅从环境读取的 `MCP_CREDENTIALS_KEY`。
- **远程 MCP 写入**：再打开 `MCP_WRITE_ENABLED=true`，并完成工具白名单、员工同意和
  `external_tool.write` 能力授权。
- **文本 Skill**：`SKILLS_ENABLED=true`；只启用文本 Skill 不要求启用 Runner，但仍要审核、
  配置部门范围并由员工订阅。
- **Sandbox 执行型 Skill**：`SKILLS_ENABLED=true`、`SANDBOX_ENABLED=true`、
  `SANDBOX_BACKEND=runner_api`，以及认证 Runner URL、runtime attestation/profile digest 和
  Artifact Scanner 引用。若使用 Sandbox MCP 服务，再设置 `SANDBOX_GATEWAY_ENABLED=true` 与
  Gateway URL/身份。

除远程 MCP 外部连接测试的显式 break-glass 诊断开关外，不要为了排错绕过启用门禁；
生产部署前先对照 [`deploy/mcp-sandbox/README.md`](../deploy/mcp-sandbox/README.md) 完成运行时验收。
