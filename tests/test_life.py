"""Tests for the life organiser (src/life) and the Session hooks it relies on.

No network: Telegram and Claude are faked. Run with `make test`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.context import Conversation
from src.life.agents import AreaAgents
from src.life.areas import ensure_folders, folder_status, load_areas, read_context
from src.life.clock import UTC, Clock, from_iso, parse_days, to_iso, utc_now
from src.life.store import LifeStore
from src.life.telegram import chunks, reminder_keyboard
from src.life.tools import build_life_tools
from src.llm.anthropic_client import request_options
from src.tools import Tool, run_tool

TZ = "Australia/Sydney"


# ---------------------------------------------------------------- fixtures


def make_cfg(root: Path) -> dict:
    return {
        "llm": {
            "max_history_turns": 10,
            "tiers": {
                "default": {"provider": "anthropic", "model": "claude-haiku-4-5"},
                "hard": {"provider": "anthropic", "model": "claude-sonnet-5"},
                "coordinator": {"provider": "anthropic", "model": "claude-opus-5-5", "thinking": "adaptive",
                                "effort": "medium", "max_tokens": 8000, "fallbacks": "default"},
                "area": {"provider": "anthropic", "model": "claude-sonnet-5-5", "thinking": "adaptive",
                         "effort": "medium", "max_tokens": 8000, "fallbacks": "default"},
            },
            "pricing": {"claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.1}},
        },
        "workspace": {"path": str(root / "iCloud" / "Jarvis"), "trash_subdir": "_trash"},
        "life": {
            "owner": "Francesco",
            "timezone": TZ,
            "inbox": "Inbox",
            "subagent_max_rounds": 4,
            "subagent_timeout_s": 60,
            "areas": [
                {"id": "work", "name": "Work", "emoji": "💼", "folder": "Work", "blurb": "Job", "focus": "Run work."},
                {"id": "fitness", "name": "Fitness", "emoji": "🏋️", "folder": "Fitness", "blurb": "Training",
                 "focus": "Coach."},
                {"id": "health", "name": "Health", "emoji": "🩺", "folder": "Health", "blurb": "Sleep",
                 "focus": "Health."},
            ],
            "checkins": [
                {"key": "morning-brief", "label": "Daily 07:30 · brief", "time": "07:30", "days": "daily",
                 "task": "Brief."},
            ],
        },
    }


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def clock():
    return Clock(TZ)


@pytest.fixture
def store(tmp_path, clock):
    return LifeStore(tmp_path / "life.db", clock)


# ---------------------------------------------------------------- clock


def test_parse_variants(clock):
    assert clock.local(clock.parse("2026-11-25T09:00")).strftime("%H:%M") == "09:00"
    assert clock.local(clock.parse("2026-11-25 9:05")).strftime("%H:%M") == "09:05"
    assert clock.local(clock.parse("2026-11-25T09:00:30")).strftime("%H:%M:%S") == "09:00:30"
    assert clock.local(clock.parse("2026-11-25")).strftime("%H:%M") == "09:00"  # bare date = 9am
    assert clock.parse("2026-11-25T09:00:00Z") == datetime(2026, 11, 25, 9, tzinfo=UTC)
    assert clock.parse("2026-11-25T09:00+01:00") == datetime(2026, 11, 25, 8, tzinfo=UTC)
    assert clock.parse("next tuesday") is None
    assert clock.parse("") is None
    assert clock.parse_date("2026-02-30") is None
    assert clock.parse_date("2026-02-28") == "2026-02-28"


def test_recurrence_dst_weekdays_monthly(clock):
    due = clock.parse("2026-10-02T09:00")  # Fri; Sydney DST starts Sun 4 Oct
    nxt = clock.next_occurrence(due, "daily", clock.parse("2026-10-04T10:00"))
    assert clock.local(nxt).strftime("%a %d %H:%M %z") == "Mon 05 09:00 +1100"
    assert clock.local(clock.next_occurrence(due, "weekdays", due)).strftime("%a %d") == "Mon 05"
    assert clock.local(clock.next_occurrence(due, "weekly", due)).strftime("%a %d") == "Fri 09"
    assert clock.next_occurrence(due, "none", due) is None
    jan31 = clock.parse("2026-01-31T08:00")
    feb = clock.next_occurrence(jan31, "monthly", jan31, anchor_day=31)
    mar = clock.next_occurrence(feb, "monthly", feb, anchor_day=31)
    assert (clock.local(feb).day, clock.local(mar).day) == (28, 31)  # no drift to the 28th


def test_slots_and_days(clock):
    assert parse_days("daily") is None
    assert parse_days("weekdays") == [0, 1, 2, 3, 4]
    assert parse_days(["sun"]) == [6]
    assert parse_days("mon, wed") == [0, 2]
    last = clock.last_slot("07:30", None)
    nxt = clock.next_slot("07:30", None)
    assert last <= utc_now() < nxt
    assert nxt - last == timedelta(days=1) or clock.local(nxt).date() != clock.local(last).date()
    assert clock.local(clock.next_slot("19:00", [6])).weekday() == 6


def test_iso_roundtrip():
    now = utc_now().replace(microsecond=0)
    assert from_iso(to_iso(now)) == now


# ---------------------------------------------------------------- store


def backdate(store, rid, when):
    """Simulate time passing: a reminder created earlier is now due at `when`."""
    with store._db() as c:
        c.execute("update reminders set due_at = ? where id = ?", (to_iso(when), rid))


def test_reminders_lifecycle(store, clock):
    past = utc_now() - timedelta(minutes=1)
    one = store.add_reminder("health", "vitamin D", past)
    daily = store.add_reminder("fitness", "stretch", utc_now() + timedelta(minutes=5), "daily")
    backdate(store, daily["id"], past)  # it came due a minute ago
    later = store.add_reminder("work", "report", utc_now() + timedelta(hours=3))
    assert [r["id"] for r in store.upcoming_reminders()] == [one["id"], daily["id"], later["id"]]
    assert len(store.upcoming_reminders(area="work")) == 1

    claimed = store.claim_due()
    assert {r["id"] for r in claimed} == {one["id"], daily["id"]}
    assert store.claim_due() == []  # claimed rows are not handed out twice
    for r in claimed:
        store.mark_sent(r)
    assert store.get_reminder(one["id"])["status"] == "sent"
    d = store.get_reminder(daily["id"])
    assert d["status"] == "pending" and from_iso(d["due_at"]) > utc_now()
    assert round((from_iso(d["due_at"]) - past).total_seconds() / 3600) == 24
    assert [r["id"] for r in store.awaiting_reminders()] == [one["id"]]

    # failed send -> released; stuck 'sending' rows recover after 10 minutes
    flaky = store.add_reminder("work", "rent", past)
    [c] = store.claim_due()
    store.release(c["id"])
    assert store.get_reminder(flaky["id"])["status"] == "pending"
    [c] = store.claim_due()
    with store._db() as conn:
        conn.execute("update reminders set updated_at = ? where id = ?",
                     (to_iso(utc_now() - timedelta(minutes=11)), c["id"]))
    assert [r["id"] for r in store.claim_due()] == [flaky["id"]]

    assert store.snooze(one["id"], utc_now() + timedelta(hours=1))["status"] == "pending"
    copy = store.snooze(daily["id"], utc_now() + timedelta(hours=1))
    assert copy["id"] != daily["id"] and copy["recurrence"] == "none"
    assert store.complete(one["id"])["status"] == "done"
    assert store.complete(daily["id"])["status"] == "pending"  # repeating: untouched
    store.mark_sent(store.get_reminder(flaky["id"]))
    assert store.update_reminder(flaky["id"], due=utc_now() + timedelta(hours=2))["status"] == "pending"
    assert store.update_reminder(later["id"], status="cancelled")["status"] == "cancelled"
    assert store.update_reminder(later["id"], text="x")["status"] == "cancelled"
    assert store.update_reminder(9999, text="x") is None
    with pytest.raises(ValueError):
        store.add_reminder("work", "x", past, "hourly")


def test_items_logs_messages(store):
    t = store.add_item("work", "task", "spike", due_date="2026-11-20", priority=1)
    g = store.add_item("work", "goal", "ship MVP")
    assert t["due_date"] == "2026-11-20" and g["due_date"] is None
    assert store.update_item(t["id"], due_date=None)["due_date"] is None
    assert store.update_item(t["id"], due_date="2026-12-01")["due_date"] == "2026-12-01"
    assert store.update_item(t["id"], title="renamed")["due_date"] == "2026-12-01"
    assert store.update_item(t["id"], status="done")["completed_at"] is not None
    assert len(store.recently_done()) == 1 and len(store.open_items()) == 1
    assert store.update_item(t["id"], status="open")["completed_at"] is None
    with pytest.raises(ValueError):
        store.update_item(t["id"], priority=7)
    with pytest.raises(ValueError):
        store.add_item("work", "idea", "x")

    store.add_log("fitness", "Workout", 45, "min", "run")
    store.add_log("health", "sleep hours", 7.5)
    assert len(store.get_logs(metric="workout")) == 1
    assert store.get_logs(area="health")[0]["metric"] == "sleep_hours"
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            store.add_log("health", "x", bad)
    with pytest.raises(ValueError):
        store.add_log("health", "  ", 1)

    store.log_message("in", "user", "morning")
    assert store.unseen_outbound() == []
    store.log_message("out", "checkin", "how was today?")
    store.log_message("out", "chat", "a chat reply")
    store.log_message("out", "reminder", "⏰ stretch (reminder #2)")
    assert [m["source"] for m in store.unseen_outbound()] == ["checkin", "reminder"]
    store.log_message("in", "user", "7h, ran 30 min")
    assert store.unseen_outbound() == []


def test_overview_today_settings_backup(store, tmp_path):
    store.add_item("work", "task", "due thing", due_date=store.clock.today().isoformat())
    store.add_reminder("work", "soon", utc_now() + timedelta(hours=1))
    ov = store.overview()
    assert TZ in ov["now"] and ov["reminder_sender_last_ran"] == "never"
    assert ov["open_items"][0]["title"] == "due thing" and "details" not in ov["open_items"][0]
    assert store.overview("fitness")["open_items"] == []
    text = store.today_text()
    assert "soon" in text and "due thing" in text
    store.set_setting("k", {"a": 1})
    assert store.get_setting("k") == {"a": 1} and store.get_setting("nope", 3) == 3
    dest = store.backup_to(tmp_path / "backups" / "life.db")
    assert dest.exists() and LifeStore(dest, store.clock).open_items()[0]["title"] == "due thing"


# ---------------------------------------------------------------- folders


def test_folders(cfg):
    done = ensure_folders(cfg)
    root = Path(done["root"])
    assert {"Inbox/", "Work/", "Fitness/", "Health/", "_backups/", "Work/CONTEXT.md", "README.md"} <= set(done["created"])
    ctx = root / "Fitness" / "CONTEXT.md"
    ctx.write_text("# mine")
    assert ensure_folders(cfg)["created"] == []  # idempotent, never overwrites
    assert ctx.read_text() == "# mine"
    st = folder_status(cfg)
    assert st["exists"] and st["writable"] and all(st["areas"].values())
    area = load_areas(cfg)[1]
    assert read_context(cfg, area) == "# mine"


# ---------------------------------------------------------------- tools


def test_life_tools_global_and_fixed(cfg, store, clock):
    areas = load_areas(cfg)
    jarvis = {t.name: t for t in build_life_tools(store, clock, areas)}
    fit = {t.name: t for t in build_life_tools(store, clock, areas, fixed=areas[1])}
    assert "area" in jarvis["life_add_reminder"].parameters["properties"]
    assert "area" not in fit["life_add_reminder"].parameters["properties"]

    out, err = jarvis["life_add_reminder"].run({"area": "work", "text": "call", "due_at": "2020-01-01T09:00"})
    assert err and "past" in out
    out, err = jarvis["life_add_reminder"].run({"area": "nope", "text": "x", "due_at": "2099-01-01T09:00"})
    assert err and "area must be one of" in out
    future = (clock.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    out, err = fit["life_add_reminder"].run({"text": "run", "due_at": future, "recurrence": "weekly"})
    assert not err and "🏋️ Fitness" in out and "repeats weekly" in out
    work_r = store.add_reminder("work", "secret work thing", utc_now() + timedelta(hours=5))
    out, err = fit["life_update_reminder"].run({"id": work_r["id"], "status": "cancelled"})
    assert err and "belongs to work" in out  # area agents can't touch other areas
    out, err = jarvis["life_update_reminder"].run({"id": work_r["id"], "due_at": future})
    assert not err

    out, err = fit["life_log"].run({"metric": "workout", "value": 30, "unit": "min"})
    assert not err and "workout 30 min" in out
    out, err = fit["life_get_logs"].run({})
    assert not err and json.loads(out)[0]["metric"] == "workout"
    out, err = jarvis["life_get_logs"].run({"area": "health"})
    assert not err and out == "no logs match"
    out, err = fit["life_add_item"].run({"kind": "goal", "title": "10k under 50", "due_date": "2027-03-01"})
    assert not err and "due 2027-03-01" in out
    item_id = store.open_items(area="fitness")[0]["id"]
    out, err = fit["life_update_item"].run({"id": item_id, "due_date": "none"})
    assert not err and "due" not in out
    out, err = jarvis["life_overview"].run({"area": "fitness"})
    assert not err and json.loads(out)["open_items"][0]["title"] == "10k under 50"
    out, err = fit["life_overview"].run({"area": "work"})  # extra arg dropped by the schema filter
    assert not err and all(i["area"] == "fitness" for i in json.loads(out)["open_items"])


# ---------------------------------------------------------------- request options


def test_request_options():
    assert request_options({"model": "claude-opus-5-5", "thinking": "adaptive", "effort": "medium"}) == {
        "thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}
    assert request_options({"model": "claude-opus-5-5"}) == {"output_config": {"effort": "low"}}
    assert request_options({"model": "claude-sonnet-5-5"})["thinking"] == {"type": "between_tools"}
    assert request_options({"model": "claude-sonnet-5"}) == {"thinking": {"type": "disabled"}}
    assert request_options({"model": "claude-haiku-4-5"}) == {}
    fb = request_options({"model": "claude-sonnet-5-5", "thinking": "adaptive", "fallbacks": "default"})
    assert fb["extra_headers"] == {"anthropic-beta": "server-side-fallback-2026-07-01"}
    assert fb["extra_body"] == {"fallbacks": "default"}


# ---------------------------------------------------------------- area agents


def _block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


class FakeAnthropic:
    """Scripted messages.create: returns the queued responses in order and
    records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.options = []
        self.messages = self

    def with_options(self, **kw):
        self.options.append(kw)
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _resp(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks),
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                                 cache_creation_input_tokens=0, cache_read_input_tokens=0))


