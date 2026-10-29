# Task 3: Streaming PII redaction

An OpenAI-compatible `POST /v1/chat/completions` endpoint. It sends the request to the model provider, reads the SSE stream that comes back, and replaces emails, SSNs and card numbers with `[REDACTED]` while the stream is still flowing.

## Run

```bash
uvicorn task3_pii_stream_guard.mock_provider:app --port 9101 &
export LLM_PROVIDER_URL=http://127.0.0.1:9101/v1/chat/completions LLM_PROVIDER_API_KEY=demo
uvicorn task3_pii_stream_guard.gateway:app_from_env --factory --port 9100 &

curl -N localhost:9100/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"mock","stream":true,"messages":[{"role":"user","content":"hi"}]}'

pytest task3_pii_stream_guard -q    # 59 tests
```

Any OpenAI-compatible provider works: set `LLM_PROVIDER_URL` and the key. The mock splits the PII across chunks on purpose (`"jo"`, `"hn.doe@exam"`, `"ple.com"`).

## How it works

The tricky case is a value split across chunks. Send each chunk as it arrives and `"Card 4111 1111"` is gone before `" 1111 1111"` shows up. Buffer the whole answer and the user waits for all of it before seeing anything.

So `StreamRedactor` (in `redactor.py`) holds back only the part that could still turn into a match, and sends the rest right away:

1. Add the new chunk to a small buffer.
2. Search it with partial matching from the `regex` module (`partial=True`). A match at the end of the buffer that isn't finished yet (`john@exa`), or that is finished but could still grow (`john@example.co` before the `m`), is where the hold starts.
3. Redact the complete matches before that point and send that text.
4. When the stream ends, redact and send whatever is left.

In normal text the held part is just the word being typed, so the output trails the model by about one token.

Details:
- Cards must look like a real card (13 to 19 digits in a row, `4-4-4-x` groups, or the Amex/Diners `4-6-5` and `4-6-4` groups) and pass the Luhn check, so order numbers and phone numbers are left alone. The code review caught that my first, looser pattern turned `qty 2 4111 1111 1111 1111` into one 17-digit number, which failed Luhn and let the real card through.
- A finished match that could still grow is held until the next character decides it. Without that, `a@mail.corp` followed by `.io` came out as `[REDACTED].io`.
- Fullwidth digits (`４１１１ …`) are caught too.
- `content`, `refusal` and tool call arguments are all redacted, each with its own redactor per choice. A model can't slip an email out through a tool call, and `n > 1` streams don't mix.
- Held text goes out with the `finish_reason` chunk, before `[DONE]`. If the provider stops without a finish, the held text is still sent instead of being dropped.
- Memory is bounded: the hold never goes past 256 characters, which is longer than any email or card. Each chunk only rescans the held text plus the new chunk.

## Performance

On a laptop, with every kind of PII in every sentence:

| Chunk size | Throughput | Time per chunk |
|---|---|---|
| 4 chars | 1.7 MB/s | 2.3 µs |
| 16 chars | 3.0 MB/s | 5.4 µs |
| 64 chars | 4.8 MB/s | 13.4 µs |

A model streams a few KB per second, so this adds no visible delay. One case was slow at first: a very long word made the email pattern retry from every position. Only starting an email at the beginning of a word fixed it, taking a 1 MB single word from 9 s to 0.3 s.

## Tests

- Every sample is split at every possible position, and also fed one character at a time. The result is always the same, and what has been sent so far is always the start of the final answer, so nothing leaks early.
- Over real HTTP, with both servers running under uvicorn, the first text arrives within 0.6 s while the whole stream takes 1.5 s.
- Memory stays bounded for 1 MB of normal text and for 1 MB of one endless word.
- A provider 429 is passed on with its `Retry-After`, a 5xx becomes a 502, and neither leaks the provider's error text. The gateway sends its own provider key, never the client's.
- I broke the code six ways (no redaction, no flush on finish, buffering the whole stream, no hold limit, no grow check, no Luhn) and each one failed a test.

## Not included

- Only the response is redacted. Redacting the prompt would use the same `StreamRedactor`.
- Non-streaming requests get a 400. Supporting them is one `feed` plus `flush`.
- There is no client auth: the gateway spends its own provider key, so in production it would sit behind auth and a limiter like the one in Task 4. Request bodies are capped at 1 MB.
- Regex only finds structured PII. Names and addresses would need an NER model, which adds latency to every chunk.
