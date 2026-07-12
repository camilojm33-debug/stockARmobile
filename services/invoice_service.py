"""Servicio de facturas para suscripciones SaaS."""

from __future__ import annotations


class InvoiceService:
    @staticmethod
    def _invoice_number(company_id: int, invoice_id: int) -> str:
        return f"INV-{company_id:04d}-{invoice_id:08d}"

    @staticmethod
    def create_invoice(
        db_session,
        company,
        subscription,
        amount: float,
        currency: str,
        detail: str,
        vat_rate: float = 0.21,
        payment_id: str | None = None,
    ):
        from app import Invoice, utcnow

        reference = f"payment:{payment_id}" if payment_id else None
        if reference:
            existing = Invoice.query.filter_by(reference=reference).first()
            if existing:
                return existing

        net_amount = float(amount or 0)
        vat_amount = round(net_amount * float(vat_rate), 2)
        total_amount = round(net_amount + vat_amount, 2)
        issued_at = utcnow()
        due_at = subscription.next_billing_date or issued_at

        invoice = Invoice(
            company_id=company.id,
            subscription_id=subscription.id if subscription else None,
            amount=total_amount,
            vat_amount=vat_amount,
            currency=currency,
            status="pending",
            issued_at=issued_at,
            due_at=due_at,
            detail=detail,
            provider="mercadopago",
            reference=reference or f"company:{company.id}:subscription:{subscription.id if subscription else 'none'}:{int(issued_at.timestamp())}",
        )
        db_session.add(invoice)
        db_session.flush()
        invoice.invoice_number = InvoiceService._invoice_number(company.id, invoice.id)
        db_session.add(invoice)
        return invoice
