from __future__ import annotations

from dataclasses import dataclass

from ..llm.base import LlmClient, LlmRequest
from ..tools.registry import ToolContext, ToolRegistry
from .models import LlmMessage

MAX_ITERATIONS = 6
ITERATION_CAP_MESSAGE = "That took more steps than I can handle. Could you narrow the request?"


@dataclass
class AgentResult:
    text: str
    new_messages: list[LlmMessage]
    capped: bool


async def run_agent(*, llm: LlmClient, tools: ToolRegistry, system: str, history: list[LlmMessage], tool_ctx: ToolContext) -> AgentResult:
    """LLM -> tools -> LLM, at most MAX_ITERATIONS rounds. Returns only the messages produced in this run."""
    produced: list[LlmMessage] = []
    for _ in range(MAX_ITERATIONS):
        res = await llm.generate(LlmRequest(system=system, messages=[*history, *produced], tools=tools.specs()))
        if not res.tool_calls:
            text = (res.text or "").strip() or "Sorry, I couldn't come up with a reply. Could you rephrase?"
            produced.append({"role": "assistant", "text": text})
            return AgentResult(text, produced, False)
        entry: LlmMessage = {"role": "assistant", "toolCalls": res.tool_calls}
        if res.text:
            entry["text"] = res.text
        produced.append(entry)
        for call in res.tool_calls:
            result = await tools.execute(call["name"], call.get("args"), tool_ctx)
            produced.append({"role": "tool", "toolCallId": call["id"], "name": call["name"], "result": result})
    # Keep the transcript consistent: end with an assistant text message.
    produced.append({"role": "assistant", "text": ITERATION_CAP_MESSAGE})
    return AgentResult(ITERATION_CAP_MESSAGE, produced, True)
