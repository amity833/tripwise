from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ..core.models import LlmMessage
from .base import LlmError, LlmRequest, LlmResponse

Step = dict[str, Any]


def to_steps(messages: list[LlmMessage]) -> list[Step]:
    """Map our transcript to Interactions API input steps (stateless: the whole history is resent each turn)."""
    out: list[Step] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"type": "user_input", "content": [{"type": "text", "text": m["text"]}]})
        elif role == "assistant":
            if m.get("text"):
                out.append({"type": "model_output", "content": [{"type": "text", "text": m["text"]}]})
            for i, c in enumerate(m.get("toolCalls") or []):
                if i == 0 and c.get("thoughtSignature"):  # Gemini 3 rejects function calls replayed without their signature
                    out.append({"type": "thought", "signature": c["thoughtSignature"]})
                out.append({"type": "function_call", "id": c["id"], "name": c["name"], "arguments": c.get("args") or {}})
        else:
            out.append({
                "type": "function_result", "name": m["name"], "call_id": m["toolCallId"],
                "result": [{"type": "text", "text": json.dumps(m["result"], default=str)}],
            })
    return out


URL = "https://generativelanguage.googleapis.com/v1beta/interactions"


class GeminiClient:
    def __init__(
        self, api_key: str, model: str, timeout_s: float = 25.0, client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._key, self._model, self._timeout, self._sleep = api_key, model, timeout_s, sleep
        self._client = client or httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate(self, req: LlmRequest) -> LlmResponse:
        """One retry with backoff on timeout, 429 and 5xx."""
        try:
            return await self._once(req)
        except LlmError as e:
            if e.kind in ("timeout", "rate_limited", "server"):
                await self._sleep(0.8)
                return await self._once(req)
            raise

    async def _once(self, req: LlmRequest) -> LlmResponse:
        body: dict[str, Any] = {
            "model": self._model, "store": False, "system_instruction": req.system, "input": to_steps(req.messages),
        }
        if req.tools:
            body["tools"] = [{"type": "function", **t} for t in req.tools]
        try:
            res = await self._client.post(URL, json=body, headers={"x-goog-api-key": self._key}, timeout=self._timeout)
        except httpx.TimeoutException:
            raise LlmError("timeout", "Gemini request timed out") from None
        except httpx.HTTPError as e:
            raise LlmError("server", f"Gemini network error: {e}") from None
        detail = f"Gemini {res.status_code}: {res.text[:300]}"
        if res.status_code == 429:
            # A per-day limit will not clear within a retry; report it as quota so the user sees the right message.
            raise LlmError("quota" if "per day" in res.text else "rate_limited", detail)
        if res.status_code >= 500:
            raise LlmError("server", detail)
        if res.status_code >= 400:
            raise LlmError("bad_request", detail)
        steps: list[Step] = res.json().get("steps") or []
        text = "".join(
            b["text"] for st in steps if st.get("type") == "model_output"
            for b in st.get("content") or [] if isinstance(b.get("text"), str)
        ).strip()
        signature = next((st["signature"] for st in steps if st.get("type") == "thought" and st.get("signature")), None)
        calls = [
            {"id": st["id"], "name": st["name"], "args": st.get("arguments") or {}}
            for st in steps if st.get("type") == "function_call"
        ]
        if calls and signature:
            calls[0]["thoughtSignature"] = signature
        return LlmResponse(text or None, calls or None)
