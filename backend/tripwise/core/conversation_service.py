from __future__ import annotations

import asyncio
import re
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeVar

from ..data.dates import now_ms, utcnow
from ..llm.base import LlmClient, LlmError
from ..logger import log
from ..store.repos import Repo, StoredMessage
from ..tools.registry import ToolContext, ToolRegistry
from .agent import run_agent
from .history import trim_history, visible_history
from .models import AssistantReply, Conversation, Ctx, Proposal
from .prompt import build_system_prompt
from .proposals import ActionResult, cancel_proposal, confirm_proposal, supersede_proposal
from .quota import RateLimiter

MAX_MESSAGE_LENGTH = 2000
MAX_CONVERSATIONS = 5
USER_IDLE_MS = 24 * 60 * 60_000
TITLE_LENGTH = 40
ProposalAction = Literal["confirm", "cancel", "change"]
T = TypeVar("T")

# Explicit approval only: an acknowledgement such as "ok" must never book.
_YES = re.compile(r"^(yes|y|confirm|confirmed|book it|do it|approve)[.!\s]*$", re.I)
_NO = re.compile(r"^(no|cancel|never ?mind|stop)[.!\s]*$", re.I)
REVIEW_FIRST = "Nothing is booked yet. Please check this proposal first, then confirm or cancel it."


class ConversationNotFound(Exception):
    def __init__(self) -> None:
        super().__init__("Conversation not found")


class ConversationLimitReached(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"You already have {limit} chats. Delete one to start a new one.")


class DemoFull(Exception):
    def __init__(self) -> None:
        super().__init__("The demo is full right now. A place opens when someone has been inactive for 24 hours, so please try again later.")


@dataclass
class ConversationSummary:
    id: str
    title: str
    created_at: int
    active: bool


@dataclass
class History:
    messages: list[dict[str, str]]
    pending_proposal: Proposal | None


@dataclass
class ActionOutcome:
    reply: AssistantReply
    toast: str
    ok: bool


