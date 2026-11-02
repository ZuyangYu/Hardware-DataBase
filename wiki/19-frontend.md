# 19 · 前端（React SPA）

对应代码：`frontend/`（React 18 + TypeScript + Vite 6 + Tailwind 4）。

---

## 1. 技术栈与构建

| 领域 | 选型 | 版本 |
|---|---|---|
| 运行时 | React / ReactDOM | ^18.3.1 |
| 路由 | react-router-dom | ^7.0.2 |
| 样式 | Tailwind CSS + `@tailwindcss/vite` + tw-animate-css + clsx + tailwind-merge + cva | ^4.3.2 |
| 组件原语 | `radix-ui` 伞包 + `@radix-ui/react-slot`（shadcn 风格本地组件） | ^1.6.1 |
| 图标 | lucide-react + 自绘 `AppIcon` | ^1.22.0 |
| Toast | sonner | ^2.0.7 |
| 字体 | `@fontsource-variable/geist` | ^5.2.9 |
| 构建/测试 | vite ^6、@vitejs/plugin-react ^5、typescript ^5.7、vitest ^3.2 | |

**刻意不引入**：Redux/Zustand/Jotai、react-query/SWR、axios、图表库、markdown 渲染库、
i18n 库、ESLint/Prettier/Biome（无 lint 配置）。

脚本：

```json
"dev":     "vite --host 127.0.0.1 --port 5176 --strictPort",
"build":   "tsc -b && vite build",
"test":    "vitest run",
"preview": "vite preview --host 127.0.0.1 --port 4173 --strictPort"
```

`vite.config.ts`：alias `@ → ./src`；dev server host `0.0.0.0`、端口 5176、
`allowedHosts: ['.trycloudflare.com']`（cloudflared 演示）；
代理 `/api` 与 `/health` → `VITE_API_PROXY_TARGET || http://127.0.0.1:${HDB_API_PORT || 8001}`。

环境变量（`frontend/.env.example`）：

| 变量 | 作用 |
|---|---|
| `VITE_API_BASE_URL` | 生产/跨域 API 源；空 → 同源 `/api`（走 dev 代理） |
| `VITE_API_PROXY_TARGET` / `HDB_API_PORT` | dev 代理目标/端口 |
| `VITE_CHAT_ATTACHMENTS_ENABLED` | 附件 UI（默认开） |
| `VITE_CHAT_ATTACHMENT_MAX_FILES` | 每会话附件上限（默认 20，与后端对齐） |

---

## 2. 应用外壳、路由与鉴权

### 2.1 Provider 栈与入口

`main.tsx`：`StrictMode → I18nProvider（no-op 中文桩）→ App`；`App`：
`TooltipProvider → BrowserRouter → Routes` + 全局 `Toaster`（sonner，`richColors closeButton`）。

### 2.2 路由表（`App.tsx`）

| 路径 | 页面 | 守卫 | 角色说明 |
|---|---|---|---|
| `/` | `HomeRedirect` | 登录 | sysadmin → `/admin/governance`；其他人 → `/assets` |
| `/assets` | `DocumentAssetsPage` | `KbContentRoute` | sysadmin 被重定向 |
| `/wiki` | `WikiPage` | `KbContentRoute` | 侧栏不显示，通常经 `/assets?tab=wiki` |
| `/kbs` | `KbListPage` | `KbContentRoute` | KB 注册表；sysadmin 只读 |
| `/chat` | `ChatRoute → ChatPage`（`?kb=`） | `KbContentRoute` + sysadmin 重定向 | sysadmin 不能聊天 |
| `/memory` | `MemoryPage` | 仅登录 | 项目域需要可用 KB |
| `/kbs/:kbName/chat/*` | `KbChatRedirect` | — | 重定向到 `/chat?kb=` |
| `/kbs/:kbName/files` | `KbFilesPage` | `KbContentRoute` | 内容工作台 |
| `/admin/users`、`/admin/departments`、`/admin/kb-mount`、`/admin/config`、`/admin/evaluation` | 对应页面 | `AdminRoute requireSysAdmin` | 仅系统管理员 |
| `/admin/governance`、`/admin/logs`、`/admin/status` | 对应页面 | `AdminRoute` | sysadmin 全局 / employee 本部门 |
| `*` | `HomeRedirect` | — | — |

