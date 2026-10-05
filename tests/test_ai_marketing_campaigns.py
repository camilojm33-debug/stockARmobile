import pytest

from services.ai_agent.campaign_service import CampaignService
from services.ai_agent.tenant_campaign_service import (
    TenantCampaignAttribution,
    TenantCampaignRecipient,
    campaign_metrics,
    dispatch_due_campaigns,
    prepare_recipients,
    refresh_attribution,
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

        # Attribution is refreshed independently of the campaign queue, so a sale
        # occurring after the campaign is ENVIADA is still attributed within the window.
        refresh_attribution(company_id=company.id)
        refresh_attribution(company_id=company.id)
        assert TenantCampaignAttribution.query.filter_by(campaign_id=campaign.id, sale_id=sale.id).count() == 1

        # Repeating attribution is idempotent.
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


def test_inactive_audience_is_distinct_from_channel_eligible_targets(app, monkeypatch):
    from datetime import datetime, timedelta
    from app import Client, Company, Sale, User, db
    from services.ai_agent.tools.analyst_marketing import ClientesInactivosTool

    with app.app_context():
        company = Company(name="Audience Co", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="audience-admin",
            email="audience@example.com",
            role="admin",
            active=True,
            company_id=company.id,
        )
        user.set_password("password123")
        client = Client(
            name="Roberto Mora",
            email="roberto@example.com",
            company_id=company.id,
            active=True,
            email_marketing_consent="unknown",
            whatsapp_marketing_consent="unknown",
        )
        db.session.add_all([user, client])
        db.session.flush()
        db.session.add(
            Sale(
                company_id=company.id,
                client_id=client.id,
                total_amount=10499,
                status="confirmada",
                date=datetime.utcnow() - timedelta(days=66),
            )
        )
        db.session.commit()

        detected = ClientesInactivosTool(company_id=company.id).execute(days=60, limit=20)
        assert detected["count"] == 1
        assert detected["items"][0]["name"] == "Roberto Mora"

        campaign = CampaignService.create_draft(
            company_id=company.id,
            user_id=user.id,
            title="Recuperación",
            objective="Recuperar",
            campaign_type="recuperacion_clientes_inactivos",
            content="Hola {{cliente}}",
            system_data={"channel": "email", "days": 60, "audience_count": 1},
            audience_segment="clientes inactivos",
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

        db.session.refresh(campaign)
        assert campaign.audience_count == 1
        assert campaign.target_count == 0


def test_whatsapp_campaign_requires_connection_before_queue(app):
    from app import Company, User, db

    with app.app_context():
        company = Company(name="WhatsApp Co", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="wa-admin",
            email="wa@example.com",
            role="admin",
            active=True,
            company_id=company.id,
        )
        user.set_password("password123")
        db.session.add(user)
        db.session.flush()

        campaign = CampaignService.create_draft(
            company_id=company.id,
            user_id=user.id,
            title="WhatsApp",
            objective="Recuperar",
            campaign_type="recuperacion_clientes_inactivos",
            content="Hola",
            system_data={"channel": "whatsapp", "days": 60},
            audience_segment="clientes inactivos",
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

        with pytest.raises(ValueError, match="WhatsApp no está conectado"):
            prepare_recipients(campaign.id, company_id=company.id)


def test_queued_campaign_can_be_cancelled_before_delivery(app):
    from app import Company, User, db

    with app.app_context():
        company = Company(name="Cancel Co", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="cancel-admin",
            email="cancel@example.com",
            role="admin",
            active=True,
            company_id=company.id,
        )
        user.set_password("password123")
        db.session.add(user)
        db.session.flush()

        campaign = CampaignService.create_draft(
            company_id=company.id,
            user_id=user.id,
            title="Cancelar",
            objective="Prueba",
            campaign_type="general",
            content="Hola",
            system_data={"channel": "email"},
            audience_segment="clientes activos",
            audience_count=0,
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
        CampaignService.transition(
            company_id=company.id,
            campaign_id=campaign.id,
            target_status="EN_PREPARACION",
            user_id=user.id,
        )
        CampaignService.transition(
            company_id=company.id,
            campaign_id=campaign.id,
            target_status="CANCELADA",
            user_id=user.id,
        )
        assert campaign.status == "CANCELADA"
