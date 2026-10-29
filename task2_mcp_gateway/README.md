# Task 2: MCP security gateway (auth and tool filtering)

A reverse proxy (FastAPI + httpx) that sits between an AI agent and a downstream MCP server. It reads the role from a bearer JWT and blocks `admin_*` tool calls for anyone who is not an admin. A blocked call never reaches the downstream server.

```
agent --(Bearer JWT)--> gateway :9000 /mcp --(original bytes, no token)--> MCP server :9001 /mcp
```

## Run it

```bash
export GATEWAY_JWT_SECRET="$(openssl rand -hex 32)"
export DOWNSTREAM_MCP_URL=http://127.0.0.1:9001/mcp

uvicorn task2_mcp_gateway.mock_server:app --port 9001 &
uvicorn task2_mcp_gateway.gateway:app_from_env --factory --port 9000 &

VIEWER=$(python -m task2_mcp_gateway.make_token viewer)
curl -s localhost:9000/mcp -H "Authorization: Bearer $VIEWER" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
# {"jsonrpc":"2.0","id":2,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}

pytest task2_mcp_gateway -q    # 47 tests
```

## Request flow

| Step | Check | On failure |
|---|---|---|
| 1. Authenticate | `Authorization: Bearer <jwt>`, HS256 only, signature valid, `exp` present and not expired, `role` is `"admin"` or `"viewer"` | HTTP 401, `WWW-Authenticate: Bearer`, JSON-RPC error `-32000`. Nothing is forwarded. |
| 2. Parse | Body is at most 1 MB, is JSON with no duplicate keys, and is a single JSON-RPC 2.0 message (string or integer `id`, string `method`) | HTTP 413 if too large; HTTP 400 with `-32700` (not JSON) or `-32600` (duplicate keys, bad envelope, batch), `id: null` |
| 3. Authorize | `tools/call` with a `params.name` starting with `admin_` needs role `admin` | HTTP 200, `{"code": -32001, "message": "Unauthorized Tool Call"}` with the request's `id` |
| 4. Forward | Everything else, `tools/list` included | The downstream response is streamed back as is (JSON or SSE) |
| | Downstream unreachable or timed out | HTTP 502 or 504, `-32603`, with no internal addresses in the body |

The order is deliberate: an unauthenticated caller learns nothing about which tools exist or how the gateway parses requests.

## Design decisions

**JWT, verified and pinned.** The role comes from a signed claim, so a client cannot simply claim to be an admin. `algorithms=["HS256"]` is pinned, so `alg: none` and algorithm-swap tokens fail. `exp` is required, the role must be a string from a known set (a list such as `["admin"]` is rejected), and the app refuses to start with a secret shorter than 32 bytes. The secret comes from an environment variable and is never in the repo.

**Fail closed on anything ambiguous.**
- The `admin_` check ignores case. MCP tool names are case sensitive, but if the downstream server ever normalized case, `ADMIN_reset_key` would become a working admin tool.
- A `tools/call` without a readable string `params.name` is refused with `-32602`. A call the gateway cannot read is a call it cannot authorize.
- A `tools/call` sent as a notification (no `id`) is refused, since there would be nowhere to send the denial.
- JSON-RPC batches are refused. MCP removed batching in the 2025-06-18 revision, and refusing outright means an admin call cannot be hidden inside a batch next to an allowed one.
- Duplicate keys are refused. The gateway forwards the original bytes, and Python keeps the *last* duplicate while some parsers keep the *first*, so `{"name":"admin_reset_key","name":"get_weather"}` would look harmless here and run the admin tool downstream. The code review caught this one.

**The prefix check is exact on purpose.** `get_admin_contact` is allowed. The rule is "starts with `admin_`", not "contains admin".

**Transparent forwarding.** The original request bytes are forwarded unchanged rather than re-serialized, so the downstream server sees exactly what the client sent. MCP headers such as `Mcp-Session-Id` and `Mcp-Protocol-Version` pass through both ways. The response is streamed with `aiter_raw()`, so a streamable HTTP server's SSE replies work, and `GET`/`DELETE /mcp` (the SSE stream and session end) are forwarded after authentication. The `GET` notification stream has no read timeout, because it can sit idle for minutes.

**The user's token stops at the gateway.** `Authorization`, cookies and hop-by-hop headers are not forwarded. Passing a client token through to a downstream server is a known MCP anti-pattern: the server could replay it elsewhere. In production the gateway would authenticate to the downstream server with its own credential.

**Audit trail.** Every forwarded call and every denial is logged with the token's `sub` and role (quoted, so a tool name with a newline cannot fake a log line). The token itself is never logged.

## Tests and what they prove

- `tools/list` arrives downstream byte for byte, and the response comes back unchanged.
- A viewer calling `admin_reset_key`, `ADMIN_reset_key` or `Admin_delete_user` gets the exact `-32001` response, **and the mock server records zero requests**.
- An admin's call goes through, and `get_admin_contact` is not blocked by mistake.
- 14 bad-token cases (missing, wrong scheme, garbage, wrong key, expired, no `exp`, no role, unknown role, role as a list, `alg: none`, HS512) all get 401.
- Malformed bodies, batches, duplicate keys, bodies over 1 MB and unreadable `tools/call` names are rejected without being forwarded.
- `GET` and `DELETE` need a token too, and the token is never forwarded.
- I checked that the tests are meaningful by breaking the gateway five ways (case-sensitive prefix, accepting HS512, forwarding `Authorization`, no role type check, no admin check). Each change made the matching tests fail.

## Not included

- **`tools/list` is not filtered.** The task says to forward it transparently. Hiding admin tools from viewers would be an easy next step, and good for the model's context, but the call check is the real security boundary either way.
- Token issuing, key rotation and a real identity provider (it would use RS256/JWKS rather than a shared secret).
