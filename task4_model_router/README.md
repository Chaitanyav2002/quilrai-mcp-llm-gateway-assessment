# Task 4: Token rate limiter and model fallback

A routing module for an LLM gateway. Each tenant API key gets 50,000 tokens per minute, tracked in a sliding window in SQLite on disk. If the primary model answers 429 or takes longer than 3000 ms, the request goes to a backup provider. Every error comes back in one fixed shape with no upstream details.

## Run

```bash
MOCK_MODE=slow uvicorn task4_model_router.mock_provider:app --port 9201 &   # primary, answers after 5 s
MOCK_MODE=ok   uvicorn task4_model_router.mock_provider:app --port 9202 &   # backup

export PRIMARY_URL=http://127.0.0.1:9201/v1/chat/completions PRIMARY_API_KEY=p
export SECONDARY_URL=http://127.0.0.1:9202/v1/chat/completions SECONDARY_API_KEY=s SECONDARY_MODEL=backup-model
export TENANT_API_KEYS=tenant-a,tenant-b
uvicorn task4_model_router.app:app_from_env --factory --port 9200 &

curl -si localhost:9200/v1/chat/completions -H "Authorization: Bearer tenant-a" \
  -H "Content-Type: application/json" -d '{"max_tokens":200,"messages":[{"role":"user","content":"hi"}]}'
# HTTP 200 after about 3 s, X-Gateway-Provider: secondary

pytest task4_model_router -q    # 58 tests
```

| File | Job |
|---|---|
| `limiter.py` | `TokenRateLimiter`: the sliding window in SQLite (reserve, settle, release) |
| `router.py` | `ModelRouter`: reserve tokens, call the primary with a 3 s deadline, fall back, record real usage |
| `app.py` | The HTTP endpoint: checks the key, validates the body, turns every failure into the standard error |

## Rate limiting

Only keys listed in `TENANT_API_KEYS` are accepted. Without that list, any new string would be a fresh 50k budget on the gateway's own provider keys.

Each request is one row in `usage(tenant, ts, tokens)`. The tenant is a SHA-256 of the API key, so raw keys are never written to disk.

1. Reserve tokens before calling a model. The estimate is the size of the whole body, tools included (about 4 characters per token for English, 1 per character for other scripts) plus `max_completion_tokens` or `max_tokens` (1024 if neither is set) for each of the `n` answers. When the client sets a limit, that is the most the request can cost.
2. In one `BEGIN IMMEDIATE` transaction: delete rows older than 60 s, add up the tenant's remaining tokens, and insert the new row only if it fits. If it doesn't, return 429 with a `Retry-After` that says when enough old usage will have dropped out.
3. Settle when the model answers: the row is updated to the real `usage.total_tokens`, so a request that reserved 1,224 tokens but used 20 gives the rest back right away.
4. Release the row if both providers fail, so the tenant isn't charged for an outage.

`BEGIN IMMEDIATE` takes the write lock before the read, so two requests can't both see "45,000 used" and both add 5,000. The lock is in the database file, which means this also works with several gateway processes sharing it. The test fires 40 requests of 5k from 16 threads through 4 separate limiters, and exactly 10 get through.

I store one row per request rather than a counter per minute. A per-minute counter lets a tenant spend 50k at 0:59 and another 50k at 1:01.

The SQLite calls block, so the router runs them in a thread (`asyncio.to_thread`) and the event loop stays free.

## Fallback

| Primary result | What happens |
|---|---|
| 200 with a JSON object | Returned, `X-Gateway-Provider: primary` |
| 429, or no answer in 3000 ms | Go to the backup (the two cases in the task) |
| 5xx, 401/403/404, connection error, a reply that isn't JSON | Also go to the backup: the primary is down or misconfigured, which isn't the client's fault |
| 400 or 422 | No fallback. The request itself is bad and the backup would refuse it too. |

The primary call runs inside `asyncio.timeout(3.0)`. At the deadline the request is cancelled, not just ignored, so a reply that turns up at 3.1 s can never be used or counted twice. The backup has its own timeout (10 s by default) so it can't hang a request either. It can also use a different model name (`SECONDARY_MODEL`).

## Errors

Every failure, including unexpected bugs, looks like this:

```json
{"error": {"type": "gateway_error", "code": "providers_unavailable",
           "message": "All model providers are unavailable, please retry", "request_id": "3f9c0a1b2d4e5f60"}}
```

Codes: `missing_api_key` / `invalid_api_key` (401), `invalid_request` (400), `request_too_large` (400 over the token limit, 413 over 1 MB), `rate_limited` (429 + `Retry-After`), `upstream_rejected_request` (400), `providers_unavailable` (503), `internal_error` (500).

Upstream error bodies, tracebacks and exception text never reach the client. They go to the server log with the same `request_id`, so a support ticket can be traced.

## Tests

- The window itself: sliding, expired rows actually deleted, correct `Retry-After`, settle, release, separate tenants, surviving a restart, no raw keys on disk.
- No overshoot when many threads reserve at once.
- Fallback on 429, timeout, 5xx, 401/403/404, connection errors and bad replies, and no fallback on 400 or 422.
- A slow primary really is cancelled at the deadline, and 10 concurrent requests time out together rather than one after another.
- Every error has the standard shape, and fake tracebacks and internal paths from upstream never show up in it. Bodies with `NaN`, `1e999`, very deep nesting or a bad `n` get a 400, not a 500.
- If the database fails after the model has answered, the client still gets the answer, and a failed refund doesn't hide the real error.
- I broke the code on purpose (no transaction, no eviction, no refund, no settle, no timeout, falling back on every error, leaking upstream text), and each change failed a test. A deferred `BEGIN` happened to pass, but only because the first statement is a write. `IMMEDIATE` keeps it safe if someone reorders the statements.

## Not included

- Tokens are estimated from the body size, at about 4 English characters or 1 character of other scripts each. A real setup would use the model's tokenizer. Recording the provider's real usage keeps the window accurate either way.
- If the client sets no output limit, the model can write more than the 1024 tokens assumed. Real usage is still recorded, so the extra counts against the next minute.
- The 3 s deadline includes waiting for a free connection. With more than 100 requests in flight, a full connection pool can look like a slow primary. A bigger pool per provider would fix that.
- Non-streaming only. A streaming version would record usage when the final usage chunk arrives.
- No circuit breaker. After many primary failures, a breaker would go straight to the backup instead of waiting 3 s each time.
