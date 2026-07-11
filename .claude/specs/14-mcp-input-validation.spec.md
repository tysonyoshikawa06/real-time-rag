# Step 14: MCP input validation + limits

## Feature

A single shared validation module, `mcp_server/validation.py`, that all four
MCP tools (`query_stats`, `semantic_search`, `get_transactions`,
`system_freshness`) call to validate and bound their inputs consistently,
before an LLM agent (Week 4) starts calling them with generated arguments.

## Context

- `mcp_server/stats.py::query_stats()` (L33) — currently only checks
  membership (`group_by`/filter keys in `_COLUMNS` L23-28, `metric` in
  `_METRICS` L30) and `> 0` on `window_minutes`/`limit`. No max caps, no
  status/method value whitelisting, no clamping.
- `mcp_server/semantic.py::semantic_search()` (L20) — rejects (does not
  clamp) `window_minutes` outside `(0, 1440]` (L45-48) and `k` outside
  `(0, 50]` (L49-50); rejects empty/whitespace `query` (L43-44); no length cap
  on `query`.
- `mcp_server/transactions.py::get_transactions()` (L67) — mode exclusivity
  check exists (L97-101) but with a different message than required below;
  `_MAX_WINDOW_MINUTES = 360` (L25-26, must become 1440 per this spec);
  `_MAX_IDS = 100` is a reject cap (L102-105, correct — keep as reject); no
  UUID format validation on `transaction_ids` before hitting SQL; `limit`
  over `_MAX_LIMIT` is rejected (L106-107, must become clamp); status/method
  filter values are not whitelisted.
- `mcp_server/freshness.py::system_freshness()` (L21) — `_MAX_WINDOW_MINUTES
  = 60` (L18) is a reject cap (L33-36, must become clamp).
- `mcp_server/server.py` registers all four tools (`query_stats` L28,
  `semantic_search` L58, `get_transactions` L99, `system_freshness` L145) and
  owns connection lifecycle; tool signatures/docstrings there are unaffected
  by this step except where a new `notes`/error shape needs mentioning.
- Existing tests: `tests/test_mcp_query_stats.py`,
  `tests/test_mcp_semantic_search.py`, `tests/test_mcp_get_transactions.py`,
  `tests/test_mcp_system_freshness.py` — this step's test-writer appends new
  cases to these same four files (one file per existing feature), it does not
  create a fifth file for `validation.py` in isolation from its callers.

## Behavior

### Shared module: `mcp_server/validation.py`

1. Exposes the column allowlist (`method`, `status`, `gateway`, `merchant`)
   for `group_by`/filter keys, shared by `query_stats` and `get_transactions`.
2. Exposes enum allowlists: `status` ∈ {`success`, `failure`}, `method` ∈
   {`card`, `ach`, `wallet`}, `metric` ∈ {`count`, `failure_rate`}.
3. Provides a clamp helper: given a value, a name, a default, and a max
   (min is always 1), returns `(effective_value, note | None)`. If the
   caller passes `None`, use the default silently (no note). If the value is
   a positive integer `<= max`, pass through unchanged, no note. If `> max`,
   return `(max, "<name> capped at <max> (requested <value>)")`.
4. Provides a "reject if not a positive integer" check (used before
   clamping): non-integer or non-positive input raises `ValueError` stating
   the rule and how to fix it (e.g. `"window_minutes must be a positive
   integer, got 0"`). Booleans are not accepted as integers (Python
   `bool`/`int` overlap — reject `True`/`False` explicitly).
5. Provides an enum-membership check: value not in the allowed set raises
   `ValueError` naming the field, the invalid value, and the full sorted
   list of valid options.
6. Provides a column/key allowlist check (for `group_by` and `filters` keys):
   same message shape as (5) — invalid key, valid options listed.
7. Provides a UUID-format check for a list of ID strings: returns which
   entries are not valid UUIDs (does not itself raise — callers decide
   reject vs. partial-accept, per tool below). Uses `uuid.UUID(x)` parsing,
   not a regex.
8. Provides a query-text check: empty/whitespace-only raises `ValueError`;
   longer than a max length is truncated to that length and a note is
   returned alongside the truncated string (caller attaches the note).
