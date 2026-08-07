"""Totals computation service for sales."""

from services.sales_calculation_service import calculate_sale_totals


class TotalsService:
    @staticmethod
    def calculate(lines, *, general_discount=0, surcharge=0):
        return calculate_sale_totals(lines, general_discount=general_discount, surcharge=surcharge)
