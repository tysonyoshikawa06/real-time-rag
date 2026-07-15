# Step 19A — Eval runner (capture only, no grading)

## Feature

`eval/run_eval.py` (`python -m eval.run_eval`, `make eval-run`) — runs every
golden question from `demo/golden_questions.json` N times each (default 3)
against the live agent, resetting stream state and gating on real
incident-visibility between runs exactly like Step 18A's demo does, and
persists a fully self-contained JSON transcript file per invocation. This
step captures only — no scoring, no pass/fail. Grading is Step 19B, offline,
against the saved file.

## Context

- `demo/golden_questions.json` (Step 18B) — 6 entries, each with `id`,
  `category`, `question`, `incident_context` (`null`, or an object with
  `type` + fixed params: `gateway`/`card_bin`/`merchant`, `duration`, and
  `severity`/`intensity` depending on type), `tools_expected`, `assertions`.
  This file is the runner's only question source — do not duplicate question
  text or incident params inline in `eval/run_eval.py`.
- `demo/run_demo.py` (Step 18A, community=85 per `graphify query`) currently
  contains, as private module-level functions, everything this step needs to
  reuse: `_parse_tool_json` (L255), `_inject`/`_clear_incident` (L322-329,
  subprocess wrappers around `producer.inject`), `_poll_until` (L332-348, the
  generic signal-gated poll loop — 5s interval / 90s bounded max wait, warns
  and proceeds on timeout rather than hanging), and three incident-specific
  visibility predicates keyed to run_demo.py's own fixed constants:
  `_gateway_degraded` (L351, polls `query_stats(metric="failure_rate",
  group_by="gateway", window_minutes=2)` for `TARGET_GATEWAY` >=
  `GATEWAY_FAILURE_RATE_THRESHOLD` 0.12), `_fraud_visible` (L365, polls
  `get_transactions(method="card", window_minutes=2, limit=100)` for >=5 rows
  matching `TARGET_CARD_BIN` under $5), `_novel_error_visible` (L381, polls
  `get_transactions(status="failure", window_minutes=2, limit=100)` for
  `producer.scenarios.NOVEL_ERROR_SIGNATURE` in `error_text`). Also
  `POST_GATE_BUFFER_SECONDS` (75, L110) and `POST_CLEAR_PAUSE_SECONDS` (2,
  L67) — extra fixed settle time proven live in 18A/18B to push a
  gateway_degradation/novel_error_pattern incident's window-averaged signal
  clear of this dev environment's chronic residual baseline (see
  run_demo.py's own extensive comments at L69-110 for why).
  **This step's required refactor:** extract all of the above into a new
  shared module `demo/incident_control.py` — same logic, made public and
  parameterized (targets/thresholds become function arguments instead of
  reads of run_demo.py's fixed module constants) so `eval/run_eval.py` can
  drive arbitrary incident targets read from `golden_questions.json` instead
  of run_demo.py's hardcoded demo targets. Update `demo/run_demo.py` to call
  into `demo/incident_control.py` with its existing constants passed as
  arguments — this must be a **pure, behavior-preserving extraction**: same
  polling cadence, same thresholds, same buffer/pause durations, same
  printed narration. Do not change `make demo`'s observable behavior.
- `agent/loop.py::run_turn(messages, question, bridge, tools, on_call=None,
  on_result=None) -> (answer_text, last_response)` (L90-142) is the reusable
  call point — it mutates a caller-supplied `messages` list in place with
  every assistant/tool_result turn, which is exactly the transcript this
  step needs to capture. Call `run_turn` directly (fresh `messages = []`,
  fresh `MCPBridge()`, fresh `bridge.list_tools()` per run — same per-call
  setup `run_loop` (L145-158) does internally, but `run_loop` itself doesn't
  expose `messages`, so call `run_turn` instead of `run_loop`). Do not modify
  `agent/loop.py` or `agent/mcp_bridge.py` — call them programmatically as-is.
  Content blocks in assistant messages are Anthropic SDK Pydantic objects
  (`TextBlock`/`ToolUseBlock`) with `.model_dump()` for JSON serialization;
  user/tool_result messages `run_turn` appends are already plain dicts.
- `mcp_server/stats.py::query_stats(conn, window_minutes, group_by, filters,
  metric, limit) -> dict` (L38-127) is a pure function over a psycopg
  connection — call it **directly against your own connection with your own
  chosen window/group_by/metric**, not through the MCP layer and not by
  reading the agent's own tool call. This is what makes the resulting number
  independent ground truth rather than a replay of what the agent asked.
- `consumer/db.py::connect() -> psycopg.Connection` (L47-50-ish, registers
  pgvector) and `consumer/freshness.py::query_freshness(window: str) -> dict
  | None` (opens/closes its own connection) are the existing DB connection
  patterns to reuse for ground-truth queries — don't hand-roll a new
  connection helper.
- `producer/scenarios.py::NOVEL_ERROR_SIGNATURE` (the fixed novel-error
  string) and `producer/inject.py`'s CLI arg shapes (`gateway_degradation
  --gateway --duration --severity`, `fraud_burst --duration --card-bin
  --intensity`, `novel_error_pattern --duration --merchant --intensity`) —
  `eval/run_eval.py` maps each question's `incident_context` dict onto these
  exact CLI args when injecting (via `demo/incident_control.py`'s
  `inject()`).
- `eval/ground_truth/incidents.jsonl` (scenario engine's own ground-truth
  log, one JSON object per line: `incident_id`, `type`, `action`
  start/end`, `timestamp`, `params`) — read this file after asking an
  incident-tagged question and filter to the record(s) whose `timestamp` is
  >= the moment this run called `inject()`, to snapshot the *independent*
  confirmation that the scenario engine actually activated what was asked
  for (not just that the CLI call returned).
