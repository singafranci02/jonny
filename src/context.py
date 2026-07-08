"""Prompt assembly.

Order per turn (per the architecture doc):
  system persona -> retrieved memories -> knowledge chunks -> history -> user msg

Phase 1: memories/knowledge are empty; the slots exist so Phases 3-4
plug in without touching the call sites. Volatile context is injected
into the *user turn* so the cached system-prompt prefix stays intact.
"""

from __future__ import annotations


def build_user_content(
    user_message: str,
    profile: str | None = None,
    memories: list[str] | None = None,
    knowledge: list[tuple[str, str]] | None = None,  # (source_file, chunk)
) -> str:
    parts: list[str] = []
    if profile:
        parts.append("ABOUT THE USER (their own profile):\n" + profile)
    if memories:
        parts.append("MEMORIES:\n" + "\n".join(f"- {m}" for m in memories))
    if knowledge:
        parts.append(
            "KNOWLEDGE:\n"
            + "\n\n".join(f"[source: {src}]\n{chunk}" for src, chunk in knowledge)
        )
    parts.append(user_message)
    return "\n\n".join(parts)


class Conversation:
    """Rolling user/assistant history, trimmed to max_history_turns pairs."""

    def __init__(self, max_history_turns: int = 20):
        self.max_turns = max_history_turns
        self.messages: list[dict] = []

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})
        # keep the last N user/assistant pairs
        if len(self.messages) > self.max_turns * 2:
            self.messages = self.messages[-self.max_turns * 2 :]