守卫语义：`AdminRoute` 需 `hasDashboardAccess(user)`（sysadmin 或 employee），
`requireSysAdmin` 时再要求 sysadmin，失败 `Navigate` 到 `/kbs`；
`KbContentRoute` 阻止 system_admin 进入内容页。

### 2.3 侧栏与布局

`Shell`：`SidebarProvider`（可折叠 240px/72px，状态存 `localStorage['hdb_sidebar_expanded']`）
+ `AppSidebar` + 路由出口。

- 普通导航：**资产中心 / 全部知识库 / 对话 / 长期记忆**；
- `anyAdmin`（employee 或 sysadmin）：**治理面板 / 日志中心 / 系统状态**；
  sysadmin 额外：**员工管理 / 部门管理 / 知识库挂载 / 系统配置 / RAGAS 评估**；
- 页头统一 `AppHeader`（标题/描述 + `NotificationBell` + `UserMenu`），聊天页用 `ChatHeader`。

### 2.4 鉴权流（`auth.ts`）

- 登录：`POST /api/v1/login` → 存 `localStorage['hdb_auth_session'] = {token,user}`；
- 启动校验：`GET /api/v1/whoami`（401 清会话；网络错误**保留**会话，`authChecked` 前渲染 `null`）；
- 登出：`POST /api/v1/logout`（尽力）→ 清本地；
- 跨标签登出：`subscribeAuthCrossTab` 监听 `storage` 事件的「非空 → null」；
- 401 处理：`client.ts` 仅在请求**带过 token** 时调用全局 `onUnauthorized`（避免登录失败触发全局登出）；
- 传输：`Authorization: Bearer <token>` 由 `authHeader()` 注入到 JSON/下载/SSE 全部请求；
- 角色工具：`isSystemAdmin` / `isEmployee` / `hasDashboardAccess` / `ROLE_LABELS`。

---

## 3. API 客户端（`src/api/`）

文件：`client.ts`（fetch/SSE/上传）、`types.ts`（≈1500 行，镜像后端 `src/api/schemas.py`）、
`chatAttachments.ts`、`documentAuthoring.ts`。

### 3.1 基础

- `API_BASE = import.meta.env.VITE_API_BASE_URL || ''`；
- `request<T>`：`fetch` + JSON + `authHeader()`；非 2xx → 读文本 → 401 且带凭据时触发全局登出 →
  抛 `ApiError`；
- `ApiError` 解析 FastAPI `detail`（字符串 / `{message,warnings[]}` / 422 数组）；
  特判 Vite 代理的空白 500 →「无法连接后端服务，请确认 API 已在 8001 端口运行」；
- `isAuthError`(401)、`isForbiddenError`(403)；`api.get/post/put/patch/delete`；
- `downloadBlob(path, filename)`：带鉴权 fetch → Blob → 临时 `<a download>`（**延迟 1s 才 revoke**，
  否则会出现 0 字节下载）；
- `uploadFiles`（FormData）与 `uploadFilesWithProgress`（XHR `upload.onprogress` → 百分比，
  并解析错误 `detail`）。

### 3.2 SSE（**不用 EventSource**）

原因：turn 由 POST 创建、且需要 Bearer 头。

- `parseSseResponse`：缓冲分片，按 `\n\n` 切帧，解析 `event:`/`id:`/`data:`（多行 data 拼接），
  默认事件 `message`；非 OK 抛 `ApiError`；`finally` 取消 reader；
- `sseStream(path, body, signal)`：POST JSON 流；
- `sseGetStream(path, signal, lastEventId)`：**持久订阅**：
  - 发送 `Last-Event-ID` 头，逐帧更新游标；
  - 遇终态 `done`/`error` 结束生成器；
  - 网络错误/5xx 按 `[500,1000,2000,4000,8000,12000,20000] ms` 退避重连并**从游标续传**；
    4xx（除 408/429）不重试；abort 感知；
  - 重试预算耗尽 → 「流式连接中断，自动重连失败，请刷新页面继续接收结果」。

**消费的 turn 事件**（`useKbChat.ts`）：
`delta`（追加文本）、`narration`（从累积回答中剥离计划叙述）、
`stage`/`tool_started`/`tool_result`（轨迹步骤，统一经 `api/agentEvents.ts` 解析，工具用 `call_id` 配对）、`degraded`（降级横幅）、
`document_card`/`document_clarification_question`/`document_clarification_ready`（卡片）、
`done`（**权威回答** + footer + `summary.evidence` + `summary.memory_context`）、`error`。

