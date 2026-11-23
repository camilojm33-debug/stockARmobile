import pytest

from io import BytesIO

from openpyxl import Workbook

from services.saas_campaign_preflight import _smtp_probe
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
    assert result["rows"][0]["_source_row_number"] == 2
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
        assert summary["eligible"] == 1
        assert {recipient.lead_id for recipient in campaign.recipients} == {opted.id}


def test_campaign_both_prepares_independent_channel_recipients(app):
    from app import SaaSCampaign, SaaSCampaignRecipient, SaaSLead, SaaSLeadConsent, User, db

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
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
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
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

        summary = build_campaign_recipients(db.session, campaign.id)
        assert summary["eligible_whatsapp"] == 1
        assert campaign.target_count == 1
        calls = []

        def fake_whatsapp(recipient, current_campaign):
            calls.append((recipient.destination, current_campaign.whatsapp_template_name))
            return True, "sent", "wamid.test.001"

        monkeypatch.setattr("services.saas_commercial_service._send_whatsapp", fake_whatsapp)
        monkeypatch.setattr(
            "services.saas_commercial_whatsapp.validate_commercial_whatsapp_template",
            lambda *args, **kwargs: {"ok": True, "status": "APPROVED", "category": "MARKETING", "variable_count": 2, "detail": "approved"},
        )
        recipient_before = SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).first()
        assert recipient_before is not None
        assert recipient_before.status == "pending"
        assert campaign.status == "APROBADA"

        campaign.whatsapp_parameter_fields = "contacto"
        db.session.commit()
        blocked = dispatch_due_campaigns(db.session, per_campaign=50)
        recipient = SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).first()
        assert blocked["blocked"] == 1
        assert blocked["sent"] == 0
        assert calls == []
        assert recipient.status == "pending"
        assert campaign.status == "APROBADA"

        campaign.whatsapp_parameter_fields = "contacto,empresa"
        db.session.commit()
        result = dispatch_due_campaigns(db.session, per_campaign=50)

        recipient = SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).first()
        assert result["sent"] == 1, f"dispatch={result}; error={recipient.error_reason if recipient else None}"
        assert calls == [("5493333333333", "stockarmobile_prospecto_01")]
        assert recipient.status == "sent"
        assert recipient.provider_message_id == "wamid.test.001"
        assert recipient.provider_status == "accepted"


def test_whatsapp_campaign_requires_explicit_whatsapp_number(app):
    from app import SaaSCampaign, SaaSCampaignRecipient, SaaSLead, User, db
    from services.saas_commercial_service import build_campaign_recipients

    with app.app_context():
        user = User(
            username="wa-fallback-admin",
            email="wa-fallback-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        phone_only = SaaSLead(
            company_name="Teléfono con permiso",
            contact_name="Contacto Teléfono",
            phone="+54 9 11-1234-5678",
            whatsapp=None,
            whatsapp_consent_status="opted_in",
            created_by_user_id=user.id,
        )
        explicit = SaaSLead(
            company_name="WhatsApp explícito",
            contact_name="Contacto WhatsApp",
            phone="5491111111111",
            whatsapp="+54 9 11-2222-3333",
            whatsapp_consent_status="opted_in",
            created_by_user_id=user.id,
        )
        local_mobile = SaaSLead(
            company_name="WhatsApp con número local",
            contact_name="Contacto Local",
            whatsapp="3624228396",
            whatsapp_consent_status="opted_in",
            created_by_user_id=user.id,
        )
        no_permission = SaaSLead(
            company_name="Sin permiso",
            contact_name="Contacto Sin Permiso",
            phone="5491111111112",
            whatsapp_consent_status="unknown",
            created_by_user_id=user.id,
        )
        invalid_number = SaaSLead(
            company_name="Número corto",
            contact_name="Contacto Número Corto",
            phone="1234",
            whatsapp_consent_status="opted_in",
            created_by_user_id=user.id,
        )
        db.session.add_all([phone_only, explicit, local_mobile, no_permission, invalid_number])
        db.session.flush()

        campaign = SaaSCampaign(
            name="Campaña WhatsApp explícito",
            subject="Hola",
            channel="whatsapp",
            body_html="<p>Hola</p>",
            body_text="Hola",
            whatsapp_template_name="stockarmobile_prospecto_01",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.commit()

        summary = build_campaign_recipients(db.session, campaign.id)
        rows = SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).all()
        by_lead = {row.lead_id: row.destination for row in rows}

        assert summary["eligible_whatsapp"] == 2
        assert by_lead == {
            explicit.id: "5491122223333",
            local_mobile.id: "5493624228396",
        }
        assert phone_only.id not in by_lead
        assert no_permission.id not in by_lead
        assert invalid_number.id not in by_lead


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
        from app import SaaSLeadSuppression
        assert db.session.query(SaaSLeadSuppression).filter_by(email="borrar@example.com").count() == 1
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


