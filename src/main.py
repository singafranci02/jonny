"""Jarvis entry point: text chat (Phase 1) and voice loop (Phase 2).

    python -m src.main                # interactive text REPL
    python -m src.main --once "hi"    # single turn, then exit (for testing)
    python -m src.main --voice        # mic -> STT -> LLM -> TTS -> speaker
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
from .memory import make_memory_store

console = Console()


class Session:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.system_prompt = load_system_prompt(self.cfg)
        self.llm = make_llm_client(self.cfg)
        self.memory = make_memory_store(self.cfg)
        self.conversation = Conversation(self.cfg["llm"].get("max_history_turns", 20))
        self.total_cost = 0.0
        self._writebacks: set[asyncio.Task] = set()

    async def turn(self, user_message: str) -> LLMResponse:
        loop = asyncio.get_event_loop()
        tier = pick_tier(user_message, self.cfg.get("routing", {}))
        memories = await loop.run_in_executor(None, self.memory.search, user_message)
        # Phase 4 will pass knowledge= here too.
        self.conversation.add_user(build_user_content(user_message, memories=memories))
        resp = await self.llm.chat(
            self.system_prompt, self.conversation.messages, tier=tier
        )
        self.conversation.add_assistant(resp.text)
        if resp.cost_usd is not None:
            self.total_cost += resp.cost_usd
        # fact extraction happens in the background: never adds turn latency
        task = loop.run_in_executor(
            None, self.memory.add_turn, user_message, resp.text
        )
        t = asyncio.ensure_future(task)
        self._writebacks.add(t)
        t.add_done_callback(self._writebacks.discard)
        return resp

    async def flush(self) -> None:
        """Wait for pending memory write-backs (call before process exit)."""
        if self._writebacks:
            await asyncio.gather(*self._writebacks, return_exceptions=True)

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
    await session.flush()
    console.print(f"[dim]session total: ${session.total_cost:.6f}[/dim]")


async def voice_loop(session: Session) -> None:
    from .stt import make_stt_engine
    from .tts import make_tts_engine

    console.print("[dim]loading speech-to-text model (first run downloads it)...[/dim]")
    loop = asyncio.get_event_loop()
    stt = await loop.run_in_executor(None, make_stt_engine, session.cfg)
    tts = await loop.run_in_executor(None, make_tts_engine, session.cfg)
    console.print(
        Panel(
            "Jarvis — voice mode\nSpeak when ready. Ctrl-C to exit.",
            style="cyan",
        )
    )
    try:
        while True:
            console.print("[dim]listening...[/dim]")
            user_message = await loop.run_in_executor(None, stt.listen)
            if not user_message:
                continue
            console.print(f"[bold green]you>[/bold green] {user_message}")
            try:
                resp = await session.turn(user_message)
            except Exception as e:
                console.print(f"[red]error:[/red] {e}")
                await loop.run_in_executor(
                    None, tts.speak, "Sorry, I hit an error talking to the model."
                )
                continue
            console.print(f"[bold cyan]jarvis>[/bold cyan] {resp.text}")
            session.print_usage_line(resp)
            await loop.run_in_executor(None, tts.speak, resp.text)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        tts.stop()
        stt.shutdown()
        await session.flush()
        console.print(f"\n[dim]session total: ${session.total_cost:.6f}[/dim]")


async def once(session: Session, message: str) -> int:
    try:
        resp = await session.turn(message)
    except Exception as e:
        console.print(f"[red]error:[/red] {e}")
        return 1
    console.print(resp.text)
    session.print_usage_line(resp)
    await session.flush()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis text chat")
    parser.add_argument("--once", metavar="MSG", help="send one message and exit")
    parser.add_argument("--voice", action="store_true", help="voice mode (mic + speaker)")
    args = parser.parse_args()

    session = Session()
    if args.once:
        sys.exit(asyncio.run(once(session, args.once)))
    if args.voice:
        try:
            asyncio.run(voice_loop(session))
        except KeyboardInterrupt:
            pass
        return
    asyncio.run(repl(session))


if __name__ == "__main__":
    main()
