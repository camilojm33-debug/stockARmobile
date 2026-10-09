"""Safe numeric normalization for inventory quantities exposed to AI tools.

Database FLOAT columns may contain binary representation noise (for example
17.80000000000001). These helpers normalize tool output only; inventory writes
and stock validation continue to use the persisted product quantity.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def normalized_stock_quantity(value, *, places: int = 6) -> float:
    """Return a stable numeric representation without binary float noise."""
    try:
        amount = Decimal(str(value if value is not None else 0))
        if not amount.is_finite():
            return 0.0
        quantum = Decimal(1).scaleb(-max(0, min(int(places), 12)))
        return float(amount.quantize(quantum, rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError, ArithmeticError):
        return 0.0
