"""Where things are: one index over everything Jarvis and the area agents read.

Agents used to hunt by listing folders one level at a time: a question
about a contract once spent all its tool rounds opening one folder, then
the folder inside it, then the next, three times over, and never got to the
answer. The index fixes that:

    find      one call searches the text inside every file (documents,
              spreadsheets, PDFs, slides, notes, code) in the Jarvis folder
              and his project folders and repos, plus the board, reminders,
              logs, agent reports and the chat. It answers with the file,
              which part to open, and the matching lines.
    map       an outline of a place: its folders, what's in them, dates and
              titles. Each area agent gets its own map with every task
              (WHERE THINGS ARE), so it knows the territory before it starts.
    map_note  one line an agent leaves on a file or folder once it knows
              what it holds ("the signed supplier contract"). Notes show in
              the map and in search, and are flagged when the file changes.

Places: "folder" (the Jarvis folder in iCloud Drive; an area agent sees only
its own subfolder) and every project in data/project_sources.yaml (local
folders are read in full; GitHub-only repos give their file list and their
docs). Built without model calls, incrementally (a file is re-read only when
its size or date changes), into data/index.db: rebuildable, never backed up,
never in git. Secret files are never read and key-like text is masked.
"""

from __future__ import annotations

import base64
import difflib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from ..tools import Tool
from ..tools.documents import DOC_EXT, document_text, is_text, split_parts
from . import sources as src
from .sources import MAX_CHARS, SKIP_DIRS, Source, _tilde, is_secret_name, redact

log = logging.getLogger(__name__)

FOLDER = "folder"  # the place name of the Jarvis folder in iCloud Drive
FOLDER_PART = 6000  # ws_read_file's part size (tools/workspace.py _MAX_READ)
HIDDEN_DIRS = {"_trash", "_backups"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock",
              "bun.lockb", "Gemfile.lock", "composer.lock"}
TEXT_EXT = {
    ".md", ".markdown", ".txt", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html", ".css", ".scss", ".sql", ".sh", ".zsh",
    ".rs", ".go", ".java", ".kt", ".swift", ".rb", ".php", ".c", ".h", ".cpp", ".sol", ".move", ".prisma",
    ".graphql", ".ipynb", ".tex", ".xml",
}
KINDS = {".pdf": "pdf", ".docx": "doc", ".doc": "doc", ".pages": "doc", ".xlsx": "sheet", ".xlsm": "sheet",
         ".xls": "sheet", ".numbers": "sheet", ".csv": "sheet", ".pptx": "slides", ".ppt": "slides",
         ".key": "slides", ".md": "note", ".markdown": "note", ".txt": "note", ".rst": "note"}
KIND_LABELS = {"pdf": "PDF", "doc": "Word", "sheet": "sheets", "slides": "slides", "note": "notes",
               "code": "code", "image": "images", "file": "other"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".svg", ".tiff", ".bmp"}
MAX_DOC_BYTES = 25 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_JSON_BYTES = 200 * 1024
MAX_FILES = 4000  # per place
CHUNK = 900  # characters per searchable piece
MAX_CHUNKS = 600  # per file
GITHUB_EVERY = 6 * 3600  # how often a GitHub-only repo is re-checked
GITHUB_DOCS = 12  # docs per GitHub-only repo whose text is indexed
STALE = 10 * 60  # a background refresh after this long
PLACE_TIMEOUT = 180  # how long one refresh waits for its places
CHAT_SKIP_S = 300  # the chat's last minutes are the question itself, not an answer

STOP = set("""a an and are as at be been but by can could did do does for from had has have he her his how i if in
into is it its me my no not of on or our she so than that the their them then there these they this to up us
was we were what when where which who why will with would you your about any some get got tell show find look
please whats where's""".split())
_SUFFIXES = ("ings", "ing", "ies", "es", "s", "ed")
_LOC = re.compile(r"^(?:\[(page|slide) (\d+)\]|== sheet: (.+)|(#{1,4}) (.+))$")

SCHEMA_VERSION = 1  # bump to rebuild: the index holds nothing that can't be read again
# pieces: the searchable text (part 0 = a file's or folder's card: its path
# words, title and note). chunks: the full-text index over it, kept in step by
# triggers, so a file's rows are found and dropped by (place, path) directly.
_SCHEMA = """
create table if not exists files (
  place text not null, path text not null, kind text not null, size integer, mtime real,
  title text, parts integer not null default 1, primary key (place, path));
create table if not exists notes (
  place text not null, path text not null, note text not null, author text, file_mtime real,
  updated_at text not null, primary key (place, path));
create table if not exists places (place text primary key, refreshed_at real, state text, error text);
create table if not exists pieces (
  id integer primary key, place text not null, path text not null, part integer not null, loc text, body text);
create index if not exists pieces_file on pieces (place, path);
create virtual table if not exists chunks using fts5(
  body, content = 'pieces', content_rowid = 'id', tokenize = 'porter unicode61 remove_diacritics 2');
create trigger if not exists pieces_ai after insert on pieces begin
  insert into chunks (rowid, body) values (new.id, new.body);
end;
create trigger if not exists pieces_ad after delete on pieces begin
  insert into chunks (chunks, rowid, body) values ('delete', old.id, old.body);
end;
"""


@dataclass
class Place:
    name: str
    area: str | None  # None: the Jarvis folder (each area sees its own subfolder)
    about: str
    root: Path | None = None  # a folder on this Mac
    github: str | None = None  # a GitHub repo with no local folder
    part_size: int = MAX_CHARS

    def where(self) -> str:
        if self.name == FOLDER:
            return "the Jarvis folder in iCloud Drive"
        return _tilde(self.root) if self.root else f"GitHub {self.github}"


