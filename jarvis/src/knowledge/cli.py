"""Index the knowledge/ folder.

    python -m src.knowledge.cli            # incremental ingest
    python -m src.knowledge.cli --force    # re-embed everything
    python -m src.knowledge.cli --search "query"   # test retrieval
"""

from __future__ import annotations

import argparse

from rich.console import Console

from ..config import load_config
from . import make_knowledge_index

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(prog="ingest", description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-embed everything")
    parser.add_argument("--search", metavar="QUERY", help="test retrieval and exit")
    args = parser.parse_args()

    index = make_knowledge_index(load_config())
    if args.search:
        hits = index.search(args.search)
        if not hits:
            console.print("[dim]no matches[/dim]")
        for src, chunk in hits:
            console.print(f"[bold]{src}[/bold]: {chunk[:200]}...")
        return

    stats = index.ingest(force=args.force)
    console.print(
        f"indexed {stats['indexed']} file(s) ({stats['chunks']} chunks), "
        f"{stats['unchanged']} unchanged, {stats['removed']} removed"
    )


if __name__ == "__main__":
    main()