文档任务流：`GET /document-generation/chat-tasks/events?session_id=`（SSE，可重放，
`document_task` 事件）；仅在 SSE 预算耗尽后退化为 10s REST 轮询。

当前会话选择保存在标签页级 `sessionStorage`，key 按用户名与知识库隔离。
刷新时先检查保存的 ID 是否仍在服务端授权列表中，再恢复；被删除或不再可见时回到最新授权会话。
其他标签页新建会话不会改变当前标签页的刷新目标。跨标签页登出仍由 `hdb_auth_session` 的 storage 事件传播。

真实浏览器回归入口为 `HDB_E2E=1 npm run test:e2e`（Chromium）；
`HDB_BROWSER_API_URL` 配置 Vite 代理，`HDB_BROWSER_USERNAME/PASSWORD` 配置验收账户。
用例覆盖断网/刷新、并发创建其他会话后的选择恢复、重复提交、SSE 游标续传、取消与跨 tab 退出。
`vitest.config.ts` 将浏览器用例从单测收集中排除。

### 3.3 轮询节拍

| 功能 | 节拍 | 位置 |
|---|---|---|
| 附件解析状态 | 2s（会话打开时） | `ChatPage.tsx` |
| 单条消息导出任务 | 40 次，500ms 后 1s | `ChatPage.tsx#waitForExportJob` |
| 导出任务中心 | 有任务 5s / 空闲 30s | `ExportTaskCenter.tsx` |
| 通知 | 30s | `NotificationBell.tsx` |
| KB 文件 + 解析任务 | 有活动任务时 3s | `KbFilesPage.tsx` |
| Wiki 蒸馏任务 | 运行中 4s | `WikiPage.tsx` |
| 评估运行详情 | 2s（silent） | `EvaluationPage.tsx` |
| 评估预检 | 600ms 去抖 | 同上 |
| 文档任务兜底 | 10s（SSE 预算耗尽后） | `useKbChat.ts` |

---

## 4. 页面清单（含端点）

### 4.1 `LoginPage`
全屏 Hero + 动画 SVG 数据流背景 + 功能卡 + 可展开登录表单（密码可见性切换）。
端点：`POST /api/v1/login`。

### 4.2 `KbListPage`
分页（10/页）`DataTable` + `StatCard`（总数/可访问/未注册）。
动作：进入对话、内容管理（需 `write|admin`）、所有者「部门共享/改为私有」、
admin 删除、employee 创建。
端点：`GET /kbs`、`POST /kbs`、`DELETE /kbs/{name}`、`PATCH /kbs/{name}/sharing`。

### 4.3 `ChatPage` + `useKbChat`（核心页面）

页面壳很薄，数据/流式逻辑全在 `useKbChat`，渲染在 `chat/components/*`。
布局：`ChatHeader`（标题/KB + wiki 辅助开关 + 自动抽取开关 + 会话同意台账弹窗 +
内嵌 `ExportTaskCenter` + 用户菜单）、`MessageList`（仅在接近底部时自动滚动、骨架、空态）、
队列提示条（`waiting_for_attachments` / `queued_behind_previous_turn` + 截止时间）、
文档状态卡、`Composer`、右侧可拖拽 `ChatSessionSidebar`（180–420px）。

关键行为：

- **轮次**：乐观用户气泡（`id = -Date.now()`）→ 确保会话 → `POST /conversations/{id}/turns`
  （`query_mode` KB 用 `deep`、`__general__` 用 `fast`；带 `document_context`/`attachment_ids`/
  `source_scope`/`wiki_assist`）→ `POST /turns/{id}/start` → SSE。
  `done` 时用 `created.user_message` 校正乐观消息并追加助手消息（含证据/记忆上下文）；
- **恢复**：打开会话时 `GET /conversations/{id}/turns?include_terminal=true`；
  非终态 turn 重新 start 并从 seq 0 重放；历史轨迹与卡片从
  `MessageView.trace_steps` / `.cards` 回填；
- **停止**：只有显式点「停止」才 `POST /turns/{id}/cancel`；
  导航/切 KB/卸载只**断开客户端流**（`chatTaskLifecycle.ts`）；
