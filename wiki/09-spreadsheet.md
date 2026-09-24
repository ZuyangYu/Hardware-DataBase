# 09 · 表格子系统（Excel 结构化索引与 SQL）

对应代码：`src/pipelines/spreadsheet/`（`xlsx_parser.py` / `table_store.py` / `sql_materializer.py` /
`pipeline.py`）、`src/services/spreadsheet_index_service.py`、`src/agents/tools/spreadsheet_tools.py`。

设计目标：**不依赖 openpyxl/pandas**，直接解析 OOXML；把每张表变成「可 LIKE 的语义行 + 可 JOIN 的物理表」，
让 Agent 既能做模糊全文检索，也能生成只读 SQL 精确查数。

---

## 1. 纯标准库 xlsx 解析（`xlsx_parser.py`）

依赖仅 `os, re, zipfile, xml.etree.ElementTree, collections.abc, dataclasses, datetime`。

### 1.1 数据模型

```python
ParsedCell(row_index, col_index, ref, value, raw_value="", number_format="")
ParsedRow(row_index, values: list[str])
ParsedSheet(name, rows: list[list[str]], row_indices: list[int],
            cells: list[ParsedCell], merged_ranges: list[str])
ParsedWorkbook(file_name, sheets, embedded_object_count, media_object_count, drawing_object_count)

XlsxParseLimits(max_rows, max_cells, max_sheets, max_shared_strings,
                max_cell_text_length, max_uncompressed_bytes)   # from_value(instance|mapping|None)
```

KB 管道传 `limits=None`（不限）；附件管道传 `CHAT_ATTACHMENT_*` 限额。

### 1.2 解析流程（`parse_xlsx`）

1. `zipfile.is_zipfile` 校验（`ValueError("Not a valid .xlsx package")`）；
2. 包体积限制：总解压尺寸 ≤ `max_uncompressed_bytes`；单成员压缩比 > **200** 视为可疑（zip bomb）；
3. `sharedStrings.xml` 用 `ET.iterparse` 流式读取，每个 `<si>` 拼接全部 `<t>`；
4. 解析 `workbook.xml` + rels，按 `r:id` 解析每个 sheet 目标路径（归一为 `xl/...`），
   校验 `max_sheets ≥ 1`；
5. 读 `xl/styles.xml` → `_NumberFormatResolver`（样式索引 → numFmtId → 格式码；损坏则空降级）；
6. 逐 sheet 读取（`_read_sheet_rows`，**三遍法**）：
   - **第一遍**：`iterparse` 流式读 `<row>/<c>`，稀疏存 `row_by_index[row][col]=(raw, display, fmt)`，
     记录真实行号顺序与 `mergeCell`；强制 cells/文本长度上限；
   - **第二遍**：合并单元格展开——取左上角实际值填入矩形内空缺（**不覆盖已写单元格**）；
     被 `sheetData` 省略的行不产生幻影行；
   - **第三遍**：物化 `rows`（按最大列稠密化）、`row_indices`（真实行号）、`cells`
     （仅保留非空显示值，1-based 列号 + A1 引用 + raw_value + number_format）；
7. 统计嵌入对象（`xl/embeddings/`、`xl/media/`、`xl/drawings/`）。

### 1.3 单元格取值（`_cell_value`）

| `t` | 取值 |
|---|---|
| `inlineStr` | 拼接 `<t>` 文本，去空白；raw=display |
| `s` | 共享字符串索引 |
| `b` | `"TRUE"` / `"FALSE"` |
| `str` / `e` | 原样透传（公式字符串结果 / 错误） |
| 缺省 / `n` | `raw = <v>`；按样式解析格式码 → `display` |

**公式不求值**：只取缓存 `<v>`（必要时 `t="str"` 透传）。

### 1.4 数字格式（`_NumberFormatResolver`）

- 内置表覆盖 numFmtId 0-49；自定义 `numFmtId → formatCode` 来自 `<numFmts>`；
  解析顺序：自定义 → 内置 → `""`；
- `_classify_format`：取第一个 `;` 段 → 去引号字面量/转义/颜色与条件方括号（保留 `[$...]` 货币标记）
  → 分类 `text("@")/percentage(%)/datetime|date|time(yy|mmm+|d{1,4} 与 [hs] 标记，避免裸 m 歧义)/`
  `currency([$ 或 $€£¥￥)/general/其它=number`；
- `_serial_to_datetime_display`：Excel 1900 历（1899-12-31 起算；序号 >60 减 1 抵消虚构的 1900-02-29，
  序号 60 直接渲染为 `1900-02-29`）；小数部分 → `HH:MM:SS`；负序号原样字符串；
- 百分比按小数位乘 100 渲染；货币 `符号+数字`；number/general/text 保留原值。

