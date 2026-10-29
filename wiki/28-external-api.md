# 28 · 外部消费者 API 使用说明

对应代码：`src/api/routes/external.py`、`src/api/routes/api_keys.py`、
`src/api/external_support.py`、`scripts/manual/external_api_client.py`。

本文面向 WCCA、脚本、自动化服务等**外部消费者**。它不是 HDB 前端对话 API，
也不会创建 HDB Chat/Agent 会话。外部消费者通过员工创建的 API Key，以受限、可审计、
只返回 opaque reference 的方式访问指定知识库。

---

## 1. 服务地址与基本约定

所有接口均以 `/api/v1` 开头。将下面的地址替换为实际部署的 API 地址：

```bash
# 当前隔离部署
export HDB_BASE_URL=http://127.0.0.1:18001

# 生产环境示例
# export HDB_BASE_URL=https://hdb.example.com
```

前端地址（例如 `http://127.0.0.1:5176`）不是 API 地址。消费者只需要访问
`HDB_BASE_URL`，不需要访问 RAGFlow、PostgreSQL、Redis 或内部文件路径。

### 1.1 外部 API 认证头

外部 API **只接受**员工 API Key：

```http
X-HDB-API-Key: hdbk_<public_id>_<secret>
```

外部 API 不接受 `Authorization: Bearer ...`；Bearer Session 只用于员工管理自己的 API Key。
API Key 不要写入代码、日志、命令历史或提交到 Git。

### 1.2 外部引用

生产环境只使用以下 opaque reference：

| 引用 | 来源 | 用途 |
|---|---|---|
| `kb_ref` | `/external/kbs` 或员工 Key 管理接口 | 知识库引用 |
| `file_ref` | `/external/kbs/{kb_ref}/files` | 文件引用 |
| `asset_ref` | `/external/kbs/{kb_ref}/assets` | 文档资产引用 |
| `table_ref` | `/tables/schema` | 表格查询中的表名 |
| `evidence_id` | 检索结果 | 证据回读/验证 |
| `job_id` | 上传结果 | 查询导入任务 |
| `scope_ref` | 来源范围接口 | 固定检索来源 |

不要提交 HDB 内部数据库 ID、RAGFlow ID、宿主机路径、知识库名称作为外部引用。

---

## 2. 第一次接入流程

推荐流程如下：

```text
员工登录 → 获取可绑定知识库 → 创建 selected_kbs API Key
       → capabilities → list kbs/files
       → retrieve → 保存 evidence_id/file_ref/content_hash
       → 可选：证据回读、证据验证、文件下载或结构化查询
```

生产消费者建议创建 `read + invoke` Key：

- `read`：查看知识库/文件/资产、下载文件、读取切片和证据；
- `invoke`：执行文档检索、批量检索、来源范围检索、表格查询和电路查询；
- `write`：上传文件和添加资产版本，**不包含**删除权限，也不自动包含 `invoke`。

### 2.1 员工获取可绑定知识库

以下接口使用员工 Session，不使用 API Key。Session 来自 `/api/v1/login` 返回的 `token`。

```bash
export HDB_SESSION_TOKEN='<员工登录返回的 token>'

curl -fsS "$HDB_BASE_URL/api/v1/me/api-keys/available-kbs" \
  -H "Authorization: Bearer $HDB_SESSION_TOKEN"
```

返回示例：

```json
[
  {
    "kb_ref": "kbref_...",
    "name": "ADAS",
    "department_name": "ADAS"
  }
]
```

### 2.2 创建 API Key

只有启用的 `employee` 可以创建 Key；`system_admin` 不能创建、使用或管理员工 API Key。

```bash
curl -fsS -X POST "$HDB_BASE_URL/api/v1/me/api-keys" \
  -H "Authorization: Bearer $HDB_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "wcca-reader",
    "scopes": ["read", "invoke"],
    "kb_access_mode": "selected_kbs",
    "kb_refs": ["kbref_..."],
    "expires_in_days": 90
  }'
```

重要约束：

