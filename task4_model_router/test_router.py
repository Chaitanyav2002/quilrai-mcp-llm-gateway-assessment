import asyncio
import json
import time

import httpx
import pytest

from task4_model_router.app import create_app
from task4_model_router.limiter import TokenRateLimiter
from task4_model_router.router import PRIMARY_TIMEOUT, ModelRouter, Provider

BODY = {"model": "gpt-x", "max_tokens": 200, "messages": [{"role": "user", "content": "hello"}]}
KEY = {"Authorization": "Bearer tenant-key-1"}


def reply(name, tokens=120):
    return httpx.Response(200, json={"id": f"cmpl-{name}", "object": "chat.completion",
                                     "choices": [{"index": 0, "message": {"role": "assistant", "content": f"hi from {name}"}}],
                                     "usage": {"total_tokens": tokens}})


class Upstream:

    def __init__(self, primary, secondary=lambda request: reply("secondary")):
        self.handlers = {"primary": primary, "secondary": secondary}
        self.calls = {"primary": [], "secondary": []}

    async def __call__(self, request):
        name = request.url.host
        self.calls[name].append(json.loads(request.content))
        result = self.handlers[name](request)
        return await result if asyncio.iscoroutine(result) else result


@pytest.fixture
def make(tmp_path):
    def build(primary, secondary=lambda request: reply("secondary"), timeout=0.3):
        upstream = Upstream(primary, secondary)
        limiter = TokenRateLimiter(tmp_path / "router.db", limit=50_000)
        router = ModelRouter(
            Provider("primary", "http://primary/v1/chat/completions", "pk", timeout=timeout),
            Provider("secondary", "http://secondary/v1/chat/completions", "sk", timeout=5, model="backup-model"),
            limiter,
            httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        )
        app = create_app(router, api_keys={"tenant-key-1", "tenant-key-2"})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
        return client, upstream, limiter
    return build


async def complete(client, body=BODY, headers=KEY):
    return await client.post("/v1/chat/completions", json=body, headers=headers)


def is_gateway_error(response, status, code):
    error = response.json()["error"]
    assert response.status_code == status
    assert error["type"] == "gateway_error" and error["code"] == code
    assert error["request_id"] == response.headers["x-request-id"]
    assert set(error) == {"type", "code", "message", "request_id"}
    return error


# routing and fallback

def test_primary_timeout_is_3_seconds():
    assert PRIMARY_TIMEOUT == 3.0


async def test_primary_answers(make):
    client, upstream, _ = make(lambda r: reply("primary"))
    response = await complete(client)

    assert response.json()["id"] == "cmpl-primary"
    assert response.headers["x-gateway-provider"] == "primary"
    assert upstream.calls["secondary"] == []


async def test_primary_429_falls_back_to_secondary(make):
    client, upstream, _ = make(lambda r: httpx.Response(429, json={"error": "slow down"}))
    response = await complete(client)

    assert response.json()["id"] == "cmpl-secondary"
    assert response.headers["x-gateway-provider"] == "secondary"
    assert upstream.calls["secondary"][0]["model"] == "backup-model"


async def test_slow_primary_is_cancelled_at_the_timeout(make):
    state = {"cancelled": False}

    async def slow(request):
        try:
            await asyncio.sleep(2)
            return reply("primary")
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    client, _, _ = make(slow, timeout=0.3)
    start = time.perf_counter()
    response = await complete(client)
    elapsed = time.perf_counter() - start

    assert response.headers["x-gateway-provider"] == "secondary"
    assert 0.3 <= elapsed < 0.8
    assert state["cancelled"]  # the late reply can never be used or counted


async def test_concurrent_requests_time_out_in_parallel(make):
    async def slow(request):
        await asyncio.sleep(2)
        return reply("primary")

    client, _, _ = make(slow, timeout=0.3)
    start = time.perf_counter()
    responses = await asyncio.gather(*(complete(client) for _ in range(10)))

    assert time.perf_counter() - start < 1.0
    assert {r.headers["x-gateway-provider"] for r in responses} == {"secondary"}


@pytest.mark.parametrize("failure", [
    lambda r: httpx.Response(500, text="Traceback (most recent call last): File /srv/model.py"),
    lambda r: httpx.Response(503),
    lambda r: httpx.Response(200, text="<html>proxy error</html>"),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused by 10.0.0.7")),
])
async def test_other_primary_outages_also_fall_back(make, failure):
    client, _, _ = make(failure)
    response = await complete(client)

    assert response.headers["x-gateway-provider"] == "secondary"


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_primary_side_errors_fall_back(make, status):
    # a revoked key or a wrong URL on the primary is not the client's fault
    client, _, _ = make(lambda r: httpx.Response(status, json={"error": "invalid api key"}))
    response = await complete(client)

    assert response.headers["x-gateway-provider"] == "secondary"