9. All `ValueError` messages follow one shape: state what's wrong, then
   what to pass instead (valid range or the allowed set). No bare "invalid
   input".
10. This module has no dependency on `psycopg`/FastMCP — it is pure
    validation logic, importable and testable standalone.

### `query_stats` (`mcp_server/stats.py`)

11. `window_minutes`: default 30. Non-positive/non-integer → reject. `> 1440`
    → clamp to 1440 with a note (comment in code: aggregation can reasonably
    span up to a day of retained data).
12. `group_by`: if given, must be one of the allowed columns, else reject
    listing valid columns.
13. `filters`: each key must be an allowed column, else reject listing valid
    columns (naming the bad key(s)). `status`/`method` values, if those keys
    are present, must be in their enum allowlists, else reject listing valid
    values. `gateway`/`merchant` values are free text — never validated
    against a whitelist, always passed as a bound SQL parameter (already the
    case).
14. `metric`: must be in `{count, failure_rate}`, else reject listing valid
    values (this check already exists — keep it, route it through the
    shared enum helper).
15. `limit`: default 10. Non-positive/non-integer → reject. `> 100` → clamp
    to 100 with a note.
16. The returned dict gains a `notes: list[str]` key (empty list when no
    clamping occurred) carrying any clamp notes from window_minutes/limit.

### `semantic_search` (`mcp_server/semantic.py`)

17. `query`: required; empty/whitespace-only → reject (existing check, keep,
    route through shared helper). Longer than 2000 chars → truncate to 2000
    and add a note (do not reject).
18. `window_minutes`: default 30. Non-positive/non-integer → reject. `> 1440`
    → clamp with a note (change from current reject-only behavior).
19. `k`: default 10. Non-positive/non-integer → reject. `> 50` → clamp with
    a note (change from current reject-only behavior).
20. `gateway`: free text, parameterized, never whitelisted (unchanged).
21. The returned dict gains a `notes: list[str]` key (empty list when
    nothing was clamped/truncated) carrying any window/k/query-truncation
    notes.

### `get_transactions` (`mcp_server/transactions.py`)

22. Mutual exclusivity: if `transaction_ids` (non-empty) AND any of
    `window_minutes`/`status`/`gateway`/`method` are supplied → reject with
    exactly this message: `"get_transactions accepts either transaction_ids
    OR filter params (window_minutes/status/gateway/method), not both. Pass
    IDs to look up specific rows, or filters to search."` (replaces the
    current message at L98-101).
23. Neither `transaction_ids` nor any filter param supplied → filter mode
    with defaults (window 30, limit 10) — already the behavior, keep.
24. ID mode: each string in `transaction_ids` must be a valid UUID (via the
    shared UUID check) — malformed entries → reject, naming which entries
    are malformed. `transaction_ids` longer than 100 → reject (do not clamp
    — dropping requested IDs breaks grounding); keep the existing
    `_MAX_IDS` reject behavior. Valid-but-not-found IDs → unchanged: return
    found rows plus `missing_ids`.
25. Filter mode: `window_minutes` default 30, max raised from 360 to 1440
    (comment noting this bound reflects data retention and can be lowered if
    the environment resets often); non-positive/non-integer → reject; `>
    1440` → clamp with a note. `status`/`method`, if given, must be in their
    enum allowlists → reject listing valid values, naming the bad value.
    `gateway`/`merchant` free text, parameterized, unchanged. `limit`
    default 10; non-positive/non-integer → reject; `> 100` → clamp with a
    note (change from current reject-only behavior).
26. The returned dict gains a `notes: list[str]` key (empty list when
    nothing was clamped) in both modes.

### `system_freshness` (`mcp_server/freshness.py`)

27. `window_minutes`: default 5. Non-positive/non-integer → reject. `> 60` →
    clamp to 60 with a note (change from current reject-only behavior;
    comment stays: freshness is a "now" metric, long windows stop meaning
    current).
28. The returned dict gains a `notes: list[str]` key (empty list when
    nothing was clamped).

## Inputs / Outputs