- `expires_in_days` 为 1–365 天，默认 90 天；到期自动失效；
- 生产必须使用 `selected_kbs`，并显式提交至少一个 `kb_ref`；
- Key 只能绑定员工当前有权访问、且属于员工当前部门的知识库；
- 完整 `api_key` 只在创建和轮换响应中返回一次，关闭页面后不能恢复；
- 默认每个员工最多 10 个有效 Key；达到上限返回 `409 KEY_LIMIT_REACHED`。

创建响应中的关键字段：

```json
{
  "key_id": "...",
  "name": "wcca-reader",
  "scopes": ["invoke", "read"],
  "kb_access_mode": "selected_kbs",
  "kb_refs": ["kbref_..."],
  "expires_at": "2027-01-26T...+00:00",
  "status": "active",
  "masked_key": "hdbk_..._••••",
  "api_key": "hdbk_..."
}
```

### 2.3 管理、轮换和撤销 Key

这些接口全部使用员工 Session，且只能操作当前员工自己的 Key：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/me/api-keys` | 列出自己的 Key 元数据；只显示掩码 |
| GET | `/api/v1/me/api-keys/{key_id}` | 查看一个 Key 元数据 |
| POST | `/api/v1/me/api-keys/{key_id}/rotate` | 创建新 Key，并立即撤销旧 Key |
| PATCH | `/api/v1/me/api-keys/{key_id}` | 缩小 `kb_refs` allowlist；不能扩大权限 |
| DELETE | `/api/v1/me/api-keys/{key_id}` | 立即撤销 Key |

轮换示例：

```bash
curl -fsS -X POST \
  "$HDB_BASE_URL/api/v1/me/api-keys/$KEY_ID/rotate" \
  -H "Authorization: Bearer $HDB_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"expires_in_days": 90}'
```

轮换响应会返回新的完整 `api_key`，旧 Key 立即失效。撤销后不可恢复，只能重新创建或轮换。

---

## 3. 外部 API 能力发现

### GET `/api/v1/external/capabilities`

调用方应在启动时先读取能力和限制，不要把部署限制硬编码在客户端：

```bash
export HDB_API_KEY='hdbk_...'

curl -fsS "$HDB_BASE_URL/api/v1/external/capabilities" \
  -H "X-HDB-API-Key: $HDB_API_KEY"
```

当前能力包括：

| capability | 功能 |
|---|---|
| `document_claim_lookup` | 文档检索并返回带来源证据 |
| `file_download` | 下载原始文件 |
| `chunk_listing` | 读取文档解析切片 |
| `file_upload` / `ingestion_job` | 上传文件并查询导入任务 |
| `asset_index` / `asset_version` | 查询文档资产和追加资产版本 |
| `table_schema` / `table_query` | 表格 Schema 和只读 SQL |
| `circuit_query` | 电路模块、实例、网络和交叉引用查询 |
| `batch_evidence` | 批量检索，单项失败不影响其他项 |
| `source_scope` | 建立短期固定来源范围 |
| `evidence_validation` / `evidence_readback` | 证据验证和回读 |

响应中的 `limits` 是当前部署的真实限制，例如 `max_top_k`、`max_file_refs`、
`max_upload_bytes`、`max_batch_items`、`rate_limit_per_minute` 和 `daily_quota`。

---

## 4. 知识库、文件和下载

### 4.1 列出允许访问的知识库

```http
GET /api/v1/external/kbs
X-HDB-API-Key: <key>
```

Key 只能看到自己的 allowlist 中仍然可访问的知识库。未知或无权的 `kb_ref` 不会泄露内部资源信息。

### 4.2 列出文件

```bash
curl -fsS "$HDB_BASE_URL/api/v1/external/kbs/$KB_REF/files?offset=0&limit=100&status=ready" \
  -H "X-HDB-API-Key: $HDB_API_KEY"
