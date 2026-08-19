"""Pricing service orchestrating totals and discounts."""

from stockarmobile.helpers.money import safe_decimal

from .totals_service import TotalsService


class PricingService:
    @staticmethod
    def normalize_adjustments(data):
        discount = data.get("discount") if isinstance(data.get("discount"), dict) else {}
        surcharge = data.get("surcharge_adjustment") if isinstance(data.get("surcharge_adjustment"), dict) else {}
        return {
            "general_discount": safe_decimal(data.get("descuento_general") or data.get("general_discount")),
            "surcharge": safe_decimal(data.get("recargo") or data.get("surcharge")),
            "discount_type": data.get("discount_type") or discount.get("type"),
            "discount_value": data.get("discount_value") if data.get("discount_value") not in (None, "") else discount.get("value"),
            "discount_reason": data.get("discount_reason") or discount.get("reason"),
            "discount_applied_amount": data.get("discount_applied_amount"),
            "surcharge_type": data.get("surcharge_type") or surcharge.get("type"),
            "surcharge_value": data.get("surcharge_value") if data.get("surcharge_value") not in (None, "") else surcharge.get("value"),
            "surcharge_reason": data.get("surcharge_reason") or surcharge.get("reason"),
            "surcharge_applied_amount": data.get("surcharge_applied_amount"),
        }

    @classmethod
    def calculate(cls, lines, data):
        return TotalsService.calculate(lines, **cls.normalize_adjustments(data))
