#!/usr/bin/env python3
"""Generate a reviewable ICD candidate from an ICD template and project files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.document_authoring.icd_generation import generate_icd_workbook


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, help="ICD .xlsx/.xlsm template")
    parser.add_argument("--edf", required=True, help="Schematic EDF source")
    parser.add_argument(
        "--connector-refdes",
        required=True,
        nargs="+",
        help="Connector reference designators in the requested ICD scope",
    )
    parser.add_argument("--fpt", help="Optional FPT workbook for direct functional statements")
    parser.add_argument("--requirements", help="Optional requirements workbook for connector models")
    parser.add_argument("--output", required=True, help="Candidate ICD output path")
    parser.add_argument("--manifest", help="Optional JSON source/integrity manifest path")
    args = parser.parse_args()

    result = generate_icd_workbook(
        template_path=args.template,
        edf_path=args.edf,
        connector_refdes=args.connector_refdes,
        fpt_path=args.fpt,
        requirements_path=args.requirements,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result.content)
    if args.manifest:
        Path(args.manifest).write_text(json.dumps({
            "source_summary": result.source_summary,
            "integrity_manifest": result.integrity_manifest,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