```

`status` 可选：`ready`、`processing`、`failed`、`unsupported`、`unavailable`。
返回的 `items` 包含 `file_ref`、文件名、大小、`content_hash`、状态以及是否可下载。

单文件详情：

```http
GET /api/v1/external/kbs/{kb_ref}/files/{file_ref}
```

下载原始文件：

```bash
curl -fL "$HDB_BASE_URL/api/v1/external/kbs/$KB_REF/files/$FILE_REF/download" \
  -H "X-HDB-API-Key: $HDB_API_KEY" \
  -o ./downloaded-file.bin
```

下载前后服务端会校验来源内容哈希；文件超过部署限制或哈希不一致时不会返回内容。

### 4.3 读取文档切片

```http
GET /api/v1/external/kbs/{kb_ref}/files/{file_ref}/chunks?offset=0&limit=50
```

仅支持已完成解析的文档型文件。返回 `chunk_ref`、`index`、文本和可选页码；表格、电路等文件类型不一定支持切片接口。

---

## 5. 文档检索与证据

### 5.1 单知识库检索

```bash
curl -fsS -X POST "$HDB_BASE_URL/api/v1/external/kbs/$KB_REF/retrieve" \
  -H "X-HDB-API-Key: $HDB_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: wcca-search-001' \
  -d '{
    "query": "absolute maximum rating",
    "top_k": 10,
    "scope": {
      "mode": "files",
      "file_refs": ["fileref_..."],
      "expected_content_hashes": {
        "fileref_...": "sha256..."
      }
    },
    "capabilities": ["document_claim_lookup"]
  }'
```

`scope` 支持：

- `mode: files`：必须显式给出 `file_refs`，推荐消费者使用；
- `mode: kb`：在当前 Key 允许的知识库范围内检索，仍会执行部门和文件来源校验；
- `expected_content_hashes`：可选，用于防止文件更新后继续使用旧来源。

检索响应：

```json
{
  "request_id": "wcca-search-001",
  "status": "completed",
  "results": [
    {
      "evidence_id": "evidence_...",
      "content": "...",
      "source": {
        "file_ref": "fileref_...",
        "file_name": "datasheet.pdf",
        "content_hash": "sha256...",
        "locator": {"page": 3, "chunk_ref": "chunkref_..."}
      },
      "retrieval": {
        "type": "document",
        "backend": "ragflow",
        "score": 0.87
      }
    }
  ],
  "warnings": []
}
```

无命中时返回 `status: completed_no_hits`，不应当被当作服务失败。

### 5.2 批量检索

```http
POST /api/v1/external/evidence/batch-retrieve
```

请求体为 `{"items": [...]}`，每项包含 `kb_ref`、`query`、`top_k`、`scope` 等字段。
返回整体 `status`：`completed`、`partial` 或 `failed`；每项独立包含 `status`、`results` 或 `error`。
单项失败不会取消其他知识库的结果。

### 5.3 固定来源范围

对需要多次针对同一组文件检索的消费者，可以先创建短期来源范围：

```bash
curl -fsS -X POST "$HDB_BASE_URL/api/v1/external/source-scopes" \
  -H "X-HDB-API-Key: $HDB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "kb_ref": "kbref_...",
    "file_refs": ["fileref_a", "fileref_b"],
    "ttl_seconds": 300
  }'
```

服务端会记录当时的文件哈希。后续检索在请求中加入 `source_scope_ref`，并保持相同的
`scope.file_refs`；来源发生变化时会 fail-closed，返回 `409 SOURCE_HASH_CONFLICT`。
`ttl_seconds` 范围为 60–86400，默认由部署配置决定。

### 5.4 证据回读与验证

检索结果中的 `evidence_id` 可以回读：

```http
GET /api/v1/external/evidence/{evidence_id}
```

返回的 `status` 为 `current` 或 `stale`。消费者在生成报告、写入下游系统前，建议验证证据：

```bash
curl -fsS -X POST "$HDB_BASE_URL/api/v1/external/evidence/validate" \
  -H "X-HDB-API-Key: $HDB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "items": [{
      "evidence_id": "evidence_...",
      "required_terms": ["absolute maximum"],
      "required_fields": [],
      "expected_content_hash": "sha256..."
    }]
  }'
