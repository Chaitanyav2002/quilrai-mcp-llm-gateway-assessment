# Task 4: Token rate limiter and model fallback router

A model-routing module for an LLM gateway. Each tenant API key gets 50,000 tokens per minute, tracked in a sliding window in SQLite on disk. If the primary model answers `429` or takes longer than 3000 ms, the request goes to a backup provider. Every error comes back in one standard shape, with no upstream details.

## Run it

```bash
MOCK_MODE=slow uvicorn task4_model_router.mock_provider:app --port 9201 &   # primary, answers after 5 s
MOCK_MODE=ok   uvicorn task4_model_router.mock_provider:app --port 9202 &   # backup

export PRIMARY_URL=http://127.0.0.1:9201/v1/chat/completions PRIMARY_API_KEY=p
export SECONDARY_URL=http://127.0.0.1:9202/v1/chat/completions SECONDARY_API_KEY=s SECONDARY_MODEL=backup-model
export TENANT_API_KEYS=tenant-a,tenant-b
uvicorn task4_model_router.app:app_from_env --factory --port 9200 &

curl -si localhost:9200/v1/chat/completions -H "Authorization: Bearer tenant-a" \
  -H "Content-Type: application/json" -d '{"max_tokens":200,"messages":[{"role":"user","content":"hi"}]}'
# HTTP 200 after about 3.0 s, X-Gateway-Provider: secondary

pytest task4_model_router -q    # 41 tests
```

## Files

| File | What it does |
|---|---|
| `limiter.py` | `TokenRateLimiter`: sliding window in SQLite with reserve, settle and release |
| `router.py` | `ModelRouter`: reserves tokens, calls the primary with a 3 s deadline, falls back, settles real usage |
| `app.py` | FastAPI endpoint: checks the key against `TENANT_API_KEYS`, validates the body, turns every failure into the standard error |

## Rate limiting

Only keys listed in `TENANT_API_KEYS` are accepted. Otherwise any new string would be a fresh 50k budget on the gateway's own provider keys.

Each request is one row in `usage(tenant, ts, tokens)`. The tenant is the SHA-256 of the API key, so raw keys never touch the disk.

1. **Reserve** before calling any model. The estimate is the prompt size (about 4 characters per token) plus `max_tokens` (default 1024), which is the most the request can cost.
2. In one `BEGIN IMMEDIATE` transaction: delete rows older than 60 s (eviction), add up the tenant's remaining rows, and insert the new row only if it fits. Otherwise return `429` with a `Retry-After` header that says when enough old usage will have left the window.
3. **Settle** once the provider answers: the row is changed to `usage.total_tokens`. A request that reserved 1,224 tokens but used 20 frees the difference straight away.
4. **Release** the row if both providers fail, so the tenant is not charged for an outage.

**Why `BEGIN IMMEDIATE`.** Two requests must not both read "45,000 used" and both add 5,000. `IMMEDIATE` takes SQLite's write lock before the read, so the check and the insert act as one step. The lock lives in the database file, so this holds across several gateway processes sharing the file, not just across coroutines in one process. The test runs 40 reservations of 5k from 16 threads through 4 separate limiter objects, and exactly 10 succeed.

**Why rows instead of a counter.** A fixed-window counter lets a tenant spend 50k at 0:59 and another 50k at 1:01. Rows give an exact sliding window, and the window only ever holds one minute of a tenant's requests, so the table stays small.

The sqlite calls are blocking, so the router runs them with `asyncio.to_thread` to keep the event loop free while it waits on the database.

## Fallback

| Primary result | What happens |
|---|---|
| 200 with a JSON object | Returned, `X-Gateway-Provider: primary` |
| 429, or no answer within 3000 ms | Fall back to the secondary (the task's two cases) |
| 5xx, 401/403/404, connection error, reply that is not JSON | Also fall back: the primary is down or misconfigured, which is not the client's fault |
| 400 or 422 (the request itself is bad) | **No** fallback: the backup would reject it too. Returned as `upstream_rejected_request` |

**The timeout race.** The primary call runs inside `asyncio.timeout(3.0)`. At the deadline the request is *cancelled*, not just ignored, so a reply that arrives at 3.1 s can never be used or counted alongside the secondary's. The test checks that the slow handler actually receives the cancellation, and that 10 concurrent requests with a slow primary all finish in about 0.3 s total (the test timeout) rather than one after another. The secondary has its own timeout (10 s by default) so it cannot hang a request either.

The backup can take a different model name (`SECONDARY_MODEL`), since a backup provider rarely serves the same model.

## Error payload

Every failure, including unexpected bugs, looks like this:

```json
{"error": {"type": "gateway_error", "code": "providers_unavailable",
           "message": "All model providers are unavailable, please retry", "request_id": "3f9c0a1b2d4e5f60"}}
```

Codes: `missing_api_key` / `invalid_api_key` (401), `invalid_request` (400), `request_too_large` (400 over the token limit, 413 over 1 MB), `rate_limited` (429 + `Retry-After`), `upstream_rejected_request` (400/502), `providers_unavailable` (503), `internal_error` (500).

Upstream bodies, tracebacks and exception text are never sent to the client. They go to the server log next to the same `request_id`, so a support ticket can be traced. The tests feed real-looking Python tracebacks and internal paths through the providers and through a crashing limiter, and check that none of it reaches the response.

## Tests and what they prove

- Window maths: sliding, eviction (the expired row is deleted, not just skipped), `Retry-After` accuracy, settle, release, per-tenant isolation, survival across a restart, no raw keys on disk.
- Concurrency: no overshoot with many threads and processes-style separate connections.
- Fallback on 429, timeout, 5xx, connection errors and garbage replies, and no fallback on a 400.
- The slow primary is cancelled at the deadline, and concurrent requests time out in parallel.
- Every error matches the standard shape and leaks nothing.
- I broke the code eight ways to make sure the tests can fail: no transaction, no eviction, no refund, no settle, no timeout, failing over on any error, leaking the upstream text, and a deferred `BEGIN`. All but the last made the matching tests fail. A deferred `BEGIN` happens to be safe here only because the first statement is a write; `IMMEDIATE` keeps it safe if the statements are ever reordered.

## Not included

- Token counts are estimated at about 4 characters per token. A real deployment would use the model's tokenizer (for example `tiktoken`). Settling to the provider's real `usage` keeps the window accurate either way.
- A request can still go over the limit after the fact, if the model writes more than the estimate allowed for. The extra usage counts against the next minute.
- Non-streaming only. A streaming version would settle when the final usage chunk arrives.
- No circuit breaker. After repeated primary failures, a breaker would skip straight to the secondary instead of waiting 3 s each time.
