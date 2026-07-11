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
