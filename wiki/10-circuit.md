# 10 · 电路子系统（EDIF 网表结构化建模）

对应代码：`src/circuit/`（约 8.6k 行，不含 vendor）、`src/circuit/vendor/spydrnet/`（内置依赖）。

目标：把 EDF/EDIF 网表解析为**结构化、可交叉引用的电路模型**，支撑确定性的网络/器件/模块/电源拓扑
检索，并（可选）叠加语义向量与 datasheet 证据。设计基调是「**直接事实优先，推断必须标注 certainty**」。

---

## 1. 领域模型（`models.py`）

| 类型 | 关键字段 |
|---|---|
| `CircuitDesign` | `design_id, kb_name, source_files[DesignFile], modules[Module], instances[Instance], nets[Net], properties, component_identities, status(CircuitStatus), warnings` |
| `DesignFile` | `file_name, file_type(edf/schematic/pdf), path, page_count?` |
| `Module` | `module_id, name, strategy, instances[refdes], nets[name], boundaries, properties` |
| `Instance` | `refdes, library_cell, part_number, footprint, value, erp_number, properties{...}, pins[Pin]` |
| `Pin` | `number, name, net` |
| `Net` | `name, net_type∈{signal,power,ground,unknown}, connections[{refdes,pin}]` |
| `ComponentIdentity` | `refdes, identifiers[Identifier], aliases[Alias], roles[RoleAssertion]` |

状态：`CircuitStatus.EMPTY / COMPLETE`（`PARTIAL_EDF/PARTIAL_PDF` 保留但当前不写）。
序列化：`CircuitDesign.to_dict()` → `circuit_state.json`。

---

## 2. 解析层（`parsers/`）

### 2.1 `EdfParser`：内置 SpyDrNet + 猴子补丁

- **内置依赖**：`src/circuit/vendor/spydrnet/`，项目不依赖 PyPI 的 spydrnet；
  `_load_spydrnet` 把 vendor 根加入 `sys.path`，并**清除已加载的 `spydrnet*` 模块**，
  确保 vendor 版本权威（`tests/test_edf_parser_dependency.py` 锁定该行为）。
- **补丁**（`edf_parser.py:36-87`）：替换 `EdifParser.parse_dataOrigin` / `parse_userData` /
  `parse_technology` 为「括号平衡跳过」实现，原方法保留为 `_orig_<method>`；
  用模块级 `_SPYDRNET_PATCHED` 保证幂等。
- **回退**：解析失败时退到 `edif_lite_parser.py`（轻量实现），保证「至少能出一部分结构」。

### 2.2 `edf_partition.py`：按页分模块

- **按页分组**：实例若带 `Page Name` 属性即按页归入模块；
  但这是**全有或全无**——只要有一个实例缺 `Page Name`，整体回退到按 refdes 数字前缀分组
  （`edf_partition.py:112-120`）。
- 产出 `Module(strategy)` 标注分组策略，供文档与查询区分可信度。

---

## 3. 索引与存储（`index_service.py` + `store.py`）

### 3.1 写入流程（`CircuitIndexService.index_file`）

```
index_file(kb_name, record_id, file_path, original_name, department_id, uploaded_by)
  ├─ 解析（EdfParser，失败→lite 回退）
  ├─ design_id = "<stem 截断 119>-<sha256(file)[:8]>"（内容寻址；哈希失败退化为 stem）
  ├─ CircuitStore.save(design)         # storage/circuits/{kb}/{design_id}/circuit_state.json
  │                                    # 同时维护 storage/circuits/index.json 全局索引
  ├─ _write_metadata(design)           # pipeline_metadata.json sidecar（发布元数据）
  ├─ GraphStore.build/save             # connectivity_graph.gpickle
  ├─ 语义索引重建（fail-soft，见 §6）
  └─ 返回 {status: indexed|degraded, message, stats}
```

- 写锁：`circuit_index_write_lock(kb_dir, exclusive=True)`（`index_lock.py`：POSIX `fcntl.flock` +
  Windows `msvcrt.locking` + 进程内 RLock 映射；锁文件 `<kb_dir>/.index-locks/root-<sha256>.lock`）；
- **注意可重入性**：`reindex_stored_design` 自己取写锁，调用方（如 backfill）不能已持锁；
- 失败保留台账行（`preserve_failed_record=True`），状态写 `failed` 供排查。

### 3.2 磁盘布局

