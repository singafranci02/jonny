"""Mac jobs: what he texts Jarvis, done by Claude Code on the Mac mini.

    Telegram ─► Jarvis ── mac_task ──► job (life.db) ──► claude -p, in the project's folder
       ▲                                                   │ detached: a restart doesn't stop it
       └── one card (live steps, ■ Stop) + the summary ◄───┘ data/mac_jobs/<id>.jsonl

Jarvis writes the brief; Claude Code (the CLI, signed in with his Claude
account, never Jarvis's API key) does the work unattended: auto permission
mode with nobody to answer prompts, plus a deny list (push, deploy,
restart, sudo) on top. A job proposed by Jarvis waits for a ▶ Start tap
unless his message asked for it outright; /mac starts one directly.

The server (LifeService) calls tick() every few seconds: it follows each
running job's output, finishes jobs whose process ended and starts queued
ones (one at a time per folder). A reply to a job's card or summary
continues the same Claude Code session, and every job is an ordinary
Claude Code session, so `claude --resume` in its folder opens it on the
Mac too.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..tools import Tool
from .clock import Clock, from_iso, to_iso, utc_now

GENERAL = "mac"  # not a project: the Mac itself, working from the home folder
FOLDER = "folder"  # his Jarvis folder in iCloud
ACTIVE = ("proposed", "queued", "running")
ENDED = ("done", "failed", "stopped", "cancelled")
MAX_MINUTES = 60  # a job still running after this is stopped
MAX_RUNNING = 2  # jobs at once (always one per folder)
# Claude Code can answer, then wait on a task it put in the background and
# answer again, all in one run: a job ends when its process does. One that
# answered and then went quiet this long is over, whatever it still waits on.
RESULT_GRACE_S = 600
PERMISSION_MODE = "auto"  # Claude Code's own judge approves safe steps; anything that would ask is refused
# never unattended, whatever the brief says (config life.mac.deny replaces it)
DENY = (
    "Bash(git push:*)", "Bash(git subtree push:*)", "Bash(gh pr merge:*)", "Bash(gh release:*)",
    "Bash(make push:*)", "Bash(make install-web:*)", "Bash(make install-agent:*)",
    "Bash(make install-tunnel:*)", "Bash(make install-watchdog:*)", "Bash(make uninstall-web:*)",
    "Bash(launchctl:*)", "Bash(sudo:*)", "Bash(shutdown:*)", "Bash(reboot:*)", "Bash(pkill:*)",
    "Bash(killall:*)", "Bash(osascript:*)", "Bash(vercel:*)", "Bash(npm publish:*)",
    "Bash(security:*)", "Bash(defaults write:*)",
)
# the job's environment: none of Jarvis's keys (a stray ANTHROPIC_API_KEY
# would also bill the API instead of his Claude plan)
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CHAT_ID|^ANTHROPIC_|^CLAUDECODE$|^CLAUDE_CODE_", re.I)
_HOME = str(Path.home())
_PATH = ":".join((
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", f"{_HOME}/.local/bin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
))

_SCHEMA = """
create table if not exists mac_jobs (
  id integer primary key autoincrement,
  target text not null,
  cwd text not null,
  task text not null,
  status text not null,
  session_id text not null,
  resume integer not null default 0,
  parent_id integer,
  pid integer,
  card_id integer,
  result_id integer,
  reported integer not null default 0,
  steps integer not null default 0,
  last_step text,
  files text not null default '[]',
  summary text,
  error text,
  cost real,
  created_at text not null,
  started_at text,
  finished_at text
);
create index if not exists mac_jobs_status on mac_jobs (status, id);
"""


@dataclass
class Target:
    name: str
    cwd: Path
    about: str
    extra: tuple[Path, ...] = ()


@dataclass
class _Follow:
    """What a running job's output has said so far (rebuilt from the file
    after a restart)."""
    offset: int = 0
    steps: int = 0
    last: str = ""
    files: list[str] = field(default_factory=list)
    text: str = ""
    result: dict | None = None  # its latest answer: a run can give more than one
    limit: dict | None = None
    seen_at: float = field(default_factory=time.monotonic)  # when it last wrote anything


class MacJobs:
    def __init__(self, store, clock: Clock, root: Path, sources=None, cfg: dict | None = None,
                 folder: Path | None = None, owner: str = "the user"):
        cfg = cfg or {}
        mcfg = (cfg.get("life", {}) or {}).get("mac", {}) or {}
        self.enabled = bool(mcfg.get("enabled", True))
        self.store, self.clock = store, clock
        self.root = Path(root)
        self.dir = Path(mcfg.get("jobs_dir") or self.root / "data" / "mac_jobs")
        self.claude_path = mcfg.get("claude") or None
        self.model = mcfg.get("model") or None
        self.permission_mode = mcfg.get("permission_mode") or PERMISSION_MODE
        self.max_minutes = float(mcfg.get("max_minutes", MAX_MINUTES))
        self.max_running = int(mcfg.get("max_running", MAX_RUNNING))
        self.deny = list(mcfg.get("deny") or DENY)
        self.sources = list(sources or [])
        self.folder = Path(folder) if folder else None
        self.owner = owner
        self._procs: dict[int, subprocess.Popen] = {}
        self._follow: dict[int, _Follow] = {}
        self._lock = threading.Lock()
        with store._db() as c:
            c.executescript(_SCHEMA)

    # ---------------- where a job can run ----------------

    def targets(self) -> dict[str, Target]:
        out = {GENERAL: Target(GENERAL, Path.home(), "the Mac itself, from the home folder: anything not in one project")}
        if self.folder:
            out[FOLDER] = Target(FOLDER, self.folder, "his Jarvis folder in iCloud (Inbox, the area folders)")
        for s in self.sources:
            if s.path:
                out[s.name] = Target(s.name, Path(s.path), s.about)
        # Jarvis's own code is where this runs; a "jarvis" project entry may
        # point at another copy (the website repo): that comes along as an extra folder
        j = out.get("jarvis")
        extra = (j.cwd,) if j and j.cwd.resolve() != self.root.resolve() else ()
        out["jarvis"] = Target("jarvis", self.root, j.about if j else "Jarvis, this assistant", extra)
        return out

    def claude_bin(self) -> str | None:
        """The Claude Code CLI: config life.mac.claude, then the PATH, then the
        copy the VS Code extension ships (newest version installed)."""
        if self.claude_path:
            p = Path(os.path.expanduser(str(self.claude_path)))
            return str(p) if p.is_file() else None
        found = shutil.which("claude", path=_PATH)
        if found:
            return found
        local = Path.home() / ".claude" / "local" / "claude"
        if local.is_file():
            return str(local)
        best = None
        for p in Path.home().glob(".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"):
            v = tuple(int(n) for n in re.findall(r"\d+", p.parts[-4].split("claude-code-", 1)[-1])[:3])
            if best is None or v > best[0]:
                best = (v, p)
        return str(best[1]) if best else None

    # ---------------- records ----------------

    def get(self, job_id: int) -> dict | None:
        with self.store._db() as c:
            row = c.execute("select * from mac_jobs where id = ?", (int(job_id),)).fetchone()
        return _job(row)

    def by_message(self, message_id: int) -> dict | None:
        """The job whose card or summary this Telegram message is."""
        with self.store._db() as c:
            row = c.execute(
                "select * from mac_jobs where card_id = ? or result_id = ? order by id desc limit 1",
                (message_id, message_id),
            ).fetchone()
        return _job(row)

    def recent(self, limit: int = 8) -> list[dict]:
        with self.store._db() as c:
            rows = c.execute("select * from mac_jobs order by id desc limit ?", (limit,)).fetchall()
        return [_job(r) for r in rows]

    def to_show(self) -> list[dict]:
        """Jobs the service still has to show: active ones (their card) and
        ended ones not yet reported (their summary)."""
        with self.store._db() as c:
            rows = c.execute(
                "select * from mac_jobs where status in ('proposed', 'queued', 'running') "
                "or (reported = 0 and status in ('done', 'failed')) order by id"
            ).fetchall()
        return [_job(r) for r in rows]

    def busy(self) -> bool:
        with self.store._db() as c:
            return c.execute("select 1 from mac_jobs where status in ('queued', 'running') limit 1").fetchone() is not None

    def update(self, job_id: int, **fields) -> dict | None:
        if fields:
            if "files" in fields:
                fields["files"] = json.dumps(fields["files"])
            cols = ", ".join(f"{k} = ?" for k in fields)
            with self.store._db() as c:
                c.execute(f"update mac_jobs set {cols} where id = ?", (*fields.values(), int(job_id)))
        return self.get(job_id)

    # ---------------- what Jarvis and the buttons do ----------------

    def create(self, target: str | None, task: str, start: bool = False) -> dict:
        if not self.enabled:
            raise RuntimeError("Mac jobs are turned off (config life.mac.enabled)")
        task = (task or "").strip()
        if not task:
            raise ValueError("the task is empty")
        targets = self.targets()
        name = (target or GENERAL).strip().lower()
        if name not in targets:
            raise ValueError(f"no project {target!r} on the Mac; one of: {', '.join(targets)}")
        t = targets[name]
        return self._insert(t.name, str(t.cwd), task, str(uuid.uuid4()), False, None, start)

    def follow_up(self, parent: dict, text: str, start: bool = True) -> dict:
        """The next instruction in a job's session. A job that hasn't started
        yet just gets the instruction added to its brief."""
        text = (text or "").strip()
        if not text:
            raise ValueError("the follow-up is empty")
        if parent["status"] in ("proposed", "queued"):
            return self.update(parent["id"], task=f"{parent['task']}\n\nAlso: {text}")
        resume = bool(parent.get("started_at"))  # a session exists only once it ran
        task = text if resume else f"{parent['task']}\n\nAlso: {text}"
        return self._insert(parent["target"], parent["cwd"], task, parent["session_id"] if resume else str(uuid.uuid4()),
                            resume, parent["id"], start)

    def _insert(self, target, cwd, task, session, resume, parent_id, start) -> dict:
        with self.store._db() as c:
            cur = c.execute(
                "insert into mac_jobs (target, cwd, task, status, session_id, resume, parent_id, created_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?)",
                (target, cwd, task, "queued" if start else "proposed", session, int(resume), parent_id,
                 to_iso(utc_now())),
            )
            job_id = int(cur.lastrowid)
        return self.get(job_id)

    def approve(self, job_id: int) -> dict | None:
        with self.store._db() as c:
            c.execute("update mac_jobs set status = 'queued' where id = ? and status = 'proposed'", (int(job_id),))
        return self.get(job_id)

    def cancel(self, job_id: int) -> dict | None:
        with self.store._db() as c:
            c.execute(
                "update mac_jobs set status = 'cancelled', reported = 1, finished_at = ? "
                "where id = ? and status in ('proposed', 'queued')",
                (to_iso(utc_now()), int(job_id)),
            )
        return self.get(job_id)

    def stop(self, job_id: int) -> dict | None:
        """Stop a running job (its whole process group), or cancel one not started."""
        job = self.get(job_id)
        if job is None or job["status"] != "running":
            return self.cancel(job_id) if job else None
        with self._lock:
            job = self.get(job_id)
            if job["status"] != "running":  # it ended while the tap was on its way
                return job
            self._kill(job)
            f = self._read(job)  # what it did before the stop still shows
            self.update(job["id"], steps=f.steps, last_step=f.last or None, files=f.files, summary=f.text or None)
            self._end(job, "stopped", reported=1)
        return self.get(job_id)

    # ---------------- the loop (the server calls this every few seconds) ----------------

    def tick(self) -> None:
        """Follow running jobs, end the ones whose process is gone, start queued ones."""
        with self._lock:
            with self.store._db() as c:
                running = [_job(r) for r in c.execute("select * from mac_jobs where status = 'running'")]
            for job in running:
                self.tick_one(job)
            self._start_queued()

    def tick_one(self, job: dict) -> None:
        alive = self._alive(job)  # before reading: once it's gone, the file is complete
        f = self._read(job)
        patch: dict = {}
        if (f.steps, f.last, f.files) != (job["steps"], job["last_step"] or "", job["files"]):
            patch.update(steps=f.steps, last_step=f.last or None, files=f.files)
        quiet = time.monotonic() - f.seen_at > RESULT_GRACE_S
        if f.result is not None and (not alive or quiet):
            if alive:
                self._kill(job)  # it answered long ago and is only waiting on something in the background
            ok = f.result.get("subtype") == "success" and not f.result.get("is_error")
            summary = str(f.result.get("result") or "").strip() or f.text
            patch.update(summary=summary or None, cost=f.result.get("total_cost_usd"))
            self.update(job["id"], **patch)
            self._end(self.get(job["id"]), "done" if ok else "failed", error=None if ok else self._why(job, f))
            return
        if not alive:
            self.update(job["id"], **patch, summary=f.text or None)
            self._end(self.get(job["id"]), "failed", error=self._why(job, f))
            return
        started = from_iso(job["started_at"]) if job["started_at"] else utc_now()
        if (utc_now() - started).total_seconds() > self.max_minutes * 60:
            self._kill(job)
            self.update(job["id"], **patch, summary=f.text or None)
            self._end(self.get(job["id"]), "failed", error=f"still going after {self.max_minutes:g} min, so I stopped it")
            return
        if patch:
            self.update(job["id"], **patch)

    def _start_queued(self) -> None:
        with self.store._db() as c:
            queued = [_job(r) for r in c.execute("select * from mac_jobs where status = 'queued' order by id")]
            busy = {r["cwd"] for r in c.execute("select cwd from mac_jobs where status = 'running'")}
        for job in queued:
            if len(busy) >= self.max_running:
                return
            if job["cwd"] in busy:
                continue  # one at a time per folder: it starts when that one ends
            if self._start(job):
                busy.add(job["cwd"])

    def _start(self, job: dict) -> bool:
        claude = self.claude_bin()
        cwd = Path(job["cwd"])
        if not claude:
            self._end(job, "failed", error="Claude Code isn't installed on the Mac (no claude command found)")
            return False
        if not cwd.is_dir():
            self._end(job, "failed", error=f"{_tilde(cwd)} isn't there")
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        base = self.dir / str(job["id"])
        base.with_suffix(".prompt").write_text(job["task"], encoding="utf-8")
        cmd = [
            claude, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", self.permission_mode, "--permission-prompts", "none",
            "--settings", json.dumps({"permissions": {"deny": self.deny}}),
            "--append-system-prompt", self._rules(job),
        ]
        if job["resume"]:
            cmd += ["--resume", job["session_id"]]
        else:
            cmd += ["--session-id", job["session_id"], "--name", f"Jarvis #{job['id']}: {_short(job['task'], 50)}"]
        if self.model:
            cmd += ["--model", self.model]
        target = self.targets().get(job["target"])
        for d in (target.extra if target else ()):
            if Path(d).is_dir():
                cmd += ["--add-dir", str(d)]
        # the brief goes in on stdin: a variadic option can't swallow it
        with open(base.with_suffix(".prompt"), "rb") as stdin, open(base.with_suffix(".jsonl"), "wb") as out, \
                open(base.with_suffix(".err"), "wb") as err:
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, stdin=stdin, stdout=out, stderr=err, env=self._env(),
                                        start_new_session=True, close_fds=True)
            except OSError as e:
                self._end(job, "failed", error=f"Claude Code didn't start: {e}")
                return False
        self._procs[job["id"]] = proc
        self._follow.pop(job["id"], None)
        self.update(job["id"], status="running", pid=proc.pid, started_at=to_iso(utc_now()))
        return True

    def _end(self, job: dict, status: str, error: str | None = None, reported: int | None = None) -> None:
        fields = {"status": status, "finished_at": to_iso(utc_now())}
        if error:
            fields["error"] = error
        if reported is not None:
            fields["reported"] = reported
        self.update(job["id"], **fields)
        proc = self._procs.pop(job["id"], None)
        if proc is not None:
            try:
                proc.wait(timeout=2)  # reap it: a zombie would still look alive
            except subprocess.TimeoutExpired:
                pass
        self._follow.pop(job["id"], None)

    def _alive(self, job: dict) -> bool:
        proc = self._procs.get(job["id"])
        if proc is not None:
            return proc.poll() is None
        pid = job.get("pid")
        if not pid:
            return False
        try:  # started before a restart: not our child any more (launchd reaps it)
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def _kill(self, job: dict) -> None:
        pid = job.get("pid")
        if not pid:
            return
        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(int(pid), sig)  # its own process group: Claude Code and whatever it started
            except (ProcessLookupError, PermissionError):
                return
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if not self._alive(job):
                    return
                time.sleep(0.1)

    def _read(self, job: dict) -> _Follow:
        """New lines of the job's output since the last look."""
        f = self._follow.setdefault(job["id"], _Follow())
        path = self.dir / f"{job['id']}.jsonl"
        try:
            with open(path, "rb") as fh:
                fh.seek(f.offset)
                data = fh.read()
        except OSError:
            return f
        end = data.rfind(b"\n")
        if end < 0:
            return f
        f.offset += end + 1
        f.seen_at = time.monotonic()
        cwd = Path(job["cwd"])
        for line in data[: end + 1].splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            _apply(f, e, cwd)
        return f

    def _why(self, job: dict, f: _Follow) -> str:
        err = ""
        try:
            err = (self.dir / f"{job['id']}.err").read_text(errors="replace")[-2000:]
        except OSError:
            pass
        said = " ".join(filter(None, [str((f.result or {}).get("result") or ""), err])).lower()
        if "not logged in" in said or "/login" in said or "authenticat" in said:
            return "Claude Code isn't signed in on the Mac: open it there and /login"
        limit = f.limit or {}
        if limit.get("status") == "rejected" or re.search(r"usage limit|rate limit|limit reached", said):
            resets = limit.get("resetsAt")
            when = ""
            if resets:
                when = f"; it resets {self.clock.when(datetime.fromtimestamp(int(resets), tz=timezone.utc))}"
            return f"hit the Claude usage limit{when}"
        if f.result is not None:
            sub = str(f.result.get("subtype") or "")
            if sub == "error_max_turns":
                return "ran out of turns before finishing"
            return _short(str(f.result.get("result") or sub or "it reported an error"), 200)
        last = [ln for ln in err.strip().splitlines() if ln.strip()]
        return "Claude Code stopped unexpectedly" + (f": {_short(last[-1], 160)}" if last else "")

    def _rules(self, job: dict) -> str:
        o = self.owner
        return (
            f"You are running unattended on {o}'s Mac mini. {o} asked for this by text (Telegram), through "
            "Jarvis, his assistant. He is not at the Mac: he can't see this session, answer questions or "
            "approve anything until you have finished.\n"
            "- Do the whole task. Where it could be read two ways, take the sensible reading and say which.\n"
            "- Work in this folder (and any extra folders you were given) unless the task names another place.\n"
            "- Never push, deploy, publish, restart services, change system settings, send messages or emails, "
            "spend money, or delete anything you didn't create. Leave changes local and say they are ready.\n"
            "- Don't commit unless the task says to.\n"
            "- If it needs something only he can do or decide, stop there and say exactly what.\n"
            "- Your last message is texted to his phone: plain text, no markdown, at most 6 short lines: what "
            "you did, what changed (files), and anything left for him."
        )

    @staticmethod
    def _env() -> dict:
        env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
        env["PATH"] = _PATH + (":" + env["PATH"] if env.get("PATH") else "")
        env.pop("VIRTUAL_ENV", None)  # the job's own commands pick their own Python
        return env

    def status(self) -> dict:
        with self.store._db() as c:
            counts = {r["status"]: r["n"] for r in c.execute(
                "select status, count(*) as n from mac_jobs where status in ('proposed', 'queued', 'running') "
                "group by status")}
        return {"enabled": self.enabled, "claude": self.claude_bin(), **counts}


