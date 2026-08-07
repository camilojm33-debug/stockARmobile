"""Money/decimal conversion helpers."""

from decimal import Decimal, InvalidOperation


def safe_decimal(value, default="0.00"):
    try:
        if value in (None, ""):
            return Decimal(default)
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
