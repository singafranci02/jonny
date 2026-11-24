"""Area subagents: the specialists Jarvis delegates to.

Each area (Work, Projects, Fitness, Health, Life) has an agent with more
context about that part of the user's life than Jarvis carries in every turn:
its own folder in the iCloud Jarvis folder, its CONTEXT.md brief, and that
area's reminders/tasks/logs. Jarvis hands it a self-contained task through the
`delegate` tool; the agent works with its own tools and returns a report,
which Jarvis relays to the user.

Runs on the Mac with the Claude Messages API (its own small tool loop), so
the file tools act directly on the iCloud folder.
"""

from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path

from ..tools import Tool, run_tool
from .areas import Area, read_context, workspace_root
from .clock import Clock
from .store import LifeStore
from .tools import build_life_tools

# Read-mostly helpers every area agent may use, taken from Jarvis's own tools.
SHARED_TOOL_NAMES = ("web_search", "fetch_page", "get_datetime", "calculate", "search_memory", "search_knowledge")


def area_system(area: Area, owner: str, tz_name: str) -> str:
    return f"""You are the {area.name} agent in {owner}'s personal life organiser. Jarvis, the main assistant who talks to {owner}, hands you tasks about the {area.name} area and passes your report on.

{area.focus}

Your folder is "{area.folder}/" inside the Jarvis folder in iCloud Drive (it syncs to all of {owner}'s Macs). Your file tools work only inside it. Keep it organised: list it before creating files, use clear lowercase-with-dashes filenames, one file per plan or topic, subfolders for bigger topics (for example one per project), and update existing files instead of creating near-duplicates.

CONTEXT.md in your folder is your standing brief about this part of {owner}'s life; its current text comes with every task. When a task reveals something durable about this area (a goal, a routine, a preference, a person, an important date), add it to CONTEXT.md under the right heading with ws_edit_file. Never remove what {owner} wrote there.

Your reminder, task and log tools only cover the {area.name} area. Time zone: {tz_name}; each task message states the current local time. Pass date-times as local "YYYY-MM-DDTHH:mm". Check life_overview before creating reminders or tasks so you don't duplicate existing ones.

Do the task fully, then reply to Jarvis with a short report: what you found, what you changed (files, reminders and tasks, with their times), and any question Jarvis should put to {owner}. Plain text, no headings or tables."""


