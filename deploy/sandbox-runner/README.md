# Sandbox Runner（参考部署，dev-l1 档）

独立的沙箱执行服务：**只有它可以控制 OCI 运行时**。HDB API/Worker 与 MCP Gateway
通过受认证的 JSON HTTP 调用它；调用方不能传 shell、命令、容器参数、宿主路径或环境变量。

## 运行时档位：dev-l1（重要边界）

| 项 | 值 |
|---|---|
| 后端 | 本机 rootful Docker 29.x + runc |
| 隔离 | 只读 rootfs、`--cap-drop ALL`、`no-new-privileges`、非 root 用户、`--pids-limit 128`、`--memory 256m`、`--cpus 1`、`tmpfs /tmp` |
| 网络 | task：`none`；service：`--internal` 网桥（无出网，仅宿主可达） |
| 挂载 | 仅任务自己的输入（只读）与输出（读写）目录；无宿主路径/socket |
| 适用 | HDB 自有的固定 digest bundle 与固定只读 stdio MCP，单租户 |
| 不适用 | 第三方/模型生成代码、多租户高敏感数据（需 L2 rootless OCI 或 L3 gVisor/microVM） |

本机没有可验证的 rootless OCI / gVisor / Kata 档位，非特权 user namespace 创建失败，
且无法非交互 sudo，因此仅选择 L1 加固档。
profile 与 digest 记录在 `runtime-policy.dev-l1.yaml`（HDB 侧通过
`SANDBOX_RUNTIME_PROFILE_DIGEST` 固定）。

## 组成

```
deploy/sandbox-runner/
├─ runner.py                     # FastAPI 服务：任务账本 + 真实容器执行 + 产物清单
├─ bundles/report/               # 固定 bundle：确定性报告（stdlib）
├─ bundles/slow/                 # 固定 bundle：超时/取消验证
├─ Dockerfile                    # 无继承 VOLUME 的 scratch + 固定 Python 运行时
├─ service-profiles.json         # Runner 自己审核的服务/profile/镜像 ID 目录
├─ runtime-policy.dev-l1.yaml    # 运行时档位与 digest 来源
├─ smoke.py                      # Runner 契约 + 真实运行时验证（17 项）
├─ runtime_acceptance.py         # 运行中挂载/限制、超时及取消后的回收
├─ runtime_restart.py            # 隔离 Runner SIGKILL/重启与未知执行对账
├─ runtime_service_tamper.py     # 拒绝同名错镜像/改入口点/额外出网网络
├─ hdb_e2e.py                    # HDB 控制面 <-> Runner/Gateway 端到端（10 项）
├─ api_smoke.py                  # HDB HTTP API 端到端（22/24 项，含跨部门拒绝）
└─ secrets/token                 # 0600 服务令牌（不要提交）
```

## 启动

```bash
# 1) 一次性：离线构建并确认 ID。重建后必须更新 bundle.json 的 image_digest，
#    重新审核 HDB 目录；运行时按 ID 执行，不按可变标签执行。
cd deploy/sandbox-runner && docker build -t hdb-sandbox-bundle:1.0 .
docker image inspect hdb-sandbox-bundle:1.0 --format '{{.Id}}'

# 2) 独立服务令牌（0600）；HDB→Runner/HDB→Gateway 与 Gateway→Runner 分开
mkdir -p secrets && umask 077 && head -c 32 /dev/urandom | base64 | tr -d '\n' > secrets/token
mkdir -p ../mcp-gateway/secrets && umask 077 && head -c 48 /dev/urandom | base64 | tr -d '\n' > ../mcp-gateway/secrets/runner-token
cd ../..

# 3) 启动（非 root；root 身份会被拒绝）
# 冒烟实例使用 8 秒，验证完成后重启为正常的 120 秒。
setsid nohup env SANDBOX_RUNNER_TASK_TIMEOUT_SECONDS=8 \
  uv run python deploy/sandbox-runner/runner.py \
  >> storage/logs/sandbox-runner.log 2>&1 < /dev/null &

# 4) 验证
uv run python deploy/sandbox-runner/smoke.py
uv run python deploy/sandbox-runner/runtime_acceptance.py # 测试实例 timeout=8（小于 slow bundle 的 30 秒）
uv run python deploy/sandbox-runner/runtime_restart.py    # 隔离端口、账本和容器标签
uv run python deploy/sandbox-runner/runtime_service_tamper.py # 先禁用参考 Gateway 服务
uv run python deploy/sandbox-runner/hdb_e2e.py   # 需要 HDB 沙箱环境变量
uv run python deploy/sandbox-runner/api_smoke.py # 需要 HDB API 已启用沙箱
```

## 接口（`/v1`，Bearer + 对应 `X-Audience`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/tasks` | 提交固定 bundle（校验 bundle digest/profile/参数 schema） |
| GET | `/tasks/{id}` | 观察状态/执行身份/产物清单 |
| POST | `/tasks/{id}/cancel` | 请求取消；容器与工作区清理确认后终结为 cancelled |
| POST | `/tasks/{id}/collect` | 返回内联产物清单（base64 + sha256 + execution_id） |
| POST | `/reconcile[/{id}]` | 清理孤儿容器 |
| POST | `/services` | Gateway 独立身份；必须匹配 Runner 的 deployment/profile/镜像 ID 目录 |
| GET | `/services/{ref}` | 逐次复核运行实例的镜像、挂载和隔离参数 |
| DELETE | `/services/{ref}` | 停止 service profile |

## 运维

- 任务状态持久化在 `state/runner.db`；启动及 `POST /v1/reconcile` 对账孤儿；
  重启后已丢失执行身份的任务标为 `unknown`，不会猜测成功。
- 幂等键在账本中唯一且与参数/调用身份的请求指纹绑定；旧账本自动加列，
  缺少指纹的历史请求无法作为新提交重放，需走观察/对账。
- 镜像 ID 分别固定在 `bundles/*/bundle.json` 与 `service-profiles.json`；
  Runner 核对镜像源标签、无继承 VOLUME、Docker seccomp/AppArmor、服务入口点、
  环境及唯一内网；以实际 OCI 容器 ID 生成稳定服务身份，Gateway 调用时复核实例。
- 日志：`storage/logs/sandbox-runner.log`；镜像：`hdb-sandbox-bundle:1.0`。
- 新增 bundle：把固定代码放到 `bundles/<name>/`（`entrypoint.py` + `bundle.json`，
  含 `entrypoint_path`、输入 schema 与 digest 算法），重建镜像，并在 HDB 侧以
  `sha256(entrypoint.py)` 注册新 digest。
- **生产替换项**：升级到 L2/L3 运行时、外部产物扫描器（替换
  `scanner://builtin/basic-v1`）、工作负载身份（mTLS/短时令牌）与真实混沌/重启测试。
