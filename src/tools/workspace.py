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
import time
from pathlib import Path

from . import Tool

_MAX_READ = 6000
_MAX_WRITE = 100_000


class WorkspaceError(Exception):
    pass


def _root(cfg: dict) -> Path:
    ws = cfg.get("workspace", {}) or {}
    root = Path(ws.get("path", "~/JarvisWorkspace")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


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
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, _trash(cfg) / f"{path.name}.{stamp}")


def create_file(cfg: dict, name: str, content: str) -> str:
    path = _resolve(cfg, name)
    if len(content or "") > _MAX_WRITE:
        raise WorkspaceError("content too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    _version_to_trash(cfg, path)
    path.write_text(content or "")
    _audit(cfg, "overwrite" if existed else "create", path, f"{len(content or '')}ch")
    return f"{'overwrote' if existed else 'created'} {path.name} ({len(content or '')} characters)"


def edit_file(cfg: dict, name: str, find: str, replace: str) -> str:
    path = _resolve(cfg, name)
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
    text = path.read_text(errors="replace")
    return text[:_MAX_READ] + ("\n...(truncated)" if len(text) > _MAX_READ else "")


def list_files(cfg: dict) -> str:
    root = _root(cfg)
    trash = (cfg.get("workspace", {}) or {}).get("trash_subdir", "_trash")
    entries = sorted(
        p for p in root.rglob("*")
        if p.is_file() and trash not in p.relative_to(root).parts
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
    if not src.exists():
        raise WorkspaceError(f"{src.name} doesn't exist")
    dst.parent.mkdir(parents=True, exist_ok=True)
    _version_to_trash(cfg, dst)  # never silently clobber a file by moving onto it
    shutil.move(str(src), str(dst))
    _audit(cfg, "move", src, f"-> {dst}")
    return f"moved {src.name} to {dst.relative_to(_root(cfg))}"


def build(cfg: dict) -> list[Tool]:
    root = _root(cfg)
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
