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


def test_pos_qr_reservation_reduces_available_stock_and_blocks_other_operations(app):
    from app import Branch, Product, BranchStockReservation
    from stockarmobile.branch_inventory import (
        adjust_branch_stock,
        available_stock,
        reserve_branch_stock_for_payment,
        release_branch_stock_reservations,
        set_branch_stock,
    )

    with app.app_context():
        company = Company(name="POS QR reservations", active=True)
        db.session.add(company)
        db.session.flush()
        branch = Branch(company_id=company.id, name="Casa central", code="CENTRAL", active=True, is_default=True)
        product = Product(company_id=company.id, barcode="POS-RES-001", name="Producto reservado", stock=5, min_stock=0, active=True)
        db.session.add_all([branch, product])
        db.session.flush()
        set_branch_stock(product, branch.id, 5)
        payment = Payment(
            company_id=company.id,
            user_id=None,
            branch_id=branch.id,
            amount=300,
            currency="ARS",
            status="pending",
            provider="mercadopago_pos",
            reference="pos_draft:test",
        )
        db.session.add(payment)
        db.session.flush()

        reserve_branch_stock_for_payment(payment, [(product, 3)], branch.id)
        db.session.commit()
        assert BranchStockReservation.query.filter_by(payment_id=payment.id, status="reserved").count() == 1
        assert available_stock(product, branch.id) == 2

        try:
            adjust_branch_stock(product, branch.id, -3, movement_type="sale")
        except ValueError as exc:
            assert "reservado" in str(exc).lower()
            db.session.rollback()
        else:
            raise AssertionError("Una venta externa no debía consumir stock reservado.")

        db.session.refresh(payment)
        assert release_branch_stock_reservations(payment.id) == 1
        db.session.commit()
        assert available_stock(product, branch.id) == 5
        assert BranchStockReservation.query.filter_by(payment_id=payment.id, status="released").count() == 1


def test_expiring_pos_qr_releases_reserved_branch_stock(app):
    from app import Branch, Product, BranchStockReservation
    from stockarmobile.branch_inventory import available_stock, reserve_branch_stock_for_payment, set_branch_stock

    with app.app_context():
        now = datetime(2026, 9, 21, 21, 0, 0)
        company = Company(name="POS QR reservation expiry", active=True)
        db.session.add(company)
        db.session.flush()
        branch = Branch(company_id=company.id, name="Casa central", code="CENTRAL", active=True, is_default=True)
        product = Product(company_id=company.id, barcode="POS-RES-002", name="Producto reservado expiry", stock=4, min_stock=0, active=True)
        db.session.add_all([branch, product])
        db.session.flush()
        set_branch_stock(product, branch.id, 4)
        old = Payment(
            company_id=company.id, user_id=None, branch_id=branch.id,
            amount=100, currency="ARS", status="pending",
            provider="mercadopago_pos", reference="pos_draft:expired",
            created_at=now - timedelta(hours=80),
        )
        db.session.add(old)
        db.session.flush()
        reserve_branch_stock_for_payment(old, [(product, 2)], branch.id)
        db.session.commit()

        assert available_stock(product, branch.id) == 2
        assert expire_stale_pos_drafts(db.session, company_id=company.id, now=now) == 1
        db.session.commit()

        assert db.session.get(Payment, old.id).status == "expired"
        assert available_stock(product, branch.id) == 4
        assert BranchStockReservation.query.filter_by(payment_id=old.id, status="released").count() == 1


def test_pos_qr_webhook_payload_merge_preserves_snapshot_and_provider_timestamps():
    from services.webhook_service import _merge_pos_draft_payload
    import json

    snapshot = {"branch_id": 17, "items": [{"productId": 12, "quantity": 2}]}
    existing = json.dumps({"flow": "pos_sale", "snapshot": snapshot})
    updated = json.loads(_merge_pos_draft_payload(existing, {
        "id": "mp-123", "status": "approved",
        "date_last_updated": "2026-11-03T18:00:00.000-03:00",
    }))

    assert updated["snapshot"] == snapshot
    assert updated["flow"] == "pos_sale"
    assert updated["provider_response"]["status"] == "approved"
    assert updated["date_last_updated"].startswith("2026-11-03")
