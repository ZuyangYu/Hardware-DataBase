# 内网离线部署：依赖清单

> Docker 路线见 `deploy/docker/README.md`（镜像 + compose，`docker save/load` 即可离线分发）；
> 本清单是「应用原生部署 + PG/Redis 走 Docker」的另一条路线。

面向「目标机无外网」的场景：应用原生部署，PostgreSQL/Redis 走 Docker（若允许）；
**RAGFlow 与 LLM 是外部服务**（内网另一台机器/另一套平台），不在本清单内。

> 状态：清单已核实（2026-09-21，开发机实测）。标 ❓ 的两项需要目标机信息才能定稿。

## 0. 待确认（决定打包路线）

| # | 待确认项 | 影响 |
|---|---|---|
| 1 | ❓ 目标机 OS + 版本（决定 glibc / DEB|RPM） | 依赖能否直接复用现有 wheel |
| 2 | ❓ 目标机架构（x86_64 / ARM） | ARM 必须重新下载/构建 wheel，现有包全不可用 |
| 3 | ❓ 内网是否允许 Docker Engine | 允许 → PG/Redis 用镜像（推荐）；不允许 → 系统包方案（PG+pgvector 较麻烦） |
| 4 | ❓ 内网是否有内部 apt/yum 镜像 | 有 → 系统依赖（字体等）可直接装 |

## 1. 目标机基础（先卡这三条）

| 项 | 要求 | 实测/说明 |
|---|---|---|
| 架构 | **x86_64** | ARM（鲲鹏/飞腾）需换 wheel 源重新构建 |
| glibc | **≥ 2.28** | 依赖里有 22 个 `manylinux_2_28` wheel（pandas/numpy/cryptography/lxml/pillow/aiohttp…）。**CentOS 7 不行**；Ubuntu 20.04+/麒麟 V10/统信 UOS 可以 |
| 系统命令 | `curl`、`pgrep`(procps)、`fuser`(psmisc)、`setsid`(util-linux) | `scripts/hdb.sh` 启停/健康检查必需；`sqlite3` CLI 可选（排障用） |
| 时间同步 | NTP/chrony | 租约、心跳、幂等依赖时钟 |
| 磁盘 | 建议 ≥ 200G | 应用 ~2G + PG 数据 + storage 归档；若 RAGFlow 也在本机另算 |
| 中文字体 | `fonts-droid-fallback`（`/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf`，4.0MB） | 缺失会导致导出 PDF 中文变方框；也可自备 TTF + `RESULT_EXPORT_PDF_FONT` |

## 2. 应用运行时（离线包）

| 组件 | 版本/体积 | 来源 |
|---|---|---|
| Python | **3.12**（开发机 3.12.3） | 系统包或 uv 托管的独立运行时（离线需带 tarball） |
| uv | 0.12.5，**57MB** 单文件 | 官方二进制，直接拷 |
| Python 依赖 | **155 个包**，wheelhouse 约 **400~500MB**（`.venv` 未压缩 861MB） | 由 `uv export` 生成 requirements 后离线下载 |
| 前端产物 | `frontend/dist` **960KB / 8 文件**（已构建，无需 Node） | 直接拷 |
| 应用代码 | 本仓库（`src/`、`scripts/`、`wiki/`、`pyproject.toml`、`uv.lock`），约 10MB | 不含 `.venv`/`storage`/`node_modules` |

**可选**：便携 Node（若要重建前端）`storage/nodeenv` 205MB；构建好的 dist 已够用，通常不必带。

## 3. 数据服务

### 路线 A：Docker（推荐，和开发机一致）

| 镜像 | 体积 | 用途 |
|---|---|---|
| `pgvector/pgvector:pg17` | **627MB** | 控制面账本 + 记忆投影（**必须含 vector 扩展**，普通 alpine 镜像没有） |
| `redis:8-alpine` | **160MB** | 实时面（取消快路径 / 跨进程 SSE）；fail-soft，可后置 |

