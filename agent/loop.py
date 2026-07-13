"""Bare tool-use loop against the raw Anthropic SDK.

Sends a question to a Sonnet-class model, executes whatever tool_use blocks
it returns via the MCP bridge, feeds the results back, and repeats until the
model ends its turn or MAX_ITERATIONS is hit. Step 16 adds a system prompt
(grounding/citation/routing discipline) carried via the API's `system`
parameter; the loop mechanics themselves are unchanged from Step 15.

Step 17 splits "run one turn" (`run_turn`) from "who owns the messages list":
`run_turn` takes a caller-supplied, persistent `messages` list and mutates it
in place (appending the new question plus every assistant/tool_result turn
generated along the way), so a REPL (agent/chat.py) can keep one growing
history across many questions. `run_loop` is now a thin one-shot wrapper
around `run_turn` with a throwaway messages list, kept so agent/ask.py's
interface (and every Step 15/16 behavior) is unchanged.
"""

import os
from collections.abc import Callable
from pathlib import Path

import anthropic
from dotenv import dotenv_values

from agent.mcp_bridge import MCPBridge

# Same .env-loading pattern as consumer/config.py: prefer a real environment
# variable, fall back to the .env file's value.
_env = dotenv_values(".env")

MODEL = "claude-sonnet-5"
MAX_ITERATIONS = 10
MAX_TOKENS = 1024
_RESULT_PREVIEW_CHARS = 300

_client = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY", _env.get("ANTHROPIC_API_KEY"))
)

# Loaded once at import time and reused across every run_loop call, same
# "load once" pattern as _client/_env above - never re-read per question.
_SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text(encoding="utf-8")


def _extract_text(content: list) -> str:
    """Join the text blocks in a response's content list into one string.

    A response can mix TextBlocks with other block types (e.g. tool_use), so
    this pulls out just the parts that read as an answer to a human.
    """
    return "\n".join(block.text for block in content if block.type == "text")


def _run_tool_calls(
    bridge: MCPBridge,
    content: list,
    on_call: Callable[[str, dict], None] | None = None,
    on_result: Callable[[str], None] | None = None,
) -> list[dict]:
    """Execute every tool_use block in a response, announcing each call and result.

    `on_call`/`on_result` let a caller (e.g. the chat REPL) render calls with
    `rich` instead of the plain `print` used here by default - the execution
    path (bridge.call_tool) and the returned tool_result shape are identical
    either way, so this is display-only branching, not a second tool-use
    implementation. Returns one tool_result dict per tool_use block, in the
    same order the model requested them, so the caller can hand them back as
    a single user message keyed by tool_use_id.
    """
    results = []
    for block in content:
        if block.type != "tool_use":
            continue
        if on_call is not None:
            on_call(block.name, block.input)
        else:
            print(f"  [tool call] {block.name}({block.input})")
        result_text = bridge.call_tool(block.name, block.input)
        preview = result_text[:_RESULT_PREVIEW_CHARS]
        if len(result_text) > _RESULT_PREVIEW_CHARS:
            preview += "..."
        if on_result is not None:
            on_result(preview)
        else:
            print(f"  [tool result] {preview}")
        results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})
    return results


def run_turn(
    messages: list,
    question: str,
    bridge: MCPBridge,
    tools: list[dict],
    on_call: Callable[[str, dict], None] | None = None,
    on_result: Callable[[str], None] | None = None,
) -> tuple[str, "anthropic.types.Message | None"]:
    """Run one question through the tool-use loop against a persistent messages list.

    Mutates `messages` in place - appends the user question, then every
    assistant/tool_result turn generated while resolving it - so a caller
    that keeps reusing the same list across calls (agent/chat.py) gets real
    multi-turn memory: the model sees earlier turns because they're still
    sitting in `messages`, not because anything is stored server-side (the
    Anthropic API is stateless per call).

    Returns (answer_text, last_response) - the response is exposed so a
    caller can read `.usage.input_tokens` for a context-size counter;
    `run_loop` below ignores it.
    """
    messages.append({"role": "user", "content": question})

    response = None
    for _ in range(MAX_ITERATIONS):
        response = _client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return _extract_text(response.content), response

        # Append the assistant turn verbatim (all blocks, not just tool_use -
        # the model may think out loud in text alongside the calls) so the
        # history the model sees next matches what it actually said.
        messages.append({"role": "assistant", "content": response.content})

        tool_results = _run_tool_calls(
            bridge, response.content, on_call=on_call, on_result=on_result
        )
        messages.append({"role": "user", "content": tool_results})

    # Iteration cap exhausted without the model ending its turn: report that
    # plainly instead of looping forever, plus whatever text (if any) came
    # back on the last turn.
    fallback_text = _extract_text(response.content) if response is not None else ""
    cap_msg = f"[hit iteration cap of {MAX_ITERATIONS} without a final answer]"
    answer = f"{cap_msg}\n{fallback_text}" if fallback_text else cap_msg
    return answer, response


def run_loop(question: str) -> str:
    """Run one question end-to-end with a fresh, throwaway history and return the answer.

    Thin one-shot wrapper around `run_turn` - kept so agent/ask.py's
    interface and behavior are unchanged from Step 15/16. Each call gets its
    own bridge, tool list, and empty `messages`, so there is no memory across
    separate `run_loop` calls (that's what agent/chat.py's persistent
    `messages` list is for).
    """
    bridge = MCPBridge()
    tools = bridge.list_tools()
    messages: list = []
    answer, _ = run_turn(messages, question, bridge, tools)
    return answer
