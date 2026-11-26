"""Area subagents: the specialists Jarvis delegates to.

Each area (Work, Projects, Fitness, Health, Life) has an agent with more
context about that part of the user's life than Jarvis carries in every turn:
its own folder in the iCloud Jarvis folder, its CONTEXT.md brief, and that
area's reminders/tasks/logs. Jarvis hands it a self-contained task through the
`delegate` tool; the agent works with its own tools and returns a report,
which Jarvis relays to the user.

Runs on the Mac with the Claude Messages API (its own small tool loop), so
the file tools act directly on the iCloud folder.

Each agent keeps a thread of its own (an idea from LangGraph, without the
library): every run is recorded in life.db as it happens (task, each verified
change, the report), and the agent's next run sees its last few. A follow-up
("make Tuesday Wednesday", his answer to the agent's Question) continues the
earlier work, and a run cut off by a restart is finished, not duplicated.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from pathlib import Path

from ..tools import Tool, run_tool
from .areas import Area, read_context, workspace_root
from .clock import Clock
from .sources import Source, build_source_tools
from .store import LifeStore
from .tools import build_life_tools

# Read-mostly helpers every area agent may use, taken from Jarvis's own tools.
SHARED_TOOL_NAMES = ("web_search", "fetch_page", "get_datetime", "calculate", "search_memory", "search_knowledge")

# Tools that change something. Their successful results become the report's
# verified change list (a crewAI-style output guardrail, done structurally):
# Jarvis relays what actually happened, not what the agent says happened.
MUTATING_TOOLS = frozenset({
    "life_add_reminder", "life_update_reminder", "life_add_item", "life_update_item", "life_log",
    "ws_create_file", "ws_edit_file", "ws_move",
})
CHANGES_HEADER = "Verified changes:"
RECENT_RUNS = 3  # how many of its own past runs an agent sees with each task

log = logging.getLogger(__name__)


def area_system(area: Area, owner: str, tz_name: str, projects: bool = False) -> str:
    return f"""You are the {area.name} agent in {owner}'s personal life organiser. Jarvis, the main assistant who talks to {owner}, hands you tasks about the {area.name} area and passes your report on.

{area.focus}

