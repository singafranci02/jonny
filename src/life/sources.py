"""Read-only windows into the user's other projects: code repos, folders,
spreadsheets and documents, so Jarvis and the area agents answer from the
real thing instead of from memory.

The list is private, in data/project_sources.yaml (gitignored, never in the
public repo), one entry per project:

    yardbird:
      area: projects            # the area agent that may read it (Jarvis reads all)
      about: one line on what it is
      path: ~/YardBird          # a folder on this Mac, and/or
      github: owner/repo        # a GitHub repo (read through the gh CLI)

Nothing here writes anything. Agents can't edit the list: it sits outside
their folders. Secrets stay out: .env files, keys and credential files are
refused, and anything in a file's text that looks like a key is masked.
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from ..tools import Tool
from ..tools.documents import document_text, part_text

log = logging.getLogger(__name__)

MAX_CHARS = 20000  # one part of a read: longer files come in numbered parts
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".mypy_cache",
             ".pytest_cache", ".turbo", ".vercel", ".idea", ".vscode"}
_SECRET_EXT = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".kdbx", ".ppk", ".asc", ".gpg")
_SECRET_WORDS = ("secret", "credential", "id_rsa", "id_ed25519", "id_ecdsa", "private_key", "privatekey",
                 "keypair", "service-account", "service_account", ".netrc", ".npmrc", ".pypirc")
_SAFE_ENV = (".example", ".sample", ".template", ".dist")
_REDACT = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:sk|pk|rk)-(?:ant-|proj-|live_|test_)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}"),  # Telegram bot tokens
    re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWTs
]
_ASSIGNED = re.compile(
    r"""(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|token|password|passwd|private[_-]?key|access[_-]?key)[A-Z0-9_]*)"""
    r"""(\s*[:=]\s*["']?)([^\s"'`,;]{8,})"""
)


@dataclass
class Source:
    name: str
    area: str
    about: str
    path: Path | None = None
    github: str | None = None

    def where(self) -> str:
        parts = []
        if self.path:
            parts.append(f"folder {_tilde(self.path)}")
        if self.github:
            parts.append(f"GitHub {self.github}")
        return ", ".join(parts)


# ---------------------------------------------------------------- the list


def sources_file(cfg: dict) -> Path:
    from ..config import ROOT

    p = Path((cfg.get("life", {}) or {}).get("sources_file", "data/project_sources.yaml")).expanduser()
    return p if p.is_absolute() else ROOT / p


def load_sources(cfg: dict, area_ids: list[str]) -> list[Source]:
    """The project list; a missing file means no project tools. Bad entries
    are skipped with a warning, never fatal."""
    import yaml

    f = sources_file(cfg)
    if not f.exists():
        return []
    try:
        raw = yaml.safe_load(f.read_text()) or {}
    except Exception as e:
        log.warning("project sources: can't read %s: %s", f, e)
        return []
    out = []
    for name, entry in raw.items() if isinstance(raw, dict) else []:
        try:
            out.append(_source(str(name), entry or {}, area_ids))
        except ValueError as e:
            log.warning("project sources: skipped %s: %s", name, e)
    return out


def _source(name: str, e: dict, area_ids: list[str]) -> Source:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name):
        raise ValueError("name must be short, lowercase letters, digits, - or _")
    area = str(e.get("area", "projects"))
    if area not in area_ids:
        raise ValueError(f"area must be one of {', '.join(area_ids)}")
    path = None
    if e.get("path"):
        path = Path(str(e["path"])).expanduser().resolve()
        home = Path.home().resolve()
        if path in (home, Path("/")) or not path.is_dir():
            raise ValueError(f"path {path} is not a project folder")
        if path.is_relative_to(home):
            first = path.relative_to(home).parts[0]
            if first.startswith(".") or first == "Library":
                raise ValueError("hidden folders and ~/Library are off limits")
    github = str(e["github"]).strip() if e.get("github") else None
    if github and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github):
        raise ValueError("github must look like owner/repo")
    if not path and not github:
        raise ValueError("needs a path or a github repo")
    return Source(name, area, " ".join(str(e.get("about", "")).split()), path, github)


# ---------------------------------------------------------------- safety


def is_secret_name(name: str) -> bool:
    n = name.lower()
    if n.startswith(".env") and not n.endswith(_SAFE_ENV):
        return True
    if n.endswith(_SECRET_EXT) or n in ("id.json", "wallet.json", "auth.json", "token.json", "tokens.json"):
        return True
    return any(w in n for w in _SECRET_WORDS)


