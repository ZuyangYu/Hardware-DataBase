# AAA 知识库 × 硬件设计 Skill —— MCP 试跑报告

- 日期：2026-11-06
- 知识库：`AAA`（kb_id=4，部门：AAA测试部门，权限：admin）
- 试跑范围：MCP 只读工具 + `hw-design-solution` 的“输入齐套性核对”
- 结论：**结构化检索链路可用；RAG 问答链路当前不可用**（模型服务额度受限/超时）。以 AAA 现有资料，② 框图、④ 接口表具备开工条件；③+ 电源树可出结构稿；①③⑤⑥ 缺关键手册数据，暂不能出正式表。

---

## 1. 知识库家底

| 类别 | 内容 | 规模 |
|---|---|---|
| 源文件 | 7 个 xlsx + 1 个 docx + 1 个 EDF 网表 + 1 个 PDF 原理图 | 10 个 |
| 结构化表格 | 7 个表格文件、70 个 sheet、2319 条语义行 | 61 张物化表 |
| 原理图 | `825504380_ADAS_SCH_TCN2.EDF`，状态 complete | 1052 实例 / 504 网络 / 20 模块 |
| 文档 | `600608964_ADAS_HSI.docx` | 370 个 chunk |
| 文档资产 | 10 条“影子档”，已生效 | 未认领、无标签 |
| 硬件资产 | `hdb_list_assets` 返回空 | 0 条 |

项目识别：AA250004 奇瑞 T1G ADAS 产品，零件号 600608964（来源：ICD `t_30_6` 第 2 行、FPT `t_32_5` 第 3~4 行）。

---

## 2. MCP 工具连通性实测

| MCP 工具 | 实测对象 | 结果 | 备注 |
|---|---|---|---|
| `hdb_list_kbs` | 全部知识库 | ✅ | AAA / ADAS / FPGA 三个 |
| `hdb_list_kb_files` | AAA | ✅ | 10 个源文件，均 indexed/parsed |
| `hdb_list_spreadsheets` | AAA | ✅ | 7 文件 / 70 sheet / 2319 语义行 |
| `hdb_list_tables` | AAA | ✅ | 61 张表，含列名与样例 |
| `hdb_query_table` | `t_28_6`、`t_29_4`、`t_30_6`、`t_32_5` | ✅ | 只读 SELECT 正常，支持 LIKE/聚合 |
| `hdb_list_circuit_designs` | AAA | ✅ | 1 个设计、1052 实例、504 网络 |
| `hdb_query_circuit` | `X1900`、`X1903`、`U1600`、`U1700`、`U1800` | ✅ | 可返回 refdes→pin→net 映射 |
| `hdb_read_file_chunks` | HSI docx | ✅ | 370 chunk，可分页读取 |
| `hdb_list_doc_assets` | AAA | ✅ | 10 条影子档，均为 effective |
| `hdb_list_assets` | AAA | ⚠️ | 返回空数组，无审核资产 |
| `hdb_ask_knowledge_base` | 供电/温度/休眠电流 | ❌ | 首次报“模型服务调用过于频繁或额度受限”，重试 300s 超时，无 evidence |

> 影响：本次结论全部走“表格 SQL + 网表查询 + 文档 chunk”取证，不依赖 RAG 问答。

---

## 3. 已核实的关键设计事实

