from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal


def test_reconciler_repairs_interrupted_ai_payment_once(app, monkeypatch):
    from app import Company, Payment, WebhookEvent, db
    from services.ai_agent.subscription_service import AISubscriptionService
    from services.billing_reconciliation_service import BillingReconciliationService

    external_reference = "stockarmobile|ai_subscription:true|company_id:PLACEHOLDER"
    with app.app_context():
        company = Company(
            name="Billing reconciliation QA",
            active=True,
            preferences_json="{}",
        )
        db.session.add(company)
        db.session.flush()
        reference = external_reference.replace("PLACEHOLDER", str(company.id))
        company.preferences_json = json.dumps({
            "ai_agent": {
                "plan_code": "pro",
                "status": "PENDIENTE",
                "origin": "MERCADO_PAGO",
                "mercadopago_preapproval_id": "preapproval-reconcile-qa",
                "mercadopago_external_reference": reference,
                "mercadopago_status": "pending",
                "checkout_method": "automatic",
            }
        })
        event = WebhookEvent(
            provider="mercadopago",
            event_key="subscription_authorized_payment:qa-reconcile",
            event_type="subscription_authorized_payment",
            status="processing",
            payload_json=json.dumps({
                "type": "subscription_authorized_payment",
                "data": {"id": "authorized-payment-reconcile-qa"},
            }),
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.session.add(event)
        company_id = company.id
        db.session.commit()
        event_id = event.id

    paid_at = datetime.now(timezone.utc).replace(microsecond=0)
    next_payment = (paid_at + timedelta(days=30)).isoformat()
    amount = AISubscriptionService.plan_amount_ars("pro")

    class FakeMercadoPago:
        def get_authorized_payment(self, authorized_id):
            assert authorized_id == "authorized-payment-reconcile-qa"
            return {
                "id": authorized_id,
                "preapproval_id": "preapproval-reconcile-qa",
                "external_reference": reference,
                "transaction_amount": amount,
                "currency_id": "ARS",
                "debit_date": paid_at.isoformat(),
                "payment": {
                    "id": "payment-reconcile-qa",
                    "status": "approved",
                    "transaction_amount": amount,
                    "currency_id": "ARS",
                },
            }

        def get_preapproval(self, preapproval_id):
            assert preapproval_id == "preapproval-reconcile-qa"
            return {
                "id": preapproval_id,
                "external_reference": reference,
                "status": "authorized",
                "next_payment_date": next_payment,
            }

    monkeypatch.setattr(
        "services.billing_reconciliation_service.MercadoPagoService",
        FakeMercadoPago,
    )

    with app.app_context():
        first = BillingReconciliationService.run()
        persisted = db.session.get(WebhookEvent, event_id)
        company = db.session.get(Company, company_id)
        ai = json.loads(company.preferences_json)["ai_agent"]
        payment = Payment.query.filter_by(payment_id="payment-reconcile-qa").first()

        assert first["ok"] is True
        assert first["events_scanned"] == 1
        assert first["repaired"] == 1
        assert persisted.status == "processed_ai_subscription_payment"
        assert payment is not None and payment.status == "approved"
        assert ai["status"] == "ACTIVA"
        assert ai["mercadopago_paid_payment_ids"].count("payment-reconcile-qa") == 1
        assert datetime.fromisoformat(ai["ends_at"]) == datetime.fromisoformat(next_payment).replace(tzinfo=None)

        second = BillingReconciliationService.run()
        db.session.refresh(payment)
        ai_after_retry = json.loads(company.preferences_json)["ai_agent"]
        assert second["events_scanned"] == 0
        assert ai_after_retry["mercadopago_paid_payment_ids"].count("payment-reconcile-qa") == 1


def test_reconciler_refuses_non_ai_or_unmatched_preapproval(app, monkeypatch):
    from app import Company, WebhookEvent, db
    from services.billing_reconciliation_service import BillingReconciliationService

    with app.app_context():
        company = Company(name="Unmatched reconcile QA", active=True, preferences_json="{}")
        db.session.add(company)
        db.session.flush()
        event = WebhookEvent(
            provider="mercadopago",
            event_key="subscription_authorized_payment:qa-unmatched",
            event_type="subscription_authorized_payment",
            status="processing",
            payload_json=json.dumps({
                "type": "subscription_authorized_payment",
                "data": {"id": "authorized-payment-unmatched"},
            }),
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.session.add(event)
        db.session.commit()
        event_id = event.id

    class FakeMercadoPago:
        def get_authorized_payment(self, authorized_id):
            return {
                "id": authorized_id,
                "preapproval_id": "unknown-preapproval",
                "external_reference": "flow:commercial_checkout|company_id:1",
                "transaction_amount": 500,
                "currency_id": "ARS",
                "payment": {"id": "unmatched-payment", "status": "approved"},
            }

        def get_preapproval(self, preapproval_id):
            raise AssertionError("must reject an unmatched reference before provider preapproval lookup")

    monkeypatch.setattr(
        "services.billing_reconciliation_service.MercadoPagoService",
        FakeMercadoPago,
    )
    with app.app_context():
        result = BillingReconciliationService.run()
        persisted = db.session.get(WebhookEvent, event_id)
        assert result["ok"] is False
        assert result["attention"] == 1
        assert persisted.status == "reconciliation_attention_needed"
        assert "flow:commercial_checkout" not in persisted.payload_json



def test_reconciler_closes_pending_payment_event_without_granting_access(app, monkeypatch):
    from app import Company, Payment, WebhookEvent, db
    from services.billing_reconciliation_service import BillingReconciliationService

    with app.app_context():
        company = Company(name="Pending reconcile QA", active=True, preferences_json="{}")
        db.session.add(company)
        db.session.flush()
        reference = f"stockarmobile|ai_subscription:true|company_id:{company.id}"
        company.preferences_json = json.dumps({
            "ai_agent": {
                "plan_code": "pro",
                "status": "PENDIENTE",
                "origin": "MERCADO_PAGO",
                "mercadopago_preapproval_id": "preapproval-pending-qa",
                "mercadopago_external_reference": reference,
            }
        })
        event = WebhookEvent(
            provider="mercadopago",
            event_key="subscription_authorized_payment:qa-pending",
            event_type="subscription_authorized_payment",
            status="processing",
            payload_json=json.dumps({
                "type": "subscription_authorized_payment",
                "data": {"id": "authorized-payment-pending-qa"},
            }),
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.session.add(event)
        db.session.commit()
        event_id = event.id
        company_id = company.id

    class FakeMercadoPago:
        def get_authorized_payment(self, authorized_id):
            return {
                "id": authorized_id,
                "preapproval_id": "preapproval-pending-qa",
                "external_reference": reference,
                "transaction_amount": 1000,
                "currency_id": "ARS",
                "payment": {
                    "id": "payment-pending-qa",
                    "status": "pending",
                    "transaction_amount": 1000,
                    "currency_id": "ARS",
                },
            }

        def get_preapproval(self, preapproval_id):
            raise AssertionError("a non-approved payment must not trigger access synchronization")

    monkeypatch.setattr(
        "services.billing_reconciliation_service.MercadoPagoService",
        FakeMercadoPago,
    )
    with app.app_context():
        result = BillingReconciliationService.run()
        event = db.session.get(WebhookEvent, event_id)
        company = db.session.get(Company, company_id)
        assert result["ok"] is True
        assert result["ignored"] == 1
        assert event.status == "reconciliation_not_approved"
        assert Payment.query.filter_by(payment_id="payment-pending-qa").first() is None
        assert json.loads(company.preferences_json)["ai_agent"]["status"] == "PENDIENTE"



def test_reconciler_repairs_interrupted_commercial_subscription_payment_once(app, monkeypatch):
    import json
    from datetime import datetime, timedelta, timezone

    from app import Company, Payment, Plan, Subscription, WebhookEvent, db
    from services.billing_reconciliation_service import BillingReconciliationService
    from services.subscription_service import SubscriptionService

    with app.app_context():
        company = Company(name="Commercial reconcile QA", active=True)
        plan = Plan(
            code="reconcile-business",
            name="Reconcile Business",
            price=Decimal("12999.00"),
            currency="ARS",
            duration_days=30,
            state="active",
            active=True,
        )
        db.session.add_all([company, plan])
        db.session.flush()
        subscription_id = 0
        preapproval_id = "preapproval-commercial-reconcile-qa"
        external_reference = (
            f"stockarmobile|flow:subscription_auto|company_id:{company.id}|"
            f"subscription_id:PLACEHOLDER|nonce:reconcile-qa"
        )
        subscription = Subscription(
            company_id=company.id,
            plan_id=plan.id,
            status=SubscriptionService.STATE_ACTIVE,
            starts_at=datetime.utcnow() - timedelta(days=30),
            start_date=datetime.utcnow() - timedelta(days=30),
            next_billing_date=datetime.utcnow() + timedelta(days=1),
            ends_at=datetime.utcnow() + timedelta(days=1),
            renewal_enabled=True,
            auto_renew=True,
            external_reference=external_reference,
            metadata_json="{}",
        )
        db.session.add(subscription)
        db.session.flush()
        subscription_id = subscription.id
        external_reference = external_reference.replace("PLACEHOLDER", str(subscription.id))
        subscription.metadata_json = json.dumps({
            "mercadopago_preapproval_id": preapproval_id,
            "mercadopago_external_reference": external_reference,
            "checkout_method": "automatic",
            "flow": "subscription_auto",
        })
        event = WebhookEvent(
            provider="mercadopago",
            event_key="subscription_authorized_payment:notification:commercial-qa",
            event_type="subscription_authorized_payment",
            status="failed",
            payload_json=json.dumps({
                "type": "subscription_authorized_payment",
                "id": "commercial-qa",
                "data": {"id": "authorized-commercial-qa"},
            }),
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.session.add(event)
        company_id = company.id
        plan_id = plan.id
        db.session.commit()
        event_id = event.id

    paid_at = datetime.now(timezone.utc).replace(microsecond=0)
    next_payment = (paid_at + timedelta(days=30)).isoformat()

    class FakeMercadoPago:
        def get_authorized_payment(self, authorized_id):
            return {
                "id": authorized_id,
                "preapproval_id": preapproval_id,
                "external_reference": external_reference,
                "transaction_amount": 12999.0,
                "currency_id": "ARS",
                "debit_date": paid_at.isoformat(),
                "payment": {
                    "id": "payment-commercial-reconcile-qa",
                    "status": "approved",
                    "transaction_amount": 12999.0,
                    "currency_id": "ARS",
                },
            }

        def get_preapproval(self, requested_id):
            assert requested_id == preapproval_id
            return {
                "id": requested_id,
                "external_reference": external_reference,
                "status": "authorized",
                "next_payment_date": next_payment,
            }

    monkeypatch.setattr(
        "services.billing_reconciliation_service.MercadoPagoService",
        FakeMercadoPago,
    )

    with app.app_context():
        first = BillingReconciliationService.run()
        event_row = db.session.get(WebhookEvent, event_id)
        company = db.session.get(Company, company_id)
        subscription = db.session.get(Subscription, subscription_id)
        payment = Payment.query.filter_by(payment_id="payment-commercial-reconcile-qa").first()
        assert first["ok"] is True
        assert first["repaired"] == 1
        assert event_row.status == "processed_subscription_payment"
        assert payment is not None and payment.status == "approved"
        assert payment.company_id == company_id and payment.subscription_id == subscription_id
        assert company.active is True
        assert subscription.renewal_enabled is True
        assert datetime.fromisoformat(subscription.ends_at.isoformat()) == datetime.fromisoformat(next_payment).replace(tzinfo=None)

        second = BillingReconciliationService.run()
        assert second["events_scanned"] == 0
        assert Payment.query.filter_by(payment_id="payment-commercial-reconcile-qa").count() == 1



def test_failed_signed_authorized_payment_is_persisted_for_reconciliation(app, monkeypatch):
    from app import WebhookEvent, db
    from services.webhook_service import WebhookService

    with app.app_context():
        service = WebhookService()
        monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
        event = WebhookEvent(
            provider="mercadopago",
            event_key="subscription_authorized_payment:notification:notification-persist-qa",
            event_type="subscription_authorized_payment",
            status="failed",
            payload_json=json.dumps({
                "type": "subscription_authorized_payment",
                "id": "notification-persist-qa",
                "data": {"id": "authorized-persist-qa"},
                "_billing_reconciliation_attempts": 9,
                "_billing_reconciliation_last_error": "TimeoutError",
            }),
        )
        db.session.add(event)
        db.session.commit()

        saved = service.persist_failed_authorized_payment(
            db_session=db.session,
            headers={"X-Request-ID": "request-qa", "X-Signature": "ts=1,v1=fake"},
            payload={
                "type": "subscription_authorized_payment",
                "id": "notification-persist-qa",
                "data": {"id": "authorized-persist-qa"},
            },
        )
        assert saved is True
        event = WebhookEvent.query.filter_by(
            event_key="subscription_authorized_payment:notification:notification-persist-qa"
        ).first()
        assert event is not None
        assert event.status == "failed"
        preserved = json.loads(event.payload_json)
        assert preserved["_billing_reconciliation_attempts"] == 9
        assert preserved["_billing_reconciliation_last_error"] == "TimeoutError"

        monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: False)
        not_saved = service.persist_failed_authorized_payment(
            db_session=db.session,
            headers={"X-Request-ID": "request-qa", "X-Signature": "bad"},
            payload={
                "type": "subscription_authorized_payment",
                "id": "notification-invalid-qa",
                "data": {"id": "authorized-invalid-qa"},
            },
        )
        assert not_saved is False
        assert WebhookEvent.query.filter_by(
            event_key="subscription_authorized_payment:notification:notification-invalid-qa"
        ).first() is None
