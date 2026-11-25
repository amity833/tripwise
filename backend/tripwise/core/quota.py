from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from ..data.dates import utcnow
from ..llm.base import LlmClient, LlmError, LlmRequest, LlmResponse


class RateLimiter:
    """Sliding-window rate limiter keyed by user."""

    def __init__(self, max_hits: int = 20, window_ms: int = 60_000, clock: Callable[[], float] | None = None) -> None:
        self._max = max_hits
        self._window = window_ms
        self._clock = clock or (lambda: time.time() * 1000)
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        recent = [t for t in self._hits.get(key, []) if now - t < self._window]
        if len(recent) >= self._max:
            self._hits[key] = recent
            return False
        recent.append(now)
        self._hits[key] = recent
        return True


class QuotaLlm:
    """Global daily cap on LLM calls (UTC day)."""

    def __init__(self, inner: LlmClient, cap: int, clock: Callable[[], datetime] = utcnow) -> None:
        self._inner = inner
        self._cap = cap
        self._clock = clock
        self._day = ""
        self._used = 0

    async def generate(self, req: LlmRequest) -> LlmResponse:
        d = self._clock().astimezone(UTC).date().isoformat()
        if d != self._day:
            self._day, self._used = d, 0
        if self._used >= self._cap:
            raise LlmError("quota", "Daily LLM cap reached")
        self._used += 1
        return await self._inner.generate(req)
