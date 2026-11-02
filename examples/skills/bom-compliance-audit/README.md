# BOM Compliance Audit Skill

这是可直接上传到 HDB「扩展 → 上传个人 Skill → 执行型」的示例 Skill 源包。

## 打包

```bash
cd examples/skills/bom-compliance-audit
python - <<'PY'
import zipfile
from pathlib import Path
root = Path('.')
with zipfile.ZipFile('../bom-compliance-audit.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if path.is_file() and '__pycache__' not in relative.parts and path.suffix != '.pyc':
            z.write(path, relative.as_posix())
PY
```

## 本地执行

Skill 在 Runner 中使用 `/work/in` 和 `/work/out`。本地验收可以用环境变量覆盖：

```bash
HDB_SKILL_IN=/tmp/bom/in HDB_SKILL_OUT=/tmp/bom/out \
  python examples/skills/bom-compliance-audit/entrypoint.py
```

`/tmp/bom/in/parameters.json` 示例：

```json
{
  "bom_rows": [
    {"reference": "R1", "part_number": "10k", "quantity": 2,
     "voltage_v": 3.3, "min_voltage_v": 2.0, "max_voltage_v": 5.0},
    {"reference": "C1", "part_number": "100nF", "quantity": 1}
  ]
}
```

## 结果

- `completed`：输出 `normalized-bom.json`、`summary.md`
- `validation_failed`：额外输出 `validation-report.json`，包含重复位号、缺少型号、非法数量和电压越界等问题
- `needs_user_input`：没有参数行和输入文件时返回 `bom_rows` / `bom_csv_or_json_asset`

实际验收可运行：

```bash
./.venv/bin/python -m pytest -q tests/test_bom_compliance_skill.py
```
