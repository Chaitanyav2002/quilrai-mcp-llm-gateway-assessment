import json
import time

import httpx
import jwt
import pytest

from task2_mcp_gateway.gateway import create_app
from task2_mcp_gateway.mock_server import create_mock_server

SECRET = "test-secret-long-enough-for-hs256-and-for-the-hs512-negative-case"


def make_token(role="viewer", secret=SECRET, algorithm="HS256", **claims):
    payload = {"sub": "user-1", "role": role, "exp": int(time.time()) + 300, **claims}
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, secret, algorithm=algorithm)


@pytest.fixture
def downstream():
    return create_mock_server()


@pytest.fixture
async def gateway(downstream):
    to_downstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=downstream))
    app = create_app(downstream_url="http://downstream/mcp", jwt_secret=SECRET, http_client=to_downstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        yield client
    await to_downstream.aclose()


async def post(client, body, role="viewer", token=None, **headers):
    token = token if token is not None else make_token(role)
    content = body if isinstance(body, (str, bytes)) else json.dumps(body)
    return await client.post("/mcp", content=content, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", **headers,
    })


def call(name, request_id=1, arguments=None):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}}}


# forwarding

async def test_tools_list_is_forwarded_transparently(gateway, downstream):
    body = '{"jsonrpc":"2.0","id":"list-1","method":"tools/list"}'

    response = await post(gateway, body)

    assert response.status_code == 200
    assert response.json()["id"] == "list-1"
    names = [t["name"] for t in response.json()["result"]["tools"]]
    assert "admin_reset_key" in names  # listing is not filtered, only calls are
    assert downstream.state.received[0]["body"] == body.encode()  # same bytes downstream


async def test_bearer_token_is_not_passed_downstream(gateway, downstream):
    await post(gateway, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert "authorization" not in downstream.state.received[0]["headers"]


async def test_mcp_session_header_passes_both_ways(gateway, downstream):
    response = await post(gateway, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                          **{"Mcp-Session-Id": "session-abc"})

    assert downstream.state.received[0]["headers"]["mcp-session-id"] == "session-abc"
    assert response.headers["mcp-session-id"] == "session-abc"


async def test_viewer_can_call_a_regular_tool(gateway, downstream):
    response = await post(gateway, call("get_weather", arguments={"city": "Austin"}))

    assert response.json()["result"]["content"][0]["text"] == "called get_weather"
    assert len(downstream.state.received) == 1


async def test_admin_can_call_an_admin_tool(gateway, downstream):
    response = await post(gateway, call("admin_reset_key"), role="admin")

    assert response.json()["result"]["content"][0]["text"] == "called admin_reset_key"


async def test_other_methods_are_forwarded(gateway, downstream):
    response = await post(gateway, {"jsonrpc": "2.0", "id": 3, "method": "ping"})

    assert response.json() == {"jsonrpc": "2.0", "id": 3, "result": {}}


async def test_sse_response_from_downstream_is_streamed_back(gateway):
    response = await post(gateway, call("get_weather", request_id=5), **{"X-Mock-Respond-With": "sse"})

    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"id": 5' in response.text or '"id":5' in response.text


# authorization

@pytest.mark.parametrize("name", ["admin_reset_key", "ADMIN_reset_key", "Admin_delete_user"])
async def test_viewer_cannot_call_admin_tools_and_downstream_is_never_called(gateway, downstream, name):
    response = await post(gateway, call(name, request_id=7))

    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32001, "message": "Unauthorized Tool Call"}}
    assert downstream.state.received == []


async def test_tool_that_only_contains_admin_is_not_blocked(gateway):
    response = await post(gateway, call("get_admin_contact"))

    assert "result" in response.json()


@pytest.mark.parametrize("params", [None, {}, {"name": 42}, {"name": None}, "admin_reset_key"])
async def test_tools_call_without_a_readable_name_is_refused(gateway, downstream, params):
    body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call"}
    if params is not None:
        body["params"] = params

    response = await post(gateway, body)

    assert response.json()["error"]["code"] == -32602
    assert downstream.state.received == []


async def test_batch_cannot_smuggle_an_admin_call(gateway, downstream):
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, call("admin_reset_key", request_id=2)]

    response = await post(gateway, batch)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    assert downstream.state.received == []