---

## 2. 每 KB 表索引（`table_store.py`）

### 2.1 位置与连接

```
{storage_root}/table_indexes/departments/{safe(dept)}/kbs/{validate_kb_name(kb)}/table_indexes.db
```

（`SpreadsheetIndexService.kb_index_path`；`_safe_scope_part` 把非法字符替换为 `_`，截断 80。）
PRAGMA：`WAL`、`busy_timeout=5000`、`synchronous=NORMAL`、**`foreign_keys=ON`**。

### 2.2 DDL

```sql
table_documents(record_id PK, kb_id, kb_name, department_id, document_name, source_group,
                local_path, content_hash, document_kind DEFAULT 'tabular',
                embedded_object_count, media_object_count, drawing_object_count,
                warnings_json, created_at, updated_at)

table_sheets(id PK, record_id FK CASCADE, sheet_name, row_count, column_count,
             non_empty_row_count, non_empty_cell_count, header_row_index, header_json,
             profile_json, merged_ranges_json)

table_rows(id PK, record_id FK, sheet_name, row_index, row_text)        -- LIKE 检索
table_cells(id PK, record_id FK, sheet_name, row_index, col_index, cell_ref, value,
            header, number_format, raw_value)                            -- 精确/表头检索
table_text_blocks(id PK, record_id FK, sheet_name, block_index, block_text)  -- 文本块（≤1200字）
table_semantic_rows(id PK, record_id FK, sheet_name, row_index, header_row_index,
            semantic_text, raw_text, values_json, raw_values_json, inherited_json,
            confidence, confidence_score, confidence_reasons_json, source_json)
```

索引：`(record_id,sheet_name,row_index)`、`row_text`、`(record_id,sheet_name,row_index,col_index)`、
`value`、`header`、`block_text`、`semantic_text`、`confidence`、`(kb_name,department_id)`。
`_ensure_columns` 对旧库做加列迁移。

### 2.3 索引流程（`index_xlsx`）

```
parse_xlsx → _workbook_stats（sheet/行/非空单元格/容器对象计数）
document_kind = "universal_table_index"；基线告警
BEGIN IMMEDIATE:
  删旧 cells/rows/text_blocks/semantic_rows/sheets + drop_record_tables（物理表+注册行）
  upsert table_documents
  逐 sheet（SPREADSHEET_SKIP_BOILERPLATE_SHEETS=true 时跳过样板页）：
      插 sheet 行 → rows（_row_to_text 非空）→ cells（带推断表头）→ text blocks
      → semantic rows → materialize_sheet（物化物理表）
进展 20 → 35 → 85 → COMMIT
```

`TableIndexStats`：`sheet_count, row_count, cell_count, document_kind, embedded/media/drawing counts,`
`text_block_count, semantic_row_count, warnings`。

### 2.4 结构与表头推断

- `_sheet_profile`：`row_count/column_count(最大列)/non_empty_*/header_row_index/headers/`
  `title_like_row_count/sparsity = 1 - non_empty/(rows*cols)`；
- `_infer_headers`：扫描前 30 行，跳过非空 <2 或全数字行，
  打分 `text_count + min(len,8)*0.25 - duplicate_penalty`，取分数 ≥2 的最优行；
- `_row_to_text`：非空单元格 `" | "` 连接；
- `_sheet_text_blocks(max_chars=1200)`：连续非空行分组，块头 `Sheet: {name}\nRows: {start}-{end}`；
- `_section_heading_text`：若某行含「章节名称/heading」且首列匹配 `^\s*\d+(?:\.\d+)+`，
  作为 section 路径；
- **样板页识别**：表名匹配（`模板使用说明/模板变更历史/template instructions/template change history/`
  `instruction manual/填写说明`）或前 6 行 × 前 8 列出现 ≥2 个内容特征
  （`变更单号/变更原因/填写说明/revision note/fill in instructions`）。

### 2.5 语义行与置信度（`_sheet_semantic_rows`）

算法：

1. 取表头行位置与 `1-based 列 → header` 映射；
2. `merged_fill` 集合（合并区域非左上角单元格）视为**权威事实**，不向前继承；
3. 表头行之前的真实数据行（如连接器信息）→ 独立语义行 `inference_type="pre_header_row"`，
   置信度 −0.1，理由 `row_above_inferred_header`，且**不作为前向填充上下文**；
4. 空行 / 表头重复（与期望表头交集 ≥ `max(2, min(actual,expected)//2)`）→ 清空上下文；
   标题样行（单格、≥8 字符或中英混排）→ 清空并记 `section_title`；
5. 主循环：`raw_values = _row_header_values`；有值 → 记 `values` 并更新上下文；缺值且有非结构性上下文
   → 记 `inherited[header]=来源行`；
