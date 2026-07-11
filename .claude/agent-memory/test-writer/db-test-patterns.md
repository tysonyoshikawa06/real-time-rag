---
name: db-test-patterns
description: Reusable Postgres/MCP test patterns for this repo — rollback-isolated seeding, live-stream determinism, exploding-conn validation checks, fastmcp in-memory client
metadata:
  type: project
---

Test patterns that worked for tests/test_mcp_query_stats.py (spec 11B) and generalize to future features here.

**Why:** the dev Postgres (localhost:5433, dbname=streaming_rag, user=rag, pw=localdev; prefer `consumer.config.POSTGRES_DSN`) may have a LIVE producer/consumer streaming rows into `transactions` while tests run, and tests must leave no trace.

**How to apply:**
- `tests/conftest.py` has `connect_db()`, `db_conn` fixture (REPEATABLE READ, always rolled back), and `connect_db_factory` fixture. REPEATABLE READ matters: it freezes the snapshot so a baseline `count(*)` taken inside the txn stays consistent with later queries despite concurrent live inserts. Postgres `now()` = transaction start, also stable in-txn.
- Seed synthetic rows with distinctive values (e.g. merchant "TEST-MERCHANT-11B") and filter assertions to them for exact numbers; for unavoidable unfiltered assertions, measure a baseline inside the same snapshot first and assert `baseline + N`.
- Insert timestamps via SQL `now() - make_interval(mins => %s)` (avoids host/DB clock skew).
- `transactions` CHECK constraints: method IN ('card','ach','wallet'), status IN ('success','failure'); merchant/gateway/amount NOT NULL; transaction_id is uuid.
- "ValueError before SQL" spec claims: pass a `_ForbiddenConn` whose `__getattr__` raises AssertionError — proves validation never touches the connection.
- "issues only SELECTs" claims: set `conn.read_only = True` and call the function; any write raises ReadOnlySqlTransaction.
- fastmcp behavior tests: no pytest-asyncio in deps — use `asyncio.run()` inside sync tests with `async with fastmcp.Client(mcp)`. Extract tool payload via helper trying `.data`, then `.structured_content`, then `json.loads(result.content[0].text)`.
- The project has no build-system in pyproject, so `uv run pytest` does NOT put the repo root on sys.path — app packages (mcp_server, consumer) fail to import from test modules. Fix lives in tests/conftest.py: prepend `Path(__file__).parent.parent` to sys.path before other imports (conftest loads before test modules). Already in place; don't remove it.
- Don't `from tests.conftest import ...` in test files (tests/ has no `__init__.py`; import mode makes it unreliable) — expose helpers as fixtures instead.
- To verify a test file collects before the parallel coder finishes: stub the missing package in the scratchpad and run collect-only with `PYTHONPATH=<stub> uv run pytest <file> -q --collect-only`.

**Seeding a second FK-linked table (e.g. `embeddings` -> `transactions`) (spec 12):**
- Call `from pgvector.psycopg import register_vector; register_vector(conn)` on the test connection before inserting/using any `vector(N)` column or passing an ndarray as a query param — psycopg has no adapter for pgvector's type otherwise. `consumer.db.connect()` does this for production connections; `tests/conftest.py`'s `db_conn`/`connect_db_factory` do NOT, so call it yourself in a fixture built on top of `db_conn`.
- Insert parent row (transactions) then child row (embeddings) in the same open transaction, same distinctive-gateway-per-test-file discipline described above. FK requires parent first.
- For a deterministic fake `Embedder` (ABC with `embed(self, texts) -> list[list[float]]`, L2-normalized, model-free): hash each token via `hashlib.sha256(tok.encode())` (NOT Python's salted `hash()` — not reproducible across processes) into one of N fixed dims + a sign bit from the same hash, sum per text, L2-normalize. Verified empirically: texts sharing literal words land reliably closer (smaller cosine distance) than texts sharing none (which land at exactly distance 1.0, orthogonal) — enough to assert exact top-match identity deterministically. It does NOT capture paraphrase-level semantic meaning (zero shared words -> orthogonal by construction), so don't use it to test real semantic/paraphrase quality — that has to stay a manual/live check with the real model.
- `json.dumps(result)` is a stronger, clearer-failure-message check for "plain JSON-serializable dict" spec claims than field-by-field `isinstance` — e.g. it directly catches an implementation returning a raw `uuid.UUID` (not JSON serializable, `TypeError: Object of type UUID is not JSON serializable`) instead of the spec-required plain `str`, where a bare `==` comparison against an expected string would raise a confusing `UUID(...) == 'string'` failure. Add both: `json.dumps(result)` for the blanket claim, plus a targeted `isinstance(field, str)` for the specific field.

**Seeding rows with precise known lag values (spec 13b `system_freshness`):**
- When testing freshness/lag percentiles that depend on (ingested_at - event_timestamp) computed values, set both timestamps explicitly in INSERT: event_timestamp to a fixed time (e.g. 5 min ago via SQL), ingested_at = event_timestamp + lag_seconds. This lets you assert exact percentile output without flaky race conditions (live data changing lag values).
- Query directly via raw psycopg to get the stable tx_now: `cur.execute("SELECT now()::timestamptz AS now"); tx_now = cur.fetchone()["now"]`. Use this baseline for all rows in a test so relative timedeltas are consistent.
- Pass lag values as list to a seed function, insert one row per lag value with precise timedeltas: `event_ts + timedelta(seconds=lag_sec)`. Then assert `percentile_cont` output via `round(value, 1)` matching the expected distribution (e.g. [0.5, 1.0, 1.5, 2.0, 2.5, 3.0] → p50=1.75, p99≈2.985).
- For testing "no events in window" case: insert a row with event_timestamp outside the query window (e.g. 10 min ago for a 5-min window test), verify event_count=0 and all percentile fields are None (not an error, per spec).

**Input validation + clamping test patterns (Step 14):**
- All tools gain a `notes: list[str]` key in return dict. Regression tests: valid defaults + in-range values should have `notes == []`.
- Clamp tests verify: a) the value is clamped to the max, b) `len(result["notes"]) > 0` is true, c) clamp note text contains the field name and some clamp verb ("capped", "exceeded", etc.), d) the clamped value respects the stated bound. Use `" ".join(result["notes"]).lower()` to check note text case-insensitively.
- Reject (non-clamping) numeric tests: all four tools reject window_minutes=0/-5/3.5/True/False. Use parametrize with multiple bad values; all should raise ValueError with "positive" or "integer" in message. Booleans are critical: `True`/`False` are instances of `int` in Python, so validation must explicitly reject them.
- Enum reject tests (e.g. `status="maybe"` for query_stats): use `_ForbiddenConn()` to ensure validation happens before SQL. Rejection message should list valid enum values (use lowercase assertion with `.lower()` for robustness).
- UUID validation test (get_transactions): pass `[bad_id, valid_id]` list; assert the bad_id name appears in the error message. Test absent-but-valid UUID too: should return `found rows + missing_ids`, never error.
- Exact message tests (e.g. get_transactions mutual exclusivity): copy the spec's required message verbatim, use `assert expected_msg in str(excinfo.value)` (substring match in case surrounding context is added).
- Boundary value tests (min/max edge cases): test=1, test=max_allowed should NOT clamp or note. Use same seeded data, assert `result["notes"] == []`.
- For truncation/limited-length (semantic_search query), test exactly-at-limit (2000 chars) and over-limit (2500 chars); assert truncated result is exactly the limit.
