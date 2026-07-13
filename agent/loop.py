"""Bare tool-use loop against the raw Anthropic SDK.

Sends a question to a Sonnet-class model, executes whatever tool_use blocks
it returns via the MCP bridge, feeds the results back, and repeats until the
model ends its turn or MAX_ITERATIONS is hit. Step 16 adds a system prompt
(grounding/citation/routing discipline) carried via the API's `system`
parameter; the loop mechanics themselves are unchanged from Step 15.
"""

import os
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


def _run_tool_calls(bridge: MCPBridge, content: list) -> list[dict]:
    """Execute every tool_use block in a response, printing each call and result.

    Returns one tool_result dict per tool_use block, in the same order the
    model requested them, so the caller can hand them back as a single user
    message keyed by tool_use_id.
    """
    results = []
    for block in content:
        if block.type != "tool_use":
            continue
        print(f"  [tool call] {block.name}({block.input})")
        result_text = bridge.call_tool(block.name, block.input)
        preview = result_text[:_RESULT_PREVIEW_CHARS]
        if len(result_text) > _RESULT_PREVIEW_CHARS:
            preview += "..."
        print(f"  [tool result] {preview}")
        results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})
    return results


def run_loop(question: str) -> str:
    """Run one question through the tool-use loop and return the final answer text.

    The Anthropic API is stateless per call, so the entire conversation so
    far (every prior assistant turn and tool_result) is resent on each
    iteration - the model only "remembers" earlier tool calls because they're
    still sitting in `messages`. `stop_reason` is the control-flow signal:
    "tool_use" means the model wants to call one or more tools before it can
    answer; anything else (normally "end_turn") means it's done and
    response.content holds the final answer.
    """
    bridge = MCPBridge()
    tools = bridge.list_tools()
    messages = [{"role": "user", "content": question}]

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
            return _extract_text(response.content)

        # Append the assistant turn verbatim (all blocks, not just tool_use -
        # the model may think out loud in text alongside the calls) so the
        # history the model sees next matches what it actually said.
        messages.append({"role": "assistant", "content": response.content})

        tool_results = _run_tool_calls(bridge, response.content)
        messages.append({"role": "user", "content": tool_results})

    # Iteration cap exhausted without the model ending its turn: report that
    # plainly instead of looping forever, plus whatever text (if any) came
    # back on the last turn.
    fallback_text = _extract_text(response.content) if response is not None else ""
    cap_msg = f"[hit iteration cap of {MAX_ITERATIONS} without a final answer]"
    return f"{cap_msg}\n{fallback_text}" if fallback_text else cap_msg
