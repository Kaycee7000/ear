"""Lightweight async tools for EAR agents."""

from typing import Any

from .agent import invoke
from .contract import Key, Seed, Wait


async def generate_image(
    prompt: str, seed: Seed | None = None, wait_seconds: Wait = 0, request_id: Key | None = None
) -> dict[str, Any]:
    """Submit one remote 512×512 image generation. Reuse request_id for idempotency."""
    return await invoke(
        "generate_image", prompt=prompt, seed=seed, wait_seconds=wait_seconds, request_id=request_id
    )


async def get_image_result(job_id: str, wait_seconds: Wait = 0) -> dict[str, Any]:
    """Resume a saved job. A completed result includes a freshly signed B2 download URL."""
    return await invoke("get_image_result", job_id=job_id, wait_seconds=wait_seconds)
