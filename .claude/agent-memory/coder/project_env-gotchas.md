---
name: env-gotchas
description: Environment quirks for the coder agent in this repo (graphify CLI missing, DB port, uv-managed deps)
metadata:
  type: project
---

- `graphify` CLI is not on PATH in the Bash tool (bash or PowerShell) despite
  CLAUDE.md instructing graph-first + `graphify update .`. Query attempts fail
  with "command not found" — go straight to Glob/Grep/Read and mention the
  skipped graph update in the report.
- Local Postgres for smoke checks: localhost:5433, db `streaming_rag`, user
  `rag`; `consumer.db.connect()` reads this from `consumer/config.py`.
- Dependencies are uv-managed: add with `uv add <pkg>`, never hand-edit
  `uv.lock`. Run everything through `uv run`.

**Why:** wasted a call discovering graphify is missing; specs assume uv.
**How to apply:** at task start, skip graphify probing unless it reappears;
use `uv add`/`uv run` for all Python work here.

- The `mcp_server` test suite (4 files: query_stats, semantic_search,
  get_transactions, system_freshness) takes ~20+ minutes the *first* run in a
  session because `semantic_search`'s tests load the real sentence-transformers
  embedding model — subsequent reruns are fast (~25-30s, model stays cached in
  the process/session). Don't assume a hang; redirect output to a file with a
  trailing sentinel line (e.g. `... ; echo DONE >> out.txt`) via
  `run_in_background: true`, then wait for the actual completion notification
  rather than polling with short sleeps (short-sleep polling loops get
  blocked by the harness anyway). Issuing a *second* real long-running Bash
  command while one is still in flight appears to kill the first one — don't
  re-issue the same pytest command to "check progress"; wait for the
  notification instead.
- On this Windows machine, Docker Desktop is installed but not auto-started —
  `docker ps` fails with a dockerDesktopLinuxEngine pipe error if it isn't
  running, and `make up` / `docker compose` will not work until the daemon is
  up. Fix: `powershell -Command "Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'"`,
  then poll `docker ps` (e.g. `until docker ps >/dev/null 2>&1; do sleep 5; done`)
  until it succeeds (~1-2 min cold start) before running `make up`.
- `fastmcp.Client.call_tool(name, args)` defaults to `raise_on_error=True`
  (confirmed fastmcp 3.4.4) — it raises a client-side `ToolError` on a tool's
  `ValueError`/exception rather than returning a `CallToolResult` with
  `is_error=True`. Pass `raise_on_error=False` explicitly to get the
  content-not-exception behavior (uniform `CallToolResult.content`/`.is_error`
  for both success and tool-level failure) — needed anywhere the codebase
  wants MCP tool errors to come back as text instead of a raised exception
  (see `agent/mcp_bridge.py`).
- Printing existing repo docstrings that contain em-dashes (e.g.
  `mcp_server/server.py`) to this Windows console renders as `�` — that's a
  cp1252 console *display* artifact only, not file corruption (verified via
  raw byte read: the em-dash is valid UTF-8 `\xe2\x80\x94` on disk). Don't
  mistake it for a bug in files you didn't touch. Separately, the project
  style calls for ASCII-only in new code, so avoid typing em-dashes in new
  files in the first place (use ` - ` instead) rather than relying on this
  being harmless. Note this affects *model-generated* answer text too (e.g.
  bullets/em-dashes in `agent/chat.py` answers) — that's the LLM's own output
  encoding, not something to "fix" in our code.
- `rich.console.Console.print(f"...")` runs the string through Rich's markup
  parser by default. Any literal `[...]` in dynamic/interpolated content
  (e.g. a `[ctx ~12k tokens]` counter string, or JSON/list reprs with `[`) can
  be silently swallowed if it parses as an (unrecognized) style tag — no
  error, the bracketed text just vanishes from output. Wrap any
  interpolated/dynamic text with `rich.markup.escape(...)` before formatting
  it into a `console.print(f"...")` call; verified this fixed a real bug in
  `agent/chat.py`'s context-counter and tool-call display lines (Step 17).
- Running a standalone verification script with `uv run python <script.py>`
  from the repo root does NOT put the repo root on `sys.path`, so
  `from agent.loop import ...`-style imports fail with `ModuleNotFoundError`
  even though `uv run pytest`/`uv run python -m agent.x` both work fine (the
  latter two get the cwd on sys.path via different mechanisms). Fix: prefix
  with `PYTHONPATH="<repo-root>"` when running a loose script that imports
  project packages.
- `agent/loop.py`'s `run_turn` uses `MAX_TOKENS = 1024` for the whole response
  (thinking + visible text combined) and this model ("claude-sonnet-5") uses
  extended thinking by default. Verified live (Step 18A demo build): asking a
  question that leads the model to pull a sizeable raw tool payload (e.g.
  `get_transactions(..., limit=30)` or `limit=100`) can make a later turn spend
  ~900+ of the 1024 tokens on the hidden `thinking` block, leaving too few (or
  zero) tokens for the visible answer — `stop_reason` comes back `"max_tokens"`
  (not `"tool_use"`), `run_turn` treats that as "done", and `_extract_text`
  returns whatever text happened to be emitted before the cutoff (sometimes
  empty, sometimes a sentence cut off mid-word). This isn't a crash and prints
  no error — it just silently produces a truncated/empty final answer, so it's
  easy to miss. Confirmed via a debug harness that called
  `L._client.messages.create(...)` directly and inspected
  `response.usage.output_tokens_details.thinking_tokens`. Since `agent/loop.py`
  is off-limits to edit in most feature work, the fix lives on the *caller*
  side: word prompts/golden questions that go through `run_loop`/`run_turn` to
  bias the model toward a small, bounded tool call (e.g. "look at the 10-15
  most recent X" rather than an open-ended "check recent X") rather than a
  large `limit=100` dump it has to reason over in one turn. Verified fix: the
  same investigation with `limit=10-15` reliably finished with `stop_reason
  == "end_turn"` and a complete, correctly cited answer.
- This environment can accumulate a large stale Kafka consumer-group backlog
  (observed ~580k messages/partition-lag) if a producer was ever left running
  without a matching consumer for a while. Symptom: a freshly started consumer
  logs continuous `[batch] wrote N events, total=...` (looks healthy) but
  `system_freshness`/`query_freshness` reports `event_count: 0` for a while,
  because the events it's furiously writing all have old `event_timestamp`s
  from the backlog and fall outside any recent window — freshness is windowed
  on event time, not ingest time. Diagnose with `MSYS_NO_PATHCONV=1 docker exec
  kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server
  localhost:9092 --describe --group rag-consumer` and check the `LAG` column;
  if it's large, just wait for it to drain (it catches up fast once started,
  but "fast" can still be a few minutes for a very large backlog) rather than
  assuming something is broken. (The `MSYS_NO_PATHCONV=1` prefix is needed in
  Git Bash so it doesn't mangle the in-container `/opt/kafka/...` path.)
