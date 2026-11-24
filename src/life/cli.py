"""Manage the life organiser from a terminal on the Mac.

    make life                          # status (same as ARGS=status)
    make life ARGS=folders             # create the iCloud folder structure
    make life ARGS=today               # today's reminders and due items
    make life ARGS=test-telegram       # send a test message
    make life ARGS="delegate fitness 'summarise my training this month'"
    make life ARGS=backup              # copy the database into iCloud/_backups now
    make life ARGS=python-path         # the binary to give Full Disk Access
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from pathlib import Path

from rich.console import Console

from ..config import load_config
from .areas import ensure_folders, folder_status, full_disk_access_target, inbox_name, workspace_root

console = Console()

OLD_WORKSPACE = Path("~/JarvisWorkspace").expanduser()


def _copy_old_workspace(cfg: dict) -> str | None:
    """One-time: copy files from the pre-iCloud workspace into Inbox/ (a
    copy, so nothing is lost if anything goes wrong)."""
    root = workspace_root(cfg)
    if not OLD_WORKSPACE.is_dir() or OLD_WORKSPACE.resolve() == root.resolve():
        return None
    dest = root / inbox_name(cfg) / "from-JarvisWorkspace"
    if dest.exists():
        return None
    files = [p for p in OLD_WORKSPACE.rglob("*") if p.is_file() and "_trash" not in p.parts]
    if not files:
        return None
    shutil.copytree(OLD_WORKSPACE, dest, ignore=shutil.ignore_patterns("_trash"))
    return f"copied {len(files)} file(s) from {OLD_WORKSPACE} into {dest}"


def main() -> None:
    parser = argparse.ArgumentParser(prog="life", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", nargs="?", default="status",
                        choices=["status", "folders", "today", "test-telegram", "delegate", "backup", "python-path"])
    parser.add_argument("rest", nargs="*")
    args = parser.parse_args()
    cfg = load_config()
    if not cfg.get("life"):
        console.print("[red]config.yaml has no life: section[/red]")
        return

    if args.cmd == "python-path":
        # what needs Full Disk Access for the background service to reach
        # iCloud Drive (for framework pythons that's Python.app, not bin/python)
        console.print(full_disk_access_target())
        return

    if args.cmd == "folders":
        done = ensure_folders(cfg)
        console.print(f"Jarvis folder: {done['root']}")
        console.print("created: " + (", ".join(done["created"]) or "nothing (all there)"))
        moved = _copy_old_workspace(cfg)
        if moved:
            console.print(moved)
        return

    if args.cmd == "status":
        st = folder_status(cfg)
        ok = st["exists"] and st["writable"]
        console.print(f"[{'green' if ok else 'red'}]folder[/] {st['root']}  exists={st['exists']} writable={st['writable']}")
        if st.get("error"):
            console.print(f"  [red]{st['error']}[/red]")
        if not ok:
            console.print(
                "  The background service needs Full Disk Access to reach iCloud Drive: System Settings > "
                f"Privacy & Security > Full Disk Access > + > {full_disk_access_target()}  (Cmd-Shift-G pastes a path)"
            )
        for area, present in st["areas"].items():
            console.print(f"  {'✓' if present else '✗'} {area}")
        for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY"):
            console.print(f"[{'green' if os.environ.get(var) else 'red'}]{var}[/] {'set' if os.environ.get(var) else 'missing'}")
        tiers = cfg["llm"]["tiers"]
        for t in (cfg["life"].get("coordinator_tier", "coordinator"), cfg["life"].get("area_tier", "area")):
            console.print(f"tier {t}: {tiers.get(t, {}).get('model', '[red]missing[/red]')}")
        return

    # the rest need the store (and maybe the agents)
    from ..tools import make_tools
    from . import make_context

    if args.cmd == "delegate":
        if len(args.rest) < 2:
            console.print("usage: make life ARGS=\"delegate <area> '<task>'\"")
            return
        from ..knowledge import make_knowledge_index
        from ..memory import make_memory_store

        base = make_tools(cfg, make_memory_store(cfg), make_knowledge_index(cfg))
        ctx = make_context(cfg, base)
        console.print(ctx.agents.run(args.rest[0], " ".join(args.rest[1:])))
        return

    ctx = make_context(cfg, base_tools=[])
    if args.cmd == "today":
        labels = {a.id: a.label for a in ctx.areas}
        console.print(ctx.store.today_text(lambda a: labels.get(a, a)))
    elif args.cmd == "backup":
        from .areas import workspace_root

        dest = workspace_root(cfg) / "_backups" / f"life-{ctx.clock.today().isoformat()}.db"
        console.print(f"backed up to {ctx.store.backup_to(dest)}")
    elif args.cmd == "test-telegram":
        from .telegram import TelegramClient

        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            console.print("[red]set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first[/red]")
            return

        async def _send():
            tg = TelegramClient(token)
            try:
                me = await tg.get_me()
                await tg.send(int(chat), "👋 Jarvis here. Telegram works from the Mac.")
                return me
            finally:
                await tg.close()

        me = asyncio.run(_send())
        console.print(f"sent via @{me.get('username')}")


if __name__ == "__main__":
    main()
