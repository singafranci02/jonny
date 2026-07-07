"""Deep research: plan -> gather -> synthesize -> save into knowledge/.

Hybrid-cheap by design (AgenticSeek pattern, our models):
- planning + final synthesis use Claude (research tier)
- per-page note-taking uses the local model (summarize tier), so reading
  ten pages costs nothing
The report lands in knowledge/research/, where the existing watcher
auto-indexes it — future questions answer from it with citations.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from pathlib import Path

from ..config import ROOT
from ..tools.web import fetch_page, web_search

PLAN_PROMPT = """You are planning web research on this topic:

{topic}

Output ONLY a JSON array of 3 to 5 distinct, specific web search queries
covering complementary angles of the topic. No other text."""

NOTES_PROMPT = """TOPIC: {topic}
SOURCE URL: {url}

PAGE TEXT:
{text}

Distill every fact from this page that is relevant to the topic into dense
bullet notes (numbers, names, dates, tradeoffs). Output ONLY the bullets.
If the page has nothing relevant, output exactly: NOTHING RELEVANT"""

SYNTH_PROMPT = """Write a markdown research report on the topic below, using ONLY
the sourced notes provided.

TOPIC: {topic}

NOTES (each block is one numbered source):
{notes}

Requirements:
- Start with "# <title>" then a "## Summary" section of exactly 2-3 plain,
  speakable sentences (no citations there).
- Then well-organized sections covering the findings, with inline citation
  markers like [1], [2] that refer to the sources.
- End with "## Sources" listing each number with its URL.
- Only claim what the notes support; note open questions honestly."""

RESEARCH_SYSTEM = "You are a careful research analyst. Be factual and concise."


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "research"


class ResearchPipeline:
    def __init__(self, cfg: dict, llm):
        self.cfg = cfg
        self.llm = llm
        rcfg = cfg.get("research", {})
        self.results_per_query = rcfg.get("results_per_query", 3)
        self.pages_per_query = rcfg.get("pages_per_query", 2)
        self.out_dir = ROOT / cfg["knowledge"].get("folder", "knowledge") / "research"

    async def _plan(self, topic: str, progress) -> list[str]:
        progress("planning search queries...")
        resp = await self.llm.chat(
            RESEARCH_SYSTEM, [{"role": "user", "content": PLAN_PROMPT.format(topic=topic)}],
            tier="research",
        )
        match = re.search(r"\[.*\]", resp.text, re.DOTALL)
        queries = json.loads(match.group(0)) if match else []
        return [q for q in queries if isinstance(q, str)][:5] or [topic]

    async def _gather(self, topic: str, queries: list[str], progress) -> list[dict]:
        loop = asyncio.get_event_loop()
        seen: set[str] = set()
        sources: list[dict] = []
        for query in queries:
            progress(f"searching: {query}")
            try:
                hits = json.loads(
                    await loop.run_in_executor(None, web_search, query, self.results_per_query)
                )
            except Exception:
                continue
            picked = 0
            for hit in hits:
                url = hit.get("url")
                if not url or url in seen or picked >= self.pages_per_query:
                    continue
                seen.add(url)
                text = await loop.run_in_executor(None, fetch_page, url, 8000)
                if text.startswith("could not fetch") or text.startswith("no readable"):
                    continue
                picked += 1
                progress(f"reading: {url}")
                notes = await self.llm.chat(
                    RESEARCH_SYSTEM,
                    [{
                        "role": "user",
                        "content": NOTES_PROMPT.format(topic=topic, url=url, text=text),
                    }],
                    tier="summarize",
                )
                if "NOTHING RELEVANT" in notes.text or not notes.text.strip():
                    continue
                sources.append({"url": url, "title": hit.get("title", url), "notes": notes.text})
        return sources

    async def _synthesize(self, topic: str, sources: list[dict], progress) -> str:
        progress(f"synthesizing report from {len(sources)} sources...")
        notes_block = "\n\n".join(
            f"[{i + 1}] {s['title']}\nURL: {s['url']}\n{s['notes']}"
            for i, s in enumerate(sources)
        )
        resp = await self.llm.chat(
            RESEARCH_SYSTEM,
            [{
                "role": "user",
                "content": SYNTH_PROMPT.format(topic=topic, notes=notes_block),
            }],
            tier="research",
        )
        return resp.text

    async def run(self, topic: str, progress=lambda msg: None) -> tuple[Path, str]:
        """Returns (report_path, speakable_summary)."""
        queries = await self._plan(topic, progress)
        sources = await self._gather(topic, queries, progress)
        if not sources:
            raise RuntimeError("research found no readable sources")
        report = await self._synthesize(topic, sources, progress)

        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{slugify(topic)}-{date.today():%Y%m%d}.md"
        path.write_text(report)

        summary_match = re.search(r"## Summary\s*\n(.*?)(?=\n#|\Z)", report, re.DOTALL)
        summary = (
            summary_match.group(1).strip() if summary_match else report[:400]
        )
        return path, summary
