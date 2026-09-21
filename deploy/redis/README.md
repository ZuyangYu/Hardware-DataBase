# Hardware DataBase 项目自管 Redis（实时平面）

取消快路径与跨进程 SSE 事件流的加速层。**持久真相永远在 SQLite/PostgreSQL**：
Redis 不可用时实时面按 fail-soft 退化（取消回落到 DB 列，SSE 回落到 DB 轮询），
不影响回答正确性。分布式准入/供应商配额（fail-closed）默认关闭，本栈不涉及。

## 快速开始

```bash
bash scripts/redis.sh up        # 生成密码 -> 起容器 -> 健康检查 -> 写应用 .env
bash scripts/hdb.sh restart     # REDIS_URL 是重启生效项
bash scripts/redis.sh test      # 跑 Redis 门控测试（15 号库）
```

`up` 只在应用 `.env` 的 `REDIS_URL` 为空时写入；已指向其它实例时只提示，
需要用 `FORCE_ENV=1 bash scripts/redis.sh up` 才会覆盖。

## 实例参数

| 项 | 值 | 说明 |
|---|---|---|
| 镜像 | `redis:8-alpine` | 升级时改 compose 并记录兼容性 |
| 监听 | `127.0.0.1:${HDB_REDIS_PORT:-6380}` | 仅宿主机回环；默认 6380 避开已有 6379 实例 |
| 鉴权 | `REDIS_PASSWORD`（必填） | compose 缺失即报错；随机 32 位 hex，存 `deploy/redis/.env`（0600，gitignored） |
| 持久化 | AOF `everysec`，无 RDB | 数据卷 `hardware-database-redis-data`，`down` 不删 |
| 内存 | `maxmemory 256mb` + `volatile-lru` | 实时键全带 TTL，压力下只逐出可再生键 |
| 健康检查 | `redis-cli ping`（compose healthcheck） | `scripts/redis.sh status` 复核 |

## 命令

```bash
bash scripts/redis.sh init      # 仅生成 deploy/redis/.env
bash scripts/redis.sh status    # 容器状态 + PING + 应用侧 REDIS_URL 提示
bash scripts/redis.sh logs      # 最近 80 行日志
bash scripts/redis.sh url       # 打印 REDIS_URL（默认打码，--show 明文）
bash scripts/redis.sh down      # 停止容器（保留数据卷）
```

## 观测与排障

- `GET /health/dependencies`（system_admin）新增 `redis` 项：
  `up` / `degraded`（配置了但连不上，不拖垮就绪）/ `not_configured`（URL 为空）。
- key 约定：`hdb:turn:cancel:{turn_id}`、`hdb:turn:events:{turn_id}`，
  均带 TTL（默认 7200s），事件流 `maxlen 5000`。
- 应用 .env 里的 `REDIS_URL` 写错/Redis 挂了：回答链路照常，只是取消/SSE 变慢；
  `/health/dependencies` 的 `redis` 项显示 `degraded`，SSE 走 DB 轮询兜底。
- 不需要 Redis 时：`scripts/redis.sh down` + 清空 `REDIS_URL` + `hdb.sh restart`。

## 与其它 Redis 实例共存

本机若已有其它 Redis（如手工起的 6379），互不影响：本栈固定发布到
`127.0.0.1:6380`，且 `up` 不会覆盖已指向别处的 `REDIS_URL`。
