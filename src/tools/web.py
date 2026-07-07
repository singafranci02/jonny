"""Web tools: search (DuckDuckGo -> Wikipedia fallback chain) and page fetch."""

from __future__ import annotations

import json

from . import Tool


def web_search(query: str, max_results: int = 5) -> str:
    # primary: DuckDuckGo
    try:
        from ddgs import DDGS

        results = list(DDGS().text(query, max_results=max_results))
        if results:
            return json.dumps(
                [
                    {"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")}
                    for r in results
                ],
                ensure_ascii=False,
            )
    except Exception:
        pass
    # fallback: Wikipedia search API
    import httpx

    resp = httpx.get(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": max_results,
        },
        headers={"User-Agent": "jarvis-assistant/0.1"},
        timeout=10,
    )
    hits = resp.json().get("query", {}).get("search", [])
    if not hits:
        return "no results found"
    return json.dumps(
        [
            {
                "title": h["title"],
                "url": f"https://en.wikipedia.org/wiki/{h['title'].replace(' ', '_')}",
                "snippet": h.get("snippet", ""),
            }
            for h in hits
        ],
        ensure_ascii=False,
    )


def fetch_page(url: str, max_chars: int = 6000) -> str:
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return f"could not fetch {url}"
    text = trafilatura.extract(downloaded, include_comments=False) or ""
    if not text.strip():
        return f"no readable text at {url}"
    return text[:max_chars]


def build(cfg: dict) -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description=(
                "Search the web. Call this when the answer depends on current "
                "events, prices, weather, news, or anything you don't reliably "
                "know. Returns JSON results with title, url, snippet."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            func=web_search,
        ),
        Tool(
            name="fetch_page",
            description=(
                "Fetch a web page and return its readable text. Call this "
                "after web_search when a snippet isn't enough and you need "
                "the actual page content."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            func=fetch_page,
        ),
    ]
