# Spec: 11B — MCP server skeleton + `query_stats` tool

## Feature

A FastMCP server in `mcp_server/` exposing one tool, `query_stats`: parameterized
SQL aggregation over the `transactions` table for "counting" questions, with a
whitelisted, injection-safe query surface.

## Context

- New package `mcp_server/` at the repo root (`__init__.py`, `stats.py`, `server.py`).
  - `mcp_server/stats.py` — the pure query function `query_stats(conn, ...) -> dict`.
    All logic and validation lives here so it is testable without MCP transport.
  - `mcp_server/server.py` — the FastMCP app. Creates `mcp = FastMCP("streaming-rag")`,
    registers the `query_stats` tool (a thin wrapper that opens a DB connection via
    `consumer.db.connect()`, calls `stats.query_stats`, closes the connection), and
    runs with stdio transport under `if __name__ == "__main__":` via `mcp.run()`.
- Reuse `consumer/db.py::connect()` and `consumer/config.py` for the DB connection —
  do not invent a new connection pattern.
- Add dependency `fastmcp>=2` to `pyproject.toml` via `uv add fastmcp`.
- Add a Makefile target `mcp-dev:` → `uv run fastmcp dev mcp_server/server.py:mcp`
  (MCP inspector) and `mcp:` → `uv run python -m mcp_server.server` (stdio server).
- Queries hit the `transactions` table only (columns: `transaction_id`,
  `event_timestamp`, `merchant`, `method`, `amount`, `status`, `gateway`,
  `error_text`, `card_bin`). `status` is `'success' | 'failure'`.

## Inputs / Outputs

`query_stats(conn, window_minutes=30, group_by=None, filters=None, metric="count", limit=10) -> dict`

The MCP tool exposes the same parameters minus `conn`.

Parameters:

- `window_minutes: int = 30` — look back this many minutes on `event_timestamp`
  (i.e. `event_timestamp >= now() - make_interval(mins => window_minutes)`).
- `group_by: str | None = None` — dimension to aggregate. Allowed values (friendly
  name → column): `method`, `status`, `gateway`, `merchant`. If `None`, return one
  overall row.
- `filters: dict[str, str] | None = None` — equality filters. Allowed keys: the same
  four (`method`, `status`, `gateway`, `merchant`). Values are matched exactly and
  MUST be passed as SQL parameters, never interpolated.
- `metric: str = "count"` — `"count"` or `"failure_rate"`.
  `failure_rate` per group = `avg((status = 'failure')::int)`, a float in [0, 1],
  rounded to 4 decimal places in the returned rows.
- `limit: int = 10` — top-N groups by the metric (SQL `LIMIT`, parameterized).

Return shape (plain dict, JSON-serializable):

```python
{
    "window_minutes": 30,
    "metric": "failure_rate",
    "group_by": "gateway",          # or None
    "filters": {"method": "card"},  # {} when none given
    "total_events": 12345,           # all rows in window matching filters (no limit)
    "rows": [
        # group_by given: one row per group, ordered by the metric DESC, max `limit`
        {"group": "stripe-proxy", "count": 812, "failure_rate": 0.31},
        ...
    ],
}
```

Row shape rules:

- Every row always includes `"count"`.
- Rows include `"failure_rate"` only when `metric == "failure_rate"`.
- Rows include `"group"` only when `group_by` is given.
- `group_by=None` → `rows` is a single row list, e.g. `[{"count": 4021}]`
  (plus `failure_rate` when that metric is requested). This single row is returned
  even when the count is 0.
- Ordering: `metric == "count"` → `ORDER BY count DESC`; `metric == "failure_rate"`
  → `ORDER BY failure_rate DESC`. Tie order among equal metric values is unspecified.
- When `metric == "failure_rate"` and the (ungrouped) window matches zero rows,
  `failure_rate` is `None` — a rate over zero events is undefined, and `0.0`
  would falsely report health.

## Behavior

1. Given rows in the window and no `group_by`/`filters`, `query_stats` returns
   `total_events` = the number of transactions with `event_timestamp` in the last
   `window_minutes`, and `rows == [{"count": total_events}]`.
2. Given `group_by="gateway"`, it returns one row per distinct gateway in the
   filtered window with that gateway's count, ordered by count descending,
   truncated to `limit`.
3. Given `metric="failure_rate"` and `group_by="gateway"`, each row contains that
   gateway's `failure_rate` (failures / total for the group, 4-dp float) and its
   `count`, ordered by failure_rate descending.
4. Given `filters={"method": "card"}`, only card transactions are counted — both
   in `rows` and in `total_events`.
5. Multiple filters combine with AND (e.g. `{"method": "card", "status": "failure"}`).
6. Rows with `event_timestamp` older than the window are excluded from all numbers.
7. A filter value that matches nothing yields `total_events == 0` and
   `rows == []` (grouped) or `[{"count": 0}]` (ungrouped) — no error.
8. `query_stats` takes an existing psycopg connection as its first argument and
   issues only SELECTs — tests may call it inside an uncommitted transaction
   containing synthetic rows and roll back afterwards.
9. The FastMCP server object `mcp` exists in `mcp_server/server.py`, is named
   `"streaming-rag"`, and has a registered tool named `query_stats` whose
   parameters mirror the function (minus `conn`). Calling the tool end-to-end
   (e.g. via `fastmcp.Client(mcp)` in-memory) returns the dict shape above.

## Edge cases & errors

- `group_by` not in the whitelist (e.g. `"amount"`, `"transaction_id; DROP"`) →
  raises `ValueError` naming the allowed values. Never reaches SQL.
- Any `filters` key not in the whitelist → `ValueError` naming the allowed keys.
- `metric` not in `{"count", "failure_rate"}` → `ValueError`.
- `window_minutes <= 0` or `limit <= 0` → `ValueError`. (Basic sanity only —
  exhaustive bounds/caps are Step 14; note this in a comment.)
- Filter **values** are never validated against a whitelist (gateways/merchants are
  open-ended) but are always parameterized: a value like
  `"x' OR '1'='1"` must simply match zero rows, not error or inject.
- Identifier interpolation: the only strings ever formatted into SQL are column
  names looked up from the module's own whitelist dict — never caller input.

## Out of scope

- `semantic_search` MCP tool (Step 12), `get_transactions` / `system_freshness`
  (Step 13), exhaustive input validation and result caps (Step 14), the agent
  (Step 15).
- Auth, HTTP transport, deployment concerns.
- Any writes to the database.
- Time-bucketing / histograms — a single window per call only.

## Acceptance criteria

- [ ] `uv run python -c "from mcp_server.stats import query_stats"` works;
      `fastmcp` is in `pyproject.toml` dependencies.
- [ ] Behaviors 1–8 each covered by at least one test in
      `tests/test_mcp_query_stats.py` (single test file for this feature).
- [ ] Every listed error case raises `ValueError` before any SQL executes.
- [ ] Injection probe test passes (parameterized values, whitelisted identifiers).
- [ ] Tool `query_stats` is callable through the FastMCP in-memory client and
      returns the documented shape (behavior 9).
- [ ] `make mcp-dev` starts the MCP inspector against the server (manual check).
