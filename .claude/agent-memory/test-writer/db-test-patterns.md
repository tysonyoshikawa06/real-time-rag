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
