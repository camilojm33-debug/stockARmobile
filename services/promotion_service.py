"""Motor de promociones comerciales, tenant-scoped y determinista."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from app import Product, db, utcnow

TWOPLACES = Decimal("0.01")

def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")

@dataclass(frozen=True)
class PromotionResult:
    promotion_id: int | None
    promotion_name: str | None
    promotion_type: str | None
    quantity: Decimal
    paid_quantity: Decimal
    free_quantity: Decimal
    unit_price: Decimal
    gross_amount: Decimal
    legacy_discount: Decimal
    promotion_discount: Decimal
    final_amount: Decimal
    @property
    def discount(self) -> Decimal:
        return (self.legacy_discount + self.promotion_discount).quantize(TWOPLACES)

class PromotionEngine:
    @staticmethod
    def _candidate_query(company_id: int, product: Product, now: datetime):
        from promotions import Promotion
        query = Promotion.query.filter(
            Promotion.company_id == int(company_id),
            Promotion.active.is_(True),
            Promotion.status.in_(["ACTIVA", "PROGRAMADA"]),
            db.or_(Promotion.starts_at.is_(None), Promotion.starts_at <= now),
            db.or_(Promotion.ends_at.is_(None), Promotion.ends_at >= now),
        )
        condition = db.or_(
            Promotion.product_id == product.id,
            db.and_(Promotion.product_id.is_(None), Promotion.category.is_(None)),
            db.and_(Promotion.product_id.is_(None), Promotion.category == (product.category or "")),
        )
        return query.filter(condition).order_by(Promotion.priority.desc(), Promotion.id.asc())

    @classmethod
    def evaluate(cls, *, company_id: int, product: Product, quantity: Any, now: datetime | None = None) -> PromotionResult:
        qty = Decimal(str(quantity or 0))
        list_price = money(product.price)
        legacy_unit_discount = money(getattr(product, "discount", 0))
        unit_price = max(list_price - legacy_unit_discount, Decimal("0.00"))
        gross = (unit_price * qty).quantize(TWOPLACES)
        legacy_total = (legacy_unit_discount * qty).quantize(TWOPLACES)
        if qty <= 0:
            return PromotionResult(None, None, None, qty, qty, Decimal("0"), unit_price, gross, legacy_total, Decimal("0"), gross)
        promotion = cls._candidate_query(int(company_id), product, now or utcnow()).first()
        if promotion is None:
            return PromotionResult(None, None, None, qty, qty, Decimal("0"), unit_price, gross, legacy_total, Decimal("0"), gross)
        ptype = str(promotion.type or "").strip().lower()
        promo_discount = Decimal("0.00")
        paid_qty = qty
        free_qty = Decimal("0")
        if ptype == "bogo":
            buy = Decimal(str(promotion.buy_quantity or 0))
            pay = Decimal(str(promotion.pay_quantity or 0))
            if buy <= 0 or pay < 0 or pay >= buy or qty < buy:
                return PromotionResult(None, None, None, qty, qty, Decimal("0"), unit_price, gross, legacy_total, Decimal("0"), gross)
            blocks = qty // buy
            free_qty = blocks * (buy - pay)
            paid_qty = qty - free_qty
            promo_discount = (unit_price * free_qty).quantize(TWOPLACES)
        elif ptype == "percent_quantity":
            minimum = Decimal(str(promotion.min_quantity or 0))
            percent = Decimal(str(promotion.discount_percent or 0))
            if minimum <= 0 or percent <= 0 or qty < minimum:
                return PromotionResult(None, None, None, qty, qty, Decimal("0"), unit_price, gross, legacy_total, Decimal("0"), gross)
            promo_discount = (gross * percent / Decimal("100")).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
        elif ptype == "fixed_quantity":
            minimum = Decimal(str(promotion.min_quantity or 0))
            amount = money(promotion.discount_amount)
            if minimum <= 0 or amount <= 0 or qty < minimum:
                return PromotionResult(None, None, None, qty, qty, Decimal("0"), unit_price, gross, legacy_total, Decimal("0"), gross)
            blocks = qty // minimum
            promo_discount = min(gross, (amount * blocks).quantize(TWOPLACES))
        else:
            return PromotionResult(None, None, None, qty, qty, Decimal("0"), unit_price, gross, legacy_total, Decimal("0"), gross)
        final_amount = max(gross - promo_discount, Decimal("0.00")).quantize(TWOPLACES)
        return PromotionResult(int(promotion.id), promotion.name, ptype, qty, paid_qty, free_qty, unit_price, gross, legacy_total, promo_discount, final_amount)

    @classmethod
    def apply_to_lines(cls, *, company_id: int, lines: list[dict[str, Any]], now: datetime | None = None):
        enriched, results = [], []
        for line in lines:
            result = cls.evaluate(company_id=company_id, product=line["product"], quantity=line["quantity"], now=now)
            row = dict(line)
            row["promotion"] = result
            row["discount"] = result.discount
            enriched.append(row)
            results.append(result)
        return enriched, results
