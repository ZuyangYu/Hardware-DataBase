# CAM XLSM 包级安全 PoC（只读）

检查日期：2026-07-23

目标模板：`22_825504681 825504682_CAM_硬件原理图设计评审检查单.xlsm`

| 项目 | 结果 |
| --- | --- |
| SHA-256 | `166dd6b78fc1ee6fee2644a18c417e6780bdcef3fd7d5bd27578af1cd6918916` |
| OOXML Part 数 | 178 |
| relationship Part 数 | 39 |
| VBA | `xl/vbaProject.bin` |
| 外部链接 | 6 组 `xl/externalLinks/*`（含 relationship） |
| 嵌入/控件 | 7 个 `ctrlProps`，3 个 Visio `.vsd` 嵌入对象 |
| 自动结论 | `requires_approval` |

结论：此模板包含主动内容，不能使用通用工作簿库的 load/save 路径，也不能由默认 Renderer Policy 直接生成。当前 P2a `XlsmRenderer` 会复制原始 OOXML 包、仅改批准单元格，并验证关系和主动内容 Part 没有变动；但在该精确 SHA-256 被人工批准并加入 Renderer Policy allowlist 前，它会拒绝渲染。

尚需完成的环境验收：在目标 Microsoft Excel 版本中执行 3～5 个 allowlist 单元格的写入后，打开、保存和重新打开；同时确认宏签名、外部链接、Visio 对象和计算属性的行为。LibreOffice 结果只作为补充，不能替代 Excel 兼容结论。