```
storage/circuits/
├── index.json                              # 全局设计清单（design_id/kb/来源文件/状态/计数）
├── .index-locks/root-<sha256>.lock
└── <kb>/<design_id>/
    ├── circuit_state.json                  # 权威模型（旧版 circuit.json 读取兼容并在下次保存删除）
    ├── pipeline_metadata.json              # 发布元数据（含 graph_index_status 等）
    ├── connectivity_graph.gpickle          # networkx Graph（无 networkx 时为等价 dict 投影）
    ├── datasheet_links.json                # datasheet 链接（见 §8）
    ├── module_screenshots/                 # 可选的模块 PNG（外部/UI 写入）
    └── pdf_cache/                          # 原理图缓存
```

### 3.3 design_id 演进与别名

- 旧版 id 是纯 stem；新版是 `<stem>-<8hex>`；`design_id_matches_file` 同时接受两者；
  `_resolve_allowed_design` 只在后缀恰为 8 位十六进制时把旧 id 映射到新条目。
- 别名（`store.derive_circuit_aliases`）：design_id、其「分隔符转空格」形式、每个文件 stem 及其空格形式；
  `resolve_circuit_by_alias` 在一个 KB 内匹配；`.edf/.edif/.pdf` 交给 `resolve_circuit_by_file`
  （精确文件名或 basename）。

---

## 4. 查询引擎（`query_engine.py`，核心）

### 4.1 入口

```python
CircuitQueryEngine(store=..., vector_index=...).query(kb_name, query, top_k=..., filters=...)
CircuitIndexService.query(...)      # 校验 filters["query_operation"]，但自身不按 operation 分派
CircuitIndexService.typed_query(...)  # 当 query_operation != "auto" 时按类型读模型
```

`question_analysis.analyze_question(query)` 做确定性意图分析（规则/关键词），
产出 operations（如 `enable`、电源、连接、模块接口等），驱动：
- 结构化检索分支；
- 图证据的 enable 追踪分支（只沿二极管类器件）；
- `typed_query` 的读模型选择。

### 4.2 能力清单（节选）

| 方法 | 返回 |
|---|---|
| `search_nets` | 网络行：`design_id, name, net_type, connection_count, connections[:20]`（"REFDES.PIN"） |
| `search_instances` | 器件行：`refdes, library_cell, part_number, footprint, value, erp_number, pin_count, pins[]` |
| `search_modules` | 模块行：`module_id, name, strategy, instance_count, net_count, instances[:max], nets[:20], descriptions` |
| `get_instance_connections` | 位号邻接（peer_refdes/net/net_type） |
| `build_power_topology` | 电源转换树（见 §5.4） |
| `search_bias_topologies` | 上拉/下拉（见 §5.2） |
| `search_protection_topologies` | TVS/保险丝拓扑（见 §5.3） |
| `search_power_protection_candidates` | 电源控制候选（U 开头且器件分类成功） |
| `search_module_descriptions` | 仅语义：模块描述（向量索引可用时） |
| `resolve_entity_mention` | 实体解析（见 §7） |
| `_semantic_supplement` | 语义补充：`top_k=max(limit,8)`，任何异常 → `[]` |

### 4.3 归一化与匹配

- `_normalize_refdes`：取路径最后一段 → 去非字母数字 → 大写 → `^([A-Z]+)0*(\d+)([A-Z0-9]*)$`
  → `prefix + int(digits) + suffix`（`U01 ≡ U1`，`R003 ≡ R3`）；
- `_normalize_entity`：`re.sub(r"[^A-Z0-9]+","",upper)`（模块/网络 id 比较）；
- `_find_net_by_name`：先大小写不敏感精确，再归一化；
- `_find_module_by_id_or_name`：归一化 id → 归一化 name → 大小写不敏感 name。

### 4.4 语义补充与证据上限

| 来源 | 分数上限 | 说明 |
|---|---|---|
| 直接结构化行 | 0.80 ~ 0.99 | 由证据映射器给分 |
| 图关系证据（`_graph_evidence`） | **0.88** | 端点 ≤8（超出加 `, +N more`），内容 ≤512 字符 |
| 语义补充（`_semantic_evidence`） | **0.69** | 语义命中**永不压过直接 EDF 事实**；要求显式 `allowed_design_ids`/`allowed_generations`，并核对 generation |

**阶段顺序（而非跨阶段分数）决定截断**：先结构化、再图、最后语义。

