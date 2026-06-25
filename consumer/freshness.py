"""Freshness query: ingest-lag percentiles over a recent window.

Reusable module — Step 13's system_freshness MCP tool will call
query_freshness() directly.
"""

import psycopg

from consumer.config import POSTGRES_DSN

_FRESHNESS_SQL = """
    SELECT
        count(*) AS event_count,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY lag_sec) AS p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY lag_sec) AS p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY lag_sec) AS p99,
        max(lag_sec) AS max_lag
    FROM (
        SELECT extract(epoch FROM (ingested_at - event_timestamp)) AS lag_sec
        FROM transactions
        WHERE event_timestamp >= now() - %(window)s::interval
    ) t
"""


def query_freshness(window: str = "5 minutes") -> dict | None:
    """Return freshness stats for events within the given window.

    Uses event_timestamp (not ingested_at) for the window filter so that
    old backlog events with stale event_timestamps don't pollute the
    metric. We want to measure the freshness of the *live* stream, not
    how long it took to drain a historical backlog.

    Returns None if no events fall within the window.
    """
    conn = psycopg.connect(POSTGRES_DSN)
    try:
        cur = conn.cursor()
        cur.execute(_FRESHNESS_SQL, {"window": window})
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None or row[0] == 0:
        return None

    return {
        "event_count": row[0],
        "p50": row[1],
        "p95": row[2],
        "p99": row[3],
        "max": row[4],
        "window": window,
    }


def format_freshness(stats: dict) -> str:
    return (
        f"last {stats['window']}: {stats['event_count']:,} events"
        f" | p50 {stats['p50']:.1f}s"
        f" | p95 {stats['p95']:.1f}s"
        f" | p99 {stats['p99']:.1f}s"
        f" | max {stats['max']:.1f}s"
    )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Report ingest-lag freshness")
    parser.add_argument("--window", default="5 minutes", help="e.g. '5 minutes', '1 hour'")
    args = parser.parse_args()

    stats = query_freshness(args.window)
    if stats is None:
        print(f"No events in the last {args.window}.")
    else:
        print(format_freshness(stats))


if __name__ == "__main__":
    main()