```

验证结果可能为 `supported`、`source_changed`、`source_missing`、`missing_terms`、
`missing_fields`、`content_integrity_failed`、`not_found` 或 `access_denied`。

---

## 6. 上传和导入任务

上传需要 Key 同时具有 `write` scope，且该员工对目标知识库具有写权限。

```bash
curl -fsS -X POST "$HDB_BASE_URL/api/v1/external/kbs/$KB_REF/files" \
  -H "X-HDB-API-Key: $HDB_API_KEY" \
  -H "Idempotency-Key: wcca-upload-20261028-001" \
  -F 'files=@./datasheet.pdf' \
  -F 'files=@./limits.xlsx' \
  -F 'source_group=wcca-import'
```

接口返回 HTTP `202`。`Idempotency-Key` 必填，格式为 1–200 个字母、数字、`.`、`_`、`:` 或 `-`。
相同 Key 和相同内容会返回之前的结果；相同 Key 但内容不同返回 `409 IDEMPOTENCY_CONFLICT`。

上传响应中的每个文件包含：`file_ref`、`job_id`、`status`、`success` 和 `error_code`。
如果存在 `job_id`，通过以下接口轮询：

```bash
curl -fsS "$HDB_BASE_URL/api/v1/external/ingestion-jobs/$JOB_ID" \
  -H "X-HDB-API-Key: $HDB_API_KEY"
```

文件处理状态以任务返回为准；只有 `ready` 文件才适合后续检索或下载。

---

## 7. 文档资产接口

文档资产是对有效文档、版本和生命周期的结构化索引，不等同于原始文件列表。

| 方法 | 路径 | 所需权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/external/kbs/{kb_ref}/assets` | `read` | 支持 `query`、`status`、`category`、`project`、分页 |
| GET | `/api/v1/external/kbs/{kb_ref}/assets/{asset_ref}` | `read` | 返回资产详情及版本列表 |
| POST | `/api/v1/external/kbs/{kb_ref}/assets/{asset_ref}/versions` | `write` | 使用已有 `file_ref` 追加资产版本 |

添加版本示例：

```bash
curl -fsS -X POST \
  "$HDB_BASE_URL/api/v1/external/kbs/$KB_REF/assets/$ASSET_REF/versions" \
  -H "X-HDB-API-Key: $HDB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"file_ref":"fileref_...", "note":"WCCA revision update"}'
```

外部 API 不提供删除资产、删除文件或修改知识库共享设置的能力。

---

## 8. 表格和电路结构化查询

### 8.1 表格 Schema

```http
GET /api/v1/external/kbs/{kb_ref}/tables/schema
GET /api/v1/external/kbs/{kb_ref}/tables/schema?file_ref={file_ref}
```

先读取 `tables[].table_ref` 和 `columns[].key`，再构造查询。不要猜测内部 SQLite 表名。

### 8.2 表格只读 SQL

```bash
curl -fsS -X POST \
  "$HDB_BASE_URL/api/v1/external/kbs/$KB_REF/tables/query" \
  -H "X-HDB-API-Key: $HDB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "sql": "SELECT \"part_number\", \"max_rating\" FROM \"table_ref_...\" LIMIT 20",
    "max_rows": 20
  }'
```

该接口仅允许只读 SQL，单次最多返回 200 行；返回 `truncated: true` 表示达到行数上限。
表格查询需要 `invoke`，不是 `read`。

### 8.3 电路查询

```bash
curl -fsS -X POST \
  "$HDB_BASE_URL/api/v1/external/kbs/$KB_REF/circuits/query" \
  -H "X-HDB-API-Key: $HDB_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "kind": "modules",
    "query": "power",
    "limit": 20
  }'
```

`kind` 可选：`modules`、`instances`、`nets`、`cross_references`。电路查询需要 `invoke`，
结果已按当前员工和知识库范围过滤。

---

## 9. 错误处理、重试和限制