@dataclass
class Scope:
    """What one caller may see: Jarvis sees everything; an area agent its own
    subfolder of the Jarvis folder, its own projects and its area's data."""
    places: list[str]
    folder: str | None = None  # the area's subfolder ("Fitness"); None = the whole Jarvis folder
    area: str | None = None
    chat: bool = False
    owner: str = "the user"

    def prefix(self) -> str:
        return f"{self.folder}/" if self.folder else ""


@dataclass
class Hit:
    place: str
    path: str
    score: float = 0.0
    card: bool = False
    pieces: list = field(default_factory=list)  # (part, loc, snippet)
    meta: dict | None = None
    note: dict | None = None


class Index:
    def __init__(self, path: Path, folder_root: Path, sources: list[Source] | None = None, store=None):
        self.path = Path(path)
        self.store = store  # LifeStore, for the board/logs/chat side of find
        self.places: dict[str, Place] = {FOLDER: Place(FOLDER, None, "the Jarvis folder", folder_root,
                                                       part_size=FOLDER_PART)}
        for s in sources or []:
            if s.name == FOLDER:
                log.warning("index: a project can't be called %r; skipped", FOLDER)
                continue
            self.places[s.name] = Place(s.name, s.area, s.about, s.path, None if s.path else s.github)
        self._lock = threading.Lock()  # one refresh at a time
        self._thread: threading.Thread | None = None
        self._stuck: dict[str, threading.Thread] = {}  # places whose last read hasn't come back yet
        self.building = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.execute("pragma journal_mode=wal")
            if c.execute("pragma user_version").fetchone()[0] != SCHEMA_VERSION:
                # everything but the notes (agents wrote those; the rest is read again)
                for (kind, name) in c.execute("select type, name from sqlite_master where type in ('table', "
                                              "'trigger') and name not like 'sqlite_%' and name not like "
                                              "'chunks_%' and name != 'notes'").fetchall():
                    c.execute(f"drop {kind} if exists {name}")
                c.executescript(_SCHEMA)
                c.execute(f"pragma user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- scopes

    def scope_all(self, owner: str = "the user") -> Scope:
        return Scope(list(self.places), chat=True, owner=owner)

    def scope_for(self, area_id: str, folder: str, owner: str = "the user") -> Scope:
        mine = [p.name for p in self.places.values() if p.area == area_id]
        return Scope([FOLDER, *mine], folder=folder, area=area_id, owner=owner)

    # ---------------------------------------------------------------- building

    def refresh_soon(self, min_age: float = STALE) -> bool:
        """Start a background refresh if the last one is older than min_age
        and none is running. Never blocks."""
        if self.building or (self._thread and self._thread.is_alive()):
            return False
        last = self._last_refresh()
        if last and time.time() - last < min_age:
            return False
        self._thread = threading.Thread(target=self._refresh_safe, name="index-refresh", daemon=True)
        self._thread.start()
        return True

    def _refresh_safe(self) -> None:
        try:
            self.refresh()
        except Exception:
            log.exception("index refresh failed")

    def _last_refresh(self) -> float | None:
        """When the last full refresh finished (the "*" row), or None."""
        with self._db() as c:
            r = c.execute("select refreshed_at from places where place = '*'").fetchone()
        return r["refreshed_at"] if r else None

    def refresh(self, only: list[str] | None = None, force: bool = False, wait: float | None = None) -> dict:
        """Bring the index up to date: new and changed files are read, gone
        ones dropped. Every place refreshes in its own thread, so one that
        hangs can't hold up the rest: macOS blocks a read of a protected
        folder (Desktop, Documents) until someone answers its permission
        prompt. A place that isn't done by the deadline is reported as
        timed out and left to finish on its own; it's skipped until it does.
        Returns {place: files re-read, "error", "timed out" or "still
        reading"} (or {"busy": True})."""
        if not self._lock.acquire(blocking=False):
            return {"busy": True}
        self.building = True
        stats: dict = {}
        try:
            started: dict[str, tuple[threading.Thread, dict]] = {}
            for p in self.places.values():
                if only is not None and p.name not in only:
                    continue
                if not (p.root or p.github):
                    continue
                stuck = self._stuck.get(p.name)
                if stuck is not None and stuck.is_alive():
                    stats[p.name] = "still reading"
                    continue
                box: dict = {}
                t = threading.Thread(target=self._refresh_place, args=(p, force, box), name=f"index-{p.name}",
                                     daemon=True)
                t.start()
                started[p.name] = (t, box)
            deadline = time.monotonic() + (PLACE_TIMEOUT if wait is None else wait)
            for name, (t, box) in started.items():
                t.join(max(0.0, deadline - time.monotonic()))
                if t.is_alive():
                    self._stuck[name] = t
                    self._set_error(name, "timed out reading it: macOS may be waiting for someone to allow access "
                                          "to this folder (a prompt on the Mac's screen, or System Settings > "
                                          "Privacy & Security > Full Disk Access for the server's Python)")
                    stats[name] = "timed out"
                else:
                    self._stuck.pop(name, None)
                    stats[name] = box.get("result", "error")
            self._drop_orphan_notes()
            if only is None:
                with self._db() as c:
                    c.execute("insert into places (place, refreshed_at) values ('*', ?) on conflict(place) do "
                              "update set refreshed_at = excluded.refreshed_at", (time.time(),))
        finally:
            self.building = False
            self._lock.release()
        return stats

    def _refresh_place(self, p: Place, force: bool, box: dict) -> None:
        try:
            box["result"] = self._refresh_local(p, force) if p.root else self._refresh_github(p, force)
        except Exception as e:
            log.warning("index: %s: %s", p.name, e)
            self._set_error(p.name, f"{type(e).__name__}: {e}")
            box["result"] = "error"

    def _set_error(self, place: str, error: str) -> None:
        with self._db() as c:
            c.execute("insert into places (place, error) values (?, ?) on conflict(place) do update "
                      "set error = excluded.error", (place, error[:300]))

    def _walk(self, root: Path):
        n, stack = 0, [root]
        while stack and n < MAX_FILES:
            d = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                if d == root:  # the place itself is unreadable: an error, not an empty place
                    raise
                continue
            for e in entries:
                name = e.name
                if name.startswith(".") or name in SKIP_DIRS or name in HIDDEN_DIRS or is_secret_name(name):
                    continue
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir():
                        stack.append(Path(e.path))
                    elif e.is_file() and name not in SKIP_FILES and not name.endswith((".min.js", ".map")):
                        n += 1
                        yield Path(e.path), e.stat()
                except OSError:
                    continue

    def _refresh_local(self, p: Place, force: bool) -> int:
        root = p.root.resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"{p.where()} is missing")
        with self._db() as c:
            known = {r["path"]: (r["size"], r["mtime"]) for r in
                     c.execute("select path, size, mtime from files where place = ?", (p.name,))}
            notes = self._notes(c, p.name)
            seen, changed = set(), 0
            for f, st in self._walk(root):
                rel = f.relative_to(root).as_posix()
                seen.add(rel)
                if not force and known.get(rel) == (st.st_size, st.st_mtime):
                    continue
                text = self._read(f, st.st_size)
                # immediate: another process refreshing at the same time waits,
                # and forgetting first means a file is never in twice
                c.execute("begin immediate")
                self._forget(c, p.name, rel)
                self._store(c, p, rel, f.suffix.lower(), st.st_size, st.st_mtime, text, notes.get(rel))
                c.execute("commit")
                changed += 1
            gone = set(known) - seen
            if gone:
                c.execute("begin immediate")
                for rel in gone:
                    self._forget(c, p.name, rel)
                c.execute("commit")
            c.execute("insert into places (place, refreshed_at, error) values (?, ?, null) on conflict(place) "
                      "do update set refreshed_at = excluded.refreshed_at, error = null", (p.name, time.time()))
        return changed

    @staticmethod
    def _read(f: Path, size: int) -> str | None:
        ext = f.suffix.lower()
        if ext in DOC_EXT:
            if size > MAX_DOC_BYTES:
                return None
        elif ext in TEXT_EXT or ext == "":
            if size > (MAX_JSON_BYTES if ext == ".json" else MAX_TEXT_BYTES):
                return None
        else:
            return None
        try:
            text = document_text(f.name, f.read_bytes())
        except OSError:
            return None
        return redact(text) if is_text(text) else None

    def _refresh_github(self, p: Place, force: bool) -> int:
        with self._db() as c:
            row = c.execute("select * from places where place = ?", (p.name,)).fetchone()
        state = json.loads(row["state"]) if row and row["state"] else {}
        if not force and row and row["refreshed_at"] and time.time() - row["refreshed_at"] < GITHUB_EVERY \
                and not row["error"]:
            return 0
        tree = src._gh(f"repos/{p.github}/git/trees/HEAD?recursive=1")
        if not force and state.get("tree") == tree.get("sha"):
            with self._db() as c:
                c.execute("update places set refreshed_at = ?, error = null where place = ?", (time.time(), p.name))
            return 0
        try:
            commit = src._gh(f"repos/{p.github}/commits?per_page=1")[0]["commit"]
            state["last_commit"] = commit["author"]["date"][:10]
        except Exception:
            pass
        blobs = []
        for t in tree.get("tree", []):
            parts = t["path"].split("/")
            if t.get("type") != "blob" or any(x.startswith(".") or x in SKIP_DIRS or is_secret_name(x)
                                               for x in parts) or parts[-1] in SKIP_FILES:
                continue
            blobs.append(t)
        blobs = blobs[:MAX_FILES]
        # the docs get read: READMEs first, then other notes near the top
        docs = sorted((b for b in blobs if Path(b["path"]).suffix.lower() in (".md", ".markdown", ".txt", ".rst")
                       and b["path"].count("/") <= 2 and b.get("size", 0) <= 200_000),
                      key=lambda b: (not Path(b["path"]).name.lower().startswith("readme"), b["path"].count("/"),
                                     b["path"]))[:GITHUB_DOCS]
        texts = {}
        for b in docs:
            try:
                data = src._gh(f"repos/{p.github}/contents/{quote(b['path'])}")
                texts[b["path"]] = redact(document_text(b["path"], base64.b64decode(data.get("content") or "")))
            except Exception as e:
                log.info("index: %s/%s unread: %s", p.github, b["path"], e)
        with self._db() as c:
            notes = self._notes(c, p.name)
            c.execute("begin immediate")
            c.execute("delete from files where place = ?", (p.name,))
            c.execute("delete from pieces where place = ?", (p.name,))
            for b in blobs:
                self._store(c, p, b["path"], Path(b["path"]).suffix.lower(), b.get("size"), None,
                            texts.get(b["path"]), notes.get(b["path"]))
            for folder, n in notes.items():  # folder notes keep their card
                if not any(b["path"] == folder for b in blobs):
                    self._card(c, p.name, folder, "", n)
            state["tree"] = tree.get("sha")
            c.execute("insert into places (place, refreshed_at, state, error) values (?, ?, ?, null) "
                      "on conflict(place) do update set refreshed_at = excluded.refreshed_at, "
                      "state = excluded.state, error = null", (p.name, time.time(), json.dumps(state)))
            c.execute("commit")
        return len(blobs)

    def _store(self, c, p: Place, rel: str, ext: str, size, mtime, text: str | None, note: dict | None) -> None:
        kind = KINDS.get(ext) or ("image" if ext in IMAGE_EXT else "code" if ext in TEXT_EXT else "file")
        parts = split_parts(text, p.part_size) if text else []
        title = _title(text, ext, rel) if text else ""
        c.execute("insert or replace into files (place, path, kind, size, mtime, title, parts) values "
                  "(?, ?, ?, ?, ?, ?, ?)", (p.name, rel, kind, size, mtime, title, max(1, len(parts))))
        self._card(c, p.name, rel, title, note)
        rows = []
        for i, part in enumerate(parts, 1):
            rows += [(p.name, rel, i, loc, body) for loc, body in _pieces(part, ext)]
            if len(rows) >= MAX_CHUNKS:
                break
        c.executemany("insert into pieces (place, path, part, loc, body) values (?, ?, ?, ?, ?)", rows[:MAX_CHUNKS])

    @staticmethod
    def _card(c, place: str, rel: str, title: str, note: dict | None) -> None:
        """The searchable name of a file or folder: its path words, title and note."""
        words = re.sub(r"[/_\-.]+", " ", rel)
        body = " ".join(x for x in (words, title, (note or {}).get("note", "")) if x)
        c.execute("insert into pieces (place, path, part, loc, body) values (?, ?, 0, '', ?)", (place, rel, body))

    @staticmethod
    def _forget(c, place: str, rel: str) -> None:
        c.execute("delete from files where place = ? and path = ?", (place, rel))
        c.execute("delete from pieces where place = ? and path = ?", (place, rel))

    @staticmethod
    def _notes(c, place: str) -> dict[str, dict]:
        return {r["path"]: dict(r) for r in c.execute("select * from notes where place = ?", (place,))}

    def _drop_orphan_notes(self) -> None:
        """A note whose file or folder is gone goes with it."""
        with self._db() as c:
            for n in c.execute("select place, path from notes").fetchall():
                exists = c.execute("select 1 from files where place = ? and (path = ? or substr(path, 1, ?) = ?) "
                                   "limit 1", (n["place"], n["path"], *_under(n["path"]))).fetchone()
                if not exists and n["place"] in self.places:
                    c.execute("delete from notes where place = ? and path = ?", (n["place"], n["path"]))
                    c.execute("delete from pieces where place = ? and path = ? and part = 0", (n["place"], n["path"]))

    # ---------------------------------------------------------------- notes

    def set_note(self, scope: Scope, place: str, path: str, note: str, author: str = "") -> str:
        place, full = self._locate(scope, place, path)
        note = redact(" ".join(str(note or "").split()))[:200]
        with self._db() as c:
            f = c.execute("select * from files where place = ? and path = ?", (place, full)).fetchone()
            folder = None if f else c.execute("select 1 from files where place = ? and substr(path, 1, ?) = ? "
                                              "limit 1", (place, *_under(full))).fetchone()
            if not f and not folder:
                close = self.suggest(place, full, scope=scope)
                raise ValueError(f"nothing at {self._show(scope, place, full)} in the index"
                                 + (f". Did you mean: {'; '.join(close)}" if close else ""))
            c.execute("begin immediate")
            c.execute("delete from pieces where place = ? and path = ? and part = 0", (place, full))
            if note:
                row = {"note": note}
                c.execute("insert or replace into notes (place, path, note, author, file_mtime, updated_at) values "
                          "(?, ?, ?, ?, ?, ?)", (place, full, note, author, f["mtime"] if f else None,
                                                 datetime.now().isoformat(timespec="seconds")))
            else:
                row = None
                c.execute("delete from notes where place = ? and path = ?", (place, full))
            if f or row:
                self._card(c, place, full, f["title"] if f else "", row)
            c.execute("commit")
        where = self._show(scope, place, full)
        return f"noted on {where}: {note}" if note else f"note removed from {where}"

    # ---------------------------------------------------------------- search

    def find(self, scope: Scope, query: str, place: str | None = None, limit: int = 6) -> str:
        if place:
            place, _ = self._locate(scope, place, "")
        self._freshen()
        terms = _terms(query)
        if not terms:
            return "Give find a few words to look for: a name, a number, the thing itself."
        hits = self._search(scope, terms, [place] if place else scope.places, limit)
        life = [] if place else self._life(scope, terms)
        lines = []
        if hits:
            lines.append("Files, best match first:")
            for i, h in enumerate(hits, 1):
                lines += self._hit_lines(scope, i, h)
        if life:
            lines.append(("" if not hits else "\n") + "Board, reminders, logs and past work:")
            lines += [f"- {x}" for x in life]
        if not lines:
            where = f"in {place}" if place else "anywhere I can see"
            lines.append(f"Nothing {where} matches \"{query}\". Try other words (a name, a number, a "
                         "different term), or map a place to see what is in it.")
        if self.building or not self._last_refresh():
            lines.append("(The index is still being built: results may be incomplete.)")
        return "\n".join(lines)

    def _freshen(self) -> None:
        """Before a search: the Jarvis folder is re-checked in place when
        it's over a minute old (it's small and agents write to it); the rest
        refreshes in the background when stale."""
        with self._db() as c:
            r = c.execute("select refreshed_at from places where place = ?", (FOLDER,)).fetchone()
        if not r or not r["refreshed_at"] or time.time() - r["refreshed_at"] > 60:
            self.refresh(only=[FOLDER])
        self.refresh_soon()

    def _search(self, scope: Scope, terms: list[str], places: list[str], limit: int) -> list[Hit]:
        found: dict[tuple, Hit] = {}
        for op in ("AND", "OR") if len(terms) > 1 else ("AND",):
            if len(found) >= limit:
                break
            batch = self._match(scope, f" {op} ".join(f'"{t}"' for t in terms), places)
            for key in sorted(batch, key=lambda k: batch[k].score):
                if key not in found:
                    found[key] = batch[key]
                    batch[key].score += 1000 if op == "OR" else 0  # all words beat some words
        hits = sorted(found.values(), key=lambda h: h.score)[:limit]
        if hits:
            with self._db() as c:
                for h in hits:
                    m = c.execute("select * from files where place = ? and path = ?", (h.place, h.path)).fetchone()
                    n = c.execute("select * from notes where place = ? and path = ?", (h.place, h.path)).fetchone()
                    h.meta, h.note = (dict(m) if m else None), (dict(n) if n else None)
        return hits

    def _match(self, scope: Scope, expr: str, places: list[str]) -> dict[tuple, Hit]:
        marks = ",".join("?" * len(places))
        sql = (f"select p.place, p.path, p.part, p.loc, snippet(chunks, 0, '«', '»', '…', 18) as snip, "
               f"bm25(chunks) as r from chunks join pieces p on p.id = chunks.rowid "
               f"where chunks match ? and p.place in ({marks})")
        args: list = [expr, *places]
        if scope.folder:
            sql += " and (p.place != ? or substr(p.path, 1, ?) = ?)"
            args += [FOLDER, *_under(scope.folder)]
        sql += " order by r limit 400"
        with self._db() as c:
            try:
                rows = c.execute(sql, args).fetchall()
            except sqlite3.OperationalError as e:
                log.info("index: bad query %r: %s", expr, e)
                return {}
        out: dict[tuple, Hit] = {}
        for r in rows:
            h = out.setdefault((r["place"], r["path"]), Hit(r["place"], r["path"]))
            if r["part"] == 0:
                h.card, h.score = True, h.score + r["r"]
            elif len(h.pieces) < 2:
                h.pieces.append((r["part"], r["loc"], " ".join(r["snip"].split())))
                h.score += r["r"] * (1 if len(h.pieces) == 1 else 0.5)
        return out

    def _hit_lines(self, scope: Scope, i: int, h: Hit) -> list[str]:
        m = h.meta
        where = self._show(scope, h.place, h.path)
        if m is None:  # a folder, found by its note
            return [f"{i}. {where}/ (folder)", *([f"   note: {h.note['note']}"] if h.note else []),
                    f"   see inside: map place=\"{h.place}\" path=\"{self._rel(scope, h.place, h.path)}\""]
        facts = [KIND_LABELS.get(m["kind"], m["kind"]).rstrip("s") if m["kind"] not in ("code", "file") else m["kind"]]
        if m["mtime"]:
            facts.append(_date(m["mtime"]))
        if m["parts"] > 1:
            facts.append(f"{m['parts']} parts")
        lines = [f"{i}. {where} ({', '.join(facts)})" + (f" \"{m['title']}\"" if m["title"] else "")]
        for part, loc, snip in h.pieces:
            label = ", ".join(x for x in (f"part {part}" if m["parts"] > 1 else "", loc) if x)
            lines.append(f"   {label + ': ' if label else ''}{snip}")
        if not h.pieces and h.card:
            lines.append("   (its name or note matches)")
        if h.note:
            stale = h.note["file_mtime"] and m["mtime"] and m["mtime"] > h.note["file_mtime"] + 1
            lines.append(f"   note: {h.note['note']}" + (" (the file has changed since)" if stale else ""))
        lines.append("   open: " + self._open(scope, h.place, h.path, h.pieces[0][0] if h.pieces else 1))
        return lines

    def _life(self, scope: Scope, terms: list[str]) -> list[str]:
        if self.store is None:
            return []
        stems = list(dict.fromkeys(_stem(w) for t in terms for w in t.split()))
        need = 1 if len(stems) <= 1 else max(2, (len(stems) + 1) // 2)
        try:
            rows = self.store.search_text(stems, area=scope.area, chat=scope.chat)
        except Exception as e:
            log.warning("index: life search failed: %s", e)
            return []
        cutoff = datetime.now().timestamp() - CHAT_SKIP_S
        out = []
        for h in rows:
            if h["score"] < need:
                continue
            if h["kind"] == "chat" and (_ts(h["when"]) > cutoff or (h["row"]["direction"] == "in"
                                                                    and h["row"]["text"].rstrip().endswith("?"))):
                continue  # this turn's own question, or an old question: not an answer
            out.append(_life_line(h, stems, self.store.clock, scope.owner))
            if len(out) >= 6:
                break
        return out

    # ---------------------------------------------------------------- maps

    def places_text(self, scope: Scope) -> str:
        """One line per place: what it is, where, how many files of what
        kind, how fresh, and its top folders."""
        lines = []
        with self._db() as c:
            for name in scope.places:
                p = self.places[name]
                rows = self._files(c, scope, name, "")
                st = c.execute("select * from places where place = ?", (name,)).fetchone()
                if name == FOLDER:
                    head = f"your folder ({scope.folder}/)" if scope.folder else "the Jarvis folder in iCloud Drive"
                else:
                    head = f"{_squash(p.about, 110)} · {p.where()}" if p.about else p.where()
                if st is None:
                    facts = ["not indexed yet"]
                elif st["error"]:
                    facts = [f"couldn't index it: {st['error']}"]
                else:
                    facts = [f"{_files_n(len(rows))}{_kinds(rows)}"]
                    state = json.loads(st["state"]) if st["state"] else {}
                    newest = max((r["mtime"] for r in rows if r["mtime"]), default=None)
                    if newest:
                        facts.append(f"newest {_date(newest)}")
                    elif state.get("last_commit"):
                        facts.append(f"last commit {state['last_commit']}")
                    skip = len(scope.prefix()) if name == FOLDER else 0
                    top = sorted({r["path"][skip:].split("/")[0] + "/" for r in rows if "/" in r["path"][skip:]},
                                 key=_nat)
                    if top:
                        facts.append(", ".join(top[:6]) + (" …" if len(top) > 6 else ""))
                lines.append(f"- {name}: {head} · " + " · ".join(facts))
        return "\n".join(lines)

    def outline(self, scope: Scope, place: str, path: str = "", budget: int = 70) -> str:
        place, full = self._locate(scope, place, path)
        with self._db() as c:
            rows = self._files(c, scope, place, full)
            notes = self._notes(c, place)
        if not rows:
            close = self.suggest(place, full, scope=scope) if full else []
            return (f"Nothing indexed under {self._show(scope, place, full)}"
                    + (f". Did you mean: {'; '.join(close)}" if close else "") + ".")
        base = (full + "/") if full else ""
        tree = _tree(rows, base)
        lines = _detail(tree, 0, base, notes)
        depth = 3
        while len(lines) > budget and depth >= 1:
            lines = _summary(tree, 0, depth, base, notes)
            depth -= 1
        if len(lines) > budget:
            lines = lines[:budget] + [f"… ({len(lines) - budget} more lines: map a subfolder with path)"]
        if full:
            head = self._show(scope, place, full)
        elif place == FOLDER:
            head = f"your folder ({scope.folder}/)" if scope.folder else "the Jarvis folder"
        else:
            head = f"{place} ({self.places[place].where()})"
        return f"{head} · {_files_n(len(rows))}{_kinds(rows)}\n" + "\n".join(lines)

    def briefing(self, scope: Scope, task: str, also: list[str] | None = None) -> str:
        """WHERE THINGS ARE, for an area agent's task: its folder in full, a
        line per project it can read, and the full map of any project the
        task names."""
        if not self._last_refresh():
            return ""
        self._freshen()
        parts = ["WHERE THINGS ARE (from the index; find searches inside all of it):"]
        own = self.outline(scope, FOLDER, "", budget=30)
        parts.append(f"Your folder ({scope.folder}/):\n" + "\n".join("  " + x for x in own.splitlines()[1:]))
        projects = [n for n in scope.places if n != FOLDER]
        if projects:
            sub = Scope(projects, area=scope.area, owner=scope.owner)
            parts.append("Projects you can read with project_read:\n" + self.places_text(sub))
            text = " ".join([task, *(also or [])]).lower()
            named = [n for n in projects if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text)][:2]
            for n in named:
                parts.append(f"Map of {n}:\n" + self.outline(scope, n, "", budget=45))
        return "\n\n".join(parts)

    # ---------------------------------------------------------------- helpers

    def suggest(self, place: str, path: str, n: int = 3, scope: Scope | None = None) -> list[str]:
        """Paths in a place that look like `path`: a wrong guess gets the
        likely right ones."""
        target = path.strip("/").lower()
        if not target:
            return []
        with self._db() as c:
            files = [r["path"] for r in c.execute("select path from files where place = ?", (place,))]
        if scope and place == FOLDER and scope.folder:
            files = [f for f in files if f.startswith(scope.prefix())]
        cands = set(files)
        for f in files:
            parts = f.split("/")
            cands.update("/".join(parts[:i]) for i in range(1, len(parts)))
        words = set(re.findall(r"[^\W_]+", target))
        base = target.rsplit("/", 1)[-1]

        def score(cand: str) -> float:
            low = cand.lower()
            s = difflib.SequenceMatcher(None, target, low).ratio()
            s += 0.5 * difflib.SequenceMatcher(None, base, low.rsplit("/", 1)[-1]).ratio()
            if words:
                s += 2 * len(words & set(re.findall(r"[^\W_]+", low))) / len(words)
            return s

        ranked = sorted(cands, key=score, reverse=True)[:n]
        shown = [self._rel(scope, place, x) if scope else x for x in ranked if score(x) > 1.2]
        return [f'"{x}"' for x in shown]

    def _locate(self, scope: Scope, place: str, path: str) -> tuple[str, str]:
        """(place, full path in the index) for a caller's place and path."""
        name = str(place or "").strip().lower()
        if name in ("jarvis folder", "icloud", "my folder", "your folder", "@folder"):
            name = FOLDER
        if name not in scope.places:
            raise ValueError(f"no place '{place}'; you can see: {', '.join(scope.places)}")
        rel = "/".join(p for p in str(path or "").strip().strip("/").split("/") if p not in ("", "."))
        if ".." in rel.split("/"):
            raise ValueError("paths can't go up out of a place")
        if name == FOLDER and scope.folder:
            if rel == scope.folder or rel.startswith(scope.prefix()):  # already given from the top
                return name, rel
            return name, (scope.prefix() + rel).rstrip("/")
        return name, rel

    def _files(self, c, scope: Scope, place: str, under: str) -> list[dict]:
        rows = [dict(r) for r in c.execute("select * from files where place = ?", (place,))]
        if place == FOLDER and scope.folder:
            rows = [r for r in rows if r["path"].startswith(scope.prefix())]
        if under:
            rows = [r for r in rows if r["path"].startswith(under + "/")]
        return rows

    def _rel(self, scope: Scope | None, place: str, full: str) -> str:
        if scope and place == FOLDER and scope.folder and full.startswith(scope.prefix()):
            return full[len(scope.prefix()):]
        return full

    def _show(self, scope: Scope, place: str, full: str) -> str:
        rel = self._rel(scope, place, full)
        if place == FOLDER:
            return f"your folder › {rel}" if scope.folder else f"Jarvis folder › {rel}"
        return f"{place} › {rel}"

    def _open(self, scope: Scope, place: str, full: str, part: int) -> str:
        rel = self._rel(scope, place, full)
        p = f" part={part}" if part and part > 1 else ""
        if place == FOLDER:
            return f'ws_read_file name="{rel}"{p}'
        return f'project_read name="{place}" path="{rel}"{p}'

    def status(self) -> dict:
        with self._db() as c:
            files = c.execute("select count(*) as n from files").fetchone()["n"]
            rows = c.execute("select * from places").fetchall()
        last = self._last_refresh()
        return {
            "files": files,
            "places": len(self.places),
            "building": self.building,
            "refreshed": datetime.fromtimestamp(last).isoformat(timespec="minutes") if last else None,
            "errors": {r["place"]: r["error"] for r in rows if r["error"]},
        }


