"""Fake completion provider for trying the router by hand.

MOCK_MODE=ok (default), rate_limited (always 429) or slow (answers after 5 s).
Run: MOCK_MODE=slow uvicorn task4_model_router.mock_provider:app --port 9201
"""

import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
MODE = os.environ.get("MOCK_MODE", "ok")


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    if MODE == "rate_limited":
        return JSONResponse({"error": {"message": "quota exceeded"}}, status_code=429)
    if MODE == "slow":
        await asyncio.sleep(5)
    return {
        "id": "cmpl-mock",
        "object": "chat.completion",
        "model": body.get("model"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"hello from {body.get('model')}"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }
