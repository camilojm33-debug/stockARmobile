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

        # Persist high-signal payment/system events for the tenant admins and,
        # for platform-level validation failures, the Superadmin operators.
        normalized_status = str(status or "").strip().lower()
        event_name = str(event or "").strip().lower()
        alert_event = (
            normalized_status in {"approved", "failed", "rejected", "refunded", "cancelled", "canceled", "charged_back"}
            or any(token in event_name for token in ("error", "failed", "reversed", "refund", "activated"))
        )
        if alert_event:
            try:
                from app import User
                from services.notification_service import record_notification_event
                recipients = [
                    row.id for row in db_session.query(User.id).filter(
                        User.company_id == int(company_id),
                        User.role == "admin",
                        User.active.is_(True),
                    ).all()
                ]
                href = "/admin/portal"
                stable_event = event_id or f"{source}:{event}:{payment_id or subscription_id or invoice_id or 'system'}:{status or ''}"
                with db_session.begin_nested():
                    record_notification_event(
                        db_session,
                        event_key=stable_event,
                        title="Pago acreditado" if normalized_status == "approved" else "Incidencia de pago" if normalized_status in {"failed", "rejected", "refunded", "cancelled", "canceled", "charged_back"} else "Incidencia de billing",
                        body=str(detail or event)[:1500],
                        href=href,
                        category="payments",
                        priority="normal" if normalized_status == "approved" else "high",
                        company_id=int(company_id),
                        recipient_user_ids=recipients,
                        source_type=source,
                        source_id=str(payment_id or subscription_id or invoice_id or event_id or ""),
                    )
                    if "webhook_validation_error" in event_name or "webhook_error" in event_name:
                        platform_users = [row.id for row in db_session.query(User.id).filter(User.role == "superadmin", User.active.is_(True)).all()]
                        record_notification_event(
                            db_session,
                            event_key=f"platform:{stable_event}",
                            title="Validación de webhook",
                            body=str(detail or event)[:1500],
                            href="/superadmin",
                            category="system",
                            priority="critical",
                            company_id=None,
                            recipient_user_ids=platform_users,
                            source_type=source,
                            source_id=str(event_id or payment_id or ""),
                        )
            except Exception:
                # Notification delivery must never abort or roll back billing.
                import logging
                logging.getLogger(__name__).exception("No se pudo generar notificación de billing")
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
