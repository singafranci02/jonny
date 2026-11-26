"""The life organiser running inside the brain server (`make serve`):

- Telegram: long-polls for your messages and button presses; text goes to
  Jarvis (main agent), which can delegate to the area agents.
- Scheduler: every 20s sends due reminders; runs the check-ins (morning
  brief, evening check-in, Sunday review) at their times; nightly backup.
- Dashboard data for the website (see api.py).

Only the server process runs this; `make chat` / `make voice` get the life
tools and delegation but don't poll Telegram or send reminders.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from ..context import Conversation
from . import board
from .areas import ensure_folders, folder_status, full_disk_access_target, workspace_root
from .clock import from_iso, parse_days, to_iso, utc_now
from .telegram import TelegramClient, TelegramError, reminder_keyboard

log = logging.getLogger("jarvis.life")

# Durable turns (LangGraph's checkpoint-and-resume, done on life.db): work in
# progress is checkpointed, and what a restart cut off is resumed on start.
INFLIGHT = "inflight_turns"  # Telegram messages whose reply is being worked on
RESUME_WINDOW = timedelta(minutes=20)  # older than this, just tell him instead
STRAVA_EVERY = timedelta(minutes=30)
STRAVA_SETUP = (
    "Strava needs a free API app first (2 minutes):\n"
    "1. strava.com/settings/api: create an app. Category: Training. Website: anything. "
    "Authorization Callback Domain: localhost\n"
    "2. Put its Client ID and Client Secret in Jarvis's .env on the Mac mini as STRAVA_CLIENT_ID and "
    "STRAVA_CLIENT_SECRET, then make install-web\n"
    "3. Send /strava again."
)
STRAVA_CONNECT = (
    "Tap Connect Strava and approve. You'll land on a page that won't load: that's expected. "
    "Copy its address (it starts with http://localhost/exchange_token) and send it to me here."
)

HELP = (
    "I'm Jarvis. Tap the buttons below, or just text me.\n"
    "📋 Board: your projects by area. Tap one for its tracker; tap a task to tick it off\n"
    "➕ Add: a new project\n"
    "📅 Today: what's on\n"
    "📝 Log: sleep, mood, training in one tap\n"
    "/strava: connect Strava, so workouts log themselves\n\n"
    "Text works too: \"remind me to call mum Sunday 6pm\", \"plan my training week\", "
    "\"work on YardBird: draft the founder contributions note\""
)


@dataclass
class CheckIn:
    key: str
    label: str
    time: str  # "HH:MM" local
    days: list[int] | None  # None = every day
    task: str
    catch_up_min: int = 120
    buttons: str | None = None  # "daily-log": tap-to-log sleep/mood/training under the message


class LifeService:
    def __init__(self, session):
        self.session = session
        self.cfg = session.cfg
        self.life = session.life
        self.store = self.life.store
        self.clock = self.life.clock
        self.areas = self.life.areas
        self.labels = {a.id: a.label for a in self.areas}
        lcfg = self.cfg.get("life", {}) or {}
        tiers = self.cfg["llm"]["tiers"]
        tier = lcfg.get("coordinator_tier", "coordinator")
        self.coordinator_tier = tier if tier in tiers else "hard"
        max_turns = self.cfg["llm"].get("max_history_turns", 10)
        self.text_convo = Conversation(max_turns)
        self._cut_off: list[dict] = []
        try:
            # the chat thread survives restarts: rebuilt from the message feed
            self.text_convo.messages = self.store.thread_history(max_turns)
            self._cut_off = self.store.get_setting(INFLIGHT) or []
        except Exception as e:
            log.warning("could not restore the chat thread: %s", e)
        self._inflight: dict[str, dict] = {}
        self._inflight_lock = asyncio.Lock()
        self._running_checkins: set[str] = set()
        self.text_lock = asyncio.Lock()
        self.checkins = [
            CheckIn(
                key=c["key"],
                label=c.get("label", c["key"]),
                time=c["time"],
                days=parse_days(c.get("days", "daily")),
                task=" ".join(str(c["task"]).split()),
                catch_up_min=int(c.get("catch_up_minutes", 120)),
                buttons=c.get("buttons"),
            )
            for c in lcfg.get("checkins", []) or []
        ]
        self.backup_keep = int(lcfg.get("backup_keep", 14))
        tg_enabled = (lcfg.get("telegram", {}) or {}).get("enabled", True)
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.tg = TelegramClient(token) if (tg_enabled and token) else None
        self.owner_chat = int(chat) if chat.lstrip("-").isdigit() else None
        self.status = {
            "telegram": "not configured" if not self.tg else "starting",
            "bot": None,
            "last_poll": None,
        }
        self._tasks: list[asyncio.Task] = []

    # ---------------- lifecycle ----------------

    async def start(self) -> None:
        try:
            done = await asyncio.to_thread(ensure_folders, self.cfg)
            if done["created"]:
                log.info("life folders created: %s", ", ".join(done["created"]))
        except Exception as e:  # iCloud not ready / no permission: keep running
            log.warning("could not prepare the Jarvis folder: %s", e)
        if self.tg:
            self._tasks.append(asyncio.ensure_future(self._poll_loop()))
            self._tasks.append(asyncio.ensure_future(self.announce_menu()))
            self._tasks.append(asyncio.ensure_future(self.resume_cut_off()))
        self._tasks.append(asyncio.ensure_future(self._scheduler_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.tg:
            await self.tg.close()

    def can_message(self) -> bool:
        return bool(self.tg and self.owner_chat)

    # ---------------- outbound ----------------

    async def notify(self, text: str, source: str, area: str | None = None, keyboard: dict | None = None) -> bool:
        if not self.can_message():
            return False
        await self.tg.send(self.owner_chat, text, keyboard)
        await asyncio.to_thread(self.store.log_message, "out", source, text, area)
        return True

    # ---------------- text conversation (Telegram + dashboard) ----------------

    def _note(self, channel: str, outside: list[dict], resumed: bool = False) -> str:
        parts = []
        if resumed:
            parts.append(
                "A restart cut off your first reply to this message; part of it may already be done. Check "
                "life_overview before adding or changing anything so nothing is duplicated, then reply as normal."
            )
        if outside:
            lines = [
                f"- {'reminder' if m['source'] == 'reminder' else 'check-in'}: {m['text'][:600]}"
                for m in outside
            ]
            parts.append("Since your last exchange, the user was sent:\n" + "\n".join(lines))
        parts.append(
            f"[{self.clock.label_now()}] Channel: {channel} (text chat, not voice). Text like a person "
            "in a chat: one short line, a few words when that's enough. A capture gets a terse receipt "
            "(\"Logged: 5 km run, 9:42/km.\", \"Done. Sunday 18:00.\"). Never restate his message, explain "
            "a conversion or offer alternatives. If something could be read two ways, take the obvious "
            "reading and move on: he corrects you if needed. Don't ask for anything he didn't bring up "
            "unless you can't act without it. Longer only for a plan or summary he asked for, and then "
            "at most 6 short lines. Plain text, no markdown."
        )
        return "\n\n".join(parts)

    async def chat(self, text: str, channel: str = "telegram", resumed: bool = False) -> str:
        """One text turn with Jarvis on the coordinator tier. Serialised: the
        text conversation has a single history."""
        async with self.text_lock:
            outside = await asyncio.to_thread(self.store.unseen_outbound)  # before logging this one
            await asyncio.to_thread(self.store.log_message, "in", "user", text)
            resp = await self.session.turn(
                text,
                conversation=self.text_convo,
                tier=self.coordinator_tier,
                note=self._note(channel, outside, resumed),
            )
            reply = resp.text
            await asyncio.to_thread(self.store.log_message, "out", "chat", reply)
        # a deep_research request made in THIS turn (never another channel's)
        if resp.extra.get("research_topic"):
            asyncio.ensure_future(self._research(resp.extra["research_topic"]))
        return reply

    async def _research(self, topic: str) -> None:
        try:
            # the summary joins the text conversation, not the voice one
            path, summary = await self.session.research(topic, conversation=self.text_convo)
            await self.notify(f"Research done: {topic}\n\n{summary}\n\nSaved as {path.name}", "checkin")
        except Exception as e:
            log.exception("research failed")
            await self.notify(f"The research on \"{topic}\" failed: {e}", "system")

    # ---------------- Telegram ----------------

    async def _poll_loop(self) -> None:
        offset = await asyncio.to_thread(self.store.get_setting, "telegram_offset")
        backoff = 2
        while True:
            try:
                if not self.status["bot"]:
                    me = await self.tg.get_me()
                    self.status["bot"] = me.get("username")
                updates = await self.tg.get_updates(offset, timeout=50)
                self.status.update(telegram="polling", last_poll=to_iso(utc_now()))
                backoff = 2
                for u in updates:
                    offset = u["update_id"] + 1
                    # persist first: a crash mid-reply must not replay the message
                    await asyncio.to_thread(self.store.set_setting, "telegram_offset", offset)
                    asyncio.ensure_future(self._handle_safe(u))
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                if e.code == 409 and "webhook" in str(e).lower():
                    # a webhook is set: polling needs it gone
                    try:
                        await self.tg.delete_webhook()
                        continue
                    except Exception:
                        pass
                if e.code == 409:
                    # another process polls this bot (e.g. a second Mac running
                    # `make serve`): back off rather than fight over updates
                    self.status["telegram"] = "conflict: another Jarvis is polling this bot"
                else:
                    self.status["telegram"] = f"error: {e}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:  # network down etc.
                self.status["telegram"] = f"error: {e}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_safe(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                await self._on_button(update["callback_query"])
            elif "message" in update:
                await self._on_message(update["message"])
        except Exception:
            log.exception("telegram update %s failed", update.get("update_id"))

    async def _on_message(self, msg: dict) -> None:
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if self.owner_chat is None:
            # first-run helper: tell the owner which id to configure
            if text.startswith("/start"):
                await self.tg.send(
                    chat_id,
                    f"Hi! Your chat id is {chat_id}. Put TELEGRAM_CHAT_ID={chat_id} in Jarvis's .env "
                    "on the Mac mini, then restart Jarvis (make install-web).",
                )
            return
        if chat_id != self.owner_chat:
            return  # ignore everyone else
        if not text:
            await self.tg.send(chat_id, "I can only read text for now.")
            return
        if text.startswith(("/start", "/help", "/menu")):
            await self.tg.send(chat_id, HELP, board.menu_keyboard())
            await asyncio.to_thread(self.store.set_setting, "menu_version", board.MENU_VERSION)
            return
        if text.startswith("/strava") or self._strava() and self._strava().looks_like_callback(text):
            await self._on_strava(chat_id, text)
            return
        if text == board.MENU_BOARD or text.startswith("/board"):
            await self.send_board(chat_id)
            return
        if text == board.MENU_ADD:
            await self._ask_input(chat_id, {"kind": "new_project", "area": None},
                                  "New project: what's it called?", "Project name")
            return
        if text == board.MENU_LOG:
            await self.tg.send(chat_id, "Log today:", board.log_keyboard(await self._log_values()))
            return
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to and await self._answer_prompt(chat_id, reply_to, text):
            return
        if text == board.MENU_TODAY or text.startswith("/today"):
            await asyncio.to_thread(self.store.log_message, "in", "command", text)
            summary = await asyncio.to_thread(self.store.today_text, lambda a: self.labels.get(a, a))
            await self.tg.send(chat_id, summary)
            return

        await self._reply(chat_id, text, key=str(msg.get("message_id") or to_iso(utc_now())))

    async def _reply(self, chat_id: int, text: str, key: str, resumed: bool = False) -> None:
        """Answer a message through Jarvis. While the turn runs, the message
        is checkpointed in life.db: if a restart cuts the turn off, the
        checkpoint is still there when Jarvis starts again (resume_cut_off)."""
        await self._checkpoint(key, {"key": key, "text": text, "at": to_iso(utc_now()), "resumed": resumed})
        typing = asyncio.ensure_future(self._keep_typing())
        try:
            reply = await self.chat(text, "telegram", resumed=resumed)
        except Exception as e:
            log.exception("chat turn failed")
            reply = f"Sorry, something went wrong on my side ({type(e).__name__}). Try again in a moment."
        finally:
            typing.cancel()
        # skipped when a shutdown cancels the turn: the checkpoint stays
        await self._checkpoint(key, None)
        try:
            await self.tg.send(chat_id, reply)  # retries transient failures itself
        except Exception as e:
            log.error("reply not delivered: %s", e)
            await asyncio.to_thread(
                self.store.log_message, "out", "system", f"(This reply couldn't be delivered to Telegram: {e})"
            )

    async def _checkpoint(self, key: str, entry: dict | None) -> None:
        async with self._inflight_lock:
            if entry is None:
                self._inflight.pop(key, None)
            else:
                self._inflight[key] = entry
            await asyncio.to_thread(self.store.set_setting, INFLIGHT, list(self._inflight.values()))

    async def resume_cut_off(self) -> None:
        """Messages whose reply a restart cut off: answered now, once. If
        that's too late (the Mac was off a while) or the retry was cut off
        too, he gets one line instead of silence."""
        cut_off, self._cut_off = self._cut_off, []
        if not cut_off or not self.can_message():
            return
        for e in cut_off:
            try:
                fresh = utc_now() - from_iso(e["at"]) < RESUME_WINDOW
                if fresh and not e.get("resumed"):
                    log.info("resuming a reply cut off by a restart")
                    await self._reply(self.owner_chat, e["text"], key=e["key"], resumed=True)
                else:
                    short = " ".join(e["text"].split())
                    short = short if len(short) <= 60 else short[:59] + "…"
                    await self.notify(f"A restart cut off my reply to \"{short}\". Send it again if it still matters.",
                                      "system")
            except Exception:
                log.exception("could not resume a cut-off reply")
        async with self._inflight_lock:  # drop leftovers the loop above didn't re-save
            await asyncio.to_thread(self.store.set_setting, INFLIGHT, list(self._inflight.values()))

    def _strava(self):
        return getattr(self.life, "strava", None)

    async def _on_strava(self, chat_id: int, text: str) -> None:
        """/strava connects (or reports and syncs); the pasted localhost
        address from Strava's consent page finishes connecting."""
        strava = self._strava()
        if strava is None:
            await self.tg.send(chat_id, "Strava isn't available here.")
            return
        if not strava.keys():
            await self.tg.send(chat_id, STRAVA_SETUP)
            return
        if strava.looks_like_callback(text):
            try:
                name = await asyncio.to_thread(strava.finish_connect, text)
                summary = await asyncio.to_thread(strava.sync)
            except Exception as e:
                await self.tg.send(chat_id, f"Strava didn't connect: {e}")
                return
            await asyncio.to_thread(self.store.log_message, "in", "command", "/strava (connected)")
            await self.tg.send(chat_id, f"Strava connected, {name}. {summary.splitlines()[0]}")
            return
        await asyncio.to_thread(self.store.log_message, "in", "command", "/strava")
        if strava.tokens() and "connect" not in text:
            try:
                summary = await asyncio.to_thread(strava.sync)
            except Exception as e:
                summary = f"Sync failed: {e}"
            await self.tg.send(chat_id, f"{summary.splitlines()[0]} (/strava connect to link again)")
            return
        url = await asyncio.to_thread(strava.connect_url)
        await self.tg.send(chat_id, STRAVA_CONNECT, {"inline_keyboard": [[{"text": "Connect Strava", "url": url}]]})

    async def _maybe_sync_strava(self) -> None:
        strava = self._strava()
        if strava is None or not strava.connected():
            return
        last = await asyncio.to_thread(self.store.get_setting, "strava_last_sync")
        if last and utc_now() - from_iso(last) < STRAVA_EVERY:
            return
        try:
            await asyncio.to_thread(strava.sync)
        except Exception as e:  # quiet: the next sweep tries again
            log.warning("strava sync failed: %s", e)
            await asyncio.to_thread(self.store.set_setting, "strava_last_sync", to_iso(utc_now()))

    async def _keep_typing(self) -> None:
        while True:
            await self.tg.typing(self.owner_chat)
            await asyncio.sleep(4.5)

    async def _on_button(self, q: dict) -> None:
        message = q.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if self.owner_chat is None or chat_id != self.owner_chat:
            await self.tg.answer_callback(q["id"])
            return
        action, _, rest = (q.get("data") or "").partition(":")
        if action in ("done", "snooze"):
            await self._on_reminder_button(q, message, chat_id, action, rest)
        elif action in ("bd", "pj", "ps", "pn", "pa", "pc", "lg", "tk"):
            try:
                await self._on_board_button(q, message, chat_id, action, rest)
            except Exception:
                log.exception("board button %r failed", q.get("data"))
                await self.tg.answer_callback(q["id"], "Something went wrong there.")
        else:
            await self.tg.answer_callback(q["id"])

    async def _on_reminder_button(self, q: dict, message: dict, chat_id: int, action: str, rest: str) -> None:
        rid_text, _, arg = rest.partition(":")
        rid = int(rid_text) if rid_text.isdigit() else -1
        reminder = await asyncio.to_thread(self.store.get_reminder, rid)
        if reminder is None:
            await self.tg.answer_callback(q["id"], "That reminder no longer exists.")
            return
        if action == "done":
            await asyncio.to_thread(self.store.complete, rid)
            status = "✅ Done"
        elif action == "snooze":
            until = self.clock.tomorrow_morning() if arg == "tomorrow" else utc_now() + timedelta(minutes=int(arg or 60))
            await asyncio.to_thread(self.store.snooze, rid, until)
            status = f"⏰ Snoozed until {self.clock.when(until)}"
        else:
            await self.tg.answer_callback(q["id"])
            return
        await self.tg.answer_callback(q["id"], status)
        original = message.get("text") or f"⏰ {reminder['text']}"
        await self.tg.edit_text(chat_id, message["message_id"], f"{original}\n\n{status}")
        await asyncio.to_thread(
            self.store.log_message, "in", "button", f"{status}: {reminder['text']}", reminder["area"]
        )

    # ---------------- board, menu and tap-to-log (no model calls) ----------------

    async def announce_menu(self) -> None:
        """Send the menu bar once per MENU_VERSION, so the buttons appear
        without the user having to type /start again."""
        try:
            if not self.can_message():
                return
            if await asyncio.to_thread(self.store.get_setting, "menu_version") == board.MENU_VERSION:
                return
            await self.tg.send(self.owner_chat, "Buttons are ready below. 📋 Board for your projects, "
                               "📝 Log for sleep, mood and training.", board.menu_keyboard())
            await asyncio.to_thread(self.store.set_setting, "menu_version", board.MENU_VERSION)
        except Exception as e:
            log.warning("could not send the menu bar: %s", e)

    def _area(self, area_id: str | None):
        return next((a for a in self.areas if a.id == area_id), None)

    async def _board_view(self, area_id: str | None = None) -> tuple[str, dict]:
        projects = await asyncio.to_thread(self.store.projects)
        counts = await asyncio.to_thread(self.store.task_counts)
        return board.board_view(projects, self.areas, area_id, counts)

    async def _card(self, item: dict) -> tuple[str, dict]:
        """A project card with its tracker (open tasks, and the last 3 days' ticks)."""
        tasks = await asyncio.to_thread(self.store.project_tasks, item["id"], 3)
        return board.project_card(item, self._area(item["area"]), tasks)

    async def send_board(self, chat_id: int) -> None:
        await self.tg.send(chat_id, *(await self._board_view()))

    async def _ask_input(self, chat_id: int, pending: dict, prompt: str, placeholder: str) -> None:
        """Ask for typed text with a force-reply prompt; the reply to that
        exact message is the answer (anything else is normal chat)."""
        mid = await self.tg.send(chat_id, prompt, board.force_reply(placeholder))
        await asyncio.to_thread(self.store.set_setting, "pending_input", {**pending, "prompt_id": mid})

    async def _answer_prompt(self, chat_id: int, reply_to: int, text: str) -> bool:
        pending = await asyncio.to_thread(self.store.get_setting, "pending_input")
        if not pending or pending.get("prompt_id") != reply_to:
            return False
        await asyncio.to_thread(self.store.set_setting, "pending_input", None)
        await asyncio.to_thread(self.store.log_message, "in", "button", text)
        if pending["kind"] == "next_step":
            item = await asyncio.to_thread(lambda: self.store.update_item(int(pending["item"]), next_step=text))
            if item is None:
                await self.tg.send(chat_id, "That project no longer exists.")
                return True
            await self.tg.send(chat_id, *(await self._card(item)))
        elif pending["kind"] == "new_project":
            title = " ".join(text.split())[:120]
            if pending.get("area") and self._area(pending["area"]):
                await self._create_project(chat_id, pending["area"], title)
            else:
                await asyncio.to_thread(self.store.set_setting, "pending_project_title", title)
                await self.tg.send(chat_id, f"Which area for \"{title}\"?", board.area_picker(self.areas))
        return True

    async def _create_project(self, chat_id: int, area_id: str, title: str, message_id: int | None = None) -> None:
        item = await asyncio.to_thread(self.store.add_item, area_id, "project", title)
        text, keyboard = await self._card(item)
        if message_id is None:
            await self.tg.send(chat_id, text, keyboard)
        else:
            await self.tg.edit_text(chat_id, message_id, text, keyboard)

    async def _log_values(self) -> dict:
        ids = [a.id for a in self.areas]

        def read() -> dict:
            out = {}
            for key in board.LOG_ROWS:
                area, metric, _ = board.log_target(key, ids)
                out[key] = self.store.today_value(area, metric)
            return out

        return await asyncio.to_thread(read)

    async def _on_board_button(self, q: dict, message: dict, chat_id: int, action: str, rest: str) -> None:
        mid = message.get("message_id")
        first, _, second = rest.partition(":")
        if action == "bd":
            area = first if self._area(first) else None
            await self.tg.answer_callback(q["id"])
            await self.tg.edit_text(chat_id, mid, *(await self._board_view(area)))
            return
        if action == "tk":
            task = await asyncio.to_thread(self.store.get_item, int(first)) if first.isdigit() else None
            parent = await asyncio.to_thread(self.store.get_item, task["parent_id"]) if task and task.get("parent_id") else None
            if parent is None:
                await self.tg.answer_callback(q["id"], "That task is no longer on a tracker.")
                return
            if task["status"] == "open":
                await asyncio.to_thread(lambda: self.store.update_item(task["id"], status="done"))
                await asyncio.to_thread(self.store.log_message, "in", "button", f"☑ {task['title']} ({parent['title']})",
                                        parent["area"])
            await self.tg.answer_callback(q["id"], f"☑ {task['title'][:60]}")
            await self.tg.edit_text(chat_id, mid, *(await self._card(parent)))
            return
        if action == "pa":
            await self.tg.answer_callback(q["id"])
            await self._ask_input(chat_id, {"kind": "new_project", "area": first or None},
                                  "New project: what's it called?", "Project name")
            return
        if action == "pc":
            title = await asyncio.to_thread(self.store.get_setting, "pending_project_title")
            if not title or not self._area(first):
                await self.tg.answer_callback(q["id"], "Tap ➕ Add and send the name again.")
                return
            await asyncio.to_thread(self.store.set_setting, "pending_project_title", None)
            await self.tg.answer_callback(q["id"], "Added")
            await self._create_project(chat_id, first, title, mid)
            return
        if action == "lg":
            if first not in board.LOG_ROWS:
                await self.tg.answer_callback(q["id"])
                return
            value = float(second)
            area, metric, unit = board.log_target(first, [a.id for a in self.areas])
            await asyncio.to_thread(self.store.set_today_log, area, metric, value, unit)
            label = board.log_confirmation(first, value)
            await asyncio.to_thread(self.store.log_message, "in", "button", label, area)
            await self.tg.answer_callback(q["id"], f"{label} ✓")
            await self.tg.edit_keyboard(chat_id, mid, board.log_keyboard(await self._log_values()))
            return

        # project actions: pj / ps / pn carry the project id first
        item = await asyncio.to_thread(self.store.get_item, int(first)) if first.isdigit() else None
        if item is None or item["kind"] != "project":
            await self.tg.answer_callback(q["id"], "That project no longer exists.")
            return
        if action == "pn":
            await self.tg.answer_callback(q["id"])
            await self._ask_input(chat_id, {"kind": "next_step", "item": item["id"]},
                                  f"Next step for \"{item['title']}\"?", "Next step")
            return
        if action == "ps" and second in ("todo", "doing", "waiting", "done"):
            patch = {"status": "done"} if second == "done" else {"stage": second}
            item = await asyncio.to_thread(lambda: self.store.update_item(item["id"], **patch))
            label = f"{board.STAGE_ICON[second]} {board.STAGE_NAME[second]}"
            await asyncio.to_thread(self.store.log_message, "in", "button", f"{label}: {item['title']}", item["area"])
            await self.tg.answer_callback(q["id"], label)
        else:
            await self.tg.answer_callback(q["id"])
        await self.tg.edit_text(chat_id, mid, *(await self._card(item)))

    # ---------------- scheduler ----------------

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self.send_due_reminders()
                await asyncio.to_thread(self.store.set_setting, "scheduler_last_sweep", to_iso(utc_now()))
                await self._due_checkins()
                await self._maybe_backup()
                await self._maybe_sync_strava()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler sweep failed")
            await asyncio.sleep(20)

    async def send_due_reminders(self) -> int:
        if not self.can_message():
            return 0  # leave them pending until Telegram is configured
        sent = 0
        for r in await asyncio.to_thread(self.store.claim_due):
            repeat = "" if r["recurrence"] == "none" else f" · repeats {r['recurrence']}"
            try:
                await self.tg.send(
                    self.owner_chat,
                    f"⏰ {r['text']}\n{self.labels.get(r['area'], r['area'])}{repeat}",
                    reminder_keyboard(r["id"]),
                )
            except Exception as e:
                log.warning("reminder %s send failed: %s", r["id"], e)
                await asyncio.to_thread(self.store.release, r["id"])
                continue
            await asyncio.to_thread(self.store.mark_sent, r)
            await asyncio.to_thread(
                self.store.log_message, "out", "reminder", f"⏰ {r['text']} (reminder #{r['id']})", r["area"]
            )
            sent += 1
        return sent

    async def _due_checkins(self) -> None:
        if not self.can_message():
            return
        for ci in self.checkins:
            slot = self.clock.last_slot(ci.time, ci.days)
            key = f"checkin_last:{ci.key}"
            last = await asyncio.to_thread(self.store.get_setting, key)
            if last is None:
                # first run after install: start counting from now, don't fire late
                await asyncio.to_thread(self.store.set_setting, key, to_iso(slot))
                continue
            running = f"checkin_running:{ci.key}"
            if ci.key not in self._running_checkins:
                cut = await asyncio.to_thread(self.store.get_setting, running)
                if cut:
                    # a restart cut this check-in off mid-run: run it again, once, if still in time
                    await asyncio.to_thread(self.store.set_setting, running, None)
                    if cut.get("attempt", 1) < 2 and utc_now() - from_iso(cut["slot"]) <= timedelta(
                            minutes=ci.catch_up_min):
                        log.info("resuming check-in %s cut off by a restart", ci.key)
                        asyncio.ensure_future(self._run_checkin_safe(ci, cut["slot"], attempt=2))
                        continue
            if last >= to_iso(slot):
                continue
            await asyncio.to_thread(self.store.set_setting, key, to_iso(slot))
            if utc_now() - slot > timedelta(minutes=ci.catch_up_min):
                log.info("skipped check-in %s: the Mac was off at %s", ci.key, slot)
                continue
            asyncio.ensure_future(self._run_checkin_safe(ci, to_iso(slot)))

    async def _run_checkin_safe(self, ci: CheckIn, slot: str | None = None, attempt: int = 1) -> None:
        running = f"checkin_running:{ci.key}"
        self._running_checkins.add(ci.key)
        await asyncio.to_thread(self.store.set_setting, running, {"slot": slot or to_iso(utc_now()), "attempt": attempt})
        try:
            await self.run_checkin(ci)
        except Exception as e:
            log.exception("check-in %s failed", ci.key)
            await self.notify(f"My {ci.label} check-in failed ({type(e).__name__}). I'll try again next time.", "system")
        finally:
            self._running_checkins.discard(ci.key)
        # skipped when a shutdown cancels the run: the marker stays for the next start
        await asyncio.to_thread(self.store.set_setting, running, None)

    async def run_checkin(self, ci: CheckIn) -> str:
        """A scheduled turn: a fresh conversation on the coordinator tier,
        whose final answer is sent to Telegram."""
        resp = await self.session.turn(
            f"[scheduled check-in: {ci.label}] {ci.task}",
            conversation=Conversation(4),
            tier=self.coordinator_tier,
            note=(
                f"[{self.clock.label_now()}] Automated check-in: nobody is waiting on a live reply. Your "
                "reply text is sent to the user on Telegram as one message, like a text from a friend: "
                "as few words as possible, no greeting or sign-off, skip anything empty. Plain text, no "
                "markdown. Reply with just NOTHING if there is genuinely nothing worth sending."
            ),
            learn=False,
        )
        text = (resp.text or "").strip()
        if text and text.upper() != "NOTHING":
            keyboard = board.log_keyboard(await self._log_values()) if ci.buttons == "daily-log" else None
            await self.notify(text, "checkin", keyboard=keyboard)
        if resp.extra.get("research_topic"):
            asyncio.ensure_future(self._research(resp.extra["research_topic"]))
        return text

    def checkin_by_key(self, key: str) -> CheckIn | None:
        return next((c for c in self.checkins if c.key == key), None)

    async def _maybe_backup(self) -> None:
        now = self.clock.now()
        today = now.date().isoformat()
        if now.hour < 3 or await asyncio.to_thread(self.store.get_setting, "backup_last") == today:
            return
        # one attempt per day: a blocked iCloud folder shouldn't retry (and
        # log) every 20 seconds
        await asyncio.to_thread(self.store.set_setting, "backup_last", today)
        try:
            await asyncio.to_thread(self.backup)
        except Exception as e:
            log.warning("nightly backup failed: %s", e)

    def backup(self) -> str:
        folder = workspace_root(self.cfg) / "_backups"
        dest = folder / f"life-{self.clock.today().isoformat()}.db"
        self.store.backup_to(dest)
        olds = sorted(folder.glob("life-*.db"))
        for old in olds[: max(0, len(olds) - self.backup_keep)]:
            old.unlink(missing_ok=True)
        return str(dest)

    # ---------------- dashboard + status ----------------

    def status_dict(self) -> dict:
        sweep = self.store.get_setting("scheduler_last_sweep")
        return {
            "telegram": {
                **self.status,
                "configured": bool(self.tg),
                "chat_id_set": self.owner_chat is not None,
                "last_poll": self.clock.when(self.status["last_poll"]) if self.status["last_poll"] else None,
            },
            "scheduler_last_sweep": self.clock.when(sweep) if sweep else None,
            "scheduler_ok": bool(sweep) and utc_now() - from_iso(sweep) < timedelta(minutes=2),
            "folder": folder_status(self.cfg),
            "full_disk_access_target": full_disk_access_target(),
            "timezone": self.clock.tz_name,
            "coordinator_model": self.cfg["llm"]["tiers"][self.coordinator_tier]["model"],
            "area_model": self.cfg["llm"]["tiers"].get(self.life.agents.tier_name, {}).get("model"),
            "checkins": [
                {"key": c.key, "label": c.label, "next": self.clock.when(self.clock.next_slot(c.time, c.days))}
                for c in self.checkins
            ],
        }

    def dashboard(self) -> dict:
        when = self.clock.when
        s = self.store
        now = self.clock.now()
        return {
            "now": self.clock.label_now(),
            "date_label": f"{now:%A %d %B}",
            "hour": now.hour,
            "areas": [
                {"id": a.id, "name": a.name, "emoji": a.emoji, "folder": a.folder, "blurb": a.blurb}
                for a in self.areas
            ],
            "upcoming": [
                {"id": r["id"], "area": r["area"], "text": r["text"], "when": when(r["due_at"]),
                 "recurrence": r["recurrence"]}
                for r in s.upcoming_reminders(days=7, limit=30)
            ],
            "awaiting": [
                {"id": r["id"], "area": r["area"], "text": r["text"], "when": when(r["last_sent_at"] or r["due_at"])}
                for r in s.awaiting_reminders(8)
            ],
            "items": [
                {"id": i["id"], "area": i["area"], "kind": i["kind"], "title": i["title"],
                 "priority": i["priority"], "due_date": i["due_date"]}
                for i in s.open_items(limit=200)
            ],
            "logs": [
                {"id": l["id"], "area": l["area"], "metric": l["metric"], "value": l["value"],
                 "unit": l["unit"], "note": l["note"], "when": when(l["logged_at"])}
                for l in s.get_logs(days=14, limit=60)
            ],
            "messages": [
                {"id": m["id"], "direction": m["direction"], "source": m["source"], "area": m["area"],
                 "text": m["text"], "when": when(m["created_at"])}
                for m in s.recent_messages(25)
            ],
            "default_due": self.clock.input_value((now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)),
            "status": self.status_dict(),
        }
