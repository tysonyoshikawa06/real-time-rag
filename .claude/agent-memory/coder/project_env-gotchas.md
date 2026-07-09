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
