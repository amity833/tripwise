from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.models import LlmMessage, ToolCall


@dataclass
class LlmRequest:
    system: str
    messages: list[LlmMessage]
    tools: list[dict[str, Any]] = field(default_factory=list)  # [{name, description, parameters}]


@dataclass
class LlmResponse:
    text: str | None = None
    tool_calls: list[ToolCall] | None = None


class LlmError(Exception):
    """kind: timeout | rate_limited | server | quota | outage | bad_request"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class LlmClient(Protocol):
    async def generate(self, req: LlmRequest) -> LlmResponse: ...
