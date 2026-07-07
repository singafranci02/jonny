"""Run deep research from the terminal.

    python -m src.research.cli "best tide prediction APIs"
"""

from __future__ import annotations

import argparse
import asyncio

from rich.console import Console

from ..config import load_config
from ..llm import make_llm_client
from .pipeline import ResearchPipeline

console = Console()


async def amain(topic: str) -> None:
    cfg = load_config()
    pipeline = ResearchPipeline(cfg, make_llm_client(cfg))
    path, summary = await pipeline.run(topic, progress=lambda m: console.print(f"[dim]{m}[/dim]"))
    console.print(f"\n[bold]saved:[/bold] {path}")
    console.print(f"\n{summary}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="research", description=__doc__)
    parser.add_argument("topic")
    args = parser.parse_args()
    asyncio.run(amain(args.topic))


if __name__ == "__main__":
    main()
