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
  being harmless.
