# Chat session selector stability

## Scope

Make the Streamlit chat-session selector stable when a user switches knowledge
bases, creates a new session, resets chat state, or deletes the selected
session. Preserve the existing agent query flow and conversation persistence.

## Design

The selector key is derived from a monotonically increasing selector version
and the current knowledge-base name. Invalidating the selector increments that
version, so Streamlit treats the next selector as a new widget rather than
reusing a stale selected session id.

`ensure_current_chat_session` synchronizes the current session id to the
current selector key. The selectbox uses an `on_change` callback which loads
the selected session before the next script rerun. This prevents a stale
widget value from overwriting a newly selected or newly created session.

The sidebar also exposes deletion of the selected session. Deletion loads the
most recent remaining session, or resets state when none remain, and
invalidates the selector.

## Testing

Add focused unit tests for selector-key isolation by knowledge base and
version. Verify the chat-session helper behavior through the existing
conversation service integration points without changing agent execution.

## Compatibility

The change remains in `streamlit_app.py` except for a small pure helper if one
is needed for unit testing. It does not change `AppPipeline`, retrieval, agent
observability, or the current conversation database schema.
