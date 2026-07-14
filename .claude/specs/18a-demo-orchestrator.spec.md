# Step 18A — Demo orchestrator

## Feature

A scripted, repeatable demo runner (`python -m demo.run_demo`, `make demo`)
that brings the stack up cold, establishes a calm baseline, then narrates
all three incident types on a schedule, asking the agent grounded questions
at moments when each incident's signature has actually become observable —
never on a blind fixed sleep. Reuses existing pieces end to end; builds no
new retrieval, injection, or tool-use logic.

## Context

- `producer/inject.py` — the scenario CLI: `gateway_degradation
  --gateway --duration --severity`, `fraud_burst --duration --card-bin
  --intensity`, `novel_error_pattern --duration --merchant --intensity`,
  `status`, `clear`. All write to `producer/control.json`
  (`producer/scenarios.py:CONTROL_FILE`), polled by the running producer
  every 0.5s (`producer/scenarios.py:_POLL_INTERVAL`). The demo orchestrator
  calls this CLI (subprocess or direct import of its command functions) —
  it must not reimplement control-file writing.
- `producer/scenarios.py` — incident mechanics, useful for the demo's own
  readiness checks (not for the agent's answers):
  - `_apply_gateway_degradation`: per matching event, flips to `failure`
    with probability `severity` (default 0.35) and a timeout/network error
    string. Detectable via `query_stats(metric="failure_rate",
    group_by="gateway", window_minutes=...)` — this stays in the
    `query_stats` allowlist (`gateway` is a valid `group_by`/filter column
    per `mcp_server/validation.py`).
  - `_generate_fraud_event`: generates a *separate* extra event per tick
    with probability `intensity` (default 0.25) - always `status="success"`,
    `method="card"`, amount `$1-5`, fixed `card_bin`. Because it's always
    `success`, it is invisible to `query_stats(metric="failure_rate")` and
    to `semantic_search` (embeddings are failure-text only, per
    `consumer/db.py`'s selective-embedding design) - there is no
    `card_bin`/amount aggregate in any MCP tool's allowlist
    (`mcp_server/validation.py`'s `ALLOWED_COLUMNS` covers
    method/status/gateway/merchant only). The only way to observe this
    incident through the existing tools is `get_transactions(method="card",
    window_minutes=..., limit=...)` and inspect the returned rows'
    `card_bin`/`amount` fields directly - both for the demo's own readiness
    gate and for what the agent itself must do when asked the fraud
    question.
  - `_apply_novel_error`: per matching event from the target merchant, flips
    to `failure` with probability `intensity` (default 0.15) and sets
    `error_text` to the fixed `NOVEL_ERROR_SIGNATURE` constant
    (`producer/scenarios.py`, a `currency_mismatch...` string). The demo's
    own readiness gate may import and check for this exact constant via
    `get_transactions(status="failure", window_minutes=..., limit=...)`
    (string containment on returned `error_text`) - this is the demo
    script's internal plumbing only; the agent must still be asked to find
    it by meaning via `semantic_search`, not told the string.
  - `producer/config.py`: `GATEWAYS = ["stripe-proxy", "adyen-gw",
    "braintree-edge", "checkout-io"]`, `CARD_BINS` (6-digit strings, e.g.
    `"411111"` first), `MERCHANTS` (18 names, e.g. `"24 Hour Fitness"`
    first) - pick one fixed value per incident type (not random) so the
    demo is deterministic and repeatable run to run.
- `agent/loop.py` - `run_loop(question: str) -> str` (Step 15/16/17,
  already committed) is the reusable one-shot tool-use loop, already wired
  to `agent/system_prompt.md`'s grounding/citation/routing discipline. The
  demo asks each golden question through this function - it must not spin
  up a second tool-use implementation. Each demo question is independent
  (fresh throwaway history via `run_loop`), not a running conversation - the
  chat's multi-turn memory (Step 17, `agent/chat.py`) is a different
  feature, not needed here.
- `agent/mcp_bridge.py` - `MCPBridge` - the demo's own preflight/readiness
  checks (data-flowing check, incident-visibility gating) also go through
  this bridge (`bridge.call_tool("system_freshness", {...})` etc.), not a
  raw `psycopg` connection - same "the agent talks MCP, nothing bypasses
  it" rule from Step 15 applies to the demo's instrumentation too.
- `Makefile` - existing `up:` target runs `docker compose --env-file .env -f
  infra/docker-compose.yml up -d` (containers: `kafka`, `postgres`,
  `pgweb`); `produce:`/`consume:` run `uv run python -m producer.main` /
  `consumer.main` in the foreground (a human normally runs these in
  separate terminals) - the demo orchestrator must itself launch them as
  background subprocesses if they are not already running, since nothing
  else will start them for an unattended `make demo` run. Add a `demo:`
  target following the existing style (see `mcp:`/`chat:` at the end of the
  file): `uv run python -m demo.run_demo`.
- No `demo/` directory exists yet.

## Behavior

### Preflight

1. Check the three containers (`kafka`, `postgres`, `pgweb`) are running
   (e.g. parse `docker ps --format "{{.Names}}"` for the expected names). If
   not all up, run the same command `make up` runs (invoke `make up` via
   subprocess, or the equivalent `docker compose --env-file .env -f
   infra/docker-compose.yml up -d` - reuse, don't hand-roll a different
   compose invocation) and wait for them healthy.
2. Check data is actually flowing: call `system_freshness` via the bridge
   with `window_minutes=1`. If `event_count` is 0 (or the call fails because
   nothing is listening), the producer/consumer aren't running - launch
   `python -m producer.main` and `python -m consumer.main` as background
   subprocesses (redirect their stdout/stderr to log files under `demo/`,
   e.g. `demo/logs/producer.log`, `demo/logs/consumer.log`, so they don't
   clutter the narrated console), then re-check `system_freshness` after a
   few seconds until `event_count > 0`. Leave both running after the demo
   finishes (matches the existing dev workflow where these are long-running
   processes) - the script does not need to tear them down at exit.
3. Print a clear confirmation line once data is confirmed flowing (event
   count and p50 lag from the `system_freshness` result) before proceeding.
   If containers or processes still aren't reachable after a reasonable
   bounded wait, fail loudly with a clear message rather than proceeding
   silently into a demo that will only produce empty results.

### Baseline

4. After preflight, let the calm stream run for a fixed wait (default 30s,
   a module constant) before injecting anything - purely a time-based wait
   printed as a narrated beat ("letting the stream run to establish a
   baseline..."), not gated on any signal, because there is nothing incident
   specific to poll for yet; the absence of an anomaly isn't something you
   can detect early, only something you can wait long enough to be
   confident of. (Contrast with incident-visibility gating below, which
   *is* signal-gated - explain this distinction in the code's comments so
   the two kinds of "wait" aren't confused.)
5. Ask the pure-aggregation golden question here (e.g. "What are the top
   payment methods and gateways in the last 5 minutes?") via `run_loop`,
   print the answer.

### Scripted incident timeline

6. For each of the three incidents, in order - gateway degradation, fraud
   burst, novel error - sequentially (never overlapping: fully clear one
   before injecting the next):
   a. Print what is about to be injected and why (narration).
   b. Inject via `producer.inject` (subprocess or direct call into
      `producer/inject.py`'s command functions) with a fixed, deterministic
      target (see Context: `stripe-proxy` for gateway degradation,
      `CARD_BINS[0]` for fraud burst, `MERCHANTS[0]` for novel error) and a
      generous duration (e.g. 3 minutes) so it can't expire mid-demo before
      being explicitly cleared in step (d).
   c. **Gate on real visibility, not a blind sleep** (see Behavior item 7
      below for the mechanism per incident type). Poll on a short interval
      (e.g. every 5s) up to a bounded max wait (e.g. 90s); print a short
      "waiting for the incident to become visible..." narration while
      polling, not a silent hang. If the max wait is exceeded without the
      signal appearing, print a clear warning and proceed to ask the
      question anyway (a demo run should never hang forever - a late or
      missing signal is itself useful information, not a reason to freeze).
   d. Ask that incident's golden question via `run_loop`, print the answer.
   e. Clear the incident (`producer.inject clear`) and pause briefly (e.g.
      2s, to let the producer's 0.5s poll pick up the clear) before moving
      to the next incident - this keeps the timeline sequential and
      unambiguous rather than waiting out the full injected duration.
7. Per-incident visibility gate (all via `MCPBridge`, all client-side checks
   in the demo script - the agent itself is never told what to look for):
   - **Gateway degradation**: poll `query_stats(metric="failure_rate",
     group_by="gateway", window_minutes=2)`; ready when the targeted
     gateway's `failure_rate` is clearly elevated (e.g. >= 0.12, well above
     the ~0.03-0.05 normal baseline established in prior steps).
   - **Fraud burst**: poll `get_transactions(method="card",
     window_minutes=2, limit=100)`; ready when at least a handful (e.g. >=5)
     of the returned rows carry the injected `card_bin` with `amount < 5.00`
     (client-side check over the returned rows - there is no aggregate tool
     for this, per Context).
   - **Novel error**: poll `get_transactions(status="failure",
     window_minutes=2, limit=100)`; ready when any returned row's
     `error_text` contains the known novel-error signature (imported from
     `producer.scenarios.NOVEL_ERROR_SIGNATURE` for the check - again,
     client-side only, the agent is not given this string).
8. After all three incidents, ask the two remaining golden categories
   (order doesn't matter, e.g. at the end): the freshness question (e.g.
   "How current is this data?") and the hallucination-control question
   (e.g. "How many crypto payments failed today?"), via `run_loop`, printing
   each answer.

### Modes

9. `--pause` flag: before each narrated beat (each injection, each
   question, and the transition into baseline), wait for an `Enter`
   keypress (`input()`) instead of continuing immediately - for live/
   recorded demos where a human is narrating alongside it.
10. Default (no `--pause`): fully unattended - proceed as soon as each
    step's real gating condition (or the fixed baseline wait) is satisfied,
    with no artificial extra delay. This is what `make demo` runs as a
    smoke test.

## Inputs / Outputs

- `python -m demo.run_demo [--pause]` / `make demo`: no other required
  inputs. Console output is the entire "narrated session" - preflight
  status, baseline countdown, per-incident injection/gating/answer beats,
  final freshness + hallucination-control beats.
- No new files written besides the producer/consumer log files under
  `demo/logs/` (create the directory if missing) and whatever
  `producer/inject.py` already writes (`producer/control.json`,
  `eval/ground_truth/incidents.jsonl` - untouched, already-existing
  behavior).

## Edge cases & errors

- Containers or producer/consumer unreachable after a bounded preflight
  wait: fail loudly with a clear message, don't proceed into a demo that
  can only produce empty results.
- Incident visibility gate exceeds its max wait: warn clearly, proceed to
  ask the question anyway rather than hanging indefinitely.
- Running `make demo` a second time back-to-back: must work identically
  (repeatability is an explicit verification requirement) - since incidents
  are explicitly cleared at the end of each incident's beat (step 6e), nothing
  incident-related should be left active between runs; the fixed target
  values (`stripe-proxy`/`CARD_BINS[0]`/`MERCHANTS[0]`) mean the second run's
  baseline window may still contain residue from the first run's incidents
  if run back-to-back quickly - that's fine and expected (real operational
  data doesn't reset between demo runs either), not a bug to work around.
- `Ctrl-C` during the demo: acceptable to exit however Python's default
  `KeyboardInterrupt` handling does (a clean-ish traceback is fine here,
  unlike the Step 17 chat REPL requirement - this is a scripted run, not an
  interactive loop the spec asks to harden against Ctrl-C).

## Out of scope

- The Week-5 eval harness/grading itself (Steps 19-20) - the demo prints
  answers for a human to read, it does not grade them.
- `demo/golden_questions.md` and its assertions (Step 18B, next sub-commit)
  - 18A's questions are simple inline strings in the demo script; 18B
    formalizes the canonical set separately and may, as a minimal follow-up
    refactor, have the demo script load its questions from that file instead
    of duplicating them - not required for 18A to anticipate.
- The README (Step 21), the demo video (Step 22).
- Any change to `agent/loop.py`, `agent/mcp_bridge.py`,
  `agent/system_prompt.md`, or any MCP tool - the demo only calls existing
  entry points.
- Tearing down producer/consumer/containers at the end of the run.
- Handling Ctrl-C gracefully mid-run (unlike Step 17's chat REPL - see Edge
  cases).

## Acceptance criteria

- [ ] `demo/run_demo.py` (plus `demo/__init__.py`) exists; `python -m
      demo.run_demo` and `make demo` both launch it.
- [ ] Preflight brings the stack and producer/consumer up if not already
      running, and confirms data is flowing via `system_freshness` before
      proceeding.
- [ ] Baseline: a fixed, narrated wait, then the pure-aggregation question
      answered correctly (window stated, ranked counts, basis line).
- [ ] All three incidents run sequentially, each: injected with a fixed
      deterministic target, gated on a real, incident-specific visibility
      signal (not a blind sleep), then answered correctly and groundedly
      (gateway named + rate quantified for degradation; shared-BIN/
      small-amount pattern with cited transaction_ids for fraud; the novel
      error surfaced by meaning with cited transaction_ids for the novel
      pattern), then explicitly cleared before the next incident begins.
- [ ] Freshness and hallucination-control questions answered correctly at
      the end.
- [ ] `--pause` mode waits for Enter between beats; default mode runs fully
      unattended.
- [ ] `make demo` run twice back-to-back both complete successfully
      start-to-finish with correct, grounded answers each time
      (repeatability).
- [ ] Explain-back: why gated timing (ramp + ingest lag) is a genuine
      distributed-systems detail, not a cosmetic delay - delivered to the
      user after verification.
- [ ] Stop here for commit (per process rules) - do not start 18B
      (`demo/golden_questions.md`) until the user has reviewed and committed
      18A.
