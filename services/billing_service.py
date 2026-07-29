"""Orquestador de facturacion/suscripciones para checkout y renovaciones."""

from __future__ import annotations

import base64
from io import BytesIO

import qrcode

from services.billing_notification_service import NotificationService
from services.mercadopago_service import MercadoPagoService
from services.subscription_service import SubscriptionService


class BillingService:
    def __init__(self):
        self.mp_service = MercadoPagoService()

    def create_checkout_for_plan(self, *, db_session, company, plan, user, subscription=None):
        from app import utcnow

        target_subscription = subscription
        if target_subscription is None:
            result = SubscriptionService.change_plan_transaction(
                db_session,
                company=company,
                plan=plan,
                actor_user_id=user.id,
                origin="billing_checkout",
                managed_by="system",
            )
            target_subscription = result["subscription"]
        db_session.flush()

        external_reference = (
            f"company_id:{company.id}|plan_id:{plan.id}|subscription_id:{target_subscription.id}|"
            f"user_id:{user.id}|ts:{int(utcnow().timestamp())}"
        )
        SubscriptionService.run_command(
            db_session,
            SubscriptionService.ChangePaymentMethodCommand(
                company_id=company.id,
                subscription_id=target_subscription.id,
                actor_user_id=user.id,
                actor_role=getattr(user, "role", None),
                origin="checkout",
                idempotency_key=f"payment-method:{company.id}:{target_subscription.id}:{external_reference}",
                payment_method="mercadopago",
                external_reference=external_reference,
            ),
        )

        preference = self.mp_service.create_checkout_preference(
            title=f"StockArmobile - {plan.name}",
            amount=float(plan.price or 0),
            currency=plan.currency or "ARS",
            external_reference=external_reference,
            company_id=company.id,
            plan_id=plan.id,
            subscription_id=target_subscription.id,
            user_id=user.id,
        )

        NotificationService.record_event(
            db_session,
            company_id=company.id,
            subscription_id=target_subscription.id,
            event="checkout_preference_created",
            detail=f"Preference {preference.get('id')} para plan {plan.name}",
            source="mercadopago",
            status="pending",
            event_id=str(preference.get("id") or ""),
            payload=preference,
            user_id=user.id,
        )
        return {"subscription": target_subscription, "preference": preference}

    @staticmethod
    def checkout_preview_payload(*, preference: dict, plan, company) -> dict:
        checkout_url = preference.get("init_point") or preference.get("sandbox_init_point") or ""
        return {
            "preference_id": preference.get("id"),
            "checkout_url": checkout_url,
            "plan_name": getattr(plan, "name", "Plan"),
            "amount": float(getattr(plan, "price", 0) or 0),
            "currency": getattr(plan, "currency", "ARS") or "ARS",
            "company_name": getattr(company, "name", ""),
            "qr_data_uri": BillingService._qr_data_uri(checkout_url) if checkout_url else "",
        }

    @staticmethod
    def _qr_data_uri(content: str) -> str:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
        qr.add_data(content)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    @staticmethod
    def cancel_subscription(db_session, *, subscription, user_id: int | None = None):
        SubscriptionService.run_command(
            db_session,
            SubscriptionService.CancelSubscriptionCommand(
                company_id=subscription.company_id,
                subscription_id=subscription.id,
                actor_user_id=user_id,
                origin="portal",
                idempotency_key=f"cancel:{subscription.company_id}:{subscription.id}:{user_id or 0}",
                cancel_at_period_end=True,
            ),
        )
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
        return subscription

    @staticmethod
    def reactivate_subscription(db_session, *, subscription, user_id: int | None = None):
        SubscriptionService.run_command(
            db_session,
            SubscriptionService.ReactivateSubscriptionCommand(
                company_id=subscription.company_id,
                subscription_id=subscription.id,
                actor_user_id=user_id,
                origin="portal",
                idempotency_key=f"reactivate:{subscription.company_id}:{subscription.id}:{user_id or 0}",
            ),
        )
        NotificationService.record_event(
            db_session,
            company_id=subscription.company_id,
            subscription_id=subscription.id,
            event="subscription_reactivated",
            detail="El usuario reactivo renovacion automatica.",
            source="portal",
            status=SubscriptionService.active_subscription_for_company(subscription.company_id).status,
            user_id=user_id,
        )
        return subscription
