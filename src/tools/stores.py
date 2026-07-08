"""Tools over Jarvis's own memory and knowledge stores."""

from __future__ import annotations

from . import Tool


def build(memory, knowledge) -> list[Tool]:
    def search_memory(query: str) -> str:
        hits = memory.search(query)
        return "\n".join(f"- {m}" for m in hits) or "no matching memories"

    def remember(fact: str) -> str:
        memory.add_fact(fact)
        return f"stored: {fact}"

    def search_knowledge(query: str) -> str:
        hits = knowledge.search(query)
        return (
            "\n\n".join(f"[source: {src}]\n{chunk}" for src, chunk in hits)
            or "nothing relevant in the knowledge folder"
        )

    return [
        Tool(
            name="search_memory",
            description="Search stored facts about Francesco. Call when the question involves something he told you before that isn't in MEMORIES.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            func=search_memory,
        ),
        Tool(
            name="remember",
            description="Store a durable fact when Francesco asks you to remember something.",
            parameters={
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
            func=remember,
        ),
        Tool(
            name="search_knowledge",
            description="Search Francesco's notes and documents. Call when the question is about his projects or files and KNOWLEDGE doesn't answer it. Cite the source file.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            func=search_knowledge,
        ),
    ]
