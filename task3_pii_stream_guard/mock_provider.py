"""Fake OpenAI style provider that streams a fixed reply with PII split across chunks.

Run: uvicorn task3_pii_stream_guard.mock_provider:app --port 9101
"""

import asyncio
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

DEFAULT_REPLY = [
    "Sure. Email jo", "hn.doe@exam", "ple.com or call", " 555-123-4567. Card 4111 1111",
    " 1111 1111 is on file, SSN 123-45", "-6789",
]


def sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def create_mock_provider(chunks=DEFAULT_REPLY, delay=0.05) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": body.get("model", "mock")}

        def event(delta, finish=None):
            return sse({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

        async def stream():
            yield event({"role": "assistant", "content": ""})
            for piece in chunks:
                await asyncio.sleep(delay)
                yield event({"content": piece})
            yield event({}, finish="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


app = create_mock_provider()