def test_campaign_preflight_blocks_unknown_consent_and_is_channel_specific(app):
    from app import SaaSCampaign, SaaSLead, User, db
    from services.saas_campaign_preflight import campaign_preflight

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
        app.config["SMTP_HOST"] = "smtp.test"
        app.config["SMTP_USER"] = "user@test"
        app.config["SMTP_PASSWORD"] = "password"
        app.config["SAAS_CAMPAIGN_WORKER_TOKEN"] = "worker-token"
        app.config["SAAS_CAMPAIGN_WORKER_URL"] = "https://example.test/internal/crm/campaign-worker"
        user = User(username="preflight-admin", email="preflight@example.com", role="superadmin", active=True)
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()
        lead = SaaSLead(
            company_name="Sin permiso",
            contact_name="Contacto",
            email="sinpermiso@example.com",
            email_status="valid",
            email_consent_status="unknown",
            created_by_user_id=user.id,
        )
        db.session.add(lead)
        db.session.flush()
        campaign = SaaSCampaign(
            name="Preflight",
            subject="Hola",
            channel="email",
            status="BORRADOR",
            body_html="<p>Hola {{contacto}}</p>",
            body_text="Hola {{contacto}}",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.commit()

        report = campaign_preflight(db.session, campaign)
        assert report["ready"] is False
        assert any(item["code"] == "audience" for item in report["issues"])
        assert report["audience"]["eligible_email"] == 0


def test_delete_saas_lead_creates_permanent_suppression(app):
    from app import SaaSLead, SaaSLeadSuppression, User, db
    from services.saas_commercial_service import delete_saas_lead
    from services.saas_ops_service import SaaSOpsService

    with app.app_context():
        user = User(username="suppression-admin", email="suppression@example.com", role="superadmin", active=True)
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()
        lead = SaaSLead(
            company_name="No Volver",
            contact_name="Contacto",
            email="novolver@example.com",
            phone="+54 9 343 1234567",
            created_by_user_id=user.id,
        )
        db.session.add(lead)
        db.session.commit()
        lead_id = lead.id

        delete_saas_lead(db.session, lead_id, suppressing_user_id=user.id)
        db.session.commit()

        suppression = SaaSLeadSuppression.query.filter_by(email="novolver@example.com").first()
        assert suppression is not None
        assert suppression.created_by_user_id == user.id

        recreated = SaaSOpsService.create_or_update_lead(
            db.session,
            company_name="No Volver",
            contact_name="Contacto Nuevo",
            email="novolver@example.com",
            phone="+54 9 343 1234567",
            source="ops_auto",
            notes="reingreso",
            company_id=None,
            preferred_user_id=user.id,
        )
        assert recreated is None


def test_campaign_detail_has_rapid_consent_flow():
    template = open("templates/saas/crm_campaign_detail.html", encoding="utf-8").read()
    source = open("saas.py", encoding="utf-8").read()
    assert "Autorización rápida de esta campaña" in template
    assert "crm_campaign_bulk_consent" in template
    assert '/crm/campaigns/<int:campaign_id>/bulk-consent' in source
    assert "campaign.status != \"BORRADOR\"" in source




def test_parse_xlsx_detects_flexible_contact_headers():
    wb = Workbook()
    ws = wb.active
    ws.append(["Empresa", "Contacto Comercial", "Correo electrónico", "Teléfono", "WhatsApp Comercial", "Rubro", "Localidad"])
    ws.append([
        "Comercio Completo",
        "Juan Pérez",
        "ventas@completo.com",
        "+54 9 11 4444-1234",
        "+54 9 11 5555-5678",
        "Ferretería",
        "CABA",
    ])
    buf = BytesIO()
    wb.save(buf)

    result = parse_prospect_file(buf.getvalue(), "completo.xlsx")
    row = result["rows"][0]
    assert row["company_name"] == "Comercio Completo"
    assert row["contact_name"] == "Juan Pérez"
    assert row["email"] == "ventas@completo.com"
    assert row["phone"] == "5491144441234"
    assert row["whatsapp"] == "5491155555678"
    assert result["header_map"]["email"] == "Correo electrónico"
    assert result["header_map"]["whatsapp"] == "WhatsApp Comercial"


def test_import_preview_exposes_contact_mapping_and_visibility():
    template = open("templates/saas/crm_import.html", encoding="utf-8").read()
    source = open("saas.py", encoding="utf-8").read()
    assert '"header_map": header_map' in source
    assert '"contact_counts"' in source
    assert "Emails detectados" in template
    assert "WhatsApp detectados" in template
    assert "Consent. Email" in template
    assert "Columnas reconocidas" in template


def test_parse_xlsx_handles_oversized_contact_values_without_db_overflow():
    wb = Workbook()
    ws = wb.active
    ws.append(["Empresa", "Email", "Teléfono", "WhatsApp"])
    ws.append([
        "Comercio Seguro",
        "ventas@seguro.com",
        "1" * 500,
        "2" * 500,
    ])
    buf = BytesIO()
    wb.save(buf)

    result = parse_prospect_file(buf.getvalue(), "oversized.xlsx")
    row = result["rows"][0]
    assert row["email"] == "ventas@seguro.com"
    assert row["phone"] is None
    assert row["whatsapp"] is None
    assert row["phone_status"] == "invalid"


def test_smtp_probe_reports_connection_closed(app, monkeypatch):
    import smtplib

    with app.app_context():
        app.config["SMTP_HOST"] = "smtp.example.com"
        app.config["SMTP_PORT"] = 587
        app.config["SMTP_USER"] = "user@example.com"
        app.config["SMTP_PASSWORD"] = "secret"
        app.config["SMTP_USE_TLS"] = True

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def ehlo(self):
                return None
            def starttls(self):
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
            def login(self, *args):
                raise AssertionError("login should not run")

        def fake_smtp_server(*args, **kwargs):
            server = FakeSMTP()
            server.esmtp_features = {"starttls": ""}
            server.starttls()
            return server

        monkeypatch.setattr("services.saas_commercial_service._smtp_server", fake_smtp_server)
        ok, detail = _smtp_probe()
        assert ok is False
        assert "cerró la conexión" in detail.lower()


def test_smtp_probe_uses_ssl_for_port_465(app, monkeypatch):
    with app.app_context():
        app.config["SMTP_HOST"] = "smtp.example.com"
        app.config["SMTP_PORT"] = 465
        app.config["SMTP_USER"] = "user@example.com"
        app.config["SMTP_PASSWORD"] = "secret"
        app.config["SMTP_USE_TLS"] = True

        class FakeSMTPSSL:
            def __init__(self, *args, **kwargs):
                self.esmtp_features = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def ehlo(self, *args):
                self.esmtp_features = {"auth": "PLAIN"}
            def login(self, *args):
                return None

        monkeypatch.setattr("services.saas_commercial_service._SMTPSSLIPv4", FakeSMTPSSL)
        ok, detail = _smtp_probe()
        assert ok is True
        assert "465" in detail


def test_crm_campaign_worker_is_not_started_during_tests(app):
    from services.saas_commercial_service import start_crm_campaign_dispatch_worker

    with app.app_context():
        assert start_crm_campaign_dispatch_worker(app) is False


def test_crm_campaign_preflight_uses_in_process_worker_not_legacy_worker_url(app, monkeypatch):
    from services.saas_campaign_preflight import campaign_preflight

    class Campaign:
        name = "Campaña de prueba"
        subject = "StockARmobile"
        body_html = "<p>Hola</p>"
        body_text = "Hola"
        channel = "email"
        target_count = 1
        segment_json = "{}"

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
        app.config["SMTP_HOST"] = "smtp.example.com"
        app.config["SMTP_USER"] = "user@example.com"
        app.config["SMTP_PASSWORD"] = "test-password"
        app.config["SAAS_CAMPAIGN_WORKER_TOKEN"] = ""
        app.config["SAAS_CAMPAIGN_WORKER_URL"] = ""
        monkeypatch.setattr(
            "services.saas_commercial_service.campaign_audience_metrics",
            lambda db_session, campaign: {"eligible_email": 1},
        )
        monkeypatch.setattr(
            "services.saas_commercial_service.crm_campaign_dispatch_worker_started",
            lambda: True,
        )
        monkeypatch.setattr(
            "services.saas_campaign_preflight._smtp_probe",
            lambda: (True, "SMTP verificado."),
        )

        result = campaign_preflight(None, Campaign())
        worker_check = next(check for check in result["checks"] if check["code"] == "worker")
        assert worker_check["ok"] is True
        assert worker_check["detail"] == "Dispatcher interno iniciado."



def test_dispatch_due_campaigns_can_target_only_the_requested_campaign(app, monkeypatch):
    from app import SaaSCampaign, SaaSCampaignRecipient, SaaSLead, SaaSLeadConsent, User, db
    from services.saas_commercial_service import build_campaign_recipients, dispatch_due_campaigns

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
        user = User(
            username="targeted-dispatch-admin",
            email="targeted-dispatch-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        campaigns = []
        for suffix in ("older", "selected"):
            lead = SaaSLead(
                company_name=f"Target {suffix}",
                contact_name=f"Contact {suffix}",
                email=f"{suffix}@example.com",
                email_status="valid",
                email_consent_status="opted_in",
                created_by_user_id=user.id,
            )
            db.session.add(lead)
            db.session.flush()
            db.session.add(SaaSLeadConsent(
                lead_id=lead.id,
                unsubscribe_token=f"target-token-{suffix}",
                email_status="opted_in",
            ))
            campaign = SaaSCampaign(
                name=f"Campaign {suffix}",
                subject="Hola",
                channel="email",
                status="APROBADA",
                body_html="<p>Hola</p>",
                body_text="Hola",
                created_by_user_id=user.id,
            )
            db.session.add(campaign)
            db.session.flush()
            build_campaign_recipients(db.session, campaign.id)
            campaigns.append(campaign)
        db.session.commit()

        selected_target_count = SaaSCampaignRecipient.query.filter_by(campaign_id=campaigns[1].id).count()
        older_target_count = SaaSCampaignRecipient.query.filter_by(campaign_id=campaigns[0].id).count()
        sent_campaign_ids = []

        def fake_send_email(recipient, campaign):
            sent_campaign_ids.append(campaign.id)
            return True, "sent"

        monkeypatch.setattr("services.saas_commercial_service._send_email", fake_send_email)
        result = dispatch_due_campaigns(
            db.session,
            limit=1,
            per_campaign=50,
            campaign_id=campaigns[1].id,
        )

        db.session.refresh(campaigns[0])
        db.session.refresh(campaigns[1])
        older_recipients = SaaSCampaignRecipient.query.filter_by(campaign_id=campaigns[0].id).all()
        selected_recipients = SaaSCampaignRecipient.query.filter_by(campaign_id=campaigns[1].id).all()

        assert selected_target_count > 0
        assert len(older_recipients) == older_target_count
        assert len(selected_recipients) == selected_target_count
        assert result["sent"] == selected_target_count
        assert set(sent_campaign_ids) == {campaigns[1].id}
        assert len(sent_campaign_ids) == len(selected_recipients)
        assert all(recipient.status == "pending" for recipient in older_recipients)
        assert all(recipient.status == "sent" for recipient in selected_recipients)
        assert campaigns[0].status == "APROBADA"
        assert campaigns[1].status == "ENVIADA"


def test_gmail_sync_follows_pagination_and_carries_reply_metadata(app, monkeypatch):
    import base64
    import services.gmail_commercial_service as gmail_module

    captures = []
    class Request:
        def __init__(self, payload):
            self.payload = payload
        def execute(self):
            return self.payload

    class Messages:
        def list(self, **kwargs):
            if kwargs.get("pageToken") == "page-2":
                return Request({"messages": [{"id": "gmail-2"}]})
            return Request({"messages": [{"id": "gmail-1"}], "nextPageToken": "page-2"})
        def get(self, *, userId, id, format):
            headers = [
                {"name": "From", "value": f"Contacto {id} <{id}@example.com>"},
                {"name": "To", "value": "stockarmobile@gmail.com"},
                {"name": "Subject", "value": f"Consulta {id}"},
                {"name": "Message-ID", "value": f"<{id}@example.com>"},
                {"name": "References", "value": "<root@example.com>"},
            ]
            encoded = base64.urlsafe_b64encode(f"Mensaje {id}".encode()).decode().rstrip("=")
            return Request({
                "id": id,
                "threadId": f"thread-{id}",
                "payload": {"headers": headers, "mimeType": "text/plain", "body": {"data": encoded}},
            })
    class Users:
        def messages(self):
            return Messages()
    class FakeGmail:
        def users(self):
            return Users()

    monkeypatch.setenv("GMAIL_COMMERCIAL_MAILBOX", "stockarmobile@gmail.com")
    monkeypatch.setattr(gmail_module, "gmail_service", lambda: FakeGmail())
    monkeypatch.setattr(
        "services.saas_commercial_service.capture_inbound_email",
        lambda **kwargs: captures.append(kwargs) or {"status": "received"},
    )

    result = gmail_module.sync_recent_messages(hours=48, max_pages=4)

    assert result["found"] == 2
    assert result["pages"] == 2
    assert result["has_more"] is False
    assert result["received"] == 2
    assert [item["thread_id"] for item in captures] == ["thread-gmail-1", "thread-gmail-2"]
    assert captures[0]["rfc_message_id"] == "<gmail-1@example.com>"
    assert captures[0]["references"] == "<root@example.com>"


def test_gmail_commercial_reply_preserves_thread_and_message_headers(monkeypatch):
    import base64
    from email import message_from_bytes
    import services.gmail_commercial_service as gmail_module

    captured = {}
    class Request:
        def execute(self):
            return {"id": "sent-message-id", "threadId": "thread-123"}
    class Messages:
        def send(self, *, userId, body):
            captured.update(body)
            return Request()
    class Users:
        def messages(self):
            return Messages()
    class FakeGmail:
        def users(self):
            return Users()

    monkeypatch.setenv("GMAIL_COMMERCIAL_MAILBOX", "stockarmobile@gmail.com")
    monkeypatch.setattr(
        gmail_module,
        "_settings",
        lambda: {"mailbox": "stockarmobile@gmail.com"},
    )
    monkeypatch.setattr(gmail_module, "gmail_service", lambda: FakeGmail())

    result = gmail_module.send_commercial_reply(
        recipient_email="prospect@example.com",
        subject="Consulta de StockARmobile",
        body="Gracias por escribirnos.",
        thread_id="thread-123",
        in_reply_to="<incoming@example.com>",
        references="<root@example.com>",
    )

    raw = base64.urlsafe_b64decode(captured["raw"] + "=" * (-len(captured["raw"]) % 4))
    message = message_from_bytes(raw)
    assert captured["threadId"] == "thread-123"
    assert result["message_id"] == "sent-message-id"
    assert message["From"] == "stockarmobile@gmail.com"
    assert message["To"] == "prospect@example.com"
    assert message["Subject"] == "Re: Consulta de StockARmobile"
    assert message["In-Reply-To"] == "<incoming@example.com>"
    assert message["References"] == "<root@example.com> <incoming@example.com>"


def test_gmail_configuration_requires_pubsub_identity_and_confirmed_send_scope(monkeypatch):
    import services.gmail_commercial_service as gmail_module

    for key, value in {
        "GOOGLE_GMAIL_CLIENT_ID": "client-id",
        "GOOGLE_GMAIL_CLIENT_SECRET": "client-secret",
        "GMAIL_COMMERCIAL_REFRESH_TOKEN": "refresh-token",
        "GMAIL_COMMERCIAL_PUBSUB_TOPIC": "projects/project/topics/gmail",
        "GMAIL_COMMERCIAL_PUBSUB_SERVICE_ACCOUNT": "",
        "GMAIL_COMMERCIAL_SEND_SCOPE_CONFIRMED": "0",
    }.items():
        monkeypatch.setenv(key, value)

    state = gmail_module.commercial_gmail_configuration_status()
    assert state["watch_configured"] is True
    assert state["pubsub_configured"] is False
    assert state["send_configured"] is False

    monkeypatch.setenv("GMAIL_COMMERCIAL_PUBSUB_SERVICE_ACCOUNT", "pubsub@project.iam.gserviceaccount.com")
    monkeypatch.setenv("GMAIL_COMMERCIAL_SEND_SCOPE_CONFIRMED", "1")
    state = gmail_module.commercial_gmail_configuration_status()
    assert state["pubsub_configured"] is True
    assert state["send_configured"] is True


def test_gmail_watch_worker_is_not_started_during_tests(app):
    from services.gmail_commercial_service import start_gmail_watch_maintenance_worker

    with app.app_context():
        assert start_gmail_watch_maintenance_worker(app) is False


def test_campaign_preflight_prefers_authorized_gmail_api_over_smtp(app, monkeypatch):
    from services.saas_campaign_preflight import campaign_preflight

    class Campaign:
        name = "Campaña de prueba"
        subject = "StockARmobile"
        body_html = "<p>Hola</p>"
        body_text = "Hola"
        channel = "email"
        target_count = 1
        segment_json = "{}"

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
        monkeypatch.setattr(
            "services.saas_commercial_service.campaign_audience_metrics",
            lambda db_session, campaign: {"eligible_email": 1},
        )
        monkeypatch.setattr(
            "services.saas_commercial_service.crm_campaign_dispatch_worker_started",
            lambda: True,
        )
        monkeypatch.setattr(
            "services.gmail_commercial_service.commercial_gmail_configuration_status",
            lambda: {"send_configured": True, "mailbox": "stockarmobile@gmail.com"},
        )
        monkeypatch.setattr(
            "services.gmail_commercial_service.commercial_gmail_send_status",
            lambda: (True, "Gmail API verificado para stockarmobile@gmail.com."),
        )
        monkeypatch.setattr(
            "services.saas_campaign_preflight._smtp_probe",
            lambda: (_ for _ in ()).throw(AssertionError("No debe probar SMTP si Gmail API está autorizado")),
        )

        result = campaign_preflight(None, Campaign())
        email_check = next(check for check in result["checks"] if check["code"] == "email_config")
        assert email_check["ok"] is True
        assert "Gmail API verificado" in email_check["detail"]


def test_superadmin_campaign_email_uses_gmail_api_without_smtp(app, monkeypatch):
    from types import SimpleNamespace
    from services.saas_commercial_service import _send_email

    sent = []
    lead = SimpleNamespace(id=77, company_name="Comercio Demo", contact_name="Ana")
    recipient = SimpleNamespace(
        id=31,
        lead=lead,
        destination="prospect@example.com",
        tracking_token="campaign-track-token",
    )
    campaign = SimpleNamespace(
        id=12,
        subject="Novedades StockARmobile",
        body_text="Hola {{contacto}}",
        body_html="<p>Hola {{contacto}}</p>",
    )

    with app.app_context():
        app.config["SAAS_MARKETING_SEND_ENABLED"] = True
        app.config["SMTP_HOST"] = ""
        app.config["SMTP_USER"] = ""
        app.config["SMTP_PASSWORD"] = ""

        monkeypatch.setattr(
            "services.gmail_commercial_service.commercial_gmail_configuration_status",
            lambda: {"send_configured": True, "mailbox": "stockarmobile@gmail.com"},
        )
        monkeypatch.setattr(
            "services.gmail_commercial_service.send_commercial_message",
            lambda message: sent.append(message) or {"message_id": "gmail-message-123"},
        )
        monkeypatch.setattr(
            "services.saas_commercial_service.render_merge",
            lambda value, current_lead: str(value).replace("{{contacto}}", "Ana"),
        )
        monkeypatch.setattr("services.saas_commercial_service._unsubscribe_url", lambda current_lead: None)
        monkeypatch.setattr("services.saas_commercial_service._one_click_unsubscribe_url", lambda current_lead: None)
        monkeypatch.setattr(
            "services.saas_commercial_service._render_tracking_html",
            lambda html, current_recipient: html,
        )

        ok, detail = _send_email(recipient, campaign)

    assert ok is True
    assert detail == "sent"
    assert len(sent) == 1
    message = sent[0]
    assert message["From"] == "stockarmobile@gmail.com"
    assert message["To"] == "prospect@example.com"
    assert message["X-StockArMobile-Campaign"] == "12"
    assert message["X-StockArMobile-Recipient"] == "31"



def test_manual_campaign_recipient_selection_only_adds_selected_consented_contacts(app):
    from app import SaaSCampaign, SaaSCampaignRecipient, SaaSLead, SaaSLeadConsent, User, db
    from services.saas_commercial_service import add_selected_campaign_recipients

    with app.app_context():
        user = User(
            username="recipient-picker-admin",
            email="recipient-picker-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        email_ok = SaaSLead(
            company_name="Comercio con Email autorizado",
            contact_name="Ana",
            email="ana@example.com",
            email_status="valid",
            email_consent_status="opted_in",
            whatsapp_consent_status="unknown",
            created_by_user_id=user.id,
        )
        email_unknown = SaaSLead(
            company_name="Comercio sin permiso",
            contact_name="Luis",
            email="luis@example.com",
            email_status="valid",
            email_consent_status="unknown",
            created_by_user_id=user.id,
        )
        whatsapp_ok = SaaSLead(
            company_name="Comercio con WhatsApp autorizado",
            contact_name="Eva",
            whatsapp="5493624000000",
            whatsapp_consent_status="opted_in",
            email_consent_status="unknown",
            created_by_user_id=user.id,
        )
        db.session.add_all([email_ok, email_unknown, whatsapp_ok])
        db.session.flush()
        db.session.add_all([
            SaaSLeadConsent(lead_id=email_ok.id, unsubscribe_token="recipient-email-ok"),
            SaaSLeadConsent(lead_id=email_unknown.id, unsubscribe_token="recipient-email-unknown"),
            SaaSLeadConsent(lead_id=whatsapp_ok.id, unsubscribe_token="recipient-whatsapp-ok"),
        ])
        campaign = SaaSCampaign(
            name="Selector manual",
            subject="Prueba",
            channel="both",
            body_html="<p>Hola</p>",
            body_text="Hola",
            created_by_user_id=user.id,
        )
        db.session.add(campaign)
        db.session.commit()

        result = add_selected_campaign_recipients(
            db.session,
            campaign.id,
            [
                ("email", email_ok.id),
                ("email", email_unknown.id),
                ("whatsapp", whatsapp_ok.id),
                ("whatsapp", email_ok.id),
            ],
        )
        db.session.commit()
        recipients = {
            (row.lead_id, row.channel)
            for row in SaaSCampaignRecipient.query.filter_by(campaign_id=campaign.id).all()
        }

        assert result["added"] == 2
        assert recipients == {(email_ok.id, "email"), (whatsapp_ok.id, "whatsapp")}
        assert campaign.target_count == 2


def test_argentine_whatsapp_numbers_are_normalized_to_international_format():
    from types import SimpleNamespace
    from services.saas_commercial_service import (
        _lead_whatsapp_destination,
        normalize_whatsapp_destination,
    )

    assert normalize_whatsapp_destination("3624228396") == "5493624228396"
    assert normalize_whatsapp_destination("0362 15 422-8396") == "5493624228396"
    assert normalize_whatsapp_destination("+54 9 362 422-8396") == "5493624228396"
    assert normalize_whatsapp_destination("5493624228396") == "5493624228396"
    assert _lead_whatsapp_destination(SimpleNamespace(whatsapp="3624228396")) == "5493624228396"

def test_whatsapp_send_normalizes_legacy_local_destination(app, monkeypatch):
    from types import SimpleNamespace
    from services.saas_commercial_service import _send_whatsapp

    with app.app_context():
        sent = []
        company = SimpleNamespace(id=999)
        monkeypatch.setattr(
            "services.saas_commercial_whatsapp.get_commercial_company",
            lambda: company,
        )
        monkeypatch.setattr(
            "services.ai_agent.config_service.get_whatsapp_connection",
            lambda _company: {
                "enabled": True,
                "phone_number_id": "COMMERCIAL_PHONE_QA",
                "access_token": "test-token",
            },
        )

        def fake_send_template(_company, **kwargs):
            sent.append(kwargs)
            return {"messages": [{"id": "wamid.local-number-normalized"}]}

        monkeypatch.setattr(
            "services.ai_agent.whatsapp_service.WhatsAppService.send_template",
            fake_send_template,
        )
        recipient = SimpleNamespace(
            destination="3624228396",
            lead=SimpleNamespace(
                company_name="Comercio de prueba",
                contact_name="Camilo",
                industry="Comercio",
                locality="Resistencia",
            ),
        )
        campaign = SimpleNamespace(
            whatsapp_template_name="campana_marketing_es_ar",
            whatsapp_template_language="es_AR",
            whatsapp_parameter_fields="",
            body_text="Hola {{contacto}}, probá StockARmobile.",
            subject="StockARmobile",
        )

        ok, detail, provider_id = _send_whatsapp(recipient, campaign)

        assert ok is True
        assert detail == "sent"
        assert provider_id == "wamid.local-number-normalized"
        assert sent[0]["to"] == "5493624228396"
        assert recipient.destination == "5493624228396"


def test_prospect_import_records_new_updated_and_duplicate_rows(app):
    from app import SaaSLeadImportRecord, User, db
    from services.saas_commercial_service import import_prospect_rows

    with app.app_context():
        user = User(
            username="import-history-admin",
            email="import-history-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        row = {
            "company_name": "Ferretería Importada",
            "contact_name": "Ana",
            "email": "ventas@importada.example",
            "phone": None,
            "whatsapp": "5491112345678",
            "industry": "Ferretería",
            "subindustry": None,
            "province": "Chaco",
            "locality": "Resistencia",
            "lead_score": 75,
            "email_status": "valid",
            "phone_status": "unknown",
            "email_consent_status": "unknown",
            "whatsapp_consent_status": "unknown",
            "phone_consent_status": "unknown",
            "do_not_contact": False,
            "source": "import_publico",
            "website": None,
        }
        first = import_prospect_rows(
            db.session,
            rows=[row, {**row, "company_name": "Ferretería repetida"}],
            filename="comercios.xlsx",
            user_id=user.id,
        )
        db.session.commit()

        assert first.inserted_count == 1
        assert first.duplicate_count == 1
        first_records = SaaSLeadImportRecord.query.filter_by(import_id=first.id).order_by(
            SaaSLeadImportRecord.row_number.asc()
        ).all()
        assert [record.outcome for record in first_records] == ["new", "duplicate"]
        assert first_records[0].lead_id is not None
        assert first_records[1].lead_id == first_records[0].lead_id

        updated_row = {**row, "website": "https://ferreteria.example"}
        second = import_prospect_rows(
            db.session,
            rows=[updated_row],
            filename="actualizacion.csv",
            user_id=user.id,
        )
        db.session.commit()
        assert second.inserted_count == 0
        assert second.updated_count == 1
        assert second.duplicate_count == 0
        assert SaaSLeadImportRecord.query.filter_by(
            import_id=second.id, outcome="updated"
        ).count() == 1

        third = import_prospect_rows(
            db.session,
            rows=[updated_row],
            filename="repeticion.csv",
            user_id=user.id,
        )
        db.session.commit()
        assert third.inserted_count == 0
        assert third.updated_count == 0
        assert third.duplicate_count == 1
        assert SaaSLeadImportRecord.query.filter_by(
            import_id=third.id, outcome="duplicate"
        ).count() == 1


def test_campaign_segments_by_import_date_and_never_campaign(app):
    import json
    from datetime import datetime
    from app import (
        SaaSCampaign,
        SaaSCampaignRecipient,
        SaaSLead,
        SaaSLeadImport,
        SaaSLeadImportRecord,
        User,
        db,
    )

    with app.app_context():
        user = User(
            username="prospect-segment-admin",
            email="prospect-segment-admin@example.com",
            role="superadmin",
            active=True,
        )
        user.set_password("test-password")
        db.session.add(user)
        db.session.flush()

        batch = SaaSLeadImport(
            file_name="lote-nuevos.csv",
            file_format="csv",
            source="import_publico",
            created_by_user_id=user.id,
        )
        db.session.add(batch)
        db.session.flush()

        new_lead = SaaSLead(
            company_name="Nuevo de la importación",
            contact_name="Contacto nuevo",
            email="nuevo@segmento.example",
            email_status="valid",
            email_consent_status="opted_in",
            created_at=datetime(2026, 11, 20, 10, 0, 0),
            created_by_user_id=user.id,
        )
        old_lead = SaaSLead(
            company_name="Antiguo de la importación",
            contact_name="Contacto antiguo",
            email="antiguo@segmento.example",
            email_status="valid",
            email_consent_status="opted_in",
            created_at=datetime(2026, 10, 20, 10, 0, 0),
            created_by_user_id=user.id,
        )
        already_campaigned = SaaSLead(
            company_name="Ya incluido en otra campaña",
            contact_name="Contacto previo",
            email="previo@segmento.example",
            email_status="valid",
            email_consent_status="opted_in",
            created_at=datetime(2026, 11, 21, 10, 0, 0),
            created_by_user_id=user.id,
        )
        db.session.add_all([new_lead, old_lead, already_campaigned])
        db.session.flush()

        db.session.add_all([
            SaaSLeadImportRecord(
                import_id=batch.id, lead_id=new_lead.id, row_number=1,
                outcome="new", company_name=new_lead.company_name,
            ),
            SaaSLeadImportRecord(
                import_id=batch.id, lead_id=old_lead.id, row_number=2,
                outcome="new", company_name=old_lead.company_name,
            ),
            SaaSLeadImportRecord(
                import_id=batch.id, lead_id=already_campaigned.id, row_number=3,
                outcome="new", company_name=already_campaigned.company_name,
            ),
        ])

        previous_campaign = SaaSCampaign(
            name="Campaña anterior",
            subject="Presentación",
            channel="email",
            status="ENVIADA",
            body_html="<p>Hola</p>",
            created_by_user_id=user.id,
        )
        target_campaign = SaaSCampaign(
            name="Solo nuevos del lote",
            subject="StockArMobile",
            channel="email",
            status="BORRADOR",
            body_html="<p>Hola {{contacto}}</p>",
            segment_json=json.dumps({
                "created_from": "2026-11-01",
                "created_to": "2026-11-30",
                "import_id": batch.id,
                "never_campaign": True,
            }),
            created_by_user_id=user.id,
        )
        db.session.add_all([previous_campaign, target_campaign])
        db.session.flush()
        db.session.add(SaaSCampaignRecipient(
            campaign_id=previous_campaign.id,
            lead_id=already_campaigned.id,
            channel="email",
            destination=already_campaigned.email,
            status="sent",
        ))
        db.session.commit()

        result = build_campaign_recipients(db.session, target_campaign.id)
        assert result["eligible"] == 1
        assert result["added"] == 1
        assert [recipient.lead_id for recipient in target_campaign.recipients] == [new_lead.id]
