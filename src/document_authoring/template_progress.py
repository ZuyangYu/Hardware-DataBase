"""Progress reporting for the governed template-upload pipeline.

The upload/analysis/activation flow in ``DocumentGenerationService`` reports
its lifecycle through a lightweight callback so the Streamlit UI and the API
layer can surface progress without coupling to the service internals.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, Field


class TemplateProgress(BaseModel):
    """A single lifecycle event emitted during governed template upload.

    ``stage`` is the stable machine-readable identifier (e.g. ``upload_started``,
    ``sanitization_completed``, ``analysis_persisted``, ``activation_completed``).
    ``template_version_id`` is populated once a template version is known.
    """

    stage: str
    template_version_id: str | None = None
    error_type: str | None = None
    unit_count: int | None = None
    writable_unit_count: int | None = None


TemplateProgressCallback = Callable[[TemplateProgress], None]


def report_template_progress(
    progress_callback: TemplateProgressCallback | None,
    progress: TemplateProgress,
) -> None:
    """Forward a progress event to the callback when one is attached.

    The callback is optional and fail-soft: absence of a listener is not an
    error, and a failure inside the callback never aborts the pipeline.
    """
    if progress_callback is None:
        return
    try:
        progress_callback(progress)
    except Exception:
        # Progress reporting is best-effort; never let it break the operation.
        pass