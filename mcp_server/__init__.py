"""MCP server package: tools that let an agent query the streaming RAG store.

Layout mirrors the testability split used elsewhere in the repo: pure query
functions live in their own modules (stats.py) and take an existing psycopg
connection, so they are testable without MCP transport. server.py is the thin
FastMCP layer that owns connections and tool registration.
"""
