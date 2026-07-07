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
            description=(
                "Search long-term memories about Francesco (projects, "
                "preferences, people, deadlines). Call this when the question "
                "involves something he may have told you before that isn't "
                "already in the MEMORIES context."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            func=search_memory,
        ),
        Tool(
            name="remember",
            description=(
                "Store a durable fact about Francesco. Call this when he "
                "explicitly asks you to remember something."
            ),
            parameters={
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
            func=remember,
        ),
        Tool(
            name="search_knowledge",
            description=(
                "Search Francesco's own notes and documents (the knowledge "
                "folder). Call this when the question is about his projects, "
                "research, or files and the KNOWLEDGE context doesn't already "
                "answer it. Cite the source file in your answer."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            func=search_knowledge,
        ),
    ]
