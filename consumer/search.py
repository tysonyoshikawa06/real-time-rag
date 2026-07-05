"""Hybrid search: vector similarity over embeddings + structured SQL filters.

Prototype for Step 12's semantic_search MCP tool — kept reusable (embed_and_search
takes plain arguments, returns plain dicts) so that tool can lift it directly.

The crux this proves: time/status/gateway filters must live in the SQL WHERE
clause, not applied after fetching the top-k nearest vectors. If you instead
took the k nearest vectors first and filtered afterward, the k nearest might
all fall outside the window — leaving zero results even though in-window
matches exist further down the ranking. Filtering inside the query means the
top-k is drawn only from rows that already pass the filter, so every result
is both semantically relevant and in-window.

Run with: make search-demo "<query>"
"""

import numpy as np

from consumer.db import connect
from consumer.embedder import Embedder, LocalEmbedder

_SEARCH_SQL = """
    SELECT
        t.transaction_id,
        e.embedded_text,
        t.event_timestamp,
        e.embedding <=> %(query_vec)s AS distance
    FROM embeddings e
    JOIN transactions t ON t.transaction_id = e.transaction_id
    WHERE t.event_timestamp >= now() - %(window)s::interval
      AND (%(status)s::text IS NULL OR t.status = %(status)s)
      AND (%(gateway)s::text IS NULL OR t.gateway = %(gateway)s)
    ORDER BY e.embedding <=> %(query_vec)s
    LIMIT %(k)s
"""


def search(
    conn,
    embedder: Embedder,
    query: str,
    window: str = "1 hour",
    k: int = 5,
    status: str | None = None,
    gateway: str | None = None,
) -> list[dict]:
    """Embed `query` and return the k nearest failure embeddings within `window`.

    The status/gateway filters are optional (pass None to skip). Each result
    has transaction_id, embedded_text, distance (cosine distance, 0 = identical
    direction), and event_timestamp.
    """
    query_vec = np.array(embedder.embed([query])[0])

    cur = conn.cursor()
    cur.execute(
        _SEARCH_SQL,
        {
            "query_vec": query_vec,
            "window": window,
            "status": status,
            "gateway": gateway,
            "k": k,
        },
    )
    return cur.fetchall()


def _format_result(rank: int, row: dict) -> str:
    ts = str(row["event_timestamp"])[:19]
    return (
        f"  [{rank}] distance={row['distance']:.3f}  {ts}  "
        f"{row['transaction_id']}\n"
        f"       {row['embedded_text']}"
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Hybrid semantic + structured search demo")
    parser.add_argument("query", help="Natural-language query, e.g. 'connection timed out'")
    parser.add_argument("--window", default="1 hour", help="e.g. '1 hour', '10 minutes'")
    parser.add_argument("--k", type=int, default=5, help="Number of results (default: 5)")
    parser.add_argument("--status", default=None, help="Filter to a status, e.g. 'failure'")
    parser.add_argument("--gateway", default=None, help="Filter to a gateway")
    args = parser.parse_args()

    print("Loading all-MiniLM-L6-v2 (downloads ~80 MB on first run, then cached)...")
    embedder = LocalEmbedder()

    conn = connect()
    try:
        rows = search(
            conn,
            embedder,
            args.query,
            window=args.window,
            k=args.k,
            status=args.status,
            gateway=args.gateway,
        )
    finally:
        conn.close()

    print(f'\nQuery: "{args.query}"  (window={args.window}, k={args.k})\n')
    if not rows:
        print("  No matches in window.")
    else:
        for i, row in enumerate(rows, start=1):
            print(_format_result(i, row))


if __name__ == "__main__":
    main()
