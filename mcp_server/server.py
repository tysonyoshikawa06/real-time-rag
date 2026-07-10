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
from consumer.embedder import LocalEmbedder
from mcp_server import freshness, semantic, stats, transactions

mcp = FastMCP("streaming-rag")

# Loading the model is the expensive part (~80MB download + init) — load it
# once at import time and reuse it across every semantic_search call, never
# reconstruct it per request.
_embedder = LocalEmbedder()


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


@mcp.tool
def semantic_search(
    query: str,
    window_minutes: int = 30,
    gateway: str | None = None,
    k: int = 10,
) -> dict:
    """Search recent failure text by meaning and return the matching transactions.

    Use this tool for fuzzy, meaning-based questions over messy error text —
    things like "is anything unusual in the errors?", "find failures similar
    to X", "are there new/novel error patterns?", or "what are the timeout
    errors saying?" — where the right match isn't a fixed keyword or category.
    It embeds `query` and returns the k most semantically similar failure
    events from the last `window_minutes`, optionally narrowed to a single
    `gateway`.

    Use query_stats instead when the question is about counting, rates, or
    top-N breakdowns (e.g. "how many failures in the last hour by gateway?" or
    "what's the failure rate for stripe-proxy?") — that tool aggregates,
    this tool does not.

    This tool returns individual matching transactions, each with its own
    transaction_id, similarity score, and details (gateway, method, amount,
    status, timestamp, and the embedded failure text) — not an aggregate or
    summary. Cite the transaction_id(s) when reporting results from this tool.
    """
    conn = connect()
    try:
        return semantic.semantic_search(
            conn,
            _embedder,
            query,
            window_minutes=window_minutes,
            gateway=gateway,
            k=k,
        )
    finally:
        conn.close()


@mcp.tool
def get_transactions(
    transaction_ids: list[str] | None = None,
    window_minutes: int | None = None,
    status: str | None = None,
    gateway: str | None = None,
    method: str | None = None,
    limit: int = 10,
) -> dict:
    """Fetch complete individual transaction rows, by ID or by filter.

    Use this to drill down into specific transactions after a query_stats
    aggregate or a semantic_search match — pass the transaction_id(s) you
    want to cite and get back their full rows, including the raw error_text
    and card_bin needed to describe them concretely. You can also use it
    without IDs to pull a representative sample of example rows behind a
    filter, e.g. "show me example failures from stripe-proxy in the last 10
    minutes" (status="failure", gateway="stripe-proxy", window_minutes=10).

    Pass transaction_ids for ID mode, or window_minutes/status/gateway/method
    for filter mode — not both in the same call. In filter mode,
    window_minutes defaults to 30 and rows come back newest first, capped at
    `limit`. In ID mode, any requested ID with no matching row is listed in
    missing_ids rather than causing an error.

    This is the drill-down step after query_stats (which only returns
    aggregates, never individual rows) or semantic_search (which finds
    matches by meaning but is not meant for pulling a plain filtered sample)
    — use those tools first to find what's interesting, then this tool to
    see the full rows behind it.
    """
    conn = connect()
    try:
        return transactions.get_transactions(
            conn,
            transaction_ids=transaction_ids,
            window_minutes=window_minutes,
            status=status,
            gateway=gateway,
            method=method,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool
def system_freshness(window_minutes: int = 5) -> dict:
    """Report how current the data is, as ingest-lag percentiles.

    Use this to answer or caveat "is this current?" / "how fresh is this
    data?" style questions. It measures ingest lag — the delay between an
    event happening (event_timestamp) and becoming queryable in Postgres
    (ingested_at) — over the last `window_minutes` (default 5). It does not
    measure query latency or system uptime.

    Returns event_count plus p50/p95/p99/max lag in seconds and a short
    human_readable summary line. If no events fall in the window, the
    percentile fields come back as None with an explanatory message rather
    than an error.

    Unlike query_stats/semantic_search/get_transactions, this tool does not
    open a database connection here: consumer.freshness.query_freshness()
    already manages its own connection internally, so mcp_server.freshness
    calls it directly rather than being handed one — that asymmetry is
    intentional, not an oversight.
    """
    return freshness.system_freshness(window_minutes)


if __name__ == "__main__":
    mcp.run()
