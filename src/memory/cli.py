"""Inspect and manage Jarvis's long-term memory.

    python -m src.memory.cli list
    python -m src.memory.cli search "grant deadline"
    python -m src.memory.cli add "I prefer metric units"
    python -m src.memory.cli forget <memory-id>
    python -m src.memory.cli forget --all
"""

from __future__ import annotations

import argparse

from rich.console import Console
from rich.table import Table

from ..config import load_config
from . import make_memory_store

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(prog="memory", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show all stored memories")
    p = sub.add_parser("search", help="semantic search")
    p.add_argument("query")
    p = sub.add_parser("add", help="store a fact manually")
    p.add_argument("fact")
    p = sub.add_parser("forget", help="delete one memory (or --all)")
    p.add_argument("memory_id", nargs="?")
    p.add_argument("--all", action="store_true", dest="forget_all")
    args = parser.parse_args()

    store = make_memory_store(load_config())

    if args.cmd == "list":
        rows = store.list_all()
        if not rows:
            console.print("[dim]no memories stored[/dim]")
            return
        table = Table("id", "memory")
        for r in rows:
            table.add_row(r["id"], r["memory"])
        console.print(table)
    elif args.cmd == "search":
        for m in store.search(args.query) or ["(no matches)"]:
            console.print(f"- {m}")
    elif args.cmd == "add":
        store.add_fact(args.fact)
        console.print("stored.")
    elif args.cmd == "forget":
        if args.forget_all:
            store.forget_all()
            console.print("all memories deleted.")
        elif args.memory_id:
            store.forget(args.memory_id)
            console.print("deleted.")
        else:
            console.print("[red]give a memory id or --all[/red]")


if __name__ == "__main__":
    main()
