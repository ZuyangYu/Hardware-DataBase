"""Test-data domain skeleton.

Plan section 4.2/4.3 mandates that the test-data parser, store and query
engine live under their own package and never import from `src/circuit/**`.
This skeleton ships only the minimum surface needed so the rest of the
codebase (ParserRegistry, UnifiedQueryRouter, Streamlit UI) can already
depend on it; the actual parser logic is a TODO for the test-data
sub-team.
"""
