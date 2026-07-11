---
name: stale-test-triage
description: How to tell a genuine code bug from a pre-spec-update test in this repo's fix loop, without editing tests
metadata:
  type: feedback
---

When a spec explicitly changes a bound or behavior (e.g. "reject → clamp",
"max 360 → max 1440", "add a new required output key"), pre-existing tests
written against the *old* behavior will fail after a correct implementation —
this is expected, not a sign the code is wrong. Triage each failure:

1. Does the spec explicitly call out this old behavior as changing (grep the
   spec's Behavior section for "change from current ... behavior")? If yes,
   and the test still asserts the old behavior, it's a stale test — do not
   touch it (hook blocks it anyway); report it to the orchestrator by name so
   the test-writer can update it.
2. Does the failure trace show the code touching a resource (DB cursor,
   embedder) *before* finishing input validation, in a case where validation
   should have run first regardless of old/new spec? That's a real ordering
   bug in the implementation — fix it.
3. Does the failure show a missing/extra key in a returned dict that the spec
   doesn't explicitly mandate either way? Lean toward adding it if it makes
   the tool self-consistent with sibling tools (e.g. `stats.py` was the only
   one of four MCP delegates not echoing back its own `limit` after adding
   clamping — added it since `window_minutes`/`k` were already echoed
   elsewhere) — this is a legitimate implementation gap, not a spec conflict.

**Why:** In the Step 14 (`mcp_server` validation) task, ~15 failures across
`test_mcp_semantic_search.py`, `test_mcp_get_transactions.py`, and
`test_mcp_system_freshness.py` were pre-existing tests asserting reject-only
behavior/bounds (360 vs 1440, no-`notes`-key, etc.) that the locked spec
explicitly said must change to clamp — verified this by re-reading the spec's
Behavior section line by line before assuming my code was wrong. Meanwhile a
real bug (creating the DB cursor before validating `transaction_ids`
length/UUID format and before enum-checking `status`/`method` in
`get_transactions`) surfaced in the exact same test run and needed an actual
code fix.

**How to apply:** Whenever the test-runner reports failures for a
spec-implementing feature, re-open the spec first and check whether the
failing assertion matches something the spec says should have changed. Only
then start reading tracebacks for a code-side root cause.