---

## 5. 关系推导（`relations/`）

### 5.1 `RelationExtractor.extract(design)` —— 只产直接事实

| 边 | id | 证据 |
|---|---|---|
| `connectivity.pin_on_net` | `pin_on_net:{pin}->{net}` | `"{pin} -> {net}"` |
| `connectivity.instance_uses_net` | `instance_uses_net:{refdes}->{net}` | `"{refdes}.{pin} -> {net}"` |
| `module.contains_instance` | `module_contains:{module_id}->{refdes}` | — |
| `module.uses_net` | `module_uses_net:{module_id}->{net}` | — |

net 角色：`net.net_type ∈ {power,ground}` 直接用；否则 `classify_net_name(name)`。
certainty = `direct`。

### 5.2 `RelationDeriver.derive` —— 推断必须标注

- `is_series_passive(inst)`：refdes 以 `L/FB/F` 开头，或描述含 `FERRITE|FILTER|INDUCTOR|INDUCT`；
- `is_probable_power_ic(inst)`：排除 `Y/X` 位号与 `CRYSTAL/OSC/XTAL`；必须 `U` 开头；
  引脚键集合命中「输入 ∩（输出 ∪ 开关 ∪ 反馈前缀）」，或描述含
  `PMIC|BUCK|DCDC|DC-DC|LDO|REGULATOR|LOADSW|TPS|LP877|SA230`。

| 推导 | 边 | 置信度 | certainty |
|---|---|---|---|
| 串联滤波（两个非地引脚都在电源网） | `power.series_filter:{from}->{to}:{refdes}` | 0.66 | derived |
| 电源 IC 输入→直接输出 | `power.input_to_output:{from}->{to}:{refdes}` | 0.78 | derived |
| 输入→推断输出（反馈/开关-电感路径） | `power.inferred_conversion:...` | 0.74 | inferred |

`_switch_inductor_paths`：开关引脚网络上的串联无源器件（恰好一个其它非地引脚且落在电源网）
→ 三段式证据 `refdes.pin -> net`。

### 5.3 偏置/防护拓扑（引擎内算法）

**上拉/下拉**（`search_bias_topologies`）：

1. 候选：refdes 以 `R` 开头 或 描述含 `RES`；
2. 排除零欧电阻（`_is_zero_ohm_resistor`：值 strip+upper ∈ `{"0","0R","0OHM","0Ω","0R,5%%"}`）；
3. 必须恰好两个有网络的引脚；两者网络角色分类必须是 `(signal, power|ground)`；
4. `topology = "pull_up"`（网侧是 power）或 `"pull_down"`（ground）；
5. 行：`design_id, circuit_id, topology, refdes, library_cell, part_number, value, signal_net,`
   `signal_pin, rail_net, rail_pin, certainty="direct", confidence=1.0`。

**防护**（`search_protection_topologies`）：

1. 分类：描述含 `TVS/ESD` → `protection_tvs`；否则 refdes `F` 开头或描述含 `FUSE/PTC` → `protection_fuse`；
2. ≥2 个有网络引脚，按角色分 `ground_pins` / `signal_pins`；
3. 行包含 `protected_nets`、`ground_nets`、`pins[]`；
   文档字符串明确：**只能证明「器件-网络」拓扑，不能推断 IC 的短路防护能力**。

**电源控制候选**（`search_power_protection_candidates`）：

- `rail_names` = 电源/地类型网络或名字分类为电源的网络；
- 对每个实例跑 `_power_converter_candidate`，必须有非空且非 incomplete 的候选，
  排除 `fuse/ferrite_bead/series_passive`，且 refdes 必须 `U` 开头；
- 行标记 `capability_candidate=True`。

`_power_converter_candidate` 细节：

