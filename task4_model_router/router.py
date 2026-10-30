"""Sends a completion to the primary model and falls back to the secondary one."""

import asyncio
import json
import logging
from dataclasses import dataclass

import httpx

from task4_model_router.limiter import RateLimited, RequestTooLarge, TokenRateLimiter

log = logging.getLogger("model-router")

PRIMARY_TIMEOUT = 3.0
DEFAULT_MAX_TOKENS = 1024


@dataclass
class Provider:
    name: str
    url: str
    api_key: str
    timeout: float = PRIMARY_TIMEOUT
    model: str | None = None  # the backup may need a different model name


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


class Failover(Exception):
    pass


def estimate_tokens(body: dict) -> int:
    # everything sent counts, tools too: about 4 English characters per token, other scripts about 1 per character.
    # Plus the most the model may write for each of the n answers.
    text = json.dumps(body, ensure_ascii=False)
    ascii_chars = len(text.encode("ascii", "ignore"))
    prompt = ascii_chars // 4 + len(text) - ascii_chars
    # the larger limit, a provider may honour either one
    output = max(body.get("max_completion_tokens") or 0, body.get("max_tokens") or 0) or DEFAULT_MAX_TOKENS
    return prompt + output * (body.get("n") or 1)


class ModelRouter:
    def __init__(self, primary: Provider, secondary: Provider, limiter: TokenRateLimiter, client: httpx.AsyncClient):
        self.providers = (primary, secondary)
        self.limiter = limiter
        self.client = client

    async def complete(self, api_key: str, body: dict) -> tuple[dict, str]:
        # sqlite calls block, so they run in a thread to keep the event loop free
        try:
            reservation = await asyncio.to_thread(self.limiter.reserve, api_key, estimate_tokens(body))
        except RateLimited as exc:
            raise GatewayError(429, "rate_limited", "Token rate limit reached for this API key",
                               retry_after=exc.retry_after) from None
        except RequestTooLarge:
            raise GatewayError(400, "request_too_large", "Request needs more tokens than the per minute limit") from None

        try:
            data, name = await self._first_answer(body)
        except BaseException:
            await self._quietly(self.limiter.release, reservation)
            raise

        usage = data.get("usage")
        used = usage.get("total_tokens") if isinstance(usage, dict) else None
        if type(used) is int and used >= 0:
            await self._quietly(self.limiter.settle, reservation, used)
        return data, name

    async def _quietly(self, action, *args) -> None:
        # a database hiccup here must not cost the client an answer it already has, or hide the real error
        try:
            await asyncio.to_thread(action, *args)
        except Exception:
            log.exception("could not %s reservation %s", action.__name__, args[0])

    async def _first_answer(self, body: dict) -> tuple[dict, str]:
        for provider in self.providers:
            try:
                return await self._call(provider, body), provider.name
            except Failover as exc:
                log.warning("provider %s failed, trying next: %s", provider.name, exc)
        raise GatewayError(503, "providers_unavailable", "All model providers are unavailable, please retry")

    async def _call(self, provider: Provider, body: dict) -> dict:
        payload = {**body, "model": provider.model} if provider.model else body
        try:
            # cancels the request at the deadline, so a late reply is never used
            async with asyncio.timeout(provider.timeout):
                response = await self.client.post(
                    provider.url, json=payload, headers={"Authorization": f"Bearer {provider.api_key}"})
        except TimeoutError:
            raise Failover(f"no answer in {provider.timeout}s") from None
        except httpx.TransportError as exc:
            raise Failover(repr(exc)) from None

        if response.status_code in (400, 422):
            # the request itself is bad, the backup would refuse it too
            log.warning("provider %s rejected the request: %s %s", provider.name, response.status_code, response.text[:500])
            raise GatewayError(400, "upstream_rejected_request", "The model provider rejected the request")
        if response.status_code != 200:
            raise Failover(f"status {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise Failover("reply was not JSON") from None
        if not isinstance(data, dict):
            raise Failover("reply was not a JSON object")
        return data