# ---------------------------------------------------------------- the tools


def build_index_tools(index: Index | None, scope: Scope, author: str = "jarvis") -> list[Tool]:
    if index is None:
        return []
    names = list(scope.places)
    folder_hint = (f'"{FOLDER}" is your own folder ({scope.folder}/)' if scope.folder
                   else f'"{FOLDER}" is the Jarvis folder in iCloud Drive')
    return [
        Tool(
            name="find",
            description=(
                "Find where something is, in one call: searches the text inside every file you can read (the "
                "Jarvis folder and his projects' folders and repos: documents, spreadsheets, PDFs, slides, notes, "
                "code)" + (", plus the board, reminders, logs, past agent work and the chat" if scope.chat else
                           ", plus your area's board, reminders, logs and past work") + ". Returns the best files "
                "with the matching lines and the exact call to open each (path and part). Use it FIRST to look "
                "up a fact, a figure, a date, a document or a file, before listing folders. Query with a few "
                "distinctive words (a name, a number, the thing itself); \"quotes\" for an exact phrase."
            ),
            parameters={"type": "object", "properties": {
                "query": {"type": "string"},
                "place": {"type": "string", "enum": names,
                          "description": f"optional: search one place only; {folder_hint}"},
            }, "required": ["query"]},
            func=lambda query, place=None: index.find(scope, query, place),
            timeout=30,
        ),
        Tool(
            name="map",
            description=(
                "What is inside a place, at a glance: its folders with file counts, files with dates and titles, "
                "and the notes left on them. No place: one line per place you can read. Use it to get your "
                "bearings in a project instead of listing folders one by one; path narrows it to a subfolder. "
                + folder_hint + "."
            ),
            parameters={"type": "object", "properties": {
                "place": {"type": "string", "enum": names},
                "path": {"type": "string", "description": "optional: a folder inside the place"},
            }},
            func=lambda place=None, path="": (index.outline(scope, place, path) if place
                                              else index.places_text(scope)),
            timeout=30,
        ),
        Tool(
            name="map_note",
            description=(
                "Leave a one-line note on a file or folder saying what it holds when its name doesn't (\"the "
                "signed supplier contract; payment terms in clause 4\"). Notes show in map and are searched "
                "by find, so the next lookup lands straight on it. Empty note removes it."
            ),
            parameters={"type": "object", "properties": {
                "place": {"type": "string", "enum": names},
                "path": {"type": "string"},
                "note": {"type": "string"},
            }, "required": ["place", "path", "note"]},
            func=lambda place, path, note: index.set_note(scope, place, path, note, author),
        ),
    ]


