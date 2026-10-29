"""LLM gateway: streams chat completions from a provider with PII redacted on the way back.

Run (needs LLM_PROVIDER_URL and LLM_PROVIDER_API_KEY):
    uvicorn task3_pii_stream_guard.gateway:app_from_env --factory --port 9100
"""

import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from task3_pii_stream_guard.redactor import StreamRedactor

log = logging.getLogger("llm-gateway")

MAX_BODY = 1_000_000


def error(status: int, message: str, headers=None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "upstream_error" if status >= 429 else "invalid_request"}},
                        status_code=status, headers=headers)


def event(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    data = []
    async for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif not line and data:
            yield "\n".join(data)
            data = []
    if data:
        yield "\n".join(data)


def text_fields(delta: dict):
    """Text in a delta that can carry PII: content, refusal and tool call arguments.

    Yields (field, holder, key). field is "content", "refusal" or ("tool", index).
    """
    yield "content", delta, "content"
    yield "refusal", delta, "refusal"
    for call in delta.get("tool_calls") or []:
        if isinstance(call, dict) and isinstance(call.get("function"), dict):
            yield ("tool", call.get("index", 0)), call["function"], "arguments"


def append(delta: dict, field, text: str) -> None:
    if isinstance(field, str):
        delta[field] = (delta.get(field) or "") + text
        return
    # join the entry with the same index if there is one, not every client merges two
    calls = delta.setdefault("tool_calls", [])
    for call in calls:
        if isinstance(call, dict) and call.get("index", 0) == field[1] and isinstance(call.get("function"), dict):
            call["function"]["arguments"] = (call["function"].get("arguments") or "") + text
            return
    calls.append({"index": field[1], "function": {"arguments": text}})


async def redact_stream(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    # one redactor per choice and text field, so streams never mix
    redactors: dict[tuple, StreamRedactor] = {}
    last, done = None, False
    async for data in sse_data(lines):
        if data == "[DONE]":
            done = True
            break
        chunk = json.loads(data)
        last = chunk
        for choice in chunk.get("choices", []):
            index = choice.get("index", 0)
            delta = choice.get("delta") or {}
            choice["delta"] = delta
            for field, holder, key in text_fields(delta):
                if isinstance(holder.get(key), str):
                    holder[key] = redactors.setdefault((index, field), StreamRedactor()).feed(holder[key])
            if choice.get("finish_reason") is not None:
                for (i, field), redactor in redactors.items():
                    if i == index and (rest := redactor.flush()):
                        append(delta, field, rest)
        yield event(chunk)

    # provider stopped without a finish_reason: still send what was held back
    for (index, field), redactor in redactors.items():
        if rest := redactor.flush():
            delta = {}
            append(delta, field, rest)
            yield event({"id": last.get("id"), "object": "chat.completion.chunk", "model": last.get("model"),
                         "choices": [{"index": index, "delta": delta, "finish_reason": None}]})
    if done:
        yield "data: [DONE]\n\n"


def create_app(provider_url: str, api_key: str, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))

    @asynccontextmanager
    async def lifespan(app):
        yield
        if http_client is None:
            await client.aclose()

    app = FastAPI(title="LLM gateway", lifespan=lifespan)

    async def relay(upstream: httpx.Response) -> AsyncIterator[str]:
        try:
            async for item in redact_stream(upstream.aiter_lines()):
                yield item
        except Exception as exc:
            log.warning("provider stream failed: %r", exc)
            yield event({"error": {"message": "Upstream stream failed", "type": "upstream_error"}})
        finally:
            await upstream.aclose()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        raw = bytearray()
        async for part in request.stream():
            raw += part
            if len(raw) > MAX_BODY:
                return error(413, "Request body too large")
        try:
            body = json.loads(raw)
        except ValueError:
            return error(400, "Request body must be JSON")
        if not isinstance(body, dict) or body.get("stream") is not True:
            return error(400, 'This endpoint streams only, send "stream": true')

        upstream_request = client.build_request(
            "POST", provider_url, json=body, headers={"Authorization": f"Bearer {api_key}"})
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            return error(504, "Upstream provider timed out")
        except httpx.TransportError as exc:
            log.warning("provider unavailable: %r", exc)
            return error(502, "Upstream provider unavailable")

        if upstream.status_code != 200:
            await upstream.aclose()
            log.warning("provider returned %s", upstream.status_code)
            if upstream.status_code == 429:
                retry = upstream.headers.get("retry-after")
                return error(429, "Upstream provider rate limit", headers={"Retry-After": retry} if retry else None)
            return error(502, "Upstream provider error")

        return StreamingResponse(relay(upstream), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


def app_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app(os.environ["LLM_PROVIDER_URL"], os.environ["LLM_PROVIDER_API_KEY"])
