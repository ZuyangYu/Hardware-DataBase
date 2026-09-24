# 07 · 模型层、并发与资源治理

对应代码：`src/core/model_factory.py`、`chat_model_runtime.py`、`llm_governor.py`、
`model_gateway.py`、`multimodal_gateway.py`、`llm_headers.py`、
`src/core/concurrency/`（`resource_governor.py` 2125 行）、`src/api/admission.py`。

---

## 1. 分层职责

```
业务代码（agent / 文档生成 / 记忆 / 评估 / 外部对话）
   │  create_chat_model(profile=..., priority=...)    ← 唯一构造入口
   ▼
model_factory          配置解析 + 缓存 + profile 化 + 上下文窗口声明
   ▼
model_gateway          受治理适配器 + 用量账本（token 统计）
   │   _GovernedChatOpenAI / _GovernedChatOllama（持有 governor 槽位）
   ▼
llm_governor           进程内准入（interactive vs batch）+ 部署级配额许可
   ▼
chat_model_runtime     调用边界 + 观测（span/指标/token/结构化输出兼容）
   ▼
langchain-openai / langchain-ollama → 真实 LLM
```

---

## 2. `model_factory.py`（275 行）

### 2.1 配置对象

```python
ChatModelConfig(
    profile, provider, model, endpoint, api_key,
    max_tokens, timeout, temperature, max_retries,
    require_structured_tools=False, max_input_tokens=0,
    priority=PRIORITY_INTERACTIVE,
)
_PROFILE_NAMES = {"default", "authoring_auxiliary", "template_suggestion",
                  "external_conversation", "memory"}     # 未知 profile 抛 ValueError
```

> **不存在 deep/fast 两套模型**。`query_mode` 只影响提示词/工作流、`top_k` 与 recursion_limit；
> 两种模式都走 `create_chat_model(profile="default")`。

### 2.2 provider 与配置解析

```python
normalize_provider(...)   # settings 里的 openai → "custom"；合法值 ollama|custom
resolve_chat_model_config(*, profile, provider="", model="",
                          settings_module=None, priority=interactive)
```

| 维度 | custom | ollama |
|---|---|---|
| model | `AGENT_CUSTOM_MODEL` | `AGENT_OLLAMA_MODEL`（默认 `qwen2.5:32b`） |
| endpoint | `AGENT_CUSTOM_BASE_URL` | `AGENT_OLLAMA_BASE_URL`（默认 `http://localhost:11434`） |
| api_key | `AGENT_CUSTOM_API_KEY` | `""` |
| 传输重试 | `AGENT_RATE_LIMIT_MAX_RETRIES`（4） | 0 |
| 公共 | `max_tokens=AGENT_CUSTOM_MAX_TOKENS`(4096)、`temperature=AGENT_TEMPERATURE`(0.2)、`timeout=AGENT_TIMEOUT_SECONDS`(120)、`max_input_tokens=AGENT_MODEL_MAX_INPUT_TOKENS`(65536) | |

profile 覆盖：

| profile | 覆盖 |
|---|---|
| `authoring_auxiliary` | timeout 20s、retries 0 |
| `template_suggestion` | timeout 60s |
| `external_conversation` | timeout = `EXTERNAL_CONVERSATION_LLM_TIMEOUT_SECONDS − 5`（≥1）、retries 0 |
| `memory` | provider/model/key 优先 `MEMORY_MODEL_*`；timeout `MEMORY_REFLECTION_TIMEOUT_SECONDS`(120)；`require_structured_tools=MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS`(true) |

### 2.3 缓存与失效

```python
create_chat_model(provider="", model="", profile="default", priority=interactive)
    # lru_cache(maxsize=32)；priority 参与缓存键（interactive/batch 各一份）
_build_cached_chat_model(generation, config)   # 二级缓存
notify_settings_reloaded()                     # bump _SETTINGS_GENERATION + 清两级缓存
create_chat_model_for_settings(settings_module, ...)   # 可注入 settings 的变体（测试/评估）
```

构建路径：`build_governed_model(...)`（custom 时附 `opencode_extra_headers(endpoint)` 请求头）
→ `_apply_model_profile(model, max_input_tokens)`：当 `max_input_tokens > 0` 且 provider 未声明窗口时
写入 `model.profile = {"max_input_tokens": N}`，**激活 deepagents 的 `SummarizationMiddleware`**
（85% 触发 / 保留 10%）。旧版 langchain-core 上赋值失败时 fail-soft。

---

## 3. `chat_model_runtime.py`（701 行）——调用边界与观测

### 3.1 错误类型