| 项目 | 事实 | 值 | 出处 |
|---|---|---|---|
| 整车供电 | 12V 乘用车，蓄电池常电 + 燃油车发动机供电 | 12V 系统 | HW-REQ-45 / 46，`t_28_6` 第 258~259 行 |
| 主供电范围 | X1900-14 `UBD` 系统供电 | 9~16V | ICD `t_30_6` 第 29 行；FPT `t_32_5` 第 9~13 行 |
| KL15 唤醒输入 | 输入电压范围 / 有效 / 无效 / 唤醒 | 0~26.5V / ≥6V / ≤2V / 支持 | HW-REQ-1，`t_28_6` 第 33~37 行 |
| 休眠电流 | 休眠条件 | <100uA @ -40~85℃、9~16V | HW-REQ-47 / 48，`t_28_6` 第 266~267 行 |
| 工作温度 | 产品工作温度 | -40℃~85℃ | HW-REQ-57 / 58，`t_28_6` 第 314~315 行 |
| 需求总量 | 去重后的 HW-REQ | 66 条 | SQL `COUNT(DISTINCT col_4)` |
| 一级电源 | `UBD_PR` → LN10046（U1600）→ `VCC3V3` | — | 架构 `t_29_4` 第 29 行；网表 U1600 |
| 二级电源 | LP87702-Q1（U1700）→ `VCC1V8` / `VCC1V1` / `VCC50` | — | 架构 `t_29_4` 第 29 行；网表 U1700 |
| EQ6 内核 | TPS62872（U1800）→ `VCC0V75` | — | 网表 U1800 |
| MIPI/DDR | SA23002 → `0V75_MIPI_DDR` | — | 架构 `t_29_4` 第 29 行 |
| MCU 过压保护 | TLVH431 → `VCC3V3_MCU`，阈值 3.77~4.3V | ≥4.43V 时 1.5ms 内关断 | 架构 `t_29_4` 第 29 行；HW-REQ-24/25/42/43 |
| 负载开关 | TPS22918 ×2 → `VCC3V3_EQ` / `VCC3V3_ETH` | — | 架构 `t_29_4` 第 29 行 |
| 电源网络 | `hdb_query_circuit` 汇总 power_nets | 41 条（含 GND/PGND 与 4 条 EN，实际电源轨约 35 条） | 网表查询结果 |
| 连接器 X1900 | TE/1318382-8，16 pin | pin14=UBD，pin1=KL15，CAN0/1/2/3，ETH100 | ICD `t_30_6`；网表 X1900 |
| 连接器 X1902 | E6S20A-40MT5-C | pin1/2 = ETH1000_P/N | ICD `t_30_6` 第 37~47 行 |
| 连接器 X1903 | 101106-203050-HF，30 pin FPC | MIPI0 4-lane、I2C、CAM 电源回采 | 网表 X1903 |
| 关键器件 | TC377 MCU、EyeQ6L、LPDDR4、NAND FLASH、DP83TC812/DP83TG720、IMX728 | — | 需求 + 架构章节 |

> 注意：HW-REQ-1 的 0~26.5V 是 **KL15 唤醒输入**指标，不是主供电范围；主供电按 9~16V 口径使用。

---

## 4. 七项交付物开工条件

| # | 交付物 | 现有输入 | 缺口 | 结论 |
|---|---|---|---|---|
| ① | 关键器件清单 | 架构器件描述、网表 1052 实例（含 part_number / ERP）、需求约束 | 价格库、白名单、历史 BOM、器件手册 | 只能出“原理图反推 BOM 草稿”，不能出正式选型清单 |
| ② | 整体硬件方案框图 | F01~F28 模块、41 条电源网络、X1900/X1902/X1903 接口、MB/CAM 板级划分 | 用户确认绘图边界（是否含摄像头板/外部负载） | **可开工** |
| ③ | 电源计算表 | 电源轨结构、电压精度要求（HW-REQ-12/20/22/27） | 各芯片手册电流 typ/max、上电冲击电流 | 不可开工 |
| ③+ | 电源树 | ③的轨表 + 网表供电路径 | 每轨电流（可留空标“待确认”） | 可出**结构稿** |
| ④ | 接口定义表 | ICD X1900 16pin 全量、X1903 30pin、X1902、FPT 测试点 | **pin 序排列方式（必须问）**、ICD 中 X1902/X1903 的完整表 | **可开工**（先定 pin 序） |
| ⑤ | 上下电时序表 + 图 | 参与芯片清单、电源轨、EN 信号 | 各芯片 datasheet 的 EN/PG/RST 延时章节 | 不可开工 |
| ⑥ | 热风险分析表 | Ta=85℃（HW-REQ-57/58）、主要发热器件清单 | 各芯片功耗、θJA/θJC、散热结构热阻 | 不可开工 |
| ⑦ | 硬件方案汇总 | 以上定案 | 依赖 ①~⑥ | 最后做 |

补充：① 的“资源库”在 AAA 中不存在（`list_assets` 为空、无价格/白名单表），这是与标准五步法最大的输入差距。

---

## 5. 建议的试跑路径

