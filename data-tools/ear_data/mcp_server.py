from mcp.server.fastmcp import FastMCP

from . import get_data_result, prepare_dataset_upload, run_data_job

server = FastMCP("ear-data")
for tool in (prepare_dataset_upload, run_data_job, get_data_result):
    server.tool()(tool)


def main():
    server.run(transport="stdio")
