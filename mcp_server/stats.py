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

# Friendly name -> transactions column. The whitelist for both group_by and
# filter keys; anything else is rejected before SQL is built.
_COLUMNS = {
    "method": "method",
    "status": "status",
    "gateway": "gateway",
    "merchant": "merchant",
}

_METRICS = {"count", "failure_rate"}


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
    """
    filters = dict(filters) if filters else {}

    # Basic sanity checks only — exhaustive bounds/caps (max window, max
    # limit, value length, etc.) arrive in Step 14.
    if group_by is not None and group_by not in _COLUMNS:
        raise ValueError(f"group_by must be one of {sorted(_COLUMNS)}, got {group_by!r}")
    bad_keys = sorted(set(filters) - set(_COLUMNS))
    if bad_keys:
        raise ValueError(f"filter keys must be among {sorted(_COLUMNS)}, got {bad_keys}")
    if metric not in _METRICS:
        raise ValueError(f"metric must be one of {sorted(_METRICS)}, got {metric!r}")
    if window_minutes <= 0:
        raise ValueError(f"window_minutes must be > 0, got {window_minutes}")
    if limit <= 0:
        raise ValueError(f"limit must be > 0, got {limit}")

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
        "total_events": total_events,
        "rows": rows,
    }
