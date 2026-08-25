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
            title=f"StockarMobile - {plan.name}" if False else f"StockarMobile - {plan.name}",
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
        """Reactivate only after Mercado Pago confirms an authorized state.

        If the previous preapproval was canceled/expired, create a fresh authorization
        and leave the local subscription disabled until the webhook confirms it.
        """
        metadata = SubscriptionService._metadata_dict(subscription)
        preapproval_id = str(metadata.get("mercadopago_preapproval_id") or "").strip()

        if preapproval_id:
            mp = MercadoPagoService()
            remote = mp.get_preapproval(preapproval_id)
            remote_status = str(remote.get("status") or "").strip().lower()

            if remote_status in {"authorized", "paused", "pending", "in_process"}:
                if remote_status == "paused":
                    remote = mp.update_preapproval(preapproval_id, {"status": "authorized"})
                    remote_status = str(remote.get("status") or "").strip().lower()
                elif remote_status in {"pending", "in_process"}:
                    remote = mp.update_preapproval(preapproval_id, {"status": "authorized"})
                    remote_status = str(remote.get("status") or "").strip().lower()

                if remote_status == "authorized":
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
                        detail="Renovacion automatica reactivada y autorizada en Mercado Pago.",
                        source="portal",
                        status=SubscriptionService.active_subscription_for_company(subscription.company_id).status,
                        user_id=user_id,
                    )
                    return subscription

            if remote_status in {"cancelled", "canceled", "expired"}:
                from app import Company
                from services.mercadopago_subscription_service import MercadoPagoSubscriptionService
                company = db_session.get(Company, subscription.company_id)
                plan = getattr(subscription, "plan", None)
                if company is None or plan is None:
                    raise RuntimeError("No se pudo determinar la empresa o el plan para crear una nueva autorización.")
                payer_email = (getattr(company, "contact_email", None) or "").strip()
                if not payer_email or "@" not in payer_email:
                    raise RuntimeError("La empresa necesita un email válido para reactivar el cobro automático.")
                config = __import__("config.billing_config", fromlist=["load_billing_config"]).load_billing_config()
                response = MercadoPagoSubscriptionService.create(
                    db_session=db_session,
                    company=company,
                    subscription=subscription,
                    plan=plan,
                    payer_email=payer_email,
                    notification_url=config.notification_url,
                    back_url=config.success_url,
                )
                NotificationService.record_event(
                    db_session,
                    company_id=subscription.company_id,
                    subscription_id=subscription.id,
                    event="subscription_reactivation_authorization_pending",
                    detail="La suscripción anterior de Mercado Pago estaba cancelada/vencida. Se generó una nueva autorización; la activación local queda pendiente del webhook.",
                    source="portal",
                    status="pending",
                    payload={"preapproval_id": response.get("id"), "init_point": response.get("init_point")},
                    user_id=user_id,
                )
                db_session.flush()
                return subscription

        # No Mercado Pago preapproval exists: preserve the existing local behavior.
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
