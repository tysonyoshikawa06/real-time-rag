"""Sync bridge from agent code to the streaming-rag MCP server, in-process.

fastmcp's Client API is async-only, but callers on the agent side (the
tool-use loop in loop.py, and this module's own smoke entry point) want plain
function calls. MCPBridge wraps the async `fastmcp.Client` in sync methods
via `asyncio.run`, so the rest of the agent package never has to juggle an
event loop.

The bridge connects with `fastmcp.Client(mcp)`, passing the live
`mcp_server.server.mcp` FastMCP instance directly as the transport. This is
an in-process connection - no subprocess, no stdio pipe - but it still speaks
the real MCP protocol, so it exercises the exact same tool-registration,
validation, and error-surfacing path a stdio client would. Each bridge call
opens its own short-lived `async with Client(mcp) as client:` session rather
than holding one open across calls: the in-process transport makes connecting
cheap, and this avoids the added complexity of managing a shared session's
lifetime (and its own event loop) across unrelated sync call sites.

Tool calls never raise on tool-level failure. Note this required one
adjustment to the assumption that motivated this design: fastmcp's
Client.call_tool defaults to raise_on_error=True, so by default it raises a
ToolError client-side for a delegate's ValueError (e.g.
mcp_server/validation.py rejecting a bad group_by) instead of just setting
is_error=True on the CallToolResult and returning normally. Passing
raise_on_error=False restores the documented MCP behavior - the call returns
a CallToolResult with is_error=True and the error text in .content - so
call_tool() below always gets a result object, never an exception, for both
success and tool-level failure. A defensive except is still kept around the
call for genuine connection-level failures (e.g. an unreachable server),
which are not tool errors and would otherwise still raise.
"""

import asyncio
import json

from fastmcp import Client

from mcp_server.server import mcp


def _tool_to_dict(tool) -> dict:
    """Convert an mcp.types.Tool into an Anthropic tool-definition dict.

    Anthropic's tool shape is {"name", "description", "input_schema"} - the
    MCP Tool object already carries valid JSON Schema in `.inputSchema`, so
    this is a field rename, not a schema conversion.
    """
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.inputSchema,
    }


def _result_to_text(result) -> str:
    """Flatten a CallToolResult's content blocks into one string.

    Returned whether result.is_error is True or False - both are valid tool
    output from the model's point of view, just one is an error message.
    """
    parts = [block.text for block in result.content if hasattr(block, "text")]
    return "\n".join(parts) if parts else str(result.content)


async def _list_tools_async() -> list[dict]:
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return [_tool_to_dict(tool) for tool in tools]


async def _call_tool_async(name: str, arguments: dict) -> str:
    async with Client(mcp) as client:
        # raise_on_error=False: get a CallToolResult with is_error=True on
        # tool-level failure instead of a raised ToolError (fastmcp's
        # default). Both success and failure return the same shape here.
        result = await client.call_tool(name, arguments, raise_on_error=False)
    return _result_to_text(result)


class MCPBridge:
    """Sync-friendly handle onto the in-process streaming-rag MCP server."""

    def list_tools(self) -> list[dict]:
        """Return Anthropic-shaped tool definitions for every registered MCP tool."""
        return asyncio.run(_list_tools_async())

    def call_tool(self, name: str, arguments: dict) -> str:
        """Call an MCP tool by name and return its result as text.

        Errors (validation failures, unknown tool names, etc.) come back as
        text content, never as a raised exception, so the caller can hand the
        result straight to a model as a tool_result block regardless of
        success or failure.
        """
        try:
            return asyncio.run(_call_tool_async(name, arguments))
        except Exception as exc:  # defensive: keep connection-level failures as content
            return f"MCP call to {name!r} failed: {exc}"


if __name__ == "__main__":
    bridge = MCPBridge()

    print("=== list_tools() ===")
    for tool_def in bridge.list_tools():
        print(f"\n--- {tool_def['name']} ---")
        print(f"description: {tool_def['description']}")
        print(f"input_schema: {json.dumps(tool_def['input_schema'], indent=2)}")

    print("\n=== call_tool('system_freshness', {}) ===")
    print(bridge.call_tool("system_freshness", {}))

    print("\n=== call_tool('query_stats', {'group_by': 'banana'}) ===")
    print(bridge.call_tool("query_stats", {"group_by": "banana"}))
