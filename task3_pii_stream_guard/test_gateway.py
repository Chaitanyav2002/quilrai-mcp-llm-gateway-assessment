import json
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn

from task3_pii_stream_guard.gateway import create_app, redact_stream
from task3_pii_stream_guard.mock_provider import DEFAULT_REPLY, create_mock_provider

EXPECTED = "Sure. Email [REDACTED] or call 555-123-4567. Card [REDACTED] is on file, SSN [REDACTED]"
REQUEST = {"model": "mock", "stream": True, "messages": [{"role": "user", "content": "hi"}]}


@contextmanager
def serve(app):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(5)


@contextmanager
def gateway_in_front_of(provider):
    with serve(provider) as provider_url:
        app = create_app(f"{provider_url}/v1/chat/completions", api_key="gateway-key")
        with serve(app) as gateway_url:
            yield gateway_url


def events_from(lines):
    return [line[len("data: "):] for line in lines if line.startswith("data: ")]


def content_of(events):
    text = ""
    for data in events:
        if data != "[DONE]":
            for choice in json.loads(data)["choices"]:
                text += (choice.get("delta") or {}).get("content") or ""
    return text


async def lines_of(*raw):
    for line in "".join(raw).split("\n"):
        yield line


# over real HTTP

async def test_stream_comes_back_redacted():
    with gateway_in_front_of(create_mock_provider(delay=0)) as url:
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", f"{url}/v1/chat/completions", json=REQUEST) as response:
                assert response.headers["content-type"].startswith("text/event-stream")
                events = events_from([line async for line in response.aiter_lines()])

    assert content_of(events) == EXPECTED
    assert events[-1] == "[DONE]"
    finish = [i for i, e in enumerate(events[:-1]) if json.loads(e)["choices"][0]["finish_reason"]]
    assert finish == [len(events) - 2]  # nothing sent after the finish chunk
    assert json.loads(events[0])["id"] == "chatcmpl-mock"
    for secret in ("john.doe", "4111", "6789"):
        assert secret not in "".join(events)


async def test_first_text_arrives_long_before_the_stream_ends():
    with gateway_in_front_of(create_mock_provider(delay=0.25)) as url:
        async with httpx.AsyncClient() as client:
            start = time.perf_counter()
            first_text = None
            async with client.stream("POST", f"{url}/v1/chat/completions", json=REQUEST) as response:
                async for line in response.aiter_lines():
                    if first_text is None and line.startswith("data: {") and content_of([line[6:]]):
                        first_text = time.perf_counter() - start
            total = time.perf_counter() - start

    assert total > 1.4  # 6 chunks, 0.25 s apart
    assert first_text < 0.6, f"first text after {first_text:.2f}s of {total:.2f}s"


# the stream transform

def chunk(content=None, index=0, finish=None):
    delta = {} if content is None else {"content": content}
    return "data: " + json.dumps({"id": "c1", "model": "m", "choices": [
        {"index": index, "delta": delta, "finish_reason": finish}]}) + "\n\n"


async def test_each_choice_is_redacted_on_its_own():
    lines = lines_of(chunk("mail a@b", 0), chunk("call 123-45", 1), chunk(".com", 0), chunk("-6789", 1),
                     chunk(index=0, finish="stop"), chunk(index=1, finish="stop"), "data: [DONE]\n\n")
    events = [e async for e in redact_stream(lines)]

    per_choice = {0: "", 1: ""}
    for data in events_from("".join(events).split("\n")):
        if data != "[DONE]":
            for choice in json.loads(data)["choices"]:
                per_choice[choice["index"]] += choice["delta"].get("content", "")
    assert per_choice == {0: "mail [REDACTED]", 1: "call [REDACTED]"}


async def test_finish_chunk_with_null_content_still_flushes():
    raw = chunk("mail a@b.co") + 'data: {"id": "c1", "choices": [{"index": 0, "delta": {"content": null}, "finish_reason": "stop"}]}\n\n'
    events = events_from("".join([e async for e in redact_stream(lines_of(raw, "data: [DONE]\n\n"))]).split("\n"))

    assert content_of(events) == "mail [REDACTED]"
    assert events[-1] == "[DONE]"


async def test_tool_call_arguments_and_refusals_are_redacted_too():
    def tool_chunk(arguments, finish=None):
        delta = {"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]}
        return "data: " + json.dumps({"id": "c1", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

    raw = [tool_chunk('{"to": "jane@cor'), tool_chunk('p.com", "ssn": "123-45-'), tool_chunk('6789"}'),
           "data: " + json.dumps({"id": "c1", "choices": [{"index": 0, "delta": {"refusal": "I won't email bob@x.io"},
                                                           "finish_reason": "stop"}]}) + "\n\n"]
    out = "".join([e async for e in redact_stream(lines_of(*raw))])

    arguments = refusal = ""
    for data in events_from(out.split("\n")):
        delta = json.loads(data)["choices"][0]["delta"]
        for call in delta.get("tool_calls", []):
            arguments += call["function"].get("arguments", "")
        refusal += delta.get("refusal") or ""
    assert arguments == '{"to": "[REDACTED]", "ssn": "[REDACTED]"}'
    assert refusal == "I won't email [REDACTED]"


async def test_held_text_is_not_lost_when_the_stream_stops_early():
    lines = lines_of(chunk("reach me at jane@example.org"))
    events = events_from("".join([e async for e in redact_stream(lines)]).split("\n"))

    assert content_of(events) == "reach me at [REDACTED]"
    assert "[DONE]" not in events


async def test_sse_comments_and_multiline_data_are_handled():
    raw = ': keep-alive\n\ndata: {"id": "c1", "choices": [{"index": 0,\ndata: "delta": {"content": "hi"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
    events = events_from("".join([e async for e in redact_stream(lines_of(raw))]).split("\n"))

    assert content_of(events) == "hi"


# errors

def gateway_with(handler):
    provider = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app("http://provider/v1/chat/completions", api_key="gateway-key", http_client=provider)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


async def test_oversized_request_is_rejected():
    async with gateway_with(lambda r: httpx.Response(200)) as client:
        response = await client.post("/v1/chat/completions", content=b"x" * 1_000_001,
                                     headers={"Content-Type": "application/json"})

    assert response.status_code == 413


async def test_non_streaming_request_is_rejected():
    async with gateway_with(lambda r: httpx.Response(200)) as client:
        response = await client.post("/v1/chat/completions", json={**REQUEST, "stream": False})

    assert response.status_code == 400


async def test_gateway_uses_its_own_provider_key():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text="data: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async with gateway_with(handler) as client:
        await client.post("/v1/chat/completions", json=REQUEST, headers={"Authorization": "Bearer client-key"})

    assert seen["auth"] == "Bearer gateway-key"


@pytest.mark.parametrize("status, expected", [(429, 429), (500, 502), (503, 502)])
async def test_provider_errors_are_passed_on_without_details(status, expected):
    def handler(request):
        return httpx.Response(status, json={"error": "db at 10.1.2.3 is down"}, headers={"retry-after": "7"})

    async with gateway_with(handler) as client:
        response = await client.post("/v1/chat/completions", json=REQUEST)

    assert response.status_code == expected
    assert "10.1.2.3" not in response.text
    assert response.json()["error"]["type"] == "upstream_error"
    if status == 429:
        assert response.headers["retry-after"] == "7"


async def test_provider_down_is_a_502():
    def handler(request):
        raise httpx.ConnectError("refused")

    async with gateway_with(handler) as client:
        response = await client.post("/v1/chat/completions", json=REQUEST)

    assert response.status_code == 502
