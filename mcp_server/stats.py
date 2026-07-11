"""Parameterized SQL aggregation over transactions for "counting" questions.

query_stats answers questions like "how many card failures in the last 30
minutes, broken down by gateway?" with a single windowed SELECT. It is the
pure core of the MCP query_stats tool: it takes an existing psycopg connection
(server.py owns opening/closing), issues only SELECTs, and returns a plain
JSON-serializable dict — so tests can call it inside an uncommitted
transaction of synthetic rows and roll back afterwards.

Injection safety rests on two rules:
  1. Every caller-supplied VALUE (filter values, window, limit) is passed as
     a SQL parameter, never interpolated.
  2. The only strings ever formatted into SQL are column names looked up from
     _COLUMNS below — caller input selects a whitelist key, it never becomes
     an identifier itself.
"""

import psycopg
from psycopg.rows import dict_row

from mcp_server import validation

# Friendly name -> transactions column. Keys are validated against
# validation.ALLOWED_COLUMNS before being used to build SQL — only
# whitelisted keys ever become identifiers here, caller values never do.
_COLUMNS = {
    "method": "method",
    "status": "status",
    "gateway": "gateway",
    "merchant": "merchant",
}

_MAX_WINDOW_MINUTES = 1440  # 24h — aggregation can reasonably span up to a
# day of retained data.
_MAX_LIMIT = 100


def query_stats(
    conn: psycopg.Connection,
    window_minutes: int = 30,
    group_by: str | None = None,
    filters: dict[str, str] | None = None,
    metric: str = "count",
    limit: int = 10,
) -> dict:
    """Count (or failure-rate) transactions in the last window_minutes.

    Returns a dict echoing the query parameters plus:
      - total_events: all rows in the window matching the filters (no limit).
      - rows: one row per group (ordered by the metric DESC, at most `limit`),
        or a single overall row when group_by is None. Every row has "count";
        "failure_rate" appears only when that metric is requested; "group"
        appears only when group_by is given.
      - notes: human-readable notes about any clamping applied to
        window_minutes/limit (empty list when nothing was clamped).
    """
    filters = dict(filters) if filters else {}
    notes: list[str] = []

    if group_by is not None:
        validation.check_allowed_keys("group_by", [group_by], validation.ALLOWED_COLUMNS)
    validation.check_allowed_keys("filter", filters.keys(), validation.ALLOWED_COLUMNS)
    if "status" in filters:
        validation.check_enum("status", filters["status"], validation.ALLOWED_STATUS)
    if "method" in filters:
        validation.check_enum("method", filters["method"], validation.ALLOWED_METHOD)
    validation.check_enum("metric", metric, validation.ALLOWED_METRIC)

    window_minutes, note = validation.clamp_positive_int(
        "window_minutes", window_minutes, default=30, max_value=_MAX_WINDOW_MINUTES
    )
    if note:
        notes.append(note)
    limit, note = validation.clamp_positive_int(
        "limit", limit, default=10, max_value=_MAX_LIMIT
    )
    if note:
        notes.append(note)

    # WHERE clause shared by both queries. Column names come from _COLUMNS
    # (never caller input); values ride in params.
    where_parts = ["event_timestamp >= now() - make_interval(mins => %(window_minutes)s)"]
    params: dict = {"window_minutes": window_minutes}
    for key, value in filters.items():
        where_parts.append(f"{_COLUMNS[key]} = %(filter_{key})s")
        params[f"filter_{key}"] = value
    where_sql = " AND ".join(where_parts)

    select_parts = ['count(*) AS "count"']
    if metric == "failure_rate":
        # avg over 0/1 ints = failures / total; ::float8 so psycopg returns a
        # plain float (round(numeric) would come back as Decimal).
        select_parts.append(
            "round(avg((status = 'failure')::int), 4)::float8 AS failure_rate"
        )

    # dict_row at cursor level so any psycopg connection works, not just ones
    # opened via consumer.db.connect().
    cur = conn.cursor(row_factory=dict_row)

    cur.execute(f"SELECT count(*) AS total FROM transactions WHERE {where_sql}", params)
    total_events = cur.fetchone()["total"]

    if group_by is None:
        rows_sql = f"SELECT {', '.join(select_parts)} FROM transactions WHERE {where_sql}"
    else:
        group_col = _COLUMNS[group_by]
        select_parts.insert(0, f'{group_col} AS "group"')
        rows_sql = (
            f"SELECT {', '.join(select_parts)} FROM transactions "
            f"WHERE {where_sql} "
            f'GROUP BY {group_col} ORDER BY "{metric}" DESC LIMIT %(limit)s'
        )
        params["limit"] = limit
    cur.execute(rows_sql, params)
    rows = [dict(row) for row in cur.fetchall()]

    return {
        "window_minutes": window_minutes,
        "metric": metric,
        "group_by": group_by,
        "filters": filters,
        "limit": limit,
        "total_events": total_events,
        "rows": rows,
        "notes": notes,
    }
