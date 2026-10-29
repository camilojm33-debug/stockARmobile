"""String normalization helpers."""

from urllib.parse import unquote, urlsplit


def normalize_text(value):
    return (value or "").strip()


def normalize_lower(value):
    return normalize_text(value).lower()


def normalize_digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def is_safe_relative_redirect(target):
    if not isinstance(target, str) or not target or target != target.strip():
        return False
    if not target.startswith("/") or target.startswith("//"):
        return False
    if "\\" in target or any(ord(char) < 32 or ord(char) == 127 for char in target):
        return False
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return False

    decoded_path = parsed.path
    for _ in range(8):
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
        if decoded_path.startswith("//") or "\\" in decoded_path:
            return False
    else:
        return False
    return True
