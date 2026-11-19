import json
import sys
import unittest
from importlib.resources import files
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None


@unittest.skipUnless(ClientSession, "requires the MCP extra")
class Tools(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_registration(self):
        path = Path(sys.executable).parent / "ear-code-mcp"
        config = StdioServerParameters(command=str(path), args=[])
        expected = json.loads(files("ear_code").joinpath("tool_schema.json").read_text())
        async with stdio_client(config) as (r, w), ClientSession(r, w) as client:
            await client.initialize()
            loaded = {t.name: t.inputSchema for t in (await client.list_tools()).tools}
            self.assertEqual(set(loaded), {t["function"]["name"] for t in expected})
            for t in expected:
                func = t["function"]
                self.assertEqual(set(loaded[func["name"]]["properties"]), set(func["parameters"]["properties"]))
            rejected = await client.call_tool("run_code_check", {"source": "a" * 64 + ".tar.gz", "project": "p",
                                                                 "plan": {"mode": "test"}})
            self.assertTrue(rejected.isError)  # a test without steps fails schema validation
            # A well-formed call without storage settings reports the configuration problem as data.
            result = await client.call_tool("run_code_check", {"source": "a" * 64 + ".tar.gz", "project": "p",
                                                               "plan": {"mode": "review"}})
            self.assertFalse(result.isError)
            self.assertEqual(result.structuredContent["error_code"], "configuration")


if __name__ == "__main__":
    unittest.main()
