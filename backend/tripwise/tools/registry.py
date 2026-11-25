from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from ..core.models import Ctx
from ..store.repos import Repo


@dataclass
class ToolContext:
    ctx: Ctx
    repo: Repo
    now: datetime
    tz: str


@dataclass
class Tool:
    name: str
    description: str
    model: type[BaseModel]  # validates the arguments the LLM supplies
    parameters: dict[str, Any]  # JSON schema shown to the LLM
    run: Callable[[Any, ToolContext], Awaitable[Any]]


def _describe(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in err['loc'])} {err['msg']}".strip() for err in e.errors())


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools = {t.name: t for t in tools or []}

    def specs(self) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in self._tools.values()]

    async def execute(self, name: str, args: Any, tc: ToolContext) -> Any:
        """Never trusts model args: validates them and turns every failure into a tool error result."""
        tool = self._tools.get(name)
        if not tool:
            return {"error": f'Unknown tool "{name}"'}
        try:
            parsed = tool.model.model_validate(args)
        except ValidationError as e:
            return {"error": f"Invalid arguments: {_describe(e)}"}
        try:
            return await tool.run(parsed, tc)
        except Exception as e:
            return {"error": str(e) or "Tool failed"}
