import json
import sys
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None


@unittest.skipUnless(ClientSession, "install the MCP extra to exercise stdio")
class Registration(unittest.IsolatedAsyncioTestCase):
    async def test_installed_stdio_tools_and_schema(self):
        schema = json.loads(files("ear_image").joinpath("tool_schema.json").read_text())
        expected = {tool["function"]["name"]: tool["function"]["parameters"] for tool in schema}
        directory = Path(sys.executable).parent
        with tempfile.TemporaryDirectory() as tmp:
            params = StdioServerParameters(
                command=str(directory / "ear-image-mcp"),
                args=[],
                env={
                    "EAR_IMAGE_DB": str(Path(tmp) / "state.sqlite3"),
                    "EAR_IMAGE_B2_REGION": "us-west-004",
                    "EAR_IMAGE_B2_BUCKET": "test-bucket",
                    "EAR_IMAGE_B2_READ_KEY_ID": "test",
                    "EAR_IMAGE_B2_READ_KEY": "test",
                    "EAR_IMAGE_GITHUB_TOKEN": "test",
                },
            )
            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                self.assertEqual({t.name for t in tools}, set(expected))
                for tool in tools:
                    self.assertEqual(
                        set(tool.inputSchema["properties"]), set(expected[tool.name]["properties"])
                    )
                result = await client.call_tool("get_image_result", {"job_id": "invalid"})
                self.assertFalse(result.isError)
                self.assertEqual(result.structuredContent["error_code"], "invalid_input")
