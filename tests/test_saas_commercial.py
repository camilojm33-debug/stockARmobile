import pytest

from io import BytesIO

from openpyxl import Workbook

from services.saas_commercial_service import (
    build_campaign_recipients,
    lead_score,
    normalize_email,
    normalize_phone,
    parse_consent,
    parse_prospect_file,
)


def test_normalize_email_and_phone():
    assert normalize_email(" VENTAS@EMPRESA.COM ") == "ventas@empresa.com"
    assert normalize_email("not-an-email") is None
    assert normalize_phone("+54 9 11-1234-5678") == "5491112345678"


def test_parse_consent_values():
    assert parse_consent("sí") == "opted_in"
    assert parse_consent("no") == "opted_out"
    assert parse_consent("quizas") == "unknown"


def test_target_lead_score():
    row = {
        "industry": "Ferretería industrial",
        "email": "ventas@e.com",
        "phone": "5491112345678",
        "website": "https://tienda.e.com",
        "province": "Buenos Aires",
        "locality": "Morón",
        "instagram": "",
        "facebook": "",
    }
    assert lead_score(row) >= 80


def test_parse_csv():
    payload = """Comercio;Rubro;Localidad;Provincia;Email;WhatsApp;Web;Consentimiento Email
Ferretería Uno;Ferretería;Morón;Buenos Aires;ventas@uno.com;+54 9 11 5555-1111;https://uno.com;si
Fila incompleta;;;;;;;si
""".encode("utf-8")

    result = parse_prospect_file(payload, "prospectos.csv")
    assert result["format"] == "csv"
    assert len(result["rows"]) == 1
    assert result["rows"][0]["email_consent_status"] == "opted_in"
    assert result["invalid_count"] == 1


def test_parse_xlsx():
    wb = Workbook()
    ws = wb.active
    ws.append(["Comercio", "Rubro", "Email", "Localidad", "Provincia"])
    ws.append(["Pet Shop Demo", "Pet Shop", "hola@petshop.com", "CABA", "CABA"])
    buf = BytesIO()
    wb.save(buf)

    result = parse_prospect_file(buf.getvalue(), "prospectos.xlsx")
    assert result["format"] == "xlsx"
    assert result["rows"][0]["company_name"] == "Pet Shop Demo"


