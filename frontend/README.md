# Hardware DataBase 前端

硬件数据平台的 Web UI。系统负责管理各部门硬件资产、解析结构化数据与治理权限；对话只是外接检索入口之一。
技术栈与配色照搬企业风 UI frontend-enterprise;不含任何"数字员工"视觉。

- Vite 6 + React 18 + TypeScript + Tailwind 4 + shadcn/ui(radix)+ react-router-dom 7 + sonner
- 后端为本仓 FastAPI(`/api/v1`),dev 下经 vite proxy 转发,无 CORS 问题

## 开发

```bash
# 1. 起后端(本仓根目录;建议 127.0.0.1:8001)
HDB_API_PORT=8001 uv run hardware-database-server

# 2. 起前端(本目录;默认 127.0.0.1:5174)
npm install
HDB_API_PORT=8001 npm run dev

# 也可以显式指定代理目标:
VITE_API_PROXY_TARGET=http://127.0.0.1:8001 npm run dev
```

打开 `http://127.0.0.1:5174`,用后端账号登录(`storage/auth.db` 里的用户)。

## 构建

```bash
npm run build   # tsc -b && vite build,产物在 dist/
```

## 里程碑 1 范围

| 页面 | 路由 | 说明 |
|---|---|---|
| 登录 | `/` | `POST /api/v1/login`,token 存 localStorage,启动时 `whoami` 校验 |
| 知识库列表 | `/kbs` | `GET /api/v1/kbs`;system_admin 只读登记表,进内容页显示治理角色提示 |
| 对话 | `/chat` | 独立侧边栏入口;可不挂载知识库做通用对话,也可选择知识库做 RAG 检索 |
| 知识库工作台 | `/kbs/:kb/files` | 具体知识库内的文件列表、上传、解析任务和结构化结果 |

## 当前功能状态

- 已实现:上传(multipart + source_group)、解析任务、文件删除、KB 权限/创建/删除(dept_admin)
- 已实现:用户/部门/治理面板/日志中心/系统配置/RAGAS 评估(admin)

## 目录

```
src/
  api/client.ts   Bearer 封装 + ApiError + sseStream(fetch 解析 SSE,POST 不能用 EventSource)
  api/types.ts    后端 DTO 镜像(对齐 src/api/schemas.py)
  auth.ts         localStorage session + 角色判断
  components/ui/  shadcn 组件(复制自企业风模板,去掉 i18n 依赖)
  pages/          LoginPage / KbListPage / chat/ChatPage / KbFilesPage
```
