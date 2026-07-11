"""Semantic (vector) search over failure text for "meaning" questions.

semantic_search is the pure core of the MCP semantic_search tool: it takes an
existing psycopg connection and an injected Embedder (server.py owns opening/
closing the connection and loading the embedder once), validates/bounds its
own inputs, and delegates the actual embedding, WHERE-clause filtering, and
exact-scan-vs-HNSW decision entirely to consumer.search.search() — this module
does not reimplement any of that. It only translates window_minutes into the
interval string search() expects and reshapes search()'s rows into a plain,
JSON-serializable dict (ISO 8601 timestamps, floats instead of Decimal).
"""

from consumer.embedder import Embedder
from consumer.search import search
from mcp_server import validation

_MAX_WINDOW_MINUTES = 1440  # 24h cap
_MAX_K = 50
_MAX_QUERY_LEN = 2000


def semantic_search(
    conn,
    embedder: Embedder,
    query: str,
    window_minutes: int = 30,
    gateway: str | None = None,
    k: int = 10,
    exact_scan_threshold: int | None = None,
) -> dict:
    """Find failure-event transactions whose embedded text is closest in meaning to `query`.

    Embeds `query` via the injected embedder, then calls
    consumer.search.search() to find the k nearest failure embeddings within
    the last `window_minutes`, optionally narrowed to a single `gateway`.
    Returns a dict with the query header plus `matches`, each a plain dict
    with transaction_id, similarity, embedded_text, event_timestamp (ISO 8601
    string), gateway, method, amount (float), and status. Also carries
    `notes`: human-readable notes about any clamping/truncation applied to
    query/window_minutes/k (empty list when nothing was clamped).

    `exact_scan_threshold` is a test-only passthrough to search() so tests can
    force the "hnsw" path deterministically; when None it is not passed to
    search() at all, so search()'s own default applies.
    """
    notes: list[str] = []

    query, note = validation.check_query_text(query, _MAX_QUERY_LEN)
    if note:
        notes.append(note)

    window_minutes, note = validation.clamp_positive_int(
        "window_minutes", window_minutes, default=30, max_value=_MAX_WINDOW_MINUTES
    )
    if note:
        notes.append(note)

    k, note = validation.clamp_positive_int("k", k, default=10, max_value=_MAX_K)
    if note:
        notes.append(note)

    search_kwargs = dict(
        window=f"{window_minutes} minutes",
        k=k,
        status=None,
        gateway=gateway,
    )
    if exact_scan_threshold is not None:
        search_kwargs["exact_scan_threshold"] = exact_scan_threshold

    rows, path = search(conn, embedder, query, **search_kwargs)

    matches = [
        {
            "transaction_id": str(row["transaction_id"]),
            "similarity": round(1 - row["distance"], 4),
            "embedded_text": row["embedded_text"],
            "event_timestamp": row["event_timestamp"].isoformat(),
            "gateway": row["gateway"],
            "method": row["method"],
            "amount": row["amount"],
            "status": row["status"],
        }
        for row in rows
    ]

    return {
        "query": query,
        "window_minutes": window_minutes,
        "gateway": gateway,
        "k": k,
        "count": len(matches),
        "path": path,
        "matches": matches,
        "notes": notes,
    }
