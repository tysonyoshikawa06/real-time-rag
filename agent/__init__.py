"""Agent package: an Anthropic-model-driven client for the streaming RAG MCP tools.

Where mcp_server exposes the tools, this package is the caller side: a bridge
that talks the MCP protocol to mcp_server.server.mcp in-process (mcp_bridge.py)
and, in a later sub-commit, a tool-use loop that lets a Sonnet-class model
decide which tools to call and when (loop.py, ask.py). Keeping the bridge
separate from the loop means the loop never touches mcp_server internals
directly - every tool call, real or invalid, goes through the same MCP path a
stdio client would use.
"""
