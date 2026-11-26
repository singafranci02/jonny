"""Life-organiser tools: reminders, tasks/goals/notes, logs.

Jarvis gets them across every area; each area agent gets a copy locked to its
own area (`fixed`): the `area` argument disappears from the schemas, is filled
in automatically, and records from other areas can't be touched.
"""

from __future__ import annotations

import json
from datetime import timedelta

from ..tools import Tool
from .areas import Area
from .clock import RECURRENCES, Clock, from_iso, utc_now
from .store import ITEM_KINDS, LifeStore

_DT = 'Local date-time in the user\'s timezone, "YYYY-MM-DDTHH:mm"'


def build_life_tools(store: LifeStore, clock: Clock, areas: list[Area], fixed: Area | None = None) -> list[Tool]:
    ids = [a.id for a in areas]
    labels = {a.id: a.label for a in areas}
    scope = f" in the {fixed.name} area" if fixed else ""

    def area_for(given: str | None) -> str:
        if fixed is not None:
            return fixed.id
        if given not in ids:
            raise ValueError(f"area must be one of: {', '.join(ids)}")
        return given

    def owned(record: dict | None, what: str, rid) -> dict:
        if record is None:
            raise ValueError(f"no {what} #{rid}")
        if fixed is not None and record["area"] != fixed.id:
            raise ValueError(f"{what} #{rid} belongs to {record['area']}, not {fixed.id}")
        return record

    def parse_dt(text: str):
        dt = clock.parse(text)
        if dt is None:
            raise ValueError(f'could not read the time "{text}"; use YYYY-MM-DDTHH:mm')
        return dt

    def with_area(props: dict, required: list[str]) -> tuple[dict, list[str]]:
        if fixed is not None:
            return props, required
        return {"area": {"type": "string", "enum": ids, "description": "life area"}, **props}, ["area", *required]

    def sender_warning() -> str:
        """Reminders are only sent by Jarvis's server (the Mac mini). If no
        sender has swept this machine's database lately, say so instead of
        silently storing a reminder nothing will deliver."""
        last = store.get_setting("scheduler_last_sweep")
        if last and utc_now() - from_iso(last) < timedelta(minutes=10):
            return ""
        return (" Warning: the reminder sender isn't running against this machine's database (it runs in "
                "Jarvis's server on the Mac mini), so this may not be delivered. Tell the user.")

    # ---------------- functions ----------------

    def overview(area: str | None = None) -> str:
        if fixed is not None:
            scoped = fixed.id
        elif area is None:
            scoped = None
        else:
            scoped = area_for(area)
        return json.dumps(store.overview(scoped), ensure_ascii=False)

    def add_reminder(text: str, due_at: str, recurrence: str = "none", area: str | None = None) -> str:
        a = area_for(area)
        due = parse_dt(due_at)
        recurrence = recurrence or "none"
        if recurrence == "none" and due < utc_now():
            raise ValueError(f"{clock.when(due)} is in the past; check the current time with life_overview")
        r = store.add_reminder(a, text, due, recurrence)
        rep = "" if r["recurrence"] == "none" else f", repeats {r['recurrence']}"
        return f"Reminder #{r['id']} \"{r['text']}\" set for {clock.when(r['due_at'])}{rep} ({labels[a]}).{sender_warning()}"

    def update_reminder(id: int, text: str | None = None, due_at: str | None = None,
                        recurrence: str | None = None, status: str | None = None,
                        area: str | None = None) -> str:
        owned(store.get_reminder(int(id)), "reminder", id)
        r = store.update_reminder(
            int(id),
            text=text,
            due=parse_dt(due_at) if due_at else None,
            recurrence=recurrence,
            status=status,
            area=area_for(area) if (area is not None and fixed is None) else None,
        )
        return (f"Reminder #{r['id']}: \"{r['text']}\", {r['status']}, {clock.when(r['due_at'])}, "
                f"repeats {r['recurrence']} ({labels.get(r['area'], r['area'])}).")

    def parent_for(project) -> int | None:
        """The project a task goes on (its tracker); for an area agent, only
        its own area's projects."""
        if project in (None, "", 0, "0") or str(project).strip().lower() in ("none", "null"):
            return None
        p = store.find_project(project)
        if p is None:
            raise ValueError(f"no project {project}; project ids come from life_overview")
        return owned(p, "project", project)["id"]

    def add_item(kind: str, title: str, details: str | None = None, priority: int = 2,
                 due_date: str | None = None, area: str | None = None, stage: str | None = None,
                 next_step: str | None = None, project=None) -> str:
        parent = parent_for(project)
        a = store.get_item(parent)["area"] if parent else area_for(area)
        due = None
        if due_date:
            due = clock.parse_date(due_date)
            if due is None:
                raise ValueError(f'could not read the date "{due_date}"; use YYYY-MM-DD')
        i = store.add_item(a, kind, title, details, priority if priority is not None else 2, due,
                           stage=stage, next_step=next_step, parent_id=parent)
        return f"Added {i['kind']} #{i['id']} \"{i['title']}\" to {labels[a]}" + _board(i) + on_tracker(i) + (
            f", due {i['due_date']}." if i["due_date"] else "."
        )

    def update_item(id: int, title: str | None = None, details: str | None = None,
                    status: str | None = None, priority: int | None = None, due_date: str | None = None,
                    kind: str | None = None, area: str | None = None, stage: str | None = None,
                    next_step: str | None = None, project=None) -> str:
        owned(store.get_item(int(id)), "item", id)
        patch: dict = {"title": title, "details": details, "status": status, "priority": priority, "kind": kind,
                       "stage": stage}
        if next_step is not None:
            patch["next_step"] = "" if next_step.strip().lower() in ("none", "null") else next_step
        if due_date is not None:
            if due_date.strip().lower() in ("", "none", "null"):
                patch["due_date"] = None
            else:
                d = clock.parse_date(due_date)
                if d is None:
                    raise ValueError(f'could not read the date "{due_date}"; use YYYY-MM-DD or "none"')
                patch["due_date"] = d
        if area is not None and fixed is None:
            patch["area"] = area_for(area)
        if project is not None:
            patch["parent_id"] = parent_for(project)
        i = store.update_item(int(id), **patch)
        return (f"Item #{i['id']} \"{i['title']}\": {i['status']}, {labels.get(i['area'], i['area'])}" + _board(i)
                + on_tracker(i) + (
            f", due {i['due_date']}." if i["due_date"] else "."
        ))

    def on_tracker(i: dict) -> str:
        if not i.get("parent_id"):
            return ""
        p = store.get_item(i["parent_id"])
        return f", on project #{i['parent_id']} {p['title']!r}'s tracker" if p else ""

    def log_entry(metric: str, value: float | None = None, unit: str | None = None,
                  note: str | None = None, logged_at: str | None = None, area: str | None = None) -> str:
        a = area_for(area)
        entry = store.add_log(a, metric, value, unit, note, parse_dt(logged_at) if logged_at else None)
        v = ""
        if entry["value"] is not None:
            v = f" {entry['value']:g}" + (f" {entry['unit']}" if entry["unit"] else "")
        return f"Logged {entry['metric']}{v} ({labels[a]}, {clock.when(entry['logged_at'])})."

    def get_logs(metric: str | None = None, days: int = 30, area: str | None = None) -> str:
        if fixed is not None:
            a = fixed.id
        else:
            a = area_for(area) if area else None
        rows = store.get_logs(a, metric, int(days or 30), limit=300)
        out = [
            {k: v for k, v in {
                "area": r["area"], "metric": r["metric"], "value": r["value"], "unit": r["unit"],
                "note": r["note"], "when": clock.when(r["logged_at"]),
            }.items() if v is not None}
            for r in rows
        ]
        return json.dumps(out, ensure_ascii=False) if out else "no logs match"

    # ---------------- schemas ----------------

    tools: list[Tool] = []

    ov_props = {} if fixed else {"area": {"type": "string", "enum": ids, "description": "limit to one area (optional)"}}
    tools.append(Tool(
        name="life_overview",
        description=(
            f"Current local time, open tasks/goals/notes, reminders in the next 7 days, logs from the last "
            f"7 days and what was done this week{scope}. Call it before summarising, planning, or answering "
            "questions about the user's plans, reminders or progress."
        ),
        parameters={"type": "object", "properties": ov_props},
        func=overview,
    ))

    p, req = with_area(
        {
            "text": {"type": "string", "description": "the reminder itself, e.g. 'Call the dentist'"},
            "due_at": {"type": "string", "description": _DT},
            "recurrence": {"type": "string", "enum": list(RECURRENCES), "description": "default none"},
        },
        ["text", "due_at"],
    )
    tools.append(Tool(
        name="life_add_reminder",
        description=f"Schedule a reminder{scope}. It arrives on Telegram at due_at with Done/Snooze buttons. Use for anything time-bound.",
        parameters={"type": "object", "properties": p, "required": req},
        func=add_reminder,
    ))

    upd = {
        "id": {"type": "integer"},
        "text": {"type": "string"},
        "due_at": {"type": "string", "description": _DT},
        "recurrence": {"type": "string", "enum": list(RECURRENCES)},
        "status": {"type": "string", "enum": ["pending", "done", "cancelled"]},
    }
    if fixed is None:
        upd["area"] = {"type": "string", "enum": ids}
    tools.append(Tool(
        name="life_update_reminder",
        description=f"Reschedule, edit, complete or cancel a reminder{scope} by id (ids come from life_overview).",
        parameters={"type": "object", "properties": upd, "required": ["id"]},
        func=update_reminder,
    ))

    p, req = with_area(
        {
            "kind": {"type": "string", "enum": list(ITEM_KINDS),
                     "description": "task = something to do; goal = an outcome being worked towards; note = "
                                    "reference info; project = a multi-step effort tracked on his board"},
            "title": {"type": "string"},
            "details": {"type": "string"},
            "priority": {"type": "integer", "enum": [1, 2, 3], "description": "1 high, 2 normal (default), 3 low"},
            "due_date": {"type": "string", "description": 'optional "YYYY-MM-DD"'},
            **_BOARD_PROPS,
        },
        ["kind", "title"],
    )
    tools.append(Tool(
        name="life_add_item",
        description=(f"Add a task, goal, note or board project{scope}. A task that belongs to a board project "
                     "goes on that project's tracker: pass project (its id)."),
        parameters={"type": "object", "properties": p, "required": req},
        func=add_item,
    ))

    item = {
        "id": {"type": "integer"},
        "title": {"type": "string"},
        "details": {"type": "string"},
        "status": {"type": "string", "enum": ["open", "done", "archived"]},
        "priority": {"type": "integer", "enum": [1, 2, 3]},
        "due_date": {"type": "string", "description": '"YYYY-MM-DD", or "none" to clear'},
        "kind": {"type": "string", "enum": list(ITEM_KINDS)},
        **_BOARD_PROPS,
    }
    if fixed is None:
        item["area"] = {"type": "string", "enum": ids}
    tools.append(Tool(
        name="life_update_item",
        description=(f"Edit, complete (status done), reopen or archive a task, goal, note or project{scope} by id. "
                     "For a project, keep stage and next_step current as things move."),
        parameters={"type": "object", "properties": item, "required": ["id"]},
        func=update_item,
    ))

    p, req = with_area(
        {
            "metric": {"type": "string",
                       "description": 'short lowercase name, reused consistently: "workout", "sleep_hours", "weight", "mood", "steps"…'},
            "value": {"type": "number", "description": "numeric value if any (minutes, hours, kg, 1-5…)"},
            "unit": {"type": "string"},
            "note": {"type": "string"},
            "logged_at": {"type": "string", "description": _DT + "; defaults to now"},
        },
        ["metric"],
    )
    tools.append(Tool(
        name="life_log",
        description=f"Record something that happened{scope}: a workout, sleep, weight, mood, a medication taken, money spent…",
        parameters={"type": "object", "properties": p, "required": req},
        func=log_entry,
    ))

    logs = {"metric": {"type": "string"}, "days": {"type": "integer", "description": "how far back, default 30"}}
    if fixed is None:
        logs["area"] = {"type": "string", "enum": ids}
    tools.append(Tool(
        name="life_get_logs",
        description=f"Log history{scope}, newest first, for spotting trends. Filter by metric; days defaults to 30.",
        parameters={"type": "object", "properties": logs},
        func=get_logs,
    ))
    return tools


# the board: a project's stage and its one concrete next step
_BOARD_PROPS = {
    "stage": {"type": "string", "enum": ["todo", "doing", "waiting"],
              "description": "projects only: todo (not started), doing (active), waiting (blocked on someone "
                             "or something); finished projects get status done"},
    "next_step": {"type": "string",
                  "description": 'projects only: the one concrete next action, a few words ("Send draft to Sam"); '
                                 '"none" clears it'},
    "project": {"type": "integer",
                "description": "tasks/notes only: id of the board project whose tracker this belongs to; "
                               "0 takes it off"},
}


def _board(item: dict) -> str:
    if item.get("kind") != "project":
        return ""
    stage = "done" if item["status"] == "done" else item.get("stage") or "todo"
    return f", {stage}" + (f", next: {item['next_step']}" if item.get("next_step") else "")
