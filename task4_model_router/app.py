"""HTTP front end for the model router.

Run (see .env.example for the variables):
    uvicorn task4_model_router.app:app_from_env --factory --port 9200
"""

import hashlib
import json
import logging
import math
import os
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from task4_model_router.limiter import TokenRateLimiter
from task4_model_router.router import GatewayError, ModelRouter, Provider

log = logging.getLogger("model-router")

MAX_BODY = 1_000_000


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def error_response(exc: GatewayError, request_id: str) -> JSONResponse:
    headers = {"X-Request-Id": request_id}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(math.ceil(exc.retry_after))
    body = {"error": {"type": "gateway_error", "code": exc.code, "message": exc.message, "request_id": request_id}}
    return JSONResponse(body, status_code=exc.status, headers=headers)


def invalid(message: str) -> GatewayError:
    return GatewayError(400, "invalid_request", message)


def read_api_key(request: Request) -> str:
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not key.strip():
        raise GatewayError(401, "missing_api_key", "Send your API key as: Authorization: Bearer <key>")
    return key.strip()


async def read_body(request: Request) -> dict:
    raw = bytearray()
    async for part in request.stream():
        raw += part
        if len(raw) > MAX_BODY:
            raise GatewayError(413, "request_too_large", "Request body is over 1 MB")
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise invalid("Request body must be JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise invalid("Request must be a JSON object with a messages list")
    max_tokens = body.get("max_tokens")
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 1):
        raise invalid("max_tokens must be a positive integer")
    if body.get("stream"):
        raise invalid("Streaming is not supported on this endpoint")
    return body


def create_app(router: ModelRouter, api_keys: set[str]) -> FastAPI:
    # without a known-key list, every new string would be a fresh 50k budget
    allowed = {key_hash(k) for k in api_keys}
    app = FastAPI(title="Model router")

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        request_id = uuid.uuid4().hex[:16]
        try:
            api_key = read_api_key(request)
            if key_hash(api_key) not in allowed:
                raise GatewayError(401, "invalid_api_key", "Unknown API key")
            body = await read_body(request)
            data, provider = await router.complete(api_key, body)
        except GatewayError as exc:
            return error_response(exc, request_id)
        except Exception:
            log.exception("unexpected error, request_id=%s", request_id)
            return error_response(GatewayError(500, "internal_error", "Unexpected gateway error"), request_id)
        return JSONResponse(data, headers={"X-Gateway-Provider": provider, "X-Request-Id": request_id})

    return app


def app_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = os.environ
    router = ModelRouter(
        Provider("primary", env["PRIMARY_URL"], env["PRIMARY_API_KEY"], model=env.get("PRIMARY_MODEL")),
        Provider("secondary", env["SECONDARY_URL"], env["SECONDARY_API_KEY"],
                 timeout=float(env.get("SECONDARY_TIMEOUT", "10")), model=env.get("SECONDARY_MODEL")),
        TokenRateLimiter(env.get("ROUTER_DB_PATH", "router.db"), limit=int(env.get("TOKENS_PER_MINUTE", "50000"))),
        httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)),
    )
    keys = {k.strip() for k in env["TENANT_API_KEYS"].split(",") if k.strip()}
    return create_app(router, keys)
