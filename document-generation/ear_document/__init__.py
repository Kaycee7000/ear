"""Async EAR document tools. Renderers live exclusively in the disposable worker."""

from typing import Any

from .agent import invoke
from .contract import Format, Key, Outline, Wait


async def generate_document(
    outline: Outline, format: Format, wait_seconds: Wait = 0, request_id: Key | None = None
) -> dict[str, Any]:
    """Render an agent-written outline as a PDF, Word document, or PowerPoint deck. Reuse request_id to deduplicate."""
    return await invoke(
        "generate_document",
        outline=outline,
        format=format,
        wait_seconds=wait_seconds,
        request_id=request_id,
    )


async def get_document_result(job_id: str, wait_seconds: Wait = 0) -> dict[str, Any]:
    """Resume a document job and issue a fresh private B2 download link on completion."""
    return await invoke("get_document_result", job_id=job_id, wait_seconds=wait_seconds)