- No change to any tool's parameter names or types in `server.py` — this
  step changes validation/clamping behavior inside the delegate modules
  (`stats.py`, `semantic.py`, `transactions.py`, `freshness.py`), plus the
  new shared `notes: list[str]` key on every tool's return dict.
- `validation.py` exports (suggested names, coder may refine): `ALLOWED_COLUMNS`,
  `ALLOWED_STATUS`, `ALLOWED_METHOD`, `ALLOWED_METRIC`, `require_positive_int(name,
  value) -> int`, `clamp_positive_int(name, value, default, max_value) ->
  tuple[int, str | None]`, `check_enum(name, value, allowed) -> None` (raises),
  `check_allowed_keys(kind, keys, allowed) -> None` (raises),
  `find_invalid_uuids(ids) -> list[str]`, `check_query_text(query, max_len) ->
  tuple[str, str | None]`.
- All four tools' return dicts gain `"notes": list[str]`.

## Edge cases & errors

- `query_stats(group_by="banana")` → `ValueError` listing
  `{gateway, merchant, method, status}` as valid columns.
- `query_stats(limit=99999)` → clamps to 100, `notes` includes `"limit
  capped at 100 (requested 99999)"`.
- `query_stats(filters={"status": "maybe"})` → `ValueError` listing
  `{failure, success}` as valid statuses.
- `semantic_search(query="   ")` → `ValueError` (empty/whitespace).
- `semantic_search(k=1000)` → clamps to 50, `notes` includes a capped-k note.
- `semantic_search(query=<2500 chars>)` → truncated to 2000 chars, `notes`
  includes a truncation note.
- `get_transactions(transaction_ids=[...], gateway="stripe-proxy")` → reject
  with the exact message from Behavior #22.
- `get_transactions(transaction_ids=<500 ids>)` → reject (too many), message
  states the cap (100) and the count given.
- `get_transactions(transaction_ids=["not-a-uuid", <valid-uuid>])` → reject,
  naming `"not-a-uuid"` specifically as malformed.
- `get_transactions(transaction_ids=[<valid-but-absent-uuid>])` → returns
  `rows: []`, `missing_ids: [<that-uuid>]`, not an error.
- `get_transactions()` (no args at all) → filter mode, window 30, limit 10.
- `system_freshness(window_minutes=600)` → clamps to 60, `notes` includes a
  capped-window note.
- Every previously-valid call (defaults, in-range values) must still return
  the same shape plus an empty `notes: []` — no behavior regression.
- Non-integer or non-positive numeric args (e.g. `window_minutes=0`,
  `window_minutes=-5`, `window_minutes=3.5`, `window_minutes=True`) always
  reject, never clamp, across all four tools.

## Out of scope

- Week 4 agent/tool-use loop — not started in this step.
- Rate limiting, auth, or request-level throttling.
- Changing the DB schema, SQL query shapes beyond what's needed for
  whitelisting, or the exact-scan/HNSW decision logic in `consumer/search.py`.
- Changing `consumer/freshness.py::query_freshness()` itself (freshness.py's
  window-minutes-to-interval translation is the only touch point).
- New MCP tools or renamed tool parameters.

## Acceptance criteria

- [ ] `mcp_server/validation.py` exists with the shared helpers above; no
      `psycopg`/`fastmcp` import in it.
- [ ] All four delegate modules (`stats.py`, `semantic.py`, `transactions.py`,
      `freshness.py`) call into `validation.py` for bounds/allowlist checks
      instead of repeating inline checks.
- [ ] Every tool's return dict includes `notes: list[str]`.
- [ ] `get_transactions` mutual-exclusivity error uses the exact message
      from Behavior #22.
- [ ] All clamp bounds match: query_stats window 1440/limit 100,
      semantic_search window 1440/k 50/query 2000 chars, get_transactions
      window 1440/limit 100/ids cap 100 (reject, not clamp),
      system_freshness window 60.
- [ ] All edge cases in this spec pass as tests in the four existing test
      files (appended, not new files).
- [ ] `uv run pytest tests/ -q` passes in full.
- [ ] `PROJECT_STATE.md` updated: Step 14 checked off, Week 3 marked
      complete, current status pointed at Step 15 (per CLAUDE.md process
      rules — orchestrator does this after tests pass, not the coder).