# ---------------------------------------------------------------- text helpers


def _terms(query: str) -> list[str]:
    """Search terms: "quoted phrases" whole, other words without filler."""
    phrases = [" ".join(re.findall(r"[^\W_]+", p.lower())) for p in re.findall(r'"([^"]+)"', query or "")]
    rest = re.sub(r'"[^"]*"', " ", query or "")
    words = re.findall(r"[^\W_]+", rest.lower())
    kept = [w for w in words if w not in STOP] or words
    return [t for t in dict.fromkeys([*phrases, *kept]) if t][:10]


def _stem(word: str) -> str:
    for s in _SUFFIXES:
        if word.endswith(s) and len(word) - len(s) >= 4:
            return word[: -len(s)]
    return word


def _pieces(text: str, ext: str) -> list[tuple[str, str]]:
    """A part's text as searchable pieces of ~CHUNK characters, each with
    where it is: "page 3", "sheet Ledger", "slide 4" or a heading."""
    out, buf, size, loc = [], [], 0, ""
    start = loc
    headings = ext in (".md", ".markdown")

    def flush():
        nonlocal buf, size, start
        body = "\n".join(buf).strip()
        if body:
            out.append((start, body))
        buf, size, start = [], 0, loc

    for line in text.split("\n"):
        m = _LOC.match(line.strip())
        if m and (m.group(1) or m.group(3) or (m.group(4) and headings)):
            flush()
            loc = (f"{m.group(1)} {m.group(2)}" if m.group(1) else f"sheet {m.group(3).strip()}" if m.group(3)
                   else f"§ {_squash(m.group(5), 60)}")
            start = loc
            if not m.group(4):
                continue  # a page/slide/sheet marker: the location says it already
            line = m.group(5)  # a heading: its words, without the #s
        buf.append(line)
        size += len(line) + 1
        if size >= CHUNK:
            flush()
    flush()
    return out


