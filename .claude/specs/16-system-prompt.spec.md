# Step 16 — Agent: system prompt + citation discipline

## Feature

Add a versioned system prompt that turns the bare Step-15 tool-use loop into a
disciplined operations analyst: it must ground every factual claim in a tool
call made this conversation, cite counts/windows/transaction IDs, route
questions to the correct tool, and never present general payments knowledge as
a finding from our data. Wire it into the existing loop via the Anthropic API's
`system` parameter. No structural changes to the loop beyond what carrying the
prompt requires.

## Context

- `agent/loop.py:61` — `run_loop(question)`, the function this step touches.
  Currently calls `_client.messages.create(model=MODEL, max_tokens=MAX_TOKENS,
  tools=tools, messages=messages)` with no `system` argument
  (`agent/loop.py:78-83`).
- `agent/loop.py:21-23` — module constants `MODEL`, `MAX_ITERATIONS`,
  `MAX_TOKENS` — the natural place to add a loaded-prompt constant alongside.
- `agent/mcp_bridge.py` (`MCPBridge`) — unchanged this step; tool descriptions
  it surfaces (from `mcp_server/server.py`'s docstrings) already carry
  per-tool routing hints (e.g. `semantic_search`'s docstring at
  `mcp_server/server.py:64-83` already says "cite the transaction_id(s)") —
  the system prompt reinforces this as a conversation-wide rule rather than
  duplicating each tool's own routing text.
- `agent/ask.py` — CLI entry point; interface (`python -m agent.ask
  "question"`) must stay unchanged. It only calls `run_loop`, so no edits
  needed there unless `run_loop`'s signature changes (it should not need to).
- No `agent/system_prompt.md` or prompt-loading code exists yet.
- PROJECT_STATE.md's Step 15 entry (`PROJECT_STATE.md` "Current status"
  section, Step 15 bullets) is this step's recorded before-state: (1) the
  "unusual errors" question cited dollar amounts as evidence without printing
  backing `transaction_id`s despite the tool docstring instructing citation;
  (2) the gateway-degradation incident question did cite IDs. This
  inconsistency is the concrete regression example the prompt must fix.

## Behavior

1. `agent/system_prompt.md` is a standalone Markdown file (not an inline
   Python string) containing the full system prompt text. Loaded once at
   import time in `agent/loop.py` (read the file, store as a module-level
   string) — same "load once, reuse" pattern already used for `MCPBridge`'s
   tools and `LocalEmbedder` elsewhere in the repo, not re-read per call.
2. The prompt establishes the agent as a real-time payments operations
   analyst that answers only from retrieved tool data, and encodes at least:
   - **Grounding**: never answer a data question from memory/general
     knowledge; every factual claim about the stream must trace to a tool
     call made in this conversation; if a question needs data no tool has
     returned yet, call a tool rather than guess.
   - **Honest emptiness**: empty tool results are reported as an answer
     ("no matching transactions in the last N minutes"), never papered over
     with invented plausible-sounding findings; tool validation errors are
     either corrected and retried (adjust the call per the error message) or
     reported as a limitation — never silently dropped.
   - **Citation**: quantitative claims state their basis ("based on N
     transactions in the last M minutes"); claims about specific
     behavior/examples carry real, retrieved `transaction_id`s verbatim,
     never constructed; drill-down detail on cited rows goes through
     `get_transactions` with those IDs.
   - **Routing**: counting/rates/rankings/trends -> `query_stats`;
     meaning/similarity/novelty/"anything weird" -> `semantic_search`;
     specific rows/examples behind a number -> `get_transactions`; data
     currency -> `system_freshness`; incident-style questions ("is
     something wrong right now?") typically need a baseline-vs-recent
     comparison (two `query_stats` calls at different windows) plus a
     search/drill-down step to characterize what's wrong — multi-tool
     investigation is expected, the iteration cap is the budget, not a
     target to avoid.
   - **Style**: concise and operational, lead with the finding, state the
     time window examined, include a freshness note when relevant, no
     hedging filler — uncertainty is expressed as "the data shows / does not
     show," not vague hedges.
3. `run_loop` passes the loaded prompt via `system=<prompt text>` in the
   `messages.create(...)` call — never folded into a fake first user message.
4. `agent/ask.py`'s public interface (`python -m agent.ask "question"`, one
   positional arg, prints the final answer) is unchanged.
5. Any loop adjustment beyond adding the `system` argument must be minimal
   and justified (e.g. nothing structural: no new control-flow branches, no
   change to the tool-execution or message-role mechanics from Step 15).

## Inputs / Outputs

- `agent/system_prompt.md`: no inputs: a Markdown file read as plain text.
- `run_loop(question: str) -> str`: signature unchanged from Step 15; only
  its internal `messages.create` call gains a `system=` argument.
- CLI: `python -m agent.ask "question"` unchanged.

## Edge cases & errors

- `agent/system_prompt.md` missing/unreadable at import time -> let the
  resulting exception surface naturally (no silent fallback to an empty
  prompt) — this is a startup-time misconfiguration, not a runtime data
  edge case, so no special handling is required beyond what a normal
  `open()`/read failure already gives you.
- A question the prompt's rules make the model refuse to answer directly
  (e.g. asking about a payment method that doesn't exist) must still produce
  a tool call and an honest "no such data" style answer, not a refusal to
  engage — the grounding rule requires calling a tool and reporting what it
  returns, not stonewalling.
- The prompt must not block the model from ever using general knowledge
  transparently labeled as such (e.g. "typical card decline reasons" in
  general) — the rule is "don't present priors as findings from our data,"
  not "never use general knowledge language at all." The prompt should make
  this distinction explicit enough that the model can tell the two apart.

## Out of scope

- Interactive multi-turn CLI chat (Step 17).
- Demo scripting (Step 18).
- Retries, streaming output.
- Conversation memory across separate `agent.ask` invocations.
- Any change to `agent/mcp_bridge.py` or the MCP tool implementations
  themselves — this step is prompt-only plus the one-line wiring change.

## Acceptance criteria

- [ ] `agent/system_prompt.md` exists, is loaded once at import time in
      `agent/loop.py`, and is passed via the API's `system` parameter.
- [ ] `agent/ask.py`'s CLI interface is unchanged.
- [ ] Re-running the Step-15 "are there any unusual errors recently?"
      question (or an equivalent unusual-errors question) now cites
      transaction IDs for any specific-behavior claim, closing the
      inconsistency recorded in PROJECT_STATE.md's Step 15 entry.
- [ ] Hallucination probe ("How many crypto payments failed today?" or a
      merchant not in the data pool) results in a tool call, an empty/no-match
      result, and an honest "no such transactions found" answer — not
      invented numbers.
- [ ] Grounding probe ("What are typical card decline reasons?") results in
      either a tool call answering from our actual recent failure data, or an
      answer that explicitly separates general knowledge from what the stream
      shows — never presents general knowledge as a stream finding.
- [ ] Full incident drill: inject `gateway_degradation`, ask "Is anything
      wrong right now?" -> multi-tool behavior (baseline-vs-recent
      `query_stats` comparison plus a drill-down/search call), gateway
      identified, failure rate quantified, example transaction IDs cited,
      time window stated.
  - [ ] Spot-check answers routinely state the window examined and a basis
      line for quantitative claims.
- [ ] Before/after pairs (Step 15's uncited example vs. this step's re-run)
      recorded for the user as documentation evidence.
- [ ] PROJECT_STATE.md updated after this step: Step 16 checked off, status
      moved to Step 17, before/after evidence recorded, message explaining
      "system prompt as tested contract" and "no priors as grounding
      linchpin" concepts delivered to the user.
