"""Date/time helpers with legacy-compatible parsing behavior."""

from datetime import datetime, timezone


def parse_date_yyyy_mm_dd(value):
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def utcnow_naive():
    """Return UTC now without tzinfo for compatibility with current DB columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
