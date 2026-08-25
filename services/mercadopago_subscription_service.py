"""Mercado Pago recurring subscription helpers using Preapproval."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from services.mercadopago_service import MercadoPagoService
from services.subscription_service import SubscriptionService


class MercadoPagoSubscriptionService:
    FLOW = "subscription_auto"

    @staticmethod
    def _external_reference(*, company_id: int, subscription_id: int) -> str:
        return (
            f"stockarmobile|flow:{MercadoPagoSubscriptionService.FLOW}|"
            f"company_id:{company_id}|subscription_id:{subscription_id}|nonce:{uuid.uuid4().hex}"
        )

    @staticmethod
    def _metadata(subscription):
        return SubscriptionService._metadata_dict(subscription)

    @staticmethod
    def _parse_datetime(value):
        if not value:
            return None
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)

    @classmethod
    def create(cls, *, db_session, company, subscription, plan, payer_email: str, notification_url: str, back_url: str):
        payer_email = (payer_email or "").strip().lower()
        if not payer_email or "@" not in payer_email:
            raise ValueError("La empresa necesita un email válido para activar el cobro automático de Mercado Pago.")
        amount = float(plan.price or 0)
        if amount <= 0:
            raise ValueError("El plan seleccionado no tiene un importe válido para cobro automático.")

        metadata = cls._metadata(subscription)
        existing_id = str(metadata.get("mercadopago_preapproval_id") or "").strip()
        if existing_id:
            current = MercadoPagoService().get_preapproval(existing_id)
            current_status = str(current.get("status") or "").lower()
            if current_status in {"authorized", "pending"}:
                return current

        external_reference = cls._external_reference(company_id=company.id, subscription_id=subscription.id)
        response = MercadoPagoService().create_preapproval(
            reason=f"StockArMobile - Plan {plan.name}",
            payer_email=payer_email,
            external_reference=external_reference,
            amount=amount,
            currency=plan.currency or "ARS",
            frequency=1,
            frequency_type="months",
            notification_url=notification_url,
            back_url=back_url,
        )
        preapproval_id = str(response.get("id") or "").strip()
        init_point = str(response.get("init_point") or "").strip()
        if not preapproval_id or not init_point:
            raise RuntimeError("Mercado Pago no devolvió una suscripción válida (id/init_point).")

        SubscriptionService._set_metadata(
            subscription,
            {
                "mercadopago_preapproval_id": preapproval_id,
                "mercadopago_status": str(response.get("status") or "pending"),
                "mercadopago_payer_email": payer_email,
                "mercadopago_external_reference": external_reference,
                "payment_method": "mercadopago_subscription",
                "auto_renew": True,
                "subscription_auto_created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        # Local renewal stays disabled until Mercado Pago confirms authorization.
        subscription.renewal_enabled = False
        subscription.auto_renew = False
        subscription.cancel_at_period_end = True
        db_session.flush()
        return response

    @classmethod
    def sync_preapproval(cls, *, db_session, preapproval: dict):
        from app import Subscription

        preapproval_id = str(preapproval.get("id") or "").strip()
        if not preapproval_id:
            return None
        rows = Subscription.query.filter(Subscription.metadata_json.contains(preapproval_id)).all()
        subscription = next(
            (row for row in rows if cls._metadata(row).get("mercadopago_preapproval_id") == preapproval_id),
            None,
        )
        if subscription is None:
            return None

        status = str(preapproval.get("status") or "").lower()
        metadata = cls._metadata(subscription)
        metadata.update(
            {
                "mercadopago_status": status,
                "mercadopago_payer_id": preapproval.get("payer_id"),
                "mercadopago_payment_method_id": preapproval.get("payment_method_id"),
                "mercadopago_last_sync_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        SubscriptionService._set_metadata(subscription, metadata)

        if status == "authorized":
            if subscription.status not in {SubscriptionService.STATE_ACTIVE, SubscriptionService.STATE_SCHEDULED}:
                SubscriptionService._transition(
                    subscription,
                    SubscriptionService.STATE_ACTIVE,
                    reason="mercadopago_preapproval_authorized",
                )
            subscription.renewal_enabled = True
            subscription.auto_renew = True
            subscription.cancel_at_period_end = False
            next_payment = cls._parse_datetime(preapproval.get("next_payment_date"))
            if next_payment:
                subscription.next_billing_date = next_payment
                subscription.ends_at = next_payment
        elif status in {"paused", "cancelled", "canceled", "expired"}:
            subscription.renewal_enabled = False
            subscription.auto_renew = False
            subscription.cancel_at_period_end = True
            if status in {"cancelled", "canceled"} and subscription.status not in {
                SubscriptionService.STATE_CANCELLED,
                SubscriptionService.STATE_EXPIRED,
            }:
                SubscriptionService._transition(
                    subscription,
                    SubscriptionService.STATE_CANCELLED,
                    reason="mercadopago_preapproval_cancelled",
                )
        elif status == "pending":
            subscription.renewal_enabled = False
            subscription.auto_renew = False
            subscription.cancel_at_period_end = True

        db_session.flush()
        return subscription