- **处理过程**：`narration` 仍只从最终答案中剥离，不进入时间线；`stage`/`tool_started`/
  `tool_result` → 按 `call_id` 配对的扁平时间线；工具行显示输入 query/SQL、命中数、来源、
  证据条数、候选条数（未登记、不可引用）、`empty_reason`（零命中原因）。完成后 5s 自动隐藏，流式文本 64ms 节流 flush；
  刷新后从 `MessageView.trace_steps` 恢复同一时间线。
- **证据/引用**：`EvidenceBlock`（默认折叠，`[n]` + 文件名 + 3 行预览 + 定位：sheet+cell / 页码 /
  chunk / 设计）；记忆上下文单独以 `[M n]` 渲染；
- **附件**：左侧「+」，`CHAT_ATTACHMENT_ACCEPT = .pdf,.docx,.txt,.md,.xlsx,.xlsm,.edf,.edif`；
  上传按需建会话；chip 显示解析状态与重试；解析失败阻止发送；已选附件跨轮保持；
- **导出菜单**：每条助手消息 `ExportMenu`（按 `GET /api/v1/exports/formats` 过滤），
  后台任务逐消息跟踪预览/下载，另有全局任务中心；
- **记忆控制**：会话自动抽取开关、「重新提炼」、创建个人记忆（opt-in 提示 + 同意事件）、
  「记入项目记忆」、会话同意台账（可撤销）；
- **403 页**：KB 列表/消息 403 时整页「无法访问该知识库」。

端点（页面 + hook + 组件）：

```
GET /whoami
GET /kbs                                         (Shell)
GET /kbs/{kb}/wiki/stats
GET /exports/formats ; GET /exports?session_id= ; GET /exports/{id} ; POST /exports
GET /artifacts/{id}/download ; GET /artifacts/{id}/preview
GET/POST /conversations ; DELETE /conversations/{id} ; GET /conversations/{id}/messages
PATCH /conversations/{sid}/messages/{mid}
GET /conversations/{id}/turns?include_terminal=true ; POST /conversations/{id}/turns
POST /turns/{id}/start ; POST /turns/{id}/cancel ; GET /turns/{id}/events (SSE)
GET /conversations/{id}/memory-summary ; PUT /conversations/{id}/memory-settings
GET /memory-consents?session_id= ; POST /conversations/{id}/memory-consents ; DELETE /memory-consents/{id}
POST /conversations/{id}/extract-memory ; GET/PUT /memory-settings
附件: GET/POST /conversations/{id}/attachments ; DELETE .../{aid} ; POST .../{aid}/retry
文档桥: POST /document-generation/templates/analyze-from-attachment
        POST .../sessions/{sid}/messages ; GET .../sessions/{sid}
        GET .../work-orders/{woid}/status ; GET .../chat-tasks/current?session_id=
        GET .../chat-tasks/events?session_id= (SSE)
        POST .../artifacts/{aid}/release-request ; GET .../artifacts/{aid}/download
        POST .../sessions/{sid}/confirm-plan        (hook 已实现，页面当前无按钮接入)
```

### 4.4 `KbFilesPage`（内容工作台，约 1750 行）

头部：KB 名/可见性/所有权 + 「全部知识库」「打开对话」；有写权限（非 sysadmin 且 `write|admin`）时
显示上传卡（来源分组选择、多文件、按分组的扩展名白名单、512MB 批量预检、XHR 进度）
与解析任务列表（进度条、阶段文案、「停止并删除」「清理完成」）。

7 个标签页（首次激活懒加载）：**文件 / Excel 台账 / 电路设计 / 模块树 / 测试数据 / 原理图 / 外部对话**：

- 文件：`DataTable`（状态/处理器标签）、分块查看弹窗（`ParseResultView`）、admin 删除；
- Excel 台账：总量与逐文件 sheet 详情；
- 电路设计：设计列表 + 详情（概要/模块/网络/器件/交叉引用）+ `net_query`/`instance_query` 过滤 + 解析日志；
- 模块树 / 测试数据（报告 + 测量项查询，limit 100）/ 原理图（设计 → 页面、标签/模块区域/截图/PDF 缓存）；
- 外部对话：列表 + 轮次/块详情、「AI 摘要」重生成、删除。

端点：

```
GET /kbs ; GET/POST /kbs/{kb}/files ; DELETE .../files/{fileId} ; GET .../files/{fileId}/chunks
GET .../parse-tasks ; DELETE .../parse-tasks/{id|finished}
GET .../structured/spreadsheets | circuit-designs | circuit-designs/{id}
| circuit-designs/{id}/parse-log | modules | test-reports | test-measurements
| schematics | schematics/{id}/pages/{page}
GET .../external-conversations ; GET .../{id} ; POST .../{id}/summary ; DELETE .../{id}
```