```
ChatModelRuntimeError
├── ModelInvocationError        # 底层调用失败
├── TextResponseError           # 文本为空
├── StructuredOutputCapabilityError  # 模型不支持结构化输出
└── StructuredOutputValidationError  # 结构化输出校验失败
```

### 3.2 API

| 函数 | 行为 |
|---|---|
| `invoke_text(model, messages, *, operation, profile, **kw) -> TextCallResult(text, response, usage, provider, model, operation, profile)` | 包裹调用 + 活跃计数；空文本抛 `TextResponseError` |
| `invoke_structured(model, schema, messages, *, operation, profile, text_fallback=None, **kw)` | 优先原生结构化输出；能力错误且提供 fallback 时降级为文本路径；结果标注 `native` / `text_compat` |
| `stream_text(...)` | 单次流式观测（含 TTFT），异常包装为 `ModelInvocationError`；含 "timeout" 时记超时指标 |
| `invoke_observed(runnable, messages, *, operation, profile, identity_model=None)` | 保留完整 provider 响应（用于 tool-bound runnable） |
| `instrument_chat_model(model, *, operation, profile)` | 仅包裹真实 `BaseChatModel`（测试替身不动） |

代理类 `RuntimeObservedChatModel(BaseChatModel)`：`_generate` 走 `invoke_observed`；
`bind_tools` / `with_structured_output` 返回 `_RuntimeObservedRunnable`（每次调用都被观测）。

### 3.3 观测产出

| 类型 | 名称 |
|---|---|
| span | `hdb.llm.runtime`（属性 `provider/model/stage(operation)/profile/streaming`） |
| 指标 | `hdb.llm.calls`、`hdb.llm.duration`、`hdb.llm.ttft`、`hdb.llm.timeout_total`、`hdb.llm.tokens{kind=input|output}`、gauge `hdb.llm.active_requests` |
| 观察值 | `hdb.usage_returned`（bool）、`llm.token_count.prompt/completion/total`、`gen_ai.usage.*` |

`extract_text` 兼容 str/Mapping（`content/text/output_text/delta`）/list/对象 `.content|.text`；
`normalize_usage` 兼容 `usage_metadata` / `usage` / `token_usage` / `response_metadata`
（含嵌套 `token_usage`），统一映射 input/output/total。

---

## 4. `llm_governor.py`（276 行）——进程内 LLM 准入

### 4.1 概念

```python
PRIORITY_INTERACTIVE = "interactive"     # 用户交互请求
PRIORITY_BATCH = "batch"                 # wiki 提炼 / 记忆反思 / 评估裁判 / 导出整理
CURRENT_LLM_PRIORITY: ContextVar         # 默认 interactive
ProviderQuotaExceeded(TimeoutError)      # RPM/TPM 超限
LLMQueueTimeoutError(TimeoutError)       # 队列等待超时
```

### 4.2 `LLMGovernor.slot(priority, *, provider="", model="", estimated_tokens=0)`

1. 计算 `total = LLM_MAX_CONCURRENCY`(4)、`batch_cap = min(LLM_BATCH_MAX_CONCURRENCY`(1)`, total)`、
   `timeout = LLM_QUEUE_TIMEOUT_SECONDS`(180；0=无限)；
2. 在 `Condition` 上等待准入（等待粒度 `min(0.25, remaining)`）：
   - interactive 需要 `active < total`；
   - batch 需要 `active_batch < batch_cap and active < total`；
   - 超时 → `LLMQueueTimeoutError`（带 active/queued 诊断），`_timeouts` 计数；
3. 等待 ≥ `LLM_QUEUE_WARN_SECONDS`(10) 时 WARNING 日志（同一类告警 30 秒内最多一次）；
4. **部署级许可**：`governor_acquire("llm", interactive=not is_batch)`；
   失败释放进程槽位并向上传播（**不静默绕过**）；
5. **提供商配额**：`_check_provider_quota(provider, model, estimated_tokens)`：
   `allow_provider_call` 返回 None/False → `ProviderQuotaExceeded`（fail-closed）；
   有 TPM 时再查 `allow_provider_tokens`；
6. `yield` 期间持有槽位；`finally` 释放部署许可 + 进程槽位 + `notify_all` + 复位 ContextVar。

`snapshot()` → `{max_concurrency, batch_max_concurrency, active, active_interactive, active_batch,`
`queued, queued_interactive, queued_batch, completed, timeouts, max_wait_ms}`
（系统状态页展示）。

> 作用域是**进程内**：worker 进程有独立预算；多副本需要部署级治理（`RESOURCE_GOVERNOR_SCOPE=deployment`）。

---

