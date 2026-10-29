"""Redacts emails, SSNs and card numbers from text that arrives in chunks."""

import regex

REDACTED = "[REDACTED]"

PII = regex.compile(
    # card layouts: 13-19 digits, 4-4-4-x, or 4-6-5 / 4-6-4. \d also catches fullwidth digits
    r"(?P<card>(?<!\d)(?:\d{13,19}|\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,7}|\d{4}[ -]\d{6}[ -]\d{4,5})(?!\d))"
    r"|(?P<ssn>(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d))"
    # only start at the beginning of a word, otherwise long words get slow
    r"|(?P<email>(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
)

# text that could still extend a finished match, e.g. "a@mail.corp" followed by "."
CAN_CONTINUE = {
    "email": regex.compile(r"[A-Za-z0-9.-]*"),
    "card": regex.compile(r"[ -]?"),
    "ssn": regex.compile(r"[ -]?"),
}


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def replacement(match) -> str:
    if match.lastgroup == "card":
        digits = "".join(ch for ch in match.group() if ch.isdigit())
        if not luhn_valid(digits):
            return match.group()
    return REDACTED


class StreamRedactor:
    def __init__(self, max_hold: int = 256):
        # longer than any email or card, so past this nothing can still be one value
        self.max_hold = max_hold
        self._pending = ""

    @property
    def held(self) -> int:
        return len(self._pending)

    def feed(self, text: str) -> str:
        """Returns the text that is safe to send now and keeps the rest."""
        self._pending += text
        out, pos, hold_from = [], 0, len(self._pending)
        for match in PII.finditer(self._pending, partial=True):
            at_end = match.end() == len(self._pending)
            if at_end or CAN_CONTINUE[match.lastgroup].fullmatch(self._pending, match.end()):
                hold_from = match.start()
                break
            out.append(self._pending[pos:match.start()])
            out.append(replacement(match))
            pos = match.end()

        hold_from = max(hold_from, len(self._pending) - self.max_hold)
        out.append(self._pending[pos:hold_from])
        self._pending = self._pending[hold_from:]
        return "".join(out)

    def flush(self) -> str:
        rest, self._pending = self._pending, ""
        return PII.sub(replacement, rest)
