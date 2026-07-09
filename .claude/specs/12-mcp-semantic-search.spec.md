# Spec: 12 — `semantic_search` MCP tool

## Feature

A `semantic_search` tool on the FastMCP server that wraps the existing,
exact-scan-hardened `consumer/search.py::search()` so the agent can search
failure text by meaning and get back individually-cited transactions.

## Context

- New module `mcp_server/semantic.py` — the pure function `semantic_search(...)`
  (same split as 11B: pure logic testable without MCP transport, thin tool
  wrapper in `mcp_server/server.py`).
- `mcp_server/server.py` gains one new tool, `semantic_search`, registered
  alongside the existing `query_stats`. No new Makefile targets needed —
  `make mcp` / `make mcp-dev` already expose whatever tools are on `mcp`.
- **Do not reimplement search logic.** `mcp_server/semantic.py` must import and
  call `consumer.search.search()` for the embedding step, the WHERE-clause
  filtering, and the exact-scan/HNSW decision. This module only: validates/
  bounds its own inputs, converts `window_minutes` → the interval string
  `search()` expects (e.g. `f"{window_minutes} minutes"`), and reshapes
  `search()`'s output into the documented return shape.
- **One additive, in-scope edit to `consumer/search.py`:** `_SEARCH_SQL`
  currently selects only `transaction_id, embedded_text, event_timestamp,
  distance`. Extend the `SELECT` list to also return `t.gateway, t.method,
  t.status`, and `t.amount::float8 AS amount` (the `::float8` cast matters —
  `amount` is `NUMERIC(12,2)`, which psycopg returns as `Decimal`, not
  JSON-serializable; casting in SQL is the same pattern `stats.py` already
  uses for `failure_rate`). This is the only change to `search.py`: the
  WHERE-clause construction, the `_count_candidates` / exact-scan-threshold
  decision, and the embedding call are untouched and reused as-is. The
  existing CLI (`make search-demo`, `_format_result`) only reads specific
  keys out of each row dict, so adding columns is backward compatible.
- The `Embedder` used for the query text is dependency-injected into
  `semantic_search(conn, embedder, ...)` — mirroring how `search()` itself
  takes `embedder` as a parameter. `mcp_server/server.py` is the only place
  that hardcodes a concrete embedder: load `LocalEmbedder()` **once**, at
  module import time (a global), and reuse it across calls — never
  reconstruct it per request (loading the model is the expensive part;
  `consumer/main.py` and `consumer/search.py::main()` already follow this
  load-once pattern).
- Tests should not load the real ~80MB sentence-transformer model per run.
  `consumer/embedder.py::Embedder` is an ABC (`embed(self, texts: list[str])
  -> list[list[float]]`) — expect the test-writer to implement a small
  deterministic fake conforming to that interface, not to load
  `LocalEmbedder`. `mcp_server/semantic.py` must accept `embedder` as a
  parameter for exactly this reason.

## Inputs / Outputs

Pure function:

```python
def semantic_search(
    conn,
    embedder: Embedder,
    query: str,
    window_minutes: int = 30,
    gateway: str | None = None,
    k: int = 10,
    exact_scan_threshold: int | None = None,
) -> dict
```

- `exact_scan_threshold` is a **test-only passthrough** to `search()` (so
  tests can force the "hnsw" path deterministically without seeding 50,000+
  rows). When `None`, it is simply not passed to `search()` and `search()`'s
  own default (`EXACT_SCAN_THRESHOLD = 50_000`) applies. **It is not one of
  the four parameters exposed on the MCP tool** — the tool wrapper in
  `server.py` only exposes `query`, `window_minutes`, `gateway`, `k` (plus the
  server's own injected `conn`/`embedder`).

MCP tool (registered in `server.py`): same four caller-facing parameters —
`query: str`, `window_minutes: int = 30`, `gateway: str | None = None`,
`k: int = 10` — opens a connection via `consumer.db.connect()`, calls
`semantic.semantic_search(conn, _embedder, query, window_minutes, gateway, k)`,
closes the connection, returns the dict below.

Return shape (plain dict, JSON-serializable — every value already a JSON-
native type, no `Decimal`/`datetime` objects):

```python
{
    "query": "connection timed out",
    "window_minutes": 30,
    "gateway": None,          # echoed as given
    "k": 10,                  # requested k, after validation
    "count": 3,               # actual number of matches, == len(matches), <= k
    "path": "exact",          # or "hnsw" -- exactly what search() reported
    "matches": [
        {
            "transaction_id": "3f2e...-uuid",
            "similarity": 0.834,        # round(1 - distance, 4); higher = more similar
            "embedded_text": "card payment via stripe-proxy failed: ...",
            "event_timestamp": "2026-07-09T21:43:44+00:00",  # .isoformat() string
            "gateway": "stripe-proxy",
            "method": "card",
            "amount": 46.67,            # plain float
            "status": "failure",
        },
        ...
    ],
}
```

## Behavior

1. Given `query` and default parameters, `semantic_search` embeds the query
   via the injected `embedder`, calls `consumer.search.search()` with
   `window=f"{window_minutes} minutes"`, `k=k`, `gateway=gateway`, and returns
   the header + `matches` shape above.
2. Each match includes all of: `transaction_id`, `similarity`, `embedded_text`,
   `event_timestamp` (ISO 8601 string, not a datetime object), `gateway`,
   `method`, `amount` (plain float, not Decimal), `status`.
3. `similarity = round(1 - distance, 4)` where `distance` is the cosine
   distance `search()` returned for that row; higher similarity = closer
   match.
4. The header's `path` field is exactly the `path` string `search()` returned
   (`"exact"` or `"hnsw"`) — this module makes no scan-strategy decisions of
   its own.
5. `matches` preserves `search()`'s ordering (ascending distance == descending
   similarity) — nearest match first.
