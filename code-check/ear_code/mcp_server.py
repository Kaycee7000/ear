from mcp.server.fastmcp import FastMCP

from . import get_code_result, prepare_source_upload, run_code_check

server = FastMCP("ear-code")
for tool in (prepare_source_upload, run_code_check, get_code_result):
    server.tool()(tool)


def main():
    server.run(transport="stdio")
