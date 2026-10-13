"""Orquestador de facturacion/suscripciones para checkout y renovaciones."""

from __future__ import annotations

import base64
import json
import math
from io import BytesIO

import qrcode

from services.billing_notification_service import NotificationService
from services.mercadopago_service import MercadoPagoService
from services.subscription_service import SubscriptionService


class BillingService:
    def __init__(self):
        self.mp_service = MercadoPagoService()

    def create_checkout_for_plan(self, *, db_session, company, plan, user, subscription=None):
        from app import Payment, PaymentHistory, Subscription

        plan_code = str(getattr(plan, "code", "") or "").strip().lower()
        try:
            amount = float(getattr(plan, "price", 0) or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("El plan seleccionado no tiene un importe válido para cobrar.") from exc
        if plan_code == "trial" or not math.isfinite(amount) or amount <= 0:
            raise ValueError("El plan de prueba o un plan sin precio no puede cobrarse por Mercado Pago. Elegí un plan pago.")

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
        target_subscription = (
            db_session.query(Subscription)
            .filter_by(id=target_subscription.id, company_id=company.id)
            .with_for_update()
            .first()
        )
        if target_subscription is None or target_subscription.plan_id != plan.id:
            raise ValueError("La suscripción pendiente no corresponde al plan seleccionado.")

        checkout_events = db_session.query(PaymentHistory).filter_by(
            company_id=company.id,
            subscription_id=target_subscription.id,
            event="checkout_preference_created",
        )
        checkout_attempts = checkout_events.count()
        latest_event = checkout_events.order_by(PaymentHistory.id.desc()).first()
        if latest_event is not None:
            try:
                existing_preference = json.loads(latest_event.payload_json or "{}")
            except (TypeError, ValueError):
                existing_preference = {}
            if isinstance(existing_preference, dict):
                preference_id = str(latest_event.event_id or existing_preference.get("id") or "").strip()
                checkout_url = str(
                    existing_preference.get("init_point")
                    or existing_preference.get("sandbox_init_point")
                    or ""
                ).strip()
                if preference_id and checkout_url:
                    payment = db_session.query(Payment).filter_by(
                        company_id=company.id,
                        subscription_id=target_subscription.id,
                        preference_id=preference_id,
                    ).first()
                    payment_status = str(getattr(payment, "status", "") or "").strip().lower()
                    if payment is None or payment_status in {"pending", "in_process", "authorized"}:
                        return {
                            "subscription": target_subscription,
                            "preference": existing_preference,
                            "checkout_status": "pending",
                            "reused": True,
                        }
                    if payment_status == "approved":
                        from app import utcnow

                        next_due = target_subscription.next_billing_date
                        if next_due is None or next_due > utcnow():
                            return {
                                "subscription": target_subscription,
                                "preference": existing_preference,
                                "checkout_status": "paid",
                                "reused": True,
                            }

        attempt = checkout_attempts + 1

        external_reference = (
            f"company_id:{company.id}|plan_id:{plan.id}|subscription_id:{target_subscription.id}|"
            f"user_id:{user.id}|checkout_attempt:{attempt}"
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
        SubscriptionService._set_metadata(target_subscription, {"checkout_method": "qr", "checkout_cancelled": False})
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
    def cancel_subscription(
        db_session, *, subscription, user_id: int | None = None,
        cancel_at_period_end: bool = True,
    ):
        """Cancel MP recurring billing and apply the caller's explicit access policy."""
        metadata = SubscriptionService._metadata_dict(subscription)
        preapproval_id = str(metadata.get("mercadopago_preapproval_id") or "").strip()
        remote_status = "not_configured"

        if preapproval_id:
            mp = MercadoPagoService()
            remote = mp.get_preapproval(preapproval_id)
            remote_status = str(remote.get("status") or "").strip().lower()
            if remote_status in {"authorized", "paused", "pending", "in_process"}:
                canceled = mp.cancel_preapproval(preapproval_id)
                remote_status = str(canceled.get("status") or "canceled").strip().lower()
                if remote_status not in {"canceled", "cancelled"}:
                    raise RuntimeError(
                        f"Mercado Pago no confirmó la cancelación de la suscripción automática (estado: {remote_status or 'desconocido'})."
                    )
            elif remote_status not in {"canceled", "cancelled", "expired", "rejected"}:
                raise RuntimeError(
                    f"No se puede cancelar la suscripción automática de Mercado Pago porque su estado actual es '{remote_status or 'desconocido'}'."
                )

        SubscriptionService.run_command(
            db_session,
            SubscriptionService.CancelSubscriptionCommand(
                company_id=subscription.company_id,
                subscription_id=subscription.id,
                actor_user_id=user_id,
                origin="portal",
                idempotency_key=BillingService._subscription_action_key("cancel", subscription, user_id),
                cancel_at_period_end=bool(cancel_at_period_end),
            ),
        )
        SubscriptionService._set_metadata(
            subscription,
            {
                "mercadopago_cancellation_requested": True,
                "mercadopago_cancellation_status": remote_status,
                "cancel_requested_by_user_id": user_id,
            },
        )
        NotificationService.record_event(
            db_session,
            company_id=subscription.company_id,
            subscription_id=subscription.id,
            event="subscription_cancel_requested",
            detail=(
                "Mercado Pago cancelado; el acceso pagado se conserva hasta el vencimiento."
                if cancel_at_period_end
                else "Contrato recurrente de Mercado Pago cancelado por administración; acceso local bloqueado según la acción administrativa."
            ),
            source="portal",
            status="cancelled",
            payload={"preapproval_id": preapproval_id, "remote_status": remote_status},
            user_id=user_id,
        )
        return subscription

    @staticmethod
    def suspend_subscription(db_session, *, subscription, user_id: int | None = None, reason: str = "superadmin_suspend"):
        """Pause MP recurring billing before applying an intentional local suspension."""
        metadata = SubscriptionService._metadata_dict(subscription)
        preapproval_id = str(metadata.get("mercadopago_preapproval_id") or "").strip()
        remote_status = "not_configured"
        if preapproval_id:
            mp = MercadoPagoService()
            remote = mp.get_preapproval(preapproval_id)
            if str(remote.get("id") or "").strip() != preapproval_id:
                raise RuntimeError("Mercado Pago devolvió una autorización diferente a la registrada.")
            remote_status = str(remote.get("status") or "").strip().lower()
            if remote_status in {"authorized", "pending", "in_process"}:
                remote = mp.update_preapproval(preapproval_id, {"status": "paused"})
                if str(remote.get("id") or "").strip() != preapproval_id:
                    raise RuntimeError("Mercado Pago devolvió una autorización diferente al suspender.")
                remote_status = str(remote.get("status") or "").strip().lower()
            if remote_status not in {"paused", "canceled", "cancelled", "expired", "rejected"}:
                raise RuntimeError(
                    f"Mercado Pago no confirmó la suspensión del contrato recurrente (estado: {remote_status or 'desconocido'})."
                )

        result = SubscriptionService.run_command(
            db_session,
            SubscriptionService.ExpireSubscriptionCommand(
                company_id=subscription.company_id,
                subscription_id=subscription.id,
                actor_user_id=user_id,
                actor_role="superadmin",
                origin="superadmin",
                idempotency_key=BillingService._subscription_action_key("suspend", subscription, user_id),
                reason=reason,
            ),
        )
        SubscriptionService._set_metadata(
            subscription,
            {"mercadopago_suspension_status": remote_status, "mercadopago_suspended_by_user_id": user_id},
        )
        NotificationService.record_event(
            db_session,
            company_id=subscription.company_id,
            subscription_id=subscription.id,
            event="subscription_suspended",
            detail="Suscripción suspendida por administración; estado recurrente verificado en Mercado Pago.",
            source="superadmin",
            status=remote_status,
            payload={"preapproval_id": preapproval_id or None, "remote_status": remote_status, "reason": reason},
            user_id=user_id,
        )
        return result

    @staticmethod
    def _subscription_action_key(action: str, subscription, user_id: int | None = None) -> str:
        """Stable across retries, but different after the subscription state changes."""
        import hashlib

        revision = "|".join([
            str(getattr(subscription, "status", "") or ""),
            str(getattr(subscription, "updated_at", "") or ""),
            str(getattr(subscription, "ends_at", "") or ""),
            str(bool(getattr(subscription, "renewal_enabled", False))),
            str(bool(getattr(subscription, "auto_renew", False))),
            str(bool(getattr(subscription, "cancel_at_period_end", False))),
        ])
        digest = hashlib.sha256(revision.encode("utf-8")).hexdigest()[:20]
        return f"{action}:{getattr(subscription, 'company_id', 0)}:{getattr(subscription, 'id', 0)}:{user_id or 0}:{digest}"

    @staticmethod
    def reactivate_subscription(db_session, *, subscription, user_id: int | None = None):
        """Reactivate only after Mercado Pago confirms authorization.

        Pending contracts send the payer back to authorization. A canceled
        contract gets a new preapproval and stays locally inactive until the
        verified webhook confirms it.
        """
        metadata = SubscriptionService._metadata_dict(subscription)
        preapproval_id = str(metadata.get("mercadopago_preapproval_id") or "").strip()

        if preapproval_id:
            mp = MercadoPagoService()
            remote = mp.get_preapproval(preapproval_id)
            remote_id = str(remote.get("id") or "").strip()
            remote_status = str(remote.get("status") or "").strip().lower()
            if remote_id != preapproval_id:
                raise RuntimeError("Mercado Pago devolvió una autorización diferente a la registrada.")

            if remote_status == "paused":
                remote = mp.update_preapproval(preapproval_id, {"status": "authorized"})
                if str(remote.get("id") or "").strip() != preapproval_id:
                    raise RuntimeError("Mercado Pago devolvió una autorización diferente al reactivar.")
                remote_status = str(remote.get("status") or "").strip().lower()
                if remote_status != "authorized":
                    raise RuntimeError(
                        "Mercado Pago no confirmó la reactivación; el acceso y la renovación local no se modificaron."
                    )

            if remote_status == "authorized":
                SubscriptionService.run_command(
                    db_session,
                    SubscriptionService.ReactivateSubscriptionCommand(
                        company_id=subscription.company_id,
                        subscription_id=subscription.id,
                        actor_user_id=user_id,
                        origin="portal",
                        idempotency_key=BillingService._subscription_action_key("reactivate", subscription, user_id),
                    ),
                )
                SubscriptionService._set_metadata(
                    subscription,
                    {"mercadopago_reactivation_init_point": None},
                )
                NotificationService.record_event(
                    db_session,
                    company_id=subscription.company_id,
                    subscription_id=subscription.id,
                    event="subscription_reactivated",
                    detail="Renovación automática reactivada y autorización confirmada por Mercado Pago.",
                    source="portal",
                    status=SubscriptionService.active_subscription_for_company(subscription.company_id).status,
                    user_id=user_id,
                )
                return subscription

            if remote_status in {"pending", "in_process"}:
                # Never turn local auto-renewal back on until the payer completes
                # authorization and Mercado Pago sends the verified confirmation.
                init_point = str(remote.get("init_point") or "").strip()
                if not init_point:
                    from urllib.parse import urlencode
                    init_point = "https://www.mercadopago.com.ar/subscriptions/checkout?" + urlencode({"preapproval_id": preapproval_id})
                SubscriptionService._set_metadata(
                    subscription,
                    {
                        "mercadopago_status": remote_status,
                        "mercadopago_reactivation_init_point": init_point,
                    },
                )
                NotificationService.record_event(
                    db_session,
                    company_id=subscription.company_id,
                    subscription_id=subscription.id,
                    event="subscription_reactivation_authorization_pending",
                    detail="La autorización de Mercado Pago sigue pendiente; no se habilitó la renovación local.",
                    source="portal",
                    status="pending",
                    payload={"preapproval_id": preapproval_id, "init_point": init_point},
                    user_id=user_id,
                )
                db_session.flush()
                return subscription

            if remote_status in {"cancelled", "canceled", "expired", "rejected"}:
                from app import Company, User
                from config.billing_config import load_billing_config
                from services.mercadopago_subscription_service import MercadoPagoSubscriptionService

                company = db_session.get(Company, subscription.company_id)
                plan = getattr(subscription, "plan", None)
                if company is None or plan is None:
                    raise RuntimeError("No se pudo determinar la empresa o el plan para crear una nueva autorización.")
                user = db_session.get(User, user_id) if user_id else None
                payer_email = (getattr(user, "email", None) or getattr(company, "contact_email", None) or "").strip()
                if not payer_email or "@" not in payer_email:
                    raise RuntimeError("La empresa necesita un email válido para reactivar el cobro automático.")
                config = load_billing_config()
                response = MercadoPagoSubscriptionService.create(
                    db_session=db_session,
                    company=company,
                    subscription=subscription,
                    plan=plan,
                    payer_email=payer_email,
                    notification_url=config.notification_url,
                    back_url=config.success_url,
                )
                init_point = str(response.get("init_point") or "").strip()
                if not init_point:
                    raise RuntimeError(
                        "Mercado Pago creó o encontró la autorización, pero no devolvió el enlace para completarla. "
                        "No se habilitó la renovación local."
                    )
                SubscriptionService._set_metadata(
                    subscription,
                    {"mercadopago_reactivation_init_point": init_point},
                )
                NotificationService.record_event(
                    db_session,
                    company_id=subscription.company_id,
                    subscription_id=subscription.id,
                    event="subscription_reactivation_authorization_pending",
                    detail="Se generó una nueva autorización de Mercado Pago; la activación local queda pendiente del webhook.",
                    source="portal",
                    status="pending",
                    payload={"preapproval_id": response.get("id"), "init_point": init_point},
                    user_id=user_id,
                )
                db_session.flush()
                return subscription

            raise RuntimeError(
                f"No se puede reactivar la suscripción: Mercado Pago informó el estado "
                f"'{remote_status or 'desconocido'}'."
            )

        SubscriptionService.run_command(
            db_session,
            SubscriptionService.ReactivateSubscriptionCommand(
                company_id=subscription.company_id,
                subscription_id=subscription.id,
                actor_user_id=user_id,
                origin="portal",
                idempotency_key=BillingService._subscription_action_key("reactivate", subscription, user_id),
            ),
        )
        NotificationService.record_event(
            db_session,
            company_id=subscription.company_id,
            subscription_id=subscription.id,
            event="subscription_reactivated",
            detail="El usuario reactivó la renovación de la suscripción administrada localmente.",
            source="portal",
            status=SubscriptionService.active_subscription_for_company(subscription.company_id).status,
            user_id=user_id,
        )
        return subscription
