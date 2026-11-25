import asyncio
from dataclasses import replace

import pytest
from pydantic import BaseModel

from tripwise.config import ConfigError, load_config
from tripwise.core.agent import ITERATION_CAP_MESSAGE, MAX_ITERATIONS
from tripwise.core.conversation_service import ConversationNotFound, ConversationService
from tripwise.core.models import Booking, Ctx
from tripwise.llm.base import LlmError, LlmRequest, LlmResponse
from tripwise.llm.scripted import ScriptedLlm
from tripwise.store.memory import MemoryRepo
from tripwise.tools.definitions import list_bookings_tool
from tripwise.tools.registry import Tool, ToolRegistry


class EchoArgs(BaseModel):
    q: str


async def _echo(a: EchoArgs, _tc):
    return {"echoed": a.q}


echo = Tool("echo", "echo", EchoArgs, {"type": "object"}, _echo)


class S:
    def __init__(self, llm: ScriptedLlm):
        self.repo = MemoryRepo()
        self.llm = llm
        self.svc = ConversationService(self.repo, llm, ToolRegistry([echo, list_bookings_tool]))

    async def mk(self, user_id: str) -> Ctx:
        c = await self.svc.start_conversation(user_id)
        return Ctx(user_id, c.id, "r1")

    def say(self, ctx: Ctx, text: str, event_id: str | None = None):
        return self.svc.handle_message(ctx, text, "test", event_id)


def call(id_, name, args):
    return {"id": id_, "name": name, "args": args}


async def texts(s: S, user: str, ctx: Ctx):
    return [m["text"] for m in (await s.svc.history(user, ctx.conversation_id)).messages]


async def test_1_isolates_users_and_conversations():
    s = S(ScriptedLlm([LlmResponse("hi A"), LlmResponse("hi B")]))
    a, b = await s.mk("web:a"), await s.mk("web:b")
    await s.say(a, "secret from A")
    await s.say(b, "hello from B")
    assert await texts(s, "web:a", a) == ["secret from A", "hi A"]
    with pytest.raises(ConversationNotFound):
        await s.svc.history("web:b", a.conversation_id)
    with pytest.raises(ConversationNotFound):
        await s.say(replace(b, conversation_id=a.conversation_id), "hijack")
    assert "secret from A" not in str(s.llm.requests[1])


async def test_2_follow_up_includes_prior_turn_and_tool_results():
    llm = ScriptedLlm([
        LlmResponse(tool_calls=[call("t1", "echo", {"q": "flight-2"})]),
        LlmResponse("Found flight-2"),
        LlmResponse("Booking the second one"),
    ])
    s = S(llm)
    ctx = await s.mk("web:a")
    await s.say(ctx, "find flights")
    await s.say(ctx, "book the second one")
    third = llm.requests[2].messages
    assert any(m["role"] == "tool" and "flight-2" in str(m["result"]) for m in third)
    # UI history stays text-only
    assert await texts(s, "web:a", ctx) == ["find flights", "Found flight-2", "book the second one", "Booking the second one"]


async def test_3_a_new_chat_keeps_the_users_bookings():
    llm = ScriptedLlm([LlmResponse(tool_calls=[call("t", "list_bookings", {})]), LlmResponse("ok")])
    s = S(llm)
    first = await s.mk("tg:1")
    await s.repo.add_booking(Booking("b1", "tg:1", "TW-1", "x", {"to": "BER"}, 1))
    second = await s.svc.start_conversation("tg:1")
    await s.say(replace(first, conversation_id=second.id), "my trips")
    tool_msg = next(m for m in llm.requests[1].messages if m["role"] == "tool")
    assert "TW-1" in str(tool_msg)


async def test_4_processes_rapid_messages_serially_in_order():
    order: list[str] = []

    def fallback(req: LlmRequest):
        last = req.messages[-1]
        order.append(last["text"] if last["role"] == "user" else "?")
        return LlmResponse("ok")

    llm = ScriptedLlm([], fallback)
    inner = llm.generate
    first = True

    async def slow(req):
        nonlocal first
        if first:
            first = False
            await asyncio.sleep(0.03)
        return await inner(req)

    llm.generate = slow  # type: ignore[method-assign]
    s = S(llm)
    ctx = await s.mk("web:a")
    await asyncio.gather(s.say(ctx, "one"), s.say(ctx, "two"))
    assert order == ["one", "two"]


