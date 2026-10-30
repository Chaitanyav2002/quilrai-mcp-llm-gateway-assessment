# Task 3: Streaming PII redaction

An OpenAI-compatible `POST /v1/chat/completions` endpoint. It sends the request to the model provider, reads the SSE stream that comes back, and replaces emails, SSNs and card numbers with `[REDACTED]` while the stream is still flowing.

## Run

```bash
uvicorn task3_pii_stream_guard.mock_provider:app --port 9101 &
export LLM_PROVIDER_URL=http://127.0.0.1:9101/v1/chat/completions LLM_PROVIDER_API_KEY=demo
uvicorn task3_pii_stream_guard.gateway:app_from_env --factory --port 9100 &

curl -N localhost:9100/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"mock","stream":true,"messages":[{"role":"user","content":"hi"}]}'

pytest task3_pii_stream_guard -q    # 104 tests
```

Any OpenAI-compatible provider works: set `LLM_PROVIDER_URL` and the key. The mock splits the PII across chunks on purpose (`"jo"`, `"hn.doe@exam"`, `"ple.com"`).

## How it works

The tricky case is a value split across chunks. Send each chunk as it arrives and `"Card 4111 1111"` is gone before `" 1111 1111"` shows up. Buffer the whole answer and the user waits for all of it before seeing anything.

So `StreamRedactor` (in `redactor.py`) holds back only the text that could still be part of an email, card or SSN, and sends the rest right away:

1. Add the new chunk to a small buffer.
2. Find the last character that can't be inside any of them: a comma, a line break, most punctuation, or a space that isn't between two digit groups. Everything up to there is final.
3. Redact that part and send it. Keep the rest.
4. When the stream ends, redact and send whatever is left.

In normal text the held part is just the word being typed, so the output trails the model by about one token. A number with spaces in it, like a card, is held until a word or punctuation comes after it.

The cut always falls where no value can cross it, so each piece is redacted exactly as the full text would be. A test checks this on 5,000 random texts split at random points, and I ran 200,000 more by hand.

If 512 characters build up with no safe place to cut (a long run of dashes or spaced numbers), everything but the last 256 is sent. That cut never goes through a card, SSN or email. If one crosses the cut point, the cut moves to just before it, so it stays held. If it is longer than 256 characters (an email can start at the beginning of a long word), the cut moves to just after it instead, and it goes out redacted. Either way the value that crosses is seen whole, since 256 characters follow the cut point.

Details:
- Cards must look like a real card (13 to 19 digits in a row, groups of 4 ending in a shorter group such as `4-4-4-4-3`, or the Amex/Diners `4-6-5` and `4-6-4` groups) and pass the Luhn check, so order numbers and phone numbers are left alone.
- Every run of neighbouring digit groups is checked, so a card or SSN right next to another number is still found. My first version picked one reading of a long number before running Luhn. `IDs 2024 0001 0002 123-45-6789` was read as a 15-digit card, that failed Luhn, and the SSN at the end went out unredacted.
- Any one dash, hyphen, minus sign or space (no-break, narrow, thin and ideographic ones too) can sit between groups. Fullwidth digits (`４１１１ …`) are caught too.
- Emails can have accented letters (`jöhn@…`), typed either as one character or as a letter plus a combining mark. Only Latin letters count, so an email written straight into Chinese text, with no spaces around it, doesn't take the sentence with it.
- `content`, `refusal` and tool call arguments are all redacted, each with its own redactor per choice. A model can't slip an email out through a tool call, and `n > 1` streams don't mix.
- Held text goes out with the `finish_reason` chunk, before `[DONE]`. If the provider stops without a finish, the held text is still sent instead of being dropped.
- Memory is bounded: the hold never goes past 512 characters. Each chunk only looks at the held text plus the new chunk.

## Performance

On a laptop, with every kind of PII in every sentence:

| Chunk size | Throughput | Time per chunk |
|---|---|---|
| 4 chars | 2.3 MB/s | 1.8 µs |
| 16 chars | 4.6 MB/s | 3.5 µs |
| 64 chars | 7.4 MB/s | 8.7 µs |

A model streams a few KB per second, so this adds no visible delay. Two cases were slow at first. A very long word made the email pattern retry from every position, fixed by only starting an email at the beginning of a word. A long list of numbers made the card search slow down with the square of its length, fixed by never checking more than 5 groups at once (no card layout has more). The slowest input I found now, 1 MB of `0 0 0 …` sent 4 characters at a time, takes 1.9 s.

## Tests

- Every sample is split at every possible position, and also fed one character at a time. The result is always the same, and what has been sent so far is always the start of the final answer, so nothing leaks early.
- 5,000 random texts built from PII-like pieces, split at random points, give the same result as redacting the whole text at once. My earlier version failed this on about 1 text in 100.
- Over real HTTP, with both servers running under uvicorn, the first text arrives within 0.6 s while the whole stream takes 1.5 s.
- A card, SSN or email buried in 300 characters of dashes, letters or spaced digits on each side never leaks, not even in part, wherever the forced cut lands.
- Memory stays bounded for 1 MB of normal text, 1 MB of one endless word, and an email that never ends.
- A provider 429 is passed on with its `Retry-After`, a 5xx becomes a 502, and neither leaks the provider's error text. If the stream breaks partway, the client gets a clean error event. A body the provider couldn't take (`NaN`, `1e999`, very deep nesting) gets a 400. The gateway sends its own provider key, never the client's.
- I broke the code on purpose (no redaction, no flush on finish, buffering the whole stream, no hold limit, a forced cut through a value, no Luhn, no SSN check, cards checked only from the start of a number, every space treated as a safe cut, no error event) and each change failed a test.

## Not included

- Only the response is redacted. Redacting the prompt would use the same `StreamRedactor`.
- Non-streaming requests get a 400. Supporting them is one `feed` plus `flush`.
- There is no client auth: the gateway spends its own provider key, so in production it would sit behind auth and a limiter like the one in Task 4. Request bodies are capped at 1 MB.
- Regex only finds structured PII. Names and addresses would need an NER model, which adds latency to every chunk.
- Emails are matched with Latin letters only, so an address like `иван@почта.рф` is not caught.
- Groups must be joined by a single separator, so a card typed with double spaces (`4111  1111 …`) is not caught.
