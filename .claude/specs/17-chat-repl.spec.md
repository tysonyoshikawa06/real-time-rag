# Step 17 — Agent: interactive CLI chat with multi-turn memory

## Feature

Replace one-shot `python -m agent.ask "question"` with an interactive REPL
(`python -m agent.chat`, `make chat`) that keeps one persistent message
history for the whole session, so follow-up questions resolve against prior
turns. Show tool calls live as they happen (via `rich`), support a minimal
set of slash commands, and surface a running context-size counter. No new
retrieval or prompt logic — this step is purely about session structure
around the existing loop.

## Context

- `agent/loop.py:66` — `run_loop(question: str) -> str`, the Step 15/16
  function. Currently builds `messages = [{"role": "user", "content":
  question}]` fresh every call (`agent/loop.py:79`) and calls
  `_client.messages.create(model=MODEL, max_tokens=MAX_TOKENS,
  system=_SYSTEM_PROMPT, tools=tools, messages=messages)` in a loop
  (`agent/loop.py:83-89`), returning on `stop_reason != "tool_use"`
  (`agent/loop.py:91-92`) or the iteration cap (`agent/loop.py:102-107`).
  This function must be reused, not reimplemented, for the chat — refactored
  minimally so a caller can pass in (and get back) a persistent `messages`
  list instead of the function owning a fresh one per call.