async def test_duplicate_keys_cannot_hide_an_admin_call(gateway, downstream):
    # a parser that keeps the first "name" would run admin_reset_key
    body = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","name":"get_weather"}}'

    response = await post(gateway, body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    assert downstream.state.received == []


async def test_oversized_body_is_refused(gateway, downstream):
    response = await post(gateway, b"x" * 1_000_001)

    assert response.status_code == 413
    assert downstream.state.received == []


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_get_and_delete_need_a_token_and_are_forwarded(gateway, downstream, method):
    refused = await gateway.request(method, "/mcp")
    allowed = await gateway.request(method, "/mcp", headers={"Authorization": f"Bearer {make_token()}"})

    assert refused.status_code == 401
    assert allowed.status_code in (200, 204)
    assert [r["method"] for r in downstream.state.received] == [method]
    assert "authorization" not in downstream.state.received[0]["headers"]


async def test_get_stream_has_no_read_timeout():
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, stream=httpx.ByteStream(b""), headers={"content-type": "text/event-stream"})

    app = create_app(downstream_url="http://downstream/mcp", jwt_secret=SECRET,
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        await client.get("/mcp", headers={"Authorization": f"Bearer {make_token()}"})

    assert seen["timeout"]["read"] is None  # an idle notification stream must stay open


async def test_tools_call_notification_is_refused(gateway, downstream):
    response = await post(gateway, {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_reset_key"}})

    assert response.status_code == 400
    assert downstream.state.received == []


# authentication

@pytest.mark.parametrize("authorization", [
    None,
    "",
    "Bearer",
    "Bearer ",
    "Basic dXNlcjpwYXNz",
    "Bearer not-a-jwt",
    f"Bearer {make_token(secret='some-other-secret-also-32-bytes-long')}",
    f"Bearer {make_token(exp=int(time.time()) - 10)}",
    f"Bearer {make_token(exp=None)}",
    f"Bearer {make_token(role=None)}",
    f"Bearer {make_token(role='superuser')}",
    f"Bearer {make_token(role=['admin'])}",
    f"Bearer {make_token(algorithm='none', secret=None)}",
    f"Bearer {make_token(algorithm='HS512')}",
])
async def test_bad_or_missing_tokens_get_401(gateway, downstream, authorization):
    headers = {"Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization

    response = await gateway.post("/mcp", content=json.dumps(call("get_weather")), headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.json()["error"]["code"] == -32000
    assert downstream.state.received == []


async def test_bearer_scheme_is_case_insensitive(gateway):
    response = await post(gateway, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, **{"Authorization": f"bearer {make_token()}"})

    assert response.status_code == 200


# wire format errors

@pytest.mark.parametrize("body, code", [
    ("{not json", -32700),
    ("", -32700),
    ('"just a string"', -32600),
    ("{}", -32600),
    ('{"jsonrpc": "1.0", "id": 1, "method": "ping"}', -32600),
    ('{"jsonrpc": "2.0", "id": 1, "method": 5}', -32600),
    ('{"jsonrpc": "2.0", "id": {"nested": 1}, "method": "ping"}', -32600),
    ('{"jsonrpc": "2.0", "id": true, "method": "ping"}', -32600),
    ("[" * 200_000 + "]" * 200_000, -32700),  # too deep for the parser
])
async def test_malformed_json_rpc_is_rejected_before_forwarding(gateway, downstream, body, code):
    response = await post(gateway, body)

    assert response.status_code == 400
    assert response.json() == {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": response.json()["error"]["message"]}}
    assert downstream.state.received == []


async def test_unreachable_downstream_is_a_clean_502():
    def refuse(request):
        raise httpx.ConnectError("connection refused to 10.0.0.5:9001")

    app = create_app(downstream_url="http://downstream/mcp", jwt_secret=SECRET,
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await post(client, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})

    assert response.status_code == 502
    assert response.json() == {"jsonrpc": "2.0", "id": 4, "error": {"code": -32603, "message": "Downstream MCP server unavailable"}}
    assert "10.0.0.5" not in response.text


def test_short_jwt_secret_is_refused_at_startup():
    with pytest.raises(ValueError):
        create_app(downstream_url="http://downstream/mcp", jwt_secret="short")
