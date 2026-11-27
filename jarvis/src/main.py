"""Jarvis entry point: text chat (Phase 1) and voice loop (Phase 2).

    python -m src.main                # interactive text REPL
    python -m src.main --once "hi"    # single turn, then exit (for testing)
    python -m src.main --voice        # mic -> STT -> LLM -> TTS -> speaker
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import sys

from rich.console import Console
from rich.panel import Panel

from .config import load_config, load_system_prompt
from .context import Conversation, build_user_content
from .llm import make_llm_client
from .llm.base import LLMResponse
from .knowledge import make_knowledge_index
from .knowledge.watcher import start_watcher
from .llm.router import detect_research, is_simple, is_smalltalk, pick_tier
from .memory import make_memory_store

console = Console()

# Per-turn state (e.g. a research request made by a tool during the turn).
# Tools run via asyncio.to_thread, which carries this context into the thread.
_turn_state: contextvars.ContextVar[dict | None] = contextvars.ContextVar("jarvis_turn_state", default=None)


class Session:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.system_prompt = load_system_prompt(self.cfg)
        self.llm = make_llm_client(self.cfg)
        self.memory = make_memory_store(self.cfg)
        self.knowledge = make_knowledge_index(self.cfg)
        from .tools import make_tools

        # the model can request research itself; the active loop picks it up.
        # Within a turn the request is recorded on that turn's own state, so
        # concurrent turns (voice vs Telegram) never pick up each other's.
        self.pending_research: str | None = None

        def _request_research(topic: str) -> str:
            state = _turn_state.get()
            if state is not None:
                state["research"] = topic
            else:
                self.pending_research = topic
            return "research started in the background; the user will be told when it's ready"

        self.tools = make_tools(
            self.cfg, self.memory, self.knowledge, request_research=_request_research
        )
        # life organiser: reminders/tasks/logs tools + `delegate` to area agents
        self.life = None
        if self.cfg.get("life"):
            from .life import attach

            self.life = attach(self)
        self.conversation = Conversation(self.cfg["llm"].get("max_history_turns", 20))
        self.total_cost = 0.0
        self._writebacks: set[asyncio.Task] = set()
        self._watcher = None
        if self.cfg.get("knowledge", {}).get("watch", True):
            from .config import ROOT

            folder = ROOT / self.cfg["knowledge"].get("folder", "knowledge")
            self.knowledge.ingest()  # catch up on files added while offline
            self._watcher = start_watcher(self.knowledge, folder)

    async def turn(
        self,
        user_message: str,
        on_text=None,
        *,
        conversation: Conversation | None = None,
        tier: str | None = None,
        note: str | None = None,
        learn: bool = True,
    ) -> LLMResponse:
        """One turn. Defaults serve the voice faces; the life organiser passes
        its own `conversation` (Telegram/dashboard history, separate from
        voice), a fixed `tier`, a context `note` placed before the message,
        and learn=False for scheduled runs (no memory extraction)."""
        import time

        loop = asyncio.get_event_loop()
        t0 = time.monotonic()
        convo = conversation if conversation is not None else self.conversation
        tier = tier or pick_tier(user_message, self.cfg.get("routing", {}))
        from .profile import profile_for_prompt

        smalltalk = is_smalltalk(user_message)
        if smalltalk:
            # greetings get no injected notes/memories — just talk
            memories, knowledge = [], []
            profile = await loop.run_in_executor(None, profile_for_prompt, self.cfg)
        else:
            memories, knowledge, profile = await asyncio.gather(
                loop.run_in_executor(None, self.memory.search, user_message),
                loop.run_in_executor(None, self.knowledge.search, user_message),
                loop.run_in_executor(None, profile_for_prompt, self.cfg),
            )
        t_retrieve = time.monotonic() - t0
        # history keeps only the bare message; profile/memories/knowledge are
        # injected transiently into THIS call, so prompts don't balloon
        convo.add_user(user_message)
        enriched = build_user_content(
            user_message, profile=profile, memories=memories, knowledge=knowledge
        )
        if note:
            enriched = f"{note}\n\n{enriched}"

        first_token = [None]

        def timed_on_text(delta: str) -> None:
            if first_token[0] is None:
                first_token[0] = time.monotonic() - t0
            if on_text is not None:
                on_text(delta)

        fast = is_simple(user_message, tier)
        history = convo.messages[:-1]
        if fast:
            history = history[-6:]  # slim context: recent turns only
        scratch = [*history, {"role": "user", "content": enriched}]
        state = {"research": None}
        token = _turn_state.set(state)
        try:
            resp = await self._agent_loop(
                tier, scratch, on_text=timed_on_text, use_tools=not fast,
                # text turns (Telegram, dashboard) may dig deeper than voice
                max_rounds=((self.cfg.get("llm", {}) or {}).get("max_tool_rounds", {}) or {}).get(tier, 5),
            )
        except Exception:
            convo.messages.pop()  # keep history consistent
            raise
        finally:
            _turn_state.reset(token)
        # a research request made during this turn belongs to this turn's
        # caller: voice callers read session.pending_research (as before),
        # the life organiser reads resp.extra["research_topic"]
        resp.extra["research_topic"] = state["research"]
        if state["research"] and conversation is None:
            self.pending_research = state["research"]
        if fast:
            resp.extra["path"] = "fast"
        resp.extra["timings"] = {
            "retrieve": t_retrieve,
            "first_token": first_token[0],
            "total": time.monotonic() - t0,
        }
        if resp.extra.get("degraded"):
            console.print("[yellow]cloud API failed — answered with local model[/yellow]")
        convo.add_assistant(resp.text)
        if resp.cost_usd is not None:
            self.total_cost += resp.cost_usd
        if learn:
            # fact extraction happens in the background: never adds turn latency
            task = loop.run_in_executor(
                None, self.memory.add_turn, user_message, resp.text
            )
            t = asyncio.ensure_future(task)
            self._writebacks.add(t)
            t.add_done_callback(self._writebacks.discard)
        return resp

    async def _agent_loop(
        self,
        tier: str,
        scratch: list[dict],
        max_rounds: int = 5,
        on_text=None,
        use_tools: bool = True,
    ) -> LLMResponse:
        """Call the LLM with tools; execute tool calls until it answers.

        Tool exchanges live in a per-turn scratch list (provider format);
        only plain text is persisted to the conversation, so turns can hop
        between providers freely.
        """
        from .tools import run_tool

        loop = asyncio.get_event_loop()
        totals = {"input": 0, "output": 0, "cost": 0.0, "rounds": 0}

        tools = self.tools if use_tools else None
        # with tools in play, NOTHING streams to the voice until the loop is
        # done: any round can turn out to be pre-tool narration ("let me
        # check..."), and narration must never be spoken. The final answer is
        # delivered whole at the end; tool-less turns keep live streaming.
        live_on_text = None if tools else on_text
        resp = await self.llm.chat(
            self.system_prompt, scratch, tier=tier, tools=tools, on_text=live_on_text
        )
        while resp.tool_calls and totals["rounds"] < max_rounds:
            totals["rounds"] += 1
            totals["input"] += resp.input_tokens
            totals["output"] += resp.output_tokens
            totals["cost"] += resp.cost_usd or 0.0
            async def run_one(call):
                console.print(f"[dim]tool: {call.name}({call.arguments})[/dim]")
                tool = next((t for t in self.tools if t.name == call.name), None)
                # no tool may stall a conversation: a hard ceiling per tool
                # (20s for lookups; delegate to an area agent gets minutes)
                limit = getattr(tool, "timeout", 20) if tool else 20
                try:
                    # to_thread (not run_in_executor) carries the turn's
                    # context into the thread (see _turn_state)
                    output, is_error = await asyncio.wait_for(
                        asyncio.to_thread(run_tool, self.tools, call.name, call.arguments),
                        timeout=limit,
                    )
                except asyncio.TimeoutError:
                    output, is_error = (
                        (tool.timeout_message if tool and tool.timeout_message else None)
                        or "tool timed out — answer from what you know and say "
                        "you couldn't check live data",
                        True,
                    )
                return (call, output, is_error)

            # one round's calls run concurrently (e.g. delegating to several
            # area agents at once); results go back together, in call order
            results = list(await asyncio.gather(*(run_one(c) for c in resp.tool_calls)))
            if totals["rounds"] >= max_rounds and results:
                # last allowed round: ask for the answer instead of more calls,
                # so running out of rounds never ends in an empty reply
                call, output, is_error = results[-1]
                results[-1] = (call, f"{output}\n\n[That was the last tool call this turn: answer now "
                                     "from what you have, and say what you couldn't check.]", is_error)
            scratch.extend(self.llm.tool_messages(resp, results))
            resp = await self.llm.chat(
                self.system_prompt, scratch, tier=tier, tools=tools, on_text=live_on_text
            )

        if tools and on_text is not None and resp.text:
            on_text(resp.text)  # buffered final answer, narration already gone
        resp.input_tokens += totals["input"]
        resp.output_tokens += totals["output"]
        resp.cost_usd = (resp.cost_usd or 0.0) + totals["cost"]
        resp.extra["tool_rounds"] = totals["rounds"]
        if not resp.text:
            resp.text = "Sorry, I couldn't finish working that one out."
        return resp

    async def warmup(self) -> None:
        """Pay all the lazy-init costs up front (mem0 ~5s, local model load
        ~10s) so the first spoken turn is as fast as the rest."""
        loop = asyncio.get_event_loop()

        async def _warm_llm():
            # keep whichever local tier exists loaded (fast fallback/offline)
            for name in ("default", "fallback_local"):
                tier_cfg = self.cfg["llm"]["tiers"].get(name, {})
                if tier_cfg.get("provider") != "ollama":
                    continue
                try:
                    backend, model_cfg = self.llm.backend_for(name)
                    await backend.chat(
                        self.system_prompt,
                        [{"role": "user", "content": "hi"}],
                        {**model_cfg, "max_tokens": 1},
                        tools=self.tools,
                    )
                except Exception:
                    pass
                break

        def _warm_stores():
            try:
                self.memory.search("warmup")
                self.knowledge.search("warmup")
            except Exception:
                pass

        await asyncio.gather(
            _warm_llm(), loop.run_in_executor(None, _warm_stores)
        )

    async def research(self, topic: str, progress=lambda m: None, conversation: Conversation | None = None):
        """Run the deep-research pipeline; returns (report_path, summary).
        The summary is added to `conversation` (default: the voice one)."""
        from .profile import profile_for_prompt
        from .research.pipeline import ResearchPipeline

        loop = asyncio.get_event_loop()
        profile, memories = await asyncio.gather(
            loop.run_in_executor(None, profile_for_prompt, self.cfg),
            loop.run_in_executor(None, self.memory.search, topic),
        )
        user_context = "\n".join(
            filter(None, [profile, *(f"- {m}" for m in memories)])
        )
        pipeline = ResearchPipeline(self.cfg, self.llm)
        path, summary = await pipeline.run(
            topic, progress=progress, user_context=user_context
        )
        convo = conversation if conversation is not None else self.conversation
        convo.add_user(f"(I asked you to research: {topic})")
        convo.add_assistant(summary)
        return path, summary

    async def flush(self) -> None:
        """Wait for pending memory write-backs (call before process exit)."""
        if self._writebacks:
            await asyncio.gather(*self._writebacks, return_exceptions=True)

    def print_usage_line(self, resp: LLMResponse) -> None:
        cost = f"${resp.cost_usd:.6f}" if resp.cost_usd is not None else "n/a"
        rounds = resp.extra.get("tool_rounds", 0)
        tool_note = f" | tools x{rounds}" if rounds else ""
        timings = resp.extra.get("timings") or {}
        timing_note = ""
        if timings.get("total"):
            ft = timings.get("first_token")
            timing_note = (
                f" | {timings['total']:.1f}s"
                + (f" (first word {ft:.1f}s)" if ft else "")
                + f" retrieve {timings.get('retrieve', 0):.1f}s"
            )
        console.print(
            f"[dim]{resp.model} ({resp.tier}) | "
            f"in {resp.input_tokens} / out {resp.output_tokens} | "
            f"cache read {resp.cache_read_tokens} / write {resp.cache_write_tokens}"
            f"{tool_note}{timing_note} | turn {cost} | session ${self.total_cost:.6f}[/dim]"
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
    warm = asyncio.ensure_future(session.warmup())
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
        topic = detect_research(user_message)
        try:
            if topic:
                path, summary = await session.research(
                    topic, progress=lambda m: console.print(f"[dim]{m}[/dim]")
                )
                console.print(f"[bold cyan]jarvis>[/bold cyan] {summary}")
                console.print(f"[dim]report saved: {path}[/dim]")
                continue
            resp = await session.turn(user_message)
        except Exception as e:  # keep the loop alive on API errors
            console.print(f"[red]error:[/red] {e}")
            continue
        console.print(f"[bold cyan]jarvis>[/bold cyan] {resp.text}")
        session.print_usage_line(resp)
        if session.pending_research:  # model called the deep_research tool
            topic, session.pending_research = session.pending_research, None
            try:
                path, summary = await session.research(
                    topic, progress=lambda m: console.print(f"[dim]{m}[/dim]")
                )
                console.print(f"[bold cyan]jarvis>[/bold cyan] {summary}")
                console.print(f"[dim]report saved: {path}[/dim]")
            except Exception as e:
                console.print(f"[red]research error:[/red] {e}")
    await session.flush()
    console.print(f"[dim]session total: ${session.total_cost:.6f}[/dim]")


async def voice_loop(session: Session) -> None:
    import threading

    from .audio.echo import is_echo, is_stop_command
    from .stt import make_stt_engine
    from .tts import make_tts_engine
    from .tts.streamer import SentenceSpeaker
    from .wakeword import TranscriptGate

    console.print("[dim]loading models (speech + memory + local llm)...[/dim]")
    loop = asyncio.get_event_loop()
    warm = asyncio.ensure_future(session.warmup())  # overlaps with STT load
    stt = await loop.run_in_executor(None, make_stt_engine, session.cfg)
    tts = await loop.run_in_executor(None, make_tts_engine, session.cfg)
    await warm
    speaker = SentenceSpeaker(tts)
    gate = TranscriptGate(session.cfg)
    hint = (
        f"Say \"{session.cfg.get('wakeword', {}).get('phrase', 'hey jarvis')}\" to start."
        if gate.enabled
        else "Speak when ready."
    )
    console.print(
        Panel(
            f"Jarvis — voice mode\n{hint} Say \"stop\" to interrupt. Ctrl-C to exit.",
            style="cyan",
        )
    )

    # the mic never closes: a listener thread streams transcripts in,
    # even while Jarvis is talking (that's what makes barge-in possible)
    transcripts: asyncio.Queue[str] = asyncio.Queue()
    shutting_down = threading.Event()

    def listener() -> None:
        while not shutting_down.is_set():
            try:
                text = stt.listen()
            except Exception:
                break
            if text:
                loop.call_soon_threadsafe(transcripts.put_nowait, text)

    threading.Thread(target=listener, daemon=True).start()

    try:
        while True:
            transcript = await transcripts.get()

            if speaker.speaking:
                if is_stop_command(transcript):
                    console.print("[dim]interrupted[/dim]")
                    speaker.stop()
                    continue
                if is_echo(transcript, speaker.recent_sentences):
                    continue  # Jarvis hearing itself
                # real speech over playback: only a wake-phrase barge-in wins
                if not gate.contains_phrase(transcript):
                    continue
                speaker.stop()

            respond, user_message = gate.should_respond(transcript)
            if not respond:
                console.print(f"[dim]ignored (no wake phrase): {transcript}[/dim]")
                continue
            gate.mark_exchange()
            console.print(f"[bold green]you>[/bold green] {user_message}")

            def launch_research(t: str, announce: bool) -> None:
                async def _bg_research():
                    if announce:
                        speaker.reset()
                        await loop.run_in_executor(
                            None,
                            speaker.speak_now,
                            "On it. I'll let you know when the research is done.",
                        )
                    try:
                        path, summary = await session.research(
                            t, progress=lambda m: console.print(f"[dim]research: {m}[/dim]")
                        )
                        msg = f"Research done. {summary} The full report is saved as {path.name}."
                    except Exception as e:
                        msg = "Sorry, the research failed."
                        console.print(f"[red]research error:[/red] {e}")
                    console.print(f"[bold cyan]jarvis>[/bold cyan] {msg}")
                    speaker.reset()
                    await loop.run_in_executor(None, speaker.speak_now, msg)
                    gate.mark_exchange()

                asyncio.ensure_future(_bg_research())

            topic = detect_research(user_message)
            if topic:
                launch_research(topic, announce=True)
                continue

            speaker.reset()

            # if the answer is slow, acknowledge instantly ("One sec.")
            import random

            tcfg = session.cfg.get("tts", {})
            acks = tcfg.get("acks") or []
            ack_after = tcfg.get("ack_after", 1.6)
            sentences_before = len(speaker.recent_sentences)

            async def maybe_ack() -> None:
                await asyncio.sleep(ack_after)
                # nothing spoken yet this turn -> bridge the silence
                if (
                    acks
                    and len(speaker.recent_sentences) == sentences_before
                    and not speaker.speaking
                ):
                    speaker.feed(random.choice(acks) + " ")

            ack_task = asyncio.ensure_future(maybe_ack())
            try:
                # deltas stream into the speaker: Jarvis starts talking on
                # the first complete sentence while the rest generates
                resp = await session.turn(user_message, on_text=speaker.feed)
                ack_task.cancel()
            except Exception as e:
                ack_task.cancel()
                console.print(f"[red]error:[/red] {e}")
                speaker.reset()
                await loop.run_in_executor(
                    None, speaker.speak_now, "Sorry, I hit an error talking to the model."
                )
                continue
            console.print(f"[bold cyan]jarvis>[/bold cyan] {resp.text}")
            session.print_usage_line(resp)
            await loop.run_in_executor(None, speaker.finish)
            gate.mark_exchange()  # window starts when Jarvis finishes talking
            if session.pending_research:  # model called the deep_research tool
                topic, session.pending_research = session.pending_research, None
                launch_research(topic, announce=False)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        shutting_down.set()
        speaker.stop()
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
    if session.pending_research:  # model called the deep_research tool
        topic, session.pending_research = session.pending_research, None
        try:
            path, summary = await session.research(
                topic, progress=lambda m: console.print(f"[dim]{m}[/dim]")
            )
            console.print(summary)
            console.print(f"[dim]report saved: {path}[/dim]")
        except Exception as e:
            console.print(f"[red]research error:[/red] {e}")
            return 1
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