| 选项 | 交付物 | 理由 | 前置确认 |
|---|---|---|---|
| A（推荐） | ② 整体硬件方案框图 drawio + PNG | 输入最齐，能体现 MCP 跨“架构文档 + 网表 + ICD”整合 | 绘图边界：仅主板，还是主板 + 摄像头板 |
| B | ④ 接口定义表 xlsx | 数据最硬，X1900 已有 ICD 与网表双向校验 | pin 序：按管脚号 / 按功能分组 / 按连接器 |
| C | ③+ 电源树结构稿 drawio + PNG | 快速可视化 35 条电源轨，电流列可先标“待确认” | 是否接受电流列留空 |

---

## 6. MCP 侧发现的问题

1. **RAG 问答不可用**：`hdb_ask_knowledge_base` 两次失败（额度受限 + 300s 超时），本次未能取得带引用的自然语言答案。建议检查模型服务配额与 turn 超时。
2. **表格语义层存在列错位**：`t_28_6` 的需求正文实际在 `col_5`、需求 ID 在 `col_4`，与表头声明的 `col_6/col_7` 不一致；按表头取数会取到“需求/Requirement”类型列。
3. **同一 sheet 多连接器混排**：`t_30_6` 把 X1900（16pin）与 X1902 放在同一张表，需按“在控制器上编号”行分段解析，不能整表当成一个连接器。
4. **文档资产全是影子档**：10 条记录 project/doc_no/category 未认领，按项目检索会漏；建议补全元数据。
5. **无审核硬件资产**：`list_assets` 为空，① 无法走“资产库/白名单”路径。
6. **网表疑似缺供电源**：`VCC1V25_MCU` 只见 U900C.VDD 与去耦电容，未见 regulator 输出；需人工核对原理图电源页（可能是网络别名或提取遗漏）。

---

## 7. 复现用的 MCP 调用

```text
hdb_list_kbs()
hdb_list_kb_files(kb_name="AAA")
hdb_list_spreadsheets(kb_name="AAA")
hdb_list_tables(kb_name="AAA")
hdb_list_circuit_designs(kb_name="AAA")
hdb_list_doc_assets(kb_name="AAA")
hdb_query_table(kb_name="AAA", sql="SELECT ... FROM t_28_6 ...")
hdb_query_table(kb_name="AAA", sql="SELECT ... FROM t_29_4 ...")
hdb_query_table(kb_name="AAA", sql="SELECT ... FROM t_30_6 ...")
hdb_query_circuit(kb_name="AAA", query="X1900")
hdb_query_circuit(kb_name="AAA", query="X1903")
hdb_query_circuit(kb_name="AAA", query="U1700")
hdb_read_file_chunks(kb_name="AAA", file_id="ragflow:37", max_chunks=6)
```

---

## 8. ② 系统框图试跑结果（2026-11-06 追加）

用户确认口径后，已用 AAA 的 HSI、ICD、需求表和 EDF 网表生成系统框图：

| 产物 | 路径 |
|---|---|
| drawio（可编辑） | `docs/reports/2026-11-06-aaa-mcp-trial/ADAS_TCN2_系统框图.drawio` |
| PNG（预览） | `docs/reports/2026-11-06-aaa-mcp-trial/ADAS_TCN2_系统框图.png` |
| 数据源与待打合 | `docs/reports/2026-11-06-aaa-mcp-trial/②_系统框图_数据源与待打合.md` |
| 生成脚本 | `docs/reports/2026-11-06-aaa-mcp-trial/gen_aaa_block.py` |

口径：主板 MB + 外部摄像头模组（经 X1903）；不画 X2000 调试连接器；不画 F06~F10 采样链路。

自检：`scan_issues.py`、`_tcheck.py`、`_collision.py`、`_xcheck.py`、`scan_text_block.py`、`_audit.py` 全部无输出。PNG 由同一坐标数据的 SVG 经 Chrome headless 渲染（当前环境无 drawio CLI）。原 `_audit.py`/`_geom.py` 不解析边内联 `value` 标签，已做最小修正（Edge 记录 value；_audit 跳过已内联标注的边）。

已发现的待打合项（详见数据源 md）：MCU 型号 TC377 vs TC367；以太网 100M/1000M 选贴；CAN 每路 3 选贴；SA23002 输出命名；SoC↔MCU SPI 在网表中未见直连。

---

## 9. 100% MCP 溯源实测（2026-11-06 追加）

用户提出“资料能不能 100% 来自 MCP”。实测结论：**可以**，逐条背书见《②_系统框图_100%MCP溯源验证.md》。关键做法：

