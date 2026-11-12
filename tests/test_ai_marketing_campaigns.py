import pytest
from datetime import datetime, timezone

from services.ai_agent.campaign_service import CampaignService
from services.ai_agent.tenant_campaign_service import (
    TenantCampaignAttribution,
    TenantCampaignRecipient,
    campaign_metrics,
    dispatch_due_campaigns,
    prepare_recipients,
    refresh_attribution,
)


def _consent_evidence():
    return {
        "whatsapp_marketing_consent_recorded_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "whatsapp_marketing_consent_source": "test_fixture",
        "whatsapp_marketing_consent_evidence": "Explicit opt-in recorded for the test.",
    }


@pytest.fixture(autouse=True)
def mock_approved_campaign_template(monkeypatch):
    monkeypatch.setattr(
        "services.ai_agent.tenant_campaign_service._fetch_campaign_template",
        lambda connection: {
            "name": connection.get("template_name"),
            "language": connection.get("template_language") or "es_AR",
            "body_text": "Hola {{1}}, {{2}}. {{3}}",
            "status": "APPROVED",
        },
    )


def test_tenant_campaign_requires_whatsapp_consent_and_isolated(app, monkeypatch):
    from app import Client, Company, User, db

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

        opted = Client(name="Optado", email="optado@example.com", whatsapp="5491122334455", company_id=company_a.id, email_marketing_consent="unknown", whatsapp_marketing_consent="opted_in", active=True, **_consent_evidence())
        blocked = Client(name="Bloqueado", email="blocked@example.com", company_id=company_a.id, email_marketing_consent="opted_in", whatsapp_marketing_consent="unknown", active=True)
        other = Client(name="Otra empresa", email="other@example.com", whatsapp="5491133445566", company_id=company_b.id, email_marketing_consent="unknown", whatsapp_marketing_consent="opted_in", active=True, **_consent_evidence())
        db.session.add_all([opted, blocked, other])
        db.session.flush()

        monkeypatch.setattr(
            "services.ai_agent.tenant_campaign_service._tenant_campaign_whatsapp",
            lambda *_args, **_kwargs: (object(), {"enabled": True, "phone_number_id": "merchant-phone", "access_token": "merchant-token", "template_name": "approved_template"}),
        )
        campaign = CampaignService.create_draft(
            company_id=company_a.id, user_id=user_a.id, title="Recuperación", objective="Recuperar", campaign_type="general",
            content="Hola {{cliente}}", system_data={"channel": "email"}, audience_segment="clientes activos", audience_count=1,
        )
        assert campaign.channel == "whatsapp"
        assert '"channel": "whatsapp"' in campaign.system_data_json
        db.session.commit()
        CampaignService.transition(company_id=company_a.id, campaign_id=campaign.id, target_status="PENDIENTE_APROBACION", user_id=user_a.id)
        CampaignService.transition(company_id=company_a.id, campaign_id=campaign.id, target_status="APROBADA", user_id=user_a.id)
        first_preparation = prepare_recipients(campaign.id, company_id=company_a.id)
        second_preparation = prepare_recipients(campaign.id, company_id=company_a.id)

        rows = TenantCampaignRecipient.query.filter_by(campaign_id=campaign.id).all()
        assert first_preparation["added"] == 1
        assert second_preparation["added"] == 0
        assert {(row.client_id, row.channel) for row in rows} == {(opted.id, "whatsapp")}
        assert TenantCampaignRecipient.query.filter_by(company_id=company_b.id).count() == 0


