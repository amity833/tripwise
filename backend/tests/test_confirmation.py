"""Typed confirmation, trip summaries and error wording."""

import pytest
from test_channels import msg, tg_setup

from tripwise.core.conversation_service import REVIEW_FIRST, ConversationService
from tripwise.core.models import Ctx
from tripwise.core.proposals import create_proposal
from tripwise.data.dates import add_days, today_iso, utcnow
from tripwise.data.flights import search_flights
from tripwise.llm.base import LlmError, LlmResponse
from tripwise.llm.scripted import ScriptedLlm
from tripwise.store.memory import MemoryRepo
from tripwise.tools import default_tools

FLIGHT = search_flights("TLV", "BER", add_days(today_iso(utcnow(), "UTC"), 20), "economy")[0]


def propose_call() -> LlmResponse:
    return LlmResponse(tool_calls=[{"id": "p", "name": "propose_booking", "args": {"flight_id": FLIGHT.id}}])


async def setup(llm: ScriptedLlm):
    repo = MemoryRepo()
    svc = ConversationService(repo, llm, default_tools())
    c = await svc.start_conversation("web:a")
    return repo, svc, Ctx("web:a", c.id, "r1")


@pytest.mark.parametrize("word", ["ok", "okay", "OK!"])
async def test_an_acknowledgement_never_books(word):
    _, svc, ctx = await setup(ScriptedLlm([propose_call(), LlmResponse("Please confirm."), LlmResponse("Sure.")]))
    p = (await svc.handle_message(ctx, "book it please", "test")).proposal
    await svc.mark_shown(ctx.user_id, ctx.conversation_id, p.token)
    await svc.handle_message(ctx, word, "test")
    assert await svc.trips("web:a") == []


async def test_yes_after_a_failed_reply_shows_the_card_before_it_can_book():
    _, svc, ctx = await setup(ScriptedLlm([propose_call(), LlmError("server", "boom")]))
    assert (await svc.handle_message(ctx, "book it please", "test")).proposal is None
    first = await svc.handle_message(ctx, "yes", "test")
    assert first.text == REVIEW_FIRST and first.proposal.status == "proposed"
    assert await svc.trips("web:a") == []
    await svc.mark_shown(ctx.user_id, ctx.conversation_id, first.proposal.token)
    assert (await svc.handle_message(ctx, "yes", "test")).proposal.status == "confirmed"
    assert len(await svc.trips("web:a")) == 1


async def test_another_user_cannot_mark_a_card_as_shown():
    repo, svc, ctx = await setup(ScriptedLlm([propose_call(), LlmResponse("Please confirm.")]))
    p = (await svc.handle_message(ctx, "book it please", "test")).proposal
    await svc.mark_shown("web:b", ctx.conversation_id, p.token)
    assert (await repo.get_proposal(p.token)).shown is False


async def test_telegram_yes_does_not_book_a_card_that_failed_to_send():
    repo, api, service, tg, _ = tg_setup(ScriptedLlm([propose_call(), LlmResponse("Please confirm.")]))
    api.failures["sendMessage"] = 99
    await tg.handle_update(msg(1, "book it please"))
    api.failures.clear()
    await tg.handle_update(msg(2, "yes"))
    assert await service.trips("telegram:7") == []
    assert "PROPOSED" in api.sent()[-1]["text"]
    await tg.handle_update(msg(3, "yes"))
    assert len(await service.trips("telegram:7")) == 1


async def test_telegram_trips_show_destination_and_dates():
    repo, api, service, tg, _ = tg_setup()
    conv = await service.current_conversation("telegram:7")
    who = Ctx("telegram:7", conv.id, "r")
    p = (await create_proposal(repo, who, FLIGHT.id, None, None, utcnow(), "UTC")).proposal
    await service.handle_action(who, p.token, "confirm")
    await tg.handle_update(msg(1, "/trips"))
    text = api.sent()[-1]["text"]
    assert "to Berlin" in text and "Tel Aviv → Berlin" in text and FLIGHT.departTime in text and FLIGHT.airline in text


async def test_an_internal_error_is_not_blamed_on_the_ai_provider():
    _, svc, ctx = await setup(ScriptedLlm([RuntimeError("db down")]))
    reply = await svc.handle_message(ctx, "hi", "test")
    assert "our side" in reply.text and "AI provider" not in reply.text and "ref r1" in reply.text
    assert "db down" not in reply.text
