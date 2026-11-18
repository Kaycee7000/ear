"""The review API shaped as agent tools.

For agents that use OpenAI-style function calling (Ollama, llama.cpp, vLLM, LM Studio...):

    from model_review.tools import TOOL_SCHEMAS, call_tool

    response = client.chat.completions.create(model=..., messages=..., tools=TOOL_SCHEMAS)
    for tc in response.choices[0].message.tool_calls or []:
        result = call_tool(tc.function.name, tc.function.arguments)   # dict, JSON-serialisable
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

For MCP agents, run `model-review-mcp` instead (same tools).
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from .client import ModelReviewClient, ReviewError

TOOL_SCHEMAS: list[dict] = json.loads(
    resources.files("model_review").joinpath("tool_schema.json").read_text(encoding="utf-8")
)
DEFAULT_WAIT_S = 120

_client: ModelReviewClient | None = None


def _get_client() -> ModelReviewClient:
    global _client
    if _client is None:
        _client = ModelReviewClient()
    return _client


def set_client(client: ModelReviewClient | None) -> None:
    """Override the client (tests, or a non-default repo)."""
    global _client
    _client = client


def review_model(model: str, suite: str = "smoke", backend: str = "ollama", endpoint: str | None = None,
                 runner: str | None = None, wait_seconds: int = DEFAULT_WAIT_S) -> dict:
    try:
        return _get_client().review_model(model, suite, backend, endpoint, runner, wait_s=max(0, wait_seconds))
    except ReviewError as exc:
        return {"state": "error", "message": str(exc)}


def get_review(run_id: int, wait_seconds: int = 0) -> dict:
    try:
        client = _get_client()
        if wait_seconds > 0:
            return client.wait_for_review(int(run_id), timeout_s=wait_seconds)
        return client.get_review(int(run_id))
    except ReviewError as exc:
        return {"state": "error", "run_id": run_id, "message": str(exc)}


def list_suites() -> dict:
    try:
        return {"suites": _get_client().list_suites()}
    except ReviewError as exc:
        return {"state": "error", "message": str(exc)}


TOOLS = {"review_model": review_model, "get_review": get_review, "list_suites": list_suites}


def call_tool(name: str, arguments: str | dict | None = None) -> dict:
    """Dispatch a function call by name. `arguments` may be the raw JSON string from the model."""
    if name not in TOOLS:
        return {"state": "error", "message": f"unknown tool {name!r}; available: {sorted(TOOLS)}"}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return {"state": "error", "message": f"arguments are not valid JSON: {exc}"}
    args: dict[str, Any] = dict(arguments or {})
    try:
        return TOOLS[name](**args)
    except TypeError as exc:
        return {"state": "error", "message": f"bad arguments for {name}: {exc}"}
