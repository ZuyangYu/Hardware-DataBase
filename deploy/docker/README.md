# Docker 打包与部署

把 Hardware DataBase 打成两个业务镜像 + 生产 Compose 覆盖，一条命令起完整生产栈：

| 镜像 | 内容 | 说明 |
|---|---|---|
| `hardware-database-app` | Python 3.12 + uv.lock 全量依赖 + `src/` | API、全部后台 worker 与内置 MCP Server 共用；内置 DroidSansFallbackFull（PDF 中文） |
| `hardware-database-web` | nginx + Vite 构建产物 | 静态站点 + 反代 `/api`、`/health`（SSE 已关闭缓冲） |
| `redis:8-alpine` | 实时平面（**必需**，随编排启动） | 取消快路径 + 跨进程 SSE；只存带 TTL 的加速键（AOF 开、与项目自管实例一致），真相在 PG，无需备份 |

**不在编排内**（属于外部依赖）：RAGFlow、LLM（Ollama 或 OpenAI 兼容 API）。

**使用帮助**：桌面侧栏的“使用帮助”在新标签页打开 `/help`。章节、截图和完整手册 PDF
随 `hardware-database-web` 镜像发布，无需独立帮助容器或挂载宿主机的编辑目录。
手册采用独立阅读布局，左侧为章节目录，页头提供本地关键词搜索、返回系统和 PDF 下载；
沿用现有登录校验，阅读页不加载业务侧栏或知识库列表。搜索匹配章节、小节标题和正文，
直接定位到对应小节，索引与 Markdown 同源生成，不需要额外搜索服务或外网连接。
更新时同步 `frontend/src/content/manual/` 与 `frontend/public/help-assets/`，重新构建 web 镜像；
手册资源使用版本目录，已发布路径不原地覆盖。内部 DOCX 不进入前端发布目录。
文档维护者可在安装了中文 TTF 字体的制作环境运行
`uv run python scripts/manual/build_pdf.py`，从网页同源章节和截图生成 PDF；目标部署机无需运行此脚本。
构建后检查 `/help` 及章节锚点刷新、图片预览、PDF 下载、缺失资源 404，阅读资源不依赖外网。

**组件分工**：PostgreSQL（账本，唯一真相）+ Redis（实时平面）+ `storage/`（文件类数据），
三者都随编排启动，见第 3 节。

```
浏览器 -> web:80(宿主 8080) -> api:8001 ──> RAGFlow / LLM（宿主机或内网其他机器）
                                   └──> hdb-storage 卷（SQLite/归档/导出/日志）
                                   └──> redis:6379（内部网络，不存独有数据）
局域网 Agent -> mcp:9100(默认 0.0.0.0) -> api:8001（每用户 hdbmcp_* 令牌）
```

生产 Compose 启动独立的 chat/parse/export/memory/evaluation/quality worker，
API 的自动拉起开关在 Compose 中关闭；各进程通过持久账本协调。生产默认另起观测组件。
内置 MCP Server 与 API 共用 app 镜像，见第 3 节末的「内置 MCP Server」。

---

## 0. 快速开始（开源用户）

```bash
bash deploy/docker/quickstart.sh
```

生成密钥 → 构建 → 启动生产栈（`postgres → migrate → api → web` + 内置 `mcp` +
Collector/Prometheus/Tempo/Loki/Grafana/Phoenix）→ 打印管理员口令。
访问 `http://<主机IP>:8080`，登录后在「系统配置」填 RAGFlow 与模型端点。
资源受限时才使用 `bash deploy/docker/quickstart.sh --without-observability`。
下面的章节是手动方式与全部可选能力（镜像源、离线分发、外部 PG、备份恢复）。

## 1. 一条命令起全套（含 PostgreSQL）

```bash
# 准备两个文件（先做这步；构建本身不需要密码，但启动需要）
printf 'HDB_PG_PASSWORD=%s\n' "$(openssl rand -hex 16)" > deploy/docker/.env
mkdir -p deploy/docker/config deploy/docker/storage
cp deploy/docker/hdb.env.example deploy/docker/config/.env   # 填管理员口令、RAGFlow、模型

# 构建镜像（首次 5~15 分钟）
docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.build.yml build

# 起生产栈（业务 + 观测，postgres -> migrate -> api -> web 按依赖顺序）
docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.observability.yml up -d
```

