"""Totals computation service for sales."""

from services.sales_calculation_service import calculate_sale_totals


class TotalsService:
    @staticmethod
    def calculate(lines, **adjustments):
        return calculate_sale_totals(lines, **adjustments)
