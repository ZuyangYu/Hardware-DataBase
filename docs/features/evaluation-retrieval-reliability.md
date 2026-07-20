# Evaluation and Retrieval Reliability Enhancements

## Summary

This feature branch extends the existing joint-retrieval workflow with more
reliable evaluation execution and stronger evidence-backed responses.

## Functional changes

- Persist and expose evaluation run state so interrupted runs can be resumed,
  controlled, and shown consistently in the application.
- Add evaluation preflight checks, presentation helpers, score visualization,
  and richer RAGAS result reporting.
- Improve retrieval evidence handling through claim-evidence integration,
  circuit metadata updates, and refreshed RAGFlow dataset mappings.
- Add LLM client resilience for rate limiting and improve application logging.
- Expand automated coverage for evaluation control, reporting, retrieval
  evidence, RAGFlow metadata, UI behavior, and LLM client handling.

## Repository hygiene

Generated evaluation results, coverage and log files, parser tables, local
agent workflow records, and development-only plans are excluded from version
control. The source code, tests, configuration, dependency lockfile, and this
feature summary remain versioned.
