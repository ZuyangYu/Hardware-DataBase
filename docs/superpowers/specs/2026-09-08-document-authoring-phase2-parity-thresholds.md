# Document Authoring Phase 2 Parity Thresholds

**Recorded:** 2026-09-08
**Metric version:** `icd-pin-definition-v1`
**Threshold version:** `icd-pin-definition-thresholds-v1`
**Fixture:** `icd-pin-definition-baseline-v1`

## Scope and method

This is the offline baseline for the Phase 2 template-backed XLSX/XLSM
authoring path. The source fixture is
`tests/fixtures/document_authoring/icd_pin_definition/pin_definition_baseline.json`.
It is synthetic and reviewed for this benchmark; it contains no customer
workbook bytes or credentials. Tests construct minimal OOXML packages from the
fixture, parse their logical rows, and inspect package parts only. No Excel
process or macro is executed.

The comparator normalizes a Pin Definition row to the stable
`connector:pin_number` key and compares required columns, declared order,
duplicates, missing/extra keys, cross-field values, row/cell evidence support,
and static package protection/preservation observations. XLSX and XLSM are
compared by normalized logical output, not by byte equality.

The machine-readable gate is
`tests/fixtures/document_authoring/icd_pin_definition/baseline_thresholds.json`.
The threshold loader requires both versions and the fixture identity; an
inconclusive or missing metric fails the gate.

## Measured baseline

The benchmark ran the human baseline against both the generated XLSX and the
generated XLSM fixture (`comparison_count = 2`). Numerators and denominators
below are the aggregated values from both comparisons, rather than an average
of rounded percentages.

| Metric | Numerator / denominator | Measured value | Direction | Approved gate |
| --- | ---: | ---: | --- | ---: |
| `row_key_precision` | 6 / 6 | 1.000000 | minimum | 1.0 |
| `row_key_recall` | 6 / 6 | 1.000000 | minimum | 1.0 |
| `row_key_f1` | 12 / 12 | 1.000000 | minimum | 1.0 |
| `required_column_completeness` | 6 / 6 | 1.000000 | minimum | 1.0 |
| `exact_order_rate` | 2 / 2 | 1.000000 | minimum | 1.0 |
| `relative_order_rate` | 6 / 6 | 1.000000 | minimum | 1.0 |
| `duplicate_rate` | 0 / 6 | 0.000000 | maximum | 0.0 |
| `missing_row_rate` | 0 / 6 | 0.000000 | maximum | 0.0 |
| `extra_row_rate` | 0 / 6 | 0.000000 | maximum | 0.0 |
| `cross_field_consistency_rate` | 16 / 16 | 1.000000 | minimum | 1.0 |
| `evidence_support_rate` | 18 / 18 | 1.000000 | minimum | 1.0 |
| `physical_protection_rate` | 2 / 2 | 1.000000 | minimum | 1.0 |

Per-package observations are also fixed in the fixture manifest: both
packages have one protected sheet, one merged range, and no external links;
the XLSX has no `vbaProject.bin`, while the XLSM has the synthetic sentinel
`xl/vbaProject.bin`. The package preservation checks pass for both formats,
and the XLSM sentinel is preserved without being executed.

## Approval and rollout boundary

Approval record: this document records the reviewed synthetic baseline and the
versioned non-regression gate for Task 7 on 2026-09-08. It authorizes offline
benchmark evaluation only. Production allowlisting and enabling
`DOCUMENT_PLAN_DAG_EXECUTION_ENABLED` remain subject to the Phase 2 Task 8
rollout gate, including manual review of the fixture provenance and package
policy. A missing threshold file, version mismatch, fixture mismatch, or
inconclusive required metric is not a pass.