Your folder is "{area.folder}/" inside the Jarvis folder in iCloud Drive (it syncs to all of {owner}'s Macs). Your file tools work only inside it. Keep it organised: list it before creating files, use clear lowercase-with-dashes filenames, one file per plan or topic, subfolders for bigger topics (for example one per project), and update existing files instead of creating near-duplicates.

CONTEXT.md in your folder is your standing brief about this part of {owner}'s life; its current text comes with every task. When a task reveals something durable about this area (a goal, a routine, a preference, a person, an important date), add it to CONTEXT.md under the right heading with ws_edit_file. Never remove what {owner} wrote there: when a fact stops being true, strike it through (~~old~~) and write the new one beside it, so the history stays.

Your reminder, task and log tools only cover the {area.name} area. Time zone: {tz_name}; each task message states the current local time. Pass date-times as local "YYYY-MM-DDTHH:mm". Check life_overview before creating reminders or tasks so you don't duplicate existing ones.

Your last few runs in this area come with each task. When the task follows up on one (an answer to your Question, a change to a plan you made), continue from it instead of starting over. If a run was cut off, check what it already changed before redoing anything.

Projects on {owner}'s board that belong to your area are items of kind project, each with a stage (todo, doing, waiting) and one concrete next step. Keep them current: when your work moves a project, update its stage and next step; when a plan you write covers one, make its next step the first action of that plan.

{_projects_line(owner) if projects else ""}Do the task fully, then reply to Jarvis in this shape (plain text, no headings or tables):
Line 1: the answer for {owner} in one short sentence; Jarvis may pass it on word for word.
Then, only if needed, up to 4 short lines of detail Jarvis needs.
Last, only if something needs {owner}'s decision: "Question: ...".
Don't list what you changed: a verified list of your changes is attached to your report automatically."""


def _projects_line(owner: str) -> str:
    return (f"Some of {owner}'s projects live outside your folder, in their own folders and GitHub repos. "
            "project_list, project_activity and project_read read them (never change them). Check them before "
            "saying where a project stands, and quote dates and owners from the source, not from memory.\n\n")


class AreaAgents:
    def __init__(
        self,
        cfg: dict,
        store: LifeStore,
        clock: Clock,
        areas: list[Area],
        base_tools: list[Tool],
        log_path: Path,
        sources: list[Source] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.clock = clock
        self.areas = areas
        self.by_id = {a.id: a for a in areas}
        self.base_tools = base_tools
        self.log_path = log_path
        self.sources = sources or []
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
            *build_source_tools(self.sources, area),  # read-only: this area's projects only
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
                    f"cost=${cost:.4f} {secs:.0f}s\n  task: {task[:300]}\n  report: {report[:1500]}\n"
                )
        except OSError:
            pass

    # ---------------- the agent's thread ----------------

    def _record(self, fn, *args):
        """Run history is a help, never a blocker: a failed write is logged."""
        try:
            return fn(*args)
        except Exception as e:
            log.warning("agent run history: %s failed: %s", fn.__name__, e)
            return None

    def recent_work(self, area: Area) -> str:
        runs = self._record(self.store.recent_agent_runs, area.id, RECENT_RUNS, 14, self.timeout_s + 120) or []
        if not runs:
            return ""
        out = []
        for r in runs:
            head = f"[{self.clock.when(r['started_at'])}]"
            task = _squash(r["task"], 300)
            changes = "; ".join(_squash(c, 160) for c in r["changes"][:8])
            if r["status"] == "interrupted":
                out.append(f"{head} CUT OFF before finishing (a restart or crash). Task: {task}\n"
                           f"Changes it made before the cut: {changes or 'none'}")
                continue
            if r["status"] == "running":
                out.append(f"{head} still running in parallel. Task: {task}")
                continue
            prose = (r["report"] or "").split(f"\n\n{CHANGES_HEADER}")[0]
            out.append(f"{head} Task: {task}\nYour report: {_squash(prose, 500)}\nChanges: {changes or 'none'}")
        return "YOUR RECENT RUNS in this area (oldest first; for continuity, don't redo them):\n" + "\n\n".join(out)

    # ---------------- the subagent loop ----------------

    def run(self, area_id: str, task: str, expected_output: str | None = None) -> str:
        area = self.by_id.get(area_id)
        if area is None:
            return f"unknown area '{area_id}'; use one of: {', '.join(self.by_id)}"
        from ..llm.anthropic_client import request_options

        started = time.monotonic()
        deadline = started + self.timeout_s
        recent = self.recent_work(area)  # read before this run is recorded
        run_id = self._record(self.store.start_agent_run, area.id, task)
        model_cfg = self.model_cfg()
        tools = self.tools_for(area)
        kwargs = {
            "model": model_cfg["model"],
            "max_tokens": model_cfg.get("max_tokens", 8000),
            "system": [
                {
                    "type": "text",
                    "text": area_system(area, self.owner, self.clock.tz_name,
                                        projects=any(t.name == "project_list" for t in tools)),
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
                    + (f"{recent}\n\n" if recent else "")
                    + f"TASK FROM JARVIS:\n{task}"
                    + (f"\n\nYOUR REPORT MUST INCLUDE: {expected_output}" if expected_output else "")
                ),
            }
        ]
        rounds, cost, text = 0, 0.0, ""
        changes: list[str] = []
        resp = None
        partial = "(I ran out of time or steps; this is partial.)"
        while True:
            # every step is held to the time left, so run() always returns
            # before Jarvis stops waiting (delegate's timeout is larger)
            remaining = deadline - time.monotonic()
            if remaining < 10:
                text = (text + "\n" if text else "") + partial
                break
            retries = 2 if remaining > 120 else 0
            try:
                resp = (
                    self.client()
                    .with_options(timeout=remaining / (retries + 1), max_retries=retries)
                    .messages.create(messages=messages, **kwargs)
                )
            except Exception as e:  # API error or timeout: report what we have
                text = (text + "\n" if text else "") + f"(stopped early: the model call failed with {type(e).__name__})"
                resp = None
                break
            cost += self._cost(model_cfg["model"], resp.usage)
            text = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text").strip()
            calls = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not calls:
                break
            if rounds >= self.max_rounds or deadline - time.monotonic() < 15:
                text = (text + "\n" if text else "") + partial
                break
            rounds += 1
            results = self._run_tools(tools, calls, deadline)
            for call, result in zip(calls, results):
                if call.name in MUTATING_TOOLS and not result.get("is_error"):
                    changes.append(_squash(result["content"], 240))
                    if run_id is not None:  # saved now: survives a restart mid-run
                        self._record(self.store.add_agent_change, run_id, changes[-1])
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": results})

        if resp is not None and resp.stop_reason == "refusal":
            text = text or f"The {area.name} agent declined this task."
        elif resp is not None and resp.stop_reason == "max_tokens":
            text = (text + " " if text else "") + "(report cut off)"
        report = text or f"The {area.name} agent finished without a report."
        report += f"\n\n{CHANGES_HEADER}" + ("".join(f"\n- {c}" for c in changes) if changes else " none.")
        self._log(area, task, report, rounds, cost, time.monotonic() - started)
        if run_id is not None:
            self._record(self.store.finish_agent_run, run_id, report, rounds, cost)
        return report

    def _run_tools(self, tools: list[Tool], calls, deadline: float) -> list[dict]:
        """Execute one round's tool calls concurrently; ALL results go back in
        a single user message, in call order. Waits share one clock: each
        tool gets at most 90s from the start of the round, and never past the
        run's deadline."""

        def one(call) -> tuple[str, bool]:
            return run_tool(tools, call.name, dict(call.input or {}))

        round_end = min(time.monotonic() + 90, deadline - 10)
        # not a `with` block: its exit would wait on a hung tool forever
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            futures = [pool.submit(one, c) for c in calls]
            outputs = []
            for f in futures:
                try:
                    outputs.append(f.result(timeout=max(1.0, round_end - time.monotonic())))
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
                "in the same turn: they run in parallel. The report's first line is written for the user; "
                f"its \"{CHANGES_HEADER}\" list is what really happened (taken from the agent's tools), so trust "
                "it over the agent's wording and never tell the user about a change that isn't on it. Each agent "
                "remembers its last few runs, so send a follow-up (his answer to its Question, a tweak to its "
                "plan) back to the same agent and say which earlier work it refers to."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "area": {"type": "string", "enum": [a.id for a in self.areas]},
                    "task": {"type": "string"},
                    "expected_output": {
                        "type": "string",
                        "description": 'optional: what the report must contain, e.g. "the 3 sessions with days '
                                       'and times" or "yes/no plus the one blocker"',
                    },
                },
                "required": ["area", "task"],
            },
            func=lambda area, task, expected_output=None: self.run(area, task, expected_output),
            # run() returns by its own deadline; this outer wait is the backstop
            timeout=self.timeout_s + 60,
            timeout_message=(
                "The area agent is taking longer than usual and is still working in the background: "
                "its changes may still appear. Don't ask it again yet; tell the user it is still on it, "
                "and check life_overview before retrying so nothing gets duplicated."
            ),
        )


def _squash(text, limit: int) -> str:
    """One line, at most `limit` characters."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
