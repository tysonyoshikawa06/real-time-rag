"""Interactive CLI chat: multi-turn REPL over the streaming-rag agent.

Unlike agent/ask.py (one question, fresh history, exit), this keeps ONE
`messages` list alive for the whole process lifetime and reuses it on every
turn via agent.loop.run_turn - that persistent list is what lets a follow-up
like "and what about ACH?" resolve against the prior turn instead of needing
to be restated. The Anthropic API itself is stateless per call; "memory" here
is nothing more than the growing `messages` list being resent every time,
which is also why context size (and cost) grows with conversation length -
see the running token counter below.

Usage:
    python -m agent.chat
"""

import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape

from agent.loop import run_turn
from agent.mcp_bridge import MCPBridge

_BANNER = (
    "streaming-rag chat - ask about the live transaction stream\n"
    "/help for commands, /quit or Ctrl-C to exit"
)

_HELP_TEXT = """Commands:
  /help   show this message
  /clear  reset conversation history and the context-token counter
  /quit   exit (/exit works too)"""

# Context counter is visibility-only (no auto-trimming/summarization) - once
# the running total of resent input tokens crosses this, nudge toward /clear.
_CONTEXT_WARN_THRESHOLD = 50_000


def _format_ctx(tokens: int) -> str:
    """Render the running context-token total compactly, e.g. '[ctx ~12k tokens]'."""
    if tokens < 1000:
        return f"[ctx ~{tokens} tokens]"
    return f"[ctx ~{tokens // 1000}k tokens]"


def main() -> None:
    console = Console()
    console.print(_BANNER)

    bridge = MCPBridge()
    tools = bridge.list_tools()
    messages: list = []
    context_tokens = 0

    def on_call(name: str, tool_input: dict) -> None:
        args = ", ".join(f"{k}={v!r}" for k, v in tool_input.items())
        console.print(f"[dim]-> {escape(name)}({escape(args)})[/dim]")

    def on_result(preview: str) -> None:
        console.print(f"[dim]<- {escape(preview)}[/dim]")

    while True:
        try:
            question = input("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\nbye")
            return

        if not question:
            continue

        if question in ("/quit", "/exit"):
            console.print("bye")
            return

        if question == "/help":
            console.print(_HELP_TEXT)
            continue

        if question == "/clear":
            messages = []
            context_tokens = 0
            console.print("[dim]conversation history cleared[/dim]")
            continue

        try:
            answer, response = run_turn(
                messages, question, bridge, tools, on_call=on_call, on_result=on_result
            )
        except KeyboardInterrupt:
            console.print("\nbye")
            return

        console.print(Markdown(answer))

        if response is not None and response.usage is not None:
            context_tokens += response.usage.input_tokens
        console.print(f"[dim]{escape(_format_ctx(context_tokens))}[/dim]")
        if context_tokens >= _CONTEXT_WARN_THRESHOLD:
            console.print(
                "[dim]context is getting large - consider /clear if follow-ups don't need "
                "earlier history[/dim]"
            )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