访问 `http://<主机IP>:8080`。

启动顺序由编排保证：`postgres`（健康）→ `migrate`（一次性：建控制面 schema +
逐域建表 + 认证域形状校验，失败会拦住 api）→ `api` → `web` / `mcp`。
**migrate 是幂等的**，每次 `up -d` 都会跑一遍，升级时它就是你的"迁移门槛"。

### 评测执行与恢复

Compose 默认 `EVAL_EXECUTION_MODE=worker`：管理页的开始/评分请求写入共享评测执行账本，
`worker-evaluation` 消费；创建运行本身不会自动执行。新表由 `migrate` 初始化，
API 与 worker 在 PostgreSQL 上只校验已有 schema（版本 1）。

所有 API/评测 worker 必须挂载同一 `storage/evaluations`，容器内使用相同的绝对路径
`/app/storage/evaluations`。账本保存请求、owner、租约和递增 fencing token；
JSON 状态/快照/报告继续保存在共享目录，写入有跨进程文件锁和 owner 校验。
默认执行租约 60 秒，每 20 秒续租。失联 owner 过期后，独立 worker 自动重新认领已请求运行。

本地开发默认 `EVAL_EXECUTION_MODE=inline`。宿主机独立执行模式可启动：

```bash
EVAL_EXECUTION_MODE=worker uv run python -m src.evaluation.worker --root storage/evaluations
```

状态 GET 不会修复运行。需要手动接管时，管理页使用“恢复失联运行”按钮，或调用
`POST /api/v1/evaluation/runs/{run_id}/recover`：活跃租约返回 409，过期运行转为暂停，
再从断点继续。暂停/取消保留部分报告；部分报告和评分不足不会获得通过门禁。
CLI 使用 `--fail-on-threshold` 时，质量失败退出 2，评分不足退出 3。
既有报告缺少内容/评分规则指纹时可浏览，但不能作为严格可复现基线。

统一备份中的 PostgreSQL 全库与 `storage/` 同批次保留执行账本和评测文件。
恢复应保持相同的绝对挂载路径；改变评测路径会产生新的执行 scope，旧运行需显式重新启动。

### 生产反馈与质量复核

升级前运行 `migrate`（`python -m src.storage.bootstrap`）。它创建 `quality_schema` 版本 1 及
快照、案例、反馈、复核、outbox 表，并补齐全新控制面的级联外键。API 与 `worker-quality`
启动时校验版本和关键表/索引；质量数据库固定跟随 `CONTROL_PLANE_*`，保证终态同事务提交。

- `worker-quality` 独立消费采集和 Phoenix 投影队列，API readiness 要求 quality 角色心跳。
- 本地 API 默认托管质量 worker；独立运行时设 `HDB_API_SPAWN_QUALITY_WORKER=0`。
- `QUALITY_CAPTURE_CONTENT=false` 只保留元数据/哈希及反馈标量，不能共享未采集的固定材料。
- `QUALITY_SAMPLE_RATE=0.01`、`QUALITY_SAMPLE_DAILY_LIMIT=100` 为初始抽样配置；设 rate=0 可停随机采样。
- Phoenix 同步默认关闭。启用时在私密配置中设置 `QUALITY_PHOENIX_SYNC_ENABLED=true`、
  `QUALITY_PHOENIX_ENDPOINT`（服务端可达的 REST 地址）、`QUALITY_PHOENIX_API_KEY` 和 `OBS_PHOENIX_PROJECT`。
  独立宿主机观测 Compose 可使用 `http://host.docker.internal:6006`；跨机按实际网络/认证地址配置。
- 同步只包含受控评分/标签及关联 ID；原始问答、评论、来源标题和参考答案保留在业务账本。
  案例页会显示 completed/pending/failed/disabled/no_span 等投影状态。