6. Given `gateway=<value>`, only matches from that gateway are returned. This
   is entirely `search()`'s existing gateway filter — `semantic_search` adds
   no filtering logic of its own.
7. Given a query/window with no matching embeddings, `matches == []` and
   `count == 0` — not an error. `path` is still reported.
8. `count == len(matches)`, always `<= k`.
9. The MCP tool `semantic_search` is registered on the same `mcp` server
   object as `query_stats`, exposes exactly `query`, `window_minutes`,
   `gateway`, `k` as its parameters, and is callable end-to-end through the
   FastMCP in-memory client (`fastmcp.Client(mcp)`), returning the documented
   shape.

## Edge cases & errors

- `query` empty or whitespace-only → `ValueError`, raised before embedding or
  calling `search()`.
- `window_minutes <= 0` or `window_minutes > 1440` → `ValueError` naming the
  bound (1440 = 24h cap).
- `k <= 0` or `k > 50` → `ValueError` naming the bound.
- These are basic sanity caps only (per Step 12's scope) — comprehensive
  input validation is Step 14; note this in a comment, matching the existing
  comment style in `mcp_server/stats.py`.
- `gateway` is **not** whitelist-validated (gateway names are open-ended, same
  reasoning as `query_stats`'s filter values) — it is passed straight through
  as a parameter into `search()`'s already-parameterized SQL. No new
  injection surface is introduced.
- All bound checks raise before `search()` (and therefore before any SQL or
  embedding call) executes — same "validate before touching resources"
  discipline as `mcp_server/stats.py`.

## Out of scope

- `get_transactions` / `system_freshness` tools (Step 13).
- Comprehensive input validation beyond the caps above (Step 14).
- The agent (Step 15).
- Any change to the exact-scan/HNSW decision heuristic, the `COUNT(*)`
  candidate-sizing logic, or the embedding pipeline itself — `search()` is
  reused unmodified except for the additive `SELECT` columns described above.
- A `status` filter parameter — not requested for this tool; embeddings only
  ever exist for failure events anyway, so `search()`'s optional `status`
  parameter is simply not passed (stays `None`).

## Tool docstring requirement (not just a comment — a deliverable)

Per the workflow's "concepts to explain," the tool's docstring is the routing
signal the agent uses to pick this tool over `query_stats`. It must, in the
coder's own words:

- Make explicit this tool is for **meaning/fuzzy** questions over messy
  failure text — e.g. "is anything unusual in the errors?", "find failures
  similar to X", "are there new/novel error patterns?", "what are the timeout
  errors saying?" — and that `query_stats` (not this tool) answers counting/
  rate/top-N questions.
- State plainly that this tool returns **individual matching transactions**
  (with IDs), not aggregates.

## Acceptance criteria

- [ ] `uv run python -c "from mcp_server.semantic import semantic_search"` works.
- [ ] Behaviors 1–9 each covered by at least one test in
      `tests/test_mcp_semantic_search.py` (one test file for this feature,
      following the 11B pattern: rollback-wrapped synthetic transactions +
      embeddings, a deterministic fake `Embedder`, no real model load).
- [ ] Every listed error case raises `ValueError` before `search()` is called.
- [ ] Test coverage forces both the `"exact"` and `"hnsw"` paths at least once
      (via the `exact_scan_threshold` passthrough) and asserts the header's
      `path` matches.
- [ ] Tool `semantic_search` is callable through the FastMCP in-memory client
      and returns the documented shape (behavior 9).
- [ ] Manual/live check (not a pytest case): from the MCP inspector or an
      ad-hoc script, `"connection timed out"` surfaces gateway-timeout
      failures with high similarity and correct transaction IDs; `"do not
      honor"` surfaces those declines; a paraphrase of the novel error
      (sharing no keywords with the raw text) still surfaces it with `path`
      showing the exact-scan fallback fired; `gateway=<one gateway>` narrows
      results; spot-check one returned `transaction_id` against the live DB.
