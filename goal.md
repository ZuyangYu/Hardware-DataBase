# Goal

Status: complete
Sandbox final acceptance: incomplete (dev-l1 reference deployment only; see plan §11 and appendix B)
Objective: 按 `docs/superpowers/plans/2026-09-20-mcp-external-plugin-integration.md` 完成 Hardware-DataBase 外部 MCP 插件一期及 T14 二期受控写入通道：默认关闭零行为变更、只读优先、部门+订阅授权、写工具四道门、候选产物不覆盖原件、外部结果不进证据链、全程可观测可审计；用 MCP 专项 pytest、受影响模块回归、前端测试/build、ruff 和方案验收清单逐条验证。
Scope: `src/agents/mcp/**`、`src/agents/runner.py`（统一工具分支）、`src/agents/orchestration/capabilities.py`、`src/agents/tools/runtime.py`、`src/api/routes/plugins.py` 与 schemas/app 注册、`src/observability/*`、`frontend/src/pages/**`、`tests/**`、方案文档进度更新。
Constraints:
- 不得破坏既有语义：默认 `MCP_ENABLED=false` 时零 MCP 连接/工具/事件/审计，既有测试必须通过。
- 禁 stdio/本地进程与 PG 控制面；写入仅允许远程 MCP 候选产物模式；外部结果不写入 `rt.evidence`、不产生引用编号。
- 凭据加密落库且永不回显；`MCP_CREDENTIALS_KEY` 只从环境读取。
- 不改既有认证表结构；MCP 表只进 `AUTH_DB_PATH`。
- T14 已实现；真实 KiCad 内网服务联调与独立人工 review 仍是部署前门禁。
Done when:
1. T4–T14 各自交付物落地，且 MCP 策略、能力位、执行器、API、订阅、观测、E2E 与写入专项测试全部通过。
2. 方案 §10 一期条目与 T14 验收条目逐条有可回读证据。
3. `uv run ruff check .` 干净；MCP 专项测试 + 受影响模块回归通过；全量回归通过或明确记录未验证范围与原因。
4. `goal.md` 的 Progress 记录最终证据（命令与结果摘要）。
Stop if:
- 依赖安装/网络不可用，或需要用户决策才能继续（破坏既有语义、需要生产凭据/外部服务）。
- 同一失败在 3 次修复后仍无进展。
- 需要改变方案已确定的安全边界（stdio、本地进程、原件就地覆盖、PG 迁移）。