6. `semantic_text = "Section: ...; Header: value; ..."`；`raw_text` 为原始行文本；
7. **置信度**：`score = 0.58 + min(coverage,1)*0.22`
   `+0.12`（继承行，理由 `blank_cells_forward_filled_from_previous_rows`）
   `+0.18`（无继承）
   `+0.03`（有 section）
   `−0.18`（继承比例 >0.6，理由 `mostly_inherited_values`）或 `−0.08`（>0.35）
   上限 0.98；标签 `high ≥0.82`、`medium ≥0.68`、否则 `low`；
8. 落库 `source_json`：

```json
{"row_index", "header_row_index", "evidence_layer": "fact", "raw_row_text", "raw_headers",
 "inherited_headers", "inherited_from_rows", "structural_fill"}
```

并带 `evidence_policy` 说明「raw_text/raw_values 是事实；继承字段需核验」。

### 2.6 其它方法

| 方法 | 说明 |
|---|---|
| `delete_document(record_id)` | 事务删所有子表 + 物理表 + 主行；再尽力清理旧版索引目录与 `kb_excel_index.json` |
| `get_document_profile(record_id)` | 文档/表/列/表头/置信度分布/告警的结构画像 |
| `rank_documents_by_terms(terms, limit=20)` | 最多 8 个词，对 `LOWER(value|raw_value|header)` LIKE（每词 300 行）；打分 精确 8 / 子串 5 / 表头 3，最高分需 ≥5；用于来源路由（catalog 工具） |

---

## 3. SQL 物化（`sql_materializer.py`）

为每张有语义行的 sheet 建一张物理表 `t_{record_id}_{table_seq}`（seq = 1-based sheet 序号）。

```sql
sql_table_registry(record_id, sheet_name, table_name UNIQUE, column_count, row_count,
                   schema_json, PRIMARY KEY(record_id, sheet_name))

CREATE TABLE "t_<record>_<seq>" (
    row_index INTEGER NOT NULL,
    header_row_index INTEGER NOT NULL DEFAULT 0,
    section TEXT NOT NULL DEFAULT '',
    "col_1" <type>, "col_2" <type>, ...
)
```

- `drop_record_tables`：只删匹配 `^t_\d+_\d+$` 的表 + 注册行；
- 列顺序：**取覆盖列最多的语义行**确定（避免 pre-header 行把 `col_N` 顺序带偏），
  再按首次出现补齐；上限 `MAX_MATERIALIZED_COLUMNS=256`；
- 类型推断**只看 raw_values**（不继承）：全整数 → `INTEGER`；全整数/浮点（允许千分位）→ `REAL`；
  否则 `TEXT`（日期是显示字符串 → `TEXT`）；
- `schema_json.columns = [{column, header, dtype, samples(≤3 去重)}]`，
  `meta_columns = [row_index 原表行号 INTEGER, section 章节标题 TEXT]`；
- 值来自 `semantic_rows[*]["values"]`（含前向填充）；`section` 取
  `section_heading or section_title`；转换失败 → `NULL`。

> 模型看到的「表」是 `col_N` + 表头 + 样本值；SQL 里必须用 `col_N`，
> 这迫使模型基于 schema 而不是猜测列名。

---

## 4. 表格工具（`src/agents/tools/spreadsheet_tools.py`）

### 4.1 作用域

`_knowledge_base_scope(...)`：`kb_scope_from_context(...).require_department(...)` +
`KnowledgeBaseSpreadsheetScopeResolver`（`create=False`，直接绑定 `table_indexes.db` 路径）。
附件场景：`AttachmentSpreadsheetScopeResolver` 读 manifest 的 `index_db_path` + `record_id`；
多个不同 db 路径 → 明确拒绝。

### 4.2 `spreadsheet_schema_lookup(query="", top_k=8)`

- 加载注册表（join `table_documents` 拿文档名）；
- 按 token 过滤 `document_name + sheet_name + headers`；
- 输出分块 ≤1100 字符（单证据渲染上限 1200）；
- Evidence：`id="xlsx-schema:summary:{chunk}"`、`content_kind="spreadsheet_schema"`、
  `processor_kind="spreadsheet_table"`、`score=1.0`、`locator={"part":"i/n"}`；
- 内容形态：表名/文档/sheet/行数 + `col_N=表头(dtype) 样本:...` + 附加列说明。

### 4.3 `spreadsheet_sql_query(sql)`（LLM 生成 SQL 的通道）

