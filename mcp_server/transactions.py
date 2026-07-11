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

from mcp_server import validation

_MAX_IDS = 100  # reject (never clamp) over this — dropping requested IDs
# would silently break grounding for whatever cited them.
_MAX_WINDOW_MINUTES = 1440  # 24h — matches data retention; lower this if the
# environment resets more often.
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
    Both modes' return dicts carry a `notes` list of human-readable notes
    about any clamping applied (empty when nothing was clamped).
    """
    have_ids = bool(transaction_ids)
    notes: list[str] = []

    filters_given = (
        window_minutes is not None
        or status is not None
        or gateway is not None
        or method is not None
    )
    if have_ids and filters_given:
        raise ValueError(
            "get_transactions accepts either transaction_ids OR filter "
            "params (window_minutes/status/gateway/method), not both. Pass "
            "IDs to look up specific rows, or filters to search."
        )

    # limit is validated/clamped the same way in both modes.
    limit, note = validation.clamp_positive_int(
        "limit", limit, default=10, max_value=_MAX_LIMIT
    )
    if note:
        notes.append(note)

    if have_ids:
        if len(transaction_ids) > _MAX_IDS:
            # Reject, never clamp: silently dropping requested IDs would
            # break grounding for whatever cited them.
            raise ValueError(
                f"transaction_ids exceeds the cap of {_MAX_IDS} items "
                f"(got {len(transaction_ids)}); rejected rather than "
                f"truncated because dropping requested IDs would break "
                f"grounding — pass at most {_MAX_IDS} IDs per call."
            )
        invalid_ids = validation.find_invalid_uuids(transaction_ids)
        if invalid_ids:
            raise ValueError(
                f"transaction_ids must be valid UUIDs; malformed entries: {invalid_ids}"
            )

        # All ID-mode validation is pure and done above — only touch the
        # connection once we know the input is acceptable.
        cur = conn.cursor(row_factory=dict_row)
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
            "notes": notes,
        }

    if status is not None:
        validation.check_enum("status", status, validation.ALLOWED_STATUS)
    if method is not None:
        validation.check_enum("method", method, validation.ALLOWED_METHOD)

    window_minutes, note = validation.clamp_positive_int(
        "window_minutes", window_minutes, default=30, max_value=_MAX_WINDOW_MINUTES
    )
    if note:
        notes.append(note)

    # All filter-mode validation is pure and done above — only touch the
    # connection once we know the input is acceptable.
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        _FILTER_SQL,
        {
            "window_minutes": window_minutes,
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
        "window_minutes": window_minutes,
        "status": status,
        "gateway": gateway,
        "method": method,
        "limit": limit,
        "count": len(rows),
        "rows": [_reshape_row(row) for row in rows],
        "missing_ids": [],
        "notes": notes,
    }
