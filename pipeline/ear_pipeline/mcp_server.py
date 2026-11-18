"""MCP server (stdio) for the EAR pipeline: analyze_binary, get_analysis, get_function_code.

    pip install -r modeling/requirements-agent.txt ./pipeline
    EAR_MODELING_DIR=$PWD/modeling EAR_GITHUB_TOKEN=github_pat_... EAR_B2_...=... ear-pipeline-mcp
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import client

INSTRUCTIONS = (
    "Private reverse-engineering pipeline on GitHub Actions. analyze_binary extracts evidence from a binary with "
    "radare2 + Ghidra (imports, strings, decompiled functions), then the modeling tool builds a design of how the "
    "program is built in which every component and relationship cites that evidence. Extraction takes a minute or "
    "two and the design several more; poll get_analysis with the run_id. get_function_code returns one decompiled "
    "function. Static analysis only: the binary is never run. Everything stays in the owner's private B2 bucket."
)
server = _Server("ear-pipeline", instructions=INSTRUCTIONS)
PENDING = {"extracting", "designing"}


def _safe(fn, *args) -> dict[str, Any]:
    try:
        return fn(*args)
    except Exception as exc:  # report to the model instead of failing the tool call
        return {"state": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"}


def _wait(run_id: int, wait_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        result = _safe(client.status, int(run_id))
        if result.get("state") not in PENDING or time.monotonic() >= deadline:
            return result
        time.sleep(15)


def _analyze(binary_url, binary_key, goal, functions, design, wait_seconds):
    started = _safe(client.analyze, binary_url, binary_key, goal, functions, design)
    if started.get("state") == "error" or wait_seconds <= 0:
        return started
    return {"request_id": started["request_id"], **_wait(started["run_id"], wait_seconds)}


@server.tool()
async def analyze_binary(binary_url: str = "", binary_key: str = "", goal: str = "", functions: str = "",
                         design: bool = True, wait_seconds: int = 120) -> dict[str, Any]:
    """Extract evidence from a binary and (by default) build a cited design of how it is built.

    binary_url: https:// URL of the binary (50 MB max), or
    binary_key: an object the owner uploaded to the bucket under ear-extract/binaries/.
    goal: what the design should explain (optional, 1000 characters max).
    functions: comma-separated function names to decompile first, e.g. "main,parse_config" (optional).
    design: false = evidence only (a minute or two); true also runs the modeling step (several minutes more).
    wait_seconds: wait this long for a result; otherwise poll get_analysis with the returned run_id.
    """
    return await asyncio.to_thread(_analyze, binary_url, binary_key, goal, functions, design, wait_seconds)


@server.tool()
async def get_analysis(run_id: int, wait_seconds: int = 0) -> dict[str, Any]:
    """State of a pipeline run (extracting, designing, done, partial or error) with the extraction summary,
    the evidence key and, when ready, the design. Starts the modeling step itself once extraction succeeds."""
    return await asyncio.to_thread(_wait, run_id, wait_seconds)


@server.tool()
async def get_function_code(run_id: int, name: str) -> dict[str, Any]:
    """The decompiled code of one function from a finished extraction (6000 characters max)."""
    return await asyncio.to_thread(_safe, client.function_code, int(run_id), name)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