```bash
# 明确补采旧终态；GET 不补采，也不会运行评分。
uv run python -m src.quality.worker --once --backfill
# Phoenix 配置恢复后，显式重投失败/未启用的标注，保留稳定 identifier。
uv run python -m src.quality.worker --once --requeue-projections
```

聊天反馈绑定原始回答 revision；复核共享限定为同部门指定员工和所选材料，UI 默认 7 天。
管理员默认只读取脱敏元数据。共享撤销/过期及消息脱敏阻止后续访问；会话删除清理派生质量数据。
完整 PostgreSQL 备份包含质量材料、同步水位和执行租约；恢复后按数据库时间接管，旧 token 仍拒写。
详见 [质量反馈 Wiki](../../wiki/25-production-quality-feedback.md)。

## 固定实验、调用费用与预算

当前升级还需由 `migrate` 初始化 `quality_workflow_*`、`llm_*`（包含预算版本）和 `business_slo_*`。
API/worker 的 PG 运行路径仅校验；旧部署缺表时应先执行显式迁移。

- `/admin/quality` 可冻结经确认案例、评分固定生产回答、执行固定检索证据上的回答级候选实验，
  关联真实 baseline/candidate、发布 revision 与部署后回访。完整 RAG/Agent 来源隔离实验另配置。
- `/admin/costs` 使用持久调用账本，展示未知费用/覆盖率、时间/用途/模型/部门/项目过滤、
  趋势/分币种排行、版本化预算、任务所有尝试分页与 CSV。
- 价格/预算由管理员注册不可变版本；需要真实响应 model/version 和批准价目，缺失保持 unknown。
  预算状态仅使用已知金额下界，不自动停用模型，不宣称已与供应商结算对账。
- cost spool 为共享 storage 中的 `cost-spool/<destination-hash>/<pid-uuid>/`；Linux 自动接管同目标孤儿目录。
  模型路径不等待 DB/磁盘；内存队列中尚未落盘的数据仍可能在 SIGKILL 时丢失。
- 通过覆盖文件启动 Collector 时，`otel-storage-init` 初始化 WAL 卷权限；quality/evaluation worker
  与其他 workers 一同启用 OTLP。self-metrics 8888 为内部端口。
- `HDB_CHAT_SLO_POLICY` 仅在业务明确 version、effective time、deadline/首 token 阈值/target 后启用。
  生产通知接收端、供应商对账、复核案例及容量/24h 长稳态需使用相应实际部署窗口。

详情：[固定实验与费用 Wiki](../../wiki/26-quality-experiments-cost-ledger.md)、
[观测运行手册](../observability/README.md)。

构建加速（构建机仍需能访问源）：

```bash
APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
NPM_REGISTRY=https://registry.npmmirror.com \
HDB_INSTALL_EVAL=1 \
docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.build.yml build
```

### 指向已有 PostgreSQL（不启动 compose 内的 postgres）

不启动 `postgres` 服务，改在 `config/.env` 里显式写 5 组后端与 DSN
（模板里有注释示例），然后：

```bash
docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.observability.yml \
               up -d --scale postgres=0
```

## 2. 内网离线分发（打 tar）

在联网构建机（需 docker 权限；国内源加速见 §1）上打包：

```bash
bash deploy/docker/make-offline-bundle.sh ~/hdb-offline
```

产物目录：`offline-install.sh`、`INSTALL.md`、`VERSION`、`hdb-images.tar.gz(+.sha256)`、
`docker/`、`observability/`。包里不含 `config/.env` 与 `storage/`。
镜像已在本地时加 `--no-build`；只验证包结构用 `--skip-images`；指定版本号用 `--tag <版本>`。

> `docker/` 与 `observability/` 必须保持同级——否则观测栈的 bind mount 找不到配置，
> Docker 会自动建空目录占位，容器反复重启。脚本会校验这点，缺失即报错退出。

内网目标机（无需外网、无需重新构建）：

```bash
tar -xzf hdb-offline.tar.gz && cd hdb-offline
bash offline-install.sh --port 8080    # 校验镜像 → 导入 → 生成配置 → 启动 → 打印口令
```