Progress:
- 2026-09-20 T0–T3 完成：依赖锁定（langchain-mcp-adapters 0.3.2 / mcp 1.30.0 / cryptography）；`src/agents/mcp/{models,store,validation,cache,client}.py` 落地；MCP 专项 39 项通过，受影响模块回归 73 项通过，ruff 干净。
- 2026-09-20 T4 完成：`policy.py`（快照先于能力位、部门+订阅过滤、服务器上限、只读/停用/名称冲突过滤、prompt 追加）与 `capabilities.py`（`external_tool.invoke` 部署/授权）落地；`tests/test_mcp_policy.py` + `tests/test_capabilities_mcp.py` 13 项通过。
- 2026-09-20 T5 完成：`executor.py`（调用时复查可见性、原子回合配额、mcp governor、超时固定错误码、字符/字节截断、独立诊断、事件、调用审计、audit_failed 降级）与 runner 装配（能力前解析快照、action executor 之后装载、general chat 放行 MCP 事件、tool_label）；`tests/test_mcp_executor.py` 6 项 + `tests/test_agent_mcp_tools.py` 3 项通过；runner/能力/action-plan 回归 53 项通过。
- 2026-09-21 T6+T12 完成：`service.py`（校验/启用前置测试指纹/工具治理投影/订阅规则）、`routes/plugins.py`（`/api/v1/plugins/mcp/*` 目录/CRUD/测试/启停/工具停用/写白名单/订阅/退订/写入同意）、schemas 与 app 注册；`tests/test_mcp_api.py` 6 项通过，MCP 全量 66 项、API 回归 37 项通过。
- 2026-09-21 T8 完成：`record_mcp_*` 低基数指标、`semantic.py` transport 白名单、`health.py` MCP 三态组件、执行器/策略指标接线、事件 payload 扩展、API/worker lifespan 关闭 MCP runtime；`tests/test_mcp_observability.py` 8 项通过。
- 2026-09-21 T9 完成：`tests/test_mcp_e2e.py` 4 项通过——HTTP 治理+订阅 → 真实回合调用桩工具 → 事件/审计/指标/Trace/证据与引用不变；停用后不可见；冷缓存宕机降级（mcp_load degraded）与热缓存连接失败固定错误码；`.env` 变更在 worker turn 边界 reload。
- 2026-09-21 T10 后端完成：`/api/v1/system/status` 的 mcp 组件 `not_configured/degraded/up` 三态在 `test_mcp_api.py` 中断言。
- 2026-09-21 T7/T13 前端完成：`admin/PluginsPage.tsx`（登记/编辑、凭据留空即保留+勾选清除、测试连接、启停、工具停用与写白名单、刷新工具）、`ExtensionsPage.tsx`（目录、订阅/退订、写入同意、数据外发说明、空状态）、侧边栏与路由、系统状态页 `not_configured` 文案；组件测试 8 项，前端全量 183 项通过，`npm run build` 通过。
- 2026-09-21 T11 完成：补齐 `POST /{slug}/refresh` 与 `PUT /{slug}/write-tools`；指标名与 §10 对齐并新增 `hdb.mcp.skipped_total`；`.env.example`、`CLAUDE.md`、`docs/mcp-plugins.md` 更新；§10 一期 13 项逐条回读（§10.1）与发布演练记录（§11）写入方案；新增 `tests/test_mcp_subscription.py` 5/5。
- 2026-09-21 最终证据：`uv run ruff check .` 通过；后端全量 `uv run python -m pytest -q` **2894 passed / 75 skipped / 0 failed**（EXIT=0；含沙箱控制面、扫描器与 MCP 全量）；`git diff --check` 干净；前端 `npm test` 184 passed、`npm run build` 通过；管理 API 8/8、订阅 5/5、MCP/API/E2E/写入专项 39 passed。
 - 2026-09-21 范围说明：Done-when 中的 `tests/test_mcp_subscription.py` 已补；首轮全量唯一失败（导出测试依赖真实模型改写正文）与本功能无关，已最小加固为 `organize=false` 并复跑通过。
 - 2026-09-21 T14 完成：`external_tool.write`、服务器 `write_tools` 白名单、员工 `allow_write`、全局开关、提案/确认/拒绝/过期状态机、加密参数重放、worker 幂等执行、候选产物扩展名/大小/SHA-256 校验、独立存储、审计/指标、REST 下载、扩展页提案区与聊天 SSE 卡片落地；`tests/test_mcp_write_channel.py` 9 项通过，包含分析→候选→下载和原件哈希不变。
 - 2026-09-21 T14 回归证据：MCP/API/E2E/写入专项 39 passed；前端 183 passed、`npm run build` 通过；`uv run ruff check .` 与 `git diff --check` 通过。当时真实 KiCad 服务与独立人工 review 未执行（2026-09-22 已用内网参考服务完成联调，见下）。
 - 2026-09-21 管理页与系统配置修复：`MCP_ENABLED=false` 时「登记服务」按钮不再被前端禁用（后端本就允许登记），横幅提示在「系统配置 → 外部 MCP 扩展」开启；系统配置页新增 MCP 分组（含统一工具路径/governor 前置开关、布尔下拉、数值校验与提示），`PUT /config` 的校验失败改为 400 并返回可操作原因；新增前端 `ConfigPage.test.ts` 2 项与后端 `test_registration_allowed_while_switch_off_but_enable_is_blocked`、`test_config_route_maps_mcp_validation_to_400`；`docs/mcp-plugins.md` 同步说明。
 - 2026-09-22 真实实例联调完成：新增可部署的内网参考服务 `deploy/kicad-mcp/`（FastMCP streamable_http，只读分析 + 候选产物写工具，无 subprocess/shell 透传）与联调脚本 `smoke.py`；在运行实例上完成登记→测试（tools=2）→启用→写白名单→员工订阅→真实回合只读调用→写提案→确认→worker 执行→产物下载（1406 B，SHA-256 与提案一致，落库 `storage/mcp_artifacts/77/<proposal>/candidate.kicad_sch`）。
 - 2026-09-22 可观测性真实抓取验证：Prometheus（`:9090`，target `otel-collector:9464` = up）查得 `hdb_mcp_call_count_total{status=ok}=2`、`hdb_mcp_call_duration_seconds_*`、`hdb_mcp_write_count_total{status=proposed/completed}`、`hdb_mcp_discovery_count_total`、`hdb_mcp_tools_discovered_{read_only,writable}`；标签仅 status/transport/kind 等有界枚举。审计链路 `plugins.mcp.create/test/enable/write_tools/user_subscribe/write_proposal/write_confirm/artifact_received/call` 各 1 行可回读。
 - 2026-09-22 文档与界面：方案新增 §4.13 联调记录、§11 遗留项关闭两项；`docs/mcp-plugins.md` 指向参考服务；管理页横幅与总开关 kill-switch 行为对齐（关闭时测试连接/工具刷新不发起外部连接）。
 - 2026-09-22 备注：Sandbox/Gateway 工作流由并行 agent 在 `src/sandbox/**`、`src/agents/mcp/gateway.py`、`src/api/routes/sandbox.py` 等进行中，本次未改动；其单测 26 项通过。
 - 2026-09-22 沙箱执行平面参考部署（dev-l1）：实现并部署独立 `deploy/sandbox-runner/`（唯一运行时控制面：固定 bundle、任务账本、取消/超时/孤儿回收、内联产物清单）与 `deploy/mcp-gateway/`（MCP 路由/治理 + 只读 stdio MCP service profile）；运行时档位 dev-l1（rootful Docker + 只读 rootfs/cap-drop ALL/no-new-privileges/非 root/资源上限/--internal 无出网），profile digest 与 attestation 写入 `.env`；新增内置结构扫描器 `scanner://builtin/basic-v1`（注册 ref 即生效，外部 ref 保持 fail-closed）与 `tests/test_sandbox_scanner.py` 4 项。
 - 2026-09-22 沙箱验证证据：`deploy/sandbox-runner/smoke.py` 13/13（真实容器执行 + 超时 + 孤儿回收）、`deploy/mcp-gateway/smoke.py` 10/10（真实 stdio MCP tools/call）、`deploy/sandbox-runner/hdb_e2e.py` 10/10（HDB 控制面 → Runner → Broker 落库校验 SHA-256；Gateway attach/discover/invoke）、`deploy/sandbox-runner/api_smoke.py` 19/19（真实 HTTP API：登记/审核/启用 → 任务提交/观察/收集 → 网关挂载/调用 → 文本 Skill 登记/审核/启用）。运行实例 `SANDBOX_ENABLED=true`、`SKILLS_ENABLED=true`、Runner `127.0.0.1:8780`、Gateway `127.0.0.1:8781`；`/api/v1/system/status` 的 `sandbox` 组件为 `up`，文本 Skill 以不可信参考块加载。
 - 2026-09-22 边界：dev-l1 仅适用于 HDB 自有的固定 digest bundle 与固定只读 stdio MCP（单租户）；第三方/模型生成代码仍需 L2/L3 运行时，扫描器为内置结构扫描而非 AV，身份为共享 0600 令牌文件；详见沙箱方案附录 B。
 - 2026-09-23 最终回归（当前工作树 + 运行配置）：后端全量 `uv run python -m pytest -q` **2894 passed / 75 skipped / 0 failed**；前端 `npm test` **184 passed**、`npm run build` 通过；`uv run ruff check .` 与 `git diff --check` 干净；MCP 与沙箱两组冒烟脚本全部通过（KiCad 12 项、Runner 13 项、Gateway 10 项、HDB 控制面 10 项、HTTP API 19 项）。
 - 2026-09-23 沙箱后续实施：Runner/Gateway/HDB 三层绑定并核对 deployment/profile/镜像 ID；scratch 构建消除基础镜像继承的匿名可写卷；两段独立 0600 服务令牌；修正取消时 ASGI 事件循环阻塞、取消与 timeout CAS 竞态，启动对账将丢失执行身份标为 `unknown`。Runner 17/17、Gateway 10/10、HDB 控制面 10/10、HTTP API 22/22（含跨部门拒绝）、真实运行容器超时/取消清理、同名服务镜像篡改拒绝、隔离 Runner SIGKILL 重启对账通过。
 - 沙箱**最终验收未完成**：本机仍只有 rootful Docker dev-l1，且缺 L2/L3 rootless/gVisor/microVM、外部扫描器、短时身份/mTLS、SBOM/签名、生产规模混沌与独立人工 review；保持单租户自有固定负载的使用边界，不能当成生产多租户隔离能力。
 - 2026-09-23 后续现场纠错：Runner 幂等键与请求指纹绑定；Gateway 按稳定 OCI 容器 ID 防同名替换，拒绝额外挂出网网络；HDB 停用/吊销先本地撤权、再验证远端容器回收，失败留 `cleanup_pending`；已测试的 disabled 部署可重启。一次真实 API 冒烟暴露并发收集碰撞，修复 Broker 同 ID/同内容/同归属的幂等落盘并以线程并发测试验证，原 uncertain 任务经对账恢复 `succeeded`，之后 HTTP API 连续三轮 22/22。
 - 2026-09-23 复核与恢复：HDB HTTP API 实测 disable → Runner 容器不存在 → revoke → 原目录重新审核启用；Gateway 对停止失败先解除挂载，Runner 必须确认容器消失。Runner 同名服务使用错镜像、覆盖入口点或连接额外出网网络均在真实 Docker 中被拒绝；修复 Broker 后 API 连续三轮 22/22，随后 disable/重新 enable 再过 22/22。前端 `npm test -- --run` 184 passed、`npm run build` 通过；`kicad-cli`、rootless/gVisor、外部扫描器/SBOM 签名工具在本机均未安装，非特权 user namespace 创建失败。
 - 2026-09-23 环境决策：用户选择“先维持 dev-l1”。当前开关只用于本机自有固定负载参考验证，§11 的 L2/L3、外部扫描器、短时身份/mTLS、SBOM/签名和生产安全验收保持 NO-GO，待提供相应环境再执行。
 - 2026-09-23 边界补充：attach 过程中若本地禁用先完成，HDB 二次核对部署状态并发出远端清理；成功 attach 后健康改为 healthy。运行实例重启 API 并复验 `enabled/healthy`，受影响的专项回归通过。
 - 2026-09-23 后端全量回归（attach 竞态增量之前的工作树）：`uv run python -m pytest -q` **2914 passed / 75 skipped / 0 failed**，43 subtests passed，EXIT=0，耗时 764.48 s。增量 attach/disable 防护另由定向 pytest 验证；`uv run ruff check .`、`git diff --check` 通过。
 - 2026-09-23 可执行 Skill 参考链路：按用户需求新增 draft Skill 到已审核固定 bundle/profile/digest 的独立绑定、管理员 review/test/enable、员工能力+部门范围校验后的 `/skills/available` 与 `/skills/{ref}/run`；管理页可登记、绑定与审核，员工扩展页可提交结构化 JSON 参数并查看任务、下载 committed 产物。任务账本记录 Skill ref+文本 digest，Runner 提交前再核对当前 Skill 与 bundle；调度前若发现 Skill 或能力撤销则标为 rejected；Skill 文本内脚本始终不执行。真实 dev-l1 API→Runner `bundle/report` → Broker 验证 2 个产物 SHA-256，幂等重试和命令参数拒绝通过；既有 HDB 控制面 10/10 与 HTTP API 22/22 回归通过。该验证仅覆盖自有固定 bundle，第三方脚本 Skill 仍是 NO-GO。
  - 2026-09-23 可执行 Skill 回归：后端全量 `uv run python -m pytest -q` **2918 passed / 75 skipped / 0 failed**（43 subtests passed，exit 0，耗时 744.35 s）；全量启动后补充的部门/能力撤销门禁由专项 **57 passed** 覆盖。前端 `npm test -- --run` **186 passed**、`npm run build` 通过；`uv run ruff check .`、`git diff --check` 通过。该功能的生产级安全验收仍遵循上方 NO-GO。
  - 2026-09-23 受信任管理员提交 Skill 包：新增管理员专属 ZIP 上传/源包下载/自动构建及失败重试；HDB 只持久保存和校验源文件，独立 Runner 使用固定离线 Python 模板构建并按镜像 ID 运行，随后 bundle 与 Skill 需分别经管理员 review/test/enable。员工仅查看部门范围内已启用 Skill、按版本添加/移除并提交结构化参数；原有 MCP 插件订阅保持独立。真实 Docker 用临时源包验证固定镜像构建、校验标签及非 root/无网络任务产出 `done.txt`；新增包验证、管理员权限、构建身份、源文件持久性与版本订阅回归，沙箱专项 **62 passed**、前端 **186 passed** 且 build 通过。后端全量尝试在 600 秒时达到约 75% 后超时，未取得本次全量通过结论；L2/L3、第三方脚本与生产验收继续 NO-GO。
  - 2026-09-23 对话 Skill 自动调用与锁定依赖增量：统一工具模式下向 Agent 暴露员工已订阅且被部门/能力/审核链授权的 Skill，调用时通过同一任务账本/调度器/Artifact Broker，聊天轨迹提示任务，校验后的小型文本产物以不可信预览交回 Agent；任务超过 60 秒时返回任务 ID 供后续状态工具查询，取消会请求取消任务。管理员可配置受运维主机允许列表限制的 HTTPS pip 源；包内 `requirements.lock` 必须固定版本和 wheel SHA-256，Runner 再独立校验并在镜像构建阶段安装，执行容器仍无网络且随任务销毁。真实 Docker 使用官方 PyPI 上带哈希的 six 1.17.0 验证依赖构建、非 root/无网络容器导入与产物；MCP 默认关闭专项中的 Agent 工具隔离测试通过。此路径仍仅适用于受信任单租户管理员，构建出网不是生产级网络隔离验收。
  - 2026-09-23 最终本机 dev-l1 验收：重启 HDB API (:8001) 与独立 Runner (:8780)，工作树前端开发服务 (:5176) 已确认返回新版界面；`deploy/sandbox-runner/skill_api_smoke.py` 真实 HTTP/OCI 路径使用管理员配置的官方 PyPI、带 SHA-256 的 six wheel 自动构建，管理员核验源包/镜像并分别 review/test/enable bundle 与 Skill；员工按部门查看/订阅/执行、校验并下载 Artifact Broker 产物，跨部门与未订阅/员工管理请求被拒。Agent 工具以相同授权提交第二个任务并得到 `agent | six=1.17.0` 文本预览；Deep Agents 真正的工具调用循环回归验证下一轮模型可消费该产物。两个真实任务 `task-523dd0b07baa4058889a0de995fbfb30` 与 `task-ddb181b2c81a4c919b49f682cedb7fe8` 均 succeeded、产物 committed，各有六条账本事件，容器与 workspace 均已回收；源包和两个镜像身份持续存在（含第一次冒烟在跨部门状态码断言处中止留下的镜像，首次测试 Skill/bundle 已撤销）。验收员工账户已停用。后端全量 **2932 passed / 75 skipped / 43 subtests passed**（737 秒）；前端 **186 passed**，build、Ruff 与 diff 检查通过。测试未使用真实 LLM 预测选择（使用可控模型验证 Deep Agents 工具回环），第三方不可信脚本/敏感多租户生产隔离继续 NO-GO。
