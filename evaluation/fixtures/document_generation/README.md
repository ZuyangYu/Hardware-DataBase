# document_generation fixtures

由 `build_fixtures.py` 确定性生成（固定 zip 时间戳，可复现 sha256）。
`fixture_index.json` 冻结每个 fixture 的 hash、template/schema 引用与预期 artifact stage；
`evaluation/datasets/document_generation_v1.manifest.json` 引用同一组 hash。

- 覆盖：xlsx 标量、xlsx 表格（repeating_table）、docx 文本、缺失数据（mark_tbd / keep_blank）、
  证据冲突、越界证据（scope violation）。
- 不依赖在线 RAGFlow；`allowed_sources` 为冻结快照内的来源名。
- 修改任何 fixture 必须重跑 `build_fixtures.py`、更新 manifest 并升数据集版本。
