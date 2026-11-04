import json

import pytest

from app import Branch, Company, Payment, db
from sales import (
    _pos_qr_branch_context_error,
    _pos_qr_draft_session_key,
    _pos_qr_snapshot,
)
from services.webhook_service import WebhookService


def test_pos_qr_draft_session_key_is_scoped_to_active_branch(monkeypatch):
    import sales

    monkeypatch.setattr(sales, "_cart_tenant_key", lambda: "company-7:user-11")
    monkeypatch.setattr(sales, "_active_cart_branch_scope", lambda: 3)
    first_branch_key = _pos_qr_draft_session_key()

    monkeypatch.setattr(sales, "_active_cart_branch_scope", lambda: 8)
    second_branch_key = _pos_qr_draft_session_key()

    assert first_branch_key == "pos_qr_draft_company-7:user-11_3"
    assert second_branch_key == "pos_qr_draft_company-7:user-11_8"
    assert first_branch_key != second_branch_key


def test_pos_qr_snapshot_keeps_origin_branch_and_cash_session():
    snapshot = _pos_qr_snapshot(
        [{
            "productId": 41,
            "name": "Producto QA",
            "price": 120,
            "quantity": 2,
            "discount": 0,
        }],
        {},
        total_amount=240,
        currency="ARS",
        branch_id=3,
        cash_session_id=99,
    )

    assert snapshot["branch_id"] == 3
    assert snapshot["cash_session_id"] == 99
    assert snapshot["items"][0]["productId"] == 41


def test_pos_qr_finalization_rejects_a_different_active_branch(app):
    with app.app_context():
        company = Company(name="Empresa QR multisucursal", active=True)
        db.session.add(company)
        db.session.flush()
        origin = Branch(
            company_id=company.id,
            name="Casa central",
            code="QR-CENTRAL",
            active=True,
            is_default=True,
        )
        other = Branch(
            company_id=company.id,
            name="Sucursal Norte",
            code="QR-NORTE",
            active=True,
            is_default=False,
        )
        db.session.add_all([origin, other])
        db.session.flush()

        error = _pos_qr_branch_context_error(
            {"branch_id": origin.id},
            company_id=company.id,
            active_branch_id=other.id,
            cash_session_branch_id=other.id,
        )

        assert error is not None
        assert "Casa central" in error
        db.session.rollback()


def test_legacy_pos_qr_without_branch_is_blocked_when_company_is_multibranch(app):
    with app.app_context():
        company = Company(name="Empresa QR legacy multisucursal", active=True)
        db.session.add(company)
        db.session.flush()
        db.session.add_all([
            Branch(
                company_id=company.id,
                name="Casa central",
                code="QR-LEGACY-CENTRAL",
                active=True,
                is_default=True,
            ),
            Branch(
                company_id=company.id,
                name="Sucursal Sur",
                code="QR-LEGACY-SUR",
                active=True,
                is_default=False,
            ),
        ])
        db.session.flush()

        error = _pos_qr_branch_context_error(
            {},
            company_id=company.id,
            active_branch_id=None,
            cash_session_branch_id=None,
        )

        assert error is not None
        assert "sucursal de origen" in error
        db.session.rollback()


def test_pos_qr_webhook_preserves_sale_snapshot_and_validates_branch():
    snapshot = {
        "items": [{"productId": 41, "quantity": 2, "price": 120}],
        "branch_id": 3,
        "cash_session_id": 99,
        "total_amount": 240,
    }
    payment = Payment(
        provider="mercadopago_pos",
        external_reference="flow:pos_sale|draft_payment_id:17|branch_id:3|cash_session_id:99",
        payload_json=json.dumps({"flow": "pos_sale", "snapshot": snapshot}),
    )
    payment_data = {
        "id": "mp-payment-123",
        "status": "approved",
        "date_last_updated": "2026-11-03T20:00:00Z",
        "metadata": {"branch_id": 3, "cash_session_id": 99},
    }

    merged = json.loads(
        WebhookService._merge_pos_sale_draft_payload(
            payment,
            payment_data,
            payment.external_reference,
        )
    )

    assert merged["id"] == "mp-payment-123"
    assert merged["status"] == "approved"
    assert merged["flow"] == "pos_sale"
    assert merged["snapshot"] == snapshot


def test_pos_qr_webhook_rejects_branch_mismatch_against_original_draft():
    snapshot = {
        "items": [{"productId": 41, "quantity": 2, "price": 120}],
        "branch_id": 3,
        "cash_session_id": 99,
    }
    payment = Payment(
        provider="mercadopago_pos",
        external_reference="flow:pos_sale|draft_payment_id:17|branch_id:3|cash_session_id:99",
        payload_json=json.dumps({"flow": "pos_sale", "snapshot": snapshot}),
    )
    payment_data = {
        "id": "mp-payment-123",
        "status": "approved",
        "metadata": {"branch_id": 8, "cash_session_id": 99},
    }

    with pytest.raises(RuntimeError, match="sucursales distintas|sucursal distinta"):
        WebhookService._merge_pos_sale_draft_payload(
            payment,
            payment_data,
            "flow:pos_sale|draft_payment_id:17|branch_id:8|cash_session_id:99",
        )