- No `eval/__init__.py`, `eval/run_eval.py`, `eval/ground_truth_queries.py`,
  `demo/incident_control.py`, or `eval/results/` exist yet. `.gitignore`
  already excludes `eval/ground_truth/*.jsonl` as generated; add
  `eval/results/*.json` alongside it (same treatment — generated eval
  artifacts, not source).

## Behavior

### `demo/incident_control.py` (new, extracted + parameterized)

1. `parse_tool_json(text) -> dict` — best-effort JSON parse of an MCP tool's
   text result (non-JSON, e.g. a connection error, returns `{}`).
2. `inject(*args) -> None` / `clear_incident() -> None` — subprocess wrappers
   around `python -m producer.inject`, moved verbatim from run_demo.py.
3. `poll_until(check_fn, label, poll_interval=5, max_wait=90) -> bool` —
   moved verbatim (parameterized defaults matching current constants),
   prints the same "waiting for ... to become visible" / warning-on-timeout
   narration.
4. `gateway_failure_rate_elevated(bridge, gateway, window_minutes=2,
   threshold=0.12) -> bool`, `fraud_pattern_visible(bridge, card_bin,
   window_minutes=2, min_rows=5, max_amount=5.00) -> bool`,
   `novel_error_visible(bridge, signature, window_minutes=2) -> bool` — same
   query/threshold logic as run_demo.py's three predicates, now taking the
   target as a parameter instead of reading a fixed module constant.
5. `POST_GATE_BUFFER_SECONDS = 75`, `POST_CLEAR_PAUSE_SECONDS = 2` — moved
   as shared constants.
6. `demo/run_demo.py` updated to import and call all of the above with its
   own existing fixed constants (`TARGET_GATEWAY`, `GATEWAY_FAILURE_RATE_THRESHOLD`,
   etc. stay in run_demo.py — only the mechanics move) — no change to
   `make demo`'s printed output, timing, or behavior.

### `eval/ground_truth_queries.py` (new)

