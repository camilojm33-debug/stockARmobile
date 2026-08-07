"""Pricing service orchestrating totals and discounts."""

from stockarmobile.helpers.money import safe_decimal

from .totals_service import TotalsService


class PricingService:
    @staticmethod
    def normalize_adjustments(data):
        general_discount = safe_decimal(data.get("descuento_general") or data.get("general_discount"))
        surcharge = safe_decimal(data.get("recargo") or data.get("surcharge"))
        return general_discount, surcharge

    @classmethod
    def calculate(cls, lines, data):
        general_discount, surcharge = cls.normalize_adjustments(data)
        return TotalsService.calculate(lines, general_discount=general_discount, surcharge=surcharge)
