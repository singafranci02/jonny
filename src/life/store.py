"""The organiser's structured data, in a local SQLite file on the Mac.

Reminders, tasks/goals/notes ("items"), logs, the message feed and the area
agents' run history (agent_runs: what each agent was asked, reported and
changed, so its next run picks up where it left off). The file
lives in data/ (never in iCloud: a live database synced by iCloud corrupts);
a nightly copy goes into the iCloud folder instead (see backup_to).

Every method opens its own short connection, so it is safe from the server's
threads and from a second process (the CLI) at the same time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .clock import RECURRENCES, UTC, Clock, from_iso, to_iso, utc_now

ITEM_KINDS = ("task", "goal", "note", "project")
ITEM_STATUSES = ("open", "done", "archived")
# projects (the board): where an open project stands; "done" is status=done
PROJECT_STAGES = ("todo", "doing", "waiting")
REMINDER_STATUSES = ("pending", "sending", "sent", "done", "cancelled")

_SCHEMA = """
create table if not exists settings (
  key text primary key,
  value text not null,
  updated_at text not null
);
create table if not exists items (
  id integer primary key autoincrement,
  area text not null,
  kind text not null default 'task',
  title text not null,
  details text,
  status text not null default 'open',
  priority integer not null default 2,
  due_date text,
  created_at text not null,
  updated_at text not null,
  completed_at text,
  stage text,
  next_step text
);
create index if not exists items_area_status on items (area, status);
create table if not exists reminders (
  id integer primary key autoincrement,
  area text not null,
  text text not null,
  due_at text not null,
  recurrence text not null default 'none',
  anchor_day integer,
  status text not null default 'pending',
  last_sent_at text,
  created_at text not null,
  updated_at text not null
);
create index if not exists reminders_status_due on reminders (status, due_at);
create table if not exists logs (
  id integer primary key autoincrement,
  area text not null,
  metric text not null,
  value real,
  unit text,
  note text,
  logged_at text not null
);
create index if not exists logs_area_metric on logs (area, metric, logged_at);
create table if not exists messages (
  id integer primary key autoincrement,
  direction text not null,
  source text not null,
  area text,
  text text not null,
  created_at text not null
);
create table if not exists agent_runs (
  id integer primary key autoincrement,
  area text not null,
  task text not null,
  status text not null,
  report text,
  changes text not null default '[]',
  rounds integer,
  cost real,
  pid integer,
  started_at text not null,
  finished_at text
);
create index if not exists agent_runs_area on agent_runs (area, id);
"""


class LifeStore:
    def __init__(self, path: Path, clock: Clock):
        self.path = Path(path)
        self.clock = clock
        self._claim_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.execute("pragma journal_mode=wal")
            c.executescript(_SCHEMA)
            # older databases: add newer columns (data untouched)
            for table, col, typ in (
                ("items", "stage", "text"), ("items", "next_step", "text"),
                ("items", "parent_id", "integer"),  # a task on a project's tracker
                ("items", "folder", "text"),  # a project's own folder, inside its area's
                ("logs", "ref", "text"),  # an outside id ("strava:123"): logged once only
            ):
                if col not in {r[1] for r in c.execute(f"pragma table_info({table})")}:
                    c.execute(f"alter table {table} add column {col} {typ}")
            c.execute("create unique index if not exists logs_ref on logs (ref) where ref is not null")
            c.execute("create index if not exists items_parent on items (parent_id)")

    @contextmanager
    def _db(self, write: bool = False):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("begin immediate")
            yield conn
            if write:
                conn.execute("commit")
        except BaseException:
            if write and conn.in_transaction:
                conn.execute("rollback")
            raise
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    # ---------------- settings ----------------

    def get_setting(self, key: str, default=None):
        with self._db() as c:
            row = c.execute("select value from settings where key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self._db() as c:
            c.execute(
                "insert into settings (key, value, updated_at) values (?, ?, ?) "
                "on conflict (key) do update set value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(value), to_iso(utc_now())),
            )

    # ---------------- reminders ----------------

    def _upcoming(self, due: datetime, recurrence: str, anchor: int | None) -> datetime:
        """A repeating reminder whose time has already passed starts at its next
        occurrence ("every day at 8:40", set at 10:40, first fires tomorrow)."""
        if recurrence != "none" and due <= utc_now():
            return self.clock.next_occurrence(due, recurrence, utc_now(), anchor) or due
        return due

    def add_reminder(self, area: str, text: str, due: datetime, recurrence: str = "none") -> dict:
        if recurrence not in RECURRENCES:
            raise ValueError(f"recurrence must be one of {', '.join(RECURRENCES)}")
        now = to_iso(utc_now())
        anchor = self.clock.local(due).day if recurrence == "monthly" else None
        due = self._upcoming(due, recurrence, anchor)
        with self._db() as c:
            cur = c.execute(
                "insert into reminders (area, text, due_at, recurrence, anchor_day, created_at, updated_at) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                (area, text.strip(), to_iso(due), recurrence, anchor, now, now),
            )
            return self._row(c.execute("select * from reminders where id = ?", (cur.lastrowid,)).fetchone())

    def get_reminder(self, rid: int) -> dict | None:
        with self._db() as c:
            return self._row(c.execute("select * from reminders where id = ?", (rid,)).fetchone())

    def update_reminder(
        self,
        rid: int,
        *,
        text: str | None = None,
        due: datetime | None = None,
        recurrence: str | None = None,
        status: str | None = None,
        area: str | None = None,
    ) -> dict | None:
        if recurrence is not None and recurrence not in RECURRENCES:
            raise ValueError(f"recurrence must be one of {', '.join(RECURRENCES)}")
        if status is not None and status not in REMINDER_STATUSES:
            raise ValueError(f"status must be one of {', '.join(REMINDER_STATUSES)}")
        with self._db(write=True) as c:
            cur = self._row(c.execute("select * from reminders where id = ?", (rid,)).fetchone())
            if cur is None:
                return None
            new = dict(cur)
            if text is not None:
                new["text"] = text.strip()
            if area is not None:
                new["area"] = area
            if recurrence is not None:
                new["recurrence"] = recurrence
            if due is not None:
                new["due_at"] = to_iso(due)
            # rescheduling something already sent, or making it repeat, re-arms it
            schedule_changed = due is not None or (recurrence is not None and recurrence != cur["recurrence"])
            if status is None and cur["status"] == "sent" and schedule_changed:
                new["status"] = "pending"
            if status is not None:
                new["status"] = status
            if due is not None or recurrence is not None:
                new["anchor_day"] = (
                    self.clock.local(new["due_at"]).day if new["recurrence"] == "monthly" else None
                )
                if new["status"] == "pending":
                    new["due_at"] = to_iso(
                        self._upcoming(from_iso(new["due_at"]), new["recurrence"], new["anchor_day"])
                    )
            c.execute(
                "update reminders set text = ?, area = ?, recurrence = ?, due_at = ?, status = ?, "
                "anchor_day = ?, updated_at = ? where id = ?",
                (new["text"], new["area"], new["recurrence"], new["due_at"], new["status"],
                 new["anchor_day"], to_iso(utc_now()), rid),
            )
            return self._row(c.execute("select * from reminders where id = ?", (rid,)).fetchone())

    def upcoming_reminders(self, area: str | None = None, days: int = 7, limit: int = 50) -> list[dict]:
        horizon = to_iso(utc_now() + timedelta(days=days))
        with self._db() as c:
            rows = c.execute(
                "select * from reminders where status = 'pending' and due_at <= ? "
                "and (? is null or area = ?) order by due_at asc limit ?",
                (horizon, area, area, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def awaiting_reminders(self, limit: int = 10) -> list[dict]:
        """Sent one-off reminders not yet marked done (last 3 days)."""
        since = to_iso(utc_now() - timedelta(days=3))
        with self._db() as c:
            rows = c.execute(
                "select * from reminders where status = 'sent' and last_sent_at > ? "
                "order by last_sent_at desc limit ?",
                (since, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def claim_due(self) -> list[dict]:
        """Atomically move due reminders pending -> sending and return them.
        Rows stuck in 'sending' for 10+ minutes (crash mid-send) are first
        returned to pending."""
        now = utc_now()
        with self._claim_lock, self._db(write=True) as c:
            c.execute(
                "update reminders set status = 'pending', updated_at = ? "
                "where status = 'sending' and updated_at < ?",
                (to_iso(now), to_iso(now - timedelta(minutes=10))),
            )
            rows = [dict(r) for r in c.execute(
                "select * from reminders where status = 'pending' and due_at <= ? order by due_at",
                (to_iso(now),),
            ).fetchall()]
            for r in rows:
                c.execute(
                    "update reminders set status = 'sending', updated_at = ? where id = ?",
                    (to_iso(now), r["id"]),
                )
        return rows

    def mark_sent(self, reminder: dict) -> None:
        """After a successful send: repeating reminders roll forward (in local
        time), one-off reminders become 'sent' (awaiting Done)."""
        now = utc_now()
        nxt = self.clock.next_occurrence(
            reminder["due_at"], reminder["recurrence"], now, reminder.get("anchor_day")
        )
        with self._db() as c:
            if nxt:
                c.execute(
                    "update reminders set status = 'pending', due_at = ?, last_sent_at = ?, updated_at = ? "
                    "where id = ?",
                    (to_iso(nxt), to_iso(now), to_iso(now), reminder["id"]),
                )
            else:
                c.execute(
                    "update reminders set status = 'sent', last_sent_at = ?, updated_at = ? where id = ?",
                    (to_iso(now), to_iso(now), reminder["id"]),
                )

    def release(self, rid: int) -> None:
        """A send failed: back to pending, retried on the next sweep."""
        with self._db() as c:
            c.execute(
                "update reminders set status = 'pending', updated_at = ? where id = ? and status = 'sending'",
                (to_iso(utc_now()), rid),
            )

    def snooze(self, rid: int, until: datetime) -> dict | None:
        """One-off reminders move; repeating ones get a one-off copy so the
        series itself is untouched."""
        r = self.get_reminder(rid)
        if r is None:
            return None
        if r["recurrence"] == "none":
            return self.update_reminder(rid, due=until, status="pending")
        return self.add_reminder(r["area"], r["text"], until)

    def skip_occurrence(self, rid: int) -> dict | None:
        """For a pending repeating reminder: move it to the occurrence after its
        current one (dashboard ✓ / +1h on a series that hasn't fired yet)."""
        r = self.get_reminder(rid)
        if r is None or r["recurrence"] == "none" or r["status"] != "pending":
            return None
        nxt = self.clock.next_occurrence(r["due_at"], r["recurrence"], from_iso(r["due_at"]), r.get("anchor_day"))
        with self._db() as c:
            c.execute(
                "update reminders set due_at = ?, updated_at = ? where id = ?",
                (to_iso(nxt), to_iso(utc_now()), rid),
            )
        return self.get_reminder(rid)

    def complete(self, rid: int) -> dict | None:
        """Done: one-off reminders close; for repeating ones this only
        acknowledges today's occurrence."""
        r = self.get_reminder(rid)
        if r is None or r["recurrence"] != "none":
            return r
        return self.update_reminder(rid, status="done")

    # ---------------- items: tasks, goals, notes ----------------

    def add_item(
        self,
        area: str,
        kind: str,
        title: str,
        details: str | None = None,
        priority: int = 2,
        due_date: str | None = None,
        stage: str | None = None,
        next_step: str | None = None,
        parent_id: int | None = None,
    ) -> dict:
        """parent_id puts a task (or note/goal) on a project's tracker; it
        then lives in that project's area."""
        if kind not in ITEM_KINDS:
            raise ValueError(f"kind must be one of {', '.join(ITEM_KINDS)}")
        if kind == "project":
            stage = _stage(stage or "todo")
            next_step = (next_step or "").strip() or None
            parent_id = None  # projects don't nest
        else:
            stage = next_step = None  # only projects sit on the board
        if parent_id:
            area = self._project(parent_id)["area"]
        now = to_iso(utc_now())
        with self._db() as c:
            cur = c.execute(
                "insert into items (area, kind, title, details, priority, due_date, created_at, updated_at, "
                "stage, next_step, parent_id) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (area, kind, title.strip(), details, _priority(priority), due_date, now, now, stage, next_step,
                 parent_id or None),
            )
            return self._row(c.execute("select * from items where id = ?", (cur.lastrowid,)).fetchone())

    def update_item(self, iid: int, **patch) -> dict | None:
        """patch keys: title, details, status, priority, due_date (None clears
        it), area, kind, parent_id (None/0 takes it off a tracker), and for
        projects stage, next_step ("" clears it) and folder. Keys that are
        absent are left unchanged. Moving a finished project to a stage
        reopens it."""
        with self._db(write=True) as c:
            cur = self._row(c.execute("select * from items where id = ?", (iid,)).fetchone())
            if cur is None:
                return None
            new = dict(cur)
            for key in ("title", "details", "area"):
                if patch.get(key) is not None:
                    new[key] = patch[key]
            if patch.get("kind") is not None:
                if patch["kind"] not in ITEM_KINDS:
                    raise ValueError(f"kind must be one of {', '.join(ITEM_KINDS)}")
                new["kind"] = patch["kind"]
            if patch.get("priority") is not None:
                new["priority"] = _priority(patch["priority"])
            if "due_date" in patch:
                new["due_date"] = patch["due_date"]
            if patch.get("status") is not None:
                if patch["status"] not in ITEM_STATUSES:
                    raise ValueError(f"status must be one of {', '.join(ITEM_STATUSES)}")
                new["status"] = patch["status"]
                if patch["status"] == "done" and cur["status"] != "done":
                    new["completed_at"] = to_iso(utc_now())
                elif patch["status"] == "open":
                    new["completed_at"] = None
            if new["kind"] == "project":
                if patch.get("stage") is not None:
                    new["stage"] = _stage(patch["stage"])
                    if new["status"] != "open" and patch.get("status") is None:
                        new["status"], new["completed_at"] = "open", None
                new["stage"] = new["stage"] or "todo"
                if patch.get("next_step") is not None:
                    new["next_step"] = patch["next_step"].strip() or None
                if patch.get("folder"):
                    new["folder"] = patch["folder"]
                new["parent_id"] = None
            else:
                new["stage"] = new["next_step"] = new["folder"] = None
                if "parent_id" in patch:
                    new["parent_id"] = patch["parent_id"] or None
                if new["parent_id"]:
                    parent = self._row(c.execute("select * from items where id = ?", (new["parent_id"],)).fetchone())
                    if parent is None or parent["kind"] != "project" or parent["id"] == iid:
                        raise ValueError(f"#{new['parent_id']} is not a project")
                    new["area"] = parent["area"]
            c.execute(
                "update items set title = ?, details = ?, area = ?, kind = ?, priority = ?, due_date = ?, "
                "status = ?, completed_at = ?, stage = ?, next_step = ?, parent_id = ?, folder = ?, "
                "updated_at = ? where id = ?",
                (new["title"], new["details"], new["area"], new["kind"], new["priority"],
                 new["due_date"], new["status"], new["completed_at"], new["stage"], new["next_step"],
                 new["parent_id"], new["folder"], to_iso(utc_now()), iid),
            )
            if new["kind"] == "project" and new["area"] != cur["area"]:  # its tracker moves with it
                c.execute("update items set area = ? where parent_id = ?", (new["area"], iid))
            return self._row(c.execute("select * from items where id = ?", (iid,)).fetchone())

    def get_item(self, iid: int) -> dict | None:
        with self._db() as c:
            return self._row(c.execute("select * from items where id = ?", (iid,)).fetchone())

    def _project(self, pid: int) -> dict:
        p = self.get_item(int(pid))
        if p is None or p["kind"] != "project":
            raise ValueError(f"#{pid} is not a project")
        return p

    def find_project(self, ref, area: str | None = None) -> dict | None:
        """A project by id or (case-insensitive) title, open ones first."""
        text = str(ref).strip().lstrip("#")
        if text.isdigit():
            p = self.get_item(int(text))
            ok = p is not None and p["kind"] == "project" and (area is None or p["area"] == area)
            return p if ok else None
        with self._db() as c:
            row = c.execute(
                "select * from items where kind = 'project' and lower(title) = lower(?) and (? is null or area = ?) "
                "order by status != 'open', id desc limit 1",
                (text, area, area),
            ).fetchone()
        return self._row(row)

    def project_tasks(self, pid: int, done_days: int = 0) -> list[dict]:
        """A project's tracker: its open items (by due date, then priority),
        plus those finished in the last done_days."""
        since = to_iso(utc_now() - timedelta(days=done_days))
        with self._db() as c:
            rows = c.execute(
                "select * from items where parent_id = ? and (status = 'open' or (status = 'done' and ? > 0 "
                "and completed_at > ?)) order by status != 'open', due_date is null, due_date, priority, id",
                (pid, done_days, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def task_counts(self) -> dict[int, int]:
        """Open tracker items per project id."""
        with self._db() as c:
            rows = c.execute(
                "select parent_id, count(*) as n from items where parent_id is not null and status = 'open' "
                "group by parent_id"
            ).fetchall()
        return {r["parent_id"]: r["n"] for r in rows}

    def open_items(self, area: str | None = None, limit: int = 100) -> list[dict]:
        with self._db() as c:
            rows = c.execute(
                "select * from items where status = 'open' and (? is null or area = ?) "
                "order by due_date is null, due_date, priority, updated_at desc limit ?",
                (area, area, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def projects(self, area: str | None = None) -> list[dict]:
        """Open projects for the board: Doing first, then Waiting, then To do."""
        with self._db() as c:
            rows = c.execute(
                "select * from items where kind = 'project' and status = 'open' and (? is null or area = ?) "
                "order by case stage when 'doing' then 0 when 'waiting' then 1 else 2 end, "
                "due_date is null, due_date, priority, updated_at desc limit 200",
                (area, area),
            ).fetchall()
        return [dict(r) for r in rows]

    def recently_done(self, area: str | None = None, days: int = 7) -> list[dict]:
        since = to_iso(utc_now() - timedelta(days=days))
        with self._db() as c:
            rows = c.execute(
                "select * from items where status = 'done' and completed_at > ? "
                "and (? is null or area = ?) order by completed_at desc limit 30",
                (since, area, area),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- logs ----------------

    def add_log(
        self,
        area: str,
        metric: str,
        value: float | None = None,
        unit: str | None = None,
        note: str | None = None,
        logged_at: datetime | None = None,
    ) -> dict:
        metric = (metric or "").strip().lower().replace(" ", "_")
        if not metric:
            raise ValueError("metric is required")
        if value is not None and not (isinstance(value, (int, float)) and value == value and abs(value) != float("inf")):
            raise ValueError("value must be a finite number")
        with self._db() as c:
            cur = c.execute(
                "insert into logs (area, metric, value, unit, note, logged_at) values (?, ?, ?, ?, ?, ?)",
                (area, metric, value, unit, note, to_iso(logged_at or utc_now())),
            )
            return self._row(c.execute("select * from logs where id = ?", (cur.lastrowid,)).fetchone())

    def add_log_once(self, ref: str, area: str, metric: str, value: float | None = None, unit: str | None = None,
                     note: str | None = None, logged_at: datetime | None = None) -> dict | None:
        """Like add_log, keyed by an outside id: None if `ref` was already
        logged (a re-sync never doubles a workout)."""
        with self._db() as c:
            cur = c.execute(
                "insert or ignore into logs (area, metric, value, unit, note, logged_at, ref) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                (area, metric, value, unit, note, to_iso(logged_at or utc_now()), ref),
            )
            if cur.rowcount == 0:
                return None
            return self._row(c.execute("select * from logs where id = ?", (cur.lastrowid,)).fetchone())

    def _today_log(self, area: str, metric: str) -> dict | None:
        today = self.clock.today()
        for entry in self.get_logs(area, metric, days=2, limit=20):  # newest first
            if self.clock.local(entry["logged_at"]).date() == today:
                return entry
        return None

    def today_value(self, area: str, metric: str) -> float | None:
        """Today's value of a once-a-day metric (sleep, mood…), else None."""
        entry = self._today_log(area, metric)
        return entry["value"] if entry else None

    def set_today_log(self, area: str, metric: str, value: float, unit: str | None = None) -> dict:
        """Once-a-day metrics from a tap: re-tapping changes today's value
        instead of adding a second entry (also corrects a typed one)."""
        metric = metric.strip().lower().replace(" ", "_")
        entry = self._today_log(area, metric)
        if entry is None:
            return self.add_log(area, metric, value, unit, "tap")
        with self._db() as c:
            c.execute("update logs set value = ?, unit = coalesce(?, unit) where id = ?", (value, unit, entry["id"]))
            return self._row(c.execute("select * from logs where id = ?", (entry["id"],)).fetchone())

    def get_logs(self, area: str | None = None, metric: str | None = None, days: int = 30, limit: int = 200) -> list[dict]:
        since = to_iso(utc_now() - timedelta(days=days))
        metric = metric.strip().lower().replace(" ", "_") if metric else None
        with self._db() as c:
            rows = c.execute(
                "select * from logs where logged_at > ? and (? is null or area = ?) "
                "and (? is null or metric = ?) order by logged_at desc limit ?",
                (since, area, area, metric, metric, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- messages (activity feed) ----------------

    def log_message(self, direction: str, source: str, text: str, area: str | None = None) -> None:
        with self._db() as c:
            c.execute(
                "insert into messages (direction, source, area, text, created_at) values (?, ?, ?, ?, ?)",
                (direction, source, area, text, to_iso(utc_now())),
            )

    def recent_messages(self, limit: int = 20) -> list[dict]:
        with self._db() as c:
            rows = c.execute("select * from messages order by id desc limit ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def thread_history(self, max_turns: int) -> list[dict]:
        """The text conversation rebuilt from the message feed, so a restart
        doesn't wipe it (a checkpointed thread, as in LangGraph). Only the
        user's own messages and Jarvis's chat replies count, as pairs: a
        message whose turn failed has no reply and is dropped."""
        with self._db() as c:
            rows = c.execute(
                "select direction, source, text from messages where (direction = 'in' and source = 'user') "
                "or (direction = 'out' and source = 'chat') order by id desc limit ?",
                (max_turns * 4,),
            ).fetchall()
        pairs: list[tuple[str, str]] = []
        pending = None
        for r in reversed(rows):
            if r["direction"] == "in":
                pending = r["text"]  # a newer message replaces one that got no reply
            elif pending is not None:
                pairs.append((pending, r["text"]))
                pending = None
        out: list[dict] = []
        for user, reply in pairs[-max_turns:] if max_turns > 0 else []:
            out += [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]
        return out

    def unseen_outbound(self) -> list[dict]:
        """Check-ins, reminders and Mac job summaries sent since the user last wrote (within 24h):
        the text conversation never saw these, so they ride along as context
        with the next message ("7h, ran 30 min" makes sense after the check-in)."""
        since = to_iso(utc_now() - timedelta(hours=24))
        with self._db() as c:
            # only real replies count (button presses and /today don't answer a check-in)
            last_in = c.execute(
                "select max(id) as id from messages where direction = 'in' and source = 'user'"
            ).fetchone()["id"] or 0
            rows = c.execute(
                "select * from messages where direction = 'out' and source in ('checkin', 'reminder', 'mac') "
                "and id > ? and created_at > ? order by id asc limit 6",
                (last_in, since),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- area agent runs ----------------
    # Written as the run goes (start, each verified change, finish), so a run
    # cut off by a restart still shows what it had already changed.

    def start_agent_run(self, area: str, task: str) -> int:
        with self._db() as c:
            cur = c.execute(
                "insert into agent_runs (area, task, status, pid, started_at) values (?, ?, 'running', ?, ?)",
                (area, task, os.getpid(), to_iso(utc_now())),
            )
            return int(cur.lastrowid)

    def add_agent_change(self, run_id: int, change: str) -> None:
        with self._db(write=True) as c:
            row = c.execute("select changes from agent_runs where id = ?", (run_id,)).fetchone()
            if row is None:
                return
            changes = json.loads(row["changes"] or "[]") + [change]
            c.execute("update agent_runs set changes = ? where id = ?", (json.dumps(changes), run_id))

    def finish_agent_run(self, run_id: int, report: str, rounds: int, cost: float) -> None:
        with self._db() as c:
            c.execute(
                "update agent_runs set status = 'done', report = ?, rounds = ?, cost = ?, finished_at = ? "
                "where id = ?",
                (report, rounds, cost, to_iso(utc_now()), run_id),
            )

    def recent_agent_runs(self, area: str, limit: int = 3, days: int = 14, stale_after_s: float = 600) -> list[dict]:
        """An area's latest runs, oldest first. A run still marked running
        whose process is gone (a restart, a crash) or that is older than
        stale_after_s (longer than any run may last) was cut off: it reports
        as "interrupted"."""
        since = to_iso(utc_now() - timedelta(days=days))
        with self._db() as c:
            rows = c.execute(
                "select * from agent_runs where area = ? and started_at > ? order by id desc limit ?",
                (area, since, limit),
            ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["changes"] = json.loads(d["changes"] or "[]")
            if d["status"] == "running" and (
                not _alive(d["pid"]) or (utc_now() - from_iso(d["started_at"])).total_seconds() > stale_after_s
            ):
                d["status"] = "interrupted"
            out.append(d)
        return out

    # ---------------- overview ----------------

    # ---------------- search (the index's `find` reads this) ----------------

    def search_text(self, stems: list[str], area: str | None = None, chat: bool = False,
                    days: int = 365) -> list[dict]:
        """Board items, reminders, log notes, agent reports and (chat=True)
        the chat, where they mention the words: `stems` are lowercase word
        stems, matched anywhere in the text. Each hit: kind, id, area, text
        (what to show), when (ISO), score (how many stems matched)."""
        stems = [s for s in dict.fromkeys(stems) if s][:8]
        if not stems:
            return []
        since = to_iso(utc_now() - timedelta(days=days))
        out: list[dict] = []

        def scan(kind: str, sql: str, cols: tuple[str, ...], args: tuple, label) -> None:
            hay = " || ' ' || ".join(f"coalesce({c}, '')" for c in cols)
            where = " or ".join(f"lower({hay}) like ?" for _ in stems)
            with self._db() as c:
                rows = c.execute(f"{sql} and ({where}) limit 300", (*args, *(f"%{s}%" for s in stems))).fetchall()
            for r in rows:
                text = " ".join(str(r[c] or "") for c in cols).lower()
                score = sum(1 for s in stems if s in text)
                out.append({"kind": kind, "id": r["id"], "area": r["area"], "score": score, **label(r)})

        a = (area, area)
        scan("item", "select * from items where (? is null or area = ?)", ("title", "details", "next_step"), a,
             lambda r: {"when": r["updated_at"], "row": dict(r)})
        scan("reminder", "select * from reminders where (? is null or area = ?) and (status = 'pending' or "
             "updated_at > ?)", ("text",), (*a, since), lambda r: {"when": r["due_at"], "row": dict(r)})
        scan("log", "select * from logs where (? is null or area = ?) and logged_at > ? and note is not null",
             ("metric", "note"), (*a, since), lambda r: {"when": r["logged_at"], "row": dict(r)})
        scan("run", "select * from agent_runs where (? is null or area = ?) and started_at > ? and "
             "status = 'done'", ("task", "report"), (*a, since), lambda r: {"when": r["started_at"], "row": dict(r)})
        if chat:
            scan("chat", "select * from messages where created_at > ? and source in "
                 "('user', 'chat', 'checkin')", ("text",), (since,),
                 lambda r: {"when": r["created_at"], "row": dict(r)})
        # best match first; among equals, the newest
        out.sort(key=lambda h: (h["score"], h["when"] or ""), reverse=True)
        return out

    def overview(self, area: str | None = None) -> dict:
        when = self.clock.when
        items = self.open_items(area, limit=60)
        last_tick = self.get_setting("scheduler_last_sweep")
        return {
            "now": self.clock.label_now(),
            "open_items": [
                {k: v for k, v in {
                    "id": i["id"], "area": i["area"], "kind": i["kind"], "title": i["title"],
                    "details": i["details"], "priority": i["priority"], "due_date": i["due_date"],
                    "stage": i.get("stage"), "next_step": i.get("next_step"),
                    "project": i.get("parent_id"), "folder": i.get("folder"),
                }.items() if v is not None}
                for i in items
            ],
            "reminders_next_7_days": [
                {"id": r["id"], "area": r["area"], "text": r["text"], "due": when(r["due_at"]),
                 "recurrence": r["recurrence"]}
                for r in self.upcoming_reminders(area, days=7, limit=40)
            ],
            "logs_last_7_days": [
                {k: v for k, v in {
                    "area": l["area"], "metric": l["metric"], "value": l["value"], "unit": l["unit"],
                    "note": l["note"], "when": when(l["logged_at"]),
                }.items() if v is not None}
                for l in self.get_logs(area, days=7, limit=40)
            ],
            "done_last_7_days": [
                {"id": i["id"], "area": i["area"], "title": i["title"]}
                for i in self.recently_done(area, days=7)
            ],
            "reminder_sender_last_ran": when(last_tick) if last_tick else "never",
        }

    def today_text(self, area_label=lambda a: a) -> str:
        """Plain-text summary for /today (no model call)."""
        when = self.clock.when
        lines = [f"📅 {self.clock.label_now()}", ""]
        reminders = [
            r for r in self.upcoming_reminders(days=2)
            if self.clock.local(r["due_at"]).date() <= self.clock.today() + timedelta(days=1)
        ]
        if reminders:
            lines.append("Reminders, today and tomorrow:")
            lines += [f"• {when(r['due_at'])}: {r['text']} ({area_label(r['area'])})" for r in reminders]
        else:
            lines.append("No reminders today or tomorrow.")
        today = self.clock.today().isoformat()
        due = [i for i in self.open_items(limit=200) if i["due_date"] and i["due_date"] <= today]
        if due:
            lines += ["", "Due today or overdue:"]
            lines += [f"• {i['title']} ({area_label(i['area'])}, {i['due_date']})" for i in due]
        doing = [p for p in self.projects() if p["stage"] == "doing"]
        if doing:
            lines += ["", "Doing:"]
            lines += [f"▶ {p['title']}" + (f" → {p['next_step']}" if p["next_step"] else "") for p in doing]
        return "\n".join(lines)

    # ---------------- backup ----------------

    def backup_to(self, dest: Path) -> Path:
        """Consistent snapshot of the live database (SQLite's backup API),
        safe to drop into iCloud because nothing writes to the copy."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        src = sqlite3.connect(self.path, timeout=15)
        try:
            out = sqlite3.connect(tmp)
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        tmp.replace(dest)
        return dest


def _alive(pid) -> bool:
    if not pid:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(int(pid), 0)  # signal 0: only asks whether the process exists
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _priority(value) -> int:
    try:
        p = int(value)
    except (TypeError, ValueError):
        raise ValueError("priority must be 1 (high), 2 (normal) or 3 (low)")
    if p not in (1, 2, 3):
        raise ValueError("priority must be 1 (high), 2 (normal) or 3 (low)")
    return p


def since_label(clock: Clock, iso: str | None) -> str | None:
    if not iso:
        return None
    delta = utc_now() - from_iso(iso)
    mins = int(delta.total_seconds() // 60)
    return f"{mins} min ago" if mins < 120 else clock.when(iso)


__all__ = ["LifeStore", "ITEM_KINDS", "ITEM_STATUSES", "REMINDER_STATUSES", "UTC", "since_label"]


def _stage(value) -> str:
    v = str(value or "").strip().lower().replace(" ", "").replace("_", "")
    v = {"todo": "todo", "doing": "doing", "inprogress": "doing", "waiting": "waiting", "blocked": "waiting"}.get(v, v)
    if v not in PROJECT_STAGES:
        raise ValueError("stage must be todo, doing or waiting (finished projects: status done)")
    return v
