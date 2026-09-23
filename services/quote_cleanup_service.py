"""Safe, tenant-scoped cleanup of old commercial quotes.

The cleanup is intentionally conservative: converted quotes and quotes with an
active/approved AI-order payment are never eligible for bulk deletion.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

AI_ORDER_MARKER = "Pedido generado por el Vendedor 24 hs de StockARmobile."
AI_ORDER_PROVIDER = "mercadopago_ai_order"

# Only terminal/low-risk commercial states are eligible by default.
DEFAULT_ELIGIBLE_STATUSES = {
    "BORRADOR",
    "PENDIENTE",
    "RECHAZADO",
    "VENCIDO",
    "ANULADO",
}

# These payment states mean the AI order may still continue through Mercado Pago.
ACTIVE_PAYMENT_STATUSES = {"pending", "in_process", "authorized", "approved"}

AI_QUOTE_REFERENCE_RE = re.compile(r"(?:^|\|)quote_id:(\d+)(?:\||$)")


class QuoteCleanupService:
    """Find and safely remove old quotes for exactly one company."""

    @staticmethod
    def is_ai_quote(quote: Quote) -> bool:
        return str(getattr(quote, "observations", "") or "").strip() == AI_ORDER_MARKER

    @staticmethod
    def _payment_state_by_quote(company_id: int) -> dict[int, set[str]]:
        from app import Payment

        payments = (
            Payment.query.filter(
                Payment.company_id == int(company_id),
                Payment.provider == AI_ORDER_PROVIDER,
            )
            .all()
        )
        states: dict[int, set[str]] = {}
        for payment in payments:
            reference = str(getattr(payment, "external_reference", "") or "")
            match = AI_QUOTE_REFERENCE_RE.search(reference)
            if not match:
                continue
            quote_id = int(match.group(1))
            states.setdefault(quote_id, set()).add(
                str(getattr(payment, "status", "") or "").strip().lower()
            )
        return states

    @classmethod
    def protection_reason(cls, *, company_id: int, quote: Quote) -> str | None:
        """Return a safety reason that prevents deletion, if any."""
        if int(getattr(quote, "company_id", 0) or 0) != int(company_id):
            return "otra_empresa"
        payment_states = cls._payment_state_by_quote(company_id)
        return cls._protected_reason(quote, payment_states)

    @classmethod
    def _protected_reason(cls, quote: Quote, payment_states: dict[int, set[str]]) -> str | None:
        if getattr(quote, "converted_sale_id", None):
            return "convertido_a_venta"
        status = str(getattr(quote, "status", "") or "").upper()
        if status == "CONVERTIDO":
            return "convertido"
        if status not in DEFAULT_ELIGIBLE_STATUSES:
            return "estado_no_elegible"
        if not cls.is_ai_quote(quote):
            return None

        active_states = payment_states.get(int(quote.id), set()) & ACTIVE_PAYMENT_STATUSES
        if "approved" in active_states:
            return "pago_aprobado"
        if active_states:
            return "pago_activo"

        delivery = getattr(quote, "delivery", None)
        if delivery is not None and str(getattr(delivery, "shipping_status", "") or "").lower() == "pending":
            return "envio_pendiente"
        return None

    @classmethod
    def preview(
        cls,
        *,
        company_id: int,
        older_than_days: int = 90,
        kind: str = "all",
        statuses: set[str] | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        safe_days = max(1, min(int(older_than_days or 90), 3650))
        safe_limit = max(1, min(int(limit or 500), 2000))
        from app import Quote, utcnow

        cutoff = utcnow() - timedelta(days=safe_days)
        normalized_kind = str(kind or "all").strip().lower()
        allowed_kinds = {"all", "manual", "ai"}
        if normalized_kind not in allowed_kinds:
            normalized_kind = "all"

        requested_statuses = {
            str(value).strip().upper()
            for value in (statuses or DEFAULT_ELIGIBLE_STATUSES)
            if str(value).strip()
        }
        requested_statuses &= DEFAULT_ELIGIBLE_STATUSES

        considered_statuses = sorted(
            set(requested_statuses) | {"ENVIADO", "APROBADO", "CONVERTIDO"}
        )
        query = Quote.query.filter(
            Quote.company_id == int(company_id),
            Quote.status.in_(considered_statuses),
            Quote.date < cutoff,
        ).order_by(Quote.date.asc(), Quote.id.asc()).limit(safe_limit)

        quotes = query.all()
        payment_states = cls._payment_state_by_quote(company_id)

        eligible = []
        protected = []
        manual_count = 0
        ai_count = 0

        for quote in quotes:
            is_ai = cls.is_ai_quote(quote)
            if normalized_kind == "ai" and not is_ai:
                continue
            if normalized_kind == "manual" and is_ai:
                continue

            if is_ai:
                ai_count += 1
            else:
                manual_count += 1

            reason = cls._protected_reason(quote, payment_states)
            row = {
                "id": int(quote.id),
                "number": quote.number or f"P-{int(quote.id):06d}",
                "status": str(quote.status or ""),
                "kind": "ai" if is_ai else "manual",
                "customer": (
                    getattr(getattr(quote, "client", None), "name", None)
                    or getattr(quote, "consumer_name", None)
                    or "Consumidor final"
                ),
                "date": quote.date,
                "total_amount": quote.total_amount,
                "protected": bool(reason),
                "protected_reason": reason,
            }
            if reason:
                protected.append(row)
            else:
                eligible.append(row)

        return {
            "company_id": int(company_id),
            "older_than_days": safe_days,
            "cutoff": cutoff,
            "kind": normalized_kind,
            "eligible": eligible,
            "protected": protected,
            "eligible_count": len(eligible),
            "protected_count": len(protected),
            "manual_count": manual_count,
            "ai_count": ai_count,
        }

    @classmethod
    def delete_eligible(
        cls,
        *,
        company_id: int,
        older_than_days: int = 90,
        kind: str = "all",
        statuses: set[str] | None = None,
        ip_address: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        preview = cls.preview(
            company_id=company_id,
            older_than_days=older_than_days,
            kind=kind,
            statuses=statuses,
            limit=limit,
        )
        ids = [row["id"] for row in preview["eligible"]]
        if not ids:
            return {**preview, "deleted_count": 0}

        from app import Quote, db

        quotes = (
            Quote.query.filter(
                Quote.company_id == int(company_id),
                Quote.id.in_(ids),
            )
            .order_by(Quote.id.asc())
            .all()
        )

        # Re-check every safety rule from current DB state immediately before deletion.
        payment_states = cls._payment_state_by_quote(company_id)
        deleted = []
        for quote in quotes:
            reason = cls._protected_reason(quote, payment_states)
            if reason:
                continue
            deleted.append(
                {
                    "id": int(quote.id),
                    "number": quote.number or f"P-{int(quote.id):06d}",
                    "kind": "ai" if cls.is_ai_quote(quote) else "manual",
                    "status": str(quote.status or ""),
                    "total_amount": quote.total_amount,
                }
            )
            db.session.delete(quote)

        db.session.commit()

        if deleted:
            from app import record_audit

            for row in deleted:
                record_audit(
                    action="quote_cleanup_delete",
                    entity="quote",
                    entity_id=row["id"],
                    detail=(
                        f"Limpieza de presupuesto {row['number']} "
                        f"tipo={row['kind']} estado={row['status']} "
                        f"antiguedad>{preview['older_than_days']}d"
                    ),
                    ip_address=ip_address,
                )

        return {
            **preview,
            "deleted_count": len(deleted),
            "deleted": deleted,
        }
