# Task 3: LLM gateway with streaming PII redaction

An OpenAI-compatible `POST /v1/chat/completions` endpoint. It forwards the request to a model provider, reads the SSE stream that comes back, and replaces emails, SSNs and card numbers with `[REDACTED]` while the stream is still flowing.

## Run it

```bash
uvicorn task3_pii_stream_guard.mock_provider:app --port 9101 &
export LLM_PROVIDER_URL=http://127.0.0.1:9101/v1/chat/completions LLM_PROVIDER_API_KEY=demo
uvicorn task3_pii_stream_guard.gateway:app_from_env --factory --port 9100 &

curl -N localhost:9100/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"mock","stream":true,"messages":[{"role":"user","content":"hi"}]}'

pytest task3_pii_stream_guard -q    # 55 tests
```

Any OpenAI-compatible provider works: point `LLM_PROVIDER_URL` at it and set the key. The mock streams a reply with the PII split across chunks on purpose (`"jo" | "hn.doe@exam" | "ple.com"`).

## How the redaction works

The hard case is a value split across chunks. If each chunk is sent as soon as it arrives, `"Card 4111 1111"` goes out before `" 1111 1111"` shows up to complete the card number. If the whole response is buffered, time to first token is as slow as the full answer.

So the redactor (`redactor.py`) holds back **only the text that could still become a match**, and sends everything before it straight away:

1. Append the new chunk to a small pending buffer.
2. Scan it with partial matching (the `regex` module's `partial=True`). A match at the end of the buffer that is incomplete (`john@exa`), or complete but able to grow (`john@example.co` before the `m`), marks where the hold starts.
3. Redact the complete matches before that point and send that text. Keep the rest.
4. At the end of the stream, flush and redact whatever is left.

In normal prose the held part is just the word in progress, so the output trails the provider by about one token. The live demo shows events coming through at the provider's own pace.

**Other details**
- Card numbers must match a real card layout (13 to 19 digits in a row, `4-4-4-x` groups, or the Amex/Diners `4-6-5` / `4-6-4` groups) and pass the Luhn check, so an order number like `1234 5678 9012 3456` is left alone. The code review caught that my first, looser pattern let `qty 2 4111 1111 1111 1111` swallow the stray `2`, fail Luhn, and pass the real card through.
- A complete match that could still grow is held until the next character settles it. The tests caught this one: without it, `a@mail.corp` followed by `.io` sent `[REDACTED].io`.
- Fullwidth digits (`４１１１ …`) are caught too.
- Memory: the hold is capped at 256 characters. No email or card number is that long, so one endless "word" cannot grow the buffer without limit. Each chunk scans only the held text plus the new chunk, never the whole response.
- `content`, `refusal` and every `tool_calls[].function.arguments` stream are redacted, each with its own redactor per choice, so a model cannot route PII through a tool call and `n > 1` streams do not mix.
- Held text is sent with the `finish_reason` chunk, before `[DONE]`. If the provider ends without a finish, the held text is still flushed rather than dropped.

## Performance

Measured on a laptop, with every PII type in every sentence:

| Chunk size | Throughput | Time per chunk |
|---|---|---|
| 4 chars | 1.7 MB/s | 2.3 µs |
| 16 chars | 3.0 MB/s | 5.4 µs |
| 64 chars | 4.8 MB/s | 13.4 µs |

An LLM streams a few KB per second, so redaction adds no noticeable delay. One pattern was quadratic: a long run of letters made the email pattern retry from every position. Letting an email start only at the beginning of a word made it linear, and a 1 MB single word went from 9 s to 0.3 s.

## Tests and what they prove

- Every sample is split at **every possible position**, and also fed one character at a time. The output is always the same, and the text emitted so far is always a prefix of the final redacted text, so nothing goes out early that later turns out to be PII.
- Over real HTTP (both servers running under uvicorn), the first text arrives within 0.6 s while the full stream takes 1.5 s.
- Memory stays bounded for 1 MB of prose (peak hold under 20 characters) and for 1 MB of one endless word.
- Provider 429 is passed on with `Retry-After`, a provider 5xx becomes a 502, and neither leaks the provider's error text. The gateway sends its own provider key, not the client's.
- To make sure the tests can fail, I broke the code six ways: no redaction, no flush on finish, buffering the whole stream, no hold cap, no continuation check, no Luhn. Each one made the matching tests fail.

## Not included

- Only the response is redacted. Redacting the prompt on the way in would use the same `StreamRedactor` with a single `flush()`.
- Non-streaming requests are refused with a 400 to keep the scope tight. Supporting them is one `feed` plus `flush` over the full text.
- No client auth: the gateway spends its own provider key, so in production it sits behind auth and a limiter like Task 4's. Request bodies are capped at 1 MB.
- Regex catches structured PII only. Names and addresses would need an NER model, which costs latency on every chunk.
