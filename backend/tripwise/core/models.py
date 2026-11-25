from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# A transcript entry is a plain JSON-able dict (this is also exactly what is persisted):
#   {"role": "user", "text": str}
#   {"role": "assistant", "text"?: str, "toolCalls"?: [{"id", "name", "args"}]}
#   {"role": "tool", "toolCallId": str, "name": str, "result": Any}
LlmMessage = dict[str, Any]
ToolCall = dict[str, Any]

PROPOSAL_STATUSES = ("proposed", "confirmed", "cancelled", "expired", "superseded")


@dataclass
class Proposal:
    token: str
    user_id: str
    conversation_id: str
    status: str
    payload: dict[str, Any]
    expires_at: int
    created_at: int
    shown: bool = False  # the card reached the user; only then can a typed "yes" confirm it

    def to_dict(self) -> dict[str, Any]:
        """Wire format (camelCase), as the web app and Telegram cards expect."""
        return {
            "token": self.token, "userId": self.user_id, "conversationId": self.conversation_id,
            "status": self.status, "payload": self.payload, "expiresAt": self.expires_at, "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Proposal:
        return cls(d["token"], d["userId"], d["conversationId"], d["status"], d["payload"], d["expiresAt"], d["createdAt"])


@dataclass
class Booking:
    id: str
    user_id: str
    reference: str
    proposal_token: str
    payload: dict[str, Any]
    created_at: int


@dataclass
class User:
    id: str
    created_at: int
    last_seen_at: int = 0  # 0 = never took a user slot


@dataclass
class Conversation:
    id: str
    user_id: str
    created_at: int
    outage_armed: bool = False
    active_at: int = 0  # when it was last opened or written to; the highest one is the user's current chat


@dataclass
class AssistantReply:
    """Channel-agnostic reply. Channels render this."""

    text: str
    proposal: Proposal | None = None
    quick_replies: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"text": self.text}
        if self.proposal is not None:
            d["proposal"] = self.proposal.to_dict()
        if self.quick_replies is not None:
            d["quickReplies"] = self.quick_replies
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AssistantReply:
        p = d.get("proposal")
        return cls(d["text"], Proposal.from_dict(p) if p else None, d.get("quickReplies"))


@dataclass(frozen=True)
class Ctx:
    """Who is talking and where. Built by channels, understood by core."""

    user_id: str
    conversation_id: str
    request_id: str = field(default="")
