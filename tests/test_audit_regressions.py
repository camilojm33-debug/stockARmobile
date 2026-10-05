from datetime import timedelta
from pathlib import Path

from services.ai_agent.campaign_service import CampaignService
from services.ai_agent.tenant_campaign_service import TenantCampaignRecipient, dispatch_due_campaigns, prepare_recipients
from stockarmobile.helpers.dates import utcnow_naive


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_sale_creation_ignores_client_supplied_status():
    source = (REPO_ROOT / "services" / "sales" / "sale_service.py").read_text(encoding="utf-8")
    assert "status=SALE_STATUS_CONFIRMED," in source
    assert 'status=data.get("status")' not in source


def test_quote_form_cannot_set_sensitive_status_directly():
    source = (REPO_ROOT / "quotes.py").read_text(encoding="utf-8")
    assert 'payload.get("status")' not in source.split("def _quote_from_form", 1)[1].split("def _quote_rows", 1)[0]
    assert 'if payload.get("submit_action") == "send" and current_status in {"BORRADOR", "PENDIENTE", "ENVIADO"}:' in source


def test_quote_conversion_requires_approval_and_full_stock():
    source = (REPO_ROOT / "quotes.py").read_text(encoding="utf-8")
    conversion = source.split("def convert_to_sale", 1)[1]
    assert 'if status != "APROBADO":' in conversion
    assert "stock_shortages = []" in conversion
    assert "requested_qty > max_stock" in conversion
    assert "final_qty = min(requested_qty, max_stock)" not in conversion


def test_duplicate_quote_gets_a_fresh_expiration():
    source = (REPO_ROOT / "quotes.py").read_text(encoding="utf-8")
    duplicate = source.split("def _duplicate_commercial_quote", 1)[1].split("def _quote_rows", 1)[0]
    assert "expires_at=(utcnow() + timedelta(days=5)).replace(" in duplicate


def test_marketing_consent_form_uses_the_same_channel_field():
    source = (REPO_ROOT / "templates" / "clientes" / "form.html").read_text(encoding="utf-8")
    email_block = source.split('name="email_marketing_consent"', 1)[1].split("</select>", 1)[0]
    whatsapp_block = source.split('name="whatsapp_marketing_consent"', 1)[1].split("</select>", 1)[0]
    assert "client.email_marketing_consent == 'opted_in'" in email_block
    assert "client.whatsapp_marketing_consent == 'opted_in'" in whatsapp_block
    assert "client.whatsapp_marketing_consent == 'opted_in'" not in email_block
    assert "client.email_marketing_consent == 'opted_in'" not in whatsapp_block


def test_web_ai_chat_enables_system_prompts_for_operational_agents():
    source = (REPO_ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert 'include_system_prompt=agent_key in {"vendedor", "asistente", "analista", "marketing"}' in source


def test_marketing_preference_restricts_campaign_tool_type():
    source = (REPO_ROOT / "services" / "ai_agent" / "orchestrator_v2.py").read_text(encoding="utf-8")
    assert 'if name == "preparar_campana":' in source
    assert '"promocion_producto": "promocion"' in source
    assert '"recuperacion_clientes_inactivos": "reactivacion"' in source
    assert '"productos_sin_ventas": "stock"' in source


def test_campaign_unsubscribe_get_is_side_effect_free_and_post_is_idempotent(app):
    from app import Client, Company, db

    with app.app_context():
        company = Company(name="Consent QA", active=True)
        db.session.add(company)
        db.session.flush()
        client = Client(
            name="Cliente Consentimiento",
            email="consent@example.com",
            company_id=company.id,
            email_marketing_consent="opted_in",
            whatsapp_marketing_consent="opted_in",
            marketing_unsubscribe_token="token-consent-qa",
            active=True,
        )
        db.session.add(client)
        db.session.commit()

    client_app = app.test_client()
    base = "/agentes-ia/campanas/unsubscribe/token-consent-qa"

    response = client_app.get(base)
    assert response.status_code == 200
    with app.app_context():
        client = Client.query.filter_by(marketing_unsubscribe_token="token-consent-qa").one()
        assert client.email_marketing_consent == "opted_in"
        assert client.whatsapp_marketing_consent == "opted_in"

    response = client_app.post(base)
    assert response.status_code == 200
    response = client_app.post(base)
    assert response.status_code == 200
    with app.app_context():
        client = Client.query.filter_by(marketing_unsubscribe_token="token-consent-qa").one()
        assert client.email_marketing_consent == "opted_out"
        assert client.whatsapp_marketing_consent == "opted_out"


def test_campaign_all_failures_are_not_marked_as_sent(app, monkeypatch):
    from app import Campaign, Client, Company, User, db

    with app.app_context():
        app.config["AI_MARKETING_SEND_ENABLED"] = True
        company = Company(name="Delivery QA", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="delivery-qa",
            email="delivery-qa@example.com",
            role="admin",
            active=True,
            company_id=company.id,
        )
        user.set_password("password123")
        client = Client(
            name="Cliente Campaña",
            email="delivery-qa-client@example.com",
            company_id=company.id,
            email_marketing_consent="opted_in",
            whatsapp_marketing_consent="unknown",
            active=True,
        )
        db.session.add_all([user, client])
        db.session.flush()

        campaign = CampaignService.create_draft(
            company_id=company.id,
            user_id=user.id,
            title="Campaña QA",
            objective="Prueba",
            campaign_type="general",
            content="Hola {{cliente}}",
            system_data={"channel": "email"},
            audience_segment="clientes activos",
            audience_count=1,
        )
        db.session.commit()
        CampaignService.transition(
            company_id=company.id,
            campaign_id=campaign.id,
            target_status="PENDIENTE_APROBACION",
            user_id=user.id,
        )
        CampaignService.transition(
            company_id=company.id,
            campaign_id=campaign.id,
            target_status="APROBADA",
            user_id=user.id,
        )
        prepare_recipients(campaign.id, company_id=company.id)
        campaign.status = "EN_PREPARACION"
        db.session.commit()

        monkeypatch.setattr(
            "services.ai_agent.tenant_campaign_service._send_email",
            lambda recipient, campaign: (False, "smtp-failure", ""),
        )

        result = dispatch_due_campaigns(db.session, company_id=company.id, per_campaign=50)
        db.session.expire_all()
        refreshed = db.session.get(Campaign, campaign.id)

        assert result["failed"] == 1
        assert refreshed.status == "FALLIDA"
        assert refreshed.sent_count == 0
        assert refreshed.failed_count == 1
        assert refreshed.skipped_count == 0