| 步骤 | 规则 |
|---|---|
| 非电源源排除 | refdes `Y/X` 开头，或描述含 `CRYSTAL/OSC/XTAL` |
| 器件分类 | `FB*→ferrite_bead 0.7`、`F*→fuse 0.7`、`L*→series_passive 0.48`；描述模式 `PMIC\|POWER MANAGEMENT 0.76`、`BUCK\|DCDC\|DC-DC\|STEP-DOWN 0.76`、`BOOST\|STEP-UP 0.74`、`LDO\|LINEAR REG 0.76`、`REGULATOR\|VREG\|REG 0.66`、`LOAD SW\|POWER SW 0.66`；默认 `(None, 0.42)` |
| 引脚角色 | ground / enable(`EN,ENABLE,ON,CE,SHDN,RUN` 或后缀) / power_good(`PG,PGOOD`) / switch_node(`SW,LX,PH,BOOTSW`) / input(`VIN,IN,PVIN,AVIN,VBAT…`) / output(`VOUT,OUT…`) / `VCC/VDD` 家族落在电源网络→input / 回退按网络角色 |
| 过滤 | 丢弃角色为 `other` 且网络非电源/地/rail 的引脚 |
| 缺类型时 | 要求 `{input,output} ⊆ roles`，否则 `None`（不确定） |
| 融合 | fuse/ferrite/series_passive 恰好两引脚时强制 first=input、second=output |
| 置信度 | `min(0.98, type_conf + 0.18)`（输入输出齐全）；不完整封顶 0.62 |
| 返回 | `{refdes, type, label, library_cell, part_number, value, modules, pins{...}, input_nets, output_nets, enable_nets, power_good_nets, is_incomplete, confidence}` |

### 5.4 电源转换树（`build_power_topology`）

同时运行「关系推导」与「候选枚举」并合并：

1. `RelationExtractor → RelationDeriver → build_power_tree_view`（`direct_edges`/`derived_edges`/`inferred_edges`）；
2. 候选枚举 → `conversion_edges`（键 `(from_net,to_net,via_refdes,via_type,via_label,control_nets,confidence)`）；
3. 关系视图的 `direct_edges` 去重后并入；`inferred_edges` 的网络也并入输入/输出集合；
4. `root_nets` = 只作输入不作输出的网络；
5. `rails` 由全部边推导 `produced_by` / `consumed_by`；
6. `missing_info` 说明（无转换器/网络不可配对/候选不完整）；
7. `confidence = 0.86`（有边且有推断边）/ `0.82`（有边）/ `0.45`（无边）。

结果键：`kb_name, design_id, circuit_id, source_files, topology_type="power_conversion_tree",`
`converter_count, edge_count, inferred_edge_count, root_nets, input_nets, output_nets, rails,`
`converters, conversion_edges, direct_edges, derived_edges, inferred_edges, ambiguous_edges,`
`control_edges, relation_summary, distribution_fallback, missing_info, confidence`。
（`ambiguous_edges`/`control_edges` 目前恒为空列表。）

---

## 6. 图与向量

### 6.1 `graph_store.py` —— 连通图（在用）

- 产物：`connectivity_graph.gpickle`；无 networkx 时退化为**同构的 dict 投影**（节点 id/属性/边一致）；
- 节点 id：`component:{refdes}`、`pin:{refdes}.{pin}`（缺省 `?`）、`net:{name}`；
- 边：`component→pin`（`contains`）、`pin→net`（`on_net`）——高扇出网络保持线性，不生成器件团；
- `connected_entities(graph, refdes="", net_name="", pin="")`：直接邻居 + 器件/网络目标各邻接引脚的邻居；
- 消费方：`CircuitIndexService._graph_evidence`（**仅当** `pipeline_metadata["graph_index_status"]=="indexed"`）；
  遍历预算 `max(top_k*8, top_k+8)`；BFS 交替 component→net→component；
  种子深度 component ≤2、net ≤3；每个网络产出一条 `graph_relationship` 证据（score 0.88）。
  `analysis.operations` 含 `enable` 时只沿二极管类器件追踪。

### 6.2 `vector_index.py` —— 语义索引（默认停用）

启用条件（**两个都必须满足**）：

1. `llama_index.core.Settings._embed_model` 被绑定（`src/` 内无绑定 → 默认 None）；
2. `import chromadb` 成功（**未声明为项目依赖**）→ `PersistentClient(storage/circuit_vector_index)`。

不满足时记一次日志并保持显式 no-op（所有调用方把空结果当作「无信号」）。

| 项 | 值 |
|---|---|
| collection | `circuit_kb_{kb_name}`，metadata `{"hnsw:space":"cosine"}`（刻意与文档索引分离） |
| 文档 id | `f"{kind}:{design_id}:{natural_id}"`，`kind ∈ {module, instance, net}` |
| 生成元数据 | `generation_id` + `generation_key=f"{design_id}:{generation_id}"` |
| 重建 | `reindex_design_with_status`：先按 `design_id` 删除，再批量嵌入（`get_text_embedding_batch` 优先）后 `add` |
| 搜索 | `semantic_search`：`where` 组合 `kind $in` / `design_id $in` / `generation_key $in`；`n_results=top_k`；**score = max(0, 1 - distance)** |
| 删除 | `_delete_design` / `drop_kb`（best-effort） |

