# Step 15 — Agent: bare tool-use loop (Anthropic API ↔ MCP tools)

## Feature

Replace the human MCP-inspector driver with a model: an `agent/` package that
(a) bridges to the MCP server as a real client and converts its tool list into
Anthropic tool definitions, and (b) runs a tool-use loop that sends a question
to a Sonnet-class model, executes whatever tool calls it returns via the
bridge, feeds results back, and repeats until the model answers or an
iteration cap is hit. Two sub-commits: 15A (bridge only, stop for commit),
15B (loop + CLI entry point).

## Context

- `mcp_server/server.py:19` — `mcp = FastMCP("streaming-rag")`, the server
  object to connect to in-process. Four tools registered on it:
  `query_stats` (`mcp_server/server.py:27-54`), `semantic_search`
  (`mcp_server/server.py:57-95`), `get_transactions`
  (`mcp_server/server.py:98-141`), `system_freshness`
  (`mcp_server/server.py:144-165`). Each tool's docstring is the routing
  logic the model reads — must pass through to the Anthropic tool
  `description` field intact, unedited.
- `fastmcp.Client` (installed: fastmcp 3.4.4) accepts a `FastMCP` server
  instance directly as its `transport` arg for an in-process connection (no
  subprocess, no stdio pipe) — confirmed via
  `inspect.signature(Client.__init__)`. This is the client to use; it talks
  the same MCP protocol a stdio client would, just without the process
  boundary. `client.list_tools()` returns `mcp.types.Tool` objects with
  `.name`, `.description`, `.inputSchema` (already JSON Schema — this *is*
  the Anthropic `input_schema`, no conversion logic needed beyond field
  renaming). `client.call_tool(name, arguments)` returns a `CallToolResult`
  with `.content` (list of content blocks, e.g. `TextContent` with `.text`)
  and `.is_error` (bool) — MCP surfaces tool exceptions (e.g. `ValueError`
  from `mcp_server/validation.py`) as an error-flagged result, not a raised
  Python exception, so the bridge does not need its own try/except around
  the call to satisfy "errors become content."
- `mcp_server/validation.py` — source of the rejection errors used to prove
  error-as-content behavior (e.g. `check_enum` rejecting `group_by="banana"`
  on `query_stats`).
- No `agent/` package exists yet. New files: `agent/__init__.py`,
  `agent/mcp_bridge.py` (15A), `agent/loop.py`, `agent/ask.py` (15B).
- `.env` already has `ANTHROPIC_API_KEY=` (empty, user fills in) and is
  loaded via `python-dotenv` elsewhere in the repo (see `consumer/config.py`
  pattern) — reuse that pattern, don't invent a new env-loading mechanism.
- `pyproject.toml` — dependency list at `[project].dependencies`; `anthropic`
  is not yet present and must be added (15A, since the bridge's smoke test
  only needs `fastmcp`, but 15A's tool-definition shape is Anthropic's, so
  add the dependency now rather than deferring to 15B).

## Behavior

### 15A — `agent/mcp_bridge.py`

1. Given the `mcp_server.server.mcp` `FastMCP` instance, connect via
   `fastmcp.Client(mcp)` using an async context manager (FastMCP's client API
   is async-only); the bridge exposes sync-friendly wrapper functions/methods
   that internally run the async calls (e.g. via `asyncio.run` or an async
   bridge class used with `asyncio.run` at call sites) — implementer's choice
   of exact sync/async shape, but the smoke entry point and 15B's loop must
   be able to call it without themselves juggling an event loop per call in
   a way that reopens the connection every time (reuse one client session
   per bridge instance/process).
2. `list_tools()` (bridge method) returns a list of Anthropic tool-def dicts:
   `{"name": ..., "description": ..., "input_schema": ...}`, one per MCP
   tool, derived mechanically from `client.list_tools()` — no hand-written
   duplicate schemas.
3. `call_tool(name, arguments)` (bridge method) invokes the MCP tool via
   `client.call_tool(name, arguments)` and returns a string/JSON-serializable
   payload suitable for a `tool_result` content block. This applies whether
   `is_error` is `True` or `False` — both paths return content, neither
   raises.
4. Given a tool call whose arguments fail `mcp_server/validation.py` checks
   (e.g. `query_stats(group_by="banana")`), `call_tool` returns the
   validation error message as readable string content, not a raised
   exception — proving the "actionable-error, not crash" design from Step 14
   survives the MCP hop.
5. A smoke entry point (`python -m agent.mcp_bridge` or `if __name__ ==
   "__main__"` block) that: lists all four tools with their converted
   Anthropic-shaped definitions (name, description, input_schema) printed
   legibly; calls `system_freshness` through the bridge and prints the
   result; calls `query_stats(group_by="banana")` through the bridge and
   prints the returned error content (proving point 4).

### 15B — `agent/loop.py` + `agent/ask.py` (build only after 15A is committed)

