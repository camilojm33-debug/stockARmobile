"""Orquestador de facturacion/suscripciones para checkout y renovaciones."""

from __future__ import annotations

from services.billing_notification_service import NotificationService
from services.mercadopago_service import MercadoPagoService
from services.subscription_service import SubscriptionService


class BillingService:
    def __init__(self):
        self.mp_service = MercadoPagoService()

    def create_checkout_for_plan(self, *, db_session, company, plan, user):
        from app import utcnow

        subscription = SubscriptionService.start_or_change_plan(
            db_session,
            company=company,
            plan=plan,
            user_id=user.id,
            external_reference=None,
        )
        db_session.flush()

        external_reference = (
            f"company_id:{company.id}|plan_id:{plan.id}|subscription_id:{subscription.id}|"
            f"user_id:{user.id}|ts:{int(utcnow().timestamp())}"
        )
        subscription.external_reference = external_reference

        preference = self.mp_service.create_checkout_preference(
            title=f"StockArmobile - {plan.name}",
            amount=float(plan.price or 0),
            currency=plan.currency or "ARS",
            external_reference=external_reference,
            company_id=company.id,
            plan_id=plan.id,
            subscription_id=subscription.id,
            user_id=user.id,
        )

        NotificationService.record_event(
            db_session,
            company_id=company.id,
            subscription_id=subscription.id,
            event="checkout_preference_created",
            detail=f"Preference {preference.get('id')} para plan {plan.name}",
            source="mercadopago",
            status="pending",
            event_id=str(preference.get("id") or ""),
            payload=preference,
            user_id=user.id,
        )
        db_session.commit()
        return {"subscription": subscription, "preference": preference}

    @staticmethod
    def cancel_subscription(db_session, *, subscription, user_id: int | None = None):
        subscription.cancel_at_period_end = True
        subscription.renewal_enabled = False
        subscription.auto_renew = False
        NotificationService.record_event(
            db_session,
            company_id=subscription.company_id,
            subscription_id=subscription.id,
            event="subscription_cancel_requested",
            detail="El usuario solicito cancelar al final del periodo.",
            source="portal",
            status="cancelled",
            user_id=user_id,
        )
        db_session.commit()
        return subscription

    @staticmethod
    def reactivate_subscription(db_session, *, subscription, user_id: int | None = None):
        subscription.cancel_at_period_end = False
        subscription.renewal_enabled = True
        subscription.auto_renew = True
        if subscription.status in {"cancelled", "expired", "suspended"}:
            subscription.status = "pending"
        NotificationService.record_event(
            db_session,
            company_id=subscription.company_id,
            subscription_id=subscription.id,
            event="subscription_reactivated",
            detail="El usuario reactivo renovacion automatica.",
            source="portal",
            status=subscription.status,
            user_id=user_id,
        )
        db_session.commit()
        return subscription
