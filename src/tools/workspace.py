"""Workspace tools: the ONE folder Jarvis may write in.

Confinement is real, not advisory: every path is realpath-resolved and must
land inside the workspace (kills `..` escapes and symlinks pointing out).
There is no delete — and because edits/overwrites can destroy content just
as surely, every destructive write first copies the old version into
_trash/ (timestamped). Every write is audit-logged.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from . import Tool

_MAX_READ = 6000
_MAX_WRITE = 100_000
# folders that hold machinery, not documents: never listed
_HIDDEN_DIRS = {"_trash", "_backups"}

# tool calls in one round run in parallel: serialise read-modify-write per file
_file_locks: dict[str, threading.Lock] = {}
_file_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _file_locks_guard:
        return _file_locks.setdefault(str(path), threading.Lock())


class WorkspaceError(Exception):
    pass


def _root_path(cfg: dict) -> Path:
    """The configured folder, without touching the filesystem (safe at
    startup even when iCloud/TCC would refuse access)."""
    ws = cfg.get("workspace", {}) or {}
    return Path(ws.get("path", "~/JarvisWorkspace")).expanduser()


def _root(cfg: dict) -> Path:
    root = _root_path(cfg)
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()
    except OSError as e:
        raise WorkspaceError(
            f"can't reach the workspace folder {root} ({e.strerror or e}). If it is in iCloud "
            "Drive, the background service needs Full Disk Access (make life ARGS=python-path)."
        )


def _trash(cfg: dict) -> Path:
    t = _root(cfg) / (cfg.get("workspace", {}) or {}).get("trash_subdir", "_trash")
    t.mkdir(exist_ok=True)
    return t


def _resolve(cfg: dict, name: str) -> Path:
    """The confinement gate: whatever the model asks for, the final real
    path must live inside the workspace."""
    if not name or not str(name).strip():
        raise WorkspaceError("empty filename")
    root = _root(cfg)
    # treat absolute paths as workspace-relative if they point inside;
    # otherwise they're an escape attempt
    p = Path(str(name).strip()).expanduser()
    candidate = p if p.is_absolute() else root / p
    # resolve() follows symlinks AND flattens '..'
    real = candidate.resolve()
    if real != root and root not in real.parents:
        raise WorkspaceError(
            f"'{name}' is outside the workspace — I can only touch files in "
            f"{root}"
        )
    if real == root:
        raise WorkspaceError("that's the workspace folder itself, not a file")
    return real


def _audit(cfg: dict, action: str, path: Path, extra: str = "") -> None:
    try:
        from ..config import ROOT

        with open(ROOT / "data" / "workspace_audit.log", "a") as f:
            f.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {action:<8} "
                f"{path}  {extra}\n"
            )
    except Exception:
        pass


def _version_to_trash(cfg: dict, path: Path) -> None:
    """Before any destructive write, keep the old content."""
    if path.exists() and path.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")  # unique even within a second
        shutil.copy2(path, _trash(cfg) / f"{path.name}.{stamp}")


def create_file(cfg: dict, name: str, content: str) -> str:
    path = _resolve(cfg, name)
    if len(content or "") > _MAX_WRITE:
        raise WorkspaceError("content too large")
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        _version_to_trash(cfg, path)
        path.write_text(content or "")
    _audit(cfg, "overwrite" if existed else "create", path, f"{len(content or '')}ch")
    return f"{'overwrote' if existed else 'created'} {path.name} ({len(content or '')} characters)"


def edit_file(cfg: dict, name: str, find: str, replace: str) -> str:
    path = _resolve(cfg, name)
    with _lock_for(path):
        if not path.exists():
            raise WorkspaceError(f"{path.name} doesn't exist — create it first")
        text = path.read_text()
        if find:
            if find not in text:
                raise WorkspaceError(
                    f"couldn't find that text in {path.name}; read it first to "
                    "see its current contents"
                )
            new = text.replace(find, replace, 1)
        else:
            # empty find = append
            new = text + ("" if text.endswith("\n") or not text else "\n") + replace
        if len(new) > _MAX_WRITE:
            raise WorkspaceError("resulting file too large")
        _version_to_trash(cfg, path)
        path.write_text(new)
    _audit(cfg, "edit", path, f"{len(text)}->{len(new)}ch")
    return f"edited {path.name}"


def read_file(cfg: dict, name: str) -> str:
    path = _resolve(cfg, name)
    if not path.exists():
        raise WorkspaceError(f"{path.name} doesn't exist")
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise WorkspaceError(f"{path.name} is a binary file; open it in Finder instead")
    text = data.decode(errors="replace")
    return text[:_MAX_READ] + ("\n...(truncated)" if len(text) > _MAX_READ else "")


def _listed(rel_parts: tuple[str, ...], trash: str) -> bool:
    """Documents only: no trash/backups folders, no dotfiles (.DS_Store, and
    iCloud's '.name.icloud' placeholders for files not downloaded yet)."""
    return not any(
        part.startswith(".") or part in _HIDDEN_DIRS or part == trash for part in rel_parts
    )


def list_files(cfg: dict) -> str:
    root = _root(cfg)
    trash = (cfg.get("workspace", {}) or {}).get("trash_subdir", "_trash")
    entries = sorted(
        p for p in root.rglob("*")
        if p.is_file() and _listed(p.relative_to(root).parts, trash)
    )
    if not entries:
        return "the workspace is empty"
    return json.dumps(
        [
            {"name": str(p.relative_to(root)), "size": p.stat().st_size}
            for p in entries[:100]
        ]
    )


def move_file(cfg: dict, name: str, new_name: str) -> str:
    src = _resolve(cfg, name)
    dst = _resolve(cfg, new_name)  # destination confined too
    first, second = sorted([src, dst], key=str)  # fixed order: no deadlock
    with _lock_for(first), (_lock_for(second) if second != first else _NoLock()):
        if not src.exists():
            raise WorkspaceError(f"{src.name} doesn't exist")
        dst.parent.mkdir(parents=True, exist_ok=True)
        _version_to_trash(cfg, dst)  # never silently clobber a file by moving onto it
        shutil.move(str(src), str(dst))
    _audit(cfg, "move", src, f"-> {dst}")
    return f"moved {src.name} to {dst.relative_to(_root(cfg))}"


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def build(cfg: dict) -> list[Tool]:
    # no filesystem access here: Jarvis must start even if the folder (iCloud)
    # isn't reachable yet; each tool call reports the problem instead
    root = _root_path(cfg)
    loc = f"your workspace folder ({root})"
    return [
        Tool(
            name="ws_create_file",
            description=(
                f"Create (or overwrite) a text file in {loc}. Call when asked "
                "to write, save, or make a note/file/doc. Pick a short "
                "sensible .md or .txt filename if none given, and say the "
                "filename in your answer. This is the ONLY place you can "
                "write files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "filename, e.g. ideas.md"},
                    "content": {"type": "string"},
                },
                "required": ["name", "content"],
            },
            func=lambda name, content: create_file(cfg, name, content),
        ),
        Tool(
            name="ws_edit_file",
            description=(
                f"Edit an existing file in {loc}: replaces `find` with "
                "`replace` (first occurrence). Empty `find` appends to the "
                "end. Read the file first if unsure of its exact contents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                },
                "required": ["name", "find", "replace"],
            },
            func=lambda name, find, replace: edit_file(cfg, name, find, replace),
        ),
        Tool(
            name="ws_read_file",
            description=f"Read a file from {loc}. Call before editing, or when asked what's in a file.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            func=lambda name: read_file(cfg, name),
        ),
        Tool(
            name="ws_list",
            description=f"List the files in {loc}. Call when asked what files/notes exist.",
            parameters={"type": "object", "properties": {}},
            func=lambda: list_files(cfg),
        ),
        Tool(
            name="ws_move",
            description=f"Rename/move a file within {loc}. Cannot move anything outside it.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "new_name": {"type": "string"},
                },
                "required": ["name", "new_name"],
            },
            func=lambda name, new_name: move_file(cfg, name, new_name),
        ),
    ]