文档内容模板：模块含「模块/模块ID/包含器件(前30)/涉及网络(≤20)/Power nets/Ground nets/描述」；
器件含「位号/cell/MPN/封装/value」；网络含「名称/类型/连接数/连接样例(≤10)」。

### 6.3 语义补丁脚本

`backfill_semantic_indexes.py`：为存量设计补建/重建语义索引（需先具备 §6.2 的启用条件）。

---

## 7. 器件身份与解析（`component_identity.py`）

### 7.1 受控词表

```python
CONTROLLED_ROLES = (system_on_chip, mcu, pmic, ethernet_phy, memory, transceiver, connector)
CONTROLLER_FAMILY_ROLES = (system_on_chip, mcu);  CONTROLLER_FAMILY = "controller_family"
MAX_RETURNED_CANDIDATES = 20;  EXPLICIT_ASSERTION_CONFIDENCE = 0.95
```

### 7.2 标识符投影（`_identifiers_for_instance`）

来源与顺序：`refdes`、`internal_part_number`、`erp_number`、`library_cell`、`value`，
再加属性规则：`Manufacturer Part Number → manufacturer_part_number`、
`Part Number → internal_part_number`、`ERP NUM → erp_number`。
去重键 `(namespace, normalize_identifier_value(raw))`；`normalized_value` = 折叠空白 + casefold；
`source_kind="edf_property"`。

### 7.3 角色断言

- **显式**：只认 `"Device Role"` 属性（`soc/system on chip → system_on_chip`、
  `mcu/microcontroller → mcu`、`pmic/power management → pmic`、`ethernet phy/memory/transceiver/connector`）；
  `source_kind="edf_property"`、`assertion_mode="explicit"`、confidence 0.95；
  **其它任何字段（library cell/Description/Part Type/模块名）都不能产生角色**；
- **策展目录**：`CuratedCatalogEntry` 按 `(match_namespace, normalized match_raw_value)` 匹配，
  `source_kind="curated_catalog"`、`assertion_mode="catalog_match"`；
  一对多时在解析结果中体现为多个候选，**绝不静默选一个**。

### 7.4 解析（`resolve_entity_mention`）

1. 词面归一；同义词识别：
   - 家族词（`主控/主控芯片/处理器/controller/processor`）→ `intent_kind="role"`、`role_query="controller_family"`；
   - 角色词（`soc/单片机/pmic/电源管理/ethernet phy/以太网phy/memory/存储/收发器/连接器…`）→ 具体角色；
   - 否则 `intent_kind="identifier"`；
2. 候选扫描（设计按 `(design_id,kb_name)`、身份按 refdes 排序），匹配优先级：
   `role_assertion` → `refdes_exact` → `identifier_exact` → `alias`；**不支持子串/模糊**；
3. 候选上限 20；`resolution_status ∈ {no_evidence, unique, ambiguous}`。

---

## 8. Datasheet 链接（`datasheet_link_index.py`）

### 8.1 模型

```python
DatasheetCatalogEntry(entry_id, catalog_version, source_file, internal_pn, mpn, confidence=1.0)
DocumentProfileSnapshot(record_id, kb_name, department_id, remote_document_id, parse_status,
                        content_hash, source_version_id, revision, mpn_values, manufacturer)
ComponentDatasheetLink(circuit_id, refdes, datasheet_record_id, matched_part_number,
                       circuit_generation_id, document_source_version_id, remote_document_id,
                       match_method, link_status, locator, confidence, document_fingerprint, ...)
```

### 8.2 建链（`build_links_for_design`）