6. `agent/loop.py` implements the message loop per the pseudocode in the
   spec conversation: seed `messages` with a single user message (the
   question), loop up to `MAX_ITERATIONS` (default 10) calling
   `anthropic.Anthropic().messages.create(model=MODEL, max_tokens=...,
   tools=<bridge.list_tools()>, messages=messages)`.
7. If `response.stop_reason != "tool_use"`, return the model's final text
   (extracted from `response.content`) — loop ends, this is the answer.
8. Otherwise: append an assistant message containing `response.content`
   verbatim (all blocks, not just `tool_use` ones); execute every
   `tool_use` block in that response via the bridge (handle one or many in
   a single response); append one user message containing one `tool_result`
   block per `tool_use` block, each keyed by the matching `tool_use_id`, in
   the same order the model requested them.
9. Print each tool call as it happens: tool name + arguments before
   execution, then a truncated (e.g. first ~300 chars) result after.
10. If `MAX_ITERATIONS` is exhausted without `stop_reason != "tool_use"`,
    return a message noting the iteration cap was hit plus any best-effort
    final text extractable from the last response.
11. `agent/ask.py` — `python -m agent.ask "question"` runs one question
    through the loop end-to-end and prints the final answer text.

## Inputs / Outputs

- **Bridge `list_tools()`**: no input → `list[dict]`, each
  `{"name": str, "description": str, "input_schema": dict}`.
- **Bridge `call_tool(name: str, arguments: dict)`**: → `str` (or JSON string)
  payload representing tool output or tool error content.
- **`agent.ask` CLI**: one positional arg (the question, quoted) → stdout:
  interleaved tool-call trace lines followed by the final answer text.
- **`MODEL`**: a Sonnet-class model id (e.g. `claude-sonnet-5`), a module
  constant in `agent/loop.py`, not hardcoded inline at each call site.
- **`MAX_ITERATIONS`**: module constant in `agent/loop.py`, default `10`.

## Edge cases & errors

- MCP tool raises a validation error → bridge returns it as string content;
  loop packages it into a normal `tool_result` block (not marked as a
  content-level error the way Anthropic's own `is_error` tool_result field
  could optionally flag it — using that flag or not is an implementation
  choice, but either way the model must receive the error text and be able
  to read/react to it, never a crash).
- Model requests a tool name the bridge doesn't recognize — out of scope to
  specially handle beyond whatever the bridge naturally does (MCP client
  will raise/return an error for an unknown tool name; let that surface as
  tool_result content same as any other tool error, don't add bespoke
  handling).
- Multiple `tool_use` blocks in one response → every one gets a matching
  `tool_result`, order preserved, no dropped calls.
- Iteration cap hit → loop returns cleanly with a "hit iteration cap" message
  instead of raising or looping forever.
- Missing `ANTHROPIC_API_KEY` → whatever the `anthropic` SDK does by default
  (raises on client construction/first call) is acceptable; no custom
  handling required for this step.

## Out of scope

- No system prompt or citation discipline (Step 16).
- No interactive CLI chat loop across multiple questions (Step 17).
- No demo scripting (Step 18).
- No streaming output.
- No conversation memory across separate `agent.ask` invocations.
- No direct import of MCP tool functions into the agent — the bridge must be
  the only path from agent code to tool execution.
- No stdio subprocess transport (in-process `Client(mcp)` is the chosen
  transport per the design decision; do not add stdio unless the in-process
  approach proves infeasible, and if so, stop and flag it rather than
  silently switching).

## Acceptance criteria

15A (stop here for commit):
- [ ] `agent/mcp_bridge.py` exists, connects to `mcp_server.server.mcp` via
      `fastmcp.Client` in-process.
- [ ] `list_tools()` returns 4 correctly-named tools with intact docstrings
      as descriptions and valid JSON Schema `input_schema`.
- [ ] `call_tool("system_freshness", {})` returns a real freshness result
      through the MCP layer.
- [ ] `call_tool("query_stats", {"group_by": "banana"})` returns the
      validation rejection message as content, not a raised exception.
- [ ] Smoke entry point runs standalone and demonstrates all of the above via
      printed output.
- [ ] `anthropic` added to `pyproject.toml` dependencies.

15B (build only after 15A commit; do not start until told):
- [ ] `agent/loop.py` loop matches the pseudocode's message-role mechanics:
      assistant tool_use blocks appended as an assistant message, tool
      results appended as a user message with matching `tool_use_id`s.
- [ ] Handles multiple parallel tool_use blocks in one response.
- [ ] Hard iteration cap (default 10), no infinite loop possible.
- [ ] Prints each tool call (name + args) and truncated result as it happens.
- [ ] `python -m agent.ask "question"` works end-to-end against the live
      stack for the three verification questions in the Step 15 spec
      (transaction count by method, unusual-errors semantic search,
      gateway_degradation detection).
- [ ] PROJECT_STATE.md updated after 15B: Step 15 checked off, status moved
      to Step 16, message-role and stop_reason mechanics explained back to
      the user, misroutings/non-cited-claims noted as expected pre-Step-16
      behavior.
