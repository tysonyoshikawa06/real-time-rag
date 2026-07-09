---
name: setup-facts
description: Confirmed working test-runner setup/commands for real-time-rag on this Windows machine
metadata:
  type: project
---

- `uv run pytest tests/test_<feature>.py -q` from repo root works directly (no manual venv activation needed) — `uv run` handles the environment.
- Docker containers (`pgweb`, `kafka`, `postgres`) are commonly already running persistently on this machine (checked via `docker ps`); Postgres maps to host port 5433. Check `docker ps` before assuming containers need to be started with `docker compose up`.
- The `graphify` CLI is NOT on PATH in the Bash tool's shell (`graphify: command not found`), even though `graphify-out/` (graph.json, GRAPH_REPORT.md, etc.) exists in the repo. If graph queries are needed, read `graphify-out/GRAPH_REPORT.md` directly instead, or note this limitation.
- Test files for this project follow one-file-per-feature under `tests/`, e.g. `tests/test_mcp_semantic_search.py`, `tests/test_mcp_query_stats.py`.
