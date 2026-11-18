"""Register the same two functions with a stdio MCP client."""

from mcp.server.fastmcp import FastMCP

from . import generate_document, get_document_result

server = FastMCP("ear-document")
server.tool()(generate_document)
server.tool()(get_document_result)


def main():
    server.run(transport="stdio")