## 5. `model_gateway.py`（636 行）——受治理适配器与用量账本

### 5.1 UsageLedger

- 线程安全累计：`{calls, prompt_tokens, completion_tokens, total_tokens, usage_returned_calls}`，
  分 `_total` / `_by_channel` / `_by_model`；
- 同时持久化到 SQLite（`LLM_USAGE_LEDGER_DB` 或 `AUTH_DB_PATH`）：

```sql
llm_usage_ledger(channel, model, calls, prompt_tokens, completion_tokens, total_tokens, updated_at)
```

- 提供 `snapshot()`、`get_persisted_usage()`（跨进程总量）、`get_usage_ledger()` / `reset_usage_ledger()`。

### 5.2 受治理模型类

`_GovernedChatOpenAI(ChatOpenAI)` / `_GovernedChatOllama(ChatOllama)`
（字段 `hdb_priority/hdb_provider/hdb_model`）：

- `_generate` / `_stream`（同步）与 `_agenerate` / `_astream`（异步，经 `asyncio.to_thread` 进入上下文）
  **整次调用持有一个 governor 槽位**；
- `_stream` 额外用 `_iter_with_idle_timeout(..., LLM_STREAM_IDLE_TIMEOUT_SECONDS)`：
  pump 线程 + 有界队列；空闲超时关闭迭代器并抛
  `TimeoutError("LLM stream idle for more than Ns")`（0 = 禁用）；
- 预估 token：`_estimated_call_tokens = max(1, (len(repr(messages)) + 3)//4 + max_tokens)`
  （用于 TPM 准入，保守）；
- `_UsageLedgerCallback(channel=priority, model=...)` 在 `on_llm_end` 记录 token
  （兼容 `llm_output.token_usage/usage/usage_metadata` 与逐 generation 的 `usage_metadata`）。

### 5.3 构造入口

```python
build_governed_model(*, provider, model, temperature=0.2, max_tokens=4096,
                     max_retries=4, timeout=120, base_url="", api_key="",
                     default_headers=None, client_kwargs=None,
                     priority=interactive)          # 唯一权威构造入口
build_chat_model(...)                            # 基于 settings 的便捷壳
govern_ragas_llm(llm)                            # 评估裁判：包裹 generate/agenerate 为 batch 槽位
governance_status() / reset_governance_status()
    # {"assembled", "bypassed", "reason", "wrapped_methods"}
    # 绕过时指标 hdb.eval.governance_bypassed；评估摘要会记录 bypassed（不得用于生产验收）
```

> RAGAS 的 instructor 适配器要求真实 `AsyncOpenAI` 实例，所以治理必须包在 **LLM 对象**上而不是 client 上。

---

## 6. `multimodal_gateway.py`（173 行）——视觉分析网关

```python
VisualAnalysis(text, provider, model, request_id="")
MultimodalModelGateway.analyze(*, question, image_bytes, text_context="")
VolcengineArkGateway(base_url, api_key, model, timeout_seconds=None)
default_multimodal_gateway() -> gateway | None
```

- 协议：`POST {base_url}/responses`，body
  `{"model", "input":[{"role":"user","content":[{"type":"input_text","text":prompt}, {"type":"input_image","image_url":dataURL}]}]}`；
- 默认提示词为中文硬件描述模板，附加页面文本（≤2400 字符）；
- 解析 `output_text` 或 `output[].content[].text`；空图片/空输出抛错；
- **双开关**：`CHAT_ATTACHMENT_VISUAL_ENABLED` **且** `CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED`
  且 provider ∈ `{"", "volcengine_ark", "ark"}` 且 base_url/model/`ARK_API_KEY` 齐备，否则返回 `None`。

---

## 7. 资源治理（`src/core/concurrency/`）

### 7.1 部署级许可（`resource_governor.py`）

概念：每个「资源」有并发上限与交互保留额；调用方 `acquire(resource, interactive, timeout)`
拿到有租约的许可；超时/饱和有明确拒绝码。