def test_parse_rejects_unsupported():
    try:
        parse_prospect_file(b"", "prospectos.pdf")
    except ValueError as exc:
        assert "CSV" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_campaign_recipient_selection_uses_email_suppression_policy(app):
    from app import SaaSCampaign, SaaSLead, SaaSLeadConsent, User, db

    with app.app_context():
        user = User(username="commercial-admin", email="commercial-admin@example.com", role="superadmin", active=True)
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        opted = SaaSLead(
            company_name="Optado",
            contact_name="Contacto",
            email="optado@example.com",
            email_status="valid",
            email_consent_status="opted_in",
            created_by_user_id=user.id,
        )
        unknown = SaaSLead(
            company_name="Desconocido",
            contact_name="Contacto",
            email="unknown@example.com",
            email_status="valid",
            email_consent_status="unknown",
            created_by_user_id=user.id,
        )
        opted_out = SaaSLead(
            company_name="Baja",
            contact_name="Contacto",
            email="opted-out@example.com",
            email_status="valid",
            email_consent_status="opted_out",
            created_by_user_id=user.id,
        )
        invalid_email = SaaSLead(
            company_name="Email inválido",
            contact_name="Contacto",
            email="not-an-email",
            email_status="unknown",
            email_consent_status="opted_in",
            created_by_user_id=user.id,
        )
        db.session.add_all([opted, unknown, opted_out, invalid_email])
        db.session.flush()
        db.session.add_all([
            SaaSLeadConsent(lead_id=opted.id, unsubscribe_token="token-opted"),
            SaaSLeadConsent(lead_id=unknown.id, unsubscribe_token="token-unknown"),
            SaaSLeadConsent(lead_id=opted_out.id, unsubscribe_token="token-opted-out"),
            SaaSLeadConsent(lead_id=invalid_email.id, unsubscribe_token="token-invalid-email"),
        ])

        campaign = SaaSCampaign(
            name="Prueba",
            subject="Hola",
            channel="email",
            body_html="<p>Hola {{contacto}}</p>",
            body_text="Hola {{contacto}}",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.commit()

        summary = build_campaign_recipients(db.session, campaign.id)
        assert summary["eligible"] == 2
        assert {recipient.lead_id for recipient in campaign.recipients} == {opted.id, unknown.id}


def test_campaign_both_prepares_independent_channel_recipients(app):
    from app import SaaSCampaign, SaaSCampaignRecipient, SaaSLead, SaaSLeadConsent, User, db

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = "1"
        user = User(
            username="multichannel-admin",
            email="multichannel-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        both = SaaSLead(
            company_name="Comercio Ambos",
            contact_name="Ana",
            email="ana@ambos.com",
            whatsapp="5491111111111",
            email_status="valid",
            phone_status="valid",
            email_consent_status="opted_in",
            whatsapp_consent_status="opted_in",
            do_not_contact=False,
            created_by_user_id=user.id,
        )
        email_only = SaaSLead(
            company_name="Solo Email",
            contact_name="Eva",
            email="eva@email.com",
            email_status="valid",
            email_consent_status="opted_in",
            whatsapp_consent_status="unknown",
            created_by_user_id=user.id,
        )
        whatsapp_only = SaaSLead(
            company_name="Solo WhatsApp",
            contact_name="Willy",
            whatsapp="5492222222222",
            email_consent_status="unknown",
            whatsapp_consent_status="opted_in",
            created_by_user_id=user.id,
        )
        db.session.add_all([both, email_only, whatsapp_only])
        db.session.flush()
        db.session.add_all([
            SaaSLeadConsent(lead_id=both.id, unsubscribe_token="mc-both"),
            SaaSLeadConsent(lead_id=email_only.id, unsubscribe_token="mc-email"),
            SaaSLeadConsent(lead_id=whatsapp_only.id, unsubscribe_token="mc-wa"),
        ])

        campaign = SaaSCampaign(
            name="Multicanal",
            subject="StockArMobile",
            channel="both",
            body_html="<p>Hola {{contacto}}</p>",
            body_text="Hola {{contacto}}",
            whatsapp_template_name="stockarmobile_prospecto_01",
            whatsapp_parameter_fields="contacto,empresa",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.commit()

        from services.saas_commercial_service import build_campaign_recipients
        summary = build_campaign_recipients(db.session, campaign.id)

        rows = SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).all()
        assert summary["eligible_email"] == 2
        assert summary["eligible_whatsapp"] == 2
        assert {(row.lead_id, row.channel) for row in rows} == {
            (both.id, "email"),
            (both.id, "whatsapp"),
            (email_only.id, "email"),
            (whatsapp_only.id, "whatsapp"),
        }


def test_campaign_dispatch_continues_enviando_batches(app, monkeypatch):
    from app import SaaSCampaign, SaaSLead, SaaSLeadConsent, User, db
    from services.saas_commercial_service import build_campaign_recipients, dispatch_due_campaigns

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
        user = User(
            username="batch-admin",
            email="batch-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        leads = []
        consents = []
        for i in range(55):
            lead = SaaSLead(
                company_name=f"Batch {i}",
                contact_name=f"Contacto {i}",
                email=f"batch-{i}@example.com",
                email_status="valid",
                email_consent_status="opted_in",
                created_by_user_id=user.id,
            )
            leads.append(lead)
            db.session.add(lead)
            db.session.flush()
            consents.append(SaaSLeadConsent(lead_id=lead.id, unsubscribe_token=f"batch-token-{i}"))
        db.session.add_all(consents)

        campaign = SaaSCampaign(
            name="Batch test",
            subject="Hola",
            channel="email",
            status="APROBADA",
            body_html="<p>Hola {{contacto}}</p>",
            body_text="Hola {{contacto}}",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.commit()

        build_campaign_recipients(db.session, campaign.id)
        monkeypatch.setattr(
            "services.saas_commercial_service._send_email",
            lambda recipient, campaign: (True, "sent"),
        )

        first = dispatch_due_campaigns(db.session, per_campaign=50)
        db.session.expire_all()
        second = dispatch_due_campaigns(db.session, per_campaign=50)

        refreshed = SaaSCampaign.query.get(campaign.id)
        assert first["sent"] == 50
        assert second["sent"] == 5
        assert refreshed.sent_count == 55
        assert refreshed.status == "ENVIADA"


def test_whatsapp_campaign_dispatch_uses_prepared_whatsapp_recipient(app, monkeypatch):
    from app import SaaSCampaign, SaaSCampaignRecipient, SaaSLead, SaaSLeadConsent, User, db
    from services.saas_commercial_service import build_campaign_recipients, dispatch_due_campaigns

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = "1"
        user = User(
            username="wa-campaign-admin",
            email="wa-campaign-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        lead = SaaSLead(
            company_name="WhatsApp Test",
            contact_name="Wanda",
            whatsapp="5493333333333",
            whatsapp_consent_status="opted_in",
            do_not_contact=False,
            created_by_user_id=user.id,
        )
        db.session.add(lead)
        db.session.flush()
        db.session.add(SaaSLeadConsent(lead_id=lead.id, unsubscribe_token="wa-campaign-token"))

        campaign = SaaSCampaign(
            name="WhatsApp test",
            subject="Hola",
            channel="whatsapp",
            status="APROBADA",
            body_html="<p>Solo se usa para detalle.</p>",
            body_text="Hola",
            whatsapp_template_name="stockarmobile_prospecto_01",
            whatsapp_parameter_fields="contacto,empresa",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.commit()

        prep = build_campaign_recipients(db.session, campaign.id)
        assert prep["added"] == 1, {"prep": prep, "lead_do_not_contact": lead.do_not_contact, "lead_consent": lead.whatsapp_consent_status, "campaign_status": campaign.status}
        calls = []

        def fake_whatsapp(recipient, current_campaign):
            calls.append((recipient.destination, current_campaign.whatsapp_template_name))
            return True, "sent", "wamid.test.001"

        monkeypatch.setattr("services.saas_commercial_service._send_whatsapp", fake_whatsapp)
        print("DEBUG_SAAS_CFG", app.config.get("SAAS_MARKETING_SEND_ENABLED"))
        print("DEBUG_SAAS_CAMPAIGN", campaign.id, campaign.status, campaign.scheduled_at)
        print("DEBUG_SAAS_RECIPIENTS", [(r.id, r.status, r.channel, r.destination) for r in SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).all()])
        result = dispatch_due_campaigns(db.session, per_campaign=50)
        recipient = SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).first()
        print("DEBUG_SAAS_RESULT", result)
        print("DEBUG_SAAS_RECIPIENT_AFTER", recipient.status if recipient else None, recipient.error_reason if recipient else None, recipient.provider_message_id if recipient else None)

        recipient = SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).first()
        assert result["sent"] == 1
        assert calls == [("5493333333333", "stockarmobile_prospecto_01")]
        assert recipient.status == "sent"
        assert recipient.provider_message_id == "wamid.test.001"
        assert recipient.provider_status == "accepted"


def test_email_tracking_html_contains_open_pixel_and_tracked_links(app):
    from app import SaaSCampaignRecipient, SaaSLead, User, db
    from services.saas_commercial_service import _render_tracking_html

    with app.app_context():
        user = User(
            username="tracking-admin",
            email="tracking-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()
        lead = SaaSLead(
            company_name="Tracking",
            contact_name="Tracy",
            email="tracking@example.com",
            created_by_user_id=user.id,
        )
        recipient = SaaSCampaignRecipient(
            campaign_id=999,
            lead_id=lead.id,
            channel="email",
            destination=lead.email,
            tracking_token="track-token-123",
        )
        # The helper only needs the recipient and app context; no database
        # relationship lookup is required for rendering.
        html = _render_tracking_html(
            '<p>Hola</p><a href="/auth/register?selected_plan=trial">Probar</a>',
            recipient,
        )
        assert "/superadmin/crm/email/open/track-token-123" in html
        assert "/superadmin/crm/email/click/track-token-123" in html
        assert "%2Fauth%2Fregister" in html


def test_delete_saas_lead_removes_crm_children_and_detaches_checkout(app):
    from app import (
        Plan, SaaSAlert, SaaSCampaign, SaaSCampaignEvent, SaaSCampaignRecipient, SaaSCommercialCheckout,
        SaaSLead, SaaSLeadConsent, SaaSTask, User, db
    )
    from services.saas_commercial_service import delete_saas_lead

    with app.app_context():
        user = User(
            username="delete-lead-admin",
            email="delete-lead-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)

        plan = Plan(
            code="delete-test-plan",
            name="Delete Test Plan",
            price=100,
            currency="ARS",
            duration_days=30,
            active=True,
        )
        db.session.add(plan)
        db.session.flush()

        lead = SaaSLead(
            company_name="Borrar CRM",
            contact_name="Contacto Borrar",
            email="borrar@example.com",
            whatsapp="549999001111",
            email_consent_status="opted_in",
            whatsapp_consent_status="opted_in",
            created_by_user_id=user.id,
        )
        db.session.add(lead)
        db.session.flush()

        consent = SaaSLeadConsent(
            lead_id=lead.id,
            unsubscribe_token="delete-lead-token",
            email_status="opted_in",
            whatsapp_status="opted_in",
        )
        task = SaaSTask(
            lead_id=lead.id,
            company_id=None,
            title="Seguimiento a borrar",
            status="pendiente",
            priority="media",
            created_by_user_id=user.id,
        )
        db.session.add_all([consent, task])
        db.session.flush()

        alert = SaaSAlert(
            lead_id=lead.id,
            task_id=task.id,
            title="Alerta a borrar",
            message="Alerta CRM",
            category="comercial",
            severity="media",
            status="abierta",
            created_by_user_id=user.id,
        )
        campaign = SaaSCampaign(
            name="Lead delete campaign",
            subject="Hola",
            channel="email",
            status="BORRADOR",
            body_html="<p>Hola</p>",
            created_by_user_id=user.id,
        )
        db.session.add_all([alert, campaign])
        db.session.flush()

        recipient = SaaSCampaignRecipient(
            campaign_id=campaign.id,
            lead_id=lead.id,
            channel="email",
            destination=lead.email,
            status="pending",
        )
        db.session.add(recipient)
        db.session.flush()
        campaign_event = SaaSCampaignEvent(
            campaign_id=campaign.id,
            recipient_id=recipient.id,
            event_type="opened",
            metadata_json='{"channel":"email"}',
        )
        checkout = SaaSCommercialCheckout(
            lead_id=lead.id,
            plan_id=plan.id,
            plan_code=plan.code,
            company_name=lead.company_name,
            payer_email=lead.email,
            phone=lead.whatsapp,
            external_reference="delete-test-checkout",
            status="pending",
        )
        db.session.add_all([campaign_event, checkout])
        db.session.commit()
        lead_pk = lead.id
        task_pk = task.id
        alert_pk = alert.id
        recipient_pk = recipient.id
        campaign_event_pk = campaign_event.id
        checkout_pk = checkout.id

        result = delete_saas_lead(db.session, lead_pk)
        db.session.commit()

        assert result["lead_id"] == lead_pk
        assert result["tasks_deleted"] == 1
        assert result["alerts_deleted"] == 1
        assert result["campaign_recipients_deleted"] == 1
        assert result["campaign_events_deleted"] == 1
        assert result["consents_deleted"] == 1
        assert result["checkouts_detached"] == 1

        assert db.session.get(SaaSLead, lead_pk) is None
        assert db.session.query(SaaSLeadConsent).filter_by(lead_id=lead_pk).count() == 0
        assert db.session.query(SaaSTask).filter_by(id=task_pk).count() == 0
        assert db.session.query(SaaSAlert).filter_by(id=alert_pk).count() == 0
        assert db.session.query(SaaSCampaignRecipient).filter_by(id=recipient_pk).count() == 0
        assert db.session.query(SaaSCampaignEvent).filter_by(id=campaign_event_pk).count() == 0

        detached = db.session.get(SaaSCommercialCheckout, checkout_pk)
        assert detached is not None
        assert detached.lead_id is None

        other_lead = SaaSLead(
            company_name="No borrar",
            contact_name="Otro",
            email="otro@example.com",
            created_by_user_id=user.id,
        )
        db.session.add(other_lead)
        db.session.commit()

        with pytest.raises(ValueError):
            delete_saas_lead(db.session, 999999)

        assert db.session.get(SaaSLead, other_lead.id) is not None


def test_campaign_recipient_claim_is_idempotent(app):
    from app import SaaSCampaign, SaaSCampaignRecipient, SaaSLead, User, db
    from services.saas_commercial_service import _claim_campaign_recipient

    with app.app_context():
        user = User(
            username="claim-admin",
            email="claim-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()
        lead = SaaSLead(
            company_name="Claim Test",
            contact_name="Claim",
            email="claim@example.com",
            created_by_user_id=user.id,
        )
        campaign = SaaSCampaign(
            name="Claim campaign",
            subject="Hola",
            channel="email",
            status="APROBADA",
            body_html="<p>Hola</p>",
            created_by_user_id=user.id,
        )
        db.session.add_all([lead, campaign])
        db.session.flush()
        recipient = SaaSCampaignRecipient(
            campaign_id=campaign.id,
            lead_id=lead.id,
            channel="email",
            destination=lead.email,
            status="pending",
        )
        db.session.add(recipient)
        db.session.commit()

        now = __import__("datetime").datetime.utcnow()
        assert _claim_campaign_recipient(db.session, recipient.id, now) is True
        db.session.commit()
        assert _claim_campaign_recipient(db.session, recipient.id, now) is False
        db.session.refresh(recipient)
        assert recipient.status == "sending"


def test_campaign_dispatch_marks_partial_when_some_recipients_fail(app, monkeypatch):
    from app import SaaSCampaign, SaaSCampaignRecipient, SaaSLead, SaaSLeadConsent, User, db
    from services.saas_commercial_service import build_campaign_recipients, dispatch_due_campaigns

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
        user = User(
            username="partial-admin",
            email="partial-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        leads = []
        for i in range(2):
            lead = SaaSLead(
                company_name=f"Partial {i}",
                contact_name=f"Contacto {i}",
                email=f"partial-{i}@example.com",
                email_status="valid",
                email_consent_status="opted_in",
                created_by_user_id=user.id,
            )
            db.session.add(lead)
            db.session.flush()
            db.session.add(SaaSLeadConsent(
                lead_id=lead.id,
                unsubscribe_token=f"partial-token-{i}",
                email_status="opted_in",
            ))
            leads.append(lead)

        campaign = SaaSCampaign(
            name="Partial campaign",
            subject="Hola",
            channel="email",
            status="APROBADA",
            body_html="<p>Hola {{contacto}}</p>",
            body_text="Hola {{contacto}}",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.commit()
        build_campaign_recipients(db.session, campaign.id)

        def fake_email(recipient, current_campaign):
            if recipient.destination.endswith("-1@example.com"):
                return False, "SMTP rechazó el mensaje"
            return True, "sent"

        monkeypatch.setattr("services.saas_commercial_service._send_email", fake_email)
        result = dispatch_due_campaigns(db.session, per_campaign=50)

        refreshed = SaaSCampaign.query.get(campaign.id)
        statuses = {
            row.status
            for row in SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).all()
        }
        assert result["sent"] == 1
        assert result["failed"] == 1
        assert statuses == {"sent", "failed"}
        assert refreshed.status == "ENVIADA_PARCIAL"


def test_campaign_dispatch_marks_failed_when_all_recipients_fail(app, monkeypatch):
    from app import SaaSCampaign, SaaSCampaignRecipient, SaaSLead, SaaSLeadConsent, User, db
    from services.saas_commercial_service import build_campaign_recipients, dispatch_due_campaigns

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
        user = User(
            username="failed-admin",
            email="failed-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()
        lead = SaaSLead(
            company_name="Failed campaign",
            contact_name="Contacto",
            email="failed@example.com",
            email_status="valid",
            email_consent_status="opted_in",
            created_by_user_id=user.id,
        )
        db.session.add(lead)
        db.session.flush()
        db.session.add(SaaSLeadConsent(
            lead_id=lead.id,
            unsubscribe_token="failed-token",
            email_status="opted_in",
        ))
        campaign = SaaSCampaign(
            name="Failed campaign",
            subject="Hola",
            channel="email",
            status="APROBADA",
            body_html="<p>Hola</p>",
            body_text="Hola",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.commit()
        build_campaign_recipients(db.session, campaign.id)
        monkeypatch.setattr(
            "services.saas_commercial_service._send_email",
            lambda recipient, current_campaign: (False, "SMTP caído"),
        )

        result = dispatch_due_campaigns(db.session, per_campaign=50)
        refreshed = SaaSCampaign.query.get(campaign.id)
        recipient = SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).first()

        assert result["failed"] == 1
        assert recipient.status == "failed"
        assert refreshed.status == "FALLIDA"
