"""Register the same two functions with a stdio MCP client."""

from mcp.server.fastmcp import FastMCP

from . import generate_image, get_image_result

server = FastMCP("ear-image")
server.tool()(generate_image)
server.tool()(get_image_result)


def main():
    server.run(transport="stdio")
