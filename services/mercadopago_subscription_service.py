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
        existing_reference = str(metadata.get("mercadopago_external_reference") or "").strip()
        terminal_statuses = {"cancelled", "canceled", "expired", "rejected"}
        if existing_id:
            current = MercadoPagoService().get_preapproval(existing_id)
            current_status = str(current.get("status") or "").strip().lower()
            current_init_point = str(current.get("init_point") or "").strip()
            if current_status == "authorized":
                # Authorized preapprovals are already operational. Mercado Pago
                # may omit init_point once the payer has completed authorization.
                SubscriptionService._set_metadata(
                    subscription,
                    {
                        "mercadopago_status": current_status,
                        "checkout_method": "automatic",
                        "checkout_cancelled": False,
                        "payment_method": "mercadopago_subscription",
                        "auto_renew": True,
                    },
                )
                # A retry can be the first request after Mercado Pago has already
                # authorized the contract (for example, if the webhook was delayed).
                # Reconcile local flags from the verified resource rather than returning
                # success while automatic billing remains disabled locally.
                subscription.renewal_enabled = True
                subscription.auto_renew = True
                subscription.cancel_at_period_end = False
                if hasattr(subscription, "status"):
                    local_status = SubscriptionService._normalize_state(subscription.status)
                    if local_status not in SubscriptionService.ACTIVE_STATUSES:
                        SubscriptionService._transition(
                            subscription,
                            SubscriptionService.STATE_ACTIVE,
                            reason="mercadopago_preapproval_authorized_on_retry",
                        )
                next_payment = cls._parse_datetime(current.get("next_payment_date"))
                if next_payment is not None:
                    subscription.next_billing_date = next_payment
                    subscription.ends_at = next_payment
                return current
            if current_status in {"pending", "in_process"}:
                # A valid pending preapproval can occasionally be returned without
                # init_point on GET. Rebuild the documented checkout URL from this
                # exact preapproval ID; never POST a second recurring contract just
                # because Mercado Pago omitted the redirect field.
                if not current_init_point:
                    from urllib.parse import urlencode
                    current_init_point = (
                        "https://www.mercadopago.com.ar/subscriptions/checkout?"
                        + urlencode({"preapproval_id": existing_id})
                    )
                    current["init_point"] = current_init_point
                SubscriptionService._set_metadata(
                    subscription,
                    {
                        "mercadopago_status": current_status,
                        "mercadopago_external_reference": str(current.get("external_reference") or existing_reference or "").strip() or None,
                        "mercadopago_creation_pending": False,
                        "checkout_method": "automatic",
                        "checkout_cancelled": False,
                        "payment_method": "mercadopago_subscription",
                    },
                )
                return current
            if current_status in terminal_statuses:
                # A terminated authorization can be replaced, but an active or
                # unknown preapproval must never be silently orphaned.
                existing_reference = ""
                SubscriptionService._set_metadata(
                    subscription,
                    {
                        "mercadopago_previous_preapproval_id": existing_id,
                        "mercadopago_preapproval_id": None,
                        "mercadopago_status": current_status,
                    },
                )
                metadata = cls._metadata(subscription)
                db_session.flush()
            else:
                raise RuntimeError(
                    f"La suscripción de Mercado Pago {existing_id} está en estado no terminal '{current_status or 'desconocido'}'. "
                    "No se creará otra autorización mensual para evitar cobros duplicados."
                )

        external_reference = existing_reference or cls._external_reference(company_id=company.id, subscription_id=subscription.id)
        # Persist the attempt reference BEFORE calling Mercado Pago. If the request
        # times out after MP creates the preapproval, a retry reuses the exact same
        # reference and idempotency key, so it cannot create another subscription.
        if external_reference != str(metadata.get("mercadopago_external_reference") or "").strip() or not metadata.get("mercadopago_creation_pending"):
            SubscriptionService._set_metadata(
                subscription,
                {
                    "mercadopago_external_reference": external_reference,
                    "mercadopago_creation_pending": True,
                    "payment_method": "mercadopago_subscription",
                    "checkout_method": "automatic",
                    "checkout_cancelled": False,
                    "auto_renew": True,
                },
            )
            db_session.commit()

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
        if not preapproval_id:
            raise RuntimeError("Mercado Pago no devolvió el identificador de la suscripción automática.")
        response_status = str(response.get("status") or "pending").strip().lower()
        if not init_point and response_status in {"pending", "in_process"}:
            # Recovery path for an API response with an ID but no redirect URL.
            # The existing preapproval ID is enough to reopen the payer's
            # authorization page and must not trigger a second preapproval POST.
            from urllib.parse import urlencode
            init_point = (
                "https://www.mercadopago.com.ar/subscriptions/checkout?"
                + urlencode({"preapproval_id": preapproval_id})
            )
            response["init_point"] = init_point
        if not init_point and response_status != "authorized":
            raise RuntimeError(
                "Mercado Pago devolvió una suscripción sin enlace de autorización. "
                "Se conservó el intento para reintentar sin duplicar el contrato."
            )

        SubscriptionService._set_metadata(
            subscription,
            {
                "mercadopago_preapproval_id": preapproval_id,
                "mercadopago_status": response_status,
                "mercadopago_payer_email": payer_email,
                "mercadopago_external_reference": external_reference,
                "mercadopago_creation_pending": False,
                "payment_method": "mercadopago_subscription",
                "checkout_method": "automatic",
                "checkout_cancelled": False,
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

        if str(metadata.get("closed_reason") or "").strip().lower() == "plan_change":
            metadata["mercadopago_stale_status_ignored"] = status
            metadata["mercadopago_stale_status_ignored_at"] = datetime.now(timezone.utc).isoformat()
            SubscriptionService._set_metadata(subscription, metadata)
            subscription.renewal_enabled = False
            subscription.auto_renew = False
            subscription.cancel_at_period_end = True
            db_session.flush()
            return subscription

        if status == "authorized" and metadata.get("pending_plan_change"):
            # A new monthly preapproval is only switched on after Mercado Pago
            # confirms authorization. Retire the previous tenant subscription
            # before transitioning the pending row to active (single-active
            # company constraint) and prevent the old preapproval charging again.
            previous_id_raw = metadata.get("previous_subscription_id")
            previous_subscription = None
            if str(previous_id_raw or "").isdigit():
                previous_subscription = Subscription.query.filter_by(
                    id=int(previous_id_raw),
                    company_id=subscription.company_id,
                ).first()
            if previous_subscription is not None and previous_subscription.id != subscription.id:
                previous_metadata = SubscriptionService._metadata_dict(previous_subscription)
                previous_preapproval_id = str(previous_metadata.get("mercadopago_preapproval_id") or "").strip()
                if previous_preapproval_id and previous_preapproval_id != preapproval_id:
                    from services.mercadopago_service import MercadoPagoService

                    previous_remote = MercadoPagoService().get_preapproval(previous_preapproval_id)
                    previous_status = str(previous_remote.get("status") or "").strip().lower()
                    if previous_status not in {"cancelled", "canceled", "expired"}:
                        previous_cancelled = MercadoPagoService().cancel_preapproval(previous_preapproval_id)
                        previous_status = str(previous_cancelled.get("status") or "").strip().lower()
                    if previous_status not in {"cancelled", "canceled", "expired"}:
                        raise RuntimeError(
                            "Mercado Pago autorizó el nuevo plan, pero no confirmó la cancelación del preapproval anterior."
                        )
                    SubscriptionService._set_metadata(
                        previous_subscription,
                        {
                            "mercadopago_status": previous_status,
                            "mercadopago_cancelled_for_plan_change_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                change_at = datetime.now(timezone.utc).replace(tzinfo=None)
                SubscriptionService._close_for_change(
                    previous_subscription,
                    now=change_at,
                    actor_user_id=None,
                    origin="mercadopago_preapproval_authorized",
                )
                metadata.update({
                    "pending_plan_change": False,
                    "plan_change_applied_at": change_at.isoformat(),
                    "replaced_subscription_id": previous_subscription.id,
                })
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

            # Mercado Pago stops future charges immediately, but the local
            # subscription must preserve any period that has already been paid.
            # Treat the remote contract status and local access entitlement as
            # separate facts; access is resolved against next_billing_date.
            paid_until = subscription.next_billing_date or subscription.ends_at
            if paid_until is not None and getattr(paid_until, "tzinfo", None) is not None:
                paid_until = paid_until.astimezone(timezone.utc).replace(tzinfo=None)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            local_status = SubscriptionService._normalize_state(subscription.status)
            has_paid_access = bool(
                local_status in SubscriptionService.ACTIVE_STATUSES
                and paid_until is not None
                and paid_until > now
            )

            if status in {"cancelled", "canceled"} and not has_paid_access and local_status not in {
                SubscriptionService.STATE_CANCELLED,
                SubscriptionService.STATE_EXPIRED,
            }:
                SubscriptionService._transition(
                    subscription,
                    SubscriptionService.STATE_CANCELLED,
                    reason="mercadopago_preapproval_cancelled",
                )
            elif has_paid_access:
                metadata["mercadopago_access_preserved_until"] = paid_until.isoformat()
                metadata["mercadopago_access_preserved_reason"] = "recurring_contract_ended"
                SubscriptionService._set_metadata(subscription, metadata)
        elif status == "pending":
            subscription.renewal_enabled = False
            subscription.auto_renew = False
            subscription.cancel_at_period_end = True

        db_session.flush()
        return subscription
