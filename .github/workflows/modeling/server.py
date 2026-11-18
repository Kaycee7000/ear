"""Stdio MCP entry point for local AI agents."""
from typing import Any

from mcp.server.fastmcp import FastMCP

from design import Design, Goal, Source, Task, synthesize
from jobs import status, submit

mcp = FastMCP("ear-modeling")


@mcp.tool()
def model_design(goal: Goal, evidence_key: str) -> dict[str, Any]:
    """Dispatch one repository modeling job using evidence in private B2. Returns a run ID."""
    return submit(goal, evidence_key)


@mcp.tool()
def model_design_status(run_id: int) -> dict[str, Any]:
    """Check a GitHub run. On success, return the private design. Safe to poll."""
    return status(run_id)


@mcp.tool()
def model_design_local(goal: Goal, sources: list[Source]) -> Design:
    """Synthesize inline evidence using local inference without starting a GitHub job."""
    return synthesize(Task(goal=goal, sources=sources))


if __name__ == "__main__":
    mcp.run(transport="stdio")