class AreaAgents:
    def __init__(
        self,
        cfg: dict,
        store: LifeStore,
        clock: Clock,
        areas: list[Area],
        base_tools: list[Tool],
        log_path: Path,
    ):
        self.cfg = cfg
        self.store = store
        self.clock = clock
        self.areas = areas
        self.by_id = {a.id: a for a in areas}
        self.base_tools = base_tools
        self.log_path = log_path
        lcfg = cfg.get("life", {}) or {}
        self.owner = lcfg.get("owner", "the user")
        self.tier_name = lcfg.get("area_tier", "area")
        self.max_rounds = int(lcfg.get("subagent_max_rounds", 12))
        self.timeout_s = float(lcfg.get("subagent_timeout_s", 240))
        self._client = None

    # ---------------- plumbing ----------------

    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from .env
        return self._client

    def model_cfg(self) -> dict:
        tiers = self.cfg["llm"]["tiers"]
        if self.tier_name not in tiers:
            raise ValueError(f"llm.tiers has no '{self.tier_name}' tier for the area agents")
        return tiers[self.tier_name]

    def tools_for(self, area: Area) -> list[Tool]:
        from ..tools import workspace

        trash = (self.cfg.get("workspace", {}) or {}).get("trash_subdir", "_trash")
        area_cfg = {"workspace": {"path": str(workspace_root(self.cfg) / area.folder), "trash_subdir": trash}}
        shared = [t for t in self.base_tools if t.name in SHARED_TOOL_NAMES]
        return [
            *workspace.build(area_cfg),  # confined to this area's folder
            *build_life_tools(self.store, self.clock, self.areas, fixed=area),
            *shared,
        ]

    def _cost(self, model: str, usage) -> float:
        p = (self.cfg["llm"].get("pricing", {}) or {}).get(model)
        if not p:
            return 0.0
        return (
            (usage.input_tokens or 0) * p.get("input", 0)
            + (usage.output_tokens or 0) * p.get("output", 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p.get("cache_write", 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * p.get("cache_read", 0)
        ) / 1_000_000

    def _log(self, area: Area, task: str, report: str, rounds: int, cost: float, secs: float) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {area.id:<9} rounds={rounds} "
                    f"cost=${cost:.4f} {secs:.0f}s\n  task: {task[:300]}\n  report: {report[:500]}\n"
                )
        except OSError:
            pass

    # ---------------- the subagent loop ----------------

    def run(self, area_id: str, task: str) -> str:
        area = self.by_id.get(area_id)
        if area is None:
            return f"unknown area '{area_id}'; use one of: {', '.join(self.by_id)}"
        from ..llm.anthropic_client import request_options

        started = time.monotonic()
        deadline = started + self.timeout_s
        model_cfg = self.model_cfg()
        tools = self.tools_for(area)
        kwargs = {
            "model": model_cfg["model"],
            "max_tokens": model_cfg.get("max_tokens", 8000),
            "system": [
                {
                    "type": "text",
                    "text": area_system(area, self.owner, self.clock.tz_name),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "tools": [t.anthropic_schema() for t in tools],
            **request_options(model_cfg),
        }
        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    f"CURRENT TIME: {self.clock.label_now()}\n\n"
                    f"YOUR CONTEXT.md ({area.folder}/CONTEXT.md):\n{read_context(self.cfg, area)}\n\n"
                    f"TASK FROM JARVIS:\n{task}"
                ),
            }
        ]
        rounds, cost, text = 0, 0.0, ""
        resp = None
        while True:
            resp = self.client().messages.create(messages=messages, **kwargs)
            cost += self._cost(model_cfg["model"], resp.usage)
            text = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text").strip()
            calls = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not calls:
                break
            if rounds >= self.max_rounds or time.monotonic() > deadline:
                text = (text + "\n" if text else "") + "(I ran out of time or steps; this is partial.)"
                break
            rounds += 1
            results = self._run_tools(tools, calls)
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": results})

        if resp is not None and resp.stop_reason == "refusal":
            text = text or f"The {area.name} agent declined this task."
        elif resp is not None and resp.stop_reason == "max_tokens":
            text = (text + " " if text else "") + "(report cut off)"
        report = text or f"The {area.name} agent finished without a report."
        self._log(area, task, report, rounds, cost, time.monotonic() - started)
        return report

    def _run_tools(self, tools: list[Tool], calls) -> list[dict]:
        """Execute one round's tool calls concurrently; ALL results go back in
        a single user message, in call order."""

        def one(call) -> tuple[str, bool]:
            return run_tool(tools, call.name, dict(call.input or {}))

        # not a `with` block: its exit would wait on a hung tool forever
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            futures = [pool.submit(one, c) for c in calls]
            outputs = []
            for f in futures:
                try:
                    outputs.append(f.result(timeout=90))
                except concurrent.futures.TimeoutError:
                    outputs.append(("tool timed out; carry on without it", True))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return [
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": output,
                **({"is_error": True} if is_error else {}),
            }
            for call, (output, is_error) in zip(calls, outputs)
        ]

    # ---------------- Jarvis's side ----------------

    def delegate_tool(self) -> Tool:
        roster = "; ".join(f"{a.id} ({a.blurb})" for a in self.areas)
        return Tool(
            name="delegate",
            description=(
                f"Hand a task to an area agent: {roster}. Each owns its folder in the Jarvis iCloud folder "
                "(with a CONTEXT.md brief about that part of the user's life) plus that area's reminders, tasks "
                "and logs. Use it for anything needing that area's context or judgement: plans, reviews, "
                "organising its files, tracking progress, recording what the user tells you about that area. "
                "The agent can't see this conversation, so write a self-contained task: what the user asked, "
                "the details that matter, what to report back. For several areas, call delegate once per area "
                "in the same turn: they run in parallel. Relay the report in your own words."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "area": {"type": "string", "enum": [a.id for a in self.areas]},
                    "task": {"type": "string"},
                },
                "required": ["area", "task"],
            },
            func=lambda area, task: self.run(area, task),
            timeout=self.timeout_s + 30,
        )
