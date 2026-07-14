# Step 18B — Golden question set

## Feature

`demo/golden_questions.json` — the canonical, structured golden-question set
that documents what best demonstrates the system, one entry per capability
category, each carrying the incident context it runs under and the checkable
assertions a correct answer must satisfy. Consumed by humans now (this
step's own live verification) and by Week 5's eval harness later (Steps
19-20) — this step documents and locks the set, it does not build the
harness that grades it.

## Context

- `demo/run_demo.py` (community=85, per `graphify query`) already asks five
  of the six golden categories live, with question text already verified
  correct across two clean back-to-back `make demo` runs in 18A:
  - `BASELINE_QUESTION` (L122) — pure aggregation, asked after the fixed
    baseline wait, no incident active.
  - `GATEWAY_QUESTION` (L140-144) — rate comparison / incident detection,
    asked after `_gateway_degraded()` (L351) gates on
    `query_stats(metric="failure_rate", group_by="gateway",
    window_minutes=2)` showing `TARGET_GATEWAY` ("stripe-proxy", L114) at or
    above `GATEWAY_FAILURE_RATE_THRESHOLD` (0.12, L118).
  - `FRAUD_QUESTION` (L154-158) — structured pattern (fraud), asked after
    `_fraud_visible()` (L365) gates on `get_transactions(method="card",
    window_minutes=2, limit=100)` returning >= `FRAUD_MIN_MATCHING_ROWS` (5,
    L119) rows matching `TARGET_CARD_BIN` (`CARD_BINS[0]`, L115) under
    `FRAUD_MAX_AMOUNT` ($5.00, L120).
  - `NOVEL_QUESTION` (L182-188) — semantic / novelty, asked after
    `_novel_error_visible()` (L381) gates on `get_transactions(status=
    "failure", window_minutes=2, limit=100)` containing
    `producer.scenarios.NOVEL_ERROR_SIGNATURE` in `error_text`, targeting
    `TARGET_MERCHANT` (`MERCHANTS[0]`, L116).
  - `FRESHNESS_QUESTION` (L189) — freshness, asked at wrap-up with no
    incident gating (freshness is always answerable).
  - `HALLUCINATION_QUESTION` (L190) — negative/hallucination control
    ("How many crypto payments failed today?"), asked at wrap-up; `crypto`
    is not a valid `method` per `mcp_server/validation.py`'s enum
    allowlist, so the correct answer is an honest "no such data," not a
    fabricated number (this exact probe already verified in Step 16, see
    PROJECT_STATE.md's "Hallucination probe" note).
  - Reuse this exact wording verbatim for the five categories above — it is
    already live-verified, not a fresh untested question. Do not invent new
    phrasing.
- Sixth category (pure aggregation is covered by `BASELINE_QUESTION`; the
  spec's six categories are: aggregation, rate comparison, structured
  pattern, semantic/novelty, freshness, negative control — all six are
  already covered by the five constants above, since aggregation and rate
  comparison are two different questions but the set only needs one example
  each). No new question category needs inventing beyond what `run_demo.py`
  already asks.
- `producer/inject.py` — CLI arg names for `incident_context` field:
  `gateway_degradation --gateway --duration --severity`, `fraud_burst
  --duration --card-bin --intensity`, `novel_error_pattern --duration
  --merchant --intensity` (per 18A spec's Context section, already locked).
- Tool names for the `tools` field must match real MCP tool names:
  `query_stats`, `semantic_search`, `get_transactions`, `system_freshness`
  (`mcp_server/server.py`).
- No `demo/golden_questions.json` exists yet.

## Behavior

1. Write `demo/golden_questions.json`: a JSON array (or `{"questions": [...]}`
   object) of exactly 6 entries, one per capability category listed above.
2. Each entry is an object with these fields:
   - `id` — short stable slug, e.g. `"aggregation"`, `"gateway_rate"`,
     `"fraud_pattern"`, `"novel_error"`, `"freshness"`, `"hallucination_control"`.
   - `category` — human-readable label matching the spec's six categories
     (Pure aggregation / Rate comparison / Structured pattern / Semantic
     novelty / Freshness / Negative control).
   - `question` — the exact text, copied verbatim from the matching
     `run_demo.py` constant for the five reused categories.
   - `incident_context` — `null` for categories with no incident (baseline
     aggregation, freshness, hallucination control), or an object naming the
     incident type + the fixed deterministic params from `run_demo.py`
     (e.g. `{"type": "gateway_degradation", "gateway": "stripe-proxy",
     "duration": "3m", "severity": "0.7"}`) for the three incident-gated
     categories — so Week 5 knows exactly what to inject and with what
     params before asking.
   - `tools_expected` — list of MCP tool name(s) a correct answer should
     have used (e.g. `["query_stats"]`, `["get_transactions"]`,
     `["semantic_search", "get_transactions"]`).
   - `assertions` — list of short, individually-checkable strings, each
     phrased so a grader (human now, harness later) can verify pass/fail
     against the literal answer text without further interpretation, e.g.
     `"states the time window examined"`, `"names the gateway
     stripe-proxy"`, `"quantifies the failure rate as a percentage or
     ratio"`, `"cites at least one real transaction_id"`, `"reports no
     matching data rather than inventing a number"`. Every entry needs at
     least 2 assertions; entries requiring citation need an explicit
     "cites >= 1 real transaction_id" assertion.
3. Every field must be concrete and literal — no placeholders like `<TBD>`.
   Values that must match `run_demo.py` exactly (question text, incident
   params, target gateway/BIN/merchant) must in fact match — this file
   documents already-verified behavior, it does not diverge from it.

## Inputs / Outputs

- Output: `demo/golden_questions.json` only. No changes to `run_demo.py` or
  any other existing file (the optional future refactor where `run_demo.py`
  loads questions from this file is explicitly out of scope — see below).
- No CLI/executable behavior of its own; it's a data file. A schema-check
  test may load and validate it with the stdlib `json` module.

## Edge cases & errors

- N/A — this is a static data file, not runtime logic. The one thing worth
  a test: the file is valid JSON and every entry has all required fields
  with non-empty values (catches typos/omissions, not runtime failures).

## Out of scope

- The Week-5 eval harness/grading logic itself (Steps 19-20) — this file is
  input to that harness, not the harness.
- Refactoring `run_demo.py` to load its questions from this file instead of
  its own inline constants — mentioned in the 18A spec as an optional future
  cleanup, not required here. `run_demo.py` is unmodified by this step.
- Inventing new golden questions beyond the six categories / five
  already-verified constants — reuse, don't expand scope.
- The README (Step 21), the demo video (Step 22).

## Acceptance criteria

- [ ] `demo/golden_questions.json` exists with exactly 6 entries, one per
      capability category, all fields populated per Behavior #2.
- [ ] The five reused questions' `question` text matches the corresponding
      `run_demo.py` constant verbatim; `incident_context` params match
      `run_demo.py`'s fixed deterministic targets/severities exactly.
- [ ] A schema-validation test confirms the file is valid JSON, has 6
      entries, and every entry has all required non-empty fields.
- [ ] Live verification: each of the 6 golden questions run through the live
      agent (injecting the tagged incident where applicable, gated by 18A's
      timing mechanism) and the answer satisfies every one of its
      assertions. Any flaky/ambiguous case gets its assertion or question
      text fixed now, not deferred to Week 5.
- [ ] Explain-back: the demo timing concept (ramp + ingest lag as a genuine
      distributed-systems detail, not cosmetic) delivered to the user.
- [ ] `PROJECT_STATE.md` updated: Step 18 checked off, Week 4 marked
      complete, "Current status"/"Next action" moved to Step 19. Do not
      start Step 19 itself.
