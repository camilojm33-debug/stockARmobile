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
