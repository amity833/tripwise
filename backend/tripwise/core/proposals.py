from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from ..data.dates import now_ms
from ..store.repos import Repo
from .models import Booking, Proposal
from .policy import check_policy

PROPOSAL_TTL_MS = 30 * 60_000


def reference(token: str) -> str:
    return f"TW-{token[:6].upper()}"


class Who(Protocol):
    user_id: str
    conversation_id: str


@dataclass
class CreateResult:
    ok: bool
    proposal: Proposal | None = None
    warnings: list[str] | None = None
    error: str | None = None
    reasons: list[str] | None = None
    suggestion: str | None = None


async def create_proposal(
    repo: Repo, who: Who, flight_id: str, return_flight_id: str | None, hotel_id: str | None, now: datetime, tz: str,
) -> CreateResult:
    flight_ids = [flight_id, *([return_flight_id] if return_flight_id else [])]
    policy = check_policy(flight_ids, hotel_id, now, tz)
    if policy.verdict == "block":
        return CreateResult(False, error="Blocked by company policy", reasons=policy.reasons, suggestion=policy.suggestion)

    # Only one live proposal per conversation.
    while (p := await repo.latest_proposed(who.user_id, who.conversation_id)) is not None:
        await repo.transition_proposal(p.token, "proposed", "superseded")

    total = sum(f.priceUsd for f in policy.flights) + (policy.hotel.totalUsd if policy.hotel else 0)
    created = now_ms(now)
    proposal = Proposal(
        token=secrets.token_hex(5), user_id=who.user_id, conversation_id=who.conversation_id, status="proposed",
        created_at=created, expires_at=created + PROPOSAL_TTL_MS,
        payload={
            "flights": [f.to_dict() for f in policy.flights],
            "hotel": policy.hotel.to_dict() if policy.hotel else None,
            "totalUsd": total,
            "policy": {"verdict": policy.verdict, "reasons": policy.reasons},
        },
    )
    await repo.save_proposal(proposal)
    return CreateResult(True, proposal=proposal, warnings=policy.reasons)


@dataclass
class ActionResult:
    ok: bool
    message: str
    proposal: Proposal | None = None
    reason: str | None = None  # not_found | expired | already_booked | cancelled | superseded | blocked
    reference: str | None = None


def _stale(p: Proposal) -> ActionResult:
    table = {
        "confirmed": ("already_booked", f"Already booked (ref {reference(p.token)})."),
        "cancelled": ("cancelled", "This proposal was cancelled."),
        "superseded": ("superseded", "This proposal was replaced by a newer one."),
        "expired": ("expired", "This proposal expired. Ask me to search again."),
    }
    reason, message = table[p.status]
    return ActionResult(False, message, proposal=p, reason=reason)


_NOT_FOUND = ActionResult(False, "I could not find that proposal.", reason="not_found")


async def _load(repo: Repo, token: str, who: Who) -> Proposal | None:
    """Token is bound to the owning user AND conversation; anything else looks like "not found"."""
    p = await repo.get_proposal(token)
    return p if p and p.user_id == who.user_id and p.conversation_id == who.conversation_id else None


async def confirm_proposal(repo: Repo, token: str, who: Who, now: datetime, tz: str) -> ActionResult:
    p = await _load(repo, token, who)
    if not p:
        return _NOT_FOUND
    if p.status != "proposed":
        return _stale(p)
    if now_ms(now) > p.expires_at:
        await repo.transition_proposal(token, "proposed", "expired")
        return _stale(replace(p, status="expired"))
    flights: list[dict[str, Any]] = p.payload["flights"]
    hotel: dict[str, Any] | None = p.payload["hotel"]
    policy = check_policy([f["id"] for f in flights], hotel["id"] if hotel else None, now, tz)
    if policy.verdict == "block":
        return ActionResult(False, f"Policy no longer allows this: {' '.join(policy.reasons)}", proposal=p, reason="blocked")
    # Atomic: a double tap books exactly once.
    if not await repo.transition_proposal(token, "proposed", "confirmed"):
        current = await repo.get_proposal(token)
        assert current is not None
        return _stale(current)
    await repo.add_booking(Booking(f"bk-{token}", p.user_id, reference(token), token, p.payload, now_ms(now)))
    return ActionResult(True, f"Booked ✅ Reference {reference(token)}.", proposal=replace(p, status="confirmed"), reference=reference(token))


async def _transition(repo: Repo, token: str, who: Who, to: str, message: str) -> ActionResult:
    p = await _load(repo, token, who)
    if not p:
        return _NOT_FOUND
    if p.status != "proposed":
        return _stale(p)
    if not await repo.transition_proposal(token, "proposed", to):
        current = await repo.get_proposal(token)
        assert current is not None
        return _stale(current)
    return ActionResult(True, message, proposal=replace(p, status=to))


async def cancel_proposal(repo: Repo, token: str, who: Who) -> ActionResult:
    return await _transition(repo, token, who, "cancelled", "Cancelled. Nothing was booked.")


async def supersede_proposal(repo: Repo, token: str, who: Who) -> ActionResult:
    """'Change': the old card becomes Superseded and the next message re-searches."""
    return await _transition(repo, token, who, "superseded", "What would you like to change?")
