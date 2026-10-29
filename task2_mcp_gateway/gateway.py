"""MCP gateway: checks the caller's role before forwarding JSON-RPC to the MCP server.

Run (needs GATEWAY_JWT_SECRET and DOWNSTREAM_MCP_URL):
    uvicorn task2_mcp_gateway.gateway:app_from_env --factory --port 9000
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

log = logging.getLogger("mcp-gateway")

ROLES = {"admin", "viewer"}
ADMIN_TOOL_PREFIX = "admin_"
MAX_BODY = 1_000_000
# the GET notification stream can sit idle for minutes
STREAM_TIMEOUT = httpx.Timeout(60.0, connect=5.0, read=None)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHENTICATED = -32000
UNAUTHORIZED_TOOL_CALL = -32001

# the user's token stays here, the MCP server should never see it
DROP_REQUEST_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "host", "content-length",
    "connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade",
}
DROP_RESPONSE_HEADERS = {"content-length", "connection", "keep-alive", "transfer-encoding", "set-cookie"}


class DuplicateKey(ValueError):
    pass


def reject_duplicate_keys(pairs):
    # we forward the raw bytes, and a parser that keeps the first duplicate would see another call
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise DuplicateKey("duplicate key in JSON object")
    return obj


def json_rpc_error(request_id: Any, code: int, message: str, status: int = 200, headers=None) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    return JSONResponse(body, status_code=status, headers=headers)


def authenticate(authorization: str | None, secret: str) -> dict | None:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        # pinned algorithm, so alg=none tokens fail
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "role"]})
    except jwt.PyJWTError:
        return None
    role = claims.get("role")
    return claims if isinstance(role, str) and role in ROLES else None


def envelope_problem(message: Any) -> str | None:
    if isinstance(message, list):
        # MCP dropped batching in 2025-06-18, and an admin call could hide in one
        return "Batch requests are not supported"
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return "Invalid Request"
    if "id" in message and type(message["id"]) not in (str, int):
        return "Invalid Request: id must be a string or an integer"
    if "method" in message:
        return None if isinstance(message["method"], str) else "Invalid Request: method must be a string"
    if "id" in message and ("result" in message or "error" in message):
        return None  # a client's reply to a server request, e.g. elicitation
    return "Invalid Request"


def check_tool_call(message: dict, claims: dict) -> JSONResponse | None:
    if "id" not in message:
        return json_rpc_error(None, INVALID_REQUEST, "Invalid Request: tools/call needs an id", status=400)
    request_id = message["id"]
    params = message.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if not isinstance(name, str):
        # can't read the name, can't authorize it
        return json_rpc_error(request_id, INVALID_PARAMS, "Invalid params: tools/call needs a string params.name")
    # ignore case in case the server normalizes tool names
    if name.lower().startswith(ADMIN_TOOL_PREFIX) and claims["role"] != "admin":
        log.warning("denied tool call sub=%r role=%s tool=%r", claims.get("sub"), claims["role"], name)
        return json_rpc_error(request_id, UNAUTHORIZED_TOOL_CALL, "Unauthorized Tool Call")
    return None


def create_app(downstream_url: str, jwt_secret: str, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    if len(jwt_secret.encode()) < 32:
        raise ValueError("GATEWAY_JWT_SECRET must be at least 32 bytes for HS256")

    # SSE replies can stay open while a slow tool runs
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))

    @asynccontextmanager
    async def lifespan(app):
        yield
        if http_client is None:
            await client.aclose()

    app = FastAPI(title="MCP security gateway", lifespan=lifespan)

    async def forward(request: Request, body: bytes, request_id: Any = None):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST_HEADERS}
        timeout = STREAM_TIMEOUT if request.method == "GET" else httpx.USE_CLIENT_DEFAULT
        upstream_request = client.build_request(request.method, downstream_url, content=body, headers=headers,
                                                timeout=timeout)
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            log.warning("downstream timed out")
            return json_rpc_error(request_id, INTERNAL_ERROR, "Downstream MCP server timed out", status=504)
        except httpx.TransportError as exc:
            log.warning("downstream unavailable: %s", exc)  # details stay in our logs
            return json_rpc_error(request_id, INTERNAL_ERROR, "Downstream MCP server unavailable", status=502)

        response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS}
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream.aclose),
        )

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def mcp(request: Request):
        claims = authenticate(request.headers.get("authorization"), jwt_secret)
        if claims is None:
            return json_rpc_error(None, UNAUTHENTICATED, "Missing or invalid bearer token", status=401,
                                  headers={"WWW-Authenticate": 'Bearer realm="mcp-gateway", error="invalid_token"'})

        if request.method != "POST":
            # GET (SSE stream) and DELETE (end session) have no body to check
            return await forward(request, b"")

        raw = bytearray()
        async for part in request.stream():
            raw += part
            if len(raw) > MAX_BODY:
                return json_rpc_error(None, INVALID_REQUEST, "Request body too large", status=413)
        body = bytes(raw)
        try:
            message = json.loads(body, object_pairs_hook=reject_duplicate_keys)
        except DuplicateKey:
            return json_rpc_error(None, INVALID_REQUEST, "Invalid Request: duplicate keys", status=400)
        except ValueError:
            return json_rpc_error(None, PARSE_ERROR, "Parse error", status=400)

        if problem := envelope_problem(message):
            return json_rpc_error(None, INVALID_REQUEST, problem, status=400)

        if message.get("method") == "tools/call":
            if denied := check_tool_call(message, claims):
                return denied

        log.info("forward sub=%r role=%s method=%r", claims.get("sub"), claims["role"], message.get("method"))
        return await forward(request, body, message.get("id"))

    return app


def app_from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app(os.environ["DOWNSTREAM_MCP_URL"], os.environ["GATEWAY_JWT_SECRET"])