- `agent/loop.py:22-25` — module constants `MODEL`, `MAX_ITERATIONS`,
  `MAX_TOKENS`, `_RESULT_PREVIEW_CHARS`. `_SYSTEM_PROMPT` loaded at
  `agent/loop.py:33` from `agent/system_prompt.md`. Reused as-is; `/clear`
  resets a session's `messages` back to reflect this same starting point
  (there is no system message *in* `messages` today — `system` is a separate
  API parameter passed every call, not a `messages` entry — so "reset to
  just the system prompt" means "reset `messages` to `[]`," since the system
  prompt is carried outside `messages` already and doesn't need re-adding).
- `agent/loop.py:36-63` — `_extract_text` and `_run_tool_calls`, the
  print-and-execute helpers. `_run_tool_calls` currently does its own
  printing with plain `print(...)` (`agent/loop.py:56,61`) — Step 17 moves
  to `rich` for the chat's display, but should not break `agent/ask.py`
  which still uses the plain-text path today (spec's Out-of-scope: don't
  redesign Step 15/16 output, only what multi-turn requires).
- `agent/ask.py` — existing one-shot entry point. Must keep working
  unchanged (`python -m agent.ask "question"` still answers one question and
  exits) — Step 17 adds `agent/chat.py` alongside it, doesn't replace it.
- `agent/mcp_bridge.py` (`MCPBridge`) — unchanged this step.
- `pyproject.toml` `[project].dependencies` — has `anthropic`, `fastmcp`,
  etc., no `rich` yet; add it.
- `Makefile` — no `chat` or `ask` target exists yet (`agent.ask` has always
  been run directly via `uv run python -m agent.ask "..."`). Add a `chat`
  target following the existing style (e.g. `mcp:`/`mcp-dev:` at the end of
  `Makefile`): `uv run python -m agent.chat`.
- Anthropic SDK responses carry token usage on `response.usage` (e.g.
  `.input_tokens`, `.output_tokens`) — the running context counter accumulates
  `input_tokens` across turns as a proxy for total resent history size, per
  the spec conversation's instruction ("the SDK response includes token
  usage — accumulate input tokens as a proxy").

## Behavior

1. `agent/loop.py` refactor (minimal): change `run_loop` (or introduce a
   thin variant used by both `ask.py` and `chat.py`) so the caller supplies
   the `messages` list and gets back the updated list plus the final answer
   text (and, for the chat's context counter, the last response's usage
   info). The exact function boundary is the implementer's call, but:
   - `agent/ask.py` keeps working with no interface change and effectively
     still does "one question, fresh history, print the answer, exit."
   - `agent/chat.py` reuses the same underlying loop machinery (the
     `messages.create` call, `stop_reason` branching, tool execution via
     `MCPBridge`) — it must not duplicate that logic in a second
     implementation of the tool-use loop.
   - `_run_tool_calls`'s printing behavior can be parameterized or the chat
     can do its own `rich`-based tool-call display around the same
     execution helper — implementer's choice, but don't fork the tool
     execution logic itself (still routes through `MCPBridge.call_tool`
     exactly as today).
2. `agent/chat.py` — `python -m agent.chat` entry point:
   - Prints a short banner: project name + a one-line hint (e.g. "ask about
     the live stream; /help for commands").
   - Loop: prompt for input (`input(...)` — no Unix readline dependency,
     must work on plain Windows/PowerShell), run the tool-use loop against
     the session's persistent `messages`, print the answer, repeat.
   - One `messages` list lives for the whole process lifetime. Each question
     appends a user turn; each answer's assistant/tool_use/tool_result turns
     also stay in `messages` (this is exactly what makes "and what about
     ACH?" resolve — the model sees the earlier turns on the next call).
3. Live tool-call display via `rich`: as each tool call happens, print a
   compact line, e.g. `-> query_stats(window_minutes=30, group_by="gateway")`,
   styled (dim/colored) so it reads visually as machinery distinct from the
   final answer. The final answer is rendered via `rich`'s Markdown
   rendering (the model may emit tables/lists in its answer text).
4. Slash commands, checked before treating input as a question:
   - `/help` — list the available commands.
   - `/clear` — reset `messages` to a fresh empty history (system prompt is
     still passed via the `system` API parameter as always, so nothing
     further needs re-adding) and reset the running context-token counter to
     0. Prints a short confirmation. Does not exit the process.
   - `/quit` or `/exit` — clean exit (return/`sys.exit(0)`, no traceback).
   - Blank input is ignored (re-prompt), not sent as a question.
   - `Ctrl-C` (`KeyboardInterrupt`) at the input prompt exits cleanly (caught,
     no stack trace), same end state as `/quit`.
5. Context-size visibility (no summarization/trimming logic — visibility
   only):
   - Track a running total by accumulating `response.usage.input_tokens`
     (or equivalent) across turns in the session.
   - Surface it subtly after each answer or in the next prompt, e.g.
     `[ctx ~12k tokens]`.
   - Once the running total crosses a threshold (e.g. 50,000), print a
     gentle one-line suggestion to run `/clear` — just a printed hint, not
     an automatic action.
   - `/clear` resets this counter to 0 along with `messages`.

## Inputs / Outputs

- `python -m agent.chat`: no CLI args. Interactive stdin/stdout loop.
- `make chat`: new Makefile target, runs the above via `uv run`.
- Slash commands take no arguments beyond the command word itself
  (`/help`, `/clear`, `/quit`, `/exit`).
- Per-turn display: a tool-call line per `tool_use` block (name + input
  args), then the final answer rendered as Markdown, then the context
  counter.

## Edge cases & errors

- Empty/whitespace-only input at the prompt: re-prompt, don't send a blank
  question through the loop.
- `Ctrl-C` during a tool call in progress or at the input prompt: exits
  cleanly either way, no traceback surfaced to the terminal.
- `/clear` immediately followed by a question that references prior context
  (e.g. "what did I just ask you?") must NOT resolve it — proof the history
  was actually dropped, not just visually cleared.
- Hitting `MAX_ITERATIONS` mid-conversation: same behavior as Step
  15/16 (report the cap was hit, best-effort text) — this doesn't end the
  chat session, just that one turn's answer.
- `rich` rendering must degrade sensibly in a plain Windows terminal (no
  crash if the terminal doesn't support certain styling) — `rich` handles
  this itself when used normally (`rich.console.Console`), no special-casing
  needed beyond using it as intended.

## Out of scope

- Streaming token-by-token output.
- Persisting history to disk / across separate `agent.chat` invocations.
- Retries or model fallbacks.
- Any web UI.
- Summarization or trimming of context — the counter is visibility only,
  never automatic truncation.
- Demo scripting (Step 18).
- Changing `agent/ask.py`'s existing one-shot interface or behavior.
- Changing `agent/system_prompt.md`, `agent/mcp_bridge.py`, or any MCP tool.

## Acceptance criteria

- [ ] `rich` added to `pyproject.toml` dependencies.
- [ ] `agent/loop.py` refactored minimally so the tool-use loop can run
      against a caller-supplied, persistent `messages` list; `agent/ask.py`
      continues to work unchanged from the user's point of view.
- [ ] `agent/chat.py` exists; `python -m agent.chat` and `make chat` both
      launch it.
- [ ] Banner prints on startup; prompt loop runs; tool calls render as
      compact `rich`-styled lines distinct from the Markdown-rendered final
      answer.
- [ ] One `messages` list persists for the whole session; a follow-up
      question resolves pronouns/implied filters from prior turns without
      restating them, and triggers a fresh tool call (not stale reuse of an
      old result).
- [ ] `/help`, `/clear`, `/quit`, `/exit` all work as specified; `/clear`
      provably drops prior context (a "what did I just ask" style follow-up
      fails to resolve after it).
- [ ] `Ctrl-C` and `/quit` both exit without a stack trace.
- [ ] A running context-token counter is visible and increases turn over
      turn; a gentle `/clear` suggestion appears once it crosses the chosen
      threshold.
- [ ] Live verification (stack + producer + consumer running): ask "how many
      transactions in the last 15 minutes by method," then the follow-up
      "and what's the failure rate for just card?" resolves "card" and the
      implied window from context; inject `gateway_degradation` and confirm
      the Step 16 incident behavior (baseline-vs-recent comparison, cited
      IDs) works identically inside the chat.
- [ ] PROJECT_STATE.md updated after this step: Step 17 checked off, status
      moved to Step 18, verification evidence recorded, the
      stateless-memory-is-a-resent-list concept (and its quadratic-cost
      implication) explained to the user.
