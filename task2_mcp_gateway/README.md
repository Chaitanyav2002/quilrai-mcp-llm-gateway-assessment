# Task 2: MCP security gateway

A reverse proxy (FastAPI + httpx) between an AI agent and a downstream MCP server. It reads the caller's role from a bearer JWT and blocks `admin_*` tool calls for anyone who is not an admin. A blocked call never reaches the MCP server.

```
agent --(Bearer JWT)--> gateway :9000 /mcp --(same bytes, no token)--> MCP server :9001 /mcp
```

## Run

```bash
export GATEWAY_JWT_SECRET="$(openssl rand -hex 32)"
export DOWNSTREAM_MCP_URL=http://127.0.0.1:9001/mcp

uvicorn task2_mcp_gateway.mock_server:app --port 9001 &
uvicorn task2_mcp_gateway.gateway:app_from_env --factory --port 9000 &

VIEWER=$(python -m task2_mcp_gateway.make_token viewer)
curl -s localhost:9000/mcp -H "Authorization: Bearer $VIEWER" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
# {"jsonrpc":"2.0","id":2,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}

pytest task2_mcp_gateway -q    # 48 tests
```

## What happens to a request

1. **Auth.** The token must be an HS256 JWT signed with our secret, with `exp` and a `role` of `admin` or `viewer`. Otherwise the caller gets HTTP 401 and nothing else.
2. **Parse.** The body must be under 1 MB, valid JSON with no duplicate keys, and a single JSON-RPC 2.0 message. Otherwise: 413, or 400 with `-32700` / `-32600`.
3. **Authorize.** A `tools/call` whose `params.name` starts with `admin_` needs the admin role. Anyone else gets `-32001 Unauthorized Tool Call` with their request `id`.
4. **Forward.** Everything else, `tools/list` included, goes to the MCP server unchanged, and the reply (JSON or SSE) is streamed back. If the server is down: 502, or 504 on a timeout.

Auth comes first so an unauthenticated caller learns nothing about the tools.

## Decisions

- The JWT algorithm is pinned to HS256, so `alg: none` tokens fail. The role must be a known string, so a list like `["admin"]` is rejected. The secret comes from the environment, and the app won't start with one shorter than 32 bytes.
- The `admin_` check ignores case. MCP tool names are case sensitive, but if the server ever normalized names, `ADMIN_reset_key` would turn into a working admin tool.
- `get_admin_contact` is allowed. The rule is "starts with `admin_`", not "contains admin".
- Things that are refused rather than forwarded:
  - A `tools/call` without a readable string name (`-32602`), since there's no way to authorize it.
  - A `tools/call` with no `id`.
  - JSON-RPC batches. MCP dropped them in the 2025-06-18 spec, and an admin call could hide inside one.
  - Duplicate keys. The gateway forwards the original bytes, and Python keeps the last duplicate while some parsers keep the first, so `{"name":"admin_reset_key","name":"get_weather"}` would pass the check here and run the admin tool downstream. The code review found this.
- The user's token is not forwarded. Passing it on would let the MCP server replay it somewhere else. In production the gateway would use its own credential with the server.
- MCP headers such as `Mcp-Session-Id` pass through both ways. `GET` (the notification stream) and `DELETE` (end session) are forwarded after auth, and the `GET` stream has no read timeout because it can be idle for minutes.
- Every forwarded call and every denial is logged with the token's `sub` and role. The token itself is never logged.

## Tests

- `tools/list` reaches the server byte for byte, and the reply comes back unchanged.
- A viewer calling `admin_reset_key`, `ADMIN_reset_key` or `Admin_delete_user` gets the exact `-32001` error, and the mock server records zero requests.
- Admins get through, and `get_admin_contact` is not blocked by mistake.
- 14 bad-token cases get 401: missing, wrong scheme, wrong key, expired, no role, `alg: none`, HS512 and so on.
- Bad JSON, batches, duplicate keys, oversized bodies and unreadable tool names never reach the server.
- I broke the gateway five ways (case-sensitive check, accepting HS512, forwarding the token, no role type check, no admin check) and each one failed a test.

## Not included

- `tools/list` is not filtered, because the task says to forward it as is. Hiding admin tools from viewers would be an easy addition, but the check on calls is what actually protects them.
- Issuing tokens. A real setup would use an identity provider and RS256 keys instead of a shared secret.
