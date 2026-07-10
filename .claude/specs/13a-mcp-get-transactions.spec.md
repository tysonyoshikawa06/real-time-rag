# Spec: 13a — `get_transactions` MCP tool

## Feature

A `get_transactions` tool on the FastMCP server that fetches complete
transaction rows — either by an explicit list of IDs (citation drill-down
from a prior `semantic_search`/`query_stats` result) or by a bounded filter
(a sample of example rows behind an aggregate).

## Context

- New module `mcp_server/transactions.py` — pure function `get_transactions(conn, ...)`,
  same split as `stats.py`/`semantic.py`: pure logic testable without MCP
  transport, thin tool wrapper registered in `mcp_server/server.py` (`graphify explain mcp_server`
  shows `server.py` already contains `query_stats()`/`semantic_search()` in this
  shape — follow it).
- Uses `consumer.db.connect()` for the connection (`consumer_db_py::connect()`,
  `graphify path` shows it's already imported by `server.py`) — same pattern as
  the other two tools. No new Makefile targets.
- Table shape is `infra/init.sql`'s `transactions` table: `transaction_id uuid`,
  `event_timestamp timestamptz`, `merchant text`, `method text`, `amount
  numeric(12,2)`, `status text`, `gateway text`, `error_text text` (nullable),
  `card_bin text` (nullable), `ingested_at timestamptz`.

## Inputs / Outputs

Pure function:

```python
def get_transactions(
    conn,
    transaction_ids: list[str] | None = None,
    window_minutes: int | None = None,
    status: str | None = None,
    gateway: str | None = None,
    method: str | None = None,
    limit: int = 10,
) -> dict
```

Mode is chosen by whether `transaction_ids` is a non-empty list:

- **ID mode**: `transaction_ids` non-empty. Fetches exactly those rows via
  `WHERE transaction_id = ANY(%(ids)s::uuid[])` (parameterized; the `::uuid[]`
  cast is the safety-relevant part, not a caller-controlled identifier).
  `window_minutes`/`status`/`gateway`/`method` must all be `None` in this mode
  — see Edge cases.
- **Filter mode**: `transaction_ids` is `None` or `[]`. `window_minutes`
  defaults to 30 when not given; `status`/`gateway`/`method` are optional
  equality filters, each passed as a parameter (not interpolated) using the
  same `(%(x)s::text IS NULL OR col = %(x)s)` optional-filter pattern
  `consumer/search.py::search()` already uses. Rows ordered by
  `event_timestamp DESC`, capped at `limit`.

Return shape (plain dict, JSON-serializable — no `Decimal`/`datetime`/`UUID`
objects; reuse the same reshaping approach `semantic.py` uses for its rows):

```python
{
    "mode": "ids",                 # or "filter"
    "transaction_ids": [...],      # echoed, or None in filter mode
    "window_minutes": None,        # echoed (filter mode) or None (ids mode)
    "status": None,
    "gateway": None,
    "method": None,
    "limit": 10,
    "count": 2,                    # len(rows)
    "rows": [
        {
            "transaction_id": "3f2e...-uuid",
            "event_timestamp": "2026-07-09T21:43:44+00:00",
            "merchant": "Acme Co",
            "method": "card",
            "amount": 46.67,
            "status": "failure",
            "gateway": "stripe-proxy",
            "error_text": "connection timed out",
            "card_bin": "411111",
            "ingested_at": "2026-07-09T21:43:44+00:00",
        },
        ...
    ],
    "missing_ids": [],   # ids-mode only: requested IDs with no matching row; [] in filter mode
}
```

## Behavior

1. Given a non-empty `transaction_ids`, returns exactly the rows that exist
   for those IDs (order not guaranteed to match input order — DB order).
2. If some requested IDs have no matching row, those IDs are listed in
   `missing_ids` and the rest are returned normally — this is not an error.
3. Given no `transaction_ids` (`None` or `[]`), returns up to `limit` rows
   from the last `window_minutes` (default 30) matching any given
   `status`/`gateway`/`method`, newest first (`event_timestamp DESC`).
4. Every returned row has all ten columns: `transaction_id`,
   `event_timestamp`, `merchant`, `method`, `amount`, `status`, `gateway`,
   `error_text`, `card_bin`, `ingested_at` — `error_text`/`card_bin` may be
   `None`. `amount` is a plain float (cast `::float8` in SQL, same reason as
   `stats.py`'s `failure_rate` cast — `NUMERIC` comes back as `Decimal`
   otherwise). `event_timestamp`/`ingested_at` are ISO 8601 strings.
   `transaction_id` is a plain string.
5. `count == len(rows)` always.
6. Filter mode with no matches returns `rows == []`, `count == 0` — not an
   error.

## Edge cases & errors

- `transaction_ids` given **and** any of `window_minutes`/`status`/`gateway`/`method`
  is not `None` → `ValueError` telling the caller to pick one mode (drill-down
  by ID, or filter — not both in the same call).
- `len(transaction_ids) > 100` → `ValueError` naming the cap.
- `window_minutes` (filter mode) `<= 0` or `> 360` → `ValueError` naming the
  bound (360 = 6h; kept lower than `semantic_search`'s 24h cap since this demo
  environment's data doesn't reliably stay queryable that far back).
- `limit <= 0` or `limit > 100` → `ValueError` naming the cap.
- `status`/`method` are **not** whitelist-validated against the table's CHECK
  values (`status IN ('success','failure')`, `method IN ('card','ach','wallet')`)
  — an unmatched value just yields zero rows, same as `query_stats`'s filter
  values. `gateway` is free text, same reasoning as the other two tools.
- All checks raise before any SQL executes.
- Basic sanity caps only (Step 13 scope) — comprehensive input validation is
  Step 14; note this in a comment matching the existing comment style in
  `mcp_server/stats.py`/`mcp_server/semantic.py`.

## Out of scope

- `system_freshness` (separate spec, 13b).
- Comprehensive input validation beyond the caps above (Step 14).
- The agent (Step 15).
- Any change to `consumer/db.py`, `consumer/search.py`, or `mcp_server/stats.py`/`semantic.py`.
- Preserving input order of `transaction_ids` in the returned `rows` (DB order
  is fine).

## Tool docstring requirement

The tool's docstring must, in the coder's own words, make clear:

- This fetches **full individual rows** — by ID, to cite/show specific
  transactions behind a search result or count; or by filter, to pull
  representative example rows (e.g. "example failures from stripe-proxy in
  the last 10 minutes").
- Returned rows include **complete details**, including raw `error_text` and
  `card_bin` — everything needed to cite a transaction concretely.
- Distinguish from `query_stats` (aggregates, no individual rows) and
  `semantic_search` (meaning search over embedded failure text) — this tool
  is the drill-down step after either of those, not a replacement for them.

## Acceptance criteria

- [ ] `uv run python -c "from mcp_server.transactions import get_transactions"` works.
- [ ] Behaviors 1–6 each covered by at least one test in
      `tests/test_mcp_get_transactions.py` (one test file for this feature;
      rollback-wrapped synthetic transactions, following the 11B/12 pattern).
- [ ] Every listed error case raises `ValueError` before any SQL runs.
- [ ] Tool `get_transactions` is callable through the FastMCP in-memory client
      and returns the documented shape.
- [ ] Manual/live check (not a pytest case): from the MCP inspector, call
      `get_transactions` with IDs taken from a prior `semantic_search` result
      → full rows come back; spot-check one row against pgweb. Then call it
      with a filter (e.g. `status="failure"`, a real `gateway`, `limit=5`) →
      a sample of matching rows comes back.