### 4.5 `MemoryPage`
域切换「我的记忆 / 项目记忆」（项目需选 KB）、状态过滤（含全部状态值）、opt-in 勾选、
记忆卡（版本/来源数/投影状态/可展开来源与审计）。
候选动作（编辑 PATCH / 审核通过 / 驳回 / 替代 / 删除）按 `scope === 'user' || isEmployee(user)` 显示；
确认用 `window.confirm/prompt`（与全站其它 `ConfirmDialog` 不一致）。
个人同意区可撤销。

### 4.6 `WikiPage`
KB 选择（或内嵌模式）+ 过滤（query/page_type/status）+ 分页表（标题/slug/类型/摘要/链接/版本/更新时间）
+ 详情弹窗（markdown 内容、反向链接、修订）+ 编辑（乐观版本）/归档/恢复/回滚 +
质量报告（健康分/统计/问题）+ 蒸馏（`focused|standard|exhaustive`，`max_pages_per_ingest` 0–2000，
4s 轮询阶段与统计）。`?open=<slug>` 深链打开后清除参数。

### 4.7 `DocumentAssetsPage`（资产中心）
`?tab=` 三标签：**文档资产 / 知识 Wiki / 知识图谱** + KB 选择。
资产标签：查询/类别过滤、`DataTable`（生命周期/类别徽章、项目、文档号、版本/生效版本、标签含影子档过滤）、
详情（版本链、事件时间线、出入关系、Wiki 引用）、编辑元数据、从已完成文件添加版本、增删关系链接。
图谱标签：`WikiGraph`（pages/documents 视图，limit 200）、客户端类型过滤、点击节点跳转 Wiki 详情。
`canWrite = permission write|admin || isEmployee(user)`（**客户端放宽**，后端仍强制）。

### 4.8 管理页

| 页面 | 功能与端点 |
|---|---|
| `UsersPage`（sysadmin） | 用户表（角色/部门/状态）、创建、启停（禁止操作自己）、重置密码；`GET/POST /users`、`PUT /users/{id}/active`、`PUT /users/{id}/password`、`GET /departments` |
| `DepartmentsPage`（sysadmin） | 部门表/创建/删除（`system`/`系统` 客户端保护）；`GET/POST /departments`、`DELETE /departments/{id}` |
| `KbMountPage`（sysadmin） | 选 KB → 指定部门 + 可选 owner 员工；`PUT /kbs/{name}/assign`；`?kb=` 深链 |
| `GovernancePage`（sysadmin + employee） | KB 治理汇总（owner/部门/文件/解析/失败/员工数/注册与物理标记/`issue_flags`）+ 统计卡 + 「挂载」跳转 |
| `LogsPage`（sysadmin + employee） | 审计标签（动作过滤/关键词/limit 300/统计/详情 JSON）与查询标签（状态/关键词、trace 表含后端/类型/延迟/OTel id + Grafana/Phoenix 深链、证据弹窗）；`GET /logs/audit[+stats/actions]`、`GET /logs/query[+stats]`、`GET /logs/query/{id}/evidence` |
| `ConfigPage`（sysadmin） | 分组配置（RAGFlow/鉴权/模型/检索/文档灰度/可观测/提示词）、密钥掩码（`***` 不变）、provider 条件字段、数值范围校验、**仅提交变更键**、RAGFlow/LLM 健康探针、重置默认；`GET/PUT /config`、`GET /health/ragflow`、`GET /health/llm` |
| `EvaluationPage` + `EvaluationDashboard`（sysadmin） | 数据集上传/摘要、KB 绑定、在线/离线、样本/标签过滤 + 去抖预检、创建与启动、运行列表/详情、暂停/继续（continue\|restart）/取消/评分/删除、2s 静默轮询、基线对比（严格开关）、指标柱状 vs 阈值、可信度与评分注意事项 |
| `SystemStatusPage`（sysadmin + employee） | 总体状态、依赖卡、LLM 并发门槛统计、按通道 token 用量、24h 任务计数；`GET /system/status` |

### 4.9 任务中心与通知