7. One function per golden-question `id`, dispatched via a
   `GROUND_TRUTH_FUNCS: dict[str, Callable[[psycopg.Connection, dict | None], dict]]`
   mapping, each issuing fresh independent SQL with the window the question
   itself concerns (not whatever window the agent's own tool calls used):
   - `aggregation`: `query_stats(conn, window_minutes=5, group_by="method",
     metric="count")` + `query_stats(conn, window_minutes=5,
     group_by="gateway", metric="count")`.
   - `gateway_rate`: `query_stats(conn, window_minutes=3, group_by="gateway",
     metric="failure_rate")`.
   - `fraud_pattern`: direct SQL — the 12 most recent successful card
     transactions (`transaction_id, card_bin, amount, event_timestamp`,
     `method='card' AND status='success'`, newest-first, `LIMIT 12`), plus
     which rows match `incident_context["card_bin"]` and `amount < 5.00`.
   - `novel_error`: direct SQL — the 15 most recent failures in the last 3
     minutes (`transaction_id, error_text, event_timestamp`,
     `status='failure'`, newest-first, `LIMIT 15`), plus which rows contain
     `NOVEL_ERROR_SIGNATURE`.
   - `freshness`: `consumer.freshness.query_freshness(window="5 minutes")`
     called directly.
   - `hallucination_control`: parameterized `SELECT count(*) FROM
     transactions WHERE method = %(method)s` with `method='crypto'`.
   Every query is parameterized (no f-string SQL values), matching the
   existing codebase's injection-safety convention.

### `eval/run_eval.py` (new)

8. CLI: `python -m eval.run_eval [--repeats N] [--questions id1,id2,...]`.
   `--repeats` defaults to 3, must be a positive int. `--questions` defaults
   to all 6 ids in `golden_questions.json`'s file order. `make eval-run`
   invokes it with defaults.
9. Preflight (lightweight, unlike 18A's cold-start): one `system_freshness`
   call via a fresh `MCPBridge()`; if `event_count == 0`, fail loudly with a
   message telling the user to start the stack first (`make up` +
   `make produce` + `make consume`, or `make demo`) — do not auto-launch
   producer/consumer subprocesses here (that's 18A's job, out of scope to
   duplicate).
10. For each selected question, for each repeat (`1..N`):
    a. **Reset to a known state**: call `clear_incident()`, then
       `POST_CLEAR_PAUSE_SECONDS`. No extra fixed baseline wait for
       non-incident questions beyond this — there is no ramp/lag to wait
       out for a question with no injected incident, only "no incident
       bleeding in from the previous question/repeat," which the clear +
       short pause already ensures.
    b. If `incident_context` is not `null`: record `inject_started_at`
       (wall clock, before injecting), map `incident_context` onto the
       matching `producer.inject` CLI args, call `inject(...)`, then gate
       via the matching `demo.incident_control` predicate + `poll_until`
       (same 5s/90s cadence as 18A). For `gateway_degradation` and
       `novel_error_pattern` specifically, sleep `POST_GATE_BUFFER_SECONDS`
       after the gate passes, before asking (matching 18A/18B's proven
       timing — see Context). `fraud_burst` asks immediately once gated (no
       extra buffer, matching 18A).
    c. Ask the question: fresh `bridge = MCPBridge()`, fresh
       `tools = bridge.list_tools()`, fresh `messages = []`; time the call
       (`time.monotonic()` before/after); call
       `run_turn(messages, question_text, bridge, tools)`.
    d. **Snapshot ground truth immediately after asking** (see Context/
       concept below for why "after asking," not later): (i) if an incident
       was injected, read `eval/ground_truth/incidents.jsonl` and keep
       records with `timestamp >= inject_started_at` matching this
       incident's type/target; empty list for non-incident questions. (ii)
       call the matching `eval/ground_truth_queries.py` function against a
       fresh `consumer.db.connect()` connection (open/close per run — same
       pattern `consumer/freshness.py` and the MCP tool delegates use, not
       a long-lived shared connection).
    e. Capture and append one run record (see Inputs/Outputs) to the
       in-memory results list.
    f. If an incident was injected this repeat, `clear_incident()` then
       `POST_CLEAR_PAUSE_SECONDS` before the next repeat/question — "between
       questions" from the sub-commit description applies equally between
       repeats of the same incident-tagged question, since each repeat is
       an independent trial and must not inherit the previous repeat's
       still-active incident.
    g. Print progress per run: e.g. `[3/18] gateway_rate repeat 2/3 ... done
       (12.4s)`.
11. Write the full results (see Inputs/Outputs) to
    `eval/results/run_<UTC timestamp, e.g. 20260714_133000>.json` after all
    runs complete. Print the output path and a one-line count summary
    (e.g. "18 runs captured across 6 questions").

## Inputs / Outputs

- CLI as above; no other inputs.
- Output file `eval/results/run_<timestamp>.json`, one JSON object:
  ```
  {
    "run_started_at": "<iso>",
    "repeats_per_question": 3,
    "golden_questions_source": "demo/golden_questions.json",
    "runs": [
      {
        "question_id": "gateway_rate",
        "question_text": "...",
        "repeat_index": 1,
        "incident_context": {...} or null,
        "asked_at": "<iso>",
        "wall_clock_seconds": 12.4,
        "final_turn_usage": {"input_tokens": N, "output_tokens": N} or null,
        "answer_text": "...",
        "tool_calls": [{"name": "...", "arguments": {...}, "result_text": "..."}],
        "transcript": {"messages": [...]},
        "ground_truth": {
          "captured_at": "<iso>",
          "incident_records": [...],
          "sql": {...}
        }
      },
      ...
    ]
  }
  ```
- `final_turn_usage` is documented (code comment) as covering only the last
  API call `run_turn` made for that question — if the model chained
  multiple tool calls, earlier iterations' usage isn't separately exposed
  by `run_turn` as written; this is a known, acceptable limitation, not a
  bug to fix here (`agent/loop.py` is out of scope for this step).
- `tool_calls` is a convenience flattening of `transcript.messages` (name +
  arguments + result text per tool_use/tool_result pair) — `transcript` is
  still the full source of truth; `tool_calls` just saves 19B from having
  to walk raw SDK block dicts for routing checks.

## Edge cases & errors

- No events flowing at preflight: fail loudly, don't proceed (see Behavior
  #9).
- An incident-visibility gate exceeds its bounded max wait: warn (existing
  `poll_until` behavior) and proceed to ask anyway — the resulting run is
  still captured, just possibly with a weaker/absent incident signal; this
  is itself useful signal for 19B/20, not a reason to abort the whole eval
  run.
- `eval/ground_truth/incidents.jsonl` missing or unreadable at snapshot
  time: `incident_records` is `[]` for that run rather than raising —
  ground-truth SQL capture (the more important half) still proceeds.
- A `run_turn` call raises (e.g. a transient Anthropic API error): let it
  propagate and abort the whole `eval-run` invocation rather than silently
  skipping a run and writing a partial/misleading results file — 19A has no
  retry/partial-write logic; a failed run should be visibly failed, not
  quietly absent from the output.
- Running `eval-run` twice: each run writes its own timestamped file, no
  collision, no shared state to reset beyond what step (a) already clears
  between questions.

## Out of scope

- Any scoring/grading/pass-fail logic (Step 19B, next sub-commit).
- The aggregated metrics report/markdown table (Step 20).
- Modifying `agent/loop.py`, `agent/mcp_bridge.py`, `mcp_server/*`,
  `producer/scenarios.py`, `producer/inject.py`, or
  `demo/golden_questions.json`.
- Cold-starting containers/producer/consumer (18A's job) — 19A's preflight
  only checks and fails loudly, it doesn't launch anything.
- The README (Step 21), the demo video (Step 22).

## Acceptance criteria

- [ ] `demo/incident_control.py` exists with the extracted, parameterized
      helpers; `demo/run_demo.py` calls into it and `make demo`'s behavior
      is unchanged (verified live as part of this step's own eval-run,
      which exercises the same gating code path).
- [ ] `eval/ground_truth_queries.py` exists with all 6 dispatch functions,
      each issuing independent, parameterized SQL against its own
      connection.
- [ ] `eval/run_eval.py` (+ `eval/__init__.py`) exists; `python -m
      eval.run_eval` and `make eval-run` both launch it.
- [ ] Running the full golden set with repeats produces
      `eval/results/run_<timestamp>.json` containing, per run: question
      id/text, incident context, full transcript, wall-clock timing, token
      usage (last-turn), and independently captured ground truth
      (incidents.jsonl snapshot + fresh SQL).
- [ ] No scoring/pass-fail anywhere in the output — capture only.
- [ ] `eval/results/*.json` added to `.gitignore`.
- [ ] Explain-back: why ground truth must be captured at ask-time via our
      own SQL, not the agent's own tool outputs or a later re-query —
      delivered to the user after verification.
- [ ] Stop here for commit (per process rules) — do not start 19B
      (`eval/grade.py`) until the user has reviewed and committed 19A.
