# Docker 打包与部署

把 Hardware DataBase 打成两个镜像 + 一份 compose，一条命令起完整后端：

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
export worker 和 memory worker，所以单机只需要 3 个容器。

---

## 0. 快速开始（开源用户）

```bash
bash deploy/docker/quickstart.sh
```

生成密钥 → 构建 → 启动（`postgres → migrate → api → web`）→ 打印管理员口令。
访问 `http://<主机IP>:8080`，登录后在「系统配置」填 RAGFlow 与模型端点。
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

# 起（postgres -> migrate -> api -> web 按依赖顺序）
docker compose -f deploy/docker/docker-compose.yml up -d
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
docker compose -f deploy/docker/docker-compose.yml up -d --scale postgres=0
```

## 2. 内网离线分发（打 tar）

在联网构建机上构建完成后，把镜像存成 tar，连同**整个 `deploy/docker/` 目录**
（compose、initdb、环境模板、quickstart）拷到内网：

```bash
# 构建机（需 docker 权限；国内源加速见 §1）
docker compose -f deploy/docker/docker-compose.yml \
               -f deploy/docker/docker-compose.build.yml build
docker save -o hdb-images.tar \
  hardware-database-app:latest hardware-database-web:latest \
  pgvector/pgvector:pg17 redis:8-alpine
tar -czf hdb-docker.tar.gz hdb-images.tar deploy/docker

# 内网目标机（无需外网、无需重新构建）
tar -xzf hdb-docker.tar.gz && cd deploy/docker
docker load -i ../../hdb-images.tar
bash quickstart.sh --no-start      # 生成 PG 密码 + 管理员口令（打印在终端）+ config/.env
vi config/.env                     # 填 RAGFLOW_BASE_URL/API_KEY、模型端点（内网地址）
docker compose up -d               # postgres + redis -> migrate(自动建表) -> api -> web
```

访问 `http://<目标机IP>:8080`，用 `admin` + quickstart 打印的口令登录。

> **四个镜像都要 save**：app、web、pgvector（PG 账本）、redis（实时平面，硬依赖）。
> `quickstart.sh` 在离线机务必带 `--no-start`——否则它会尝试构建（需要外网）。
> 配置里指向宿主机上的 RAGFlow / Ollama 时用 `host.docker.internal`（compose 已加
> `extra_hosts: host-gateway`，Linux 同样可用）；指向内网其他机器直接写 IP。

## 3. 配置

- `deploy/docker/config/.env`：应用配置，挂载为容器内 `/app/config/.env`。
  管理员在「系统配置」页保存时会**原子写回宿主机这个文件**，因此该目录必须可写。
  （容器内的 `HDB_ENV_PATH` 就是指向它；镜像层与单文件 bind mount 无法 rename。）
- `deploy/docker/.env`（可选）：只给 compose 做变量插值（镜像 tag、端口），
  例如 `HDB_HTTP_PORT=9090`、`HDB_APP_IMAGE=hardware-database-app:v1`。
- 重启类配置（`HDB_API_HOST/PORT`、`REDIS_URL`、各组 `*_BACKEND/DATABASE_URL` 等）
  改完需要 `docker compose up -d --force-recreate`。

### 组件分工：PG 管账本，Redis 管实时

- **PostgreSQL**：唯一账本（账号/会话/记忆/导出/文档/项目…），要备份；
- **Redis**：实时平面——取消快路径与跨进程 SSE 事件流。与 PG 一样随应用部署，
  且是**硬依赖**（`REDIS_REQUIRED=true` 默认）：Redis 未配置/不可达时 api 拒绝
  启动、`/health/ready` 判 not_ready，不存在降级成 DB 轮询的运行形态。
  但**不是数据端**：只放带 TTL 的加速键（默认 2 小时），真相在 PG，因此不需要
  备份、不参与对账。代码对 Redis 故障 fail-soft（降级为 DB 轮询/1 秒级取消），
  但部署上它是必需组件，不再提供"关掉"的开关。
- **storage/**：文件类数据（归档/导出/附件字节/派生索引）。

## 4. 常用运维

```bash
cd deploy/docker
docker compose ps
docker compose logs -f api
docker compose up -d --force-recreate api     # 改完 config/.env 后重启
docker compose down                           # 停止，数据保留在卷里
# 启用了 Redis 的话，以上 compose 命令都要带 --profile redis（否则它不会被停止/重建）

# 备份：PG 账本 + ./storage 文件（用容器跑，root 拥有的 0600 文件也能读）
docker compose exec -T postgres pg_dump -U hdb -Fc hdb > backups/pg-hdb-$(date +%F).dump
docker run --rm -v "$PWD/storage:/data:ro" -v "$PWD/backups:/backup" alpine \
  tar -czf /backup/storage-$(date +%F).tar.gz -C /data .
# 恢复：先停 api，再 pg_restore + 解包 storage
docker compose stop api
docker compose exec -T postgres pg_restore --clean --if-exists -U hdb -d hdb < backups/pg-hdb-YYYY-MM-DD.dump
tar -xzf backups/storage-YYYY-MM-DD.tar.gz -C . && docker compose up -d

# 升级：构建机出新的 app/web 镜像 tar -> 内网 load -> 重建
docker load -i hdb-images-new.tar && docker compose up -d
```

健康检查：`curl http://127.0.0.1:8001/health/live`（API 只绑宿主回环）、
`curl http://127.0.0.1:8080/health/ready`（含依赖探针）。

## 5. 多角色/多副本（可选）

默认三个 spawn 开关都是 `1`，worker 跑在 api 容器内。要拆开或扩副本，在
compose 覆盖文件里给 `api` 设 `HDB_API_SPAWN_{WORKER,EXPORT_WORKER,MEMORY_WORKER}=0`，
再按角色起容器：

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
| 前端轨迹卡顿、取消不及时 | 没启用 Redis 属**预期行为**（轨迹靠 DB 轮询、取消延迟 ~1s）；想更快见第 3 节启用 `--profile redis` |
| 启用了 Redis 但没生效 | 两处都要改：`config/.env` 的 `REDIS_URL` 非空 **且** 用 `--profile redis` 启动；`docker compose ps` 应看到 redis healthy |
| 启动报 `port is already allocated` | 宿主机已有进程占用 8080/8001（如本机直接跑的 dev 服务）；改 `deploy/docker/.env` 的 `HDB_HTTP_PORT`，或停掉本机进程 |
| PDF 中文变方框 | 镜像已内置 Droid 字体；换了自定义基础镜像时需自装 `fonts-droid-fallback` 或设 `RESULT_EXPORT_PDF_FONT` |
| 要开附件 OCR/视觉 | 镜像未装 `tesseract-ocr`/`poppler-utils`，需要自己加一层再开 `CHAT_ATTACHMENT_{OCR,VISUAL}_ENABLED` |

## 7. 镜像构建参数

| 参数 | 默认 | 用途 |
|---|---|---|
| `PYTHON_IMAGE` / `NODE_IMAGE` / `NGINX_IMAGE` | `python:3.12-slim-bookworm` / `node:22-bookworm-slim` / `nginx:1.27-alpine` | 换成内网 Harbor 的基础镜像 |
| `APT_MIRROR` / `PIP_INDEX_URL` / `NPM_REGISTRY` | 空（走官方源） | 构建提速 |
| `HDB_INSTALL_EVAL` | `0` | `1` 时装 RAGAS 评估依赖组 |
