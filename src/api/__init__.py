"""HTTP API layer for Hardware DataBase.

This package wraps :class:`src.core.app_pipeline.AppPipeline` in a thin
FastAPI service. It is the future backend once the frontend/backend are
separated; the Streamlit app and any later frontend both become clients of
it. No business logic lives here -- only HTTP translation, auth, and
``RequestContext`` assembly, all of which delegate to existing modules.
"""
