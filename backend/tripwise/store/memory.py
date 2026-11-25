from __future__ import annotations

import copy
import time
import uuid
from dataclasses import replace
from typing import Any, Literal

from ..core.models import Booking, Conversation, LlmMessage, Proposal, User
from .repos import StoredMessage

CLAIM_TTL_MS = 2 * 60_000


def _ms() -> int:
    return int(time.time() * 1000)


class MemoryRepo:
    """
    In-memory store. LIMITATIONS (documented): data is lost on restart or sleep,
    not shared across instances, and capped per conversation. Dev and tests only.
    """

    def __init__(self, max_messages_per_conversation: int = 500) -> None:
        self._max = max_messages_per_conversation
        self._users: dict[str, User] = {}
        self._convs: dict[str, Conversation] = {}
        self._msgs: dict[str, list[StoredMessage]] = {}
        self._proposals: dict[str, Proposal] = {}
        self._bookings: list[Booking] = []
        self._events: dict[tuple[str, str], dict[str, Any]] = {}
        self._seq = 0

    async def ensure_user(self, user_id: str) -> User:
        if user_id not in self._users:
            self._users[user_id] = User(user_id, _ms())
        return self._users[user_id]

    def _next_active_at(self, user_id: str) -> int:
        # Strictly increasing per user, so two actions in the same millisecond still have an order.
        latest = max((c.active_at for c in self._convs.values() if c.user_id == user_id), default=0)
        return max(_ms(), latest + 1)

    async def create_conversation(self, user_id: str) -> Conversation:
        await self.ensure_user(user_id)
        c = Conversation(str(uuid.uuid4()), user_id, _ms(), False, self._next_active_at(user_id))
        self._convs[c.id] = c
        return c

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._convs.get(conversation_id)

    async def latest_conversation(self, user_id: str) -> Conversation | None:
        return max(await self.list_conversations(user_id), key=lambda c: c.active_at, default=None)

    async def list_conversations(self, user_id: str) -> list[Conversation]:
        return [c for c in self._convs.values() if c.user_id == user_id]  # insertion order

    async def touch_conversation(self, conversation_id: str) -> None:
        if c := self._convs.get(conversation_id):
            c.active_at = self._next_active_at(c.user_id)

    async def delete_conversation(self, conversation_id: str) -> None:
        self._convs.pop(conversation_id, None)
        self._msgs.pop(conversation_id, None)
        self._proposals = {t: p for t, p in self._proposals.items() if p.conversation_id != conversation_id}

    async def claim_seat(self, user_id: str, max_users: int, idle_ms: int, now_ms: int) -> bool:
        cutoff = now_ms - idle_ms
        u = self._users.get(user_id)
        holds = u is not None and u.last_seen_at > cutoff
        if not holds and sum(x.last_seen_at > cutoff for x in self._users.values()) >= max_users:
            return False
        if u is None:
            u = self._users[user_id] = User(user_id, _ms())
        u.last_seen_at = now_ms  # no await between check and write, so this is atomic on the event loop
        return True

    async def set_outage(self, conversation_id: str, armed: bool) -> None:
        if c := self._convs.get(conversation_id):
            c.outage_armed = armed

    async def append_messages(self, conversation_id: str, msgs: list[LlmMessage]) -> None:
        lst = self._msgs.setdefault(conversation_id, [])
        for m in msgs:
            self._seq += 1
            lst.append(StoredMessage(self._seq, conversation_id, copy.deepcopy(m), _ms()))
        if len(lst) > self._max:  # cap: drop oldest
            del lst[: len(lst) - self._max]

    async def get_messages(self, conversation_id: str) -> list[StoredMessage]:
        return list(self._msgs.get(conversation_id, []))

    async def save_proposal(self, p: Proposal) -> None:
        self._proposals[p.token] = copy.deepcopy(p)

    async def get_proposal(self, token: str) -> Proposal | None:
        p = self._proposals.get(token)
        return copy.deepcopy(p) if p else None

    async def latest_proposed(self, user_id: str, conversation_id: str) -> Proposal | None:
        live = [
            p for p in self._proposals.values()
            if p.user_id == user_id and p.conversation_id == conversation_id and p.status == "proposed"
        ]
        live.sort(key=lambda p: -p.created_at)  # stable: ties keep insertion order
        return copy.deepcopy(live[0]) if live else None

    async def transition_proposal(self, token: str, from_status: str, to_status: str) -> bool:
        p = self._proposals.get(token)
        if not p or p.status != from_status:
            return False
        p.status = to_status  # no await between check and write, so this is atomic on the event loop
        return True

    async def mark_proposal_shown(self, token: str) -> None:
        if p := self._proposals.get(token):
            p.shown = True

    async def add_booking(self, b: Booking) -> None:
        self._bookings.append(replace(b))

    async def list_bookings(self, user_id: str) -> list[Booking]:
        return [b for b in self._bookings if b.user_id == user_id]

    async def claim_event(self, channel: str, event_id: str) -> Literal["claimed", "duplicate"]:
        e = self._events.get((channel, event_id))
        # A claim that was never completed (crash) can be taken over after the TTL.
        if e and not (e["state"] == "claimed" and _ms() - e["at"] > CLAIM_TTL_MS):
            return "duplicate"
        self._events[(channel, event_id)] = {"state": "claimed", "at": _ms()}
        return "claimed"

    async def complete_event(self, channel: str, event_id: str, result: Any = None) -> None:
        self._events[(channel, event_id)] = {"state": "done", "result": copy.deepcopy(result), "at": _ms()}

    async def release_event(self, channel: str, event_id: str) -> None:
        self._events.pop((channel, event_id), None)

    async def get_event_result(self, channel: str, event_id: str) -> Any | None:
        e = self._events.get((channel, event_id))
        return copy.deepcopy(e.get("result")) if e and e["state"] == "done" else None
