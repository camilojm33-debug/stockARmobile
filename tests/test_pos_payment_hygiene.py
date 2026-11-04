import json
from types import SimpleNamespace

from datetime import datetime, timedelta

from app import Company, Payment, PaymentHistory, db
from services.payment_flow import expire_stale_pos_drafts, is_stale_pos_draft


def test_stale_pos_draft_is_detected():
    now = datetime(2026, 9, 21, 21, 0, 0)
    recent = Payment(status="pending", provider="mercadopago_pos", created_at=now - timedelta(hours=24))
    old = Payment(status="pending", provider="mercadopago_pos", created_at=now - timedelta(hours=73))
    with_payment_id = Payment(
        status="pending",
        provider="mercadopago_pos",
        payment_id="mp-123",
        created_at=now - timedelta(hours=200),
    )

    assert is_stale_pos_draft(recent, now=now) is False
    assert is_stale_pos_draft(old, now=now) is True
    assert is_stale_pos_draft(with_payment_id, now=now) is False


def test_expire_stale_pos_drafts_updates_only_abandoned_local_pos_drafts(app):
    with app.app_context():
        now = datetime(2026, 9, 21, 21, 0, 0)
        company = Company(name="POS Hygiene QA", active=True)
        db.session.add(company)
        db.session.flush()

        old = Payment(
            company_id=company.id,
            amount=100,
            currency="ARS",
            status="pending",
            provider="mercadopago_pos",
            reference="pos_draft:old",
            created_at=now - timedelta(hours=80),
        )
        recent = Payment(
            company_id=company.id,
            amount=200,
            currency="ARS",
            status="pending",
            provider="mercadopago_pos",
            reference="pos_draft:recent",
            created_at=now - timedelta(hours=24),
        )
        paid_flow = Payment(
            company_id=company.id,
            amount=300,
            currency="ARS",
            status="pending",
            provider="mercadopago_pos",
            payment_id="mp-456",
            reference="pos_draft:paid",
            created_at=now - timedelta(hours=80),
        )
        db.session.add_all([old, recent, paid_flow])
        db.session.commit()

        count = expire_stale_pos_drafts(db.session, company_id=company.id, now=now)
        db.session.commit()

        assert count == 1
        assert db.session.get(Payment, old.id).status == "expired"
        assert db.session.get(Payment, recent.id).status == "pending"
        assert db.session.get(Payment, paid_flow.id).status == "pending"

        history = PaymentHistory.query.filter_by(payment_id=old.id, event="expired_stale_pos_draft").first()
        assert history is not None
        assert history.status == "expired"


def test_pos_provider_payload_merge_preserves_trusted_sale_snapshot():
    from services.payment_flow import merge_pos_payment_provider_payload
    from services.webhook_service import WebhookService

    snapshot = {
        "branch_id": 17,
        "cash_session_id": 91,
        "items": [{"productId": 8, "quantity": 2}],
        "stock_reservation_released": False,
    }
    payment = SimpleNamespace(
        provider="mercadopago_pos",
        payload_json=json.dumps({"flow": "pos_sale", "snapshot": snapshot}),
    )

    merge_pos_payment_provider_payload(
        payment,
        {
            "id": 123456,
            "status": "approved",
            "date_last_updated": "2026-11-03T20:30:00.000-03:00",
        },
    )
    merged = json.loads(payment.payload_json)
    assert merged["flow"] == "pos_sale"
    assert merged["snapshot"] == snapshot
    assert merged["mercadopago"]["status"] == "approved"
    assert WebhookService._event_updated_at(merged) is not None


def test_pos_payment_rejects_expired_or_mismatched_reservations(app):
    from services.payment_flow import pos_payment_has_live_stock_reservation

    with app.app_context():
        payload = {
            "flow": "pos_sale",
            "snapshot": {
                "branch_id": 17,
                "cash_session_id": 91,
                "items": [{"productId": 8, "quantity": 2}],
                "stock_reserved": True,
                "stock_reservation_released": False,
            },
        }
        payment = SimpleNamespace(
            provider="mercadopago_pos",
            branch_id=17,
            payload_json=json.dumps(payload),
        )
        # A released reservation must not be treated as inventory still held
        # for the payment; the fully reserved positive path is covered by the
        # branch-aware webhook integration test in test_multibranch.py.
        payload["snapshot"]["stock_reservation_released"] = True
        payment.payload_json = json.dumps(payload)
        assert pos_payment_has_live_stock_reservation(payment) is False
    
        payload["snapshot"]["stock_reservation_released"] = False
        payload["snapshot"]["branch_id"] = 18
        payment.payload_json = json.dumps(payload)
        assert pos_payment_has_live_stock_reservation(payment) is False
