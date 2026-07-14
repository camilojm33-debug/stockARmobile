"""Procesamiento idempotente de webhooks de Mercado Pago."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from services.billing_notification_service import NotificationService
from services.invoice_service import InvoiceService
from services.mercadopago_service import MercadoPagoService
from services.referral_service import ReferralService
from services.subscription_service import SubscriptionService


class WebhookService:
    def __init__(self):
        self.mp_service = MercadoPagoService()

    def _event_key(self, payload: dict) -> str:
        event_type = payload.get("type") or payload.get("topic") or "unknown"
        data = payload.get("data") or {}
        data_id = str(data.get("id") or payload.get("id") or "none")
        return f"{event_type}:{data_id}"

    @staticmethod
    def _parse_mp_datetime(value: str | None):
        if not value:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)

    @staticmethod
    def _event_updated_at(payment_data: dict):
        return WebhookService._parse_mp_datetime(payment_data.get("date_last_updated") or payment_data.get("date_approved"))

    @staticmethod
    def _payment_event_key(payment_data: dict, fallback_key: str) -> str:
        payment_id = str(payment_data.get("id") or "")
        if not payment_id:
            return fallback_key
        status = (payment_data.get("status") or "pending").lower()
        updated_at = payment_data.get("date_last_updated") or payment_data.get("date_approved") or "na"
        return f"payment:{payment_id}:{status}:{updated_at}"

    def process(self, *, db_session, headers: dict, payload: dict) -> dict:
        from app import Payment, Subscription, User, WebhookEvent

        generic_event_key = self._event_key(payload)
        event_key = generic_event_key
        event_type = payload.get("type") or payload.get("topic") or "unknown"
        data = payload.get("data") or {}
        data_id = str(data.get("id") or payload.get("id") or "")

        request_id = headers.get("x-request-id") or headers.get("X-Request-Id") or ""
        x_signature = headers.get("x-signature") or headers.get("X-Signature") or ""
        if not self.mp_service.validate_webhook_signature(request_id=request_id, x_signature=x_signature, data_id=data_id):
            raise RuntimeError("Firma de webhook invalida")

        existing = WebhookEvent.query.filter_by(event_key=event_key).first()
        if existing:
            return {"status": "duplicate", "event_key": event_key}

        payment_data = None
        if event_type in {"payment", "payment.updated", "merchant_order", "topic_payment"}:
            payment_data = self.mp_service.get_payment(data_id)
            event_key = self._payment_event_key(payment_data, generic_event_key)
            existing_payment_event = WebhookEvent.query.filter_by(event_key=event_key).first()
            if existing_payment_event:
                return {"status": "duplicate", "event_key": event_key}

        event_row = WebhookEvent(
            provider="mercadopago",
            event_key=event_key,
            event_type=event_type,
            status="processing",
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        db_session.add(event_row)
        db_session.flush()

        payment_status = "pending"
        result = {"status": "ignored", "event_key": event_key}

        if event_type in {"payment", "payment.updated", "merchant_order", "topic_payment"}:
            payment_status = (payment_data.get("status") or "pending").lower()
            external_reference = payment_data.get("external_reference") or ""
            incoming_updated_at = self._event_updated_at(payment_data)
            paid_at = self._parse_mp_datetime(payment_data.get("date_approved"))

            payment = Payment.query.filter_by(payment_id=str(payment_data.get("id"))).first()
            if payment is None:
                metadata = payment_data.get("metadata") or {}
                company_id = int(metadata.get("company_id") or 0)
                subscription_id = int(metadata.get("subscription_id") or 0) or None
                user_id = int(metadata.get("user_id") or 0) or None

                ref_parts = {segment.split(":", 1)[0]: segment.split(":", 1)[1] for segment in external_reference.split("|") if ":" in segment}
                if not subscription_id and str(ref_parts.get("subscription_id") or "").isdigit():
                    subscription_id = int(ref_parts.get("subscription_id"))
                if not company_id and str(ref_parts.get("company_id") or "").isdigit():
                    company_id = int(ref_parts.get("company_id"))
                if not user_id and str(ref_parts.get("user_id") or "").isdigit():
                    user_id = int(ref_parts.get("user_id"))

                if not company_id or not subscription_id:
                    raise RuntimeError("Webhook Mercado Pago sin company_id/subscription_id validos")

                payment = Payment(
                    payment_id=str(payment_data.get("id")),
                    preference_id=str(payment_data.get("order", {}).get("id") or payment_data.get("metadata", {}).get("preference_id") or ""),
                    external_reference=external_reference,
                    company_id=company_id,
                    subscription_id=subscription_id,
                    user_id=user_id,
                    amount=float(payment_data.get("transaction_amount") or 0),
                    currency=payment_data.get("currency_id") or "ARS",
                    status=payment_status,
                    payment_method=payment_data.get("payment_method_id"),
                    reference=external_reference,
                    provider="mercadopago",
                    payload_json=json.dumps(payment_data, ensure_ascii=False),
                    paid_at=paid_at,
                )
                db_session.add(payment)
                db_session.flush()
            else:
                previous_payload = json.loads(payment.payload_json) if payment.payload_json else {}
                previous_updated_at = self._event_updated_at(previous_payload)
                if previous_updated_at and incoming_updated_at and incoming_updated_at < previous_updated_at:
                    event_row.status = "stale_ignored"
                    db_session.add(event_row)
                    db_session.commit()
                    return {"status": "stale_ignored", "event_key": event_key}

                payment.preference_id = str(payment_data.get("order", {}).get("id") or payment.preference_id or "")
                payment.status = payment_status
                payment.amount = float(payment_data.get("transaction_amount") or payment.amount or 0)
                payment.currency = payment_data.get("currency_id") or payment.currency or "ARS"
                payment.payment_method = payment_data.get("payment_method_id") or payment.payment_method
                payment.reference = external_reference or payment.reference
                payment.payload_json = json.dumps(payment_data, ensure_ascii=False)
                payment.paid_at = paid_at or payment.paid_at

            subscription = None
            if payment.subscription_id:
                subscription = Subscription.query.filter_by(id=payment.subscription_id).first()
            if subscription is None:
                ref_parts = {segment.split(":", 1)[0]: segment.split(":", 1)[1] for segment in external_reference.split("|") if ":" in segment}
                sub_id = ref_parts.get("subscription_id")
                if sub_id:
                    subscription = Subscription.query.filter_by(id=int(sub_id)).first()
                    if subscription:
                        payment.subscription_id = subscription.id
                        payment.company_id = subscription.company_id

            company = subscription.company if subscription and subscription.company else None
            if subscription and company:
                SubscriptionService.apply_payment_status(subscription, payment_status)
                company.active = subscription.status in {"active", "approved", "trial"}
                if subscription.status in {"active", "approved"}:
                    if payment.invoice_id is None:
                        invoice = InvoiceService.create_invoice(
                            db_session,
                            company=company,
                            subscription=subscription,
                            amount=float(payment.amount or 0),
                            currency=payment.currency or "ARS",
                            detail=f"Cobro Mercado Pago {payment.payment_id}",
                            payment_id=payment.payment_id,
                        )
                        payment.invoice_id = invoice.id

                    ReferralService.create_commission_for_sale(
                        db_session,
                        company_id=company.id,
                        subscription=subscription,
                        payment=payment,
                        plan=subscription.plan,
                    )

                user = User.query.filter_by(id=payment.user_id).first() if payment.user_id else None
                NotificationService.record_event(
                    db_session,
                    company_id=company.id,
                    payment_id=payment.id,
                    subscription_id=subscription.id,
                    invoice_id=payment.invoice_id,
                    event="mercadopago_webhook_payment",
                    detail=f"Pago {payment.payment_id} en estado {payment.status}",
                    source="mercadopago",
                    status=payment.status,
                    event_id=event_key,
                    payload=payment_data,
                    user_id=user.id if user else None,
                )

            result = {"status": "processed", "payment_status": payment_status, "event_key": event_key}

        elif event_type in {"preapproval", "subscription_preapproval"}:
            preapproval_data = self.mp_service.get_preapproval(data_id)
            subscription_id = preapproval_data.get("external_reference")
            subscription = None
            if subscription_id and str(subscription_id).isdigit():
                subscription = Subscription.query.filter_by(id=int(subscription_id)).first()
            if subscription:
                subscription.mercadopago_subscription_id = str(preapproval_data.get("id") or subscription.mercadopago_subscription_id or "")
                status = (preapproval_data.get("status") or "pending").lower()
                subscription.status = SubscriptionService.PAYMENT_STATUS_MAP.get(status, status)
                subscription.renewal_enabled = subscription.status in {"active", "approved", "trial", "pending"}
            result = {"status": "processed_preapproval", "event_key": event_key}

        event_row.status = result.get("status")
        ReferralService.refresh_commission_states(db_session)
        db_session.add(event_row)
        db_session.commit()
        return result