访问 `http://<目标机IP>:8080`，用 `admin` + 终端打印的口令登录，之后在「系统配置」填
RAGFlow 与模型端点。包内 `INSTALL.md` 有同样说明。

## 3. 配置

- `deploy/docker/config/.env`：应用配置，挂载为容器内 `/app/config/.env`。
  管理员在「系统配置」页保存时会**原子写回宿主机这个文件**，因此该目录必须可写。
  （容器内的 `HDB_ENV_PATH` 就是指向它；镜像层与单文件 bind mount 无法 rename。）
- `deploy/docker/.env`（可选）：只给 compose 做变量插值（镜像 tag、端口），
  例如 `HDB_HTTP_PORT=9090`、`HDB_APP_IMAGE=hardware-database-app:v1`、
  `HDB_MCP_HOST_PORT=9100`；需要只允许本机访问 MCP 时设
  `HDB_MCP_BIND=127.0.0.1`。观测栈手动启动时还必须
  提供 `GRAFANA_ADMIN_PASSWORD` 与 `PHOENIX_DB_PASSWORD`；quickstart 会自动生成并写入该文件。
  若服务器已有观测栈，可同时设置 `OBS_GRAFANA_PORT`、`OBS_PHOENIX_PORT`、
  `OBS_PROMETHEUS_PORT`、`OBS_ALERTMANAGER_PORT`、`OBS_OTLP_GRPC_PORT`、
  `OBS_OTLP_HTTP_PORT`、`OBS_METRICS_PORT` 使用未占用的宿主端口。
- 重启类配置（`HDB_API_HOST/PORT`、`REDIS_URL`、各组 `*_BACKEND/DATABASE_URL` 等）
  改完需要使用生产编排重新创建：
  `docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --force-recreate`。

### 组件分工：PG 管账本，Redis 管实时

- **PostgreSQL**：唯一账本（账号/会话/记忆/导出/文档/项目…），要备份；
- **Redis**：实时平面——取消快路径与跨进程 SSE 事件流。与 PG 一样随应用部署，
  且是**硬依赖**（`REDIS_REQUIRED=true` 默认）：Redis 未配置/不可达时 api 拒绝
  启动、`/health/ready` 判 not_ready，不存在降级成 DB 轮询的运行形态。
  但**不是数据端**：只放带 TTL 的加速键（默认 2 小时），真相在 PG，因此不需要
  备份、不参与对账。代码对 Redis 故障 fail-soft（降级为 DB 轮询/1 秒级取消），
  但部署上它是必需组件，不再提供"关掉"的开关。
