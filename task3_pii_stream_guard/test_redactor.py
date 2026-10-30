import random
import time

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
    ("IDs 2024 0001 0002 123-45-6789.", "IDs 2024 0001 0002 [REDACTED]."),   # SSN right after a long number
    ("Ref 1234 5678 9012 078-05-1120 end", "Ref 1234 5678 9012 [REDACTED] end"),
    ("Card 6221 2600 0000 0001 009 ok", "Card [REDACTED] ok"),               # 19 digits as 4-4-4-4-3
    ("Card 4111\u00a01111\u00a01111\u00a01111 ok", "Card [REDACTED] ok"),    # no-break spaces
    ("Mail jöhn@example.com now", "Mail [REDACTED] now"),
    ("Card 4111\u202f1111\u202f1111\u202f1111 ok", "Card [REDACTED] ok"),    # narrow no-break spaces
    ("SSN 123\u201345\u20136789 ok", "SSN [REDACTED] ok"),                    # en dashes
    ("请发邮件到john@example.com谢谢", "请发邮件到[REDACTED]谢谢"),              # no spaces around it in Chinese
    ("Card ４１１１\u3000１１１１\u3000１１１１\u3000１１１１ ok", "Card [REDACTED] ok"),   # ideographic space
    ("Card ４１１１\uff0d１１１１\uff0d１１１１\uff0d１１１１ ok", "Card [REDACTED] ok"),   # fullwidth hyphen
    ("Card 4111\u22121111\u22121111\u22121111 ok", "Card [REDACTED] ok"),        # minus sign
    ("SSN 123\u201045\u20106789 ok", "SSN [REDACTED] ok"),                            # hyphen
    ("SSN 123\u201445\u20146789 ok", "SSN [REDACTED] ok"),                            # em dash
    ("Mail john@exa\u0301mple.com now", "Mail [REDACTED] now"),                        # accent as a combining mark
    ("Mail jo\u0308hn@example.com now", "Mail [REDACTED] now"),
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


def one_shot(text: str) -> str:
    redactor = StreamRedactor()
    return redactor.feed(text) + redactor.flush()


def test_number_split_next_to_an_email_still_matches_the_one_shot_result():
    chunks = ["6,06\n-", "123-45-6789.a@b.", "co", " "]

    assert stream(StreamRedactor(), chunks)[-1] == one_shot("".join(chunks))


def test_random_text_and_random_splits_always_match_the_one_shot_result():
    rng = random.Random(7)
    pieces = ["123-45-6789", "4111 1111 1111 1111", "a@b.co", "x.y", "-", " ", "\n", ",", "7", "06", "ab", "@", ".", "\u00a0"]
    for _ in range(5000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 12)))
        expected = one_shot(text)
        cuts = sorted(rng.sample(range(len(text) + 1), k=min(5, len(text) + 1)))
        chunks = [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]
        history = stream(StreamRedactor(), chunks)
        assert history[-1] == expected, chunks
        assert all(expected.startswith(partial) for partial in history), chunks


@pytest.mark.parametrize("before, secret, after", [
    ("-" * 300, "4111111111111111", "-" * 300),
    ("a" * 300, "4111111111111111", "a" * 300),
    ("x1-" * 100, "123-45-6789", "-x1" * 100),
    ("0 " * 150, "4111 1111 1111 1111", " 0" * 150),   # one long spaced number
    ("x" * 240 + "-", "john.smith@corp.com", "-" * 300),  # the email starts with the word, so it is over 256 long
    ("y" * 250, "4111111111111111@b.co", "1" * 300),
])
def test_a_value_inside_a_run_longer_than_the_hold_limit_still_never_leaks(before, secret, after):
    # move the value a few characters at a time, so some forced cut is sure to land on it
    for shift in range(0, 260, 7):
        text = "z" * shift + before + secret + after + " end"
        for size in (1, 16):
            out = stream(StreamRedactor(), [text[i:i + size] for i in range(0, len(text), size)])[-1]
            # neither end of it may show up, so a value cut in two counts as a leak too
            assert secret[:4] not in out and secret[-4:] not in out, (shift, size)


def test_a_long_list_of_numbers_is_handled_in_linear_time():
    start = time.perf_counter()
    one_shot("1234 " * 20_000)  # 4-digit groups could all start a card
    assert time.perf_counter() - start < 1.0


def test_memory_stays_bounded_when_one_value_never_ends():
    redactor = StreamRedactor(max_hold=256)
    redactor.feed("write to a@b.co")
    for _ in range(1000):
        redactor.feed("o" * 100)

    assert redactor.held <= 2 * 256
