from datetime import UTC, datetime

import httpx
import pytest

from tripwise.core.quota import QuotaLlm
from tripwise.llm.base import LlmError, LlmRequest, LlmResponse
from tripwise.llm.gemini import GeminiClient, to_steps
from tripwise.llm.scripted import ScriptedLlm

REQ = LlmRequest("s", [{"role": "user", "text": "hi"}], [])


def ok(steps):
    return httpx.Response(200, json={"status": "completed", "steps": steps})


def say(text):
    return {"type": "model_output", "content": [{"type": "text", "text": text}]}


def client(handler):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append(request)
        return handler(len(calls), request)

    async def no_sleep(_s):
        return None

    c = GeminiClient("k", "m", client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped)), sleep=no_sleep)
    return c, calls


async def test_parses_text_and_function_calls():
    c, _ = client(lambda n, r: ok([{"type": "thought", "signature": "sig"}, say("hello"), {"type": "function_call", "id": "c1", "name": "x", "arguments": {"a": 1}}]))
    r = await c.generate(REQ)
    assert r.text == "hello"
    assert r.tool_calls == [{"id": "c1", "name": "x", "args": {"a": 1}, "thoughtSignature": "sig"}]


@pytest.mark.parametrize(("status", "kind"), [(429, "rate_limited"), (503, "server")])
async def test_retries_once_then_maps_status(status, kind):
    c, calls = client(lambda n, r: httpx.Response(status))
    with pytest.raises(LlmError) as e:
        await c.generate(REQ)
    assert e.value.kind == kind
    assert len(calls) == 2


async def test_recovers_when_the_retry_succeeds():
    c, _ = client(lambda n, r: httpx.Response(500) if n == 1 else ok([say("ok")]))
    assert (await c.generate(REQ)).text == "ok"


async def test_maps_a_timeout_and_retries_once():
    def boom(n, r):
        raise httpx.ReadTimeout("t")

    c, calls = client(boom)
    with pytest.raises(LlmError) as e:
        await c.generate(REQ)
    assert e.value.kind == "timeout"
    assert len(calls) == 2


async def test_does_not_retry_a_400():
    c, calls = client(lambda n, r: httpx.Response(400))
    with pytest.raises(LlmError) as e:
        await c.generate(REQ)
    assert e.value.kind == "bad_request"
    assert len(calls) == 1


async def test_sends_key_in_header_not_url():
    c, calls = client(lambda n, r: ok([say("ok")]))
    await c.generate(REQ)
    assert calls[0].headers["x-goog-api-key"] == "k" and "k" not in calls[0].url.query.decode()


def test_maps_transcript_to_steps_and_replays_the_thought_signature():
    c = to_steps([
        {"role": "user", "text": "q"},
        {"role": "assistant", "toolCalls": [{"id": "1", "name": "a", "args": {"x": 1}, "thoughtSignature": "sig"}, {"id": "2", "name": "b", "args": {}}]},
        {"role": "tool", "toolCallId": "1", "name": "a", "result": {"r": 1}},
        {"role": "tool", "toolCallId": "2", "name": "b", "result": 2},
        {"role": "assistant", "text": "done"},
    ])
    assert [s["type"] for s in c] == [
        "user_input", "thought", "function_call", "function_call", "function_result", "function_result", "model_output",
    ]
    assert c[1]["signature"] == "sig" and c[2]["arguments"] == {"x": 1}
    assert c[4]["call_id"] == "1" and c[4]["result"] == [{"type": "text", "text": '{"r": 1}'}]


async def test_request_is_stateless_with_flat_function_tools():
    import json

    c, calls = client(lambda n, r: ok([say("ok")]))
    await c.generate(LlmRequest("sys", [{"role": "user", "text": "hi"}], [{"name": "t", "description": "d", "parameters": {}}]))
    body = json.loads(calls[0].content)
    assert body["store"] is False and body["system_instruction"] == "sys" and body["model"] == "m"
    assert body["tools"] == [{"type": "function", "name": "t", "description": "d", "parameters": {}}]
    assert calls[0].url.path == "/v1beta/interactions"


async def test_daily_limit_is_quota_and_is_not_retried():
    c, calls = client(lambda n, r: httpx.Response(429, text="limit: 20 requests per day on Free Tier"))
    with pytest.raises(LlmError) as e:
        await c.generate(REQ)
    assert e.value.kind == "quota" and len(calls) == 1


async def test_daily_quota_throws_past_the_cap_and_resets_next_day():
    day = datetime(2026, 11, 1, tzinfo=UTC)
    q = QuotaLlm(ScriptedLlm([], LlmResponse("x")), 2, lambda: day)
    await q.generate(REQ)
    await q.generate(REQ)
    with pytest.raises(LlmError):
        await q.generate(REQ)
    day = datetime(2026, 11, 2, tzinfo=UTC)
    assert await q.generate(REQ)
