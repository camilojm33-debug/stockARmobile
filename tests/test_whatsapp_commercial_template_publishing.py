from __future__ import annotations

import json

import pytest

import app as stock_app
from app import Company, User, db


@pytest.fixture
def template_publishing_app():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False
    stock_app.app.config["SERVER_NAME"] = "localhost"
    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()
        company = Company(
            name="StockArMobile Comercial",
            active=True,
            preferences_json=json.dumps({
                "internal_channel": "whatsapp_commercial",
                "ai_agent": {"whatsapp": {"enabled": True}},
            }),
        )
        actor = User(
            username="qa_template_admin",
            email="qa-template-admin@test.local",
            role="superadmin",
            active=True,
            password_hash="unused",
        )
        db.session.add_all([company, actor])
        db.session.commit()
        yield stock_app.app, company, actor
        db.session.remove()
        db.drop_all()


def _login(client, actor):
    with client.session_transaction() as browser_session:
        browser_session["_user_id"] = str(actor.id)


def test_superadmin_can_publish_master_catalog_to_the_dedicated_commercial_waba(
    template_publishing_app, monkeypatch
):
    app, company, actor = template_publishing_app
    from services.ai_agent import config_service, whatsapp_master_template_service

    connection = {
        "enabled": True,
        "phone_number_id": "commercial-phone-id",
        "business_account_id": "1234567890",
        "display_phone_number": "+54 9 11 5555-0101",
        "access_token": "must-never-be-returned",
    }
    calls = []

    monkeypatch.setattr(config_service, "get_whatsapp_connection", lambda current: connection)
    monkeypatch.setattr(
        whatsapp_master_template_service,
        "provision_master_templates",
        lambda current_connection: calls.append(current_connection) or [{
            "key": "marketing_general",
            "name": "campana_marketing_es_ar",
            "language": "es_AR",
            "status": "PENDING",
            "created": True,
            "message": "Plantilla creada y enviada a revisión de Meta.",
        }],
    )

    client = app.test_client()
    _login(client, actor)
    response = client.post("/superadmin/whatsapp-plantillas-maestras/publicar-comercial")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["results"][0]["status"] == "PENDING"
    assert payload["results"][0]["name"] == "campana_marketing_es_ar"
    assert "must-never-be-returned" not in response.get_data(as_text=True)
    assert calls == [connection]

    db.session.expire_all()
    refreshed_company = db.session.get(Company, company.id)
    assert "master_template_sync" in refreshed_company.preferences_json


def test_superadmin_master_template_page_shows_commercial_publish_control(template_publishing_app, monkeypatch):
    app, _company, actor = template_publishing_app
    from services.ai_agent import config_service

    monkeypatch.setattr(
        config_service,
        "get_whatsapp_connection",
        lambda current: {
            "enabled": True,
            "phone_number_id": "commercial-phone-id",
            "business_account_id": "1234567890",
            "display_phone_number": "+54 9 11 5555-0101",
            "access_token": "secret-token",
        },
    )
    client = app.test_client()
    _login(client, actor)

    response = client.get("/superadmin/whatsapp-plantillas-maestras")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Publicar catálogo maestro en Meta" in html
    assert "Publicar / sincronizar en Meta" in html
    assert "1234567890" in html
    assert "secret-token" not in html


def test_publication_is_blocked_when_commercial_channel_is_not_ready(template_publishing_app, monkeypatch):
    app, _company, actor = template_publishing_app
    from services.ai_agent import config_service, whatsapp_master_template_service

    monkeypatch.setattr(
        config_service,
        "get_whatsapp_connection",
        lambda current: {
            "enabled": False,
            "phone_number_id": "",
            "business_account_id": "",
            "access_token": "",
        },
    )
    meta_calls = []
    monkeypatch.setattr(
        whatsapp_master_template_service,
        "provision_master_templates",
        lambda connection: meta_calls.append(connection) or [],
    )

    client = app.test_client()
    _login(client, actor)
    response = client.post("/superadmin/whatsapp-plantillas-maestras/publicar-comercial")

    assert response.status_code == 409
    assert response.get_json()["success"] is False
    assert "habilitá primero" in response.get_json()["error"].lower()
    assert meta_calls == []
