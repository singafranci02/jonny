"""The Telegram board: menu bar, project board, project cards, tap-to-log.

Pure text + keyboard builders; LifeService wires them to the store and the
bot. Taps never call a model, so they are instant, free and predictable.
Jarvis still sees and edits the same projects through his life tools.

Callback data (Telegram caps it at 64 bytes):
  bd:<area|all>   show the board, whole or one area
  pj:<id>         open a project card
  ps:<id>:<stage> set todo / doing / waiting, or done
  pn:<id>         ask for a new next step (force-reply)
  tk:<id>         tick a task on a project's tracker done (the card refreshes)
  pa:<area|>      start a new project (force-reply for its name)
  pc:<area>       file the pending new project under an area
  lg:<key>:<val>  log today's sleep / mood / training
"""

from __future__ import annotations

from datetime import date

MENU_BOARD, MENU_ADD, MENU_TODAY, MENU_LOG = "📋 Board", "➕ Add", "📅 Today", "📝 Log"
MENU_TEXTS = (MENU_BOARD, MENU_ADD, MENU_TODAY, MENU_LOG)
MENU_VERSION = 1  # bump to re-send the menu bar after changing it

STAGE_ICON = {"todo": "○", "doing": "▶", "waiting": "⏸", "done": "✅"}
STAGE_NAME = {"todo": "To do", "doing": "Doing", "waiting": "Waiting", "done": "Done"}
FOCUS_LIMIT = 3  # more projects than this in Doing gets a nudge (GTD-style focus)

# tap-to-log: key -> (preferred area, metric, unit, button values, first-button emoji)
LOG_ROWS = {
    "sleep": ("health", "sleep_hours", "hours", (5, 6, 7, 8, 9), "😴"),
    "mood": ("health", "mood", "1-5", (1, 2, 3, 4, 5), "🙂"),
    "trained": ("fitness", "trained", None, (1, 0), "🏃"),
}


def menu_keyboard() -> dict:
    return {
        "keyboard": [[{"text": MENU_BOARD}, {"text": MENU_ADD}], [{"text": MENU_TODAY}, {"text": MENU_LOG}]],
        "is_persistent": True,
        "resize_keyboard": True,
    }


def force_reply(placeholder: str) -> dict:
    return {"force_reply": True, "input_field_placeholder": placeholder[:64]}


def stage_of(p: dict) -> str:
    return "done" if p.get("status") == "done" else (p.get("stage") or "todo")


def _short_date(iso: str | None) -> str:
    try:
        d = date.fromisoformat(iso or "")
    except ValueError:
        return iso or ""
    return f"{d.day} {d:%b}"


def _project_line(p: dict, tasks: int = 0) -> str:
    line = f"{STAGE_ICON[stage_of(p)]} {p['title']}"
    if p.get("next_step"):
        line += f" → {p['next_step']}"
    if p.get("due_date"):
        line += f" · {_short_date(p['due_date'])}"
    if tasks:
        line += f" · {tasks} task{'s' if tasks != 1 else ''}"
    return line


project_line = _project_line


def _rows(buttons: list[dict], per_row: int) -> list[list[dict]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data[:64]}


def board_view(projects: list[dict], areas: list, area_id: str | None = None,
               counts: dict[int, int] | None = None) -> tuple[str, dict]:
    """The whole board (area_id None) or one area's list, plus its buttons.
    counts: open tracker tasks per project id."""
    counts = counts or {}

    def project_line(p: dict) -> str:
        return _project_line(p, counts.get(p["id"], 0))

    by_area = {a.id: [p for p in projects if p["area"] == a.id] for a in areas}
    if area_id and area_id in by_area:
        area = next(a for a in areas if a.id == area_id)
        mine = by_area[area_id]
        text = f"{area.emoji} {area.name}\n" + ("\n".join(project_line(p) for p in mine) or "No projects yet.")
        buttons = [[_btn(f"{STAGE_ICON[stage_of(p)]} {p['title'][:40]}", f"pj:{p['id']}")] for p in mine]
        buttons.append([_btn("◀ Board", "bd:all"), _btn("➕ New", f"pa:{area_id}")])
        return text, {"inline_keyboard": buttons}

    blocks = []
    for a in areas:
        if by_area[a.id]:
            blocks.append(f"{a.emoji} {a.name}\n" + "\n".join(project_line(p) for p in by_area[a.id]))
    if not blocks:
        text = "📋 Board is empty. Tap ➕ New to add your first project."
    else:
        text = "📋 Board\n\n" + "\n\n".join(blocks)
        doing = sum(1 for p in projects if stage_of(p) == "doing")
        if doing > FOCUS_LIMIT:
            text += f"\n\n⚠ {doing} in Doing. Pick {FOCUS_LIMIT} to focus on."
    area_buttons = [
        _btn(f"{a.emoji} {a.name}" + (f" ({len(by_area[a.id])})" if by_area[a.id] else ""), f"bd:{a.id}")
        for a in areas
    ]
    keyboard = _rows(area_buttons, 2) + [[_btn("➕ New project", "pa:")]]
    return text, {"inline_keyboard": keyboard}