class ConversationService:
    def __init__(
        self, repo: Repo, llm: LlmClient, tools: ToolRegistry, tz: str = "UTC", now: Callable[[], datetime] | None = None,
        history_token_budget: int = 24_000, limiter: RateLimiter | None = None,
        max_conversations: int = MAX_CONVERSATIONS, max_users: int | None = None,
    ) -> None:
        self.repo, self.llm, self.tools, self.tz = repo, llm, tools, tz
        self._now = now or utcnow
        self._budget = history_token_budget
        self._limiter = limiter
        self.max_conversations, self.max_users = max_conversations, max_users
        self._locks: dict[str, list] = {}  # conversation id -> [lock, waiters]

    def now(self) -> datetime:
        return self._now()

    @staticmethod
    def new_request_id() -> str:
        return str(uuid.uuid4())[:8]

    async def admit(self, user_id: str) -> None:
        """Channels call this once per incoming request. Raises DemoFull when every user slot is taken."""
        if self.max_users is None:
            return
        if not await self.repo.claim_seat(user_id, self.max_users, USER_IDLE_MS, now_ms(self.now())):
            raise DemoFull()

    async def start_conversation(self, user_id: str) -> Conversation:
        """A fresh chat. An existing empty one is reused, so repeated "New chat" never fills the limit."""

        async def job() -> Conversation:
            existing = await self.repo.list_conversations(user_id)
            for c in existing:
                if not await self.repo.get_messages(c.id):
                    await self.repo.touch_conversation(c.id)
                    return c
            if len(existing) >= self.max_conversations:
                raise ConversationLimitReached(self.max_conversations)
            return await self.repo.create_conversation(user_id)

        return await self._serial(f"user:{user_id}", job)

    async def current_conversation(self, user_id: str) -> Conversation:
        """The user's most recently active conversation, created if none exists."""

        async def job() -> Conversation:
            return await self.repo.latest_conversation(user_id) or await self.repo.create_conversation(user_id)

        return await self._serial(f"user:{user_id}", job)

    async def list_conversations(self, user_id: str) -> list[ConversationSummary]:
        current = await self.current_conversation(user_id)
        chats = await self.repo.list_conversations(user_id)
        transcripts = await asyncio.gather(*(self.repo.get_messages(c.id) for c in chats))
        return [
            ConversationSummary(c.id, _title(messages), c.created_at, c.id == current.id)
            for c, messages in zip(chats, transcripts, strict=True)
        ]

    async def open_conversation(self, user_id: str, conversation_id: str) -> Conversation:
        """Switch back to an earlier chat: it becomes the current one on this account."""
        c = await self._assert_owner(user_id, conversation_id)
        await self.repo.touch_conversation(conversation_id)
        return c

    async def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        """Waits for any message being processed in that chat. Booked trips are kept."""

        async def job() -> None:
            await self._assert_owner(user_id, conversation_id)
            await self.repo.delete_conversation(conversation_id)

        await self._serial(conversation_id, job)

    async def conversation_of_proposal(self, user_id: str, token: str) -> str | None:
        """Lets a button on an older chat's card act in the chat it belongs to."""
        p = await self.repo.get_proposal(token)
        return p.conversation_id if p and p.user_id == user_id else None

    async def history(self, user_id: str, conversation_id: str) -> History:
        await self._assert_owner(user_id, conversation_id)
        stored = await self.repo.get_messages(conversation_id)
        pending = await self.repo.latest_proposed(user_id, conversation_id)
        return History(visible_history([s.message for s in stored]), pending)

    async def mark_shown(self, user_id: str, conversation_id: str, token: str) -> None:
        """Channels call this once a proposal card has reached the user."""
        p = await self.repo.get_proposal(token)
        if p and p.user_id == user_id and p.conversation_id == conversation_id:
            await self.repo.mark_proposal_shown(token)

    async def trips(self, user_id: str):
        return await self.repo.list_bookings(user_id)

    async def simulate_outage(self, user_id: str, conversation_id: str) -> None:
        """Arm a one-shot simulated outage for this conversation only."""
        await self._assert_owner(user_id, conversation_id)
        await self.repo.set_outage(conversation_id, True)

    async def _serial(self, key: str, job: Callable[[], Awaitable[T]]) -> T:
        """Serial per key (a conversation, or "user:<id>"): later jobs wait for earlier ones (FIFO)."""
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                return await job()
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(key, None)

    async def handle_message(self, ctx: Ctx, text: str, channel: str, event_id: str | None = None) -> AssistantReply:
        return await self._serial(ctx.conversation_id, lambda: self._process(ctx, text, channel, event_id))

    async def handle_action(self, ctx: Ctx, token: str, action: ProposalAction) -> ActionOutcome:
        """Button press. Deterministic, never goes through the LLM."""

        async def job() -> ActionOutcome:
            await self._assert_owner(ctx.user_id, ctx.conversation_id)
            r = await self._apply_action(ctx, token, action)
            label = {"confirm": "Confirm", "cancel": "Cancel", "change": "Change"}[action]
            await self.repo.append_messages(
                ctx.conversation_id, [{"role": "user", "text": f'[Pressed "{label}"]'}, {"role": "assistant", "text": r.message}]
            )
            return ActionOutcome(AssistantReply(r.message, r.proposal), r.message, r.ok)

        return await self._serial(ctx.conversation_id, job)

    async def _apply_action(self, ctx: Ctx, token: str, action: str) -> ActionResult:
        if action == "confirm":
            return await confirm_proposal(self.repo, token, ctx, self.now(), self.tz)
        if action == "cancel":
            return await cancel_proposal(self.repo, token, ctx)
        return await supersede_proposal(self.repo, token, ctx)

    async def _assert_owner(self, user_id: str, conversation_id: str) -> Conversation:
        c = await self.repo.get_conversation(conversation_id)
        if not c or c.user_id != user_id:
            raise ConversationNotFound()
        return c

    async def _process(self, ctx: Ctx, text: str, channel: str, event_id: str | None) -> AssistantReply:
        repo = self.repo
        conv = await self._assert_owner(ctx.user_id, ctx.conversation_id)
        if event_id:
            if await repo.claim_event(channel, event_id) == "duplicate":
                stored = await repo.get_event_result(channel, event_id)
                return AssistantReply.from_dict(stored) if stored else AssistantReply("Still working on that one, one moment.")
        try:
            await repo.touch_conversation(ctx.conversation_id)
            reply = await self._run(ctx, conv, text)
            if event_id:
                await repo.complete_event(channel, event_id, reply.to_dict())
            return reply
        except BaseException:
            if event_id:
                await repo.release_event(channel, event_id)
            raise

    async def _run(self, ctx: Ctx, conv: Conversation, raw_text: str) -> AssistantReply:
        repo = self.repo
        text = raw_text.strip()
        if not text:
            return AssistantReply("Please type a message and I will help.")
        if len(text) > MAX_MESSAGE_LENGTH:
            return AssistantReply(f"That message is too long ({len(text)} characters). Please keep it under {MAX_MESSAGE_LENGTH}.")
        if self._limiter and not self._limiter.allow(ctx.user_id):
            return AssistantReply("You're sending messages too quickly. Please wait a few seconds and try again.")

        # The user's message is saved first, so it survives any failure below.
        await repo.append_messages(ctx.conversation_id, [{"role": "user", "text": text}])

        # Text fallback for the buttons: "yes" / "cancel" act on the latest pending proposal.
        pending = await repo.latest_proposed(ctx.user_id, ctx.conversation_id)
        if pending and (_YES.match(text) or _NO.match(text)):
            # A "yes" can't approve a card the user never received (e.g. it failed to send): show it instead.
            if _YES.match(text) and not pending.shown and now_ms(self.now()) <= pending.expires_at:
                await repo.append_messages(ctx.conversation_id, [{"role": "assistant", "text": REVIEW_FIRST}])
                return AssistantReply(REVIEW_FIRST, pending)
            r = await self._apply_action(ctx, pending.token, "confirm" if _YES.match(text) else "cancel")
            await repo.append_messages(ctx.conversation_id, [{"role": "assistant", "text": r.message}])
            return AssistantReply(r.message, r.proposal)

        try:
            if conv.outage_armed:
                await repo.set_outage(ctx.conversation_id, False)  # auto-clears after one failure
                raise LlmError("outage", "Simulated provider outage")
            now = self.now()
            stored = await repo.get_messages(ctx.conversation_id)
            history = trim_history([s.message for s in stored], self._budget)
            result = await run_agent(
                llm=self.llm, tools=self.tools, system=build_system_prompt(now, self.tz), history=history,
                tool_ctx=ToolContext(ctx, repo, now, self.tz),
            )
            await repo.append_messages(ctx.conversation_id, result.new_messages)
            proposed_now = any(m["role"] == "tool" and m.get("name") == "propose_booking" for m in result.new_messages)
            proposal = await repo.latest_proposed(ctx.user_id, ctx.conversation_id) if proposed_now else None
            return AssistantReply(result.text, proposal)
        except Exception as e:
            if isinstance(e, LlmError):
                log("error", "llm_failed", ref=ctx.request_id, kind=e.kind, error=repr(e))
            else:
                log("error", "request_failed", ref=ctx.request_id, error=repr(e), trace=traceback.format_exc())
            msg = friendly_error(e, ctx.request_id)
            await repo.append_messages(ctx.conversation_id, [{"role": "assistant", "text": msg}])
            return AssistantReply(msg, quick_replies=["Try again"])


def _title(stored: list[StoredMessage]) -> str:
    """A chat is named after the first thing the user typed (button presses don't count)."""
    for s in stored:
        m = s.message
        if m["role"] == "user" and not m["text"].startswith("[Pressed "):
            text = " ".join(m["text"].split())
            return text if len(text) <= TITLE_LENGTH else text[: TITLE_LENGTH - 1].rstrip() + "…"
    return "New chat"


def friendly_error(e: BaseException, ref: str) -> str:
    if not isinstance(e, LlmError):  # storage or code failure: don't blame the AI provider
        base = "Something went wrong on our side. Your message was saved, so please try again in a moment."
    elif e.kind == "quota":
        base = "Demo quota reached for today. Please try again tomorrow, or watch the demo video."
    else:
        base = "I'm having trouble reaching my AI provider right now. Your message was saved, so please try again in a moment."
    return f"{base} (ref {ref})"
