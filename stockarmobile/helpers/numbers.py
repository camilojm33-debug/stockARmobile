"""Numeric parsing helpers."""


def safe_float(value, default=0.0):
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default
