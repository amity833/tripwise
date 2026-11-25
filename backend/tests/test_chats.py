"""Chat history (switch, delete, limit) and the user-slot limit."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_channels import FakeTg, msg, no_sleep

from tripwise.app import build_service
from tripwise.channels.telegram.adapter import TelegramAdapter
from tripwise.config import ConfigError, load_config
from tripwise.core.conversation_service import ConversationLimitReached, ConversationNotFound, ConversationService, DemoFull
from tripwise.core.models import Booking, Ctx
from tripwise.llm.base import LlmResponse
from tripwise.llm.dev_fake import DevFakeLlm
from tripwise.llm.scripted import ScriptedLlm
from tripwise.server import create_app
from tripwise.store.memory import MemoryRepo
from tripwise.tools import default_tools

U = "web:u"


def setup(**limits):
    llm = ScriptedLlm([], LlmResponse("ok"))
    repo = MemoryRepo()
    return repo, llm, ConversationService(repo, llm, default_tools(), tz="UTC", **limits)


async def chat(svc: ConversationService, user: str, text: str) -> str:
    """Starts a new chat, says one thing in it, returns its id."""
    c = await svc.start_conversation(user)
    await svc.handle_message(Ctx(user, c.id, "r"), text, "test")
    return c.id


async def test_sixth_chat_is_refused_until_one_is_deleted():
    _, _, svc = setup()
    ids = [await chat(svc, U, f"trip {i}") for i in range(5)]
    with pytest.raises(ConversationLimitReached):
        await svc.start_conversation(U)
    assert len(await svc.list_conversations(U)) == 5
    await svc.delete_conversation(U, ids[0])
    assert (await svc.start_conversation(U)).id not in ids
    assert await chat(svc, "web:other", "my own limit")  # the limit is per user


async def test_an_empty_chat_is_reused_and_concurrent_requests_cannot_pass_the_limit():
    _, _, svc = setup()
    first = await svc.start_conversation(U)
    assert (await svc.start_conversation(U)).id == first.id
    results = await asyncio.gather(*(svc.start_conversation(U) for _ in range(10)))
    assert {c.id for c in results} == {first.id}
    assert len(await svc.list_conversations(U)) == 1


async def test_going_back_to_an_earlier_chat_restores_its_context_only():
    _, llm, svc = setup()
    berlin = await chat(svc, U, "Berlin please")
    await chat(svc, U, "London please")
    await svc.open_conversation(U, berlin)
    current = await svc.current_conversation(U)
    assert current.id == berlin
    await svc.handle_message(Ctx(U, current.id, "r"), "and a hotel", "test")
    sent = str(llm.requests[-1].messages)
    assert "Berlin please" in sent and "London" not in sent
    assert [(c.title, c.active) for c in await svc.list_conversations(U)] == [("Berlin please", True), ("London please", False)]


async def test_titles_are_shortened_and_empty_chats_are_labelled():
    _, _, svc = setup()
    assert [c.title for c in await svc.list_conversations(U)] == ["New chat"]
    await chat(svc, U, "I need   to fly to Berlin from Tel Aviv next month for a conference")
    title = (await svc.list_conversations(U))[0].title
    assert len(title) <= 40 and title.startswith("I need to fly") and title.endswith("…")


async def test_delete_removes_chat_data_keeps_bookings_and_is_owner_only():
    repo, _, svc = setup()
    a = await chat(svc, U, "one")
    b = await chat(svc, U, "two")
    await repo.add_booking(Booking("b1", U, "TW-1", "tok", {"totalUsd": 1}, 1))
    with pytest.raises(ConversationNotFound):
        await svc.delete_conversation("web:intruder", a)
    with pytest.raises(ConversationNotFound):
        await svc.open_conversation("web:intruder", a)
    await svc.delete_conversation(U, b)  # the current one
    assert (await svc.current_conversation(U)).id == a
    assert await repo.get_messages(b) == []
    assert len(await svc.trips(U)) == 1
    await svc.delete_conversation(U, a)  # the last one: a fresh empty chat takes its place
    assert [c.title for c in await svc.list_conversations(U)] == ["New chat"]


async def test_delete_waits_for_the_message_in_progress():
    repo, llm, svc = setup()
    c = await svc.current_conversation(U)
    gate = asyncio.Event()

    async def slow(_req):
        await gate.wait()
        return LlmResponse("late")

    llm.generate = slow  # type: ignore[method-assign]
    sending = asyncio.create_task(svc.handle_message(Ctx(U, c.id, "r"), "hi", "test"))
    await asyncio.sleep(0.01)
    deleting = asyncio.create_task(svc.delete_conversation(U, c.id))
    await asyncio.sleep(0.01)
    assert not deleting.done()
    gate.set()
    assert (await sending).text == "late"
    await deleting
    assert await repo.get_messages(c.id) == []  # nothing left behind


async def test_sixth_user_is_refused_until_someone_is_idle_for_a_day_and_nothing_is_lost():
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    repo = MemoryRepo()
    svc = ConversationService(repo, ScriptedLlm([], LlmResponse("ok")), default_tools(), now=lambda: clock[0], max_users=5)
    for i in range(5):
        await svc.admit(f"web:{i}")
    await chat(svc, "web:0", "keep me")
    with pytest.raises(DemoFull):
        await svc.admit("web:new")
    await svc.admit("web:0")  # existing users are unaffected
    assert await repo.latest_conversation("web:new") is None  # a refused visitor stores nothing

    clock[0] += timedelta(hours=23)
    for i in range(1, 5):
        await svc.admit(f"web:{i}")
    with pytest.raises(DemoFull):
        await svc.admit("web:new")

    clock[0] += timedelta(hours=2)  # only web:0 has now been idle for more than 24 hours
    await svc.admit("web:new")
    with pytest.raises(DemoFull):
        await svc.admit("web:0")
    clock[0] += timedelta(hours=25)
    await svc.admit("web:0")
    assert [c.title for c in await svc.list_conversations("web:0")] == ["keep me"]


def test_limits_are_configurable_and_validated():
    c = load_config({})
    assert (c.max_users, c.max_conversations) == (10, 5)
    assert load_config({"MAX_USERS": "20"}).max_users == 20
    with pytest.raises(ConfigError, match="MAX_USERS"):
        load_config({"MAX_USERS": "0"})


async def test_web_api_lists_switches_deletes_and_reports_both_limits():
    config = load_config({"MAX_USERS": "1", "MAX_CONVERSATIONS": "2"})
    repo = MemoryRepo()
    service = build_service(config, repo, DevFakeLlm("UTC"))
    app = create_app(config, service, repo)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        h = {"x-session-id": "session-aaaaaaaaaaaaaaaa"}

        async def post(path, body=None):
            return await client.post(path, json=body, headers=h)

        first = (await client.get("/api/conversation", headers=h)).json()["conversationId"]
        await post("/api/chat", {"conversationId": first, "messageId": "m-chat-0001", "text": "first chat"})
        second = (await post("/api/conversation/new")).json()["conversationId"]
        await post("/api/chat", {"conversationId": second, "messageId": "m-chat-0002", "text": "second chat"})

        full = await post("/api/conversation/new")
        assert full.status_code == 409 and "Delete one" in full.json()["error"]

        opened = (await post("/api/conversation/open", {"conversationId": first})).json()
        assert opened["messages"][0]["text"] == "first chat"
        assert (await client.get("/api/conversation", headers=h)).json()["conversationId"] == first

        listing = (await client.get("/api/conversations", headers=h)).json()
        assert listing["limit"] == 2
        assert [(c["title"], c["active"]) for c in listing["conversations"]] == [("first chat", True), ("second chat", False)]

        after = (await post("/api/conversation/delete", {"conversationId": first})).json()
        assert after["conversationId"] == second
        assert (await post("/api/conversation/open", {"conversationId": first})).status_code == 404

        other = await client.get("/api/conversation", headers={"x-session-id": "session-bbbbbbbbbbbbbbbb"})
        assert other.status_code == 503 and "full" in other.json()["error"]


def tg_press(update_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {"id": str(update_id), "from": {"id": 7}, "data": data, "message": {"message_id": 55, "chat": {"id": 7, "type": "private"}}},
    }


async def test_telegram_chat_list_switch_confirmed_delete_and_limit():
    repo, api = MemoryRepo(), FakeTg()
    service = build_service(load_config({"MAX_CONVERSATIONS": "2"}), repo, ScriptedLlm([], LlmResponse("noted")))
    tg = TelegramAdapter(api, service, repo, "s", sleep=no_sleep)
    for i, text in enumerate(["Berlin trip", "/new", "London trip", "/new"], start=1):
        await tg.handle_update(msg(i, text))
    assert "Delete one" in api.sent()[-2]["text"]
    rows = api.sent()[-1]["reply_markup"]["inline_keyboard"]
    assert [r[0]["text"] for r in rows] == ["Berlin trip", "● London trip"]
    assert all(len(b["callback_data"].encode()) <= 64 for r in rows for b in r)

    await tg.handle_update(tg_press(10, rows[0][0]["callback_data"]))
    assert api.sent()[-1]["text"].startswith("Switched to: Berlin trip")
    assert (await service.list_conversations("telegram:7"))[0].active

    await tg.handle_update(tg_press(11, rows[1][1]["callback_data"]))  # ask
    assert len(await service.list_conversations("telegram:7")) == 2  # nothing deleted yet
    confirm = api.sent("editMessageText")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await tg.handle_update(tg_press(12, confirm))
    assert [c.title for c in await service.list_conversations("telegram:7")] == ["Berlin trip"]
    await tg.handle_update(tg_press(13, confirm))
    assert "no longer exists" in api.sent("answerCallbackQuery")[-1]["text"]


async def test_telegram_button_on_an_older_chats_card_still_works():
    repo, api = MemoryRepo(), FakeTg()
    service = build_service(load_config({}), repo, DevFakeLlm("UTC"))
    tg = TelegramAdapter(api, service, repo, "s", sleep=no_sleep)
    await tg.handle_update(msg(1, "flights to Berlin from Tel Aviv"))
    await tg.handle_update(msg(2, "book the second one"))
    confirm = api.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    old = (await service.current_conversation("telegram:7")).id
    await tg.handle_update(msg(3, "/new"))
    await tg.handle_update(tg_press(10, confirm))
    assert len(await service.trips("telegram:7")) == 1
    assert (await service.current_conversation("telegram:7")).id != old  # confirming does not switch chats
    assert "Booked" in (await service.history("telegram:7", old)).messages[-1]["text"]


async def test_telegram_sixth_user_gets_a_clear_message():
    repo, api = MemoryRepo(), FakeTg()
    llm = ScriptedLlm([], LlmResponse("ok"))
    service = build_service(load_config({"MAX_USERS": "1"}), repo, llm)
    await service.admit("web:someone")
    tg = TelegramAdapter(api, service, repo, "s", sleep=no_sleep)
    assert await tg.handle_update(msg(1, "hi")) == "processed"
    await tg.handle_update(tg_press(2, "c:abcdef0123"))
    assert "full" in api.sent()[-1]["text"] and "full" in api.sent("answerCallbackQuery")[-1]["text"]
    assert llm.requests == []
