"""Phase 1: text chat loop with model routing and per-turn cost logging.

    python -m src.main                # interactive REPL
    python -m src.main --once "hi"    # single turn, then exit (for testing)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.panel import Panel

from .config import load_config, load_system_prompt
from .context import Conversation, build_user_content
from .llm import make_llm_client
from .llm.base import LLMResponse
from .llm.router import pick_tier

console = Console()


class Session:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.system_prompt = load_system_prompt(self.cfg)
        self.llm = make_llm_client(self.cfg)
        self.conversation = Conversation(self.cfg["llm"].get("max_history_turns", 20))
        self.total_cost = 0.0

    async def turn(self, user_message: str) -> LLMResponse:
        tier = pick_tier(user_message, self.cfg.get("routing", {}))
        # Phase 3/4 will pass memories= / knowledge= here.
        self.conversation.add_user(build_user_content(user_message))
        resp = await self.llm.chat(
            self.system_prompt, self.conversation.messages, tier=tier
        )
        self.conversation.add_assistant(resp.text)
        if resp.cost_usd is not None:
            self.total_cost += resp.cost_usd
        return resp

    def print_usage_line(self, resp: LLMResponse) -> None:
        cost = f"${resp.cost_usd:.6f}" if resp.cost_usd is not None else "n/a"
        console.print(
            f"[dim]{resp.model} ({resp.tier}) | "
            f"in {resp.input_tokens} / out {resp.output_tokens} | "
            f"cache read {resp.cache_read_tokens} / write {resp.cache_write_tokens} | "
            f"turn {cost} | session ${self.total_cost:.6f}[/dim]"
        )


async def repl(session: Session) -> None:
    console.print(
        Panel(
            "Jarvis — Phase 1 text chat\n"
            "Type your message. Commands: [bold]/quit[/bold]",
            style="cyan",
        )
    )
    loop = asyncio.get_event_loop()
    while True:
        try:
            user_message = await loop.run_in_executor(
                None, lambda: console.input("[bold green]you>[/bold green] ")
            )
        except (EOFError, KeyboardInterrupt):
            break
        user_message = user_message.strip()
        if not user_message:
            continue
        if user_message in ("/quit", "/q", "/exit"):
            break
        try:
            resp = await session.turn(user_message)
        except Exception as e:  # keep the loop alive on API errors
            console.print(f"[red]error:[/red] {e}")
            continue
        console.print(f"[bold cyan]jarvis>[/bold cyan] {resp.text}")
        session.print_usage_line(resp)
    console.print(f"[dim]session total: ${session.total_cost:.6f}[/dim]")


async def once(session: Session, message: str) -> int:
    try:
        resp = await session.turn(message)
    except Exception as e:
        console.print(f"[red]error:[/red] {e}")
        return 1
    console.print(resp.text)
    session.print_usage_line(resp)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis text chat")
    parser.add_argument("--once", metavar="MSG", help="send one message and exit")
    args = parser.parse_args()

    session = Session()
    if args.once:
        sys.exit(asyncio.run(once(session, args.once)))
    asyncio.run(repl(session))


if __name__ == "__main__":
    main()