# ---------------- reading Claude Code's stream ----------------


def _apply(f: _Follow, e: dict, cwd: Path) -> None:
    kind = e.get("type")
    if kind == "assistant" and not e.get("parent_tool_use_id"):
        for block in (e.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use":
                f.steps += 1
                f.last = _step(block.get("name") or "", block.get("input") or {})
                path = (block.get("input") or {}).get("file_path") or (block.get("input") or {}).get("notebook_path")
                if block.get("name") in ("Edit", "MultiEdit", "Write", "NotebookEdit") and path:
                    rel = _rel(path, cwd)
                    if rel not in f.files:
                        f.files.append(rel)
            elif block.get("type") == "text" and (block.get("text") or "").strip():
                f.text = block["text"].strip()
    elif kind == "result":
        f.result = e
    elif kind == "rate_limit_event":
        f.limit = e.get("rate_limit_info") or None


def _step(name: str, args: dict) -> str:
    file = Path(str(args.get("file_path") or args.get("notebook_path") or "")).name
    if name == "Bash":
        return _short(args.get("description") or f"$ {args.get('command', '')}", 70)
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"editing {file}"
    if name == "Read":
        return f"reading {file}"
    if name in ("Grep", "Glob"):
        return "searching the files"
    if name in ("WebSearch", "WebFetch"):
        return "looking online"
    if name in ("Task", "Agent"):
        return _short(f"a helper: {args.get('description') or ''}", 70)
    if name == "TodoWrite":
        return "planning the steps"
    if name == "ToolSearch":
        return "getting its tools ready"
    if name in ("Monitor", "TaskOutput", "BashOutput"):
        return "waiting on a background task"
    if name == "Skill":
        return _short(f"using the {args.get('skill') or ''} skill", 70)
    return name


def _rel(path: str, cwd: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(cwd.resolve()))
    except (ValueError, OSError):
        return _tilde(Path(path))


def _job(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["files"] = json.loads(d.get("files") or "[]")
    return d


def _short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _tilde(p: Path) -> str:
    s = str(p)
    return "~" + s[len(_HOME):] if s.startswith(_HOME) else s


def _dur(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def _elapsed(job: dict) -> float:
    if not job.get("started_at"):
        return 0.0
    end = from_iso(job["finished_at"]) if job.get("finished_at") else utc_now()
    return (end - from_iso(job["started_at"])).total_seconds()


# ---------------- Telegram: the card and the summary (no I/O here) ----------------


def card(job: dict) -> tuple[str, dict | None]:
    """The one message that shows a job, edited as it goes."""
    head = f"🖥 #{job['id']} {job['target']}"
    brief = _short(job["task"], 240)
    s, jid = job["status"], job["id"]
    if s == "proposed":
        return f"{head}\n{brief}", _kb(("▶ Start", f"mj:{jid}:go"), ("✖ Cancel", f"mj:{jid}:x"))
    if s == "queued":
        return f"{head} · queued\n{brief}", _kb(("✖ Cancel", f"mj:{jid}:x"))
    if s == "running":
        step = f"\n{job['steps']} steps · {job['last_step']}" if job["steps"] else "\nstarting…"
        return f"{head} · working {_dur(_elapsed(job))}\n{brief}{step}", _kb(("■ Stop", f"mj:{jid}:stop"))
    if s == "done":
        return f"{head} · ✅ done in {_dur(_elapsed(job))} · {job['steps']} steps\n{brief}", None
    if s == "failed":
        return f"{head} · ⚠ didn't finish\n{brief}", None
    if s == "stopped":
        return f"{head} · ■ stopped after {_dur(_elapsed(job))}\n{brief}", None
    return f"{head} · ✖ cancelled\n{brief}", None


def report(job: dict) -> str:
    """The summary, sent as its own message when a job ends."""
    head = f"#{job['id']} {job['target']}"
    summary = (job.get("summary") or "").strip()
    if len(summary) > 1500:
        summary = summary[:1499].rstrip() + "…"
    if job["status"] == "done":
        body = summary or "Done (it didn't say more)."
        return f"✅ {head}\n{body}\n\n↩ reply to keep going"
    why = job.get("error") or "it stopped"
    tail = f"\n{summary}" if summary else ""
    return f"⚠ {head}: {why}{tail}\n\n↩ reply to try again or keep going"


def _kb(*buttons: tuple[str, str]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}


def parse_command(text: str, names) -> tuple[str | None, str]:
    """/mac [project:] task → (project or None, task)."""
    rest = re.sub(r"^/mac(@\w+)?", "", text.strip(), flags=re.I).strip()
    if not rest:
        return None, ""
    head, sep, tail = rest.partition(":")
    if sep and head.strip().lower() in names:
        return head.strip().lower(), tail.strip()
    first, _, after = rest.partition(" ")
    if first.lower() in names and after.strip():
        return first.lower(), after.strip()
    return None, rest


# ---------------- Jarvis's tools ----------------


def build_mac_tools(mac: MacJobs) -> list[Tool]:
    targets = mac.targets()
    names = list(targets)

    def mac_task(task: str, project: str = GENERAL, run_now: bool = False, follow_up_of: int | None = None) -> str:
        if follow_up_of:
            parent = mac.get(int(follow_up_of))
            if parent is None:
                raise ValueError(f"no Mac job #{follow_up_of}")
            job = mac.follow_up(parent, task, start=True)
        else:
            job = mac.create(project, task, start=bool(run_now))
        state = {"queued": "starting now", "proposed": "waiting for his ▶ Start tap"}.get(job["status"], job["status"])
        return (f"Mac job #{job['id']} in {job['target']}: {state}. Its card (live steps, ■ Stop) and the summary "
                "reach him on Telegram by themselves. Reply in a few words; don't repeat the task.")

    def mac_jobs(job_id: int | None = None) -> str:
        if job_id:
            j = mac.get(int(job_id))
            if j is None:
                return f"No Mac job #{job_id}."
            lines = [
                f"#{j['id']} {j['target']} ({_tilde(Path(j['cwd']))}): {j['status']}, {j['steps']} steps"
                + (f", {_dur(_elapsed(j))}" if j["started_at"] else ""),
                f"Task: {j['task']}",
            ]
            if j["parent_id"]:
                lines.append(f"Continues #{j['parent_id']}")
            if j["status"] == "running" and j["last_step"]:
                lines.append(f"Now: {j['last_step']}")
            if j["files"]:
                lines.append("Changed: " + ", ".join(j["files"][:20]))
            if j["error"]:
                lines.append(f"Problem: {j['error']}")
            if j["summary"]:
                lines.append(f"Its summary: {j['summary']}")
            if j["started_at"]:
                lines.append(f"On the Mac: cd {_tilde(Path(j['cwd']))} && claude --resume {j['session_id']}")
            return "\n".join(lines)
        jobs = mac.recent(8)
        if not jobs:
            return "No Mac jobs yet."
        return "\n".join(
            f"#{j['id']} {j['target']} · {j['status']} · {mac.clock.when(j['created_at'])}: {_short(j['task'], 80)}"
            + (f" → {_short(j['summary'] or j['error'] or '', 120)}" if j["status"] in ENDED else "")
            for j in jobs
        )

    about = "; ".join(f"{t.name} = {_short(t.about, 70)}" for t in targets.values())
    return [
        Tool(
            name="mac_task",
            description=(
                "Hand work to Claude Code on his Mac mini, which does it unattended and reports back on Telegram "
                "by itself: changing or fixing code, building or running something, scripts, organising or editing "
                "files, anything that needs a computer and more than a quick look. For reading files to answer a "
                "question, use find / project_read instead. task: a complete brief, because Claude Code can't see "
                "this chat: what to do, where, what done looks like, anything he said that matters. run_now: true "
                "only when his own message asks for it to be done now (\"do it on the mac\", \"go ahead\"); "
                "otherwise he gets a ▶ Start button. follow_up_of: a job's id, to continue that job's session "
                "with this as the next instruction (it keeps everything it learned). It can't push, deploy or "
                f"restart anything: those stay with him. Projects: {about}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "the full brief for Claude Code"},
                    "project": {"type": "string", "enum": names, "description": "where it works"},
                    "run_now": {"type": "boolean", "description": "start without waiting for his tap"},
                    "follow_up_of": {"type": "integer", "description": "continue this Mac job's session"},
                },
                "required": ["task"],
            },
            func=mac_task,
        ),
        Tool(
            name="mac_jobs",
            description=(
                "His Mac jobs (Claude Code) and how they went: status, steps, what changed, the summary. Call when "
                "he asks how a Mac job is going or what it did; job_id for one job in detail."
            ),
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
            },
            func=mac_jobs,
        ),
    ]
