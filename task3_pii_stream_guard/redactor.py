"""Redacts emails, SSNs and card numbers from text that arrives in chunks."""

import re

REDACTED = "[REDACTED]"

# Latin letters, accented too (also as combining marks). Not \w, which would pull Chinese text into an email.
LETTERS = r"A-Za-z\u00c0-\u024f\u0300-\u036f"
EMAIL_CHARS = LETTERS + r"0-9._%+-"
# what can sit between digit groups: dashes and hyphens of every kind, minus sign, and spaces of every width
JOINERS = "- \u00a0\u202f\u2009\u2007\u3000\u2010\u2011\u2012\u2013\u2014\u2212\uff0d"

# only start at the beginning of a word, otherwise long words get slow
EMAIL = re.compile(rf"(?<![{EMAIL_CHARS}])[{EMAIL_CHARS}]+@[{LETTERS}0-9-]+(?:\.[{LETTERS}0-9-]+)*\.[A-Za-z]{{2,}}")
# digit groups with one joiner between them, cards and SSNs are found inside these. \d also matches fullwidth digits
NUMBER = re.compile(rf"\d+(?:[{JOINERS}]\d+)*")
GROUP = re.compile(r"\d+")
# can be part of a value wherever it is. The other joiners only count between two digits.
VALUE_CHAR = re.compile(rf"[\d@{EMAIL_CHARS}]")


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def is_card(sizes: list[int], digits: str) -> bool:
    # real layouts only: one block, 4-4-4-x, or Amex / Diners 4-6-5 and 4-6-4
    layout = len(sizes) == 1 or sizes in ([4, 6, 5], [4, 6, 4]) or (len(sizes) >= 4 and set(sizes[:-1]) == {4})
    return layout and 13 <= len(digits) <= 19 and luhn_valid(digits)


def pii_spans(text: str):
    """(start, end) of every email, card and SSN in text. They can overlap."""
    for match in EMAIL.finditer(text):
        yield match.span()
    for number in NUMBER.finditer(text):
        groups = list(GROUP.finditer(text, number.start(), number.end()))
        # try every run of neighbouring groups, a card can sit right next to other numbers
        for i, first in enumerate(groups):
            digits, sizes = "", []
            for last in groups[i:i + 5]:  # no layout has more than 5 groups
                digits += last.group()
                sizes.append(len(last.group()))
                if sizes == [3, 2, 4] or is_card(sizes, digits):
                    yield first.start(), last.end()


def merged_spans(text: str) -> list[list[int]]:
    merged = []
    for start, end in sorted(pii_spans(text)):
        if merged and start < merged[-1][1]:  # overlaps the one before, widen it
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def replace(text: str, spans) -> str:
    out, pos = [], 0
    for start, end in spans:
        out += [text[pos:start], REDACTED]
        pos = end
    out.append(text[pos:])
    return "".join(out)


def redact(text: str) -> str:
    return replace(text, merged_spans(text))


def last_boundary(text: str, start: int) -> int:
    """Index just after the last character, from start on, that no email, card or SSN can contain."""
    for i in range(len(text) - 1, start - 1, -1):
        if VALUE_CHAR.match(text, i):
            continue
        # a joiner is only inside a value when it sits between two digit groups
        if text[i] not in JOINERS or not text[i - 1:i].isdecimal() or (i + 1 < len(text) and not text[i + 1].isdecimal()):
            return i + 1
    return 0


class StreamRedactor:
    def __init__(self, max_hold: int = 256):
        # longer than any email or card, so a value that crosses a forced cut is always seen whole
        self.max_hold = max_hold
        self._pending = ""

    @property
    def held(self) -> int:
        return len(self._pending)

    def feed(self, text: str) -> str:
        """Returns the text that is safe to send now and keeps the rest."""
        # held text has no boundary in it, except maybe a last joiner that needed the next character
        start = max(len(self._pending) - 1, 0)
        self._pending += text
        cut = last_boundary(self._pending, start)
        if len(self._pending) - cut > 2 * self.max_hold:
            # holding too much: send all but the last max_hold characters. A value that crosses there is
            # held back whole, or if it is too long to hold, sent whole and redacted.
            forced = len(self._pending) - self.max_hold
            spans = merged_spans(self._pending)
            s, e = next(((s, e) for s, e in spans if s < forced < e), (forced, forced))
            cut = s if e - s <= self.max_hold else e
            ready, self._pending = self._pending[:cut], self._pending[cut:]
            return replace(ready, [span for span in spans if span[1] <= cut])
        ready, self._pending = self._pending[:cut], self._pending[cut:]
        return redact(ready)

    def flush(self) -> str:
        rest, self._pending = self._pending, ""
        return redact(rest)
