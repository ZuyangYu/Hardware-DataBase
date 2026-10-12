# Docker 打包与部署

把 Hardware DataBase 打成两个业务镜像 + 生产 Compose 覆盖，一条命令起完整生产栈：

| 镜像 | 内容 | 说明 |
|---|---|---|
| `hardware-database-app` | Python 3.12 + uv.lock 全量依赖 + `src/` | API 与全部后台 worker 共用；内置 DroidSansFallbackFull（PDF 中文） |
| `hardware-database-web` | nginx + Vite 构建产物 | 静态站点 + 反代 `/api`、`/health`（SSE 已关闭缓冲） |
| `redis:8-alpine` | 实时平面（**必需**，随编排启动） | 取消快路径 + 跨进程 SSE；只存带 TTL 的加速键（AOF 开、与项目自管实例一致），真相在 PG，无需备份 |

**不在编排内**（属于外部依赖）：RAGFlow、LLM（Ollama 或 OpenAI 兼容 API）。

**组件分工**：PostgreSQL（账本，唯一真相）+ Redis（实时平面）+ `storage/`（文件类数据），
三者都随编排启动，见第 3 节。

```
浏览器 -> web:80(宿主 8080) -> api:8001 ──> RAGFlow / LLM（宿主机或内网其他机器）
                                   └──> hdb-storage 卷（SQLite/归档/导出/日志）
                                   └──> redis:6379（内部网络，不存独有数据）
```

`api` 容器默认按 `src/api/app.py` 的 lifespan 自己拉起 chat/authoring/parse worker、
export worker 和 memory worker，所以业务侧只需要 3 个核心容器；生产默认另起观测组件。

---

## 0. 快速开始（开源用户）

```bash
bash deploy/docker/quickstart.sh
```

生成密钥 → 构建 → 启动生产栈（`postgres → migrate → api → web` +
Collector/Prometheus/Tempo/Loki/Grafana/Phoenix）→ 打印管理员口令。
访问 `http://<主机IP>:8080`，登录后在「系统配置」填 RAGFlow 与模型端点。
资源受限时才使用 `bash deploy/docker/quickstart.sh --without-observability`。
下面的章节是手动方式与全部可选能力（镜像源、离线分发、外部 PG、备份恢复）。

## 1. 一条命令起全套（含 PostgreSQL）

```bash
# 准备两个文件（先做这步；构建本身不需要密码，但启动需要）
printf 'HDB_PG_PASSWORD=%s\n' "$(openssl rand -hex 16)" > deploy/docker/.env
cp deploy/docker/hdb.env.example deploy/docker/config/.env   # 填管理员口令、RAGFlow、模型
mkdir -p deploy/docker/storage

# 构建镜像（首次 5~15 分钟）
docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.build.yml build

# 起生产栈（业务 + 观测，postgres -> migrate -> api -> web 按依赖顺序）
docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.observability.yml up -d
```

访问 `http://<主机IP>:8080`。

启动顺序由编排保证：`postgres`（健康）→ `migrate`（一次性：建控制面 schema +
逐域建表 + 认证域形状校验，失败会拦住 api）→ `api` → `web`。
**migrate 是幂等的**，每次 `up -d` 都会跑一遍，升级时它就是你的"迁移门槛"。

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
  例如 `HDB_HTTP_PORT=9090`、`HDB_APP_IMAGE=hardware-database-app:v1`。观测栈手动启动时还必须
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

生产 Docker 编排默认关闭 API 内嵌 worker，并为 chat/authoring/parse/export/memory
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
| 启动报 `port is already allocated` | 宿主机已有进程占用 8080/8001（如本机直接跑的 dev 服务）；改 `deploy/docker/.env` 的 `HDB_HTTP_PORT`，或停掉本机进程 |
| PDF 中文变方框 | 镜像已内置 Droid 字体；换了自定义基础镜像时需自装 `fonts-droid-fallback` 或设 `RESULT_EXPORT_PDF_FONT` |
| 要开附件 OCR/视觉 | 镜像已内置 `tesseract-ocr`/`poppler-utils`；视觉能力仍需显式允许远程推理 |

## 7. 镜像构建参数

| 参数 | 默认 | 用途 |
|---|---|---|
| `PYTHON_IMAGE` / `NODE_IMAGE` / `NGINX_IMAGE` | `python:3.12-slim-bookworm` / `node:22-bookworm-slim` / `nginx:1.27-alpine` | 换成内网 Harbor 的基础镜像 |
| `APT_MIRROR` / `PIP_INDEX_URL` / `NPM_REGISTRY` | 空（走官方源） | 构建提速 |
| `HDB_INSTALL_EVAL` | `0` | `1` 时装 RAGAS 评估依赖组 |
