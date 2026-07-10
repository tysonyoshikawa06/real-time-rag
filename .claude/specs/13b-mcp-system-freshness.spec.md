# Spec: 13b — `system_freshness` MCP tool

## Feature

A `system_freshness` tool on the FastMCP server that reports ingest-lag
percentiles over a recent window, so the agent can state or caveat how
current the data is.

## Context

- New module `mcp_server/freshness.py` — pure function `system_freshness(window_minutes=5)`,
  same split as `stats.py`/`semantic.py`.
- **Must reuse, not reimplement**: `consumer/freshness.py::query_freshness(window: str)`
  (`graphify explain freshness` — contains `query_freshness()`, `format_freshness()`,
  `main()`) already computes `event_count`/`p50`/`p95`/`p99`/`max` via
  `percentile_cont` SQL. This is the Step 8 module; the percentile math lives
  there and only there.
- Unlike `stats.py`/`semantic.py`, `query_freshness()` opens and closes **its
  own** connection internally (`psycopg.connect(POSTGRES_DSN)`) rather than
  accepting one — it is already fully self-contained. So `mcp_server/freshness.py`
  does **not** call `consumer.db.connect()` itself; it just calls
  `query_freshness(window=...)` directly and reshapes the result. The MCP tool
  wrapper in `server.py` therefore does not open/close a connection either,
  unlike the other two tools — note this deliberate asymmetry in a short
  comment so it doesn't look like an oversight.
- `query_freshness()` takes `window` as an interval **string** (e.g.
  `"5 minutes"`), not minutes as an int — translate `window_minutes` the same
  way `semantic.py` translates its own window (`f"{window_minutes} minutes"`).
- `query_freshness()` returns `None` when no events fall in the window — must
  be handled, not treated as an error.

## Inputs / Outputs

Pure function:

```python
def system_freshness(window_minutes: int = 5) -> dict
```

MCP tool (registered in `server.py`): same one parameter,
`window_minutes: int = 5`, calls `freshness.system_freshness(window_minutes)`,
returns the dict below directly (no connection to open/close here).

Return shape when events exist in the window:

```python
{
    "window_minutes": 5,
    "event_count": 1234,
    "p50_seconds": 0.4,
    "p95_seconds": 1.2,
    "p99_seconds": 2.1,
    "max_seconds": 3.6,
    "human_readable": "Data is current as of ~0.4s (p50) over the last 5 minutes (1,234 events).",
}
```

Return shape when no events fall in the window:

```python
{
    "window_minutes": 5,
    "event_count": 0,
    "p50_seconds": None,
    "p95_seconds": None,
    "p99_seconds": None,
    "max_seconds": None,
    "human_readable": "No events in the last 5 minutes — freshness cannot be computed.",
}
```

All percentile values are plain floats rounded to 1 decimal place (seconds).

## Behavior

1. Given default parameters, calls `query_freshness(window="5 minutes")` and
   returns the percentiles/count/human-readable line above.
2. `window_minutes` is translated to `f"{window_minutes} minutes"` and passed
   straight to `query_freshness()` — no percentile computation happens in
   `mcp_server/freshness.py` itself.
3. When `query_freshness()` returns a stats dict, `p50_seconds`/`p95_seconds`/
   `p99_seconds`/`max_seconds` are that dict's `p50`/`p95`/`p99`/`max`, each
   `round(x, 1)`; `event_count` is passed through unchanged.
4. When `query_freshness()` returns `None`, `event_count` is `0`, all four
   percentile fields are `None`, and `human_readable` states plainly that
   there's no data in the window — this is not an error.
5. `human_readable` is a short, quotable sentence stating the p50 lag (or the
   no-data message) — this is a presentation string only; it does not
   recompute anything `query_freshness()` already computed.
6. Numbers returned must agree with `make freshness` run over the same window
   (both call the same underlying `query_freshness()`).

## Edge cases & errors

- `window_minutes <= 0` or `window_minutes > 60` → `ValueError` naming the
  bound (kept tight — freshness is meant to describe *recent* ingest lag, not
  a long historical window).
- No events in the window → handled per behavior 4, not an error.
- Basic sanity caps only (Step 13 scope) — comprehensive input validation is
  Step 14; note this in a comment matching the existing comment style in
  `mcp_server/stats.py`/`semantic.py`.

## Out of scope

- `get_transactions` (separate spec, 13a).
- Any change to `consumer/freshness.py` (its SQL, `query_freshness()`,
  `format_freshness()`, or `main()` are reused as-is, untouched).
- Comprehensive input validation beyond the bound above (Step 14).
- The agent (Step 15).

## Tool docstring requirement

The tool's docstring must, in the coder's own words, make clear:

- Use this to report data freshness/recency, or to caveat how up-to-date an
  answer is (e.g. when the user asks "is this current?").
- It measures **ingest lag** — the delay between an event happening
  (`event_timestamp`) and becoming queryable (`ingested_at`) — not query
  latency or system uptime.

## Acceptance criteria

- [ ] `uv run python -c "from mcp_server.freshness import system_freshness"` works.
- [ ] Behaviors 1–6 each covered by at least one test in
      `tests/test_mcp_system_freshness.py` (one test file for this feature;
      rollback-wrapped synthetic transactions with known `event_timestamp`/
      `ingested_at` gaps so expected percentiles are predictable).
- [ ] The `window_minutes` bound raises `ValueError` before `query_freshness()`
      is called.
- [ ] The no-events-in-window case is covered and returns the documented
      all-`None` shape, not an exception.
- [ ] Tool `system_freshness` is callable through the FastMCP in-memory
      client and returns the documented shape.
- [ ] Manual/live check (not a pytest case): from the MCP inspector,
      `system_freshness` returns sub-second-to-low-seconds percentiles and a
      sensible `human_readable` line; numbers agree with `make freshness` run
      over the same window.