def _title(text: str, ext: str, rel: str) -> str:
    lines = [x.strip() for x in text.split("\n") if x.strip()]
    if not lines or ext not in KINDS and ext not in (".html",):
        return ""
    if ext in (".xlsx", ".xlsm"):
        sheets = [x[len("== sheet: "):] for x in lines if x.startswith("== sheet: ")]
        return "sheets: " + ", ".join(sheets[:5]) if sheets else ""
    if ext in (".md", ".markdown"):
        head = next((x.lstrip("#").strip() for x in lines if x.startswith("#")), lines[0])
    else:
        head = next((x for x in lines if not re.fullmatch(r"\[(page|slide) \d+\]", x)), "")
    head = _squash(head, 80)
    stem = Path(rel).stem.lower()
    return "" if head.lower() in (stem, Path(rel).name.lower()) else head


def _tree(rows: list[dict], base: str) -> dict:
    root = {"dirs": {}, "files": []}
    for r in rows:
        parts = r["path"][len(base):].split("/")
        node = root
        for d in parts[:-1]:
            node = node["dirs"].setdefault(d, {"dirs": {}, "files": []})
        node["files"].append(r)
    return root


def _count(node: dict) -> int:
    return len(node["files"]) + sum(_count(d) for d in node["dirs"].values())


