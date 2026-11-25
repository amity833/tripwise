from __future__ import annotations

import json
import math

from .models import LlmMessage


def _approx_tokens(m: LlmMessage) -> int:
    return math.ceil(len(json.dumps(m, separators=(",", ":"), ensure_ascii=False)) / 4)


def trim_history(messages: list[LlmMessage], budget_tokens: int) -> list[LlmMessage]:
    """
    Trim oldest turns to fit the budget. A turn starts at a user message, so a
    tool call is never separated from its result. The newest turn is always kept.
    """
    turns: list[list[LlmMessage]] = []
    for m in messages:
        if m["role"] == "user" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    total = sum(_approx_tokens(m) for t in turns for m in t)
    while len(turns) > 1 and total > budget_tokens:
        dropped = turns.pop(0)
        total -= sum(_approx_tokens(m) for m in dropped)
    return [m for t in turns for m in t]


def visible_history(messages: list[LlmMessage]) -> list[dict[str, str]]:
    """Text-only view for the UI and GET history."""
    out: list[dict[str, str]] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "text": m["text"]})
        elif m["role"] == "assistant" and m.get("text") and not m.get("toolCalls"):
            out.append({"role": "assistant", "text": m["text"]})
    return out
