"""Reusable pure validators."""

import re

EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)


def is_valid_email(value):
    return bool(EMAIL_RE.match((value or "").strip()))


def is_positive_number(value):
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False
