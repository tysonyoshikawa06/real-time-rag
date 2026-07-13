"""CLI entry point: ask the streaming-rag agent a single question.

Usage:
    python -m agent.ask "How many transactions in the last 10 minutes, by method?"

Runs the question through agent.loop.run_loop end-to-end (including any tool
calls the model makes along the way) and prints the final answer text. No
conversation memory across invocations - each call is a fresh question.
"""

import argparse

from agent.loop import run_loop


def main():
    parser = argparse.ArgumentParser(description="Ask the streaming-rag agent a question")
    parser.add_argument("question", help="The question to ask, in quotes")
    args = parser.parse_args()

    answer = run_loop(args.question)
    print("\n=== answer ===")
    print(answer)


if __name__ == "__main__":
    main()
