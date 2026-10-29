import pytest

from task3_pii_stream_guard.redactor import StreamRedactor

CASES = [
    ("Email me at john.doe@example.com today.", "Email me at [REDACTED] today."),
    ("Reach ops+alerts@mail.corp.io, thanks", "Reach [REDACTED], thanks"),
    ("SSN: 123-45-6789.", "SSN: [REDACTED]."),
    ("SSN 123 45 6789 ok", "SSN [REDACTED] ok"),
    ("Card 4111 1111 1111 1111 on file", "Card [REDACTED] on file"),
    ("Card 5555-5555-5555-4444", "Card [REDACTED]"),          # PII right at the end of the stream
    ("Amex 378282246310005 charged", "Amex [REDACTED] charged"),
    ("Fullwidth ４１１１ １１１１ １１１１ １１１１ too", "Fullwidth [REDACTED] too"),
    ("a@b.co and 123-45-6789 and 4111111111111111.", "[REDACTED] and [REDACTED] and [REDACTED]."),
    ("qty 2 4111 1111 1111 1111 done", "qty 2 [REDACTED] done"),        # stray digit before the card
    ("call 555 4111-1111-1111-1111", "call 555 [REDACTED]"),
    ("SSN 123-45-6789 2024-01-15", "SSN [REDACTED] 2024-01-15"),     # date right after the SSN
    ("Diners 3056 930902 5904 ok", "Diners [REDACTED] ok"),
    # Must NOT be redacted
    ("Order 1234 5678 9012 3456 shipped", "Order 1234 5678 9012 3456 shipped"),   # fails Luhn
    ("Call 555-123-4567 now", "Call 555-123-4567 now"),                           # phone number
    ("Version 1.2.3 of the build", "Version 1.2.3 of the build"),
    ("Plain prose with no secrets at all.", "Plain prose with no secrets at all."),
    ("", ""),
]


def stream(redactor: StreamRedactor, chunks: list[str]) -> list[str]:
    out, history = "", []
    for chunk in chunks:
        out += redactor.feed(chunk)
        history.append(out)
    out += redactor.flush()
    history.append(out)
    return history


@pytest.mark.parametrize("text, expected", CASES)
def test_every_two_chunk_split_gives_the_same_result(text, expected):
    for i in range(len(text) + 1):
        history = stream(StreamRedactor(), [text[:i], text[i:]])
        assert history[-1] == expected, f"split at {i}: {text[:i]!r} | {text[i:]!r}"
        # output so far must always be a prefix of the final result, so nothing leaked early
        assert all(expected.startswith(partial) for partial in history), f"split at {i}"


@pytest.mark.parametrize("text, expected", CASES)
def test_one_character_at_a_time(text, expected):
    history = stream(StreamRedactor(), list(text))

    assert history[-1] == expected
    assert all(expected.startswith(partial) for partial in history)


def test_prose_is_held_back_by_at_most_the_word_in_progress():
    redactor = StreamRedactor()
    tokens = ["The", " quick", " brown", " fox", " jumps"]
    out = ""
    for i, token in enumerate(tokens):
        out += redactor.feed(token)
        sent_so_far = "".join(tokens[: i + 1])
        last_word = token.strip()
        assert out == sent_so_far[: len(sent_so_far) - len(last_word)]
    assert out + redactor.flush() == "The quick brown fox jumps"


def test_text_after_a_space_is_released_immediately():
    redactor = StreamRedactor()

    assert redactor.feed("Hello world. ") == "Hello world. "
    assert redactor.held == 0


def test_memory_stays_bounded_on_a_long_stream_of_prose():
    redactor = StreamRedactor()
    sentence = "Contact support if the build fails again. "
    peak = 0
    for _ in range(25_000):  # about 1 MB
        redactor.feed(sentence)
        peak = max(peak, redactor.held)

    assert peak < 20


def test_memory_stays_bounded_even_on_one_endless_word():
    redactor = StreamRedactor(max_hold=256)
    emitted = sum(len(redactor.feed("a" * 1000)) for _ in range(1000))

    assert redactor.held <= 256
    assert emitted + len(redactor.flush()) == 1_000_000  # nothing lost


def test_flush_redacts_what_is_left():
    redactor = StreamRedactor()

    assert redactor.feed("send it to jane@example.org") == "send it to "
    assert redactor.flush() == "[REDACTED]"
    assert redactor.held == 0