离线方式：有网机器 `docker save` 成 tar → 内网 `docker load`（压缩后合计约 **300~400MB**）。
配置：`deploy/postgres/`、`deploy/redis/`（含 compose/initdb/密码模板），内网用
`bash scripts/postgres.sh up` / `scripts/redis.sh up` 一键起。

### 路线 B：系统包（不允许 Docker）

| 组件 | 要求 | 备注 |
|---|---|---|
| PostgreSQL | 16/17 | `postgresql-17` + `postgresql-17-pgvector`（Debian/PGDG）或对应 RPM |
| pgvector | 与 PG 版本匹配的扩展 | 无内部镜像源时，需按目标 OS 版本预先下载 .deb/.rpm |
| Redis | 7/8 | `redis-server` 系统包，简单 |

## 4. 外部依赖（内网必须已就绪）

| 服务 | 必需 | 说明 |
|---|---|---|
| **RAGFlow** | 文档功能必需 | 提供 `RAGFLOW_BASE_URL` + `RAGFLOW_API_KEY`；其自身栈（MySQL/ES/MinIO/Redis）不在本清单 |
| **LLM（OpenAI 兼容）** | 必需 | 提供 `AGENT_*` 三类地址/模型/Key；内网没有可用模型时系统无法回答 |
| Embedding（可选） | 记忆语义检索需要 | `MEMORY_EMBEDDING_*`；没有则记忆候选检索降级（不报错） |

## 5. 配置替换清单（`.env`）

| 键 | 内网应改为 |
|---|---|
| `RAGFLOW_BASE_URL` / `RAGFLOW_API_KEY` | 内网 RAGFlow 地址/Key |
| `AGENT_LLM_PROVIDER` / `AGENT_CUSTOM_BASE_URL` / `AGENT_CUSTOM_MODEL` / `AGENT_CUSTOM_API_KEY` | 内网 LLM 端点 |
| `MEMORY_EMBEDDING_*`（可选） | 内网 embedding 服务 |
| `AGENT_CHECKPOINT_BACKEND=postgres` 等五组后端 + DSN | 由 `bash scripts/postgres.sh write-env` 自动写入 |
| `REDIS_URL` | 由 `bash scripts/redis.sh up` 自动写入 |
| `HDB_API_HOST=0.0.0.0`、`HDB_API_CORS_ORIGINS` | 按内网访问方式设置 |
| `AUTH_DEFAULT_ADMIN_USERNAME/PASSWORD` | 首次启动的管理员（密码不能为空/`admin123`） |
| `OBS_ENABLED`（当前 false） | 内网无观测栈就保持 false |

## 6. 首次部署步骤（概要）

```text
1) 目标机基础：装字体、同步时间、开放端口（API 8002 / Redis 6380 / PG 55433 仅本机）
2) 解包应用：Python 3.12 + uv + wheelhouse -> uv sync --offline（或直接复用 venv）
3) 起数据服务：docker load 两个镜像 -> scripts/postgres.sh up / redis.sh up
4) 初始化 PG：全新机器用「先 sqlite 跑一次生成空库 -> load 到 PG -> 切 postgres」，
   全新库可直接用一次性初始化（已实现，容器编排里是 migrate 门槛）：
     uv run python -m src.storage.bootstrap --dsn "$CONTROL_PLANE_DATABASE_URL"
   它会建控制面 schema + 记录版本 + 逐域建表（幂等）；Docker 路线下由
   `migrate` 服务自动执行，见 deploy/docker/README.md。
5) 配 .env（第 5 节）-> bash scripts/hdb.sh start
6) 验收：/health/ready 三项 up；员工账号发一条问答；导出中文 PDF 正常
```

## 7. 打包体积合计（不含 RAGFlow/LLM）

| 部分 | 体积 |
|---|---|
| 应用离线包（wheelhouse + uv + 代码 + dist + 字体） | ~0.6GB |
| Docker 镜像（PG + Redis，压缩后） | ~0.4GB |
| **合计** | **~1.0GB**（加台便携 Node 再 +0.2GB） |
