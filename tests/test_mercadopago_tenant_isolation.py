import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("MP_OAUTH_ENCRYPTION_KEY", "test-oauth-encryption-key")

import pytest
from sqlite_test_db import clear_test_data

import app as stock_app
from app import Company, MercadoPagoConnection, Payment, db
from services.mercadopago_oauth_service import MercadoPagoOAuthService
from services.webhook_service import WebhookService


@pytest.fixture(autouse=True)
def clean_database():
    stock_app.app.config["TESTING"] = True
    with stock_app.app.app_context():
        db.session.rollback()
        db.session.remove()
        clear_test_data(db)
        yield
        db.session.rollback()
        db.session.remove()
        clear_test_data(db)
        db.session.remove()


@pytest.fixture
def company_pair():
    with stock_app.app.app_context():
        company_a = Company(name="Empresa A", active=True)
        company_b = Company(name="Empresa B", active=True)
        db.session.add_all([company_a, company_b])
        db.session.flush()
        yield company_a, company_b


def test_oauth_cannot_link_same_mp_seller_to_two_companies(company_pair):
    company_a, company_b = company_pair
    service = MercadoPagoOAuthService()

    service.save_connection(
        company_id=company_a.id,
        token_payload={"access_token": "token-a", "refresh_token": "refresh-a", "expires_in": 3600, "scope": "offline_access"},
        profile={"id": "mp-seller-1", "email": "a@mp.test"},
    )
    db.session.commit()

    with pytest.raises(RuntimeError, match="ya está vinculada a otra empresa"):
        service.save_connection(
            company_id=company_b.id,
            token_payload={"access_token": "token-b", "refresh_token": "refresh-b", "expires_in": 3600, "scope": "offline_access"},
            profile={"id": "mp-seller-1", "email": "b@mp.test"},
        )

    assert MercadoPagoConnection.query.filter_by(mp_user_id="mp-seller-1").count() == 1


def test_disconnect_releases_mp_seller_link(company_pair):
    company_a, company_b = company_pair
    service = MercadoPagoOAuthService()
    connection = service.save_connection(
        company_id=company_a.id,
        token_payload={"access_token": "token-a", "refresh_token": "refresh-a", "expires_in": 3600},
        profile={"id": "mp-seller-2"},
    )
    db.session.commit()

    service.disconnect(company_id=company_a.id)
    service.save_connection(
        company_id=company_b.id,
        token_payload={"access_token": "token-b", "refresh_token": "refresh-b", "expires_in": 3600},
        profile={"id": "mp-seller-2"},
    )

    assert connection.mp_user_id is None
    assert MercadoPagoConnection.query.filter_by(company_id=company_b.id).one().mp_user_id == "mp-seller-2"


def test_webhook_resolves_merchant_connection_from_seller_user_id(company_pair):
    company_a, _company_b = company_pair
    connection = MercadoPagoConnection(company_id=company_a.id, mp_user_id="mp-seller-3", status="connected")
    db.session.add(connection)
    db.session.commit()

    service = WebhookService()
    resolved = service._merchant_connection_for_payment_event(
        data_id="payment-not-yet-persisted",
        payload={"user_id":  "mp-seller-3"},
        Payment=Payment,
        MercadoPagoConnection=MercadoPagoConnection,
    )

    assert resolved is connection


def test_webhook_merchant_token_and_collector_are_tenant_scoped(monkeypatch, company_pair):
    company_a, company_b = company_pair
    connection_a = MercadoPagoConnection(company_id=company_a.id, mp_user_id="mp-a", status="connected")
    connection_b = MercadoPagoConnection(company_id=company_b.id, mp_user_id="mp-b", status="connected")
    db.session.add_all([connection_a, connection_b])
    db.session.commit()

    service = WebhookService()
    seen = {}

    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    def fake_ensure_access_token(self, *, company_id):
        seen["company_id"] = company_id
        return "token"

    monkeypatch.setattr(MercadoPagoOAuthService, "ensure_access_token", fake_ensure_access_token)
    monkeypatch.setattr(
        service.mp_service,
        "get_payment",
        lambda payment_id, *, access_token=None: seen.update(payment_id=payment_id, access_token=access_token) or {
            "id": payment_id,
            "status": "approved",
            "collector_id": "mp-a",
            "external_reference": f"flow:ai_order|company_id:{company_a.id}|quote_id:999",
            "metadata": {"flow": "ai_order", "company_id": company_a.id, "quote_id": 999},
        },
    )

    payload = {"id": "notification-a", "type": "payment", "user_id": "mp-a", "data": {"id": "payment-a"}}
    with pytest.raises(Exception):
        # The test intentionally stops after the tenant checks because quote 999 does not exist.
        service.process(db_session=db.session, headers={}, payload=payload)

    assert seen == {"company_id": company_a.id, "payment_id": "payment-a", "access_token": "token"}


def test_webhook_rejects_collector_from_another_company(monkeypatch, company_pair):
    company_a, company_b = company_pair
    connection_a = MercadoPagoConnection(company_id=company_a.id, mp_user_id="mp-a", status="connected")
    db.session.add(connection_a)
    db.session.commit()

    service = WebhookService()
    monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
    monkeypatch.setattr(MercadoPagoOAuthService, "ensure_access_token", lambda self, *, company_id: "token-a")
    monkeypatch.setattr(
        service.mp_service,
        "get_payment",
        lambda payment_id, *, access_token=None: {
            "id": payment_id,
            "status": "approved",
            "collector_id": "mp-b",
            "external_reference": f"flow:ai_order|company_id:{company_a.id}|quote_id:999",
            "metadata": {"flow": "ai_order", "company_id": company_a.id, "quote_id": 999},
        },
    )

    payload = {"id": "notification-b", "type": "payment", "user_id": "mp-a", "data": {"id": "payment-b"}}
    with pytest.raises(RuntimeError, match="collector del pago no coincide"):
        service.process(db_session=db.session, headers={}, payload=payload)
