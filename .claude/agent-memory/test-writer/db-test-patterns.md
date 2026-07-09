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
