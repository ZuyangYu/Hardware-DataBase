# ICD Pin Definition parity fixtures

These are small, synthetic, offline fixtures for the Phase 2 parity gate.
They intentionally contain only normalized example rows, fixture provenance,
and package-shape expectations; they are not copies of a customer workbook.

The JSON fixture is used to build an in-memory `ParsedWorkbook` and minimal
OOXML packages in tests. The XLSM package contains a non-executable sentinel
`vbaProject.bin`; tests inspect package parts only and never execute macros or
open the files through Excel.

`baseline_thresholds.json` is versioned with the fixture and is the required
machine-readable gate input. The companion Phase 2 parity-thresholds spec
records the measured values and rollout status.
