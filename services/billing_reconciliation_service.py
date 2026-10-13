"""Idempotent repair of interrupted Mercado Pago AI-subscription webhooks.

Only events already persisted after the application's webhook signature check are
eligible. The worker re-fetches the authorized-payment resource from Mercado Pago
and re-validates its tenant/preapproval binding before changing any entitlement.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from threading import Lock

from sqlalchemy import text

from services.mercadopago_service import MercadoPagoService

logger = logging.getLogger(__name__)
_LOCAL_RUN_LOCK = Lock()


class BillingReconciliationService:
    """Repair incomplete AI recurring-payment webhook processing safely."""

    ADVISORY_LOCK_KEY = 7301000101
    RETRYABLE_EVENT_STATUSES = ("", "processing", "failed", "error", "retry", "pending")
    MAX_ATTEMPTS = 10
    DEFAULT_LIMIT = 100
    MAX_LIMIT = 500
    GRACE_PERIOD_SECONDS = 300

    @classmethod
    def run(cls, *, limit: int = DEFAULT_LIMIT) -> dict:
        from app import WebhookEvent, db

        try:
            selected_limit = max(1, min(int(limit), cls.MAX_LIMIT))
        except (TypeError, ValueError):
            selected_limit = cls.DEFAULT_LIMIT

        lock_connection = None
        local_lock_acquired = False
        if db.engine.dialect.name == "postgresql":
            lock_connection = db.engine.connect()
            try:
                acquired = bool(
                    lock_connection.execute(
                        text("SELECT pg_try_advisory_lock(:lock_key)"),
                        {"lock_key": cls.ADVISORY_LOCK_KEY},
                    ).scalar()
                )
                lock_connection.commit()
            except Exception:
                lock_connection.close()
                raise
            if not acquired:
                lock_connection.close()
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "already_running",
                    "events_scanned": 0,
                    "repaired": 0,
                    "ignored": 0,
                    "errors": 0,
                    "attention": 0,
                }
        else:
            local_lock_acquired = _LOCAL_RUN_LOCK.acquire(blocking=False)
            if not local_lock_acquired:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "already_running",
                    "events_scanned": 0,
                    "repaired": 0,
                    "ignored": 0,
                    "errors": 0,
                    "attention": 0,
                }

        summary = {
            "ok": True,
            "skipped": False,
            "events_scanned": 0,
            "repaired": 0,
            "ignored": 0,
            "errors": 0,
            "attention": 0,
        }
        try:
            cutoff = datetime.utcnow() - timedelta(seconds=cls.GRACE_PERIOD_SECONDS)
            event_ids = [
                int(row.id)
                for row in (
                    WebhookEvent.query.filter(
                        WebhookEvent.event_type == "subscription_authorized_payment",
                        WebhookEvent.status.in_(cls.RETRYABLE_EVENT_STATUSES),
                        WebhookEvent.created_at <= cutoff,
                    )
                    .order_by(WebhookEvent.created_at.asc(), WebhookEvent.id.asc())
                    .limit(selected_limit)
                    .all()
                )
            ]
            summary["events_scanned"] = len(event_ids)
            if not event_ids:
                return summary

            provider = MercadoPagoService()
            for event_id in event_ids:
                try:
                    outcome = cls._reconcile_event(event_id, provider=provider)
                    if outcome == "repaired":
                        summary["repaired"] += 1
                    elif outcome == "attention":
                        summary["attention"] += 1
                    else:
                        summary["ignored"] += 1
                except Exception as exc:
                    db.session.rollback()
                    cls._mark_retryable_failure(event_id, exc)
                    summary["errors"] += 1
                    logger.warning(
                        "Billing reconciliation failed event_id=%s error_type=%s",
                        event_id,
                        type(exc).__name__,
                    )
            summary["ok"] = summary["errors"] == 0 and summary["attention"] == 0
            return summary
        finally:
            if local_lock_acquired:
                _LOCAL_RUN_LOCK.release()
            if lock_connection is not None:
                try:
                    lock_connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {"lock_key": cls.ADVISORY_LOCK_KEY},
                    )
                    lock_connection.commit()
                except Exception:
                    logger.exception("Could not release billing reconciliation advisory lock.")
                finally:
                    lock_connection.close()


    @staticmethod
    def _parse_datetime(value):
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

    @classmethod
    def _reconcile_event(cls, event_id: int, *, provider) -> str:
        from app import Payment, WebhookEvent, db
        from services.ai_agent.subscription_service import AISubscriptionService

        event = (
            db.session.query(WebhookEvent)
            .filter_by(id=event_id)
            .with_for_update()
            .first()
        )
        if event is None:
            return "ignored"
        if (
            event.event_type != "subscription_authorized_payment"
            or str(event.status or "").strip().lower() not in cls.RETRYABLE_EVENT_STATUSES
        ):
            return "ignored"

        payload = json.loads(event.payload_json or "{}")
        if not isinstance(payload, dict):
            raise ValueError("invalid_persisted_webhook_payload")
        data = payload.get("data") or {}
        authorized_id = str(data.get("id") or payload.get("id") or "").strip()
        if not authorized_id:
            raise ValueError("missing_authorized_payment_id")

        # The stored event was signature-verified before insertion. Fetching the
        # current provider object prevents replaying untrusted client-supplied data.
        authorized = provider.get_authorized_payment(authorized_id)
        if str(authorized.get("id") or "").strip() != authorized_id:
            raise ValueError("authorized_payment_id_mismatch")

        preapproval_id = str(authorized.get("preapproval_id") or "").strip()
        payment_info = authorized.get("payment") or {}
        payment_id = str(payment_info.get("id") or "").strip()
        external_reference = str(authorized.get("external_reference") or "").strip()
        if not preapproval_id or not payment_id or not external_reference:
            raise ValueError("authorized_payment_missing_binding")

        company = AISubscriptionService.company_for_mercadopago_reference(
            preapproval_id=preapproval_id,
            external_reference=external_reference,
        )
        if company is None:
            return cls._reconcile_commercial_subscription_payment(
                event_id,
                authorized=authorized,
                payment_info=payment_info,
                preapproval_id=preapproval_id,
                payment_id=payment_id,
                external_reference=external_reference,
                provider=provider,
            )

        ai = AISubscriptionService._ai_prefs(company)
        if str(ai.get("origin") or "").strip().upper() != "MERCADO_PAGO":
            return cls._mark_attention(event_id, "subscription_not_provider_managed")
        if str(ai.get("mercadopago_preapproval_id") or "").strip() != preapproval_id:
            return cls._mark_attention(event_id, "preapproval_changed")
        saved_reference = str(ai.get("mercadopago_external_reference") or "").strip()
        if saved_reference and saved_reference != external_reference:
            return cls._mark_attention(event_id, "external_reference_mismatch")

        payment_status = str(
            payment_info.get("status") or authorized.get("summarized") or "pending"
        ).strip().lower()
        if payment_status != "approved":
            # This persisted notification only describes this specific delivery.
            # A subsequent state change arrives as a new webhook event. Do not
            # poll a pending/declined payment forever or let it starve real repairs.
            event.status = "reconciliation_not_approved"
            db.session.commit()
            return "ignored"

        existing = Payment.query.filter_by(payment_id=payment_id).first()
        existing_status = str(getattr(existing, "status", "") or "").strip().lower()
        if existing_status in {"refunded", "charged_back", "refund_pending"}:
            event.status = "reconciliation_ignored_reversed"
            db.session.commit()
            return "ignored"
        if existing is not None and (
            existing.company_id != company.id
            or str(existing.provider or "").strip() != "mercadopago_ai_subscription"
            or str(existing.reference or "").strip() != preapproval_id
        ):
            return cls._mark_attention(event_id, "payment_already_bound_elsewhere")

        amount_source = (
            authorized.get("transaction_amount")
            or payment_info.get("transaction_amount")
            or (existing.amount if existing is not None else None)
        )
        try:
            amount = float(amount_source)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("approved_payment_amount_missing") from exc
        currency = str(
            authorized.get("currency_id")
            or payment_info.get("currency_id")
            or (existing.currency if existing is not None else "")
            or ""
        ).strip().upper()
        expected_amount = AISubscriptionService.plan_amount_ars(str(ai.get("plan_code") or ""))
        if amount <= 0 or abs(expected_amount - amount) > 0.01 or currency != "ARS":
            return cls._mark_attention(event_id, "approved_payment_amount_or_currency_mismatch")

        preapproval = provider.get_preapproval(preapproval_id)
        if str(preapproval.get("id") or "").strip() != preapproval_id:
            raise ValueError("preapproval_id_mismatch")
        provider_reference = str(preapproval.get("external_reference") or "").strip()
        if provider_reference and provider_reference != external_reference:
            return cls._mark_attention(event_id, "provider_external_reference_mismatch")

        paid_at = (
            cls._parse_datetime(authorized.get("debit_date"))
            or cls._parse_datetime(payment_info.get("date_approved"))
            or cls._parse_datetime(authorized.get("date_approved"))
        )
        if existing is None:
            existing = Payment(
                payment_id=payment_id,
                external_reference=external_reference,
                company_id=company.id,
                subscription_id=None,
                user_id=None,
                amount=amount,
                currency=currency,
                status="approved",
                payment_method="mercadopago_ai_subscription",
                reference=preapproval_id,
                provider="mercadopago_ai_subscription",
                payload_json=json.dumps(authorized, ensure_ascii=False),
                paid_at=paid_at,
            )
            db.session.add(existing)
            db.session.flush()
        else:
            existing.external_reference = external_reference
            existing.amount = amount
            existing.currency = currency
            existing.status = "approved"
            existing.payment_method = "mercadopago_ai_subscription"
            existing.reference = preapproval_id
            existing.provider = "mercadopago_ai_subscription"
            existing.payload_json = json.dumps(authorized, ensure_ascii=False)
            existing.paid_at = paid_at or existing.paid_at

        # record_mercadopago_payment commits payment metadata and access credit
        # together. If provider synchronization then times out, the persisted event
        # remains retryable and this operation is safe to repeat.
        AISubscriptionService.record_mercadopago_payment(
            company,
            payment_id=payment_id,
            payment_status="approved",
            amount=amount,
            currency=currency,
            paid_at=existing.paid_at,
            preapproval_id=preapproval_id,
            external_reference=external_reference,
        )
        latest = AISubscriptionService._ai_prefs(company)
        if (
            str(latest.get("last_payment_id") or "").strip() == payment_id
            and str(latest.get("last_payment_status") or "").strip().lower() == "approved"
        ):
            synced = AISubscriptionService.sync_from_mercadopago(
                preapproval=preapproval,
                verified_payment_id=payment_id,
            )
            if synced is None:
                raise ValueError("verified_ai_preapproval_sync_failed")

        event = db.session.get(WebhookEvent, event_id)
        if event is not None:
            event.status = "processed_ai_subscription_payment"
        db.session.commit()
        return "repaired"


    @classmethod
    def _reconcile_commercial_subscription_payment(
        cls,
        event_id: int,
        *,
        authorized: dict,
        payment_info: dict,
        preapproval_id: str,
        payment_id: str,
        external_reference: str,
        provider,
    ) -> str:
        """Repair an interrupted recurring payment for the main SaaS subscription."""
        from app import Company, Payment, Subscription, WebhookEvent, db
        from services.billing_notification_service import NotificationService
        from services.invoice_service import InvoiceService
        from services.mercadopago_subscription_service import MercadoPagoSubscriptionService
        from services.referral_service import ReferralService
        from services.subscription_service import SubscriptionService

        ref_parts = {
            segment.split(":", 1)[0]: segment.split(":", 1)[1]
            for segment in str(external_reference or "").split("|")
            if ":" in segment
        }
        if ref_parts.get("ai_subscription") == "true":
            return cls._mark_attention(event_id, "ai_reference_without_matching_company")
        flow = str(ref_parts.get("flow") or "").strip().lower()
        if flow and flow != "subscription_auto":
            return cls._mark_attention(event_id, "unsupported_subscription_payment_flow")
        if not preapproval_id or not payment_id or not external_reference:
            return cls._mark_attention(event_id, "commercial_payment_binding_missing")

        try:
            referenced_company_id = int(ref_parts.get("company_id") or 0)
            referenced_subscription_id = int(ref_parts.get("subscription_id") or 0)
        except (TypeError, ValueError):
            return cls._mark_attention(event_id, "commercial_reference_invalid")
        if referenced_subscription_id <= 0:
            return cls._mark_attention(event_id, "commercial_subscription_id_missing")

        rows = (
            Subscription.query
            .filter(Subscription.metadata_json.contains(preapproval_id))
            .order_by(Subscription.id.asc())
            .all()
        )
        subscription = next(
            (
                row for row in rows
                if str(SubscriptionService._metadata_dict(row).get("mercadopago_preapproval_id") or "").strip()
                == preapproval_id
            ),
            None,
        )
        if subscription is None:
            return cls._mark_attention(event_id, "commercial_subscription_not_found")
        if subscription.id != referenced_subscription_id:
            return cls._mark_attention(event_id, "commercial_subscription_reference_mismatch")
        if referenced_company_id and int(subscription.company_id or 0) != referenced_company_id:
            return cls._mark_attention(event_id, "commercial_company_reference_mismatch")

        metadata = SubscriptionService._metadata_dict(subscription)
        saved_reference = str(metadata.get("mercadopago_external_reference") or "").strip()
        if saved_reference and saved_reference != external_reference:
            return cls._mark_attention(event_id, "commercial_external_reference_mismatch")
        if str(metadata.get("checkout_method") or "automatic").strip().lower() == "qr":
            return cls._mark_attention(event_id, "manual_qr_subscription_must_not_auto_renew")
        if not subscription.plan_id or subscription.plan is None:
            return cls._mark_attention(event_id, "commercial_subscription_plan_missing")

        payment_status = str(
            payment_info.get("status") or authorized.get("summarized") or "pending"
        ).strip().lower()
        if payment_status != "approved":
            event = db.session.get(WebhookEvent, event_id)
            if event is not None:
                event.status = "reconciliation_not_approved"
            db.session.commit()
            return "ignored"

        existing = Payment.query.filter_by(payment_id=payment_id).first()
        prior_status = str(getattr(existing, "status", "") or "").strip().lower()
        if prior_status in {"refunded", "charged_back", "refund_pending"}:
            event = db.session.get(WebhookEvent, event_id)
            if event is not None:
                event.status = "reconciliation_ignored_reversed"
            db.session.commit()
            return "ignored"
        if existing is not None and (
            (existing.company_id not in (None, subscription.company_id))
            or (existing.subscription_id not in (None, subscription.id))
            or str(existing.provider or "").strip() not in {"", "mercadopago_subscription"}
            or str(existing.reference or "").strip() not in {"", preapproval_id}
        ):
            return cls._mark_attention(event_id, "commercial_payment_already_bound_elsewhere")

        amount_source = (
            authorized.get("transaction_amount")
            or payment_info.get("transaction_amount")
            or (existing.amount if existing is not None else None)
        )
        try:
            amount = float(amount_source)
        except (TypeError, ValueError, OverflowError):
            return cls._mark_attention(event_id, "commercial_approved_payment_amount_missing")
        currency = str(
            authorized.get("currency_id")
            or payment_info.get("currency_id")
            or (existing.currency if existing is not None else "")
            or ""
        ).strip().upper()
        expected_amount = float(getattr(subscription.plan, "price", 0) or 0)
        expected_currency = str(getattr(subscription.plan, "currency", "ARS") or "ARS").strip().upper()
        if amount <= 0 or expected_amount <= 0 or abs(expected_amount - amount) > 0.01 or currency != expected_currency:
            return cls._mark_attention(event_id, "commercial_approved_payment_amount_or_currency_mismatch")

        preapproval = provider.get_preapproval(preapproval_id)
        if str(preapproval.get("id") or "").strip() != preapproval_id:
            raise ValueError("commercial_preapproval_id_mismatch")
        provider_reference = str(preapproval.get("external_reference") or "").strip()
        if provider_reference and provider_reference != external_reference:
            return cls._mark_attention(event_id, "commercial_provider_external_reference_mismatch")

        paid_at = (
            cls._parse_datetime(authorized.get("debit_date"))
            or cls._parse_datetime(payment_info.get("date_approved"))
            or cls._parse_datetime(authorized.get("date_approved"))
            or (getattr(existing, "paid_at", None) if existing is not None else None)
        )
        if existing is None:
            existing = Payment(
                payment_id=payment_id,
                external_reference=external_reference,
                company_id=subscription.company_id,
                subscription_id=subscription.id,
                user_id=None,
                amount=amount,
                currency=currency,
                status="approved",
                payment_method="mercadopago_subscription",
                reference=preapproval_id,
                provider="mercadopago_subscription",
                payload_json=json.dumps(authorized, ensure_ascii=False),
                paid_at=paid_at,
            )
            db.session.add(existing)
            db.session.flush()
        else:
            existing.external_reference = external_reference
            existing.company_id = subscription.company_id
            existing.subscription_id = subscription.id
            existing.amount = amount
            existing.currency = currency
            existing.status = "approved"
            existing.payment_method = existing.payment_method or "mercadopago_subscription"
            existing.reference = preapproval_id
            existing.provider = "mercadopago_subscription"
            existing.payload_json = json.dumps(authorized, ensure_ascii=False)
            existing.paid_at = paid_at or existing.paid_at

        # Persist the authoritative provider boundary before renewing locally.
        # SubscriptionService uses the same payment ID as its idempotency key,
        # preventing a duplicate month if a worker or web request retries.
        MercadoPagoSubscriptionService.sync_preapproval(
            db_session=db.session,
            preapproval=preapproval,
        )
        remote_status = str(preapproval.get("status") or "").strip().lower()
        SubscriptionService.run_command(
            db.session,
            SubscriptionService.RenewSubscriptionCommand(
                company_id=subscription.company_id,
                subscription_id=subscription.id,
                payment_status="approved",
                paid_at=existing.paid_at,
                renewal_enabled=(remote_status == "authorized"),
                actor_user_id=existing.user_id,
                actor_role="system",
                origin="subscription_authorized_payment",
                idempotency_key=f"mercadopago-payment-approved:{payment_id}",
            ),
        )

        company = db.session.get(Company, subscription.company_id)
        if company is None:
            return cls._mark_attention(event_id, "commercial_subscription_company_missing")
        company.active = True
        if existing.invoice_id is None:
            invoice = InvoiceService.create_invoice(
                db.session,
                company=company,
                subscription=subscription,
                amount=float(existing.amount or 0),
                currency=existing.currency or expected_currency,
                detail=f"Cobro recurrente Mercado Pago {payment_id}",
                payment_id=payment_id,
            )
            existing.invoice_id = invoice.id
        ReferralService.create_commission_for_sale(
            db.session,
            company_id=company.id,
            subscription=subscription,
            payment=existing,
            plan=subscription.plan,
        )
        NotificationService.record_event(
            db.session,
            company_id=subscription.company_id,
            payment_id=existing.id,
            subscription_id=subscription.id,
            invoice_id=existing.invoice_id,
            event="mercadopago_authorized_subscription_payment",
            detail=f"Pago recurrente {payment_id} recuperado por reconciliación.",
            source="mercadopago",
            status="approved",
            event_id=f"mercadopago-authorized-payment:{payment_id}",
            payload={"payment_id": payment_id, "reconciled": True},
            user_id=existing.user_id,
        )
        event = db.session.get(WebhookEvent, event_id)
        if event is not None:
            event.status = "processed_subscription_payment"
        db.session.commit()
        return "repaired"

    @classmethod
    def _mark_attention(cls, event_id: int, reason: str) -> str:
        from app import WebhookEvent, db

        event = db.session.get(WebhookEvent, event_id)
        if event is not None:
            event.status = "reconciliation_attention_needed"
            payload = json.loads(event.payload_json or "{}")
            if isinstance(payload, dict):
                payload["_billing_reconciliation"] = {
                    "attention": True,
                    "reason_code": reason,
                }
                event.payload_json = json.dumps(payload, ensure_ascii=False)
        db.session.commit()
        logger.error(
            "Billing reconciliation requires attention event_id=%s reason_code=%s",
            event_id,
            reason,
        )
        return "attention"

    @classmethod
    def _mark_retryable_failure(cls, event_id: int, exc: Exception) -> None:
        from app import WebhookEvent, db

        event = db.session.get(WebhookEvent, event_id)
        if event is None:
            return
        try:
            payload = json.loads(event.payload_json or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except (TypeError, ValueError):
            payload = {}
        attempts = payload.get("_billing_reconciliation_attempts", 0)
        try:
            attempts = max(0, int(attempts)) + 1
        except (TypeError, ValueError):
            attempts = 1
        payload["_billing_reconciliation_attempts"] = attempts
        payload["_billing_reconciliation_last_error"] = type(exc).__name__
        event.payload_json = json.dumps(payload, ensure_ascii=False)
        event.status = (
            "reconciliation_attention_needed"
            if attempts >= cls.MAX_ATTEMPTS
            else "failed"
        )
        db.session.commit()
        if attempts >= cls.MAX_ATTEMPTS:
            logger.error(
                "Billing reconciliation attempts exhausted event_id=%s error_type=%s",
                event_id,
                type(exc).__name__,
            )
