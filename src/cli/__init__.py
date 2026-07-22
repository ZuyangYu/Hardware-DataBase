"""Hardware DataBase CLI client.

Thin HTTP client for the API in :mod:`src.api`. No business logic -- it only
authenticates, calls endpoints, and renders output. This is the same client
that will later be reused by an MCP server wrapping the same API.
"""
