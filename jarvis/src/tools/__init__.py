"""Tool registry: one place to define tools, exported to every provider."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    name: str
    description: str  # prescriptive: say WHEN to call it, not just what it does
    parameters: dict  # JSON schema, {"type": "object", ...}
    func: Callable[..., str]

    def anthropic_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def ollama_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, arguments: dict) -> tuple[str, bool]:
        """Execute; returns (output, is_error). Never raises."""
        # small local models sometimes emit junk keys (e.g. '<nil>') —
        # keep only arguments the schema actually declares
        valid = self.parameters.get("properties", {})
        arguments = {k: v for k, v in (arguments or {}).items() if k in valid}
        try:
            result = self.func(**arguments)
            return str(result)[:8000], False
        except Exception as e:
            return f"{type(e).__name__}: {e}", True


def make_tools(cfg: dict, memory, knowledge) -> list[Tool]:
    from . import local, stores, web

    return [
        *local.build(),
        *web.build(cfg),
        *stores.build(memory, knowledge),
    ]


def run_tool(tools: list[Tool], name: str, arguments: dict) -> tuple[str, bool]:
    for tool in tools:
        if tool.name == name:
            return tool.run(arguments)
    return f"unknown tool: {name}", True