TASKS_SHOWN = 8  # tracker lines on a card; the first TASK_BUTTONS get a tick button
TASK_BUTTONS = 5


def project_card(p: dict, area, tasks: list[dict] | None = None) -> tuple[str, dict]:
    """A project with its stage buttons and its tracker (open tasks first,
    then ones ticked off lately)."""
    stage = stage_of(p)
    lines = [f"{area.emoji if area else ''} {p['title']}".strip(), f"{STAGE_ICON[stage]} {STAGE_NAME[stage]}"]
    lines.append(f"Next: {p['next_step']}" if p.get("next_step") else "Next: (none yet)")
    if p.get("due_date"):
        lines.append(f"Due: {_short_date(p['due_date'])}")
    tasks = tasks or []
    open_tasks = [t for t in tasks if t["status"] == "open"]
    if tasks:
        lines.append("")
        lines.append(f"Tracker ({len(open_tasks)} open)")
        for t in tasks[:TASKS_SHOWN]:
            mark = "☐" if t["status"] == "open" else "☑"
            lines.append(f"{mark} {t['title']}" + (f" · {_short_date(t['due_date'])}" if t.get("due_date") else ""))
        if len(tasks) > TASKS_SHOWN:
            lines.append(f"… and {len(tasks) - TASKS_SHOWN} more")
    pid = p["id"]

    def stage_btn(s: str) -> dict:
        mark = "✓ " if s == stage else ""
        return _btn(f"{mark}{STAGE_ICON[s]} {STAGE_NAME[s]}", f"ps:{pid}:{s}")

    if stage == "done":
        rows = [[_btn("↩ Reopen", f"ps:{pid}:todo")]]
    else:
        rows = [
            [stage_btn("todo"), stage_btn("doing"), stage_btn("waiting")],
            [_btn("✅ Done", f"ps:{pid}:done"), _btn("✏️ Next step", f"pn:{pid}")],
        ]
    if stage != "done":
        rows = [[_btn(f"☐ {t['title'][:40]}", f"tk:{t['id']}")] for t in open_tasks[:TASK_BUTTONS]] + rows
    back = area.name if area else "Board"
    rows.append([_btn(f"◀ {back}", f"bd:{p['area']}")])
    return "\n".join(lines), {"inline_keyboard": rows}


def area_picker(areas: list) -> dict:
    return {"inline_keyboard": _rows([_btn(f"{a.emoji} {a.name}", f"pc:{a.id}") for a in areas], 2)}


def log_target(key: str, area_ids: list[str]) -> tuple[str, str, str | None]:
    """(area, metric, unit) for a tap-to-log row; falls back to the first
    area if the preferred one isn't configured."""
    area, metric, unit, _, _ = LOG_ROWS[key]
    return (area if area in area_ids else area_ids[0]), metric, unit


def log_keyboard(values: dict) -> dict:
    """values: key -> today's value (or None). The chosen value is ticked."""
    rows = []
    for key, (_, _, _, choices, emoji) in LOG_ROWS.items():
        current = values.get(key)
        row = []
        for i, v in enumerate(choices):
            if key == "trained":
                label = "Trained" if v else "Rest day"
            else:
                label = f"{v}h" if key == "sleep" else str(v)
            tick = current is not None and float(current) == float(v)
            prefix = "✓" if tick else (f"{emoji} " if i == 0 else "")
            row.append(_btn(f"{prefix}{label}", f"lg:{key}:{v}"))
        rows.append(row)
    return {"inline_keyboard": rows}


def log_confirmation(key: str, value: float) -> str:
    if key == "sleep":
        return f"😴 Sleep {value:g}h"
    if key == "mood":
        return f"🙂 Mood {value:g}/5"
    return "🏃 Trained" if value else "🛋 Rest day"
