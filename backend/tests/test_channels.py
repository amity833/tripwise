from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from tripwise.app import build_service
from tripwise.channels.telegram.adapter import TelegramAdapter
from tripwise.channels.telegram.api import TelegramApiError
from tripwise.channels.telegram.format import escape_html, split_message, to_telegram_html
from tripwise.config import load_config
from tripwise.core.conversation_service import ConversationService
from tripwise.core.models import Booking
from tripwise.data.dates import add_days, today_iso
from tripwise.data.flights import search_flights
from tripwise.llm.base import LlmResponse
from tripwise.llm.dev_fake import DevFakeLlm
from tripwise.llm.scripted import ScriptedLlm
from tripwise.server import create_app
from tripwise.store.memory import MemoryRepo
from tripwise.tools import default_tools


class FakeTg:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, int] = {}

    async def call(self, method: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, body or {}))
        if self.failures.get(method, 0) > 0:
            self.failures[method] -= 1
            raise TelegramApiError(method, 500, "boom")
        return {}

    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [b for m, b in self.calls if m == method]


async def no_sleep(_s: float) -> None:
    return None


def msg(update_id: int, text: str | None, **extra: Any) -> dict[str, Any]:
    m: dict[str, Any] = {"message_id": update_id, "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, **extra}
    if text is not None:
        m["text"] = text
    return {"update_id": update_id, "message": m}


def tg_setup(llm: ScriptedLlm | None = None):
    llm = llm or ScriptedLlm([], LlmResponse("hello **there** <b>x</b>"))
    repo, api = MemoryRepo(), FakeTg()
    service = ConversationService(repo, llm, default_tools(), tz="UTC")
    tg = TelegramAdapter(api, service, repo, "s3cret", sleep=no_sleep)
    return repo, api, service, tg, llm


class TestTelegramAdapter:
    async def test_11_duplicate_update_is_processed_once(self):
        _, _, _, tg, llm = tg_setup()
        assert await tg.handle_update(msg(1, "hi")) == "processed"
        assert await tg.handle_update(msg(1, "hi")) == "duplicate"
        assert len(llm.requests) == 1

    async def test_escapes_llm_markup_and_keeps_bold(self):
        _, api, _, tg, _ = tg_setup()
        await tg.handle_update(msg(1, "hi"))
        assert api.sent()[-1]["text"] == "hello <b>there</b> &lt;b&gt;x&lt;/b&gt;"

    async def test_8_delivery_failure_is_retried_once_and_the_reply_is_persisted(self):
        _, api, service, tg, _ = tg_setup()
        api.failures["sendMessage"] = 1
        assert await tg.handle_update(msg(1, "hi")) == "processed"
        assert len(api.sent()) == 2  # failed once, retried once
        conv = await service.current_conversation("telegram:7")
        assert "hello" in (await service.history("telegram:7", conv.id)).messages[-1]["text"]

    async def test_a_total_send_failure_never_crashes_the_update(self):
        _, api, _, tg, _ = tg_setup()
        api.failures["sendMessage"] = 99
        assert await tg.handle_update(msg(1, "hi")) == "processed"

    async def test_falls_back_to_plain_text_on_a_parse_error(self):
        _, api, _, tg, _ = tg_setup()
        orig = api.call

        async def call(method, body=None):
            if method == "sendMessage" and (body or {}).get("parse_mode"):
                api.calls.append((method, body))
                raise TelegramApiError(method, 400, "Bad Request: can't parse entities")
            return await orig(method, body)

        api.call = call  # type: ignore[method-assign]
        await tg.handle_update(msg(1, "hi"))
        assert "parse_mode" not in api.sent()[-1]

    async def test_groups_non_text_edits_and_unknown_commands_get_polite_replies(self):
        _, api, _, tg, llm = tg_setup()
        await tg.handle_update({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 7, "type": "group"}, "from": {"id": 7}, "text": "hi"}})
        await tg.handle_update(msg(2, None, photo=[{}]))
        await tg.handle_update({"update_id": 3, "edited_message": {"message_id": 1, "chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "text": "x"}})
        await tg.handle_update(msg(4, "/bogus"))
        t = [b["text"] for b in api.sent()]
        assert "direct messages" in t[0] and "text messages" in t[1] and "edited" in t[2] and "Unknown command" in t[3]
        assert len(llm.requests) == 0

    async def test_failed_processing_releases_the_claim_so_the_retry_is_processed(self):
        repo, _, _, tg, _ = tg_setup()
        orig, fail = repo.append_messages, True

        async def flaky(*a, **k):
            nonlocal fail
            if fail:
                fail = False
                raise RuntimeError("db")
            return await orig(*a, **k)

        repo.append_messages = flaky  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="db"):
            await tg.handle_update(msg(1, "hi"))
        assert await tg.handle_update(msg(1, "hi")) == "processed"

    async def test_new_keeps_trips_and_simulate_outage_hits_the_next_message_once(self):
        repo, api, _, tg, _ = tg_setup()
        await repo.add_booking(Booking("b", "telegram:7", "TW-ABC", "t", {"totalUsd": 10}, 1))
        await tg.handle_update(msg(1, "/new"))
        await tg.handle_update(msg(2, "/trips"))
        assert "TW-ABC" in api.sent()[-1]["text"]
        for i, t in enumerate(["/simulate_outage", "hi", "hi again"], start=3):
            await tg.handle_update(msg(i, t))
        t = [b["text"] for b in api.sent()]
        assert "trouble" in t[-2] and "hello" in t[-1]

    async def test_full_flow_with_buttons(self):
        repo, api = MemoryRepo(), FakeTg()
        service = build_service(load_config({}), repo)
        tg = TelegramAdapter(api, service, repo, "s", sleep=no_sleep)
        d = add_days(today_iso(datetime.now(UTC), "UTC"), 20)
        await tg.handle_update(msg(1, f"I need flights to Berlin from Tel Aviv {d} {add_days(d, 3)}"))
        await tg.handle_update(msg(2, "book the second one"))
        card = api.sent()[-1]
        assert "PROPOSED" in card["text"]
        buttons = card["reply_markup"]["inline_keyboard"][0]
        assert len(buttons) == 3 and len(buttons[0]["callback_data"]) <= 64

        def press(cid: str, data: str):
            return tg.handle_update({
                "update_id": 100 + int(cid),
                "callback_query": {"id": cid, "from": {"id": 7}, "data": data, "message": {"message_id": 55, "chat": {"id": 7, "type": "private"}}},
            })

        await press("1", buttons[0]["callback_data"])
        await press("2", buttons[0]["callback_data"])
        edits = api.sent("editMessageText")
        assert "BOOKED" in edits[0]["text"] and edits[0]["reply_markup"]["inline_keyboard"] == []
        assert "BOOKED" in edits[1]["text"]  # already booked: card refreshed, not an error
        toasts = [b["text"] for b in api.sent("answerCallbackQuery")]
        assert "Booked" in toasts[0] and "Already booked" in toasts[1]
        assert len(await service.trips("telegram:7")) == 1
        await press("3", "garbage")
        assert "no longer valid" in api.sent("answerCallbackQuery")[-1]["text"]

    async def test_webhook_rejects_a_wrong_secret_and_accepts_the_right_one(self):
        repo, _, service, tg, llm = tg_setup()
        app = create_app(load_config({}), service, repo, webhook=tg.webhook)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            body = msg(1, "hi")
            assert (await c.post("/telegram/webhook", json=body)).status_code == 401
            assert (await c.post("/telegram/webhook", json=body, headers={"x-telegram-bot-api-secret-token": "nope"})).status_code == 401
            ok = await c.post("/telegram/webhook", json=body, headers={"x-telegram-bot-api-secret-token": "s3cret"})
            assert ok.status_code == 200
            junk = await c.post("/telegram/webhook", content=b"not json", headers={"x-telegram-bot-api-secret-token": "s3cret"})
            assert junk.status_code == 200  # malformed: acknowledged, never retried forever
        assert len(llm.requests) == 1


class TestTelegramFormatting:
    def test_escapes_html(self):
        assert escape_html('<a href="x">&</a>') == '&lt;a href="x"&gt;&amp;&lt;/a&gt;'

    def test_splits_long_text_under_the_limit_without_losing_content(self):
        text = ("word " * 50 + "\n") * 200
        parts = split_message(text)
        assert len(parts) > 1
        assert all(len(to_telegram_html(p)) <= 4096 for p in parts)
        assert " ".join(" ".join(parts).split()) == " ".join(text.split())

    def test_never_cuts_an_emoji_pair_or_a_bold_tag_even_with_heavy_escaping(self):
        text = "😀" * 5000
        assert "".join(split_message(text)) == text
        for p in split_message("**&**" * 3000):
            h = to_telegram_html(p)
            assert len(h.encode("utf-16-le")) // 2 <= 4096
            assert h.count("<b>") == h.count("</b>")


class TestWebApi:
    A, B = "session-aaaaaaaaaaaaaaaa", "session-bbbbbbbbbbbbbbbb"

    @pytest.fixture
    async def web(self):
        repo = MemoryRepo()
        service = build_service(load_config({}), repo, DevFakeLlm("UTC"))
        app = create_app(load_config({}), service, repo)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:

            async def call(sid, method, path, body=None):
                r = await client.request(method, path, headers={"x-session-id": sid} if sid else {}, json=body)
                return r.status_code, r.json()

            yield call, service, repo, app

    async def test_18_chat_then_history_sessions_isolated_header_required(self, web):
        call, *_ = web
        assert (await call(None, "GET", "/api/conversation"))[0] == 401
        conv = (await call(self.A, "GET", "/api/conversation"))[1]["conversationId"]
        status, body = await call(self.A, "POST", "/api/chat", {"conversationId": conv, "messageId": "m-0001-aaaa", "text": "hello"})
        assert status == 200 and body["reply"]["text"]
        h = (await call(self.A, "GET", "/api/conversation"))[1]
        assert [m["role"] for m in h["messages"]] == ["user", "assistant"]
        assert (await call(self.B, "POST", "/api/chat", {"conversationId": conv, "messageId": "m-0002-bbbb", "text": "steal"}))[0] == 404
        assert (await call(self.B, "GET", "/api/conversation"))[1]["messages"] == []
        assert (await call(self.A, "POST", "/api/chat", {"conversationId": conv}))[0] == 400

    async def test_12_same_client_message_id_is_processed_once(self):
        llm = ScriptedLlm([LlmResponse("first")], LlmResponse("SHOULD NOT RUN"))
        repo = MemoryRepo()
        service = build_service(load_config({}), repo, llm)
        app = create_app(load_config({}), service, repo)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = {"x-session-id": self.A}
            conv = (await c.get("/api/conversation", headers=h)).json()["conversationId"]
            body = {"conversationId": conv, "messageId": "m-0003-cccc", "text": "hi"}
            a = (await c.post("/api/chat", json=body, headers=h)).json()
            b = (await c.post("/api/chat", json=body, headers=h)).json()
        assert a["reply"]["text"] == "first" and b["reply"]["text"] == "first"
        assert len(llm.requests) == 1

    async def test_outage_then_recovery_booking_flow_and_trips_survive_new_chat(self, web):
        call, *_ = web
        A = self.A
        conv = (await call(A, "GET", "/api/conversation"))[1]["conversationId"]
        await call(A, "POST", "/api/simulate-outage", {"conversationId": conv})
        bad = (await call(A, "POST", "/api/chat", {"conversationId": conv, "messageId": "m-0004-dddd", "text": "hi"}))[1]
        assert "trouble" in bad["reply"]["text"] and "ref" in bad["reply"]["text"]
        d = add_days(today_iso(datetime.now(UTC), "UTC"), 20)
        await call(A, "POST", "/api/chat", {"conversationId": conv, "messageId": "m-0005-eeee", "text": f"flights to Berlin {d} {add_days(d, 2)}"})
        prop = (await call(A, "POST", "/api/chat", {"conversationId": conv, "messageId": "m-0006-ffff", "text": "book the first one"}))[1]["reply"]["proposal"]
        assert prop["status"] == "proposed"
        yes = (await call(A, "POST", "/api/chat", {"conversationId": conv, "messageId": "m-0007-gggg", "text": "yes", "seenProposal": prop["token"]}))[1]
        assert yes["reply"]["proposal"]["status"] == "confirmed"
        fresh = (await call(A, "POST", "/api/conversation/new"))[1]["conversationId"]
        assert fresh != conv
        assert len((await call(A, "GET", "/api/trips"))[1]["trips"]) == 1
        assert len((await call(self.B, "GET", "/api/trips"))[1]["trips"]) == 0

    async def test_change_supersedes_the_card_via_the_action_endpoint(self, web):
        call, *_ = web
        A = self.A
        conv = (await call(A, "GET", "/api/conversation"))[1]["conversationId"]
        d = add_days(today_iso(datetime.now(UTC), "UTC"), 20)
        await call(A, "POST", "/api/chat", {"conversationId": conv, "messageId": "m-0008-hhhh", "text": f"flights to Berlin {d} {add_days(d, 2)}"})
        p = (await call(A, "POST", "/api/chat", {"conversationId": conv, "messageId": "m-0009-iiii", "text": "book the second one"}))[1]["reply"]["proposal"]
        ch = (await call(A, "POST", "/api/action", {"conversationId": conv, "token": p["token"], "action": "change"}))[1]
        assert ch["reply"]["proposal"]["status"] == "superseded"
        stale = (await call(A, "POST", "/api/action", {"conversationId": conv, "token": p["token"], "action": "confirm"}))[1]
        assert stale["ok"] is False

    async def test_health_and_unknown_api_routes(self, web):
        call, _, _, app = web
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = (await c.get("/health")).json()
        assert h == {"status": "ok", "store": "memory", "llm": "fake", "telegram": False}
        assert (await call(self.A, "GET", "/api/nope"))[0] == 404


def test_search_flights_import_stays_used():
    assert len(search_flights("TLV", "BER", "2026-12-10", "economy")) == 4
