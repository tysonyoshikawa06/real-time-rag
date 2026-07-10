"""Row-level lookup over transactions — the drill-down step after an
aggregate (query_stats) or a meaning search (semantic_search).

get_transactions is the pure core of the MCP get_transactions tool: it takes
an existing psycopg connection (server.py owns opening/closing, same as
stats.py/semantic.py) and runs one of two mutually exclusive read paths:

  - ID mode: fetch exact rows for a caller-supplied list of transaction_ids
    (e.g. citing specific hits from a prior semantic_search/query_stats call).
  - Filter mode: fetch a bounded, newest-first sample of rows matching
    optional status/gateway/method filters within a recent window.

Injection safety follows the same two rules as stats.py: every caller-supplied
value rides in a SQL parameter, never interpolated, and the only thing ever
formatted into SQL here is the fixed column list below (never derived from
caller input).
"""

import psycopg
from psycopg.rows import dict_row

# Basic sanity caps only (Step 13 scope) — exhaustive validation (value
# length, character whitelisting, etc.) arrives in Step 14.
_MAX_IDS = 100
_MAX_WINDOW_MINUTES = 360  # 6h — tighter than semantic_search's 24h cap;
# this demo environment's data doesn't reliably stay queryable that far back.
_MAX_LIMIT = 100

_ROW_COLUMNS = """
    transaction_id, event_timestamp, merchant, method, amount::float8 AS amount,
    status, gateway, error_text, card_bin, ingested_at
"""

_IDS_SQL = f"""
    SELECT {_ROW_COLUMNS}
    FROM transactions
    WHERE transaction_id = ANY(%(ids)s::uuid[])
"""

_FILTER_SQL = f"""
    SELECT {_ROW_COLUMNS}
    FROM transactions
    WHERE event_timestamp >= now() - make_interval(mins => %(window_minutes)s)
      AND (%(status)s::text IS NULL OR status = %(status)s)
      AND (%(gateway)s::text IS NULL OR gateway = %(gateway)s)
      AND (%(method)s::text IS NULL OR method = %(method)s)
    ORDER BY event_timestamp DESC
    LIMIT %(limit)s
"""


def _reshape_row(row: dict) -> dict:
    return {
        "transaction_id": str(row["transaction_id"]),
        "event_timestamp": row["event_timestamp"].isoformat(),
        "merchant": row["merchant"],
        "method": row["method"],
        "amount": row["amount"],
        "status": row["status"],
        "gateway": row["gateway"],
        "error_text": row["error_text"],
        "card_bin": row["card_bin"],
        "ingested_at": row["ingested_at"].isoformat(),
    }


def get_transactions(
    conn: psycopg.Connection,
    transaction_ids: list[str] | None = None,
    window_minutes: int | None = None,
    status: str | None = None,
    gateway: str | None = None,
    method: str | None = None,
    limit: int = 10,
) -> dict:
    """Fetch full transaction rows, either by ID or by a bounded filter.

    Mode is chosen by whether transaction_ids is a non-empty list:
      - ID mode: returns exactly those rows (order not guaranteed to match
        input order). Requested IDs with no matching row are reported in
        missing_ids rather than raising.
      - Filter mode (transaction_ids is None or []): returns up to `limit`
        rows from the last `window_minutes` (default 30) matching any given
        status/gateway/method, newest first.

    The two modes are mutually exclusive in a single call — see raises below.
    """
    have_ids = bool(transaction_ids)

    # Basic sanity checks only — exhaustive bounds/caps arrive in Step 14.
    filters_given = (
        window_minutes is not None
        or status is not None
        or gateway is not None
        or method is not None
    )
    if have_ids and filters_given:
        raise ValueError(
            "pass either transaction_ids (drill-down by ID) or filter "
            "arguments (window_minutes/status/gateway/method), not both"
        )
    if have_ids and len(transaction_ids) > _MAX_IDS:
        raise ValueError(
            f"transaction_ids must have at most {_MAX_IDS} items, got {len(transaction_ids)}"
        )
    if limit <= 0 or limit > _MAX_LIMIT:
        raise ValueError(f"limit must be > 0 and <= {_MAX_LIMIT}, got {limit}")

    if not have_ids:
        effective_window = 30 if window_minutes is None else window_minutes
        if effective_window <= 0 or effective_window > _MAX_WINDOW_MINUTES:
            raise ValueError(
                f"window_minutes must be > 0 and <= {_MAX_WINDOW_MINUTES}, got {effective_window}"
            )

    cur = conn.cursor(row_factory=dict_row)

    if have_ids:
        cur.execute(_IDS_SQL, {"ids": transaction_ids})
        rows = cur.fetchall()
        found_ids = {str(row["transaction_id"]) for row in rows}
        missing_ids = [tid for tid in transaction_ids if tid not in found_ids]
        return {
            "mode": "ids",
            "transaction_ids": transaction_ids,
            "window_minutes": None,
            "status": None,
            "gateway": None,
            "method": None,
            "limit": limit,
            "count": len(rows),
            "rows": [_reshape_row(row) for row in rows],
            "missing_ids": missing_ids,
        }

    cur.execute(
        _FILTER_SQL,
        {
            "window_minutes": effective_window,
            "status": status,
            "gateway": gateway,
            "method": method,
            "limit": limit,
        },
    )
    rows = cur.fetchall()
    return {
        "mode": "filter",
        "transaction_ids": None,
        "window_minutes": effective_window,
        "status": status,
        "gateway": gateway,
        "method": method,
        "limit": limit,
        "count": len(rows),
        "rows": [_reshape_row(row) for row in rows],
        "missing_ids": [],
    }
