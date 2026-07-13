# Token Usage Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show per-answer model token usage totals and per-stage usage in the chat UI.

**Architecture:** Capture provider usage inside `src/core/llm_client.py`, aggregate it in `src/agents/runner.py`, expose it through `src/core/app_pipeline.py`, and render/persist it from `streamlit_app.py`.

**Tech Stack:** Python 3.12, Streamlit, requests, unittest/pytest, existing project dataclasses.

## Global Constraints

- No token estimation when provider usage is missing.
- No new dependencies.
- Do not revert existing uncommitted changes.
- Preserve streaming response behavior.

---

### Task 1: LLM Usage Capture

**Files:**
- Modify: `src/core/llm_client.py`
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Produces: `LLMUsageRecord`, `LLMUsageSummary`, `LLMClient.reset_usage()`, `LLMClient.get_usage_records()`, `LLMClient.get_usage_summary()`

- [ ] Write failing tests for OpenAI-compatible chat, OpenAI-compatible stream, and Ollama stream usage extraction.
- [ ] Run `uv run python -m pytest tests/test_llm_client.py -q` and confirm the new tests fail because usage capture does not exist.
- [ ] Add dataclasses and usage extraction logic.
- [ ] Re-run the focused test and confirm it passes.

### Task 2: Agent Stage Aggregation

**Files:**
- Modify: `src/agents/graph.py`
- Modify: `src/agents/runner.py`
- Modify: `src/core/app_pipeline.py`
- Test: `tests/test_agentic_runner.py`

**Interfaces:**
- Consumes: `LLMClient.reset_usage()` and `LLMClient.get_usage_summary()`
- Produces: `MultiSourceAgentRunner.get_last_token_usage_summary()` and `AppPipeline.get_last_token_usage_summary()`

- [ ] Write a failing runner test using a fake LLM client that records usage stages.
- [ ] Run the specific runner test and confirm it fails because the summary is not exposed.
- [ ] Reset usage at stream start, label each LLM call with `usage_stage`, and expose the last summary.
- [ ] Re-run the runner test.

### Task 3: Streamlit Rendering

**Files:**
- Modify: `streamlit_app.py`

**Interfaces:**
- Consumes: `pipeline.get_last_token_usage_summary()`
- Produces: helper functions that format, split, render, and persist a `Token 使用量` footer.

- [ ] Add helper tests only if a local Streamlit helper test pattern exists; otherwise keep this as a small UI integration change.
- [ ] Render the token summary in a collapsed expander after the answer finishes.
- [ ] Persist the token footer after the answer so historical chat messages replay it.
- [ ] Ensure history stripping excludes token footer from future prompt history.

### Task 4: Verification

- [ ] Run `uv run python -m pytest tests/test_llm_client.py tests/test_agentic_runner.py -q`.
- [ ] Review `git diff --stat` and the touched files.
- [ ] Report any tests that could not run.

