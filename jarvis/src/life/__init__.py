"""Jarvis as the main agent of a life organiser.

    Jarvis (main agent) ── delegate ──► area agents (Work, Projects, Fitness, Health, Life)
        │                                   │ each: its iCloud folder + CONTEXT.md,
        │ life tools                        │ its area's reminders/tasks/logs
        ▼                                   ▼
    data/life.db (reminders, tasks, logs)   iCloud Drive/Jarvis/<Area>/...

attach(session) gives a Session the life tools, the delegate tool and the
read-only project_* tools over his other projects (sources.py; each area
agent sees only its own area's). The server additionally runs LifeService
(Telegram + scheduler); see service.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .agents import AreaAgents
from .areas import Area, load_areas
from .clock import Clock
from .sources import Source, build_source_tools, load_sources
from .store import LifeStore
from .strava import Strava, build_strava_tools
from .tools import build_life_tools


@dataclass
class LifeContext:
    clock: Clock
    store: LifeStore
    areas: list[Area]
    agents: AreaAgents
    sources: list[Source] | None = None
    strava: Strava | None = None


def system_timezone() -> str | None:
    """The Mac's own zone (System Settings > Date & Time), from the
    /etc/localtime link, e.g. .../zoneinfo/Europe/London -> Europe/London."""
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    if marker not in target:
        return None
    name = target.split(marker, 1)[1]
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
    except Exception:
        return None
    return name


def make_context(cfg: dict, base_tools: list) -> LifeContext:
    from ..config import ROOT

    lcfg = cfg.get("life", {}) or {}
    # config > .env TIMEZONE > the Mac's own timezone > UTC
    clock = Clock(lcfg.get("timezone") or os.environ.get("TIMEZONE") or system_timezone() or "UTC")
    store = LifeStore(ROOT / lcfg.get("db", "data/life.db"), clock)
    areas = load_areas(cfg)
    sources = load_sources(cfg, [a.id for a in areas])
    ids = [a.id for a in areas]
    strava_area = lcfg.get("strava_area", "fitness")
    strava = Strava(store, clock, ROOT / "data" / "strava.json", strava_area if strava_area in ids else ids[0])
    agents = AreaAgents(cfg, store, clock, areas, base_tools, ROOT / "data" / "life_agents.log", sources)
    agents.strava = strava
    return LifeContext(clock=clock, store=store, areas=areas, agents=agents, sources=sources, strava=strava)


def attach(session) -> LifeContext:
    """Add the life tools and `delegate` to a Session's tool list."""
    ctx = make_context(session.cfg, base_tools=list(session.tools))
    session.tools.extend(build_life_tools(ctx.store, ctx.clock, ctx.areas))
    session.tools.extend(build_source_tools(ctx.sources or []))
    session.tools.extend(build_strava_tools(ctx.strava))
    session.tools.append(ctx.agents.delegate_tool())
    return ctx