def _files_n(n: int) -> str:
    return f"{n} file" if n == 1 else f"{n} files"


def _all_files(node: dict) -> list[dict]:
    return node["files"] + [f for d in node["dirs"].values() for f in _all_files(d)]


def _nat(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _note_text(notes: dict, path: str, mtime=None) -> str:
    n = notes.get(path)
    if not n:
        return ""
    stale = n.get("file_mtime") and mtime and mtime > n["file_mtime"] + 1
    return f" — note: {n['note']}" + (" (file changed since)" if stale else "")


def _file_line(f: dict, notes: dict) -> str:
    name = f["path"].rsplit("/", 1)[-1]
    bits = [name]
    if f["mtime"]:
        bits.append(_date(f["mtime"]))
    if f["title"]:
        bits.append(f"\"{_squash(f['title'], 60)}\"")
    return " · ".join(bits) + _note_text(notes, f["path"], f["mtime"])


def _detail(node: dict, depth: int, base: str, notes: dict) -> list[str]:
    pad = "  " * depth
    lines = [pad + _file_line(f, notes) for f in sorted(node["files"], key=lambda f: _nat(f["path"]))]
    for name in sorted(node["dirs"], key=_nat):
        d = node["dirs"][name]
        path = base + name
        lines.append(f"{pad}{name}/{_note_text(notes, path)}")
        lines += _detail(d, depth + 1, path + "/", notes)
    return lines


def _summary(node: dict, depth: int, limit: int, base: str, notes: dict) -> list[str]:
    """One line per folder down to `limit` levels; deeper ones are named."""
    pad = "  " * depth
    lines = []
    if node["files"]:
        newest = sorted(node["files"], key=lambda f: f["mtime"] or 0, reverse=True)
        names = ", ".join(f["path"].rsplit("/", 1)[-1] for f in newest[:5])
        lines.append(f"{pad}{'files here' if depth else 'top level'}: {names}"
                     + (f" (+{len(newest) - 5} more)" if len(newest) > 5 else ""))
    for name in sorted(node["dirs"], key=_nat):
        d = node["dirs"][name]
        path = base + name
        head = f"{pad}{name}/ ({_files_n(_count(d))}){_note_text(notes, path)}"
        if depth + 1 < limit and (d["dirs"] or len(d["files"]) > 3):
            lines.append(head)
            lines += _summary(d, depth + 1, limit, path + "/", notes)
        else:
            subs = ", ".join(f"{k}/" for k in sorted(d["dirs"], key=_nat))
            newest = sorted(_all_files(d), key=lambda f: f["mtime"] or 0, reverse=True)[:3]
            files = ", ".join(f["path"].rsplit("/", 1)[-1] for f in newest)
            lines.append(head + ": " + "; ".join(x for x in (_squash(subs, 140), files) if x))
    return lines


def _kinds(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
    return f" ({', '.join(f'{n} {KIND_LABELS.get(k, k)}' for k, n in top)})" if len(counts) > 1 else ""


def _life_line(h: dict, stems: list[str], clock, owner: str) -> str:
    r, kind = h["row"], h["kind"]
    day = _day(clock, h["when"])
    if kind == "item":
        state = r["status"] if r["status"] != "open" else (r.get("stage") or "open")
        extra = "".join([f"; next: {_squash(r['next_step'], 80)}" if r.get("next_step") else "",
                         f"; due {r['due_date']}" if r.get("due_date") else ""])
        return f"board #{r['id']} [{r['area']}] {r['kind']} ({state}): {_squash(r['title'], 100)}{extra}"
    if kind == "reminder":
        return f"reminder #{r['id']} [{r['area']}] ({r['status']}, {clock.when(r['due_at'])}): {_squash(r['text'], 120)}"
    if kind == "log":
        value = f" {r['value']:g}" if r.get("value") is not None else ""
        return f"log {day} [{r['area']}] {r['metric']}{value}: {_around(r['note'], stems, 140)}"
    if kind == "run":
        first = (r["report"] or "").strip().split("\n")[0]
        return (f"{r['area']} agent, {day}: asked \"{_squash(r['task'], 90)}\" → "
                f"{_around(first if any(s in first.lower() for s in stems) else r['report'] or '', stems, 160)}")
    who = owner if r["direction"] == "in" else "Jarvis"
    return f"chat {day}, {who}: \"{_around(r['text'], stems, 160)}\""


def _around(text: str, stems: list[str], width: int) -> str:
    flat = " ".join(str(text or "").split())
    low = flat.lower()
    at = min((low.find(s) for s in stems if s in low), default=-1)
    if at < 0 or len(flat) <= width:
        return _squash(flat, width)
    start = max(0, at - width // 3)
    piece = flat[start:start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(flat) else "")


def _squash(text, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _date(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


def _day(clock, iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return clock.local(iso).strftime("%Y-%m-%d")
    except Exception:
        return str(iso)[:10]


def _ts(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _under(path: str) -> tuple[int, str]:
    """(length, prefix) for `substr(path, 1, ?) = ?`: everything inside a
    folder (LIKE would read _ and % in names as wildcards)."""
    prefix = path.rstrip("/") + "/"
    return len(prefix), prefix