def test_tenant_campaign_dispatch_is_idempotent_and_attributes_sale(app, monkeypatch):
    from app import Campaign, Client, Company, Sale, User, db

    with app.app_context():
        app.config["AI_MARKETING_SEND_ENABLED"] = True
        company = Company(name="Dispatch Co", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(username="dispatch-admin", email="dispatch@example.com", role="admin", active=True, company_id=company.id)
        user.set_password("password123")
        db.session.add(user)
        db.session.flush()
        client = Client(name="Cliente Venta", email="sale@example.com", whatsapp="5491122334455", company_id=company.id, email_marketing_consent="unknown", whatsapp_marketing_consent="opted_in", active=True, **_consent_evidence())
        db.session.add(client)
        db.session.flush()

        monkeypatch.setattr(
            "services.ai_agent.tenant_campaign_service._tenant_campaign_whatsapp",
            lambda *_args, **_kwargs: (object(), {"enabled": True, "phone_number_id": "merchant-phone", "access_token": "merchant-token", "template_name": "approved_template"}),
        )
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

        monkeypatch.setattr("services.ai_agent.tenant_campaign_service._send_whatsapp", lambda recipient, campaign: (True, "sent", "wamid-test"))
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


def test_queued_campaign_can_be_cancelled(app):
    from app import Company, User, db

    with app.app_context():
        company = Company(name="Cancel Queue Co", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="cancel-queue-admin",
            email="cancel-queue@example.com",
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
            title="Campaña cancelable",
            objective="Prueba",
            campaign_type="general",
            content="Hola",
            system_data={"channel": "email"},
            audience_segment="clientes activos",
            audience_count=0,
        )
        db.session.commit()

        for target in ("PENDIENTE_APROBACION", "APROBADA", "EN_PREPARACION", "CANCELADA"):
            CampaignService.transition(
                company_id=company.id,
                campaign_id=campaign.id,
                target_status=target,
                user_id=user.id,
            )

        assert campaign.status == "CANCELADA"


def test_campaign_detail_has_cancel_button_for_queued_state():
    from pathlib import Path

    template = Path("templates/ai_agents/campaign_detail.html").read_text(encoding="utf-8")
    assert 'name="status" value="CANCELADA"' in template
    assert "Cancelar campaña" in template


def test_inactive_audience_is_distinct_from_whatsapp_eligible_targets(app, monkeypatch):
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
        assert "email" not in detected["items"][0]

        monkeypatch.setattr(
            "services.ai_agent.tenant_campaign_service._tenant_campaign_whatsapp",
            lambda *_args, **_kwargs: (object(), {"enabled": True, "phone_number_id": "merchant-phone", "access_token": "merchant-token", "template_name": "approved_template"}),
        )

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

        with pytest.raises(ValueError, match="número oficial de Vendedor 24/7 de StockARmobile no está conectado"):
            prepare_recipients(campaign.id, company_id=company.id)




def test_dispatch_can_be_scoped_to_one_campaign(app, monkeypatch):
    from app import Campaign, Client, Company, User, db

    with app.app_context():
        app.config["AI_MARKETING_SEND_ENABLED"] = True
        company = Company(name="Auto Dispatch Co", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(username="auto-dispatch", email="auto-dispatch@example.com", role="admin", active=True, company_id=company.id)
        user.set_password("password123")
        client = Client(
            name="Cliente con permiso",
            email="consent@example.com",
            phone="5491122334455",
            whatsapp="5491122334455",
            company_id=company.id,
            email_marketing_consent="unknown",
            whatsapp_marketing_consent="opted_in",
            active=True,
            **_consent_evidence(),
        )
        db.session.add_all([user, client])
        db.session.flush()

        monkeypatch.setattr(
            "services.ai_agent.tenant_campaign_service._tenant_campaign_whatsapp",
            lambda *_args, **_kwargs: (object(), {"enabled": True, "phone_number_id": "merchant-phone", "access_token": "merchant-token", "template_name": "approved_template"}),
        )
        monkeypatch.setattr(
            "services.ai_agent.tenant_campaign_service._send_whatsapp",
            lambda recipient, campaign: (True, "sent", "wamid-auto-test"),
        )
        campaigns = []
        for title in ("Campaña que se envía", "Campaña que queda pendiente"):
            campaign = CampaignService.create_draft(
                company_id=company.id,
                user_id=user.id,
                title=title,
                objective="Prueba automática",
                campaign_type="general",
                content="Hola {{cliente}}",
                system_data={"channel": "email"},
                audience_segment="clientes activos",
                audience_count=1,
            )
            db.session.commit()
            CampaignService.transition(company_id=company.id, campaign_id=campaign.id, target_status="PENDIENTE_APROBACION", user_id=user.id)
            CampaignService.transition(company_id=company.id, campaign_id=campaign.id, target_status="APROBADA", user_id=user.id)
            prepare_recipients(campaign.id, company_id=company.id)
            CampaignService.transition(company_id=company.id, campaign_id=campaign.id, target_status="EN_PREPARACION", user_id=user.id)
            campaigns.append(campaign)

        db.session.commit()
        result = dispatch_due_campaigns(
            db.session,
            company_id=company.id,
            campaign_id=campaigns[0].id,
            limit=1,
            per_campaign=50,
        )

        assert result["sent"] == 1
        db.session.expire_all()
        assert db.session.get(Campaign, campaigns[0].id).status == "ENVIADA"
        assert db.session.get(Campaign, campaigns[1].id).status == "EN_PREPARACION"
        assert TenantCampaignRecipient.query.filter_by(campaign_id=campaigns[1].id, status="pending").count() == 1


def test_in_process_campaign_workers_start_only_in_gunicorn_worker():
    from pathlib import Path

    service = Path("services/ai_agent/tenant_campaign_service.py").read_text(encoding="utf-8")
    wsgi = Path("wsgi.py").read_text(encoding="utf-8")
    gunicorn = Path("gunicorn.conf.py").read_text(encoding="utf-8")

    assert "def start_campaign_dispatch_worker" in service
    assert "dispatch_due_campaigns(" in service
    assert "Campaign.status == \"EN_PREPARACION\"" in service
    assert "start_campaign_dispatch_worker(flask_app)" in gunicorn
    assert "start_crm_campaign_dispatch_worker(flask_app)" in gunicorn
    assert "start_campaign_dispatch_worker(app)" not in wsgi
    assert "start_crm_campaign_dispatch_worker(app)" not in wsgi



def test_whatsapp_campaign_does_not_use_general_phone_as_whatsapp(app, monkeypatch):
    from app import Campaign, Client, Company, User, db

    with app.app_context():
        company = Company(name="WhatsApp Argentina", active=True)
        sender = Company(
            name="StockARmobile Comercial",
            active=True,
            preferences_json='{"internal_channel":"whatsapp_commercial"}',
        )
        db.session.add_all([company, sender])
        db.session.flush()
        user = User(username="wa-argentina", email="wa-argentina@example.com", role="admin", active=True, company_id=company.id)
        user.set_password("password123")
        client = Client(
            name="Contacto local",
            phone="3624228396",
            company_id=company.id,
            active=True,
            whatsapp_marketing_consent="opted_in",
            **_consent_evidence(),
        )
        db.session.add_all([user, client])
        db.session.flush()

        campaign = CampaignService.create_draft(
            company_id=company.id,
            user_id=user.id,
            title="WhatsApp local",
            objective="Validar destino",
            campaign_type="general",
            content="Hola {{cliente}}",
            system_data={"channel": "whatsapp"},
            audience_segment="clientes activos",
            audience_count=1,
        )
        db.session.commit()
        CampaignService.transition(company_id=company.id, campaign_id=campaign.id, target_status="PENDIENTE_APROBACION", user_id=user.id)
        CampaignService.transition(company_id=company.id, campaign_id=campaign.id, target_status="APROBADA", user_id=user.id)
        monkeypatch.setattr(
            "services.saas_commercial_whatsapp.get_commercial_company",
            lambda: sender,
        )
        monkeypatch.setattr(
            "services.ai_agent.config_service.get_whatsapp_connection",
            lambda _company: {
                "enabled": True,
                "phone_number_id": "test-phone-id",
                "access_token": "test-token",
                "template_name": "approved_campaign",
            },
        )

        prepared = prepare_recipients(campaign.id, company_id=company.id)
        assert prepared["added"] == 0
        assert TenantCampaignRecipient.query.filter_by(campaign_id=campaign.id, channel="whatsapp").count() == 0



def test_campaign_dispatch_worker_does_not_start_during_tests(app):
    from services.ai_agent.tenant_campaign_service import start_campaign_dispatch_worker

    assert app.testing is True
    assert start_campaign_dispatch_worker(app) is False



def test_tenant_crm_whatsapp_campaigns_use_shared_stockarmobile_vendedor_number(app, monkeypatch):
    from app import Campaign, Client, Company, User, db
    from services.ai_agent.tenant_campaign_service import _send_whatsapp

    with app.app_context():
        tenant = Company(name="Comercio con CRM", active=True)
        sender = Company(
            name="StockArMobile Comercial",
            active=True,
            preferences_json='{"internal_channel":"whatsapp_commercial"}',
        )
        db.session.add_all([tenant, sender])
        db.session.flush()
        user = User(
            username="shared-sender-admin",
            email="shared-sender@example.com",
            role="admin",
            active=True,
            company_id=tenant.id,
        )
        user.set_password("password123")
        client = Client(
            name="Cliente autorizado",
            phone="3624228396",
            whatsapp="3624228396",
            company_id=tenant.id,
            active=True,
            whatsapp_marketing_consent="opted_in",
            **_consent_evidence(),
        )
        db.session.add_all([user, client])
        db.session.flush()

        campaign = CampaignService.create_draft(
            company_id=tenant.id,
            user_id=user.id,
            title="Campaña del comercio",
            objective="Informar una promoción",
            campaign_type="general",
            content="Hola {{cliente}}",
            system_data={
                "channel": "whatsapp",
                # A legacy tenant template must not override the shared WABA template.
                "whatsapp_template_name": "tenant_only_template",
            },
            audience_segment="clientes activos",
            audience_count=1,
        )
        db.session.commit()
        CampaignService.transition(
            company_id=tenant.id,
            campaign_id=campaign.id,
            target_status="PENDIENTE_APROBACION",
            user_id=user.id,
        )
        CampaignService.transition(
            company_id=tenant.id,
            campaign_id=campaign.id,
            target_status="APROBADA",
            user_id=user.id,
        )

        requested_company_ids = []

        def shared_connection(company):
            requested_company_ids.append(company.id)
            assert company.id == sender.id
            return {
                "enabled": True,
                "phone_number_id": "stockarmobile-vendedor-24-7",
                "access_token": "test-only-token",
                "display_phone_number": "+54 9 362 000 0000",
                "template_name": "stockarmobile_campaign_approved",
                "template_language": "es_AR",
            }

        monkeypatch.setattr(
            "services.saas_commercial_whatsapp.get_commercial_company",
            lambda: sender,
        )
        monkeypatch.setattr(
            "services.ai_agent.config_service.get_whatsapp_connection",
            shared_connection,
        )

        prepared = prepare_recipients(campaign.id, company_id=tenant.id)
        recipient = TenantCampaignRecipient.query.filter_by(
            campaign_id=campaign.id,
            channel="whatsapp",
        ).one()
        assert prepared["added"] == 1
        assert recipient.company_id == tenant.id
        assert recipient.destination == "5493624228396"
        assert requested_company_ids == [sender.id]

        sent = {}

        def fake_send_template(company, **kwargs):
            sent["company_id"] = company.id
            sent.update(kwargs)
            return {"messages": [{"id": "wamid.shared-sender-test"}]}

        monkeypatch.setattr(
            "services.ai_agent.whatsapp_service.WhatsAppService.send_template",
            fake_send_template,
        )
        ok, detail, provider_id = _send_whatsapp(recipient, campaign)

        assert ok is True
        assert detail == "sent"
        assert provider_id == "wamid.shared-sender-test"
        assert sent["company_id"] == sender.id
        assert sent["template_name"] == "stockarmobile_campaign_approved"
        assert sent["template_name"] != "tenant_only_template"
        assert requested_company_ids == [sender.id, sender.id]
        # Campaign and recipient data remain scoped to the originating tenant.
        assert Campaign.query.filter_by(id=campaign.id, company_id=tenant.id).count() == 1
        assert TenantCampaignRecipient.query.filter_by(company_id=tenant.id).count() == 1



def test_shared_campaign_reply_resolution_fails_closed_when_contact_has_multiple_tenants(app, monkeypatch):
    from datetime import datetime, timedelta
    from app import Campaign, Client, Company, User, db
    from services.ai_agent.tenant_campaign_service import resolve_shared_campaign_reply

    with app.app_context():
        platform = Company(
            name="StockARmobile Comercial",
            active=True,
            preferences_json='{"internal_channel":"whatsapp_commercial"}',
        )
        tenant_a = Company(name="Comercio A", active=True)
        tenant_b = Company(name="Comercio B", active=True)
        db.session.add_all([platform, tenant_a, tenant_b])
        db.session.flush()

        users = []
        clients = []
        campaigns = []
        for index, company in enumerate((tenant_a, tenant_b), start=1):
            user = User(
                username=f"shared-reply-{index}",
                email=f"shared-reply-{index}@example.com",
                role="admin",
                active=True,
                company_id=company.id,
            )
            user.set_password("password123")
            client = Client(
                name=f"Cliente {index}",
                phone="5493624000000",
                whatsapp="5493624000000",
                company_id=company.id,
                active=True,
                whatsapp_marketing_consent="opted_in",
                **_consent_evidence(),
            )
            db.session.add_all([user, client])
            db.session.flush()
            campaign = CampaignService.create_draft(
                company_id=company.id,
                user_id=user.id,
                title=f"Campaña {index}",
                objective="Avisar una novedad",
                campaign_type="general",
                content="Hola {{cliente}}",
                system_data={"channel": "whatsapp"},
                audience_segment="clientes activos",
                audience_count=1,
            )
            db.session.flush()
            users.append(user)
            clients.append(client)
            campaigns.append(campaign)

        first = TenantCampaignRecipient(
            campaign_id=campaigns[0].id,
            company_id=tenant_a.id,
            client_id=clients[0].id,
            channel="whatsapp",
            destination="5493624000000",
            unsubscribe_token="reply-test-a",
            status="sent",
            provider_message_id="wamid.tenant-a",
            sent_at=datetime.utcnow() - timedelta(minutes=5),
        )
        second = TenantCampaignRecipient(
            campaign_id=campaigns[1].id,
            company_id=tenant_b.id,
            client_id=clients[1].id,
            channel="whatsapp",
            destination="5493624000000",
            unsubscribe_token="reply-test-b",
            status="sent",
            provider_message_id="wamid.tenant-b",
            sent_at=datetime.utcnow() - timedelta(minutes=2),
        )
        db.session.add_all([first, second])
        db.session.commit()

        monkeypatch.setattr(
            "services.saas_commercial_whatsapp.get_commercial_company",
            lambda: platform,
        )
        monkeypatch.setattr(
            "services.ai_agent.config_service.get_whatsapp_connection",
            lambda company: {
                "enabled": True,
                "phone_number_id": "stockarmobile-shared-phone",
                "access_token": "test-only",
            } if company.id == platform.id else {},
        )

        # A quoted provider message ID uniquely identifies the originating tenant.
        assert resolve_shared_campaign_reply(
            phone_number_id="stockarmobile-shared-phone",
            sender="5493624000000",
            context_message_id="wamid.tenant-a",
        ).id == first.id

        # Without a quoted message, a customer reached by two tenants is ambiguous.
        assert resolve_shared_campaign_reply(
            phone_number_id="stockarmobile-shared-phone",
            sender="5493624000000",
        ) is None


def test_shared_campaign_reply_checks_tenant_ambiguity_beyond_first_hundred(app, monkeypatch):
    from datetime import datetime, timedelta
    from app import Client, Company, User, db
    from services.ai_agent.tenant_campaign_service import resolve_shared_campaign_reply

    with app.app_context():
        platform = Company(
            name="StockARmobile Comercial",
            active=True,
            preferences_json='{"internal_channel":"whatsapp_commercial"}',
        )
        tenant_a = Company(name="Large tenant", active=True)
        tenant_b = Company(name="Older tenant", active=True)
        db.session.add_all([platform, tenant_a, tenant_b])
        db.session.flush()
        campaigns = []
        for index, company in enumerate((tenant_a, tenant_b), start=1):
            user = User(
                username=f"large-reply-{index}",
                email=f"large-reply-{index}@example.com",
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
                title=f"Large reply campaign {index}",
                objective="Resolve safely",
                campaign_type="general",
                content="Hola",
                system_data={},
                audience_segment="clientes activos",
                audience_count=101,
            )
            campaigns.append(campaign)
        clients = [
            Client(
                name=f"Client {index}",
                whatsapp="5493624000000",
                company_id=tenant_a.id,
                active=True,
            )
            for index in range(101)
        ]
        clients.append(Client(
            name="Older duplicate",
            whatsapp="5493624000000",
            company_id=tenant_b.id,
            active=True,
        ))
        db.session.add_all(clients)
        db.session.flush()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        recipients = [
            TenantCampaignRecipient(
                campaign_id=campaigns[0].id,
                company_id=tenant_a.id,
                client_id=client.id,
                channel="whatsapp",
                destination="5493624000000",
                unsubscribe_token=f"large-reply-{index}",
                status="sent",
                provider_message_id=f"wamid.large-{index}",
                sent_at=now - timedelta(minutes=index + 1),
            )
            for index, client in enumerate(clients[:101])
        ]
        recipients.append(TenantCampaignRecipient(
            campaign_id=campaigns[1].id,
            company_id=tenant_b.id,
            client_id=clients[-1].id,
            channel="whatsapp",
            destination="5493624000000",
            unsubscribe_token="large-reply-older-tenant",
            status="sent",
            provider_message_id="wamid.large-older-tenant",
            sent_at=now - timedelta(minutes=102),
        ))
        db.session.add_all(recipients)
        db.session.commit()
        monkeypatch.setattr("services.saas_commercial_whatsapp.get_commercial_company", lambda: platform)
        monkeypatch.setattr(
            "services.ai_agent.config_service.get_whatsapp_connection",
            lambda company: {"phone_number_id": "shared-large", "enabled": True}
            if company.id == platform.id else {},
        )

        assert resolve_shared_campaign_reply(
            phone_number_id="shared-large",
            sender="5493624000000",
        ) is None


def test_whatsapp_consent_updates_are_audited_and_idempotent(app):
    from app import Client, Company, WhatsAppConsentEvent, db
    from services.whatsapp_consent_service import record_whatsapp_consent

    with app.app_context():
        company = Company(name="Consent audit", active=True)
        db.session.add(company)
        db.session.flush()
        client = Client(name="Opt-in contact", company_id=company.id, active=True)
        db.session.add(client)
        db.session.flush()
        record_whatsapp_consent(
            client,
            "opted_in",
            source="crm_form",
            evidence="Web form confirmation recorded 2026-11-10.",
            actor_user_id=None,
        )
        db.session.commit()
        record_whatsapp_consent(
            client,
            "opted_out",
            source="whatsapp_reply",
            evidence="STOP received; message ID wamid.audit-1.",
        )
        db.session.commit()
        record_whatsapp_consent(
            client,
            "opted_out",
            source="whatsapp_reply",
            evidence="STOP received; message ID wamid.audit-1.",
        )
        db.session.commit()

        events = WhatsAppConsentEvent.query.filter_by(client_id=client.id).order_by(
            WhatsAppConsentEvent.id.asc()
        ).all()
        assert [(event.previous_status, event.new_status) for event in events] == [
            ("unknown", "opted_in"),
            ("opted_in", "opted_out"),
        ]
        assert client.whatsapp_marketing_consent_recorded_at is not None
        assert client.whatsapp_marketing_consent == "opted_out"


def test_legacy_queued_tenant_email_recipient_is_skipped_never_sent(app, monkeypatch):
    from app import Campaign, Client, Company, User, db

    with app.app_context():
        app.config["AI_MARKETING_SEND_ENABLED"] = True
        company = Company(name="Legacy Email Block", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(username="legacy-email-admin", email="legacy-email@example.com", role="admin", active=True, company_id=company.id)
        user.set_password("password123")
        db.session.add(user)
        db.session.flush()
        client = Client(name="Legacy recipient", email="legacy@example.com", company_id=company.id, active=True)
        db.session.add(client)
        db.session.flush()
        campaign = Campaign(
            company_id=company.id,
            title="Legacy email campaign",
            objective="Old queued campaign",
            campaign_type="general",
            status="EN_PREPARACION",
            content="No debe salir por correo",
            system_data_json='{"channel":"email"}',
            audience_segment="clientes activos",
            audience_count=1,
            channel="email",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.flush()
        db.session.add(TenantCampaignRecipient(
            campaign_id=campaign.id,
            company_id=company.id,
            client_id=client.id,
            channel="email",
            destination="legacy@example.com",
            unsubscribe_token="legacy-token",
            status="pending",
        ))
        db.session.commit()

        monkeypatch.setattr(
            "services.ai_agent.tenant_campaign_service._send_whatsapp",
            lambda *_args: (_ for _ in ()).throw(AssertionError("No WhatsApp row was expected")),
        )
        result = dispatch_due_campaigns(db.session, company_id=company.id, per_campaign=50)
        db.session.refresh(campaign)
        row = TenantCampaignRecipient.query.filter_by(campaign_id=campaign.id).one()

        assert result["sent"] == 0
        assert result["skipped"] == 1
        assert row.status == "skipped"
        assert "únicamente por WhatsApp" in row.error_reason
        assert campaign.channel == "whatsapp"
        assert campaign.status == "SIN_ENVIO"


def test_preparation_revalidates_the_template_saved_in_the_draft(app, monkeypatch):
    import json
    from app import Client, Company, User, db

    with app.app_context():
        company = Company(name="Template continuity", active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="template-continuity-admin",
            email="template-continuity@example.com",
            role="admin",
            active=True,
            company_id=company.id,
        )
        user.set_password("password123")
        client = Client(
            name="Cliente autorizado",
            whatsapp="5491122334455",
            company_id=company.id,
            active=True,
            whatsapp_marketing_consent="opted_in",
            **_consent_evidence(),
        )
        db.session.add_all([user, client])
        db.session.flush()

        selected_template = {
            "name": "the_template_seen_in_the_draft",
            "language": "es_AR",
            "category": "MARKETING",
            "status": "APPROVED",
            "body_text": "Hola {{1}}: {{3}}",
            "campaign_compatible": True,
        }
        campaign = CampaignService.create_draft(
            company_id=company.id,
            user_id=user.id,
            title="Template continuity",
            objective="Retener clientes",
            campaign_type="general",
            content="Tenemos una novedad para vos.",
            system_data={"channel": "whatsapp", "whatsapp_template": selected_template},
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

        monkeypatch.setattr(
            "services.ai_agent.tenant_campaign_service._tenant_campaign_whatsapp",
            lambda *_args, **_kwargs: (
                object(),
                {
                    "enabled": True,
                    "phone_number_id": "platform-phone",
                    "access_token": "platform-token",
                    "template_name": "",
                    "template_language": "es_AR",
                },
            ),
        )
        calls = {}

        def revalidate(connection, *, selected_name=None, selected_language=None, allow_single_auto_select=True):
            calls["name"] = selected_name
            calls["language"] = selected_language
            calls["allow_single_auto_select"] = allow_single_auto_select
            return {
                **selected_template,
                "auto_selected": True,
            }

        monkeypatch.setattr(
            "services.ai_agent.tenant_campaign_service._fetch_campaign_template",
            revalidate,
        )

        prepared = prepare_recipients(campaign.id, company_id=company.id)
        db.session.refresh(campaign)
        saved = json.loads(campaign.system_data_json)["whatsapp_template"]

        assert prepared["added"] == 1
        assert calls == {
            "name": "the_template_seen_in_the_draft",
            "language": "es_AR",
            "allow_single_auto_select": False,
        }
        assert saved["name"] == "the_template_seen_in_the_draft"
        assert saved["auto_selected"] is True



def test_tenant_campaign_sender_uses_merchant_vendedor_connection_not_platform(app, monkeypatch):
    from app import Company, db
    from services.ai_agent.tenant_campaign_service import _tenant_campaign_whatsapp

    with app.app_context():
        platform = Company(
            name="StockARmobile Comercial",
            active=True,
            preferences_json='{"internal_channel":"whatsapp_commercial"}',
        )
        merchant = Company(name="Merchant-owned WhatsApp", active=True)
        db.session.add_all([platform, merchant])
        db.session.commit()

        monkeypatch.setattr(
            "services.saas_commercial_whatsapp.get_commercial_company",
            lambda: platform,
        )
        monkeypatch.setattr(
            "stockarmobile.branch_context.get_default_branch_id",
            lambda _company_id: None,
        )

        def connection_for(company, branch_id=None):
            assert branch_id is None
            return {
                "enabled": True,
                "phone_number_id": "merchant-phone-id" if company.id == merchant.id else "platform-phone-id",
                "business_account_id": "merchant-waba" if company.id == merchant.id else "platform-waba",
                "access_token": "merchant-token" if company.id == merchant.id else "platform-token",
                "template_name": "merchant_campaign",
                "template_language": "es_AR",
            }

        monkeypatch.setattr(
            "services.ai_agent.config_service.get_whatsapp_connection",
            connection_for,
        )

        sender_company, connection = _tenant_campaign_whatsapp(merchant.id)
        assert sender_company.id == merchant.id
        assert connection["phone_number_id"] == "merchant-phone-id"
        assert connection["access_token"] == "merchant-token"

        # The platform/commercial company is explicitly refused as a tenant sender.
        sender_company, connection = _tenant_campaign_whatsapp(platform.id)
        assert sender_company is None
        assert connection == {}


def test_tenant_campaign_reply_is_resolved_only_through_own_registered_phone(app, monkeypatch):
    from app import Client, Company, User, db
    from services.ai_agent.campaign_service import CampaignService
    from services.ai_agent.tenant_campaign_service import (
        TenantCampaignRecipient,
        resolve_tenant_campaign_reply,
    )

    with app.app_context():
        company_a = Company(name="Reply merchant A", active=True)
        company_b = Company(name="Reply merchant B", active=True)
        db.session.add_all([company_a, company_b])
        db.session.flush()
        user_a = User(username="reply-merchant-a", email="reply-a@example.com", role="admin", active=True, company_id=company_a.id)
        user_b = User(username="reply-merchant-b", email="reply-b@example.com", role="admin", active=True, company_id=company_b.id)
        user_a.set_password("password123")
        user_b.set_password("password123")
        client_a = Client(name="Cliente A", whatsapp="5493624000000", company_id=company_a.id, active=True)
        client_b = Client(name="Cliente B", whatsapp="5493624000000", company_id=company_b.id, active=True)
        db.session.add_all([user_a, user_b, client_a, client_b])
        db.session.flush()

        campaign_a = CampaignService.create_draft(
            company_id=company_a.id, user_id=user_a.id, title="A", objective="A",
            campaign_type="general", content="Hola", system_data={},
            audience_segment="clientes activos", audience_count=1,
        )
        campaign_b = CampaignService.create_draft(
            company_id=company_b.id, user_id=user_b.id, title="B", objective="B",
            campaign_type="general", content="Hola", system_data={},
            audience_segment="clientes activos", audience_count=1,
        )
        recipient_a = TenantCampaignRecipient(
            campaign_id=campaign_a.id, company_id=company_a.id, client_id=client_a.id,
            channel="whatsapp", destination="5493624000000", unsubscribe_token="reply-a",
            status="sent", provider_message_id="wamid.merchant-a",
        )
        recipient_b = TenantCampaignRecipient(
            campaign_id=campaign_b.id, company_id=company_b.id, client_id=client_b.id,
            channel="whatsapp", destination="5493624000000", unsubscribe_token="reply-b",
            status="sent", provider_message_id="wamid.merchant-b",
        )
        db.session.add_all([recipient_a, recipient_b])
        db.session.commit()

        monkeypatch.setattr(
            "services.ai_agent.config_service.company_branch_for_whatsapp_phone_id",
            lambda phone_id: (company_a, None) if phone_id == "phone-a" else (company_b, None),
        )
        assert resolve_tenant_campaign_reply(
            phone_number_id="phone-a",
            sender="5493624000000",
            context_message_id="wamid.merchant-a",
        ).id == recipient_a.id
        assert resolve_tenant_campaign_reply(
            phone_number_id="phone-a",
            sender="5493624000000",
            context_message_id="wamid.merchant-b",
        ) is None
        assert resolve_tenant_campaign_reply(
            phone_number_id="phone-b",
            sender="5493624000000",
            context_message_id="wamid.merchant-b",
        ).id == recipient_b.id


def test_tenant_campaign_send_uses_merchant_company_in_meta_request(app, monkeypatch):
    from app import Client, Company, User, db
    from services.ai_agent.campaign_service import CampaignService
    from services.ai_agent.tenant_campaign_service import TenantCampaignRecipient, _send_whatsapp

    with app.app_context():
        merchant = Company(name="Send from merchant", active=True)
        db.session.add(merchant)
        db.session.flush()
        user = User(username="merchant-sender", email="merchant-sender@example.com", role="admin", active=True, company_id=merchant.id)
        user.set_password("password123")
        client = Client(name="Cliente", whatsapp="5493624000000", company_id=merchant.id, active=True)
        db.session.add_all([user, client])
        db.session.flush()
        campaign = CampaignService.create_draft(
            company_id=merchant.id, user_id=user.id, title="Promo propia", objective="Venta",
            campaign_type="general", content="Oferta exclusiva", system_data={
                "channel": "whatsapp",
                "whatsapp_template": {
                    "name": "merchant_marketing",
                    "language": "es_AR",
                    "category": "MARKETING",
                    "status": "APPROVED",
                    "body_text": "Hola {{1}}: {{3}}",
                    "auto_selected": False,
                },
            },
            audience_segment="clientes activos", audience_count=1,
        )
        recipient = TenantCampaignRecipient(
            campaign_id=campaign.id, company_id=merchant.id, client_id=client.id,
            channel="whatsapp", destination="5493624000000", unsubscribe_token="send-merchant",
            status="pending",
        )
        db.session.add(recipient)
        db.session.commit()

        monkeypatch.setattr(
            "services.ai_agent.tenant_campaign_service._tenant_campaign_whatsapp",
            lambda company_id, branch_id=None: (
                merchant,
                {
                    "enabled": True,
                    "phone_number_id": "merchant-phone-id",
                    "business_account_id": "merchant-waba",
                    "access_token": "merchant-token",
                    "template_name": "merchant_marketing",
                    "template_language": "es_AR",
                },
            ),
        )
        sent = {}

        def fake_send_template(company, **kwargs):
            sent["company_id"] = company.id
            sent["kwargs"] = kwargs
            return {"messages": [{"id": "wamid.sent-from-merchant"}]}

        monkeypatch.setattr(
            "services.ai_agent.whatsapp_service.WhatsAppService.send_template",
            staticmethod(fake_send_template),
        )

        ok, detail, provider_id = _send_whatsapp(recipient, campaign)
        assert ok is True
        assert detail == "sent"
        assert provider_id == "wamid.sent-from-merchant"
        assert sent["company_id"] == merchant.id
        assert sent["kwargs"]["template_name"] == "merchant_marketing"
