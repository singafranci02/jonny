"""Offline tools: time and arithmetic."""

from __future__ import annotations

import ast
import operator
from datetime import datetime

from . import Tool

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"unsupported expression: {ast.dump(node)}")


def get_datetime() -> str:
    now = datetime.now().astimezone()
    return now.strftime("%A %d %B %Y, %H:%M %Z")


def calculate(expression: str) -> str:
    result = _safe_eval(ast.parse(expression, mode="eval"))
    return f"{expression} = {result:g}"


def build() -> list[Tool]:
    return [
        Tool(
            name="get_datetime",
            description="Current local date, time and weekday. Call when the answer depends on today's date or time.",
            parameters={"type": "object", "properties": {}, "required": []},
            func=get_datetime,
        ),
        Tool(
            name="calculate",
            description="Exact arithmetic (+ - * / // % **). Call for any non-trivial math instead of computing it yourself.",
            parameters={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
            func=calculate,
        ),
    ]