1. 载入注册表 → `allowed = {table_name: entry}`；
2. `_validate_readonly_sql`：
   - `sqlglot.parse(sql, read="sqlite")`，语法错 → `"SQL 语法错误: ..."`；
   - 必须恰好一条非空语句；
   - 根节点必须是 `exp.Select`（否则 `"只允许 SELECT 查询(检测到 X)"`）；
   - CTE 名从白名单检查中排除（`exp.CTE` 别名）；
   - 未知表 → 报错并列出可用表；
   - LIMIT 收敛：数值 LIMIT ≤200 保留，否则替换为 200；无 LIMIT → 追加 `limit(200)`；
3. 执行：`file:{db_path}?mode=ro` URI + `PRAGMA query_only=ON` + `fetchmany(201)` 截断到 200；
4. 结果格式化：每行一行文本，字符预算 `_SQL_RESULT_CHAR_BUDGET=2000`，超限附截断说明；
   空结果给引导语；
5. Evidence id 内容派生：`xlsx-sql:{kind}:{sha1(sql)[:12]}`（`kind ∈ {invalid,error,result}`），
   **不同 SQL 永不复用引用 id**；错误证据带 `status` 元数据与「最多重试 2 次」指示；
   成功证据 `content_kind="spreadsheet_sql_result"`、`score=1.0`、`locator={"tables": "..."}`。

### 4.4 词法检索工具

- `spreadsheet_row_search` → `_semantic_rows`：`LOWER(col) LIKE ? ESCAPE '\'` 于
  `table_semantic_rows(semantic_text, raw_text)`，join 文档范围；证据 `score=confidence_score`；
- `spreadsheet_cell_lookup` → `_cell_rows`：`LOWER(value|raw_value|header)` LIKE；
  证据 `score=1.0`；
- 类形态（`SpreadsheetSemanticTool` / `SpreadsheetCellTool`）：
  - token 化：`tokenize_hardware_query` + 硬件领域词表 + CJK bigram + 停用/噪声过滤；
  - 过采样 `_candidate_limit = min(200, max(40, top_k*8))`；
  - Python 重排键 `(命中 token 数, 命中字符数, 非样板, 置信度, -id)`。

---

## 5. 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `SPREADSHEET_SKIP_BOILERPLATE_SHEETS` | `true` | 跳过模板/封面/变更历史表 |
| `CHAT_ATTACHMENT_MAX_ROWS/CELLS/SHEETS/SHARED_STRINGS/CELL_TEXT_LENGTH/UNCOMPRESSED_BYTES` | 200000/2000000/100/1000000/100000/200MiB | 附件通道解析限额（KB 通道不限） |

硬编码：`_SQL_MAX_ROWS=200`、`_SQL_RESULT_CHAR_BUDGET=2000`、`MAX_MATERIALIZED_COLUMNS=256`、
`MAX_SAMPLE_VALUES=3`、语义行文本块 1200 字符。

---

## 6. 测试与基准

| 测试/脚本 | 保护的不变量 |
|---|---|
| `tests/test_xlsx_parser*.py`、`tests/test_xlsx_number_format.py` | 数字/日期/百分比渲染、合并单元格、公式缓存值、包限制 |
| `tests/test_spreadsheet_table_store.py` | 语义行置信度、样板页跳过、列类型推断、物理表物化/删除 |
| `tests/test_spreadsheet_sql_security.py`（如存在）/ `test_spreadsheet_tools*.py` | 只读 SQL 校验、表白名单、LIMIT 收敛、错误证据 |
| `benchmarks/spreadsheet/run_eval.py` | 解析保真度 + 检索 recall@k/MRR（`golden_qa.json` 30 题） |
| `benchmarks/spreadsheet/run_answer_eval.py` | A/B：禁用 SQL 工具 vs 启用（确定性 must_include/REGEX 评分） |
| `benchmarks/spreadsheet/verify_sql_path.py` | 18 条参考 SQL 走真实只读执行路径 |
| `docs/excel_sql_optimization_report.md` | 表格 SQL 优化的历史报告 |

跑法：

```bash
.venv/bin/python benchmarks/spreadsheet/run_eval.py
.venv/bin/python benchmarks/spreadsheet/run_answer_eval.py --arm b
```

---

## 7. 已知坑

1. 公式**不求值**：只读缓存结果；若文件从未被 Excel 重算，缓存可能为空。
2. `table_semantic_rows` 的前向填充是启发式：跨节继承可能过度（置信度会降级并给出理由，
   但消费方需要看 `inherited_json`）。
3. 列类型只按 raw_values 推断，显示值/日期是 `TEXT`——SQL 里对日期列做比较要按字符串。
4. `rank_documents_by_terms` 每词最多 300 行 LIKE，超大表可能漏匹配（用于来源路由，不用于最终回答）。
5. 物理表名 `t_{record_id}_{seq}` 随重新解析变化；跨文档 JOIN 用注册表里的名字，不要硬编码。
