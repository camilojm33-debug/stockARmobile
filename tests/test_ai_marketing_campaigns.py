import pytest

from services.ai_agent.campaign_service import CampaignService
from services.ai_agent.tenant_campaign_service import (
    TenantCampaignAttribution,
    TenantCampaignRecipient,
    campaign_metrics,
    dispatch_due_campaigns,
    prepare_recipients,
)


def test_tenant_campaign_requires_channel_consent_and_isolated(app):
    from app import Campaign, Client, Company, User, db

    with app.app_context():
        company_a = Company(name="Marketing A", active=True)
        company_b = Company(name="Marketing B", active=True)
        db.session.add_all([company_a, company_b])
        db.session.flush()

        user_a = User(username="marketing-a", email="marketing-a@example.com", role="admin", active=True, company_id=company_a.id)
        user_b = User(username="marketing-b", email="marketing-b@example.com", role="admin", active=True, company_id=company_b.id)
        user_a.set_password("password123")
        user_b.set_password("password123")
        db.session.add_all([user_a, user_b])
        db.session.flush()

        opted = Client(name="Optado", email="optado@example.com", company_id=company_a.id, email_marketing_consent="opted_in", whatsapp_marketing_consent="unknown", active=True)
        blocked = Client(name="Bloqueado", email="blocked@example.com", company_id=company_a.id, email_marketing_consent="unknown", whatsapp_marketing_consent="unknown", active=True)
        other = Client(name="Otra empresa", email="other@example.com", company_id=company_b.id, email_marketing_consent="opted_in", whatsapp_marketing_consent="unknown", active=True)
        db.session.add_all([opted, blocked, other])
        db.session.flush()

        campaign = CampaignService.create_draft(
            company_id=company_a.id, user_id=user_a.id, title="Recuperación", objective="Recuperar", campaign_type="general",
            content="Hola {{cliente}}", system_data={"channel": "email"}, audience_segment="clientes activos", audience_count=1,
        )
        db.session.commit()
        CampaignService.transition(company_id=company_a.id, campaign_id=campaign.id, target_status="PENDIENTE_APROBACION", user_id=user_a.id)
        CampaignService.transition(company_id=company_a.id, campaign_id=campaign.id, target_status="APROBADA", user_id=user_a.id)
        prepare_recipients(campaign.id, company_id=company_a.id)

        rows = TenantCampaignRecipient.query.filter_by(campaign_id=campaign.id).all()
        assert {(row.client_id, row.channel) for row in rows} == {(opted.id, "email")}
        assert TenantCampaignRecipient.query.filter_by(company_id=company_b.id).count() == 0


def test_tenant_campaign_dispatch_is_idempotent_and_attributes_sale(app, monkeypatch):
    from app import Campaign, Client, Company, Sale, User, db

    with app.app_context():
        company = Company(name="Dispatch Co", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(username="dispatch-admin", email="dispatch@example.com", role="admin", active=True, company_id=company.id)
        user.set_password("password123")
        db.session.add(user)
        db.session.flush()
        client = Client(name="Cliente Venta", email="sale@example.com", company_id=company.id, email_marketing_consent="opted_in", whatsapp_marketing_consent="unknown", active=True)
        db.session.add(client)
        db.session.flush()

        campaign = CampaignService.create_draft(
            company_id=company.id, user_id=user.id, title="Promo", objective="Venta", campaign_type="general",
            content="Hola {{cliente}}", system_data={"channel": "email"}, audience_segment="clientes activos", audience_count=1,
        )
        db.session.commit()
        CampaignService.transition(company_id=company.id, campaign_id=campaign.id, target_status="PENDIENTE_APROBACION", user_id=user.id)
        CampaignService.transition(company_id=company.id, campaign_id=campaign.id, target_status="APROBADA", user_id=user.id)
        prepare_recipients(campaign.id, company_id=company.id)
        campaign.status = "EN_PREPARACION"
        db.session.commit()

        monkeypatch.setattr("services.ai_agent.tenant_campaign_service._send_email", lambda recipient, campaign: (True, "sent", "smtp-test"))
        result = dispatch_due_campaigns(db.session, company_id=company.id, per_campaign=50)
        assert result["sent"] == 1
        db.session.expire_all()
        refreshed = db.session.get(Campaign, campaign.id)
        assert refreshed.status == "ENVIADA"

        sale = Sale(company_id=company.id, client_id=client.id, total_amount=1500, status="confirmada")
        db.session.add(sale)
        db.session.commit()

        refreshed.status = "EN_PREPARACION"
        refreshed.scheduled_at = None
        db.session.commit()
        # No pending recipient remains, so dispatch does not resend it; attribution is
        # refreshed by a second campaign cycle without duplicating the recipient.
        dispatch_due_campaigns(db.session, company_id=company.id, per_campaign=50)
        assert TenantCampaignAttribution.query.filter_by(campaign_id=campaign.id, sale_id=sale.id).count() == 1

        # Attribution is intentionally anchored to sent_at; create a new campaign
        # cycle and assert the metric remains stable rather than duplicating rows.
        metrics = campaign_metrics(company.id, campaign.id)
        assert metrics["sent"] == 1
        assert metrics["failed"] == 0


def test_campaign_metrics_are_tenant_scoped(app):
    from app import Campaign, Company, User, db

    with app.app_context():
        company_a = Company(name="Metrics A", active=True)
        company_b = Company(name="Metrics B", active=True)
        db.session.add_all([company_a, company_b])
        db.session.flush()
        user = User(username="metrics-admin", email="metrics@example.com", role="admin", active=True, company_id=company_a.id)
        user.set_password("password123")
        db.session.add(user)
        db.session.flush()
        campaign = CampaignService.create_draft(
            company_id=company_a.id, user_id=user.id, title="M", objective="M", campaign_type="general",
            content="M", system_data={"channel": "email"}, audience_segment="clientes activos", audience_count=0,
        )
        db.session.commit()
        assert campaign_metrics(company_a.id, campaign.id)["total"] == 0
        with pytest.raises(ValueError):
            campaign_metrics(company_b.id, campaign.id)
