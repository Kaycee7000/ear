"""model-review: test and validate a local AI model on GitHub Actions.

Pieces:
  runner       runs a YAML test suite against an OpenAI-compatible endpoint (used inside Actions)
  checks       the assertions a suite can make about a model's output
  suite        loads and validates suite files
  mock_server  a tiny OpenAI-compatible server for pipeline self-tests
  client       what an agent calls: dispatch a review run, wait, fetch the verdict
  tools        the same calls shaped as agent tools (function calling)
  mcp_server   the same tools over MCP
"""

__version__ = "0.1.0"
