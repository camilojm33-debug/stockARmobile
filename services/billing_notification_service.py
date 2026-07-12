"""Notificaciones de billing y auditoria de eventos de pago."""

from __future__ import annotations

import json


class NotificationService:
    @staticmethod
    def record_event(
        db_session,
        *,
        company_id: int,
        event: str,
        detail: str,
        source: str = "system",
        payment_id: int | None = None,
        subscription_id: int | None = None,
        invoice_id: int | None = None,
        status: str | None = None,
        event_id: str | None = None,
        payload: dict | None = None,
        user_id: int | None = None,
    ):
        from app import AuditLog, PaymentHistory

        history = PaymentHistory(
            company_id=company_id,
            payment_id=payment_id,
            subscription_id=subscription_id,
            invoice_id=invoice_id,
            event=event,
            detail=detail,
            source=source,
            status=status,
            event_id=event_id,
            payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
        )
        db_session.add(history)

        if user_id:
            db_session.add(
                AuditLog(
                    user_id=user_id,
                    company_id=company_id,
                    action=event,
                    entity="billing",
                    entity_id=payment_id or subscription_id or invoice_id,
                    detail=detail,
                )
            )
        return history