- `ExportTaskCenter`：浮层（有活动任务时）或内嵌（聊天头部）；列出 ≤30 个任务
  （格式/状态/时间/会话、下载/重试/取消）；5s/30s 轮询；
- `NotificationBell`：30s 轮询、未读徽标（99+）、面板内单条/全部已读；
  `attachment_revision` 类型点击跳 `/chat`；失败静默。

---

## 5. 关键组件

| 组件 | 说明 |
|---|---|
| `MarkdownMessage` / `renderMarkdownBlocks` / `renderInlineMarkdown`（`chatHelpers.tsx`） | 手写 GFM 渲染器：围栏代码（交 `CodeBlock`）、h1–h4、引用、hr、管道表格（含对齐）、ul/ol、行内代码/粗体/链接/图片（图片仅显示标签）、CJK 友好换行；SVG 代码块用 `SvgFigure`（Blob URL `<img>`，**不执行脚本**；>200KB 退回源码）；流式文本**不逐帧重解析** |
| `CodeBlock` | 自研 JSON/Python/Markdown 分词器 + VS Code 风格 CSS 变量配色 |
| `MessageBubble` | `memo`；用户气泡（纯文本 + 脱敏态 + 附件 chip）；助手 = markdown + 记忆面板 + 证据面板 + 动作行（写记忆/导出）+ 检索概览折叠 + 导出任务预览；流式变体含降级横幅、轨迹、光标 |
| `StepGroup` / `EvidenceBlock` / `MemoryContextBlock` / `ExportJobs` / `ExportPreview` | 轨迹时间线（完成后折叠、流式中展开）、引用查看器、记忆上下文、导出任务行与预览（text_preview/paragraphs/sheets/warnings） |
| `ExportMenu` | 按服务端能力列表过滤的格式下拉 |
| `Composer` | KB 挂载选择、附件 chip（状态/重试/删除/作为模板）、自适应高度 textarea（最大 200px）、Enter/Shift+Enter、发送/停止、附件上限、模板分析进度提示 |
| `ChatHeader` / `ChatSessionSidebar` / `ChatEmptyState` | 头部（开关/台账/导出中心/用户菜单）、可拖拽会话列表（删除在流式中禁用）、空态示例问题 |
| `WikiGraph` | 零依赖 SVG 近似力导（按度数同心环）、节点色按类型、边箭头、前 40 节点标签、点击跳 slug |
| `DataTable` / `StatCard` / `Paginator` / `ConfirmDialog` / `UserMenu` / `AppIcon` / `BrandLogo` | 通用企业 UI 组件 |
| `ui/*` | shadcn 风格封装：alert-dialog、badge、button、card、dialog、dropdown-menu、input、label、pagination、scroll-area、select、separator、sheet、skeleton、sonner、table、textarea、tooltip、sidebar、`app-toast.tsx`（`notify.success/error/warning/info/loading/dismiss`） |
| `templateMappingCorrection.ts` | 模板分析纠错请求的纯构造器（**仅被单测引用**，无 UI 页面消费） |

---

## 6. 状态与数据获取模式

- **无全局 store**：页面各自 `useState`，聊天用一个大型自定义 hook（`useKbChat`）；
  跨页共享靠 `Shell` 传 props（KB 列表、认证）或重新拉取；
- **拉取模式**：`useCallback` loader + `useEffect`（`cancelled`/`disposed` 标志）
  → `setLoaded(false)` → fetch → `setState` → `finally setLoaded(true)`；
  错误走 `notify.error`（403 时进入 forbidden 页态）；
- **缓存/失效**：无正式缓存；`Shell` 缓存 KB 列表直到 `onRefresh`；
  页面在挂载/过滤变化/变更后重新拉取（如上传后 `Promise.all([loadFiles(), loadTasks()])`）；
  后台轮询用 `silent` 标志避免骨架闪烁；
- **乐观更新**：聊天用户消息、`done` 时的助手消息、导出任务、消息编辑、通知已读、
  用户启停、文档放行请求（先 `pending` 再 REST 校正）、Wiki 详情合并 PATCH 响应；
- **流式**：SSE delta 存 ref，最多每 64ms flush 一次到 state；
  轨迹用函数式 `setState`；用 `AbortController` + 单调 `streamTokenRef` 防止旧流覆盖新会话；
- **错误**：`ApiError.message` → sonner；`isForbiddenError` → 整页态；
  全局 401 → 强制重登；若干后台轮询**故意吞错**（通知/导出中心/记忆摘要/附件列表）；
