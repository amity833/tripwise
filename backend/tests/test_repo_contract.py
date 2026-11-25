"""The same contract runs against MemoryRepo and, when TEST_DATABASE_URL is set, a real Postgres (CI does this)."""

import asyncio
import os
import random
import string
import time

import pytest

from tripwise.core.models import Booking, Proposal
from tripwise.store.memory import MemoryRepo


def rnd(prefix: str = "") -> str:
    return prefix + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


async def _memory():
    yield MemoryRepo()


async def _postgres():
    from tripwise.store.postgres import PostgresRepo

    repo = await PostgresRepo.connect(os.environ["TEST_DATABASE_URL"])
    yield repo
    await repo.close()


_params = [pytest.param(_memory, id="MemoryRepo")]
if os.environ.get("TEST_DATABASE_URL"):
    _params.append(pytest.param(_postgres, id="PostgresRepo(real)"))


@pytest.fixture(params=_params)
async def repo(request):
    gen = request.param()
    r = await gen.__anext__()
    yield r
    await gen.aclose()


def prop(token: str, **over) -> Proposal:
    base = dict(token=token, user_id="u1", conversation_id="c1", status="proposed", payload={"totalUsd": 5}, expires_at=9, created_at=1)
    return Proposal(**{**base, **over})


async def test_users_conversations_latest_conversation(repo):
    u = rnd("u-")
    a = await repo.create_conversation(u)
    b = await repo.create_conversation(u)
    assert (await repo.get_conversation(a.id)).user_id == u
    assert (await repo.latest_conversation(u)).id == b.id
    assert await repo.get_conversation("nope") is None
    await repo.set_outage(a.id, True)
    assert (await repo.get_conversation(a.id)).outage_armed is True


async def test_switching_changes_the_latest_conversation_but_not_the_list_order(repo):
    u = rnd("u-")
    a = await repo.create_conversation(u)
    b = await repo.create_conversation(u)
    await repo.touch_conversation(a.id)
    assert (await repo.latest_conversation(u)).id == a.id
    assert [c.id for c in await repo.list_conversations(u)] == [a.id, b.id]


async def test_delete_conversation_removes_its_messages_and_proposals_only(repo):
    u = rnd("u-")
    a = await repo.create_conversation(u)
    b = await repo.create_conversation(u)
    ta, tb = rnd("t"), rnd("t")
    for c, t in ((a, ta), (b, tb)):
        await repo.append_messages(c.id, [{"role": "user", "text": "hi"}])
        await repo.save_proposal(prop(t, user_id=u, conversation_id=c.id))
    await repo.delete_conversation(a.id)
    assert await repo.get_conversation(a.id) is None
    assert await repo.get_messages(a.id) == [] and await repo.get_proposal(ta) is None
    assert len(await repo.get_messages(b.id)) == 1 and await repo.get_proposal(tb) is not None


async def test_user_slots_are_limited_and_free_up_after_idle_time(repo):
    # A far-future, ever-increasing clock keeps rows left in a shared database out of the count.
    now, idle = 10**16 + time.time_ns() // 1000, 1000
    a, b, c = rnd("u-"), rnd("u-"), rnd("u-")
    results = await asyncio.gather(*(repo.claim_seat(u, 2, idle, now) for u in (a, b, c)))
    assert sorted(results) == [False, True, True]
    holder, refused = (a, b, c)[results.index(True)], (a, b, c)[results.index(False)]
    assert await repo.claim_seat(holder, 2, idle, now + 900) is True
    assert await repo.claim_seat(refused, 2, idle, now + 1100) is True  # the other holder went idle
    assert (await repo.latest_conversation(holder)) is None


async def test_messages_keep_order_and_structure(repo):
    c = await repo.create_conversation(rnd("u-"))
    await repo.append_messages(c.id, [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "toolCalls": [{"id": "1", "name": "t", "args": {"a": 1}}]},
        {"role": "tool", "toolCallId": "1", "name": "t", "result": {"ok": True}},
    ])
    ms = await repo.get_messages(c.id)
    assert [m.message["role"] for m in ms] == ["user", "assistant", "tool"]
    assert ms[1].message["toolCalls"][0] == {"id": "1", "name": "t", "args": {"a": 1}}


async def test_proposals_transition_atomically(repo):
    t, u, c = rnd("t"), rnd("u"), rnd("c")
    await repo.save_proposal(prop(t, user_id=u, conversation_id=c))
    assert (await repo.latest_proposed(u, c)).token == t
    assert await repo.transition_proposal(t, "proposed", "confirmed") is True
    assert await repo.transition_proposal(t, "proposed", "cancelled") is False
    assert (await repo.get_proposal(t)).status == "confirmed"
    assert await repo.latest_proposed(u, c) is None


async def test_bookings_are_owned_by_the_user(repo):
    u = rnd("u-")
    await repo.add_booking(Booking(f"b{u}", u, "TW-1", f"p{u}", {"x": 1}, 1))
    assert len(await repo.list_bookings(u)) == 1
    assert await repo.list_bookings("other") == []


async def test_event_claim_claim_duplicate_complete_release(repo):
    e = rnd("e")
    assert await repo.claim_event("web", e) == "claimed"
    assert await repo.claim_event("web", e) == "duplicate"
    await repo.complete_event("web", e, {"text": "hi"})
    assert await repo.get_event_result("web", e) == {"text": "hi"}
    assert await repo.claim_event("web", e) == "duplicate"
    e2 = rnd("e")
    await repo.claim_event("web", e2)
    await repo.release_event("web", e2)
    assert await repo.claim_event("web", e2) == "claimed"
    assert await repo.claim_event("telegram", e) == "claimed"  # channels are separate namespaces


async def test_a_proposal_is_unshown_until_marked(repo):
    t = rnd("t")
    await repo.save_proposal(prop(t))
    assert (await repo.get_proposal(t)).shown is False
    await repo.mark_proposal_shown(t)
    assert (await repo.get_proposal(t)).shown is True
