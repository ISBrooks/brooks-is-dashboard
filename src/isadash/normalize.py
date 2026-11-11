"""Phone normalization — same rules as the sibling projects: last 10 digits,
US-assumed, rendered E.164."""
from __future__ import annotations

import re

_DIGITS = re.compile(r"\d+")


def normalize_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = "".join(_DIGITS.findall(str(raw)))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"+1{digits}"