def redact(text: str) -> str:
    for pat in _REDACT:
        text = pat.sub("[redacted]", text)
    return _ASSIGNED.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)


def _tilde(p: Path) -> str:
    home = Path.home().resolve()
    return f"~/{p.relative_to(home)}" if p.is_relative_to(home) else str(p)


def _part(text: str, part: int = 1, size: int = MAX_CHARS) -> str:
    """One numbered part of a long text, cut at a line break."""
    return part_text(text, part, size)


# ---------------------------------------------------------------- git / gh


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 15) -> str:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:300] or f"exit {p.returncode}")
    return p.stdout


def _gh(endpoint: str) -> object:
    """GET a GitHub API endpoint through the gh CLI (its own login)."""
    exe = shutil.which("gh") or "/opt/homebrew/bin/gh"
    return json.loads(_run([exe, "api", endpoint]))


def _git(path: Path, *args: str) -> str:
    return _run(["git", "-C", str(path), *args])


def _is_git(path: Path | None) -> bool:
    return bool(path) and (path / ".git").exists()


def _walk(root: Path, limit: int = 3000):
    """Files under root (skipping build/dependency folders), at most `limit`."""
    n = 0
    stack = [root]
    while stack and n < limit:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.name in SKIP_DIRS or e.name.startswith(".") or e.is_symlink():
                continue
            if e.is_dir():
                stack.append(e)
            else:
                n += 1
                yield e


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- the tools


_cache: dict[str, tuple[float, str]] = {}


def last_activity(s: Source, ttl: float = 600) -> str:
    hit = _cache.get(s.name)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        if _is_git(s.path):
            out = "last commit " + _git(s.path, "log", "-1", "--format=%cs %s").strip()
        elif s.path:
            newest = max(_walk(s.path), key=lambda f: f.stat().st_mtime, default=None)
            out = ("files last changed " + datetime.fromtimestamp(newest.stat().st_mtime).strftime("%Y-%m-%d")
                   + f" ({newest.name})") if newest else "empty folder"
        else:
            c = _gh(f"repos/{s.github}/commits?per_page=1")[0]["commit"]
            out = f"last commit {c['author']['date'][:10]} {c['message'].splitlines()[0]}"
    except Exception as e:
        out = f"activity unknown ({type(e).__name__})"
    out = out[:160]
    _cache[s.name] = (time.monotonic(), out)
    return out


