"""MCP server exposing the review tools over stdio.

    pip install "./model-review[mcp]"
    MODEL_REVIEW_TOKEN=github_pat_... model-review-mcp

Register it in your agent's MCP config, for example:

    {"mcpServers": {"model-review": {
        "command": "model-review-mcp",
        "env": {"MODEL_REVIEW_TOKEN": "github_pat_...", "MODEL_REVIEW_REPO": "Kaycee7000/ear"}}}}
"""

from __future__ import annotations

import asyncio

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import tools

INSTRUCTIONS = (
    "Tools for reviewing (testing and validating) AI models on GitHub Actions. "
    "Call list_suites to see available test suites, review_model to start a review, and "
    "get_review with the returned run_id if the review was still queued or running. "
    "A review takes a few minutes: GitHub has to start a runner and load the model."
)

server = _Server("model-review", instructions=INSTRUCTIONS)


@server.tool()
async def review_model(model: str, suite: str = "smoke", backend: str = "ollama",
                       endpoint: str | None = None, runner: str | None = None,
                       wait_seconds: int = tools.DEFAULT_WAIT_S) -> dict:
    """Review (test and validate) a model by running a test suite against it on GitHub Actions.

    model: Ollama tag (e.g. 'qwen2.5:0.5b'), 'hf.co/<user>/<repo>:<quant>', or the model name at `endpoint`.
    suite: test suite name (see list_suites).
    backend: 'ollama' (pull and serve), 'endpoint' (existing OpenAI-compatible server), or 'mock' (pipeline test).
    endpoint: OpenAI-compatible base URL, only for backend='endpoint'.
    runner: runner label or JSON list of labels; default is GitHub's free 'ubuntu-latest' CPU runner.
    wait_seconds: wait this long for the verdict; if not finished you get a run_id to pass to get_review.
    """
    return await asyncio.to_thread(tools.review_model, model, suite, backend, endpoint, runner, wait_seconds)


@server.tool()
async def get_review(run_id: int, wait_seconds: int = 0) -> dict:
    """State and, once finished, the verdict of a review started with review_model."""
    return await asyncio.to_thread(tools.get_review, run_id, wait_seconds)


@server.tool()
async def list_suites() -> dict:
    """List the test suites available for review_model."""
    return await asyncio.to_thread(tools.list_suites)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
