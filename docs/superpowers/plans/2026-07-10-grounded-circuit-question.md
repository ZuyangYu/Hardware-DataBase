# Grounded Circuit Question Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add evidence-grounded handling for open-ended connection, bias, power-path and protection questions across EDF and RAGFlow manuals.

**Architecture:** A deterministic circuit-question analyzer selects bounded topology operations. `CircuitIndexService` maps their EDF facts to evidence. The LangGraph retrieval node derives a bounded set of exact part-number document queries when a question also needs datasheet capability evidence.

**Tech Stack:** Python 3.12, Pydantic, unittest, LangGraph, existing CircuitStore/CircuitQueryEngine and RAGFlow backend.

## Global Constraints

- Keep `CircuitQueryTool.run(...) -> list[Evidence]` unchanged.
- Preserve KB, department, source and record scope filtering.
- Do not use an LLM result as a circuit fact.
- Do not label circuit-only protection topology as a confirmed IC protection capability.
- Do not restore legacy query-router or legacy circuit-agent paths.

---

### Task 1: Normalize broad circuit questions

**Files:**
- Create: `src/circuit/question_analysis.py`
- Test: `tests/test_circuit_question_analysis.py`

**Interfaces:** Produces `CircuitQuestionPlan(operations: tuple[str, ...], requires_datasheet: bool)` from `analyze_question(question)`.

- [ ] Write tests that assert `上拉电源是否共用` selects `bias`, `电源输出是否有短地保护` selects `power_path` and `protection` with `requires_datasheet=True`, and `CAN0 连接` selects `connection`.
- [ ] Run `uv run python -m pytest tests/test_circuit_question_analysis.py -v`; verify import failure.
- [ ] Implement immutable plan dataclass and normalized Chinese/English term sets. Unknown questions return an empty plan.
- [ ] Re-run the test file; verify PASS.

### Task 2: Produce bias and protection topology evidence

**Files:**
- Modify: `src/circuit/query_engine.py`
- Modify: `src/circuit/evidence_mapper.py`
- Modify: `src/circuit/index_service.py`
- Modify: `tests/test_circuit_index_service.py`

**Interfaces:** `search_bias_topologies(kb_name, limit)` and `search_protection_topologies(kb_name, limit)` return rows with `design_id`, refdes, part data, pin/net facts, topology type, certainty and confidence. `CircuitIndexService.query` calls only operations present in the plan.

- [ ] Add a fixture with 4.7K signal-to-VCC pull-up, signal-to-GND pull-down, 0R link, and TVS signal-to-GND protection device.
- [ ] Add failing assertions that Chinese bias/protection questions return `derived_topology` evidence and exclude the 0R link from pull-up results.
- [ ] Implement minimal component classification and topology scans; emit only concrete pin/net connections and preserve allowed-design filtering.
- [ ] Map rows to evidence with `metadata.evidence_kind`, `metadata.part_numbers`, `metadata.certainty` and stable topology locators.
- [ ] Run `uv run python -m pytest tests/test_circuit_index_service.py -v`; verify PASS.

### Task 3: Retrieve manuals for discovered part candidates

**Files:**
- Modify: `src/agents/graph.py`
- Modify: `src/agents/prompts.py`
- Modify: `tests/test_agentic_runner.py`

**Interfaces:** `_derived_datasheet_calls(question, circuit_hits, tools) -> list[dict]` returns at most four `document_rag` calls for unique part numbers only when `analyze_question(question).requires_datasheet` is true.

- [ ] Add failing tests with a fake circuit evidence row asserting the derived query includes its part number and `document_rag`, and with a bias-only question asserting no derived call.
- [ ] Run `uv run python -m pytest tests/test_agentic_runner.py -v`; verify the new assertions fail.
- [ ] Implement deterministic extraction, deduplication, and a second bounded execution pass in `retrieve_evidence`; leave original retrieval diagnostics intact.
- [ ] Update final-answer prompt instructions to distinguish confirmed capability, observed topology and not-confirmable claims based on evidence kinds.
- [ ] Re-run focused agent tests; verify PASS.

### Task 4: Regression verification

**Files:**
- Test: `tests/test_circuit_question_analysis.py`
- Test: `tests/test_circuit_index_service.py`
- Test: `tests/test_agentic_runner.py`

- [ ] Run `uv run python -m pytest tests/test_circuit_question_analysis.py tests/test_circuit_index_service.py tests/test_agentic_runner.py -v`.
- [ ] Run `uv run python -m pytest`.
- [ ] Run `uv run ruff check src/circuit/question_analysis.py src/circuit/query_engine.py src/circuit/evidence_mapper.py src/circuit/index_service.py src/agents/graph.py`.
- [ ] Commit only the feature files and tests with `git commit -m "feat: ground broad circuit questions"`.