def build_source_tools(sources: list[Source], area=None, suggest=None) -> list[Tool]:
    """project_* tools over `sources`; an area agent only sees its own
    area's projects, and gets no tools at all if it has none. `suggest`
    (name, path) -> close paths, from the index: a wrong guess at a path
    comes back with the likely right ones instead of a dead end."""
    mine = [s for s in sources if area is None or s.area == area.id]
    if not mine:
        return []
    by_name = {s.name: s for s in mine}
    names = list(by_name)

    def get(name: str) -> Source:
        s = by_name.get(str(name).strip().lower())
        if s is None:
            raise ValueError(f"no project '{name}'; known: {', '.join(names)}")
        return s

    def project_list() -> str:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            acts = list(pool.map(last_activity, mine))
        return "\n".join(
            f"- {s.name} ({s.area}): {s.about or 'no description'} | {s.where()} | {a}" for s, a in zip(mine, acts)
        )

    def project_activity(name: str, days: int = 30) -> str:
        s = get(name)
        days = max(1, min(int(days or 30), 365))
        out = []
        if _is_git(s.path):
            log_ = _git(s.path, "log", f"--since={days} days ago", "-n", "40", "--format=%cs %s").strip()
            out.append(f"Commits in the last {days} days:\n" + (log_ or "none"))
        elif s.path:
            cutoff = time.time() - days * 86400
            recent = sorted((f for f in _walk(s.path) if f.stat().st_mtime > cutoff),
                            key=lambda f: f.stat().st_mtime, reverse=True)[:30]
            out.append(f"Files changed in the last {days} days:\n" + ("\n".join(
                f"{datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d} {f.relative_to(s.path)}" for f in recent
            ) or "none"))
        if s.github:
            if not _is_git(s.path):
                commits = _gh(f"repos/{s.github}/commits?since={_since(days)}&per_page=30")
                out.append(f"Commits in the last {days} days:\n" + ("\n".join(
                    f"{c['commit']['author']['date'][:10]} {c['commit']['message'].splitlines()[0]}" for c in commits
                ) or "none"))
            prs = _gh(f"repos/{s.github}/pulls?state=open&per_page=10")
            out.append("Open pull requests: " + ("; ".join(f"#{p['number']} {p['title']}" for p in prs) or "none"))
        return redact("\n\n".join(out))

    def project_read(name: str, path: str = "", part: int = 1) -> str:
        s = get(name)
        rel = str(path or "").strip().strip("/")
        parts = [p for p in Path(rel).parts if p not in ("", ".")]
        for p in parts:
            if p == ".." or is_secret_name(p) or p in SKIP_DIRS:
                raise ValueError(f"can't read {rel}: outside the project, or a secret or build folder")
        try:
            text = _read_local(s, parts) if s.path else _read_github(s, "/".join(parts))
        except (ValueError, RuntimeError) as e:
            close = suggest(s.name, rel) if (suggest and rel) else []
            if close:
                raise ValueError(f"{e}. Did you mean: " + "; ".join(close)) from None
            raise
        return _part(text, part)

    tools = [
        Tool(
            name="project_list",
            description=("The user's other projects you can read (never change): what each is, where it lives "
                         "(a folder on this Mac and/or a GitHub repo) and its latest activity. Start here when he "
                         "asks about one of his projects or where things stand."),
            parameters={"type": "object", "properties": {}},
            func=project_list,
            timeout=45,
        ),
        Tool(
            name="project_activity",
            description=("What changed recently in one project: commits (or changed files for a plain folder) and "
                         "open pull requests. Use it for progress questions and to spot stalled projects."),
            parameters={"type": "object", "properties": {
                "name": {"type": "string", "enum": names},
                "days": {"type": "integer", "description": "how far back, default 30"},
            }, "required": ["name"]},
            func=project_activity,
            timeout=30,
        ),
        Tool(
            name="project_read",
            description=("Read inside one project: path empty or a folder lists it; a file returns its text. "
                         "Spreadsheets (.xlsx), PDFs, Word files and slides come back as text, so a tracker or "
                         "plan can be read directly. Read-only; secrets (.env, keys) are never shown. To look "
                         "for a fact or a file, call find first and open the hit it gives you (path and part); "
                         "don't hunt through folders one level at a time."),
            parameters={"type": "object", "properties": {
                "name": {"type": "string", "enum": names},
                "path": {"type": "string", "description": 'inside the project, e.g. "README.md" or "docs"; '
                                                          'empty = top level'},
                "part": {"type": "integer", "description": "for long files: which part to read, default 1"},
            }, "required": ["name"]},
            func=project_read,
            timeout=30,
            max_output=MAX_CHARS + 200,
        ),
    ]
    return tools


def _read_local(s: Source, parts: list[str]) -> str:
    root = s.path
    target = root.joinpath(*parts).resolve() if parts else root
    if target != root and not target.is_relative_to(root):
        raise ValueError("that path leads outside the project")
    if not target.exists():
        raise ValueError(f"no such file or folder: {'/'.join(parts)}")
    if target.is_dir():
        lines = []
        for e in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if e.name in SKIP_DIRS or is_secret_name(e.name) or e.name in (".DS_Store",):
                continue
            if e.is_dir():
                lines.append(f"{e.name}/")
            else:
                st = e.stat()
                lines.append(f"{e.name} ({max(1, st.st_size // 1024)} KB, "
                             f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d})")
            if len(lines) >= 200:
                lines.append("… (more)")
                break
        return f"{_tilde(target)}:\n" + ("\n".join(lines) or "(empty)")
    size = target.stat().st_size
    if size > 25 * 1024 * 1024:
        return f"(file is {size // (1024 * 1024)} MB: too big to read here)"
    return redact(document_text(target.name, target.read_bytes()))


def _read_github(s: Source, path: str) -> str:
    data = _gh(f"repos/{s.github}/contents/{quote(path)}")
    if isinstance(data, list):
        lines = [f"{e['name']}/" if e["type"] == "dir" else f"{e['name']} ({max(1, e.get('size', 0) // 1024)} KB)"
                 for e in sorted(data, key=lambda e: (e["type"] != "dir", e["name"].lower()))
                 if e["name"] not in SKIP_DIRS and not is_secret_name(e["name"])]
        return f"{s.github}/{path}:\n" + ("\n".join(lines[:200]) or "(empty)")
    if not data.get("content"):
        return "(file too big for the GitHub contents API)"
    raw = base64.b64decode(data["content"])
    return redact(document_text(data.get("name", path), raw))