- **storage/**：文件类数据（归档/导出/附件字节/派生索引）。

### 内置 MCP Server（9100）

Compose 里的 `mcp` 服务就是 HDB 对外提供的知识库 MCP Server，和 `api` 共用
`hardware-database-app` 镜像，不额外构建。它通过内部地址
`http://api:8001/api/v1` 调用 API，工具集与 `src/mcp_bridge/server.py` 一致。

- **默认地址**：`http://<主机局域网IP>:9100/mcp`，默认发布到 `0.0.0.0:9100`，
  局域网内 agent 可直接访问。
- **必须配置**：在 `config/.env` 设置 `HDB_MCP_HTTP_HOST=<主机局域网IP或域名>`，
  否则 Host 校验只接受回环地址，局域网访问会返回 `421 Invalid Host header`。
  例如 `HDB_MCP_HTTP_HOST=192.168.1.20`。`quickstart.sh` 首次生成配置时
  会自动取当前主机首个 IPv4 写入；用域名/反向代理时再手动改。
- **鉴权**：必须带每用户 `hdbmcp_*` 令牌（`X-HDB-MCP-Token`），由
  「个人 MCP 凭证」或 `scripts/hdb_mcp_user_setup.sh` 生成；不要设置
  `HDB_MCP_HTTP_ALLOW_UNAUTHED=1`。
- **端口/绑定**：宿主端口默认 9100，可在 `deploy/docker/.env` 用
  `HDB_MCP_HOST_PORT` 调整，同时把 `config/.env` 的 `HDB_MCP_HTTP_PORT`
  改成同一端口；需要只允许本机访问时设 `HDB_MCP_BIND=127.0.0.1`。
  如果走反向代理，再把代理域名加入 `HDB_MCP_ALLOWED_HOSTS`。改完重建：
  `docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --force-recreate mcp api`。
- **安全**：局域网暴露时建议用防火墙限制来源网段；跨网段/不可信网络应在
  反向代理终止 TLS，并把 `HDB_MCP_HTTP_SCHEME` 改为 `https`。
- **边界**：MCP 只暴露知识库/表格/网表工具，不启动本地进程；远程写入仍受
  `MCP_WRITE_ENABLED`、工具白名单、员工写入同意和能力位控制。

```bash
# 本机/局域网探活：无令牌返回 401 即表示服务已监听
curl -i http://<主机局域网IP>:9100/mcp
docker compose -f docker-compose.yml -f docker-compose.observability.yml logs -f mcp
```

### 观测（生产默认启动）

默认 Docker 生产部署会通过覆盖文件在同一 Compose 网络启动观测栈，并将 API 指向
`http://otel-collector:4317`。观测导出是 fail-open：Collector 挂了也只丢遥测，不影响业务。

**默认启动：**

```bash
# 或手动（业务与观测栈同一 compose 网络）：
cd deploy/docker
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

`--with-observability` 仍可兼容旧命令；资源受限环境可显式使用
`bash deploy/docker/quickstart.sh --without-observability`。

同网络下应用自动指向 `http://otel-collector:4317`，不需要 `host.docker.internal`；
Grafana 与 Phoenix 数据库口令由 quickstart 生成并写入 `deploy/docker/.env`；手动方式必须先
设置 `GRAFANA_ADMIN_PASSWORD=<口令>` 和 `PHOENIX_DB_PASSWORD=<口令>`，编排不会再接受默认口令。

- Grafana `http://<主机IP>:3000`、Phoenix `http://<主机IP>:6006`、Prometheus `http://<主机IP>:9090`；
- 看板已预置：Hardware DataBase Overview / Production Hardening；
- 遥测导出是 fail-open：Collector 挂了也只丢遥测，不影响业务。

**观测栈放另一台机器**：在观测机起
`docker compose -f deploy/observability/docker-compose.yml up -d`，应用侧把
`config/.env` 的 `OTEL_EXPORTER_OTLP_ENDPOINT` 改成 `http://<观测机IP>:4317`，
再 `docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --force-recreate api`。

## 4. 常用运维

```bash
cd deploy/docker
docker compose -f docker-compose.yml -f docker-compose.observability.yml ps
docker compose -f docker-compose.yml -f docker-compose.observability.yml logs -f api
docker compose -f docker-compose.yml -f docker-compose.observability.yml logs -f mcp
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --force-recreate api
docker compose -f docker-compose.yml -f docker-compose.observability.yml down
# Redis 属于默认业务栈，以上 compose 命令会一并停止/重建；它不可用时 API readiness 会失败

# 备份：PG 账本 + storage 文件；脚本会自动识别 Docker 栈并短暂停止所有写入者
cd ../..
bash scripts/backup.sh create --deployment docker --dir deploy/docker/backups
# 恢复：校验同批次 sha256，先停所有写入者，再恢复 PG 与 storage
bash scripts/backup.sh restore deploy/docker/backups/storage-YYYYMMDD-HHMMSS.tar.gz --deployment docker --yes

# 升级：用新离线包执行（会自动 pg_dump 备份 → 导入镜像 → 重建）
bash offline-install.sh --upgrade --target <旧部署目录>
```

健康检查：`curl http://127.0.0.1:8001/health/live`（API 只绑宿主回环）、
`curl http://127.0.0.1:8080/health/ready`（含依赖探针）。

## 5. 多角色/多副本（可选）

生产 Docker 编排默认关闭 API 内嵌 worker，并为 chat/parse/export/memory/evaluation/quality
各起一个独立服务，避免直接扩容 API 时重复消费同一队列。宿主机单进程开发模式仍可
保留内嵌 worker；多副本 API 部署应明确设置 `HDB_API_SPAWN_*` 为 `0`。

按角色增加副本时，使用独立服务：

```yaml
  worker-chat:
    image: hardware-database-app:latest
    command: ["hardware-database-worker", "--role", "chat"]
    volumes: ["./config:/app/config", "hdb-storage:/app/storage"]
  export-worker:
    image: hardware-database-app:latest
    command: ["hardware-database-export-worker"]
```

多副本时 `MEMORY_SINGLE_WRITER=true` 只跑一个 memory worker；跨主机共享状态需要
先按文档切到 PostgreSQL，并按 `wiki/18-observability-and-ops.md` §4.2 的九条清单
逐项确认（其中 Redis 与 `DISTRIBUTED_ADMISSION_ENABLED` 是多副本的必需项）。

## 6. 故障排查

| 现象 | 原因/处理 |
|---|---|
| 容器起不来，日志提示找不到 `/app/config/.env` | 没建 `config/.env`；`cp hdb.env.example config/.env` 后重启 |
| 提示 `AUTH_DEFAULT_ADMIN_PASSWORD must be set` | 口令留空或仍是 `admin123`，改成 ≥8 位强口令 |
| 管理页保存配置报 500 / `Device or resource busy` | `config/` 目录没挂上（别把 `.env` 写成单文件挂载） |
| 前端能开、问答报连不上 RAGFlow | `RAGFLOW_BASE_URL` 在容器内不可达：用 `host.docker.internal` 或内网 IP，别用 `127.0.0.1` |
| 上传大文件 413 | nginx `client_max_body_size` 已是 512m；反向代理前置还有一层时需同步放大 |
| 前端轨迹卡顿、取消不及时 | 检查 `redis` 是否 healthy；Redis 是生产硬依赖，API 未就绪时不会接收流量 |
| Worker 健康检查失败 | 查看 `docker compose logs worker-chat worker-parse`；API readiness 会列出缺失角色 |
| MCP 返回 401 | 请求缺少有效的 `X-HDB-MCP-Token`；先在平台生成个人 MCP 凭证，确认令牌未被撤销 |
| MCP 返回 421 / `Invalid Host header` | 访问地址与 Host 头未放行：在 `config/.env` 设置 `HDB_MCP_HTTP_HOST`（或 `HDB_MCP_ALLOWED_HOSTS`）后重启 `mcp api` |
| MCP 容器启动失败，提示 `No module named src.mcp_bridge` | 镜像还是旧的（不含 MCP 代码）；用当前源码重新 `docker compose build --no-cache api` 并重建 |
| 启动报 `port is already allocated` | 宿主机已有进程占用 8080/8001/9100（如本机直接跑的 dev 服务或 hdb.sh 管理的 MCP）；改 `deploy/docker/.env` 的 `HDB_HTTP_PORT`/`HDB_MCP_HOST_PORT`，或停掉本机进程 |
| PDF 中文变方框 | 镜像已内置 Droid 字体；换了自定义基础镜像时需自装 `fonts-droid-fallback` 或设 `RESULT_EXPORT_PDF_FONT` |
| 要开附件 OCR/视觉 | 镜像已内置 `tesseract-ocr`/`poppler-utils`；视觉能力仍需显式允许远程推理 |

## 7. 镜像构建参数

| 参数 | 默认 | 用途 |
|---|---|---|
| `PYTHON_IMAGE` / `NODE_IMAGE` / `NGINX_IMAGE` | `python:3.12-slim-bookworm` / `node:22-bookworm-slim` / `nginx:1.27-alpine` | 换成内网 Harbor 的基础镜像 |
| `APT_MIRROR` / `PIP_INDEX_URL` / `NPM_REGISTRY` | 空（走官方源） | 构建提速 |
| `HDB_INSTALL_EVAL` | `0` | `1` 时装 RAGAS 评估依赖组 |
