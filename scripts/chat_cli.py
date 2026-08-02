"""Terminal REPL for the agent tree — Phase 2 without waiting on the UI.

    uv run python scripts/chat_cli.py                 # interactive
    uv run python scripts/chat_cli.py --scenarios     # run the PRD workflows

Type 'exit' to quit. '/debug' toggles showing which agent answered and which
tools it called.
"""

from __future__ import annotations

import argparse
import sys

from movie_connoisseur.agents import missing_credentials
from movie_connoisseur.chat import MovieChat

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The three workflows in section 5 of the PRD.
SCENARIOS = [
    "What are the top thriller movies available on Netflix India right now?",
    "I just finished watching Stree 2 on Hotstar. Give it 4 out of 5 stars and "
    "log it: 'Super funny, great performance by Rajkummar!'",
    "Give me a summary of my last 3 watched movies formatted nicely so I can "
    "text it to my friends.",
]


def show(turn, debug: bool) -> None:
    if debug:
        tools = ", ".join(f"{c.name}->{c.status or '?'}" for c in turn.tool_calls)
        print(f"\n[agent: {turn.agent or '?'}] [tools: {tools or 'none'}]")
    print(f"\n{turn.text}\n")
    for call in turn.failed_tools:
        print(f"  ! {call.name} failed: {call.error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", action="store_true", help="run the PRD workflows")
    parser.add_argument("--debug", action="store_true", help="show routing and tool calls")
    args = parser.parse_args()

    missing = missing_credentials()
    if missing:
        print("Missing credentials in .env: " + ", ".join(missing))
        return 1

    chat = MovieChat()
    debug = args.debug

    try:
        if args.scenarios:
            for i, prompt in enumerate(SCENARIOS, start=1):
                print(f"\n{'=' * 70}\nWORKFLOW {i}\n{'=' * 70}\nYou: {prompt}")
                show(chat.send(prompt), debug=True)
            return 0

        print("Sai's Streaming Companion — type 'exit' to quit, '/debug' to toggle detail.\n")
        while True:
            try:
                message = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            if not message:
                continue
            if message.lower() in {"exit", "quit"}:
                return 0
            if message == "/debug":
                debug = not debug
                print(f"debug {'on' if debug else 'off'}\n")
                continue

            show(chat.send(message), debug)
    finally:
        chat.close()


if __name__ == "__main__":
    sys.exit(main())