1. 来源身份：`design.component_identities`（缺失则现算）；
2. 逐标识符匹配：
   - `manufacturer_part_number` → 按 `/;,\` 拆分候选，逐个 `exact_mpn`；
   - `internal_part_number` → 策展目录 `internal_pdn_catalog`；
3. **多文档碰撞**：同一方法命中 >1 个 `record_id` → 全部降级为 `candidate`（confidence 0.3，
   `degrade_reason="multiple_documents"`），不自动选；
4. 单文档：默认 `verified`，方法置信度 `exact_mpn 0.95 / internal_pdn_catalog 0.9 / 其他 0.5`；
   以下情况降级为 `candidate 0.3`：`manufacturer_missing`、`manufacturer_conflict`、`revision_missing`；
   verified 置信度下限 0.55；
5. 去重键 `(refdes, record_id, method)`；最终按 `(datasheet_record_id, match_method, link_status)` 合并；
6. 落盘 `datasheet_links.json`（相对设计目录），原子写；`load_links` 任何异常 → `[]`。

### 8.3 读取时再校验（fail-closed）

`get_verified_datasheet_links` 要求：`link_status=="verified"` → 生成号一致 →
`document_store.get_document_by_id_scoped(record_id, department_id)` 存在 →
`record.content_hash == link.document_fingerprint` → `source_version_id` 一致。
任一不符即**静默丢弃**链接。

`verified_links_for_question(question, ...)`：抽取角色词 → 解析身份 →（unique/ambiguous）
返回 `[{refdes, design_id, part_number, record_ids}]`。

---

## 9. 结构化测试数据域（`src/test_data/`，骨架）

| 文件 | 内容 |
|---|---|
| `models.py` | `Measurement(name,value,unit,pass_fail)` → `TestCase(case_id,name,measurements,notes)` → `TestRun(run_id,operator,started_at,cases)` → `TestReport(report_id,kb_name,title,source_file,runs,metadata)`；`to_dict/from_dict` |
| `store.py` | 根 `storage/test_data`；`report_dir` 做路径包含校验；`save` 写 `report.json`（flush+fsync，**非原子**）；`list_reports`、`delete_report`、`delete_kb` |
| `query_engine.py` | `list_reports(kb)` / `search_measurements(kb, query, limit=20)`（正则取词 + OR 子串匹配，纯词法） |

**没有解析器**：包 docstring 明确「实际解析逻辑是测试数据子团队的 TODO」；
`parser_registry.PARSER_REGISTRY` 在运行期**为空**（`src/test_data/manifest.py` 不存在）。
API 暴露：`GET /kbs/{kb}/structured/test-reports`、`GET /kbs/{kb}/structured/test-measurements`。

---

## 10. 配置与依赖

| 项 | 默认 | 说明 |
|---|---|---|
| `CIRCUIT_SEMANTIC_QUERY_ENABLED` | `true` | 角色/结构读模型与语义门控；精确位号/网络查询始终可用 |
| `networkx` | 已声明依赖 | 图存储的原生表示；缺失时 dict 投影 |
| `chromadb` | **未声明** | 语义索引的必要条件之一 |
| `llama_index` embed model | 无绑定 | 语义索引的另一必要条件 |
| `STORAGE_DIR` | `storage` | 电路/向量/测试数据根 |
| pytest | `norecursedirs` 排除 `src/circuit/vendor/spydrnet` | 上游测试不参与 |

---

## 11. 测试与基准

`tests/test_edf_parser_dependency.py`（vendor 权威性）、`test_circuit_query_engine.py`、
`test_circuit_index_service.py`、`test_circuit_index_lock.py`、`test_circuit_graph_store.py`、
`test_component_identity_index.py`、`test_circuit_capabilities.py`、`test_circuit_structure_query.py`、
`test_circuit_vector_detachment.py`、`test_icd_pin_definition_benchmark.py`、
`test_icd_generation.py`。

---

## 12. 已知坑

1. **按页分区是全有或全无**：一个实例缺 `Page Name` 就整体退化为位号前缀分组。
2. `query()` 校验 `query_operation` 但**不分派**；只有 `typed_query()` 走类型化读模型。
3. 语义证据封顶 0.69、图证据 0.88——**不要跨阶段比分数**；截断按阶段顺序。
4. 零欧电阻字面量表里有 `"0R,5%%"`（双百分号）是数据怪癖，别当笔误「修复」。
5. 图文件存在但发布元数据是 `failed` 时，图证据读取会被**拒绝**。
6. 旧 design_id 只有在后缀恰为 8 位十六进制时才能映射到内容寻址 id。
7. 语义索引/身份解析都不会「猜」：无证据就是 `no_evidence`，多候选就是 `ambiguous`。
8. vendor 目录同时被 pytest 与 ruff 排除；修改 vendor 要同步 `pyproject.toml` 的打包白名单。