async def test_5_iteration_cap_returns_friendly_message_and_keeps_transcript_consistent():
    llm = ScriptedLlm([], LlmResponse(tool_calls=[call("x", "echo", {"q": "loop"})]))
    s = S(llm)
    ctx = await s.mk("web:a")
    r = await s.say(ctx, "go")
    assert r.text == ITERATION_CAP_MESSAGE
    assert len(llm.requests) == MAX_ITERATIONS
    last = (await s.repo.get_messages(ctx.conversation_id))[-1].message
    assert last == {"role": "assistant", "text": ITERATION_CAP_MESSAGE}


async def test_6_llm_failure_gives_friendly_error_with_ref_and_next_message_works():
    s = S(ScriptedLlm([LlmError("server", "boom"), LlmResponse("back")]))
    ctx = await s.mk("web:a")
    bad = await s.say(ctx, "hi")
    assert "try again" in bad.text.lower() and "ref r1" in bad.text
    assert (await s.say(ctx, "again")).text == "back"
    assert (await s.repo.get_messages(ctx.conversation_id))[0].message == {"role": "user", "text": "hi"}


async def test_7_tool_errors_reach_the_llm_as_results():
    llm = ScriptedLlm([
        LlmResponse(tool_calls=[call("a", "echo", {"q": 5}), call("b", "nope", {})]),
        LlmResponse("sorry"),
    ])
    s = S(llm)
    ctx = await s.mk("web:a")
    assert (await s.say(ctx, "x")).text == "sorry"
    results = [str(m["result"]) for m in llm.requests[1].messages if m["role"] == "tool"]
    assert "Invalid arguments" in results[0]
    assert "Unknown tool" in results[1]


async def test_9_empty_and_overlong_input_get_clear_replies_without_calling_the_llm():
    s = S(ScriptedLlm())
    ctx = await s.mk("web:a")
    assert "type a message" in (await s.say(ctx, "   ")).text.lower()
    assert "too long" in (await s.say(ctx, "x" * 2001)).text.lower()
    assert len(s.llm.requests) == 0


async def test_10_outage_affects_only_its_conversation_and_clears_after_one_failure():
    s = S(ScriptedLlm([], LlmResponse("fine")))
    a, b = await s.mk("web:a"), await s.mk("web:b")
    await s.svc.simulate_outage("web:a", a.conversation_id)
    assert (await s.say(b, "hi")).text == "fine"
    assert "trouble" in (await s.say(a, "hi")).text.lower()
    assert (await s.say(a, "hi again")).text == "fine"


async def test_11_duplicate_events_run_once_and_a_failed_event_releases_its_claim():
    llm = ScriptedLlm([Exception("crash"), LlmResponse("done")], LlmResponse("extra"))
    s = S(llm)
    ctx = await s.mk("tg:1")
    r1 = await s.say(ctx, "hi", "u1")
    r2 = await s.say(ctx, "hi", "u1")
    assert r2 == r1
    assert len(llm.requests) == 1

    original = s.repo.append_messages
    fail = True

    async def flaky(*a, **k):
        nonlocal fail
        if fail:
            fail = False
            raise RuntimeError("db down")
        return await original(*a, **k)

    s.repo.append_messages = flaky  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="db down"):
        await s.say(ctx, "x", "u2")
    assert (await s.say(ctx, "x", "u2")).text  # retry is processed, not ignored


def test_config_fails_fast_with_every_problem_listed():
    with pytest.raises(ConfigError, match=r"GEMINI_API_KEY[\s\S]*TELEGRAM_WEBHOOK_SECRET"):
        load_config({"LLM_PROVIDER": "gemini", "PUBLIC_URL": "https://x.com"})
    assert load_config({}).llm_provider == "fake"
    assert load_config({"PUBLIC_URL": "", "DATABASE_URL": "", "PORT": ""}).port == 3000
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_config({"DATABASE_URL": "mysql://x"})
