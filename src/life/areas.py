"""Life areas and the folder they live in.

One main folder (config `workspace.path`, by default iCloud Drive/Jarvis, so
it syncs to every Mac on the same Apple ID) with a subfolder per area. Each
area folder holds a CONTEXT.md: the area agent's standing brief about that
part of the user's life, read before every task and kept current by the agent
and the user alike.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Area:
    id: str
    name: str
    emoji: str
    folder: str
    blurb: str
    focus: str

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


def load_areas(cfg: dict) -> list[Area]:
    areas = []
    for a in (cfg.get("life", {}) or {}).get("areas", []) or []:
        areas.append(
            Area(
                id=a["id"],
                name=a["name"],
                emoji=a.get("emoji", ""),
                folder=a.get("folder", a["name"]),
                blurb=a.get("blurb", ""),
                focus=" ".join(str(a.get("focus", "")).split()),
            )
        )
    if len({a.id for a in areas}) != len(areas):
        raise ValueError("life.areas: ids must be unique")
    return areas


def workspace_root(cfg: dict) -> Path:
    ws = cfg.get("workspace", {}) or {}
    return Path(os.path.expanduser(ws.get("path", "~/JarvisWorkspace")))


def inbox_name(cfg: dict) -> str:
    return (cfg.get("life", {}) or {}).get("inbox", "Inbox")


CONTEXT_TEMPLATE = """# {name}: context for the {name} agent

The {name} agent reads this file before every task, so the more it says about
this part of your life, the better the agent can act like it knows you. Edit it
any time, from either Mac. The agent adds what it learns under the last heading.

## What this area covers
{blurb}

## Goals right now
-

## Routines and preferences
-

## People
-

## Dates and constraints to respect
-

## Notes the agent has learned
-
"""

README_TEMPLATE = """# Jarvis

This is the main folder Jarvis and its area agents work in. It lives in iCloud
Drive, so it is the same on every Mac signed in to your Apple ID.

{area_lines}
- Inbox/: drop anything here that hasn't found a home yet; Jarvis can sort it.
- _trash/: older versions of files, kept automatically before any overwrite.
- _backups/: nightly copies of Jarvis's reminders/tasks/logs database.

Each area folder has a CONTEXT.md: what matters in that area (goals, routines,
people, dates). Its agent reads it before every task. Fill these in: it is the
single biggest upgrade to how useful the agents are.

Reminders, tasks and logs themselves live in a database on the Mac mini (a
live database must not sit in iCloud); the dashboard and Telegram show them.
"""


def ensure_folders(cfg: dict) -> dict:
    """Create the main folder, area folders, Inbox and starter files that are
    missing. Never overwrites anything. Returns what it did."""
    root = workspace_root(cfg)
    created: list[str] = []
    if not root.exists():
        root.mkdir(parents=True)
        created.append(str(root))
    areas = load_areas(cfg)
    for name in [inbox_name(cfg), "_backups", *(a.folder for a in areas)]:
        p = root / name
        if not p.exists():
            p.mkdir(parents=True)
            created.append(f"{name}/")
    for a in areas:
        ctx = root / a.folder / "CONTEXT.md"
        if not ctx.exists():
            ctx.write_text(CONTEXT_TEMPLATE.format(name=a.name, blurb=a.blurb or "-"))
            created.append(f"{a.folder}/CONTEXT.md")
    readme = root / "README.md"
    if not readme.exists():
        lines = "\n".join(f"- {a.folder}/: {a.blurb}" for a in areas)
        readme.write_text(README_TEMPLATE.format(area_lines=lines))
        created.append("README.md")
    return {"root": str(root), "created": created}


PROJECT_TEMPLATE = """# {title}: project brief

The {area} agent reads this with every task about this project and keeps it
current. Edit it any time. Concrete tasks live on the project's tracker
(Telegram: Board, then the project), not here.

## What it is
-

## Where it stands
-

## Decisions
-

## Next
-
"""


def project_folder_name(title: str) -> str:
    """A safe folder name for a project title ("YardBird", "Jarvis website")."""
    import re

    name = re.sub(r"[^\w .&()-]+", "", title, flags=re.UNICODE).strip(" .")
    return " ".join(name.split())[:60] or "Project"


def ensure_project_folder(cfg: dict, area: Area, project: dict) -> tuple[str, str]:
    """The project's folder inside its area's (created with a brief on first
    use; never overwritten). Returns (folder name, CONTEXT.md text)."""
    name = project.get("folder") or project_folder_name(project["title"])
    folder = workspace_root(cfg) / area.folder / name
    folder.mkdir(parents=True, exist_ok=True)
    ctx = folder / "CONTEXT.md"
    if not ctx.exists():
        ctx.write_text(PROJECT_TEMPLATE.format(title=project["title"], area=area.name))
    text = ctx.read_text()
    return name, (text if len(text) <= 12000 else text[:12000] + "\n…(truncated)")


def read_context(cfg: dict, area: Area, limit: int = 12000) -> str:
    p = workspace_root(cfg) / area.folder / "CONTEXT.md"
    try:
        text = p.read_text(errors="replace")
    except FileNotFoundError:
        return "(no CONTEXT.md yet; create one with what you learn about this area)"
    except OSError as e:
        # iCloud may still be downloading the file; don't fail the task over it
        return f"(CONTEXT.md could not be read right now: {e})"
    return text[:limit] + ("\n...(truncated)" if len(text) > limit else "")


def full_disk_access_target() -> str:
    """What to add under Full Disk Access so this python can reach iCloud
    Drive. Framework builds (Homebrew, python.org) start through a
    bin/python3.x launcher that hands over to Resources/Python.app, and macOS
    checks the identity of the program actually running, so the app bundle
    is what needs the grant. Re-add it after a Python minor upgrade."""
    import sys
    import sysconfig

    if sysconfig.get_config_var("PYTHONFRAMEWORK"):
        app = Path(sys.base_prefix) / "Resources" / "Python.app"
        if app.exists():
            return os.path.realpath(app)
    return os.path.realpath(sys.executable)


def folder_status(cfg: dict) -> dict:
    """For /life/status and `make life ARGS=status`: is the folder reachable
    and writable from this process (iCloud permissions can block launchd)?"""
    root = workspace_root(cfg)
    status = {"root": str(root), "exists": False, "writable": False, "areas": {}}
    try:
        # on Python <= 3.11, exists()/is_dir() raise PermissionError when macOS
        # privacy (TCC) blocks the folder, instead of returning False
        status["exists"] = root.exists()
        if status["exists"]:
            probe = root / ".jarvis-write-test"
            probe.write_text("ok")
            probe.unlink()
            status["writable"] = True
            for a in load_areas(cfg):
                status["areas"][a.id] = (root / a.folder).is_dir()
    except OSError as e:
        status["error"] = str(e)
    return status