- 结构化表 / ICD / FPT / 架构：`hdb_query_table` 精确行；
- 网表：`hdb_query_circuit` 逐条点查（U900A / U400-1 / U1400 / U503 / U1202 / U1203 / U1501 / X1900 / X1903 / X2000 / CAN0H…CANC3H / VCC50 / 0V75_MIPI_DDR / UBD / VCC3V3_MCU 等）；
- HSI 正文（F01~F28 模块总表、系统概述等）首次在 MCP 内取得：`read_file_chunks(max_chunks=100)`，命中 chunk 42/48/49/53/61/65/76/84；
- PDF 关键注记“千百兼容设计”由 `ask_knowledge_base` 命中（evidence_id 32d8d363…/2508b7be…）。

新发现缺陷：
- `hdb_download_kb_file` 报错 `cannot resolve the configured user for file download`（用户解析 bug，加入 P0 清单）；
- `read_file_chunks` 无 `start_index`/搜索，本次靠前 100 chunk 恰好覆盖，但换文档就可能失效；
- `ask_knowledge_base` 本会话 1/4 成功率（额度+超时交替），且超时不给部分证据。

SoC↔MCU SPI0 实际经 X2000 调试口布线（HSI chunk 84 + 网表 X2000 pin53~60），已并入待打合。

---

## 10. 复盘：为什么"之前不行"、这次行了（沉淀为 skill 纪律）

| 当时卡点 | 当时的判断 | 实际边界 | 结论归因 |
|---|---|---|---|
| HSI 正文 | read_file_chunks 默认 20 个 chunk 只出封面/说明 → "chunking 拿不到正文"，转本地 docx | max_chunks=100 即命中 chunk 53（模块总表）、84（SPI0 经 X2000） | 我的操作：默认参数当工具上限 |
| 网表 pin→net | 判定"必须整份 EDF 解析" | 框图事实可降维成 ~20 次点查询闭环 | 我的操作：把"噪音大/成本高"误判成"不可行" |
| RAG 问答 | 首次 429 → 整场判死"RAG 不可用" | 服务时通时断（4 失败 1 成功） | 我的操作：单次失败拉黑链路 |
| 下载/分页 | download_kb_file 直接报错；read_file_chunks 只能从头 | 均为真缺陷 | 工具问题（已进 P0 清单） |

**归因**：约七成是把默认参数/单次失败/成本高误判成工具极限，约三成是真缺陷（download 用户解析 bug、read_file_chunks 无 start_index、ask 不稳定、query_circuit 噪音）。

**沉淀**：已写入 skill——`hw-design-solution` SKILL.md 全局纪律第 7 条（MCP 优先，探完才许放弃）+ 新增 `references/mcp-probe.md`（探边纪律、逐工具探法与缺陷快照、降级登记模板、出结论自查）。工具缺陷修复落地后需重新实测并更新该快照日期。

---

## 11. v2 重新生成 + 旧版清理（2026-11-06）

按 skill 新纪律（含 MCP 优先/探完许弃）重新生成 ② 框图：

- **重生成增量**（均为重新用 MCP 点查拿到的新事实）：
  1. P7 加 `U1502 LN20042：UBD_PR→VCC_3V3_ETHPSD`（PHY 休眠域常电，支撑 ETH 唤醒链路）；
  2. SoC↔MCU 连线标注 `SPI0（经调试口）/ RST / EN`（HSI chunk 84 + 网表 X2000 pin53~60）；
  3. 电平转换块补 "用途：MCU ↔ EQ6 FLASH"（SPI1 经 U503→NAND）。
- **校验**：6 个 checks 脚本再次全绿；SoC↔MCU 线、P7 块、LEVEL 块三处局部放大目视复核。
- **旧版清理**：
  - `docs/reports/2026-11-06-aaa-mcp-trial/` 下 v1 同名产物（drawio/svg/png + 旧 md）被 v2 覆盖删除；
  - `storage/tmp-aaa/aaa_tcn2_block.drawio`、`storage/tmp-aaa/aaa_out/aaa_tcn2_block.drawio`（早期草稿，含"板对板 X2000 / LPDDR4×2 / CAN×12"等错误事实）**已删除**；
  - 保留：`gen_aaa_block.py`（v2 生成脚本）、试跑报告与溯源 md（工作记录）。

后续建议：④ 接口定义表（X1900 16pin + X1903 30pin + 网表 pin-net 已齐，差 pin 序确认）；或先修 P0 工具缺陷。