async def test_primary_rejecting_the_request_is_not_retried_elsewhere(make):
    client, upstream, _ = make(lambda r: httpx.Response(400, json={"error": {"message": "unknown model gpt-x at /internal/v2"}}))
    response = await complete(client)

    is_gateway_error(response, 400, "upstream_rejected_request")
    assert "/internal" not in response.text
    assert upstream.calls["secondary"] == []


async def test_both_providers_down_gives_a_clean_error_and_refunds_tokens(make):
    traceback = "Traceback (most recent call last):\n  File \"/srv/app.py\", line 12\nKeyError: 'db'"
    client, _, limiter = make(lambda r: httpx.Response(500, text=traceback),
                              lambda r: httpx.Response(502, text=traceback))
    response = await complete(client)

    is_gateway_error(response, 503, "providers_unavailable")
    assert "Traceback" not in response.text and "/srv" not in response.text
    assert limiter.used("tenant-key-1") == 0


# rate limiting

async def test_null_max_tokens_uses_the_default(make):
    client, _, _ = make(lambda r: reply("primary"))
    response = await complete(client, body={**BODY, "max_tokens": None})

    assert response.status_code == 200


async def test_odd_usage_field_does_not_break_a_good_answer(make):
    odd = httpx.Response(200, json={"id": "cmpl-x", "choices": [], "usage": ["not", "a", "dict"]})
    client, _, limiter = make(lambda r: odd)
    response = await complete(client)

    assert response.status_code == 200
    assert limiter.used("tenant-key-1") > 0  # the estimate stays charged


async def test_usage_is_settled_to_what_the_provider_reports(make):
    client, _, limiter = make(lambda r: reply("primary", tokens=321))
    await complete(client)

    assert limiter.used("tenant-key-1") == 321


async def test_over_the_limit_gets_429_and_providers_are_not_called(make):
    client, upstream, limiter = make(lambda r: reply("primary"))
    limiter.reserve("tenant-key-1", 49_900)

    response = await complete(client)

    is_gateway_error(response, 429, "rate_limited")
    assert int(response.headers["retry-after"]) >= 59
    assert upstream.calls == {"primary": [], "secondary": []}


async def test_limit_is_per_api_key(make):
    client, _, limiter = make(lambda r: reply("primary"))
    limiter.reserve("tenant-key-1", 50_000)

    response = await complete(client, headers={"Authorization": "Bearer tenant-key-2"})

    assert response.status_code == 200


# request validation and sanitizing

@pytest.mark.parametrize("body, headers, status, code", [
    (BODY, {}, 401, "missing_api_key"),
    (BODY, {"Authorization": "Basic abc"}, 401, "missing_api_key"),
    (BODY, {"Authorization": "Bearer made-up-key"}, 401, "invalid_api_key"),
    ("x" * 1_000_001, KEY, 413, "request_too_large"),
    ("not json", KEY, 400, "invalid_request"),
    ([], KEY, 400, "invalid_request"),
    ({"max_tokens": 10}, KEY, 400, "invalid_request"),
    ({**BODY, "max_tokens": 0}, KEY, 400, "invalid_request"),
    ({**BODY, "max_tokens": "200"}, KEY, 400, "invalid_request"),
    ({**BODY, "stream": True}, KEY, 400, "invalid_request"),
    ({**BODY, "max_tokens": 60_000}, KEY, 400, "request_too_large"),
])
async def test_bad_requests_get_a_standard_error(make, body, headers, status, code):
    client, upstream, _ = make(lambda r: reply("primary"))
    if isinstance(body, str):
        response = await client.post("/v1/chat/completions", content=body, headers=headers)
    else:
        response = await complete(client, body=body, headers=headers)

    is_gateway_error(response, status, code)
    assert upstream.calls["primary"] == []


async def test_unexpected_failure_hides_internals(make, monkeypatch):
    client, _, limiter = make(lambda r: reply("primary"))

    def broken(*args):
        raise RuntimeError("disk full at /var/lib/gateway/router.db")

    monkeypatch.setattr(limiter, "reserve", broken)
    response = await complete(client)

    error = is_gateway_error(response, 500, "internal_error")
    assert "/var/lib" not in response.text and "RuntimeError" not in response.text
    assert error["message"] == "Unexpected gateway error"
