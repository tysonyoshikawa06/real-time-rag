---
name: feature12-semantic-search-status
description: Step 12 (semantic_search MCP tool) test history — UUID serialization bug found 2026-07-09, fixed and confirmed same day
metadata:
  type: project
---

2026-07-09 initial run: `tests/test_mcp_semantic_search.py` (Step 12, `semantic_search` MCP tool): 20/25 passed, 5 failed.

All 5 failures shared one root cause: `semantic_search()`'s returned `matches` entries carried `transaction_id` as a raw `psycopg`-returned `UUID` object instead of a `str`. This broke the spec's "every value already JSON-native" requirement (`json.dumps(result)` raised `TypeError: Object of type UUID is not JSON serializable`) and broke equality checks against string IDs elsewhere in the test file.

Failing tests were: `test_happy_path_header_and_top_match_shape`, `test_matches_ordered_nearest_first`, `test_window_minutes_excludes_and_includes_stale_row`, `test_gateway_filter_narrows_to_one_gateway`, `test_exact_scan_threshold_forces_hnsw_path`.

2026-07-09 follow-up run (same day, after coder fix): coder added `str()` around `transaction_id` in `mcp_server/semantic.py`. Re-ran and got **25/25 passed**. All 5 cleared together as predicted — confirms this was a single root cause, not 5 separate bugs.

**Why this matters:** validates the pattern that a UUID/Decimal/datetime JSON-serialization bug in a row-building path tends to manifest as multiple scattered-looking test failures with one fix.

**How to apply:** if similar "TypeError: Object of type X is not JSON serializable" failures appear in other MCP tool test files, check for the same class of bug (raw DB types leaking into response dicts) before assuming multiple bugs.