- **并发守卫**：`sendingRef`、
  `attachmentMutationVersionRef`、导出中心的 `busyIds`。

---

## 7. 常量、特性开关与权限重复

**特性开关 / 常量**

```
VITE_AGENT_DOCUMENT_TOOLS_ENABLED / VITE_DOCUMENT_AUTHORING_CHAT_ENABLED  → isDocumentAuthoringChatEnabled()（默认开）
VITE_CHAT_ATTACHMENTS_ENABLED → isChatAttachmentsUiEnabled()（默认开）
VITE_CHAT_ATTACHMENT_MAX_FILES（默认 20，对齐后端）
DOCUMENT_CONTEXT_VERSION = 1 ; DOCUMENT_CONTEXT_TTL_MS = 30min（服务端权威）
GENERAL_CHAT_KB_NAME = '__general__'
ATTACHMENT_STATUS_POLL_INTERVAL_MS = 2000 ; STREAMING_RENDER_INTERVAL_MS = 64
QUERY_TRACE_HIDE_DELAY_MS = 5000 ; DOCUMENT_TASK_POLL_INTERVAL_MS = 3000（兜底 10000）
SVG_FIGURE_MAX_CHARS = 200KB ; PAGE_SIZE = 10 ; 上传批量上限 512MB
RELEASED_EXPORT_FORMATS = md/xlsx/docx/pdf/pptx ; CHAT_ATTACHMENT_ACCEPT = .pdf,.docx,.txt,.md,.xlsx,.xlsm,.edf,.edif
localStorage 键: hdb_auth_session / hdb_sidebar_expanded / hdb.wikiAssist.<kb|__general__> / hdb-eval-last-dataset
```

**客户端权限检查（与后端重复）**

- 路由守卫（`AdminRoute` / `KbContentRoute` / `AuthedApp`）；
- `KbListPage`：创建仅 employee；内容仅 `write|admin`；删除仅 `admin`；共享仅 owner；
  sysadmin 被阻止进入内容并给 toast；
- `KbFilesPage`：`canWrite = role !== 'system_admin' && (write|admin)`；
  `canAdmin = role !== 'system_admin' && admin`；
- `WikiPage` / `DocumentAssetsPage`：`canWrite = write|admin || isEmployee`（**最宽**，
  会在只读/共享库上显示写操作入口，后端仍拒绝）；
- `MemoryPage`：治理按钮仅 `scope==='user' || isEmployee`；
- `GovernancePage`：employee 文案为「本部门」，「挂载」按钮对 employee 可见但会被守卫重定向；
- `DepartmentsPage`：`system`/`系统` 不可删；`UsersPage`：不可操作自己。

---

## 8. 测试与构建

- `npm test` → `vitest run`；`src/**` 下 21 个测试文件（api client、documentAuthoring、
  chat hook/model/components、导出结果模型、任务生命周期、队列提示、markdown SVG、
  KB 工作台、记忆视图模型、评估 dashboard、ExportTaskCenter、NotificationBell 等）；
- 无 `vitest.config`，Vitest 默认 `node` 环境；依赖 DOM 的测试用
  `react-dom/server` 的 `renderToStaticMarkup` 而不是 jsdom；
- `npm run build` = `tsc -b && vite build`；单 `tsconfig.json`（strict、noEmit、
  `moduleResolution: bundler`、`@/*` 别名）；
- 无 ESLint/Prettier/Biome；无 Playwright/E2E。

---

## 9. 已知缺口与观察

1. **SSE 而非 EventSource**（设计使然）：需要服务端事件持久化 + `Last-Event-ID` 续传。
2. 手写 markdown/SVG/高亮避免了依赖与注入风险（链接仅 http(s)；SVG 用 Blob `<img>` 而非 `innerHTML`），
   代价是功能面较窄（无图片渲染、无语法高亮库）。
3. 权限重复较多，`|| isEmployee` 是最宽松的一条。
4. i18n 是桩（`t` 恒等），全部文案硬编码中文；时间格式化
   （`lib/timezone.ts`）把后端无时区后缀的时间当 UTC，按浏览器时区渲染（回退 `Asia/Shanghai`）。
5. `system_admin` 被刻意排除在 KB 内容与聊天之外（路由守卫 + 侧栏 + `Shell` 返回空 KB 列表）。
