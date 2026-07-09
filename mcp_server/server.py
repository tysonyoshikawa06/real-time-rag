"""FastMCP server exposing the streaming RAG tools over stdio.

The server layer is deliberately thin: each tool opens a connection via
consumer.db.connect() (same DSN/config as the consumer), delegates to the
pure query function, and closes the connection. All query logic and input
validation lives in the delegate modules (stats.py), which keeps the tools
testable without MCP transport — and lets FastMCP's in-memory client exercise
the full path in integration tests.

Run with: make mcp (stdio server) or make mcp-dev (MCP inspector).
"""

from fastmcp import FastMCP

from consumer.db import connect
from mcp_server import stats

mcp = FastMCP("streaming-rag")


@mcp.tool
def query_stats(
    window_minutes: int = 30,
    group_by: str | None = None,
    filters: dict[str, str] | None = None,
    metric: str = "count",
    limit: int = 10,
) -> dict:
    """Aggregate transactions in a recent time window.

    Answers counting questions like "how many failures in the last 30 minutes,
    by gateway?". group_by and filter keys accept: method, status, gateway,
    merchant. metric is "count" or "failure_rate" (fraction of failures per
    group, 0-1). Returns total_events for the filtered window plus up to
    `limit` rows ordered by the metric, descending.
    """
    conn = connect()
    try:
        return stats.query_stats(
            conn,
            window_minutes=window_minutes,
            group_by=group_by,
            filters=filters,
            metric=metric,
            limit=limit,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
