"""Tests for the life organiser (src/life) and the Session hooks it relies on.

No network: Telegram and Claude are faked. Run with `make test`.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
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
    assert report == "Wrote training-plan.md and set a reminder."
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
    assert agents.run("work", "x") == "ok"
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
        self.fail = False

    async def send(self, chat_id, text, keyboard=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text, keyboard))
        return len(self.sent)

    async def typing(self, chat_id):
        pass

    async def answer_callback(self, cid, text=None):
        self.answers.append(text)

    async def edit_text(self, chat_id, mid, text):
        self.edits.append(text)

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

    def fake_run(ci):  # records at call time; the scheduled task itself is a no-op
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