### 9.1 统一错误格式

外部 API 错误统一返回：

```json
{
  "request_id": "req_...",
  "error": {
    "code": "RATE_LIMITED",
    "message": "请求频率超过限制",
    "retryable": true
  }
}
```

同时会返回 `X-Request-ID`；限流等错误还会返回 `Retry-After`。客户端应记录 `request_id`，
但不要记录 API Key 或文件正文。

| HTTP | 常见 code | 客户端处理 |
|---:|---|---|
| 401 | `AUTH_REQUIRED`、`AUTH_INVALID`、`KEY_EXPIRED`、`KEY_REVOKED` | 停止重试；重新创建/轮换 Key |
| 403 | `KEY_SCOPE_DENIED`、`KB_ACCESS_DENIED` | 检查 Key scope、allowlist 和员工 KB 权限 |
| 404 | `RESOURCE_NOT_FOUND` | 检查 opaque reference；不要据此枚举内部资源 |
| 409 | `SOURCE_HASH_CONFLICT`、`IDEMPOTENCY_CONFLICT` | 刷新文件/幂等状态后再决定是否重试 |
| 413 | `RESPONSE_TOO_LARGE` | 减少 `top_k`、文件数、行数或分页大小 |
| 422 | `INVALID_REQUEST`、`CAPABILITY_NOT_ENABLED` | 修正参数或先检查 capabilities |
| 429 | `RATE_LIMITED` | 按 `Retry-After` 退避，不要立即忙等重试 |
| 503/504 | `*_UNAVAILABLE`、`RETRIEVAL_TIMEOUT` | 仅对 `retryable=true` 使用指数退避 |

### 9.2 当前生产边界

以 `/capabilities` 返回值为准；当前隔离生产配置通常为：

| 限制 | 当前值 |
|---|---:|
| 每分钟限流 | 120 次（按 Key 和员工维度） |
| 每日配额 | 10000 次 |
| 并发检索 | 8 |
| `top_k` 最大值 | 20 |
| 单次最多文件引用 | 50 |
| 单次批量检索 | 20 项 |
| JSON 请求体 | 1 MiB |
| JSON 响应 | 2 MiB |
| 单文件下载 | 50 MiB |
| 上传总大小 | 100 MiB |
| 单次上传文件数 | 20 |

生产限流使用 Redis；Redis 不可用时服务 fail-closed，不回落到单进程本地限流。

---

## 10. Python 调用示例

仓库提供一个不依赖 HDB 内部模块的最小客户端：
`scripts/manual/external_api_client.py`。

```bash
export HDB_BASE_URL=http://127.0.0.1:18001
export HDB_API_KEY='hdbk_...'
export HDB_KB_REF='kbref_...'

uv run python scripts/manual/external_api_client.py
```

通用消费者验收脚本：

```bash
export HDB_FILE_REF='fileref_...'       # 可选
export HDB_QUERY='absolute maximum rating'  # 可选
uv run python scripts/manual/external_consumer_poc.py --json
```

脚本会验证能力发现、知识库/文件列表、切片、下载、检索、证据来源、批量检索隔离，
并在部署启用时尝试表格和电路能力。脚本不会打印完整 API Key。

---

## 11. 安全使用原则

1. 一个外部消费者使用一个员工 Key，不共享员工 Session。
2. 按最小权限创建：普通检索使用 `read + invoke`，只有上传场景才增加 `write`。
3. 生产使用显式 `selected_kbs`，不要依赖动态扩大的全量知识库模式。
4. 保存 `content_hash`，检索时尽可能提交 `expected_content_hashes`。
5. 对重要回答保存 `evidence_id`、`file_ref`、`content_hash` 和 `request_id`，必要时调用证据验证。
6. 对上传请求始终生成稳定的 `Idempotency-Key`，网络超时后使用同一个 Key 查询/重放。
7. 只对 `retryable=true` 的 429、503、504 错误重试，并使用指数退避。
8. API Key 泄露时立即撤销并轮换；不要等待到期。
