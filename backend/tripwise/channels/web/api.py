from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from ...core.conversation_service import ConversationLimitReached, ConversationNotFound, ConversationService, DemoFull
from ...core.models import Ctx
from ...logger import log

_SESSION = re.compile(r"[A-Za-z0-9_-]{16,64}")
_MAX_BODY = 32_768


class _ChatBody(BaseModel):
    conversationId: str = Field(min_length=8, max_length=64)
    messageId: str = Field(min_length=8, max_length=64)
    text: str = Field(max_length=10_000)
    seenProposal: str | None = Field(default=None, max_length=64)  # token of the pending card on screen


class _ActionBody(BaseModel):
    conversationId: str = Field(min_length=8, max_length=64)
    token: str = Field(min_length=4, max_length=64)
    action: Literal["confirm", "cancel", "change"]


class _ConvBody(BaseModel):
    conversationId: str = Field(min_length=8, max_length=64)


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def web_user_id(session_id: str) -> str:
    """Only a hash of the session id is stored server-side."""
    return f"web:{hashlib.sha256(session_id.encode()).hexdigest()[:32]}"


async def _read_json(request: Request) -> Any:
    size, chunks = 0, []
    async for c in request.stream():
        size += len(c)
        if size > _MAX_BODY:
            raise HttpError(413, "Request too large")
        chunks.append(c)
    try:
        return json.loads(b"".join(chunks) or b"{}")
    except ValueError:
        raise HttpError(400, "Invalid JSON") from None


def _send(status: int, body: Any) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"cache-control": "no-store"})


async def handle_api(request: Request, service: ConversationService) -> JSONResponse:
    request_id = service.new_request_id()
    try:
        sid = request.headers.get("x-session-id")
        if sid is None or not _SESSION.fullmatch(sid):
            raise HttpError(401, "Missing or invalid X-Session-Id header")
        user_id = web_user_id(sid)

        def ctx_for(conversation_id: str) -> Ctx:
            return Ctx(user_id, conversation_id, request_id)

        async def conversation_view(conversation_id: str) -> JSONResponse:
            h = await service.history(user_id, conversation_id)
            body: dict[str, Any] = {"conversationId": conversation_id, "messages": h.messages}
            if h.pending_proposal:
                body["pendingProposal"] = h.pending_proposal.to_dict()
            return _send(200, body)

        route = f"{request.method} {request.url.path}"
        await service.admit(user_id)

        if route == "GET /api/conversation":
            return await conversation_view((await service.current_conversation(user_id)).id)
        if route == "GET /api/conversations":
            items = await service.list_conversations(user_id)
            return _send(200, {
                "limit": service.max_conversations,
                "conversations": [{"id": i.id, "title": i.title, "createdAt": i.created_at, "active": i.active} for i in items],
            })
        if route == "POST /api/conversation/new":
            c = await service.start_conversation(user_id)
            return await conversation_view(c.id)
        if route == "POST /api/conversation/open":
            o = _ConvBody.model_validate(await _read_json(request))
            await service.open_conversation(user_id, o.conversationId)
            return await conversation_view(o.conversationId)
        if route == "POST /api/conversation/delete":
            d = _ConvBody.model_validate(await _read_json(request))
            await service.delete_conversation(user_id, d.conversationId)
            return await conversation_view((await service.current_conversation(user_id)).id)
        if route == "GET /api/trips":
            trips = await service.trips(user_id)
            return _send(200, {"trips": [{"reference": t.reference, "createdAt": t.created_at, **t.payload} for t in trips]})
        if route == "POST /api/chat":
            b = _ChatBody.model_validate(await _read_json(request))
            if b.seenProposal:
                await service.mark_shown(user_id, b.conversationId, b.seenProposal)
            reply = await service.handle_message(ctx_for(b.conversationId), b.text, "web", b.messageId)
            return _send(200, {"reply": reply.to_dict(), "requestId": request_id})
        if route == "POST /api/action":
            a = _ActionBody.model_validate(await _read_json(request))
            out = await service.handle_action(ctx_for(a.conversationId), a.token, a.action)
            return _send(200, {"ok": out.ok, "toast": out.toast, "reply": out.reply.to_dict()})
        if route == "POST /api/simulate-outage":
            c2 = _ConvBody.model_validate(await _read_json(request))
            await service.simulate_outage(user_id, c2.conversationId)
            return _send(200, {"ok": True})
        raise HttpError(404, "Not found")
    except HttpError as e:
        return _send(e.status, {"error": str(e), "requestId": request_id})
    except ValidationError as e:
        details = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        return _send(400, {"error": "Invalid request", "details": details, "requestId": request_id})
    except ConversationNotFound as e:
        return _send(404, {"error": str(e), "requestId": request_id})
    except ConversationLimitReached as e:
        return _send(409, {"error": str(e), "requestId": request_id})
    except DemoFull as e:
        return _send(503, {"error": str(e), "requestId": request_id})
    except Exception as e:
        log("error", "web api failure", requestId=request_id, err=str(e))
        return _send(500, {"error": f"Something went wrong on our side (ref {request_id}).", "requestId": request_id})
