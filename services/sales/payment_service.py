"""Payment split helpers for sales."""

from services.sales_calculation_service import normalize_payment_split, sale_payment_breakdown_from_values


class PaymentService:
    @staticmethod
    def normalize_split(*, total_amount, data):
        payment_primary_method = data.get("metodo_pago") or data.get("payment_method") or "EFECTIVO"
        payment_secondary_method = data.get("metodo_pago_2") or data.get("secondary_payment_method") or ""
        return normalize_payment_split(
            total_amount=total_amount,
            primary_method=payment_primary_method,
            secondary_method=payment_secondary_method,
            primary_amount=(data.get("monto_pago") or data.get("paid_amount")),
            secondary_amount=(data.get("monto_pago_2") or data.get("secondary_paid_amount")),
        )

    @staticmethod
    def cash_breakdown(*, total_amount, payment_split):
        return sale_payment_breakdown_from_values(
            total_amount=total_amount,
            primary_method=payment_split["primary_method"],
            secondary_method=payment_split["secondary_method"],
            primary_amount=payment_split["primary_amount"],
            secondary_amount=payment_split["secondary_amount"],
        )