| 变量 | 默认 | 说明 |
|---|---|---|
| `RESOURCE_GOVERNOR_ENABLED` | `false` | 总开关（**见下方注意**） |
| `RESOURCE_GOVERNOR_SCOPE` | `process` | `process`(进程内) / `deployment`(跨进程，需 PG) |
| `RESOURCE_GOVERNOR_BACKEND` / `_DATABASE_URL` | `sqlite` / `""` | 配额存储 |
| `RESOURCE_GOVERNOR_REQUIRE_PRECONFIGURED` | `false` | 拒绝自动创建配额库 |
| `RESOURCE_GOVERNOR_DB_PATH` | `storage/resource_quotas.db` | SQLite 配额库 |
| `RESOURCE_GOVERNOR_DEPLOYMENT_ID` | `local` | 部署身份 |
| `RESOURCE_GOVERNOR_MAINTENANCE_SECONDS` | `5` | 维护扫描 |
| `RESOURCE_QUEUE_MAX_WAIT_SECONDS` / `QUEUE_MAX_DEPTH` | `30` / `256` | 队列边界 |
| `RESOURCE_PERMIT_LEASE_SECONDS` | `300` | 许可租约 |
| `RESOURCE_UNCERTAIN_ESCALATE_SECONDS` | `600` | 不确定许可升级 |
| `RESOURCE_LLM_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `4` / `2` | LLM 许可 |
| `RESOURCE_RAG_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `8` / `4` | RAGFlow 许可 |
| `RESOURCE_TOOL_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `16` / `0` | 工具许可 |
| `RESOURCE_CHAT_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `4` / `0` | 会话许可 |
| `RESOURCE_HEAVY_JOB_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `4` / `1` | 重任务（文档生成等） |
| `RESOURCE_OCR_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `1` / `0` | OCR |
| `RESOURCE_EXPORT_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `4` / `0` | 导出 |
| `RESOURCE_MEMORY_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `1` / `0` | 记忆反思 |

指标：`hdb.resource.active/queue.waiting/capacity/reserved/uncertain`、
`hdb.chat.reserved_capacity`、`hdb.governor.lease_lost`；拒绝码
`deadline | queue_full | cancelled | uncertain_escalated`。

> ⚠️ 代码中大量调用点写的是 `getattr(settings, "RESOURCE_GOVERNOR_ENABLED", True)`，
> 而该属性**总是存在且默认 False**，因此默认部署下这些门是「惰性」的：
> 相关分支只有显式打开开关才生效。排障时不要假设许可在起作用。

### 7.2 请求准入（`src/api/admission.py` + `core/concurrency/admission.py`）

作用面：**API 边界**（不是模型边界），按 `(user, tenant, resource)` 记滑动窗口 RPM 与未终态 turn 数。

| 变量 | 默认 |
|---|---|
| `ADMISSION_CONTROL_ENABLED` | `true` |
| `ADMISSION_CHAT_RPM_PER_USER` / `_PER_TENANT` | `30` / `120` |
| `ADMISSION_HEAVY_RPM_PER_USER` / `_PER_TENANT` | `10` / `40` |
| `ADMISSION_{DEEP,DOCUMENT,EXPORT,OCR,EVALUATION}_MODEL_RPM_PER_USER/_PER_TENANT` | `""`（继承） |
| `ADMISSION_RETRY_AFTER_SECONDS` | `5` |
| `ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION` / `_PER_USER` | `8` / `32` |
| `DISTRIBUTED_ADMISSION_ENABLED` / `_TTL_SECONDS` | `false` / `120`（Redis L2；失败 fail-closed） |
| `PROVIDER_RPM_LIMIT` / `PROVIDER_TPM_LIMIT` | `0`（关闭） |

### 7.3 其它限流原语

- `rate_limiter.py`：令牌桶（进程内，用于工具/资源细分）；
- `distributed_limiter.py`：Redis 支撑的跨实例计数（准入 L2、提供商配额）；
- `admission.py` 控制器：`allow(user, tenant, resource)` 决策 + 拒绝原因枚举。

---

## 8. 配置与排障对照

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 请求排队很久才失败 | `LLM_QUEUE_TIMEOUT_SECONDS`(180) 内没轮到槽位 | 调大并发或超时；看 `/system/status` 的 `llm` 块 |
| `ProviderQuotaExceeded` | `PROVIDER_RPM/TPM_LIMIT` 触发 | 调额或关闭（0） |
| 流式卡住不结束 | 模型端挂起 | 设 `LLM_STREAM_IDLE_TIMEOUT_SECONDS > 0` |
| 429 但模型没被调用 | 请求级准入 RPM | 看 reason 码；调 `ADMISSION_*` |
| 长回答被截断/摘要 | `AGENT_MODEL_MAX_INPUT_TOKENS` 触发摘要中间件 | 调大或置 0 |
| 结构化输出报错 | 模型不支持 tool/结构化 | 依赖 `invoke_structured` 的 `text_fallback`；日志有 `text_compat` 标注 |
| 评估结果标记 bypassed | `govern_ragas_llm` 未成功装配 | 该结果不得用于生产容量/质量验收 |
| token 用量对不上 | 有的调用没返回 usage | `usage_returned_calls` 与 `calls` 的差值就是无 usage 的调用数 |
