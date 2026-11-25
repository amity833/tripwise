from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...core.conversation_service import (
    ConversationLimitReached,
    ConversationNotFound,
    ConversationService,
    ConversationSummary,
    DemoFull,
    ProposalAction,
)
from ...core.models import Ctx, Proposal
from ...core.trips import describe_trips
from ...logger import log
from ...store.repos import Repo
from .api import TelegramApi, TelegramApiError
from .format import escape_html, render_proposal, split_message, to_telegram_html


class _User(BaseModel):
    id: int


class _Chat(BaseModel):
    id: int
    type: str


class _Message(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    message_id: int
    chat: _Chat
    from_: _User | None = Field(default=None, alias="from")
    text: str | None = None


class _CallbackMessage(BaseModel):
    message_id: int
    chat: _Chat


class _CallbackQuery(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str
    from_: _User = Field(alias="from")
    data: str | None = None
    message: _CallbackMessage | None = None


class Update(BaseModel):
    model_config = ConfigDict(extra="allow")
    update_id: int
    message: _Message | None = None
    edited_message: _Message | None = None
    callback_query: _CallbackQuery | None = None


COMMANDS = [
    {"command": "start", "description": "Start planning a trip"}, {"command": "help", "description": "What I can do"},
    {"command": "new", "description": "Start a fresh conversation"}, {"command": "chats", "description": "Switch between or delete chats"},
    {"command": "trips", "description": "Show my booked trips"},
    {"command": "simulate_outage", "description": "Demo: simulate an AI outage once"},
]
HELP = (
    '<b>Tripwise</b>, your corporate travel assistant.\n\nTry: "I need to be in Berlin Tuesday to Thursday, flying from Tel Aviv".\n'
    "I find flights and hotels, check company policy, and propose a trip. <b>Nothing is booked until you press Confirm.</b>\n\n"
    "/new fresh conversation · /chats switch or delete chats · /trips my trips · /simulate_outage demo a failure"
)
_ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]
_COMMAND = re.compile(r"^/(\w+)(@\w+)?(\s|$)", re.ASCII)
_CALLBACK = re.compile(r"^([cmx]):([0-9a-f]{4,32})$")
_ACTIONS: dict[str, ProposalAction] = {"c": "confirm", "m": "change", "x": "cancel"}
# Chat list buttons: o = open, d = ask to delete, D = delete confirmed, l = back to the list.
_CHAT_CALLBACK = re.compile(r"^(?:([odD]):([0-9a-f-]{36})|(l))$")


def _same_secret(a: str, b: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest())


class TelegramAdapter:
    def __init__(
        self, api: TelegramApi, service: ConversationService, repo: Repo, webhook_secret: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api, self.service, self.repo, self.webhook_secret, self._sleep = api, service, repo, webhook_secret, sleep
        self._tasks: set[asyncio.Task[Any]] = set()

    # --- sending ------------------------------------------------------------------------------------------------

    async def _safe_call(self, method: str, body: dict[str, Any]) -> bool:
        """Send once, retry once, never throw: the reply is already persisted by the service."""
        for i in range(2):
            try:
                await self.api.call(method, body)
                return True
            except Exception as e:
                if isinstance(e, TelegramApiError) and re.search(r"can't parse entities", e.description, re.I):
                    return False
                if isinstance(e, TelegramApiError) and re.search(r"message is not modified", e.description, re.I):
                    return True  # an edit that changes nothing, e.g. tapping the current chat: already as wanted
                log("warn", "telegram call failed", method=method, attempt=i + 1, err=str(e))
                if i == 0:
                    await self._sleep(0.5)
        return False

    async def _send_text(self, chat_id: int, text: str, extra: dict[str, Any] | None = None, raw: bool = False) -> bool:
        """True when every chunk was delivered."""
        extra = extra or {}
        delivered = True
        chunks = split_message(text, 4096, (lambda s: s) if raw else to_telegram_html)
        for i, chunk in enumerate(chunks):
            markup = extra if i == len(chunks) - 1 else {}
            html = chunk if raw else to_telegram_html(chunk)
            ok = await self._safe_call("sendMessage", {"chat_id": chat_id, "text": html, "parse_mode": "HTML", **markup})
            if not ok:  # parse error fallback: plain text, no parse_mode
                ok = await self._safe_call("sendMessage", {"chat_id": chat_id, "text": re.sub(r"<[^>]+>", "", chunk), **markup})
            delivered = delivered and ok
        return delivered

    async def _send_card(self, chat_id: int, p: Proposal) -> None:
        card = render_proposal(p)
        extra = {"reply_markup": {"inline_keyboard": card["keyboard"]}} if card["keyboard"] else {}
        if await self._send_text(chat_id, card["text"], extra, raw=True):
            await self.service.mark_shown(p.user_id, p.conversation_id, p.token)

    @contextlib.asynccontextmanager
    async def _typing(self, chat_id: int) -> AsyncIterator[None]:
        """Keep the "typing…" indicator alive while the assistant works."""

        async def tick() -> None:
            while True:
                await self._safe_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                await asyncio.sleep(4)

        task = asyncio.create_task(tick())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _ctx_for(self, tg_user_id: int) -> Ctx:
        user_id = f"telegram:{tg_user_id}"
        conv = await self.service.current_conversation(user_id)
        return Ctx(user_id, conv.id, self.service.new_request_id())

    # --- chat list ----------------------------------------------------------------------------------------------

    def _chat_list(self, chats: list[ConversationSummary]) -> tuple[str, list[list[dict[str, str]]]]:
        text = (
            f"<b>Your chats ({len(chats)}/{self.service.max_conversations})</b>\n"
            "Tap a chat to switch to it, or 🗑 to delete it. ● marks the current one."
        )
        keyboard = [
            [
                {"text": f"{'● ' if c.active else ''}{c.title}", "callback_data": f"o:{c.id}"},
                {"text": "🗑", "callback_data": f"d:{c.id}"},
            ]
            for c in chats
        ]
        return text, keyboard

    async def _send_chat_list(self, chat_id: int, user_id: str) -> None:
        text, keyboard = self._chat_list(await self.service.list_conversations(user_id))
        await self._send_text(chat_id, text, {"reply_markup": {"inline_keyboard": keyboard}}, raw=True)

    async def _edit(self, chat_id: int, message_id: int, text: str, keyboard: list[list[dict[str, str]]]) -> None:
        await self._safe_call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": keyboard},
        })

    async def _on_chat_button(self, q: _CallbackQuery, user_id: str, action: str, conversation_id: str | None) -> str:
        """Returns the toast text."""
        chat_id = q.message.chat.id if q.message else q.from_.id
        chats = await self.service.list_conversations(user_id)
        target = next((c for c in chats if c.id == conversation_id), None)
        toast = "Done"
        if action != "l" and target is None:
            toast = "That chat no longer exists."
        elif action == "o" and target:
            await self.service.open_conversation(user_id, target.id)
            toast = "Switched"
        elif action == "d" and target:
            if q.message:
                await self._edit(
                    chat_id, q.message.message_id,
                    f"Delete <b>{escape_html(target.title)}</b>? Its messages are removed for good. Booked trips are kept.",
                    [[{"text": "Yes, delete", "callback_data": f"D:{target.id}"}, {"text": "Keep it", "callback_data": "l"}]],
                )
            return "Please confirm"
        elif action == "D" and target:
            await self.service.delete_conversation(user_id, target.id)
            toast = "Chat deleted"

        if q.message:  # refresh the list in place
            text, keyboard = self._chat_list(await self.service.list_conversations(user_id))
            await self._edit(chat_id, q.message.message_id, text, keyboard)
        if toast == "Switched" and target:
            await self._send_recap(chat_id, user_id, target)
        return toast

    async def _send_recap(self, chat_id: int, user_id: str, chat: ConversationSummary) -> None:
        """Telegram shows one continuous thread, so say where we are and where that chat left off."""
        h = await self.service.history(user_id, chat.id)
        last = next((m["text"] for m in reversed(h.messages) if m["role"] == "assistant"), None)
        text = f"Switched to: {chat.title}" + (f"\n\nLast reply there:\n{last}" if last else "")
        await self._send_text(chat_id, text)
        if h.pending_proposal:
            await self._send_card(chat_id, h.pending_proposal)

    # --- handlers -----------------------------------------------------------------------------------------------

    async def _on_message(self, m: _Message) -> None:
        chat_id = m.chat.id
        if m.chat.type != "private":
            await self._send_text(chat_id, "I only work in direct messages. Please message me privately.")
            return
        if m.from_ is None:
            return
        if m.text is None:
            await self._send_text(chat_id, "I can only read text messages for now. Please type your request.")
            return
        text = m.text.strip()
        uid = m.from_.id
        try:
            await self.service.admit(f"telegram:{uid}")
        except DemoFull as e:
            await self._send_text(chat_id, str(e))
            return

        cmd_match = _COMMAND.match(text)
        if cmd_match:
            cmd = cmd_match.group(1).lower()
            if cmd in ("start", "help"):
                await self._ctx_for(uid)
                await self._send_text(chat_id, HELP, {}, raw=True)
            elif cmd == "new":
                try:
                    await self.service.start_conversation(f"telegram:{uid}")
                except ConversationLimitReached as e:
                    await self._send_text(chat_id, str(e))
                    await self._send_chat_list(chat_id, f"telegram:{uid}")
                else:
                    await self._send_text(
                        chat_id, "Fresh conversation started. Earlier chats are under /chats, and booked trips under /trips."
                    )
            elif cmd == "chats":
                await self._send_chat_list(chat_id, f"telegram:{uid}")
            elif cmd == "trips":
                trips = await self.service.trips(f"telegram:{uid}")
                if not trips:
                    await self._send_text(chat_id, "You have no confirmed trips yet.")
                else:
                    await self._send_text(chat_id, describe_trips(trips))
            elif cmd == "simulate_outage":
                ctx = await self._ctx_for(uid)
                await self.service.simulate_outage(ctx.user_id, ctx.conversation_id)
                await self._send_text(chat_id, "Okay: the next message will hit a simulated AI outage (once). Send anything to see how I handle it.")
            else:
                await self._send_text(chat_id, "Unknown command. Try /help.")
            return

        ctx = await self._ctx_for(uid)
        try:
            async with self._typing(chat_id):
                reply = await self.service.handle_message(ctx, text, "telegram")
        except ConversationNotFound:  # deleted while this message was waiting
            await self._send_text(chat_id, "That chat was just deleted. Please send your message again.")
            return
        extra: dict[str, Any] = {}
        if reply.quick_replies and not reply.proposal:
            extra = {"reply_markup": {"keyboard": [[{"text": t} for t in reply.quick_replies]], "resize_keyboard": True, "one_time_keyboard": True}}
        await self._send_text(chat_id, reply.text, extra)
        if reply.proposal:
            await self._send_card(chat_id, reply.proposal)

    async def _on_callback(self, q: _CallbackQuery) -> None:
        toast = "Done"
        try:
            m = _CALLBACK.match(q.data or "")
            chat_button = _CHAT_CALLBACK.match(q.data or "")
            if not m and not chat_button:
                toast = "That button is no longer valid."
                return
            user_id = f"telegram:{q.from_.id}"
            try:
                await self.service.admit(user_id)
            except DemoFull as e:
                toast = str(e)
                return
            if chat_button:
                action, conversation_id, back = chat_button.groups()
                toast = await self._on_chat_button(q, user_id, action or back, conversation_id)
                return
            assert m is not None
            ctx = await self._ctx_for(q.from_.id)
            # A card from an earlier chat still works: act in the chat that proposal belongs to.
            if owner := await self.service.conversation_of_proposal(user_id, m.group(2)):
                ctx = Ctx(user_id, owner, ctx.request_id)
            out = await self.service.handle_action(ctx, m.group(2), _ACTIONS[m.group(1)])
            toast = out.toast
            chat_id = q.message.chat.id if q.message else q.from_.id
            if out.reply.proposal and q.message:
                card = render_proposal(out.reply.proposal)
                # Edit the card in place so buttons disappear and the status updates.
                ok = await self._safe_call("editMessageText", {
                    "chat_id": chat_id, "message_id": q.message.message_id, "text": card["text"], "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": card["keyboard"] or []},
                })
                if not ok:
                    await self._safe_call("editMessageReplyMarkup", {
                        "chat_id": chat_id, "message_id": q.message.message_id, "reply_markup": {"inline_keyboard": []},
                    })
            if out.ok or out.reply.proposal:
                await self._send_text(chat_id, out.reply.text)
        finally:
            await self._safe_call("answerCallbackQuery", {"callback_query_id": q.id, "text": toast[:190]})  # never leave the spinner

    async def handle_update(self, raw: Any) -> str:
        """Claim -> process -> done. On failure the claim is released so Telegram's retry is processed."""
        u = Update.model_validate(raw)
        key = f"cb:{u.callback_query.id}" if u.callback_query else f"u:{u.update_id}"
        if await self.repo.claim_event("telegram", key) == "duplicate":
            return "duplicate"
        try:
            if u.edited_message:
                await self._send_text(u.edited_message.chat.id, "I can't process edited messages. Please send your change as a new message.")
            elif u.callback_query:
                await self._on_callback(u.callback_query)
            elif u.message:
                await self._on_message(u.message)
            await self.repo.complete_event("telegram", key)
            return "processed"
        except BaseException:
            await self.repo.release_event("telegram", key)
            raise

    # --- transports ---------------------------------------------------------------------------------------------

    async def webhook(self, request: Request) -> Response:
        given = request.headers.get("x-telegram-bot-api-secret-token")
        if not self.webhook_secret or given is None or not _same_secret(given, self.webhook_secret):
            return Response(status_code=401)
        try:
            size, chunks = 0, []
            async for c in request.stream():
                size += len(c)
                if size > 1_000_000:
                    return Response(status_code=413)
                chunks.append(c)
            await self.handle_update(json.loads(b"".join(chunks)))
            return Response("ok", status_code=200)
        except (ValidationError, ValueError):
            return Response("ignored", status_code=200)  # malformed: do not make Telegram retry forever
        except Exception as e:
            log("error", "telegram update failed", err=str(e))
            return Response(status_code=500)  # claim released, so the retry is processed

    async def poll(self) -> None:
        """Local development: long polling with a separate dev bot token."""
        await self.api.call("deleteWebhook")
        offset = 0
        while True:
            try:
                updates = await self.api.call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": _ALLOWED_UPDATES})
                for u in updates:
                    offset = u["update_id"] + 1
                    task = asyncio.create_task(self._poll_one(u))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            except Exception as e:
                log("warn", "polling error", err=str(e))
                await self._sleep(2)

    async def _poll_one(self, u: Any) -> None:
        try:
            await self.handle_update(u)
        except Exception as e:
            log("error", "poll update failed", err=str(e))

    async def register(self, public_url: str | None = None) -> None:
        await self._safe_call("setMyCommands", {"commands": COMMANDS})
        if public_url:
            await self.api.call("setWebhook", {
                "url": f"{public_url.rstrip('/')}/telegram/webhook", "secret_token": self.webhook_secret, "allowed_updates": _ALLOWED_UPDATES,
            })


__all__ = ["COMMANDS", "TelegramAdapter", "escape_html"]
