# Token Usage Display Design

## Goal

After every chat answer, show the model token usage for that question: total input, output, and total tokens, plus per-stage costs for the agent calls that produced the answer.

## Design

Token usage is captured at the project-owned `LLMClient` boundary. Each `chat` or `stream_chat` call records provider, model, stage, prompt tokens, completion tokens, total tokens, and whether usage was returned by the provider. OpenAI-compatible non-streaming responses read the standard `usage` object. OpenAI-compatible streaming requests include `stream_options.include_usage=true` and record the final usage event when the provider sends it. Ollama calls map `prompt_eval_count` to input tokens and `eval_count` to output tokens.

The `MultiSourceAgentRunner` resets usage at the start of each answer, labels calls by agent stage, and exposes a summary after streaming finishes. Streamlit renders the summary below the answer in a collapsed `Token 使用量` expander and persists it in the assistant message after the existing agent-observation footer so history replay keeps the same information.

## Scope

- Do not estimate tokens when the provider does not return usage.
- Do not add new tokenizer dependencies.
- Keep the token panel display-only; no billing or quota enforcement in this change.
- Preserve existing chat streaming behavior.

