"""String normalization helpers."""

from urllib.parse import urlsplit


def normalize_text(value):
    return (value or "").strip()


def normalize_lower(value):
    return normalize_text(value).lower()


def normalize_digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def is_safe_relative_redirect(target):
    if not target:
        return False
    parsed = urlsplit(target)
    return not parsed.netloc and parsed.path.startswith("/")
