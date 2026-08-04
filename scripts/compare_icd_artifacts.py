"""Compare a generated ICD workbook with an approved/manual ICD workbook."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.document_authoring.icd_comparison import compare_workbooks
from src.pipelines.spreadsheet.xlsx_parser import parse_xlsx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, help="人工/已批准 ICD xlsx 文件")
    parser.add_argument("--generated", required=True, help="生成 ICD xlsx 文件")
    parser.add_argument("--output", required=True, help="比较 JSON 输出路径")
    args = parser.parse_args()

    result = compare_workbooks(parse_xlsx(args.reference), parse_xlsx(args.generated))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