def _agents(cfg, store, clock, fake):
    ensure_folders(cfg)
    base = [Tool("get_datetime", "now", {"type": "object", "properties": {}}, lambda: "noon"),
            Tool("remember", "store a memory", {"type": "object", "properties": {}}, lambda: "ok")]
    agents = AreaAgents(cfg, store, clock, load_areas(cfg), base, Path(cfg["workspace"]["path"]) / "agents.log")
    agents._client = fake
    return agents


def test_area_agent_runs_tools_in_its_own_folder(cfg, store, clock):
    future = (clock.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    fake = FakeAnthropic([
        _resp("tool_use",
              _block("text", text=""),
              _block("tool_use", id="t1", name="ws_create_file",
                     input={"name": "training-plan.md", "content": "# Plan\nMon: run"}),
              _block("tool_use", id="t2", name="life_add_reminder", input={"text": "Run 5k", "due_at": future}),
              _block("tool_use", id="t3", name="ws_create_file",
                     input={"name": "../Health/sneaky.md", "content": "x"})),
        _resp("end_turn", _block("text", text="Wrote training-plan.md and set a reminder.")),
    ])
    agents = _agents(cfg, store, clock, fake)
    report = agents.run("fitness", "Make a training plan")
    assert report.startswith("Wrote training-plan.md and set a reminder.\n\nVerified changes:")
    # only what really happened is listed: the file, the reminder, not the blocked escape
    changes = report.split("Verified changes:")[1]
    assert "training-plan.md" in changes and "Run 5k" in changes and "sneaky" not in changes
    root = Path(cfg["workspace"]["path"])
    assert (root / "Fitness" / "training-plan.md").read_text().startswith("# Plan")
    assert not (root / "Health" / "sneaky.md").exists()  # confined to its own folder
    assert store.upcoming_reminders(area="fitness")[0]["text"] == "Run 5k"

    first = fake.requests[0]
    names = {t["name"] for t in first["tools"]}
    assert {"ws_create_file", "life_add_reminder", "life_overview", "get_datetime"} <= names
    assert "remember" not in names and "delegate" not in names
    assert first["model"] == "claude-sonnet-5-5"
    assert first["thinking"] == {"type": "adaptive"} and first["output_config"] == {"effort": "medium"}
    assert first["extra_body"] == {"fallbacks": "default"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "CONTEXT.md" in first["messages"][0]["content"] and "Make a training plan" in first["messages"][0]["content"]
    results = fake.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2", "t3"]
    assert results[2].get("is_error") is True and "outside the workspace" in results[2]["content"]


def test_area_agent_limits_and_refusal(cfg, store, clock):
    loop_forever = [_resp("tool_use", _block("tool_use", id=f"t{i}", name="get_datetime", input={}))
                    for i in range(10)]
    agents = _agents(cfg, store, clock, FakeAnthropic(loop_forever))
    assert "partial" in agents.run("work", "loop")
    assert len(agents._client.requests) == 5  # max_rounds=4 -> 5 requests, then stops

    agents = _agents(cfg, store, clock, FakeAnthropic([_resp("refusal")]))
    assert "declined" in agents.run("health", "x")
    assert "unknown area" in agents.run("nope", "x")
    tool = agents.delegate_tool()
    assert tool.timeout == 120 and tool.parameters["properties"]["area"]["enum"] == ["work", "fitness", "health"]
    assert "still working" in tool.timeout_message


def test_area_agent_deadline_and_api_errors(cfg, store, clock):
    fake = FakeAnthropic([_resp("end_turn", _block("text", text="ok"))])
    agents = _agents(cfg, store, clock, fake)
    assert agents.run("work", "x") == "ok\n\nVerified changes: none."
    # every model call is held to the time left (and the whole run to timeout_s)
    assert 0 < fake.options[0]["timeout"] <= 60
    agents.timeout_s = 5  # less than the 10s floor: stop before calling the model
    assert "partial" in agents.run("work", "x") and len(fake.requests) == 1
    agents = _agents(cfg, store, clock, FakeAnthropic([RuntimeError("overloaded")]))
    assert "stopped early" in agents.run("work", "x")


# ---------------------------------------------------------------- telegram helpers


def test_chunks_and_keyboard():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = chunks(text, 1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts).replace("\n", "") == text.replace("\n", "")
    assert chunks("a" * 2500, 1000) == ["a" * 1000, "a" * 1000, "a" * 500]
    assert chunks("") == ["(empty)"]
    assert reminder_keyboard(7)["inline_keyboard"][0][1]["callback_data"] == "snooze:7:60"


# ---------------------------------------------------------------- service


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []
        self.edit_keyboards, self.keyboard_edits = [], []
        self.fail = False

    async def send(self, chat_id, text, keyboard=None, reply_to=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text, keyboard))
        self.replies_to = [*getattr(self, "replies_to", []), reply_to]
        return len(self.sent)

    async def typing(self, chat_id):
        pass

    async def answer_callback(self, cid, text=None):
        self.answers.append(text)

    async def edit_text(self, chat_id, mid, text, keyboard=None):
        self.edits.append(text)
        self.edit_keyboards.append(keyboard)

    async def edit_keyboard(self, chat_id, mid, keyboard):
        self.keyboard_edits.append(keyboard)

    async def close(self):
        pass


class FakeSession:
    def __init__(self, cfg, store, clock, reply="Noted."):
        from src.life import LifeContext

        self.cfg = cfg
        areas = load_areas(cfg)
        self.life = LifeContext(clock=clock, store=store, areas=areas,
                                agents=SimpleNamespace(tier_name="area"))
        self.reply = reply
        self.research_topic = None
        self.calls = []
        self.researched = []
        self.pending_research = None

    async def turn(self, text, on_text=None, *, conversation=None, tier=None, note=None, learn=True):
        self.calls.append(dict(text=text, conversation=conversation, tier=tier, note=note, learn=learn))
        return SimpleNamespace(text=self.reply, extra={"research_topic": self.research_topic})

    async def research(self, topic, progress=None, conversation=None):
        self.researched.append((topic, conversation))
        return Path(f"/tmp/{topic}.md"), f"summary of {topic}"


def _service(cfg, store, clock, monkeypatch, reply="Noted.", chat_id="42"):
    from src.life.service import LifeService

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
    svc = LifeService(FakeSession(cfg, store, clock, reply))
    svc.tg = FakeTelegram()
    return svc


def test_service_reminders_and_buttons(cfg, store, clock, monkeypatch):
    svc = _service(cfg, store, clock, monkeypatch)
    r = store.add_reminder("fitness", "stretch", utc_now() + timedelta(minutes=5), "daily")
    backdate(store, r["id"], utc_now() - timedelta(minutes=1))  # it came due a minute ago
    assert asyncio.run(svc.send_due_reminders()) == 1
    chat, text, kb = svc.tg.sent[0]
    assert chat == 42 and text.startswith("⏰ stretch") and "repeats daily" in text
    assert kb == reminder_keyboard(r["id"])
    assert store.get_reminder(r["id"])["status"] == "pending"  # rolled to tomorrow
    assert store.recent_messages(1)[0]["source"] == "reminder"

    one = store.add_reminder("work", "call", utc_now() - timedelta(minutes=1))
    svc.tg.fail = True
    assert asyncio.run(svc.send_due_reminders()) == 0
    assert store.get_reminder(one["id"])["status"] == "pending"  # released for retry
    svc.tg.fail = False
    asyncio.run(svc.send_due_reminders())
    q = {"id": "cb1", "data": f"done:{one['id']}", "message": {"chat": {"id": 42}, "message_id": 5, "text": "⏰ call"}}
    asyncio.run(svc._on_button(q))
    assert store.get_reminder(one["id"])["status"] == "done"
    assert svc.tg.answers[-1] == "✅ Done" and svc.tg.edits[-1].endswith("✅ Done")
    q2 = {"id": "cb2", "data": f"snooze:{r['id']}:tomorrow", "message": {"chat": {"id": 42}, "message_id": 6}}
    asyncio.run(svc._on_button(q2))
    assert "Snoozed until Tomorrow 09:00" in svc.tg.answers[-1]
    stranger = {"id": "cb3", "data": f"done:{r['id']}", "message": {"chat": {"id": 666}, "message_id": 1}}
    asyncio.run(svc._on_button(stranger))
    assert svc.tg.answers[-1] is None


def test_service_messages(cfg, store, clock, monkeypatch):
    svc = _service(cfg, store, clock, monkeypatch, reply="Reminder set for 6pm.")
    msg = lambda text, chat=42: {"chat": {"id": chat}, "text": text}  # noqa: E731
    asyncio.run(svc._on_message(msg("hello", chat=666)))
    assert svc.tg.sent == []  # strangers are ignored
    asyncio.run(svc._on_message(msg("/today")))
    assert "📅" in svc.tg.sent[-1][1]

    store.log_message("out", "checkin", "Evening check-in: how was today?")
    asyncio.run(svc._on_message(msg("7h sleep, ran 30 min")))
    call = svc.session.calls[-1]
    assert call["tier"] == "coordinator" and call["conversation"] is svc.text_convo
    assert "Evening check-in" in call["note"] and "Channel: telegram" in call["note"]
    assert svc.tg.sent[-1][1] == "Reminder set for 6pm."
    asyncio.run(svc._on_message(msg("and tomorrow?")))
    assert "Evening check-in" not in svc.session.calls[-1]["note"]  # only shown once
    sources = [m["source"] for m in store.recent_messages(4)]
    assert sources[:2] == ["chat", "user"]


def test_first_run_chat_id_helper(cfg, store, clock, monkeypatch):
    svc = _service(cfg, store, clock, monkeypatch, chat_id="")
    asyncio.run(svc._on_message({"chat": {"id": 1234}, "text": "/start"}))
    assert "TELEGRAM_CHAT_ID=1234" in svc.tg.sent[-1][1]
    assert asyncio.run(svc.send_due_reminders()) == 0  # nowhere to send yet


def test_checkins_schedule_and_nothing(cfg, store, clock, monkeypatch):
    svc = _service(cfg, store, clock, monkeypatch, reply="Good morning. Three things today.")
    started = []

    def fake_run(ci, *args, **kwargs):  # records at call time; the scheduled task itself is a no-op
        started.append(ci.key)
        return asyncio.sleep(0)

    svc._run_checkin_safe = fake_run
    asyncio.run(svc._due_checkins())
    assert started == []  # first run only records a baseline
    slot = clock.last_slot("07:30", None)
    store.set_setting("checkin_last:morning-brief", to_iso(slot - timedelta(days=1)))
    if utc_now() - slot <= timedelta(minutes=120):
        asyncio.run(svc._due_checkins())
        assert started == ["morning-brief"]
    else:
        asyncio.run(svc._due_checkins())
        assert started == []  # missed while "off": skipped, not sent late
    asyncio.run(svc._due_checkins())
    assert len(started) <= 1  # never twice for one slot

    text = asyncio.run(svc.run_checkin(svc.checkins[0]))
    assert text.startswith("Good morning") and svc.tg.sent[-1][1] == text
    assert svc.session.calls[-1]["learn"] is False
    svc.session.reply = "NOTHING"
    n = len(svc.tg.sent)
    asyncio.run(svc.run_checkin(svc.checkins[0]))
    assert len(svc.tg.sent) == n


def test_backup_rotation_and_dashboard(cfg, store, clock, monkeypatch):
    svc = _service(cfg, store, clock, monkeypatch)
    svc.backup_keep = 2
    folder = Path(cfg["workspace"]["path"]) / "_backups"
    folder.mkdir(parents=True)
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (folder / f"life-{d}.db").write_text("old")
    svc.backup()
    assert len(list(folder.glob("life-*.db"))) == 2
    store.add_reminder("work", "soon", utc_now() + timedelta(hours=1))
    d = svc.dashboard()
    assert d["upcoming"][0]["when"].startswith(("Today", "Tomorrow"))
    assert {"areas", "items", "logs", "messages", "status", "default_due"} <= set(d)
    assert d["status"]["telegram"]["configured"] and d["status"]["checkins"][0]["key"] == "morning-brief"


# ---------------------------------------------------------------- API


def test_api(cfg, store, clock, monkeypatch):
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    from src.life.api import make_router

    svc = _service(cfg, store, clock, monkeypatch, reply="On it.")

    def require_token(authorization: str = Header(default="")):
        if authorization != "Bearer t":
            raise HTTPException(401)

    app = FastAPI()
    app.include_router(make_router(lambda: svc, require_token))
    c = TestClient(app)
    h = {"Authorization": "Bearer t"}
    assert c.get("/life/dashboard").status_code == 401
    assert c.get("/life/dashboard", headers=h).json()["areas"][0]["id"] == "work"
    future = (clock.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    rid = c.post("/life/reminders", headers=h, json={"area": "work", "text": "x", "due": future}).json()["id"]
    assert c.post("/life/reminders", headers=h, json={"area": "zzz", "text": "x", "due": future}).status_code == 400
    assert c.post(f"/life/reminders/{rid}/snooze", headers=h).status_code == 200
    assert c.post(f"/life/reminders/{rid}/explode", headers=h).status_code == 400
    assert c.post("/life/reminders/9999/done", headers=h).status_code == 404
    iid = c.post("/life/items", headers=h, json={"area": "work", "title": "do it"}).json()["id"]
    assert c.post(f"/life/items/{iid}/done", headers=h).status_code == 200
    assert c.post("/life/logs", headers=h, json={"area": "health", "metric": "mood", "value": 4}).status_code == 200
    assert c.post("/life/ask", headers=h, json={"text": "hi"}).json() == {"text": "On it."}
    assert c.post("/life/test-telegram", headers=h).status_code == 200
    assert c.post("/life/checkins/morning-brief/run", headers=h).json() == {"started": True}
    assert c.get("/life/status", headers=h).json()["timezone"] == TZ


# ---------------------------------------------------------------- Session hooks


def _bare_session(tools, llm):
    from src.main import Session

    s = object.__new__(Session)
    s.cfg = {"routing": {}, "llm": {"tiers": {}}}
    s.system_prompt = "sys"
    s.llm = llm
    s.tools = tools
    s.conversation = Conversation(10)
    s.total_cost = 0.0
    s._writebacks = set()
    s.pending_research = None
    s.memory = SimpleNamespace(search=lambda q: [], add_turn=lambda u, a: s.__dict__.setdefault("learned", []).append(u))
    s.knowledge = SimpleNamespace(search=lambda q: [])
    return s


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def chat(self, system, messages, tier="default", tools=None, on_text=None):
        from src.llm.base import LLMResponse

        self.calls.append(dict(messages=list(messages), tier=tier))
        return self.script.pop(0) if self.script else LLMResponse(text="done", model="m", tier=tier)

    def tool_messages(self, resp, results):
        return [{"role": "assistant", "content": "calls"},
                {"role": "user", "content": [{"id": c.id, "out": o, "err": e} for c, o, e in results]}]


def test_session_turn_hooks(monkeypatch):
    import src.profile

    from src.llm.base import LLMResponse, ToolCall

    monkeypatch.setattr(src.profile, "profile_for_prompt", lambda cfg: "")
    slow = lambda: (time.sleep(1.0), "slow done")[1]  # noqa: E731
    tools = [
        Tool("a", "a", {"type": "object", "properties": {}}, slow),
        Tool("b", "b", {"type": "object", "properties": {}}, slow),
        Tool("hang", "h", {"type": "object", "properties": {}}, lambda: time.sleep(3), timeout=0.5),
    ]
    first = LLMResponse(text="", model="m", tier="coordinator",
                        tool_calls=[ToolCall("1", "a", {}), ToolCall("2", "b", {}), ToolCall("3", "hang", {})])
    llm = ScriptedLLM([first, LLMResponse(text="All done.", model="m", tier="coordinator")])
    s = _bare_session(tools, llm)
    side = Conversation(10)

    async def timed():
        # time the turn itself: asyncio.run() also waits for the hung tool's
        # thread at shutdown, which isn't part of the turn
        t0 = time.monotonic()
        r = await s.turn("plan my week", conversation=side, tier="coordinator", note="NOTE", learn=False)
        return r, time.monotonic() - t0

    resp, took = asyncio.run(timed())
    assert resp.text == "All done."
    assert took < 2.5, f"tools ran sequentially ({took:.1f}s)"
    results = llm.calls[1]["messages"][-1]["content"]
    assert [r["id"] for r in results] == ["1", "2", "3"]
    assert results[2]["err"] is True and "timed out" in results[2]["out"]
    assert llm.calls[0]["tier"] == "coordinator"
    assert llm.calls[0]["messages"][-1]["content"].startswith("NOTE\n\n")
    assert [m["role"] for m in side.messages] == ["user", "assistant"]
    assert s.conversation.messages == []  # the voice conversation is untouched
    assert "learned" not in s.__dict__  # learn=False: no memory extraction

    asyncio.run(s.turn("hi there friend"))
    assert len(s.conversation.messages) == 2


# ---------------------------------------------------------------- review fixes


def test_reminder_rearm_and_roll_forward(store, clock):
    # a fired one-off made repeating comes back at its next occurrence
    r = store.add_reminder("health", "pills", utc_now() - timedelta(minutes=1))
    [c] = store.claim_due()
    store.mark_sent(c)
    u = store.update_reminder(r["id"], recurrence="daily")
    assert u["status"] == "pending" and from_iso(u["due_at"]) > utc_now()
    # a repeating reminder whose first time is past starts at the next one, not now
    past = utc_now() - timedelta(hours=2)
    d = store.add_reminder("fitness", "stretch", past, "daily")
    assert from_iso(d["due_at"]) > utc_now() and store.claim_due() == []
    assert round((from_iso(d["due_at"]) - past).total_seconds() / 3600) == 24
    # skip_occurrence moves a series one step; one-offs aren't affected
    s = store.skip_occurrence(d["id"])
    assert round((from_iso(s["due_at"]) - from_iso(d["due_at"])).total_seconds() / 3600) == 24
    one = store.add_reminder("work", "x", utc_now() + timedelta(hours=1))
    assert store.skip_occurrence(one["id"]) is None


def test_unseen_outbound_ignores_buttons_and_commands(store):
    store.log_message("in", "user", "hi")
    store.log_message("out", "checkin", "How was today?")
    store.log_message("in", "button", "✅ Done: pills")
    store.log_message("in", "command", "/today")
    assert [m["text"] for m in store.unseen_outbound()] == ["How was today?"]


def test_workspace_unreachable_and_hygiene(tmp_path):
    import threading

    from src.tools import workspace as W

    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    bad = {"workspace": {"path": str(blocker / "Jarvis")}}
    tools = {t.name: t for t in W.build(bad)}  # must not touch the filesystem
    out, err = tools["ws_list"].run({})
    assert err and "can't reach the workspace" in out

    cfg = {"workspace": {"path": str(tmp_path / "ws"), "trash_subdir": "_trash"}}
    W.create_file(cfg, "Fitness/plan.md", "# plan\n")
    (tmp_path / "ws" / "_backups").mkdir()
    (tmp_path / "ws" / "_backups" / "life-2026-01-01.db").write_bytes(b"SQLite\x00\x00")
    (tmp_path / "ws" / ".DS_Store").write_bytes(b"\x00")
    (tmp_path / "ws" / "Work").mkdir()
    (tmp_path / "ws" / "Work" / ".notes.md.icloud").write_text("placeholder")
    (tmp_path / "ws" / "blob.bin").write_bytes(b"\x00\x01")
    names = [f["name"] for f in json.loads(W.list_files(cfg))]
    assert names == ["Fitness/plan.md", "blob.bin"]
    with pytest.raises(W.WorkspaceError):
        W.read_file(cfg, "blob.bin")

    # parallel edits to one file are serialised: none is lost
    threads = [
        threading.Thread(target=W.edit_file, args=(cfg, "Fitness/plan.md", "", f"line {i}"))
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    text = W.read_file(cfg, "Fitness/plan.md")
    assert all(f"line {i}" in text for i in range(20))


def test_full_disk_access_target():
    import sysconfig

    from src.life.areas import full_disk_access_target

    target = full_disk_access_target()
    if sysconfig.get_config_var("PYTHONFRAMEWORK"):
        assert target.endswith("Resources/Python.app")
    assert Path(target).exists()


def test_poll_conflict_and_research_routing(cfg, store, clock, monkeypatch):
    from src.life.telegram import TelegramError

    svc = _service(cfg, store, clock, monkeypatch)
    deleted = []

    async def get_me():
        return {"username": "b"}

    async def delete_webhook():
        deleted.append(1)

    async def updates(offset, timeout=50):
        raise TelegramError("getUpdates: Conflict: terminated by other getUpdates request", 409)

    svc.tg.get_me, svc.tg.delete_webhook, svc.tg.get_updates = get_me, delete_webhook, updates

    async def run_briefly():
        task = asyncio.ensure_future(svc._poll_loop())
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(run_briefly())
    assert svc.status["telegram"].startswith("conflict") and deleted == []

    # research asked for in a Telegram turn reports back on Telegram, into the text history
    svc.session.research_topic = "e-ink tablets"

    async def chat_then_wait():
        await svc.chat("research e-ink tablets")
        await asyncio.sleep(0.05)

    asyncio.run(chat_then_wait())
    assert svc.session.researched == [("e-ink tablets", svc.text_convo)]
    assert "Research done" in svc.tg.sent[-1][1]


def test_telegram_send_retries(monkeypatch):
    import src.life.telegram as T

    monkeypatch.setattr(T, "RETRY_DELAYS", (0, 0, 0))
    tg = T.TelegramClient("x")
    attempts = []

    async def flaky(method, **params):
        attempts.append(method)
        if len(attempts) < 3:
            raise T.TelegramError("Too Many Requests", 429, retry_after=0)
        return {"message_id": 7}

    tg.call = flaky
    assert asyncio.run(tg.send(1, "hi")) == 7 and len(attempts) == 3

    async def bad(method, **params):
        attempts.append(method)
        raise T.TelegramError("Bad Request: chat not found", 400)

    tg.call = bad
    attempts.clear()
    with pytest.raises(T.TelegramError):
        asyncio.run(tg.send(1, "hi"))
    assert len(attempts) == 1  # not retried


def test_dashboard_buttons_semantics(cfg, store, clock, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.life.api import make_router

    svc = _service(cfg, store, clock, monkeypatch)
    app = FastAPI()
    app.include_router(make_router(lambda: svc, lambda: None))
    c = TestClient(app)
    sunday = utc_now() + timedelta(days=3)
    one = store.add_reminder("work", "call mum", sunday)
    assert c.post(f"/life/reminders/{one['id']}/snooze").status_code == 200
    # +1h means an hour after its own time, never earlier than planned
    assert from_iso(store.get_reminder(one["id"])["due_at"]) == (sunday + timedelta(hours=1)).replace(microsecond=0)
    daily = store.add_reminder("health", "pills", utc_now() + timedelta(hours=2), "daily")
    c.post(f"/life/reminders/{daily['id']}/snooze")
    moved = store.get_reminder(daily["id"])
    assert round((from_iso(moved["due_at"]) - from_iso(daily["due_at"])).total_seconds() / 3600) == 24
    assert any(r["text"] == "pills" and r["recurrence"] == "none" for r in store.upcoming_reminders())
    before = store.get_reminder(daily["id"])["due_at"]
    c.post(f"/life/reminders/{daily['id']}/done")
    assert store.get_reminder(daily["id"])["due_at"] > before  # done early skips this occurrence

    async def refuse(*a, **k):
        raise RuntimeError("Bad Request: chat not found")

    svc.tg.send = refuse
    r = c.post("/life/test-telegram")
    assert r.status_code == 400 and "chat not found" in r.json()["detail"]


def test_session_research_stays_with_its_turn(monkeypatch):
    import src.profile

    from src.llm.base import LLMResponse, ToolCall
    from src.main import _turn_state

    monkeypatch.setattr(src.profile, "profile_for_prompt", lambda cfg: "")

    def ask_research():
        state = _turn_state.get()
        state["research"] = "solar panels"
        return "started"

    tools = [Tool("deep_research", "r", {"type": "object", "properties": {}}, ask_research)]

    def script():
        return [LLMResponse(text="", model="m", tier="t", tool_calls=[ToolCall("1", "deep_research", {})]),
                LLMResponse(text="On it.", model="m", tier="t")]

    s = _bare_session(tools, ScriptedLLM(script()))
    resp = asyncio.run(s.turn("research solar panels", conversation=Conversation(4), tier="coordinator"))
    assert resp.extra["research_topic"] == "solar panels" and s.pending_research is None
    s.llm = ScriptedLLM(script())
    resp = asyncio.run(s.turn("research solar panels please now"))
    assert s.pending_research == "solar panels"  # voice callers keep reading this


def test_system_timezone_default():
    from zoneinfo import ZoneInfo

    from src.life import system_timezone

    tz = system_timezone()
    assert tz is None or ZoneInfo(tz)  # a real zone name, e.g. "Europe/London"


# ---------------------------------------------------------------- the board


def _buttons(keyboard) -> dict:
    """callback_data -> button text, for an inline keyboard."""
    return {b["callback_data"]: b["text"] for row in keyboard["inline_keyboard"] for b in row}


def test_board_store_projects_and_migration(tmp_path, clock):
    import sqlite3

    # a database from before the board: no stage/next_step columns
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.execute("create table items (id integer primary key autoincrement, area text not null, kind text not null "
                  "default 'task', title text not null, details text, status text not null default 'open', priority "
                  "integer not null default 2, due_date text, created_at text not null, updated_at text not null, "
                  "completed_at text)")
        c.execute("insert into items (area, title, created_at, updated_at) values ('work', 'old task', 'x', 'x')")
    st = LifeStore(old, clock)
    assert st.get_item(1)["title"] == "old task" and st.get_item(1)["stage"] is None  # data kept

    p = st.add_item("work", "project", "Pitch deck", next_step="  Send draft to Sam ")
    assert p["stage"] == "todo" and p["next_step"] == "Send draft to Sam"
    assert st.add_item("work", "task", "x", stage="doing")["stage"] is None  # only projects get a stage
    st.update_item(p["id"], stage="in progress")
    assert st.get_item(p["id"])["stage"] == "doing"
    with pytest.raises(ValueError):
        st.update_item(p["id"], stage="someday")
    st.update_item(p["id"], status="done")
    assert st.projects() == []
    st.update_item(p["id"], stage="waiting")  # moving a finished project reopens it
    again = st.get_item(p["id"])
    assert again["status"] == "open" and again["stage"] == "waiting" and again["completed_at"] is None
    st.update_item(p["id"], next_step="")
    assert st.get_item(p["id"])["next_step"] is None
    q = st.add_item("fitness", "project", "Half marathon", stage="doing", next_step="Long run 12k")
    assert [x["title"] for x in st.projects()] == ["Half marathon", "Pitch deck"]  # doing before waiting
    assert "▶ Half marathon → Long run 12k" in st.today_text()
    assert any(i.get("stage") == "doing" for i in st.overview()["open_items"])
    assert q["id"]


def test_tap_logs_replace_todays_value(store):
    assert store.today_value("health", "sleep_hours") is None
    store.set_today_log("health", "sleep_hours", 7, "hours")
    store.set_today_log("health", "sleep_hours", 8, "hours")  # re-tap changes it, no duplicate
    logs = store.get_logs("health", "sleep_hours")
    assert len(logs) == 1 and logs[0]["value"] == 8 and store.today_value("health", "sleep_hours") == 8


def test_board_views(cfg, store):
    from src.life import board

    areas = load_areas(cfg)
    text, kb = board.board_view([], areas)
    assert "empty" in text and "pa:" in _buttons(kb)
    for i in range(4):
        store.add_item("work", "project", f"P{i}", stage="doing", next_step=f"step {i}", due_date="2026-12-05")
    store.add_item("fitness", "project", "Half marathon", stage="waiting")
    projects = store.projects()
    text, kb = board.board_view(projects, areas)
    assert text.startswith("📋 Board") and "▶ P0 → step 0 · 5 Dec" in text and "⏸ Half marathon" in text
    assert "⚠ 4 in Doing" in text  # focus nudge past 3
    assert _buttons(kb)["bd:work"] == "💼 Work (4)" and _buttons(kb)["bd:health"] == "🩺 Health"
    text, kb = board.board_view(projects, areas, "fitness")
    assert text.startswith("🏋️ Fitness") and "pj:" in "".join(_buttons(kb)) and "pa:fitness" in _buttons(kb)
    card, kb = board.project_card(projects[0], areas[0])
    b = _buttons(kb)
    assert "▶ Doing" in card and "Next: step 0" in card
    assert b[f"ps:{projects[0]['id']}:doing"].startswith("✓") and f"pn:{projects[0]['id']}" in b
    assert all(len(k.encode()) <= 64 for k in b)
    kb = board.log_keyboard({"sleep": 7.0, "mood": None, "trained": 0.0})
    b = _buttons(kb)
    assert b["lg:sleep:7"] == "✓7h" and b["lg:sleep:5"] == "😴 5h" and b["lg:trained:0"] == "✓Rest day"
    assert board.log_target("sleep", ["work", "life"]) == ("work", "sleep_hours", "hours")  # falls back


def test_service_menu_board_and_buttons(cfg, store, clock, monkeypatch):
    from src.life import board

    svc = _service(cfg, store, clock, monkeypatch)
    msg = lambda text, **kw: {"chat": {"id": 42}, "text": text, **kw}  # noqa: E731
    btn = lambda data, mid=9: {"id": "cb", "data": data, "message": {"chat": {"id": 42}, "message_id": mid}}  # noqa: E731

    asyncio.run(svc.announce_menu())
    assert svc.tg.sent[-1][2] == board.menu_keyboard()
    asyncio.run(svc.announce_menu())
    assert len(svc.tg.sent) == 1  # once per menu version

    # ➕ Add -> name by force-reply -> pick area -> card; no model calls anywhere
    asyncio.run(svc._on_message(msg(board.MENU_ADD)))
    prompt_id = len(svc.tg.sent)
    assert svc.tg.sent[-1][2]["force_reply"] is True
    asyncio.run(svc._on_message(msg("Pitch deck", reply_to_message={"message_id": prompt_id})))
    assert "Which area" in svc.tg.sent[-1][1] and "pc:work" in _buttons(svc.tg.sent[-1][2])
    asyncio.run(svc._on_button(btn("pc:work")))
    pid = store.projects()[0]["id"]
    assert store.get_item(pid)["title"] == "Pitch deck" and "○ To do" in svc.tg.edits[-1]

    asyncio.run(svc._on_button(btn(f"ps:{pid}:doing")))
    assert store.get_item(pid)["stage"] == "doing" and svc.tg.answers[-1] == "▶ Doing"
    asyncio.run(svc._on_button(btn(f"pn:{pid}")))
    prompt_id = len(svc.tg.sent)
    asyncio.run(svc._on_message(msg("unrelated chat")))  # not a reply: goes to Jarvis as usual
    assert svc.session.calls and store.get_item(pid)["next_step"] is None
    asyncio.run(svc._on_message(msg("Send draft to Sam", reply_to_message={"message_id": prompt_id})))
    assert store.get_item(pid)["next_step"] == "Send draft to Sam" and "Next: Send draft to Sam" in svc.tg.sent[-1][1]
    calls = len(svc.session.calls)

    asyncio.run(svc._on_message(msg(board.MENU_BOARD)))
    assert "▶ Pitch deck → Send draft to Sam" in svc.tg.sent[-1][1]
    asyncio.run(svc._on_button(btn("bd:work")))
    assert svc.tg.edits[-1].startswith("💼 Work") and f"pj:{pid}" in _buttons(svc.tg.edit_keyboards[-1])
    asyncio.run(svc._on_button(btn(f"ps:{pid}:done")))
    assert store.get_item(pid)["status"] == "done" and "↩ Reopen" in svc.tg.edit_keyboards[-1]["inline_keyboard"][0][0]["text"]
    asyncio.run(svc._on_button(btn("pj:999")))
    assert "no longer exists" in svc.tg.answers[-1]

    # 📝 Log: taps write today's values, re-tap replaces, keyboard ticks the choice
    asyncio.run(svc._on_message(msg(board.MENU_LOG)))
    asyncio.run(svc._on_button(btn("lg:sleep:7")))
    asyncio.run(svc._on_button(btn("lg:sleep:8")))
    asyncio.run(svc._on_button(btn("lg:mood:4")))
    assert store.today_value("health", "sleep_hours") == 8 and len(store.get_logs("health", "sleep_hours")) == 1
    assert _buttons(svc.tg.keyboard_edits[-1])["lg:mood:4"] == "✓4" and svc.tg.answers[-1] == "🙂 Mood 4/5 ✓"
    assert len(svc.session.calls) == calls  # none of the taps used the model
    assert store.unseen_outbound() == []  # taps don't leak into Jarvis's "you were sent" note


def test_checkin_buttons_and_delegate_contract(cfg, store, clock, monkeypatch):
    from src.life import board

    cfg["life"]["checkins"].append({"key": "evening", "label": "Daily 21:00", "time": "21:00", "days": "daily",
                                    "task": "How was today?", "buttons": "daily-log"})
    svc = _service(cfg, store, clock, monkeypatch, reply="How was today?")
    asyncio.run(svc.run_checkin(svc.checkin_by_key("evening")))
    assert svc.tg.sent[-1][2] == board.log_keyboard({"sleep": None, "mood": None, "trained": None})
    asyncio.run(svc.run_checkin(svc.checkin_by_key("morning-brief")))
    assert svc.tg.sent[-1][2] is None

    fake = FakeAnthropic([_resp("end_turn", _block("text", text="Plan ready."))])
    agents = _agents(cfg, store, clock, fake)
    tool = agents.delegate_tool()
    assert "expected_output" in tool.parameters["properties"] and "Verified changes" in tool.description
    tool.func(area="fitness", task="Plan my week", expected_output="the 3 sessions with days")
    first = fake.requests[0]
    assert "YOUR REPORT MUST INCLUDE: the 3 sessions with days" in first["messages"][0]["content"]
    assert "Line 1: the answer" in first["system"][0]["text"] and "stage and next step" in first["system"][0]["text"]


# ---------------------------------------------------------------- durable threads (LangGraph ideas)


def test_chat_thread_survives_a_restart(cfg, store, clock, monkeypatch):
    for direction, source, text in [
        ("in", "user", "a"), ("out", "chat", "A"),
        ("in", "user", "b"),  # its turn failed: no reply, dropped
        ("out", "checkin", "How was today?"), ("in", "command", "/today"),
        ("in", "user", "c"), ("out", "chat", "C"),
    ]:
        store.log_message(direction, source, text)
    assert store.thread_history(10) == [
        {"role": "user", "content": "a"}, {"role": "assistant", "content": "A"},
        {"role": "user", "content": "c"}, {"role": "assistant", "content": "C"},
    ]
    assert [m["content"] for m in store.thread_history(1)] == ["c", "C"]
    assert store.thread_history(0) == []
    svc = _service(cfg, store, clock, monkeypatch)  # a fresh process: the thread is back
    assert [m["content"] for m in svc.text_convo.messages] == ["a", "A", "c", "C"]


def test_area_agent_thread_and_cut_off_runs(cfg, store, clock):
    future = (clock.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    fake = FakeAnthropic([
        _resp("tool_use", _block("tool_use", id="t1", name="life_add_reminder",
                                 input={"text": "Long run", "due_at": future})),
        _resp("end_turn", _block("text", text="Plan set: long run tomorrow.\nQuestion: morning or evening?")),
        _resp("end_turn", _block("text", text="Moved to the morning.")),
    ])
    agents = _agents(cfg, store, clock, fake)
    agents.run("fitness", "Plan my training week")
    (run,) = store.recent_agent_runs("fitness")
    assert run["status"] == "done" and run["rounds"] == 1 and "Long run" in run["changes"][0]
    assert "Question: morning or evening?" in run["report"]

    agents.run("fitness", "He says morning")  # the follow-up sees the earlier run
    first = fake.requests[2]["messages"][0]["content"]
    assert "YOUR RECENT RUNS" in first and "Plan my training week" in first
    assert "Question: morning or evening?" in first and "Long run" in first
    assert first.index("YOUR RECENT RUNS") < first.index("TASK FROM JARVIS:\nHe says morning")
    assert "Verified changes" not in first  # the change list is shown once, as Changes
    assert "Your last few runs" in fake.requests[2]["system"][0]["text"]
    assert store.recent_agent_runs("work") == []  # threads are per area

    # a run whose process died mid-way (a restart): reported as cut off, with its changes
    cut = store.start_agent_run("health", "Book the dentist")
    store.add_agent_change(cut, 'Reminder #9 "Dentist" set for Fri 09:00')
    assert store.recent_agent_runs("health")[0]["status"] == "running"  # this process: still alive
    with store._db() as c:
        c.execute("update agent_runs set pid = null where id = ?", (cut,))
    assert store.recent_agent_runs("health")[0]["status"] == "interrupted"
    text = agents.recent_work(agents.by_id["health"])
    assert "CUT OFF" in text and "Dentist" in text and "Book the dentist" in text
    assert "remembers its last few runs" in agents.delegate_tool().description


def test_cut_off_telegram_reply_is_resumed(cfg, store, clock, monkeypatch):
    from src.life.service import INFLIGHT

    svc = _service(cfg, store, clock, monkeypatch, reply="Week planned.")
    asyncio.run(svc._on_message({"chat": {"id": 42}, "text": "hi", "message_id": 5}))
    assert store.get_setting(INFLIGHT) == []  # a finished turn leaves no checkpoint

    # a shutdown cancels a turn mid-way: its checkpoint stays behind
    class Stuck(FakeSession):
        async def turn(self, *a, **k):
            await asyncio.Event().wait()

    async def cut_off():
        task = asyncio.ensure_future(svc._on_message({"chat": {"id": 42}, "text": "plan my week", "message_id": 6}))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    svc.session = Stuck(cfg, store, clock)
    asyncio.run(cut_off())
    (left,) = store.get_setting(INFLIGHT)
    assert left["text"] == "plan my week" and left["resumed"] is False
    stale = {"key": "1", "text": "an old question from before the Mac slept", "resumed": False,
             "at": to_iso(utc_now() - timedelta(hours=3))}
    store.set_setting(INFLIGHT, [left, stale])

    svc = _service(cfg, store, clock, monkeypatch, reply="Week planned.")  # Jarvis starts again
    asyncio.run(svc.resume_cut_off())
    call = svc.session.calls[-1]
    assert call["text"] == "plan my week" and "restart cut off your first reply" in call["note"]
    sent = [t for _, t, _ in svc.tg.sent]
    assert "Week planned." in sent
    assert any(t.startswith('A restart cut off my reply to "an old question') for t in sent)
    assert store.get_setting(INFLIGHT) == []
    assert store.thread_history(5)[-2:] == [{"role": "user", "content": "plan my week"},
                                             {"role": "assistant", "content": "Week planned."}]

    # a retry that was cut off as well isn't tried a third time
    store.set_setting(INFLIGHT, [{**left, "resumed": True}])
    svc = _service(cfg, store, clock, monkeypatch)
    n = len(svc.session.calls)
    asyncio.run(svc.resume_cut_off())
    assert len(svc.session.calls) == n and svc.tg.sent[-1][1].startswith("A restart cut off my reply")


def test_cut_off_checkin_runs_again_once(cfg, store, clock, monkeypatch):
    svc = _service(cfg, store, clock, monkeypatch, reply="Brief: two things.")
    ci = svc.checkins[0]
    slot = clock.last_slot(ci.time, ci.days)
    store.set_setting(f"checkin_last:{ci.key}", to_iso(slot))  # this slot already started...
    marker = f"checkin_running:{ci.key}"
    store.set_setting(marker, {"slot": to_iso(utc_now() - timedelta(minutes=3)), "attempt": 1})  # ...and was cut off

    async def tick():
        await svc._due_checkins()
        await asyncio.gather(*(t for t in asyncio.all_tasks() if t is not asyncio.current_task()))

    asyncio.run(tick())
    assert svc.tg.sent[-1][1] == "Brief: two things." and store.get_setting(marker) is None
    asyncio.run(tick())
    assert len(svc.tg.sent) == 1  # not again for the same slot

    store.set_setting(marker, {"slot": to_iso(utc_now() - timedelta(minutes=3)), "attempt": 2})
    asyncio.run(tick())
    assert len(svc.tg.sent) == 1 and store.get_setting(marker) is None  # cut off twice: given up


# ---------------------------------------------------------------- project sources (read-only)


def _xlsx(path: Path) -> Path:
    """A minimal workbook: shared strings, a date-styled cell and a gap column."""
    import zipfile

    m = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {m} {r}><sheets><sheet name="Tracker" sheetId="1" r:id="rId1"/>'
                                      "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f"<sst {m}><si><t>Task</t></si><si><t>Agree contributions</t></si>"
                                           "<si><t>Due</t></si></sst>")
        z.writestr("xl/styles.xml", f'<styleSheet {m}><numFmts><numFmt numFmtId="164" formatCode="d mmm yyyy"/>'
                                    '</numFmts><cellXfs><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>')
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet {m}><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>2</v></c></row>'
                   '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="C2" s="1"><v>46341</v></c>'
                   '<c r="D2"><v>1750</v></c></row></sheetData></worksheet>')
    return path


def test_project_sources_are_read_only_and_safe(cfg, tmp_path, monkeypatch):
    import subprocess

    from src.life import sources

    proj = tmp_path / "proj"
    (proj / "node_modules").mkdir(parents=True)
    (proj / "docs").mkdir()
    (proj / "README.md").write_text("# Proj\nA thing.")
    (proj / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    (proj / ".env.example").write_text("ANTHROPIC_API_KEY=")
    (proj / "config.py").write_text('API_KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123"\nmax_tokens = 8000\n'
                                    "bot = '123456789:AAHabcdefghijklmnopqrstuvwxyz0123456'\n")
    (proj / "docs" / "long.md").write_text("\n".join(f"line {i} " + "x" * 80 for i in range(600)))
    _xlsx(proj / "tracker.xlsx")
    (tmp_path / "outside.txt").write_text("not yours")
    (proj / "escape.txt").symlink_to(tmp_path / "outside.txt")
    git = ["git", "-C", str(proj), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-qm", "First steps"], check=True)

    reg = tmp_path / "sources.yaml"
    reg.write_text(f"""
proj: {{area: work, about: My thing, path: "{proj}"}}
remote: {{area: work, about: Remote only, github: someone/remote}}
gym: {{area: fitness, about: Gym log, path: "{proj / 'docs'}"}}
Bad Name: {{area: projects, path: "{proj}"}}
wrongarea: {{area: money, path: "{proj}"}}
nowhere: {{area: projects, about: nothing}}
home: {{area: projects, path: "~"}}
""")
    cfg["life"]["sources_file"] = str(reg)
    srcs = sources.load_sources(cfg, [a.id for a in load_areas(cfg)])
    assert [s.name for s in srcs] == ["proj", "remote", "gym"]  # bad entries skipped, not fatal

    areas = {a.id: a for a in load_areas(cfg)}
    assert sources.build_source_tools(srcs, areas["health"]) == []  # no projects there: no tools
    fit = {t.name: t for t in sources.build_source_tools(srcs, areas["fitness"])}
    assert fit["project_read"].parameters["properties"]["name"]["enum"] == ["gym"]  # its own area only
    t = {x.name: x for x in sources.build_source_tools(srcs)}  # Jarvis: all of them
    read = lambda **kw: t["project_read"].run(kw)  # noqa: E731

    listing, err = read(name="proj")
    assert not err and "README.md" in listing and ".env.example" in listing and "docs/" in listing
    assert ".env (" not in listing and "node_modules" not in listing and ".git/" not in listing
    for bad in (".env", "../outside.txt", "escape.txt", "node_modules", ".git/config"):
        assert read(name="proj", path=bad)[1] is True, bad
    cfg_text = read(name="proj", path="config.py")[0]
    assert "sk-ant" not in cfg_text and "AAHabc" not in cfg_text and "max_tokens = 8000" in cfg_text
    sheet = read(name="proj", path="tracker.xlsx")[0]
    assert "== sheet: Tracker" in sheet and "Task |  | Due" in sheet
    assert "Agree contributions |  | 2026-11-15 | 1750" in sheet  # dates as dates, gaps kept
    first, second = read(name="proj", path="docs/long.md")[0], read(name="proj", path="docs/long.md", part=2)[0]
    assert first.startswith("line 0") and "part 1 of 3" in first and second.startswith("line ")
    assert len(first) <= sources.MAX_CHARS + 200 and "read part 3" in second
    assert "First steps" in t["project_activity"].run({"name": "proj"})[0]
    sources._cache.clear()
    assert "last commit" in sources.last_activity(srcs[0]) and "First steps" in sources.last_activity(srcs[0])

    calls = []

    def fake_gh(endpoint):
        calls.append(endpoint)
        if endpoint.startswith("repos/someone/remote/commits"):
            return [{"commit": {"author": {"date": "2026-11-20T10:00:00Z"}, "message": "Ship it\n\nbody"}}]
        if endpoint.startswith("repos/someone/remote/pulls"):
            return [{"number": 7, "title": "Add billing"}]
        if endpoint == "repos/someone/remote/contents/":
            return [{"name": "src", "type": "dir"}, {"name": ".env", "type": "file", "size": 10},
                    {"name": "README.md", "type": "file", "size": 2048}]
        if endpoint == "repos/someone/remote/contents/README.md":
            return {"name": "README.md", "content": base64.b64encode(b"# Remote\ntoken = ghp_abcdefghijklmnopqrstuvwxyz0123").decode()}
        raise AssertionError(endpoint)

    import base64

    monkeypatch.setattr(sources, "_gh", fake_gh)
    listing = read(name="remote")[0]
    assert "src/" in listing and "README.md (2 KB)" in listing and ".env" not in listing
    readme = read(name="remote", path="README.md")[0]
    assert readme.startswith("# Remote") and "ghp_" not in readme
    act = t["project_activity"].run({"name": "remote", "days": 7})[0]
    assert "2026-11-20 Ship it" in act and "#7 Add billing" in act
    assert "since=" in calls[-2]
    assert "remote (work): Remote only | GitHub someone/remote | last commit 2026-11-20 Ship it" in \
        t["project_list"].run({})[0]


def test_area_agents_get_their_own_projects(cfg, store, clock, tmp_path):
    from src.life.sources import Source

    (tmp_path / "p").mkdir()
    srcs = [Source("site", "projects", "My site", tmp_path / "p")]
    fake = FakeAnthropic([_resp("end_turn", _block("text", text="ok"))] * 2)
    agents = _agents(cfg, store, clock, fake)
    agents.sources = srcs
    assert "project_read" not in {t.name for t in agents.tools_for(agents.by_id["fitness"])}
    agents.run("fitness", "x")
    assert "project_list" not in fake.requests[0]["system"][0]["text"]
    cfg["life"]["areas"].append({"id": "projects", "name": "Projects", "emoji": "🚀", "folder": "Projects",
                                 "blurb": "Builds", "focus": "Ship."})
    agents = _agents(cfg, store, clock, fake)
    agents.sources = srcs
    assert {"project_list", "project_read", "project_activity"} <= {t.name for t in agents.tools_for(agents.by_id["projects"])}
    agents.run("projects", "where is the site at?")
    system = fake.requests[1]["system"][0]["text"]
    assert "project_list, project_activity and project_read" in system and "strike it through" in system


# ---------------------------------------------------------------- the index (where things are)


def _docx(path: Path, *paras: str) -> Path:
    import zipfile

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>")
    return path


def _pptx(path: Path, *slides: str) -> Path:
    import zipfile

    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    with zipfile.ZipFile(path, "w") as z:
        for i, text in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", f"<p:sld xmlns:p=\"p\" {a}><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>")
    return path


@pytest.fixture
def indexed(cfg, store, tmp_path):
    """A Jarvis folder and one project folder, indexed."""
    from src.life.index import Index
    from src.life.sources import Source

    ensure_folders(cfg)
    ws = Path(cfg["workspace"]["path"])
    (ws / "Fitness" / "plan.md").write_text("# Training plan\n\n" + "\n".join(f"filler line {i} " + "x" * 70
                                                                             for i in range(90))
                                           + "\n\n## Deadlift\nWork up to 140 kg for 5.\n")
    (ws / "Work" / "brief.md").write_text("# Work brief\nQuarterly targets and the hiring plan.\n")
    (ws / "Work" / "_trash").mkdir()
    (ws / "Work" / "_trash" / "old.md").write_text("hiring plan, an old version")
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "lib.js").write_text("hiring plan inside a dependency")
    (proj / "README.md").write_text("# Proj\nA pottery shop.")
    _docx(proj / "docs" / "agreement.docx", "Partnership terms",
          "Each partner pays up to 500 towards the kiln before launch.")
    _xlsx(proj / "tracker.xlsx")
    _pptx(proj / "deck.pptx", "The shop", "Pitch: handmade mugs for cafes")
    (proj / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    (proj / "config.py").write_text('API_KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123"\nretries = 3\n')
    srcs = [Source("proj", "work", "My pottery shop", proj)]
    idx = Index(tmp_path / "index.db", ws, srcs, store)
    idx.refresh_soon = lambda *a, **k: False  # tests refresh by hand
    stats = idx.refresh()
    return SimpleNamespace(idx=idx, ws=ws, proj=proj, srcs=srcs, stats=stats)


def test_index_finds_inside_documents_and_says_where_to_open(indexed):
    idx = indexed.idx
    everything = idx.scope_all("Francesco")
    assert indexed.stats["proj"] == 5 and indexed.stats["folder"] >= 5  # briefs, plan, brief; not the trash

    hit = idx.find(everything, "partner pays")
    assert "1. proj › docs/agreement.docx (Word" in hit and '"Partnership terms"' in hit
    assert "«partner» «pays» up to 500" in hit
    assert 'open: project_read name="proj" path="docs/agreement.docx"' in hit

    deadlift = idx.find(everything, "deadlift")  # deep in a long file: the hit names the part to open
    assert "Jarvis folder › Fitness/plan.md" in deadlift and "part 2, § Deadlift: «Deadlift» Work up to 140 kg" in deadlift
    assert 'open: ws_read_file name="Fitness/plan.md" part=2' in deadlift
    from src.tools import workspace as W

    assert "Work up to 140 kg" in W.read_file({"workspace": {"path": str(indexed.ws)}}, "Fitness/plan.md", part=2)

    assert "sheet Tracker: …" in idx.find(everything, "agree contributions") or \
        "sheet Tracker:" in idx.find(everything, "agree contributions")
    assert "slide 2: «Pitch»" in idx.find(everything, "pitch mugs")
    assert "proj › tracker.xlsx" in idx.find(everything, "tracker")  # file names count too

    # never indexed: secrets, dependencies, the trash
    assert "Nothing anywhere" in idx.find(everything, "abcdefghijklmnopqrstuvwxyz")
    with idx._db() as c:
        assert not c.execute("select 1 from pieces where body like '%sk-ant%'").fetchall()
        assert c.execute("select count(*) from chunks where chunks match 'kiln'").fetchone()[0] == 1
        paths = {r["path"] for r in c.execute("select path from files")}
    assert ".env" not in paths and "node_modules/lib.js" not in paths and "Work/_trash/old.md" not in paths
    assert "config.py" in paths and "retries" in idx.find(everything, "retries")
    assert "Give find a few words" in idx.find(everything, "  ")


def test_index_scopes_each_agent_to_its_own(indexed):
    idx = indexed.idx
    fit = idx.scope_for("fitness", "Fitness", "Francesco")
    assert fit.places == ["folder"]  # the project belongs to work
    hit = idx.find(fit, "deadlift")
    assert "your folder › plan.md" in hit and 'ws_read_file name="plan.md" part=2' in hit
    assert "Nothing" in idx.find(fit, "hiring") and "Nothing" in idx.find(fit, "partner")
    assert "plan.md" in idx.find(fit, "hiring plan")  # no file has both words: the closest have one
    work = idx.scope_for("work", "Work", "Francesco")
    assert "your folder › brief.md" in idx.find(work, "hiring plan") and "proj ›" in idx.find(work, "partner")
    assert "proj ›" in idx.find(work, "pottery", place="proj") and "brief.md" not in idx.find(work, "pottery", place="proj")
    with pytest.raises(ValueError):
        idx.find(fit, "partner", place="proj")
    tools = {t.name: t for t in __import__("src.life.index", fromlist=["x"]).build_index_tools(idx, fit)}
    assert tools["find"].parameters["properties"]["place"]["enum"] == ["folder"]
    out, err = tools["map"].run({"place": "proj"})
    assert err and "no place 'proj'" in out


def test_index_refreshes_only_what_changed(indexed):
    idx, ws = indexed.idx, indexed.ws
    assert idx.refresh() == {"folder": 0, "proj": 0}  # nothing changed: nothing re-read
    (ws / "Fitness" / "plan.md").write_text("# Training plan\n## Squat\nFive by five.\n")
    (ws / "Work" / "brief.md").unlink()
    assert idx.refresh()["folder"] == 1
    everything = idx.scope_all()
    assert "Nothing" in idx.find(everything, "deadlift") and "Squat" in idx.find(everything, "five squat")
    assert "Work/brief.md" not in idx.find(everything, "hiring plan")
    idx._lock.acquire()  # a refresh already running: a second one doesn't start
    try:
        assert idx.refresh() == {"busy": True}
    finally:
        idx._lock.release()


def test_index_two_refreshers_never_double_a_file(indexed, store):
    """The server and a `make life` command may refresh at once: the file
    is forgotten and stored again inside one write transaction."""
    import threading

    from src.life.index import Index

    idx = indexed.idx
    other = Index(idx.path, indexed.ws, indexed.srcs, store)
    with idx._db() as c:
        before = c.execute("select count(*) from pieces").fetchone()[0]
    threads = [threading.Thread(target=i.refresh, kwargs={"force": True}) for i in (idx, other, idx, other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with idx._db() as c:
        assert c.execute("select count(*) from pieces").fetchone()[0] == before
        assert c.execute("select count(*) from chunks where chunks match 'kiln'").fetchone()[0] == 1
    other.set_note(other.scope_all(), "proj", "README.md", "the pitch")
    reopened = Index(idx.path, indexed.ws, indexed.srcs, store)  # same schema: nothing rebuilt, note kept
    assert "note: the pitch" in reopened.outline(reopened.scope_all(), "proj")


def test_index_survives_a_hanging_or_unreadable_place(indexed, monkeypatch):
    """macOS blocks a read of ~/Desktop until someone answers its permission
    prompt: that place times out, the others still refresh, and an
    unreadable folder is an error, never an empty place."""
    import threading

    idx, ws = indexed.idx, indexed.ws
    gate = threading.Event()
    real = idx._refresh_local

    def slow(p, force):
        if p.name == "proj":
            gate.wait(10)
        return real(p, force)

    monkeypatch.setattr(idx, "_refresh_local", slow)
    (ws / "Work" / "new.md").write_text("fresh notes")
    stats = idx.refresh(wait=0.5)
    assert stats == {"folder": 1, "proj": "timed out"}
    assert "Jarvis folder › Work/new.md" in idx.find(idx.scope_all(), "fresh notes")
    assert "allow access" in idx.places_text(idx.scope_all())
    assert idx.refresh(wait=0.5)["proj"] == "still reading"  # not a second stuck thread
    gate.set()
    idx._stuck["proj"].join(5)
    assert idx.refresh(wait=5)["proj"] == 0 and "allow access" not in idx.places_text(idx.scope_all())

    monkeypatch.setattr(idx, "_refresh_local", real)
    monkeypatch.setattr("src.life.index.os.scandir", lambda d: (_ for _ in ()).throw(PermissionError(1, "denied")))
    assert idx.refresh(force=True)["proj"] == "error"
    with idx._db() as c:
        assert c.execute("select count(*) from files where place = 'proj'").fetchone()[0] == 5  # nothing wiped


def test_index_notes_and_maps(indexed):
    idx, proj = indexed.idx, indexed.proj
    everything = idx.scope_all()
    assert "noted on proj › docs/agreement.docx" in idx.set_note(everything, "proj", "docs/agreement.docx",
                                                                  "the signed terms, with the studio rent")
    hit = idx.find(everything, "studio rent")  # found by its note, not its name or text
    assert "proj › docs/agreement.docx" in hit and "note: the signed terms" in hit
    assert "changed since" not in hit
    idx.set_note(everything, "proj", "docs", "legal papers")
    folder_hit = idx.find(everything, "legal papers")
    assert "proj › docs/ (folder)" in folder_hit and 'see inside: map place="proj" path="docs"' in folder_hit
    with pytest.raises(ValueError, match="Did you mean"):
        idx.set_note(everything, "proj", "docs/agrement.docx", "typo")

    full = idx.outline(everything, "proj")
    assert full.startswith("proj (") and "5 files" in full
    assert "docs/ — note: legal papers" in full and "agreement.docx · " in full and "note: the signed" in full
    assert "deck.pptx" in full and '"sheets: Tracker"' in full
    small = idx.outline(everything, "proj", budget=3)
    assert "top level:" in small and "docs/ (1 file)" in small
    assert "Nothing indexed under proj › nope" in idx.outline(everything, "proj", "nope")
    assert "- proj: My pottery shop · " in idx.places_text(everything) and "docs/" in idx.places_text(everything)

    time.sleep(0.01)
    _docx(proj / "docs" / "agreement.docx", "Partnership terms, second draft")
    import os

    os.utime(proj / "docs" / "agreement.docx", (time.time() + 5, time.time() + 5))
    idx.refresh()
    assert "(file changed since" in idx.outline(everything, "proj")
    (proj / "docs" / "agreement.docx").unlink()
    idx.refresh()
    with idx._db() as c:
        assert [r["path"] for r in c.execute("select path from notes")] == []  # gone with the file and folder
    assert idx.set_note(everything, "proj", "README.md", "") == "note removed from proj › README.md"


def test_project_read_suggests_paths_from_the_index(indexed):
    from src.life.sources import build_source_tools

    tools = {t.name: t for t in build_source_tools(indexed.srcs, suggest=indexed.idx.suggest)}
    out, err = tools["project_read"].run({"name": "proj", "path": "docs/agrement.docx"})
    assert err and 'Did you mean: "docs/agreement.docx"' in out
    listing = tools["project_read"].run({"name": "proj", "path": "docs"})[0]
    assert re.search(r"agreement\.docx \(1 KB, \d{4}-\d\d-\d\d\)", listing)


def test_index_find_covers_the_board_logs_and_chat(indexed, store):
    from src.life.clock import to_iso

    idx = indexed.idx
    store.add_item("work", "task", "Approve the supplier contract", due_date="2026-11-30")
    store.add_log("fitness", "workout", 42, note="Run: 5 km by the harbour")
    old = to_iso(utc_now() - timedelta(days=2))
    with store._db() as c:
        for direction, source, text in (("in", "user", "what's the supplier contract cap?"),
                                        ("out", "chat", "The supplier contract caps you at 500."),
                                        ("in", "user", "the supplier contract is signed")):
            c.execute("insert into messages (direction, source, text, created_at) values (?, ?, ?, ?)",
                      (direction, source, text, old))
        c.execute("insert into messages (direction, source, text, created_at) values ('in', 'user', "
                  "'supplier contract news', ?)", (to_iso(utc_now()),))
    hit = idx.find(idx.scope_all("Francesco"), "supplier contract")
    assert "board #" in hit and "[work] task (open): Approve the supplier contract; due 2026-11-30" in hit
    assert 'Jarvis: "The supplier contract caps you at 500."' in hit and 'Francesco: "the supplier contract is signed"' in hit
    assert "cap?" not in hit and "contract news" not in hit  # questions and this turn's own message aren't answers
    assert "harbour" in idx.find(idx.scope_all(), "harbour")
    fit = idx.find(idx.scope_for("fitness", "Fitness"), "supplier contract")
    assert "board #" not in fit and "chat" not in fit  # another area's board; the chat is Jarvis's
    assert "log " in idx.find(idx.scope_for("fitness", "Fitness"), "harbour")


def test_index_github_only_repos(cfg, store, tmp_path, monkeypatch):
    import base64

    from src.life import sources
    from src.life.index import Index
    from src.life.sources import Source

    calls = []

    def fake_gh(endpoint):
        calls.append(endpoint)
        if "git/trees/HEAD" in endpoint:
            return {"sha": "t1", "tree": [
                {"path": "README.md", "type": "blob", "size": 40},
                {"path": "src", "type": "tree"},
                {"path": "src/billing.ts", "type": "blob", "size": 900},
                {"path": ".env", "type": "blob", "size": 10},
                {"path": "docs/Design Notes.md", "type": "blob", "size": 50},
            ]}
        if "commits?per_page=1" in endpoint:
            return [{"commit": {"author": {"date": "2026-11-20T10:00:00Z"}}}]
        if endpoint.endswith("contents/README.md"):
            return {"content": base64.b64encode(b"# Remote\nStripe billing for teams").decode()}
        if endpoint.endswith("contents/docs/Design%20Notes.md"):
            return {"content": base64.b64encode(b"# Design\nQueue retries").decode()}
        raise AssertionError(endpoint)

    monkeypatch.setattr(sources, "_gh", fake_gh)
    ensure_folders(cfg)
    idx = Index(tmp_path / "i.db", Path(cfg["workspace"]["path"]), [Source("remote", "work", "Remote", github="o/r")])
    assert idx.refresh()["remote"] == 3  # the .env never makes it in
    every = idx.scope_all()
    assert 'project_read name="remote" path="README.md"' in idx.find(every, "stripe billing")
    assert "docs/Design Notes.md" in idx.find(every, "queue retries")
    assert "src/billing.ts" in idx.find(every, "billing")  # code: its name, not its text
    assert "last commit 2026-11-20" in idx.places_text(every)
    n = len(calls)
    idx.refresh()
    assert len(calls) == n  # checked within the last 6 hours: no calls at all
    idx.refresh(force=True)
    assert len(calls) > n


def test_agents_get_the_map_and_the_find_tools(cfg, store, clock, indexed):
    fake = FakeAnthropic([_resp("end_turn", _block("text", text="ok"))] * 2)
    agents = _agents(cfg, store, clock, fake)
    agents.index, agents.sources = indexed.idx, indexed.srcs
    assert {"find", "map", "map_note"} <= {t.name for t in agents.tools_for(agents.by_id["fitness"])}
    agents.run("work", "Check what proj needs next")
    first = fake.requests[0]
    msg = first["messages"][0]["content"]
    assert "WHERE THINGS ARE" in msg and "Your folder (Work/):" in msg and "brief.md" in msg
    assert "- proj: My pottery shop" in msg and "Map of proj:" in msg and "agreement.docx" in msg
    assert "call find first" in first["system"][0]["text"]
    agents.run("fitness", "Plan the week")
    msg = fake.requests[1]["messages"][0]["content"]
    assert "plan.md" in msg and "proj" not in msg and "brief.md" not in msg


def test_workspace_reads_documents_in_parts(tmp_path):
    from src.tools import workspace as W

    cfg = {"workspace": {"path": str(tmp_path / "ws")}}
    (tmp_path / "ws").mkdir()
    _docx(tmp_path / "ws" / "letter.docx", "Dear Francesco", "The lease renews in March.")
    assert "The lease renews in March." in W.read_file(cfg, "letter.docx")
    (tmp_path / "ws" / "long.md").write_text("\n".join(f"row {i} " + "y" * 90 for i in range(200)))
    first = W.read_file(cfg, "long.md")
    assert first.startswith("row 0") and "part 1 of" in first and "read part 2" in first
    assert W.read_file(cfg, "long.md", part=2).startswith("row ")
    (tmp_path / "ws" / "broken.pdf").write_bytes(b"not a pdf")
    with pytest.raises(W.WorkspaceError, match="couldn't read"):
        W.read_file(cfg, "broken.pdf")


# ---------------------------------------------------------------- project trackers + folders


def test_project_trackers(cfg, store, clock, monkeypatch):
    p = store.add_item("work", "project", "YardBird", stage="doing")
    other = store.add_item("fitness", "project", "Marathon")
    t1 = store.add_item("health", "task", "Agree contributions", due_date="2026-11-15", parent_id=p["id"])
    assert t1["area"] == "work" and t1["parent_id"] == p["id"]  # a tracker task lives in its project's area
    t2 = store.add_item("work", "task", "Taste samples", parent_id=p["id"])
    with pytest.raises(ValueError):
        store.add_item("work", "task", "x", parent_id=t1["id"])  # only projects have trackers
    assert [t["title"] for t in store.project_tasks(p["id"])] == ["Agree contributions", "Taste samples"]
    assert store.task_counts() == {p["id"]: 2}
    assert store.find_project("yardbird")["id"] == p["id"] and store.find_project(f"#{p['id']}")["id"] == p["id"]
    assert store.find_project("YardBird", area="fitness") is None
    store.update_item(t2["id"], status="done")
    assert [t["title"] for t in store.project_tasks(p["id"], done_days=3)] == ["Agree contributions", "Taste samples"]
    assert store.task_counts() == {p["id"]: 1}
    store.update_item(p["id"], area="health")  # moving a project moves its tracker
    assert store.get_item(t1["id"])["area"] == "health"
    store.update_item(p["id"], area="work")
    store.update_item(t1["id"], parent_id=None)
    assert store.task_counts() == {}
    assert store.overview()["open_items"][0].get("project") is None

    areas = load_areas(cfg)
    fit = {t.name: t for t in build_life_tools(store, clock, areas, fixed=areas[1])}  # the fitness agent
    out, err = run_tool(list(fit.values()), "life_add_item", {"kind": "task", "title": "x", "project": p["id"]})
    assert err and "belongs to" in out  # another area's project: refused
    out, err = run_tool(list(fit.values()), "life_add_item",
                        {"kind": "task", "title": "Long run Sunday", "project": other["id"]})
    assert not err and "on project" in out and "Marathon" in out
    jarvis = build_life_tools(store, clock, areas)
    out, err = run_tool(jarvis, "life_add_item", {"area": "work", "kind": "task", "title": "Ring Matthew",
                                                  "project": "YardBird"})
    assert not err, out
    assert store.project_tasks(p["id"])[-1]["title"] == "Ring Matthew"

    # Telegram: the card shows the tracker and a tap ticks a task off
    from src.life import board

    tasks = store.project_tasks(p["id"], done_days=3)
    text, kb = board.project_card(store.get_item(p["id"]), areas[0], tasks)
    assert "Tracker (1 open)" in text and "☐ Ring Matthew" in text and "☑ Taste samples" in text
    assert _buttons(kb)[f"tk:{tasks[0]['id']}"] == "☐ Ring Matthew"
    text, _ = board.board_view(store.projects(), areas, None, store.task_counts())
    assert "YardBird → " not in text and "· 1 task" in text

    svc = _service(cfg, store, clock, monkeypatch)
    ring = store.project_tasks(p["id"])[0]
    q = {"id": "cb", "data": f"tk:{ring['id']}", "message": {"chat": {"id": 42}, "message_id": 9}}
    asyncio.run(svc._on_button(q))
    assert store.get_item(ring["id"])["status"] == "done" and svc.tg.answers[-1].startswith("☑ Ring Matthew")
    assert "Tracker (0 open)" in svc.tg.edits[-1] and "☑ Ring Matthew" in svc.tg.edits[-1]


def test_delegating_a_project_gives_its_folder_brief_and_tracker(cfg, store, clock):
    p = store.add_item("work", "project", "Yard/Bird: v2", stage="doing", next_step="Agree contributions")
    store.add_item("work", "task", "Agree contributions", due_date="2026-11-15", parent_id=p["id"])
    fake = FakeAnthropic([_resp("end_turn", _block("text", text="Drafted."))] * 2)
    agents = _agents(cfg, store, clock, fake)
    report = agents.run("work", "Draft the founder note", project=str(p["id"]))
    assert report.startswith("Drafted.")
    root = Path(cfg["workspace"]["path"]) / "Work"
    folder = store.get_item(p["id"])["folder"]
    assert folder == "YardBird v2" and (root / folder / "CONTEXT.md").read_text().startswith("# Yard/Bird: v2: project brief")
    first = fake.requests[0]["messages"][0]["content"]
    assert f"PROJECT #{p['id']} Yard/Bird: v2 (doing)" in first and "Agree contributions (due 2026-11-15)" in first
    assert "ITS BRIEF (YardBird v2/CONTEXT.md)" in first
    assert first.index("PROJECT #") < first.index("TASK FROM JARVIS")
    assert "you get its own folder" in fake.requests[0]["system"][0]["text"]
    (root / folder / "CONTEXT.md").write_text("# mine")  # his edits are never overwritten
    agents.run("work", "again", project="yard/bird: v2")
    assert "# mine" in fake.requests[1]["messages"][0]["content"]
    assert "Couldn't open the project" in agents.run("fitness", "x", project=str(p["id"]))  # wrong area
    assert "project" in agents.delegate_tool().parameters["properties"]


# ---------------------------------------------------------------- strava (ported from MacroClaw)


class FakeStravaHTTP:
    def __init__(self, activities):
        self.activities = activities
        self.posts, self.gets = [], []

    def post(self, url, data=None):
        self.posts.append(data)
        body = {"access_token": f"acc{len(self.posts)}", "refresh_token": f"ref{len(self.posts)}",
                "expires_at": int(time.time()) + 6 * 3600, "athlete": {"id": 7, "firstname": "Francesco"}}
        return SimpleNamespace(status_code=200, json=lambda: body, text="")

    def get(self, url, params=None, headers=None):
        self.gets.append((params, headers))
        after = params["after"]
        page = [a for a in self.activities
                if datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).timestamp() > after]
        return SimpleNamespace(status_code=200, json=lambda: page if params["page"] == 1 else [], text="")


def _activity(i, days_ago, **kw):
    start = (utc_now() - timedelta(days=days_ago)).replace(microsecond=0)
    return {"id": i, "name": f"Run {i}", "sport_type": "Run", "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "start_date_local": start.strftime("%Y-%m-%dT%H:%M:%S"), "moving_time": 1800, "distance": 5000,
            "average_speed": 5000 / 1800, "average_heartrate": 150.4, "total_elevation_gain": 42, **kw}


def test_strava_connect_sync_and_load(cfg, store, clock, monkeypatch, tmp_path):
    from src.life import strava as st

    monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
    http = FakeStravaHTTP([_activity(1, 1), _activity(2, 3, sport_type="Ride", name="Commute", distance=20000,
                                                        moving_time=3600, average_speed=20000 / 3600),
                           _activity(3, 10, moving_time=5400, distance=16000, average_speed=16000 / 5400)])
    s = st.Strava(store, clock, tmp_path / "strava.json", http=http)
    tools = {t.name: t for t in st.build_strava_tools(s)}
    assert "isn't set up" in tools["strava_sync"].run({})[0]
    monkeypatch.setenv("STRAVA_CLIENT_ID", "123")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "shh")
    assert "isn't connected" in tools["training_load"].run({})[0]

    url = s.connect_url()
    assert "client_id=123" in url and "activity%3Aread_all" in url and "localhost%2Fexchange_token" in url
    state = store.get_setting("strava_state")
    with pytest.raises(st.StravaError, match="old or not from me"):
        s.finish_connect("http://localhost/exchange_token?state=nope&code=abc&scope=read,activity:read_all")
    with pytest.raises(st.StravaError, match="activity access"):
        s.finish_connect(f"http://localhost/exchange_token?state={state}&code=abc&scope=read")
    pasted = f"here: http://localhost/exchange_token?state={state}&code=abc&scope=read,activity:read_all"
    assert st.Strava.looks_like_callback(pasted)
    assert s.finish_connect(pasted) == "Francesco"
    assert http.posts[-1]["grant_type"] == "authorization_code" and http.posts[-1]["client_secret"] == "shh"
    assert oct((tmp_path / "strava.json").stat().st_mode & 0o777) == "0o600"
    assert store.get_setting("strava_state") is None  # one use only

    out = s.sync()
    assert "3 new workouts" in out
    logs = store.get_logs("fitness", "workout", 30)
    assert len(logs) == 3 and {l["unit"] for l in logs} == {"min"}
    run = next(l for l in logs if "Run 1" in l["note"])
    assert run["value"] == 30 and run["note"] == 'Run: 5.0 km, 6:00/km, avg HR 150, 42 m up. "Run 1"'
    assert "40.0 km/h" not in out and "Ride: 20.0 km, 20.0 km/h" in out
    assert "nothing new" in s.sync() and len(store.get_logs("fitness", "workout", 30)) == 3  # never doubled

    tok = json.loads((tmp_path / "strava.json").read_text())
    tok["expires_at"] = int(time.time()) + 60  # about to expire: refreshed first
    (tmp_path / "strava.json").write_text(json.dumps(tok))
    s.sync()
    assert http.posts[-1]["grant_type"] == "refresh_token" and http.gets[-1][1]["Authorization"] == "Bearer acc2"

    load = s.training_load(14)
    assert "3 sessions" in load and "41.0 km" in load and "long run" in load
    assert "Load steady: 13 min/day this week vs 13 last week" in load  # 90 min in each week
    assert st.intensity(175, 1000 / 260, 100, True) == 10 and st.intensity(110, 9.0, 30, False) == 3
    feats = st.day_features([_activity(9, 0, moving_time=4800)])
    assert list(feats.values())[0]["long_run"] is True

    t = st.day_targets("performance", 70, None)
    assert t == {"kcal": 2571, "protein_g": 140, "fat_g": 77, "carbs_g": 330}  # MacroClaw: (70/70) x 1658.75 x 1.55
    hard = st.day_targets("cut", 70, {"minutes": 90, "intensity": 8, "long_run": True})
    assert hard["kcal"] == round(2571 * 0.85) + 700 and hard["protein_g"] == 154 and "56 g carbs" in hard["timing"]  # 40 + 2 x intensity
    store.add_log("health", "weight", 72.5, "kg")
    fuel = s.fuel_targets("performance")
    assert "weight 72.5 kg (logged" in fuel and "Rest day: 2,663 kcal" in fuel and "not medical advice" in fuel


def test_strava_on_telegram(cfg, store, clock, monkeypatch, tmp_path):
    from src.life import strava as st

    svc = _service(cfg, store, clock, monkeypatch)
    monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
    http = FakeStravaHTTP([_activity(1, 1)])
    svc.life.strava = st.Strava(store, clock, tmp_path / "strava.json", http=http)
    msg = lambda text: {"chat": {"id": 42}, "text": text}  # noqa: E731
    asyncio.run(svc._on_message(msg("/strava")))
    assert "strava.com/settings/api" in svc.tg.sent[-1][1]
    monkeypatch.setenv("STRAVA_CLIENT_ID", "123")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "shh")
    asyncio.run(svc._on_message(msg("/strava")))
    text, kb = svc.tg.sent[-1][1], svc.tg.sent[-1][2]
    assert "localhost/exchange_token" in text and kb["inline_keyboard"][0][0]["url"].startswith(st.AUTH_URL)
    state = store.get_setting("strava_state")
    asyncio.run(svc._on_message(msg(f"http://localhost/exchange_token?state={state}&code=c&scope=read,activity:read_all")))
    assert svc.tg.sent[-1][1] == "Strava connected, Francesco. Strava synced: 1 new workout logged."
    assert svc.session.calls == []  # never went to the model
    assert "code=c" not in json.dumps(store.recent_messages(10))  # the code isn't kept
    store.set_setting("strava_last_sync", to_iso(utc_now() - timedelta(hours=1)))
    http.activities.append(_activity(2, 0))
    asyncio.run(svc._maybe_sync_strava())
    assert len(store.get_logs("fitness", "workout", 7)) == 2


# ---------------------------------------------------------------- the coach


def _coach(store, clock, **profile):
    from src.life.coach import Coach

    c = Coach(store, clock, "fitness")
    c.setup(**{"goal": "muscle", "lift_days": 4, "run_days": 3, "equipment": "gym", "experience": "mid",
               "minutes": 60, **profile})
    return c


def _lift_today(store, coach, clock, template="upper_a"):
    """Make today a lifting day regardless of the weekday."""
    p = coach.profile()
    p["first_day"] = (clock.today() + timedelta(days=1)).isoformat()  # the planner leaves today alone
    coach._save_profile(p)
    with store._db() as c:
        c.execute("delete from coach_sessions where day = ?", (clock.today().isoformat(),))
        cur = c.execute("insert into coach_sessions (day, kind, template, minutes) values (?, 'lift', ?, 60)",
                        (clock.today().isoformat(), template))
        return cur.lastrowid


def test_coach_progression_rules():
    from src.life.coach import LIB, Coach

    bench, pushup = LIB["bench"], LIB["pushup"]  # 6-10 reps, +2.5 kg; bodyweight 8-20
    s = {"exercise": "bench", "load": 60.0, "reps": 8, "misses": 0, "history": []}
    assert Coach.progress(bench, s, "hit")["reps"] == 9
    assert Coach.progress(bench, s, "easy")["reps"] == 10
    top = Coach.progress(bench, {**s, "reps": 10}, "hit")
    assert (top["load"], top["reps"]) == (62.5, 6)  # top of the range: heavier, back to the bottom
    once = Coach.progress(bench, s, "missed")
    assert (once["load"], once["reps"], once["misses"]) == (60.0, 8, 1)
    twice = Coach.progress(bench, once, "missed")
    assert (twice["load"], twice["reps"], twice["misses"]) == (55.0, 6, 0)  # 54 rounds to 55
    assert Coach.progress(bench, {**once}, "hit")["misses"] == 0
    bw = {"exercise": "pushup", "load": None, "reps": 20, "misses": 0, "history": []}
    assert Coach.progress(pushup, bw, "hit")["reps"] == 20  # bodyweight tops out on reps
    assert Coach.progress(bench, {**s, "load": None}, "hit")["reps"] == 9  # no weight yet: reps only


def test_coach_week_layout_and_runs(store, clock):
    from src.life import coach as co

    c = _coach(store, clock)
    mon = co.monday(clock.today())
    week = c.desired_week(mon)
    lifts = [(date.fromisoformat(d).weekday(), t) for d, k, t, _ in week if k == "lift"]
    runs = {t: (date.fromisoformat(d).weekday(), m) for d, k, t, m in week if k == "run"}
    assert lifts == [(0, "upper_a"), (1, "lower_a"), (3, "upper_b"), (4, "lower_b")]
    # long run Sunday, intervals Wednesday (not the day before a leg day), easy Saturday
    assert runs["long"][0] == 6 and runs["intervals"][0] == 2 and runs["easy"][0] == 5
    assert c.run_base(0) == 90  # no Strava runs yet: the default for 1-3 years
    assert (runs["long"][1], runs["intervals"][1], runs["easy"][1]) == (35, 30, 30)  # 40/25/35% of 90
    store.add_log_once("strava:lonely", "fitness", "workout", 49, "min", "Run: 5.0 km, 9:42/km.", utc_now())
    assert _coach(store, clock, lift_days=2).run_base(0) == 90  # one stray run isn't a baseline

    # with Strava history the base follows what he really ran: 4 runs × 50 min in 4 weeks
    for i in range(4):
        store.add_log_once(f"strava:{i}", "fitness", "workout", 50, "min", "Run: 8.0 km, 6:15/km. \"r\"",
                           utc_now() - timedelta(days=3 + 6 * i))
    c2 = _coach(store, clock, lift_days=3, run_days=2)  # a new split re-bases
    assert c2.run_base(0) == 60 and c2.typical_pace() == 375  # (4 x 50 + the 49 above) / 4 weeks
    assert "6:45/km or slower" in c2.run_plan({"template": "easy", "minutes": 30, "day": clock.today().isoformat()})
    p = c2.profile()
    p["run_bases"]["0"] = 100
    c2._save_profile(p)
    assert c2.run_base(1) == 90  # ran less than planned: next block only -10%
    c2.setup(goal="muscle", lift_days=3, run_days=2, equipment="gym", experience="mid", minutes=45)
    assert c2.profile()["run_bases"].get("0") == 100  # a tweak keeps the block

    layouts = {n: [date.fromisoformat(d).weekday() for d, k, *_ in _coach(store, clock, lift_days=n, run_days=r)
                   .desired_week(mon)] for n, r in ((2, 4), (6, 1), (5, 2))}
    assert all(len(v) == len(set(v)) or n == 6 for n, v in layouts.items())  # no double days unless needed


def test_coach_session_readiness_and_blocks(store, clock):
    from src.life import coach as co

    c = _coach(store, clock, equipment="dumbbells", minutes=45)
    day = clock.today()
    plan = c.lift_plan("upper_a", day)
    assert [x["key"] for x in plan] == ["db_bench", "db_row", "db_ohp", "pullup", "lateral"]  # 45 min: 5
    assert plan[0]["load"] is None and "pick a weight" in co.Coach._prescription(plan[0])
    c.set_weight("db_bench", 24)
    p = c.profile()
    weeks = (co.monday(day) - date.fromisoformat(p["start"])).days // 7
    assert c.block_week(day) == (0, 0) and weeks == 0
    for wk, rir in ((1, 2), (2, 1), (3, 4)):
        p["start"] = (co.monday(day) - timedelta(weeks=wk)).isoformat()
        c._save_profile(p)
        x = c.lift_plan("upper_a", day)[0]
        assert x["rir"] == rir
        if wk == 2:
            assert x["sets"] == 4  # peak week: a set more on the main lifts
        if wk == 3:
            assert x["sets"] == 2 and x["load"] == 22 and x["deload"]  # ~60% of sets, 10% lighter
    p["start"] = co.monday(day).isoformat()
    c._save_profile(p)

    assert c.readiness()[0] == "green"
    store.add_log("health", "sleep_hours", 5, "hours")
    level, why = c.readiness()
    assert level == "red" and "sleep 5h" in why
    assert c.lift_plan("upper_a", day, "red")[0]["sets"] == 2 and c.lift_plan("upper_a", day, "red")[0]["rir"] == 4
    run = {"template": "intervals", "minutes": 40, "day": day.isoformat()}
    assert "intervals swapped out" in c.run_plan(run, "amber")
    assert "6 × 2 min hard" in c.run_plan(run, "green")


def test_coach_tracking_finish_and_review(store, clock):
    from src.life import coach as co

    c = _coach(store, clock, goal="lean")
    sid = _lift_today(store, c, clock)
    c.set_weight("bench", 60)
    keys = [x["key"] for x in c.lift_plan("upper_a", clock.today())]
    assert keys[:4] == ["bench", "row", "ohp", "pulldown"]
    c.record(sid, "bench", "hit")
    c.record(sid, "row", "missed")
    c.record(sid, "ohp", "easy")
    c.record(sid, "ohp", "hit")  # a re-tap corrects it before Finish
    assert "✓ 1. Bench press 3×6 @ 60 kg" in c.today_text() and "↓ 2. Barbell row" in c.today_text()
    out = c.finish(sid)
    assert out.startswith("Done. Next time:") and "Bench 60 kg × 7" in out
    assert c.lift("row")["misses"] == 1 and c.lift("ohp")["reps"] == 7
    assert c.finish(sid) == "Already finished."
    assert store.get_logs("fitness", "strength", 1)[0]["value"] == 60
    with pytest.raises(ValueError):
        c.set_weight("pushup", 10)  # bodyweight

    # Strava ticks off a planned run
    with store._db() as con:
        rid = con.execute("insert into coach_sessions (day, kind, template, minutes) values (?, 'run', 'easy', 30)",
                          (clock.today().isoformat(),)).lastrowid
    store.add_log_once("strava:77", "fitness", "workout", 32, "min", "Run: 5.1 km, 6:16/km. \"x\"", utc_now())
    assert c.autocomplete_runs() == 1 and c.session(rid)["status"] == "done"

    # physique loop: on a cut, weight going UP is above target; twice in a row moves calories
    for d, w in ((20, 80.0), (18, 80.1), (16, 80.0), (6, 80.6), (4, 80.8), (2, 80.7)):
        store.add_log("health", "weight", w, "kg", None, utc_now() - timedelta(days=d))
    rate, avg = c.weight_trend()
    assert rate > 0 and avg == 80.7
    r1 = c.review()
    assert "above target" in r1 and "Progressed:" in r1 and "Bench 60 kg × 6 → 60 kg × 7" in r1
    assert "2 of" in r1 or "of" in r1
    assert c.profile()["kcal_adjust"] == 0 and c.profile()["off_target"] == 1
    c.review()
    assert c.profile()["off_target"] == 1  # asked twice in one week: counted once
    p = c.profile()
    p["reviewed_week"] = "2000-01-03"
    c._save_profile(p)
    assert "Calories -150/day" in c.review() and c.profile()["kcal_adjust"] == -150

    from src.life.strava import Strava

    fuel = Strava(store, clock, Path("/nonexistent/strava.json")).fuel_targets()
    assert fuel.startswith("Goal cut") and "calories -150/day" in fuel

    # deload week: results logged, loads stay
    p = c.profile()
    p["start"] = (co.monday(clock.today()) - timedelta(weeks=3)).isoformat()
    c._save_profile(p)
    sid2 = _lift_today(store, c, clock, "upper_b")
    c.record(sid2, "lateral", "easy")
    before = c.lift("lateral")
    assert "Loads stay put" in c.finish(sid2) and c.lift("lateral")["reps"] == before["reps"]


def test_coach_on_telegram(cfg, store, clock, monkeypatch):
    from src.life import board, coach as co

    svc = _service(cfg, store, clock, monkeypatch)
    svc.life.coach = co.Coach(store, clock, "fitness")
    assert board.MENU_TRAIN in json.dumps(board.menu_keyboard(), ensure_ascii=False)
    asyncio.run(svc._on_message({"chat": {"id": 42}, "text": board.MENU_TRAIN}))
    text, kb = svc.tg.sent[-1][1], svc.tg.sent[-1][2]
    assert "Six taps" in text and "co:0:muscle" in _buttons(kb)

    tap = lambda data, mid=50: asyncio.run(svc._on_button(  # noqa: E731
        {"id": "cb", "data": data, "message": {"chat": {"id": 42}, "message_id": mid}}))
    for data in ("co:0:recomp", "co:1:4", "co:2:2", "co:3:gym", "co:4:mid", "co:5:60"):
        tap(data)
    assert svc.tg.edits[-1].startswith("✅ Program ready") and "Upper A / Lower A / Upper B / Lower B" in svc.tg.edits[-1]
    c = svc.life.coach
    assert c.profile()["goal"] == "recomp" and c.profile()["run_days"] == 2

    sid = _lift_today(store, c, clock)
    asyncio.run(svc._on_message({"chat": {"id": 42}, "text": "/train"}))
    kb = _buttons(svc.tg.sent[-1][2])
    assert kb[f"cx:{sid}:bench:h"] == "✓ Bench" and kb[f"cw:{sid}:bench"] == "⚖" and kb[f"cf:{sid}"].startswith("✅")
    tap(f"cw:{sid}:bench")
    prompt_id = len(svc.tg.sent)
    asyncio.run(svc._on_message({"chat": {"id": 42}, "text": "62,5 kg", "message_id": 99,
                                 "reply_to_message": {"message_id": prompt_id}}))
    assert c.lift("bench")["load"] == 62.5 and "3×6 @ 62.5 kg" in svc.tg.sent[-1][1]
    tap(f"cx:{sid}:bench:h")
    assert svc.tg.answers[-1] == "✓ Bench" and "✓ 1. Bench press" in svc.tg.edits[-1]
    tap(f"cf:{sid}")
    assert "Next time: Bench 62.5 kg × 7" in svc.tg.edits[-1]
    tap("cv:1")
    assert svc.tg.edits[-1].startswith("📅 Week of") and "Mon: Upper A" in svc.tg.edits[-1]
    assert svc.session.calls == []  # all taps, no model

    tools = {t.name: t for t in co.build_coach_tools(c)}
    sid3 = _lift_today(store, c, clock, "upper_b")
    out, err = tools["coach_log"].run({"exercise": "squat", "outcome": "hit", "weight_kg": 100})
    assert not err and "100 kg" in out and "isn't in today's session (Upper B" in out
    assert c.session(sid3)["results"] == {} and c.lift("squat")["load"] == 100
    out, err = tools["coach_log"].run({"exercise": "rows", "outcome": "hit"})
    assert not err and "Seated cable row: hit" in out
    out, err = tools["coach_log"].run({"exercise": "incline", "outcome": "easy", "weight_kg": 26})
    assert not err and "Incline dumbbell press: working weight 26 kg" in out
    assert c.session(sid3)["results"] == {"cable_row": "hit", "incline_db": "easy"}
    assert tools["coach_finish"].run({})[0].startswith("Done.")


def test_last_tool_round_asks_for_an_answer():
    from src.llm.base import LLMResponse

    calls = [SimpleNamespace(id=f"c{i}", name="get_datetime", arguments={}) for i in range(12)]
    script = [LLMResponse(text="", model="m", tier="t", tool_calls=[calls[i]]) for i in range(12)]
    llm = ScriptedLLM(script)
    s = _bare_session([Tool("get_datetime", "now", {"type": "object", "properties": {}}, lambda: "noon")], llm)
    s.cfg["llm"]["max_tool_rounds"] = {"coordinator": 3}
    resp = asyncio.run(s.turn("dig", conversation=Conversation(4), tier="coordinator", learn=False))
    assert resp.extra["tool_rounds"] == 3 and len(llm.calls) == 4
    last_results = llm.calls[-1]["messages"][-1]["content"]
    assert "That was the last tool call this turn" in last_results[-1]["out"]
    assert "last tool call" not in json.dumps(llm.calls[-2]["messages"][-1]["content"])
    assert resp.text == "Sorry, I couldn't finish working that one out."  # it still called a tool: fallback


# ---------------------------------------------------------------- Mac jobs (Claude Code on the Mac)

FAKE_CLAUDE = r"""#!{python}
# stands in for the Claude Code CLI: records how it was started, then acts out the brief
import json, os, sys, time
brief = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "brief": brief,
                         "env": sorted(os.environ)}}) + "\n")
def say(e):
    print(json.dumps(e), flush=True)
if "fail" in brief:
    print("Not logged in · Please run /login", file=sys.stderr)
    sys.exit(1)
say({{"type": "system", "subtype": "init", "model": "claude-opus-5-5", "permissionMode": "auto"}})
path = os.path.join(os.getcwd(), "notes.txt")
say({{"type": "assistant", "message": {{"content": [
    {{"type": "tool_use", "name": "Bash", "input": {{"command": "ls", "description": "Look around"}}}},
    {{"type": "tool_use", "name": "Edit", "input": {{"file_path": path}}}}]}}}})
open(path, "w").write("hi")
if "sleep" in brief:
    time.sleep(30)
if "background" in brief:  # answers, waits on a background task, answers again: one run
    say({{"type": "result", "subtype": "success", "is_error": False, "result": "Waiting on the build."}})
    time.sleep(1)
say({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "Working on it."}}]}}}})
say({{"type": "result", "subtype": "success", "is_error": False, "result": "Added notes.txt. Nothing left for you.",
      "total_cost_usd": 0.12, "num_turns": 3}})
"""


@pytest.fixture
def mac_env(tmp_path, cfg, store, clock, monkeypatch):
    import sys

    from src.life.mac import MacJobs
    from src.life.sources import Source

    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
    claude.chmod(0o755)
    log = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-jobs")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-for-jobs")
    shop, root = tmp_path / "shop", tmp_path / "Jarvis"
    shop.mkdir(), root.mkdir()
    cfg["life"]["mac"] = {"claude": str(claude), "jobs_dir": str(tmp_path / "jobs")}
    mac = MacJobs(store, clock, root, [Source("shop", "projects", "My pottery shop", shop),
                                       Source("jarvis", "projects", "Jarvis", tmp_path / "public-copy")],
                  cfg, folder=Path(cfg["workspace"]["path"]), owner="Francesco")

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return SimpleNamespace(mac=mac, shop=shop, root=root, calls=calls)


def _until(mac, job_id, statuses, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        mac.tick()
        job = mac.get(job_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} still {mac.get(job_id)['status']}")


def test_mac_job_runs_claude_code_unattended_and_reports(mac_env):
    from src.life import mac as macjobs

    mac = mac_env.mac
    tools = {t.name: t for t in macjobs.build_mac_tools(mac)}
    out, err = tools["mac_task"].run({"task": "add a note", "project": "shop", "run_now": True})
    assert not err and "Mac job #1 in shop: starting now" in out
    job = _until(mac, 1, ("done", "failed"))
    assert job["status"] == "done", job
    assert job["summary"] == "Added notes.txt. Nothing left for you."
    assert job["files"] == ["notes.txt"] and job["steps"] == 2 and (mac_env.shop / "notes.txt").exists()

    call = mac_env.calls()[0]
    argv = call["argv"]
    assert call["cwd"] == str(mac_env.shop.resolve()) and call["brief"] == "add a note"
    assert argv[:1] == ["-p"] and "add a note" not in argv  # the brief goes in on stdin
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--session-id") + 1] == job["session_id"]
    deny = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
    assert "Bash(git push:*)" in deny and "Bash(launchctl:*)" in deny
    rules = argv[argv.index("--append-system-prompt") + 1]
    assert "unattended on Francesco's Mac mini" in rules and "Never push, deploy" in rules
    # none of Jarvis's keys reach the job (an API key would also bill the API, not his plan)
    assert "ANTHROPIC_API_KEY" not in call["env"] and "TELEGRAM_BOT_TOKEN" not in call["env"]

    text, kb = macjobs.card(job)
    assert text.startswith("🖥 #1 shop · ✅ done in") and "2 steps" in text and kb is None
    assert macjobs.report(job).startswith("✅ #1 shop\nAdded notes.txt.") and "reply to keep going" in macjobs.report(job)
    out, _ = tools["mac_jobs"].run({"job_id": 1})
    assert "Changed: notes.txt" in out and f"claude --resume {job['session_id']}" in out
    assert "#1 shop · done" in tools["mac_jobs"].run({})[0]

    targets = mac.targets()
    assert targets["jarvis"].cwd == mac_env.root and targets["mac"].cwd == Path.home()
    assert targets["jarvis"].extra and "folder" in targets
    out, err = tools["mac_task"].run({"task": "x", "project": "nope"})
    assert err and "one of: mac, folder, shop, jarvis" in out


def test_mac_job_waits_for_a_tap_one_per_folder_and_stops(mac_env):
    import os

    mac = mac_env.mac
    job = mac.create("shop", "sleep on it")
    mac.tick()
    assert mac.get(job["id"])["status"] == "proposed" and mac_env.calls() == []  # nothing runs untapped
    assert mac.approve(job["id"])["status"] == "queued"
    running = _until(mac, job["id"], ("running",))
    second = mac.follow_up(running, "and then tidy up")  # same folder: waits its turn
    mac.tick()
    assert mac.get(second["id"])["status"] == "queued"
    _until(mac, job["id"], ("running",))
    end = time.monotonic() + 10
    while mac.get(job["id"])["steps"] < 2 and time.monotonic() < end:
        mac.tick()
        time.sleep(0.05)
    stopped = mac.stop(job["id"])
    assert stopped["status"] == "stopped" and stopped["reported"] == 1 and stopped["files"] == ["notes.txt"]
    with pytest.raises(ProcessLookupError):
        os.kill(running["pid"], 0)
    nxt = _until(mac, second["id"], ("done",))  # the folder is free: the follow-up starts
    assert nxt["session_id"] == job["session_id"] and len(mac_env.calls()) == 2
    argv = mac_env.calls()[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == job["session_id"] and "--session-id" not in argv
    assert mac_env.calls()[-1]["brief"] == "and then tidy up"


def test_mac_job_follow_ups_and_failures(mac_env):
    mac = mac_env.mac
    proposed = mac.create("jarvis", "fix the test")
    same = mac.follow_up(proposed, "and the docs")
    assert same["id"] == proposed["id"] and same["task"] == "fix the test\n\nAlso: and the docs"

    bad = mac.create("mac", "fail please", start=True)
    bad = _until(mac, bad["id"], ("failed",))
    assert "isn't signed in on the Mac" in bad["error"] and bad["reported"] == 0
    from src.life.mac import report

    assert report(bad).startswith("⚠ #2 mac: Claude Code isn't signed in")
    cancelled = mac.cancel(proposed["id"])
    assert cancelled["status"] == "cancelled" and cancelled["reported"] == 1
    assert [j["id"] for j in mac.to_show()] == [bad["id"]]  # ended + unreported: its summary is still owed

    bg = mac.create("shop", "background build", start=True)
    mac.tick()
    out, end = mac.dir / f"{bg['id']}.jsonl", time.monotonic() + 10
    while not (out.exists() and "Waiting on the build" in out.read_text()):
        assert time.monotonic() < end
        time.sleep(0.05)
    mac.tick()
    assert mac.get(bg["id"])["status"] == "running"  # its first answer isn't the end
    bg = _until(mac, bg["id"], ("done",))
    assert bg["summary"] == "Added notes.txt. Nothing left for you."  # the last answer is the summary

    mac.claude_path = str(mac_env.root / "missing")
    gone = mac.create("shop", "anything", start=True)
    gone = _until(mac, gone["id"], ("failed",))
    assert "isn't installed" in gone["error"]


def test_service_runs_mac_jobs_on_telegram(mac_env, cfg, store, clock, monkeypatch):
    svc = _service(cfg, store, clock, monkeypatch)
    svc.mac = svc.life.mac = mac_env.mac
    mac = mac_env.mac

    def sweep_until(job_id, statuses):
        end = time.monotonic() + 15
        while time.monotonic() < end:
            asyncio.run(svc.mac_sweep())
            if mac.get(job_id)["status"] in statuses:
                asyncio.run(svc.mac_sweep())
                return mac.get(job_id)
            time.sleep(0.05)
        raise AssertionError(mac.get(job_id))

    job = mac.create("shop", "add a note")  # what Jarvis's mac_task makes
    svc._replying = 1
    asyncio.run(svc.mac_sweep())
    assert svc.tg.sent == []  # Jarvis's reply goes first
    svc._replying = 0
    asyncio.run(svc.mac_sweep())
    _, text, kb = svc.tg.sent[0]
    assert text == "🖥 #1 shop\nadd a note"
    assert [b["callback_data"] for b in kb["inline_keyboard"][0]] == ["mj:1:go", "mj:1:x"]
    card_id = mac.get(job["id"])["card_id"]

    tap = {"id": "q1", "data": "mj:1:go", "message": {"message_id": card_id, "chat": {"id": 42}}}
    asyncio.run(svc._on_button(tap))
    assert svc.tg.answers[-1] == "▶ Starting"
    done = sweep_until(job["id"], ("done",))
    assert svc.tg.edits[-1].startswith("🖥 #1 shop · ✅ done")
    report_text = svc.tg.sent[-1][1]
    assert report_text.startswith("✅ #1 shop\nAdded notes.txt") and svc.tg.replies_to[-1] == card_id
    assert done["reported"] == 1 and done["result_id"] == len(svc.tg.sent)
    asyncio.run(svc.mac_sweep())
    assert sum(1 for _, t, _ in svc.tg.sent if t.startswith("✅")) == 1  # the summary goes once
    assert [m["source"] for m in store.unseen_outbound()] == ["mac"]  # Jarvis sees it with the next message

    # a reply to the summary is the job's next instruction, in the same session
    msg = {"message_id": 99, "chat": {"id": 42}, "text": "now make it say hello",
           "reply_to_message": {"message_id": done["result_id"]}}
    asyncio.run(svc._on_message(msg))
    follow = mac.recent(1)[0]
    assert follow["parent_id"] == job["id"] and follow["resume"] == 1 and follow["status"] == "queued"
    sweep_until(follow["id"], ("done",))
    assert mac_env.calls()[-1]["brief"] == "now make it say hello"
    assert svc.session.calls == []  # no Jarvis turn for any of it

    # /mac project: task starts at once (typing it is the go-ahead); /mac alone lists
    asyncio.run(svc._on_message({"message_id": 100, "chat": {"id": 42}, "text": "/mac shop: tidy the folder"}))
    direct = mac.recent(1)[0]
    assert direct["target"] == "shop" and direct["task"] == "tidy the folder" and direct["status"] == "queued"
    sweep_until(direct["id"], ("done",))
    asyncio.run(svc._on_message({"message_id": 101, "chat": {"id": 42}, "text": "/mac"}))
    assert "Projects: mac, folder, shop, jarvis" in svc.tg.sent[-1][1] and "#3 shop · done" in svc.tg.sent[-1][1]
    assert svc.status_dict()["mac"]["claude"].endswith("claude")


def test_mac_command_parsing():
    from src.life.mac import parse_command

    names = {"mac", "shop", "jarvis"}
    assert parse_command("/mac shop: tidy up", names) == ("shop", "tidy up")
    assert parse_command("/mac jarvis fix the test", names) == ("jarvis", "fix the test")
    assert parse_command("/mac clean my downloads", names) == (None, "clean my downloads")
    assert parse_command("/mac note: buy milk", names) == (None, "note: buy milk")
    assert parse_command("/mac@Bot", names) == (None, "")
