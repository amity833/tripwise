from __future__ import annotations

import copy
from collections.abc import Callable

from .base import LlmError, LlmRequest, LlmResponse

Step = LlmResponse | Exception | Callable[[LlmRequest], LlmResponse | Exception]


class ScriptedLlm:
    """Scripted fake for tests: returns queued steps in order and records every request."""

    def __init__(self, steps: list[Step] | None = None, fallback: Step | None = None) -> None:
        self.requests: list[LlmRequest] = []
        self._steps = list(steps or [])
        self._fallback = fallback

    def push(self, *steps: Step) -> None:
        self._steps.extend(steps)

    async def generate(self, req: LlmRequest) -> LlmResponse:
        self.requests.append(copy.deepcopy(req))
        step = self._steps.pop(0) if self._steps else self._fallback
        if step is None:
            raise LlmError("server", "ScriptedLlm exhausted")
        out = step(req) if callable(step) else step
        if isinstance(out, Exception):
            raise out
        return out
