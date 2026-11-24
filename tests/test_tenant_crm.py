from datetime import datetime
import json

from flask_login import login_user

from stockarmobile.permissions import employee_endpoint_permission


def test_tenant_crm_permission_matrix():
    assert employee_endpoint_permission("crm.index", "GET") == "crm"
    assert employee_endpoint_permission("crm.opportunity_new", "POST") == "crm"
    assert employee_endpoint_permission("crm.activity_complete", "POST") == "crm"


def test_tenant_crm_models_are_separate_from_saas_crm():
    from crm_models import CRMActivity, CRMOpportunity
    assert CRMOpportunity.__tablename__ == "crm_opportunities"
    assert CRMActivity.__tablename__ == "crm_activities"
    assert not CRMOpportunity.__tablename__.startswith("saas_")
    assert not CRMActivity.__tablename__.startswith("saas_")


def test_tenant_crm_ai_tool_is_read_only_and_registered():
    from services.ai_agent.orchestrator_v2 import AgentRuntime
    assert "oportunidades_crm" in AgentRuntime.tool_registry
    assert "oportunidades_crm" in AgentRuntime.agent_tool_names["analista"]
    assert "oportunidades_crm" in AgentRuntime.agent_tool_names["marketing"]


def test_tenant_crm_requires_ia_pro_route_guards():
    from crm import bp

    from pathlib import Path

    source = __import__("crm", fromlist=["__file__"]).__file__
    source_text = Path(source).read_text(encoding="utf-8")
    assert source_text.count("@tenant_required\n@ai_pro_crm_required") == 14


def test_tenant_crm_menu_is_plan_gated():
    template = open("templates/base_master.html", encoding="utf-8").read()
    assert "{% if ai_feature_crm_allowed and ai_feature_crm_permission_allowed %}" in template
    assert "CRM · requiere IA PRO" in template
    assert "CRM · requiere permiso" in template
    assert "overflow-y: auto;" in template



def test_crm_terminal_stages_are_closed():
    from crm import _stage_status

    assert _stage_status("nuevo") == "open"
    assert _stage_status("presupuesto") == "open"
    assert _stage_status("ganado") == "closed"
    assert _stage_status("perdido") == "closed"


def test_quote_conversion_updates_only_its_linked_crm_opportunity(app):
    from app import Client, Company, Quote, Sale, User, db, utcnow
    from crm_models import CRMOpportunity
    from sales import _mark_quote_as_converted_from_checkout_token

    company = Company(
        name="Comercio CRM",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    other_company = Company(
        name="Otro comercio CRM",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add_all([company, other_company])
    db.session.flush()

    user = User(
        username="crm_owner",
        email="crm-owner@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    other_user = User(
        username="crm_other_owner",
        email="crm-other@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=other_company.id,
    )
    db.session.add_all([user, other_user])
    db.session.flush()

    client = Client(name="Cliente CRM", company_id=company.id, active=True)
    other_client = Client(name="Otro cliente CRM", company_id=other_company.id, active=True)
    db.session.add_all([client, other_client])
    db.session.flush()

    quote = Quote(
        number="CRM-Q-0001",
        company_id=company.id,
        client_id=client.id,
        created_by_user_id=user.id,
        seller_id=user.id,
        status="APROBADO",
        date=utcnow(),
        subtotal=1000,
        total_amount=1000,
    )
    db.session.add(quote)
    db.session.flush()
    sale = Sale(
        company_id=company.id,
        client_id=client.id,
        seller_id=user.id,
        customer=client.name,
        status="confirmada",
        subtotal=1000,
        total_amount=1000,
        date=utcnow(),
    )
    unrelated = CRMOpportunity(
        company_id=other_company.id,
        client_id=other_client.id,
        title="No tocar oportunidad ajena",
        stage="presupuesto",
        status="open",
        value=500,
        probability=50,
        owner_user_id=other_user.id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.session.add_all([sale, unrelated])
    db.session.flush()
    opportunity = CRMOpportunity(
        company_id=company.id,
        client_id=client.id,
        title="Oportunidad del presupuesto",
        stage="presupuesto",
        status="open",
        value=900,
        probability=60,
        owner_user_id=user.id,
        quote_id=quote.id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.session.add(opportunity)
    db.session.commit()

    with app.test_request_context("/"):
        login_user(user)
        _mark_quote_as_converted_from_checkout_token(f"quote-cart-{quote.id}", sale.id)
        db.session.commit()

    db.session.refresh(quote)
    db.session.refresh(opportunity)
    db.session.refresh(unrelated)
    assert quote.status == "CONVERTIDO"
    assert quote.converted_sale_id == sale.id
    assert opportunity.sale_id == sale.id
    assert opportunity.stage == "ganado"
    assert opportunity.status == "closed"
    assert float(opportunity.value) == 1000
    assert float(opportunity.probability) == 100
    assert unrelated.sale_id is None
    assert unrelated.stage == "presupuesto"
    assert unrelated.status == "open"

    # If that confirmed sale is later annulled, its opportunity must no longer
    # remain falsely marked as won; keep the sale link for auditability.
    from decimal import Decimal
    from services.sales.sale_service import SaleService

    service = SaleService(
        require_open_cash_session=lambda **kwargs: None,
        calculate_lines=lambda *args, **kwargs: [],
        mark_quote_as_converted=lambda *args, **kwargs: None,
        cart_key=lambda: "test-cart",
        to_decimal=lambda value: Decimal(str(value or 0)),
    )
    sale.status = "anulada"
    service._sync_crm_opportunity_after_sale_annulment(sale)
    db.session.commit()
    db.session.refresh(opportunity)
    assert opportunity.stage == "perdido"
    assert opportunity.status == "closed"
    assert float(opportunity.probability) == 0
    assert opportunity.sale_id == sale.id
    assert "Venta #" + str(sale.id) + " anulada" in opportunity.notes


def test_crm_commercial_links_require_same_tenant_and_client(app):
    from app import Client, Company, Quote, User, db, utcnow
    from crm import _resolve_commercial_links
    from flask_login import login_user

    company = Company(name="Comercio vínculos CRM", active=True, preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}))
    db.session.add(company)
    db.session.flush()
    user = User(username="crm_link_owner", email="crm-link@test.local", password_hash="x", role="admin", active=True, company_id=company.id)
    db.session.add(user)
    db.session.flush()
    client_a = Client(name="Cliente A CRM", company_id=company.id, active=True)
    client_b = Client(name="Cliente B CRM", company_id=company.id, active=True)
    db.session.add_all([client_a, client_b])
    db.session.flush()
    quote = Quote(number="CRM-Q-0002", company_id=company.id, client_id=client_a.id, created_by_user_id=user.id, seller_id=user.id, status="APROBADO", date=utcnow(), total_amount=100)
    db.session.add(quote)
    db.session.commit()

    with app.test_request_context("/"):
        login_user(user)
        resolved, sale = _resolve_commercial_links(client_a.id, quote.id, None)
        assert resolved.id == quote.id
        assert sale is None
        try:
            _resolve_commercial_links(client_b.id, quote.id, None)
        except ValueError as exc:
            assert "mismo cliente" in str(exc)
        else:
            raise AssertionError("La función aceptó un presupuesto de otro cliente")



def _login_crm_user(test_client, user):
    with test_client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def test_prospect_capture_deduplicates_client_and_creates_first_followup(app):
    from app import Client, Company, User, db
    from crm_models import CRMActivity, CRMOpportunity

    company = Company(
        name="CRM Prospectos",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="crm_prospect_admin",
        email="crm-prospect@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    db.session.add(user)
    db.session.commit()

    test_client = app.test_client()
    _login_crm_user(test_client, user)
    missing_whatsapp = test_client.post(
        "/crm/prospects/new",
        data={"name": "Prospecto sin WhatsApp"},
    )
    assert missing_whatsapp.status_code == 400
    assert Client.query.filter_by(company_id=company.id).count() == 0

    response = test_client.post(
        "/crm/prospects/new",
        data={
            "name": "Comercio Prospecto",
            "phone": "+54 9 11 2233-4455",
            "whatsapp": "5491122334455",
            "opportunity_title": "Primera demostración",
            "source": "Referido",
            "value": "150000",
            "probability": "20",
            "followup_subject": "Llamar para coordinar demo",
            "followup_due_at": "2030-01-15T10:00",
        },
    )
    assert response.status_code == 302
    prospect = Client.query.filter_by(company_id=company.id).one()
    first_opportunity = CRMOpportunity.query.filter_by(company_id=company.id).one()
    first_activity = CRMActivity.query.filter_by(company_id=company.id).one()
    assert not prospect.email
    assert first_opportunity.client_id == prospect.id
    assert first_opportunity.stage == "nuevo"
    assert first_opportunity.status == "open"
    assert first_opportunity.source == "Referido"
    assert first_activity.opportunity_id == first_opportunity.id
    assert first_activity.client_id == prospect.id
    assert first_activity.status == "pending"

    response = test_client.post(
        "/crm/prospects/new",
        data={
            "name": "Nombre alternativo no debe sobrescribir al cliente existente",
            "email": "ventas@prospecto.test",
            "whatsapp": "5491122334455",
            "opportunity_title": "Segunda necesidad",
            "source": "WhatsApp",
            "value": "200000",
            "probability": "15",
        },
    )
    assert response.status_code == 302
    assert Client.query.filter_by(company_id=company.id).count() == 1
    assert CRMOpportunity.query.filter_by(company_id=company.id).count() == 2
    assert prospect.name == "Comercio Prospecto"


def test_new_opportunity_won_stage_closes_and_sets_probability(app):
    from app import Client, Company, User, db
    from crm_models import CRMOpportunity

    company = Company(
        name="CRM Oportunidades",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="crm_opportunity_admin",
        email="crm-opportunity@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    db.session.add(user)
    db.session.flush()
    client = Client(name="Cliente con oportunidad", company_id=company.id, active=True)
    db.session.add(client)
    db.session.commit()

    test_client = app.test_client()
    _login_crm_user(test_client, user)
    response = test_client.post(
        "/crm/opportunities/new",
        data={
            "client_id": str(client.id),
            "title": "Venta ganada manualmente",
            "stage": "ganado",
            "value": "87500",
            "probability": "35",
            "expected_close_date": "",
            "owner_user_id": str(user.id),
            "source": "Mostrador",
            "quote_id": "",
            "sale_id": "",
            "notes": "",
        },
    )
    assert response.status_code == 302
    opportunity = CRMOpportunity.query.filter_by(company_id=company.id, title="Venta ganada manualmente").one()
    assert opportunity.stage == "ganado"
    assert opportunity.status == "closed"
    assert float(opportunity.probability) == 100



def test_client_contact_action_logs_followup_and_opens_whatsapp(app):
    from app import Client, Company, User, db
    from crm_models import CRMActivity

    company = Company(
        name="CRM Contactos",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="crm_contact_admin",
        email="crm-contact@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    client_record = Client(
        name="Contacto WhatsApp",
        company_id=company.id,
        phone="3624228396",
        whatsapp="3624228396",
        active=True,
    )
    db.session.add_all([user, client_record])
    db.session.commit()

    test_client = app.test_client()
    _login_crm_user(test_client, user)
    response = test_client.post(f"/crm/clients/{client_record.id}/contact/whatsapp")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/crm/clients/{client_record.id}?confirm_whatsapp=1")
    assert CRMActivity.query.filter_by(company_id=company.id).count() == 0

    confirmation_page = test_client.get(response.headers["Location"])
    assert confirmation_page.status_code == 200
    assert b"Sin autorizaci" in confirmation_page.data
    assert b"Confirmar contacto no promocional" in confirmation_page.data

    from pathlib import Path
    template = Path("templates/crm/client_360.html").read_text(encoding="utf-8")
    assert 'target="_blank"' in template
    base_template = Path("templates/base_master.html").read_text(encoding="utf-8")
    assert "if ((form.target || '').toLowerCase() === '_blank') return;" in base_template

    confirmed = test_client.post(
        f"/crm/clients/{client_record.id}/contact/whatsapp",
        data={"confirm_non_marketing": "1"},
    )
    assert confirmed.status_code == 302
    assert confirmed.headers["Location"] == "https://wa.me/5493624228396"
    activity = CRMActivity.query.filter_by(company_id=company.id).one()
    assert activity.client_id == client_record.id
    assert activity.type == "whatsapp"
    assert activity.status == "pending"
    assert "no puede confirmar" in activity.description
    assert "comunicación no promocional" in activity.description

    rejected_email = test_client.post(f"/crm/clients/{client_record.id}/contact/email")
    assert rejected_email.status_code == 400
    assert CRMActivity.query.filter_by(company_id=company.id).count() == 1



def test_lost_crm_opportunity_requires_reason_and_persists_it(app):
    from app import Client, Company, User, db
    from crm_models import CRMOpportunity

    company = Company(
        name="CRM Motivos",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="crm_loss_admin",
        email="crm-loss@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    db.session.add(user)
    db.session.flush()
    client = Client(name="Cliente no convertido", company_id=company.id, active=True)
    db.session.add(client)
    db.session.commit()

    test_client = app.test_client()
    _login_crm_user(test_client, user)
    base_payload = {
        "client_id": str(client.id),
        "title": "Oportunidad perdida con motivo",
        "stage": "perdido",
        "value": "32000",
        "probability": "10",
        "expected_close_date": "",
        "owner_user_id": str(user.id),
        "source": "WhatsApp",
        "quote_id": "",
        "sale_id": "",
        "notes": "Seguimiento inicial",
    }
    missing_reason = test_client.post("/crm/opportunities/new", data=base_payload)
    assert missing_reason.status_code == 400
    assert CRMOpportunity.query.filter_by(company_id=company.id).count() == 0

    valid_payload = {**base_payload, "loss_reason": "Eligió otra solución"}
    response = test_client.post("/crm/opportunities/new", data=valid_payload)
    assert response.status_code == 302
    opportunity = CRMOpportunity.query.filter_by(company_id=company.id, title=base_payload["title"]).one()
    assert opportunity.stage == "perdido"
    assert opportunity.status == "closed"
    assert float(opportunity.probability) == 0
    assert opportunity.loss_reason == "Eligió otra solución"



def test_tenant_crm_is_whatsapp_only_and_superadmin_crm_keeps_email():
    from pathlib import Path
    from crm import ACTIVITY_TYPES

    assert all(value != "email" for value, _label in ACTIVITY_TYPES)
    prospect_form = Path("templates/crm/prospect_form.html").read_text(encoding="utf-8")
    client_360 = Path("templates/crm/client_360.html").read_text(encoding="utf-8")
    campaigns = Path("templates/ai_agents/campaign_detail.html").read_text(encoding="utf-8")
    sidebar = Path("templates/base_master.html").read_text(encoding="utf-8")
    tenant_delivery = Path("services/ai_agent/tenant_campaign_service.py").read_text(encoding="utf-8")
    superadmin_delivery = Path("services/saas_commercial_service.py").read_text(encoding="utf-8")

    assert 'name="email"' not in prospect_form
    crm_nav_gate = sidebar.split("{% if ai_feature_crm_allowed", 1)[1].split("%}", 1)[0]
    assert "current_user.role" not in crm_nav_gate
    assert "current_user_has_permission" not in crm_nav_gate
    assert "mailto:" not in client_360
    assert "channel='email'" not in client_360
    assert 'name="channel"' not in campaigns
    assert 'CHANNELS = {"whatsapp"}' in tenant_delivery
    assert "def _send_email(" not in tenant_delivery
    assert "def _send_email(" in superadmin_delivery


def test_tenant_public_consent_link_records_preferences_per_channel(app):
    from app import Client, Company, WhatsAppConsentEvent, db

    app.config["WTF_CSRF_ENABLED"] = False
    company = Company(name="Consentimiento comercio", active=True)
    db.session.add(company)
    db.session.flush()
    client_record = Client(
        name="Cliente que decide",
        email="cliente-consent@example.test",
        phone="+54 9 362 555-1000",
        whatsapp="5493625551000",
        company_id=company.id,
        active=True,
        marketing_consent_token="tenant-consent-token-001",
    )
    db.session.add(client_record)
    db.session.commit()
    client_id = client_record.id

    http = app.test_client()
    page = http.get("/crm/consent/tenant-consent-token-001")
    assert page.status_code == 200
    assert b"Consentimiento comercial" in page.data
    assert b"email_opted_in" in page.data
    assert b"whatsapp_opted_in" in page.data

    saved = http.post(
        "/crm/consent/tenant-consent-token-001",
        data={"email_opted_in": "1", "whatsapp_opted_in": "1"},
    )
    assert saved.status_code == 200
    assert b"Preferencias guardadas" in saved.data

    db.session.expire_all()
    stored = db.session.get(Client, client_id)
    assert stored.email_marketing_consent == "opted_in"
    assert stored.email_marketing_consent_recorded_at is not None
    assert stored.email_marketing_consent_source == "public_consent_link"
    assert stored.email_marketing_consent_evidence
    assert stored.whatsapp_marketing_consent == "opted_in"
    assert stored.whatsapp_marketing_consent_recorded_at is not None
    assert stored.whatsapp_marketing_consent_source == "public_consent_link"
    assert stored.whatsapp_marketing_consent_evidence
    assert stored.phone_marketing_consent == "opted_out"
    assert stored.phone_marketing_consent_recorded_at is not None
    assert stored.phone_marketing_consent_source == "public_consent_link"
    event = WhatsAppConsentEvent.query.filter_by(
        company_id=company.id, client_id=client_id, new_status="opted_in"
    ).one()
    assert event.source == "public_consent_link"
    assert "enlace público" in event.evidence


def test_tenant_public_consent_link_get_is_read_only_and_can_revoke_all(app):
    from app import Client, Company, db

    app.config["WTF_CSRF_ENABLED"] = False
    company = Company(name="Consentimiento revocable", active=True)
    db.session.add(company)
    db.session.flush()
    client_record = Client(
        name="Cliente revocable",
        email="revoke@example.test",
        phone="3625551001",
        whatsapp="5493625551001",
        company_id=company.id,
        active=True,
        email_marketing_consent="opted_in",
        whatsapp_marketing_consent="opted_in",
        marketing_consent_token="tenant-consent-token-002",
    )
    db.session.add(client_record)
    db.session.commit()
    client_id = client_record.id

    http = app.test_client()
    response = http.get("/crm/consent/tenant-consent-token-002")
    assert response.status_code == 200
    db.session.expire_all()
    stored = db.session.get(Client, client_id)
    assert stored.email_marketing_consent == "opted_in"
    assert stored.whatsapp_marketing_consent == "opted_in"

    response = http.post(
        "/crm/consent/tenant-consent-token-002",
        data={"reject_all": "1"},
    )
    assert response.status_code == 200
    db.session.expire_all()
    stored = db.session.get(Client, client_id)
    assert stored.email_marketing_consent == "opted_out"
    assert stored.whatsapp_marketing_consent == "opted_out"
    assert stored.phone_marketing_consent == "opted_out"


def test_tenant_public_consent_link_rejects_invalid_tokens_and_inactive_clients(app):
    from app import Client, Company, db

    app.config["WTF_CSRF_ENABLED"] = False
    company = Company(name="Consentimiento invalido", active=True)
    db.session.add(company)
    db.session.flush()
    inactive = Client(
        name="Cliente inactivo",
        company_id=company.id,
        active=False,
        marketing_consent_token="tenant-consent-token-003",
    )
    db.session.add(inactive)
    db.session.commit()

    http = app.test_client()
    assert http.get("/crm/consent/no-existe").status_code == 404
    assert http.get("/crm/consent/tenant-consent-token-003").status_code == 404


def test_tenant_client_profile_can_generate_consent_link_only_for_own_company(app):
    from app import Client, Company, User, db

    app.config["WTF_CSRF_ENABLED"] = False
    company = Company(name="Comercio dueño del contacto", active=True)
    other_company = Company(name="Otro comercio", active=True)
    db.session.add_all([company, other_company])
    db.session.flush()
    user = User(
        username="consent-link-admin",
        email="consent-link-admin@example.test",
        role="admin",
        active=True,
        company_id=company.id,
    )
    user.set_password("test-password")
    own_client = Client(
        name="Cliente propio",
        email="own@example.test",
        whatsapp="5493625551010",
        company_id=company.id,
        active=True,
    )
    foreign_client = Client(
        name="Cliente ajeno",
        email="foreign@example.test",
        whatsapp="5493625551011",
        company_id=other_company.id,
        active=True,
    )
    db.session.add_all([user, own_client, foreign_client])
    db.session.commit()

    http = app.test_client()
    _login_crm_user(http, user)
    response = http.post(f"/clientes/{own_client.id}/consent-link")
    assert response.status_code == 302
    db.session.refresh(own_client)
    assert own_client.marketing_consent_token
    assert len(own_client.marketing_consent_token) >= 48

    foreign_response = http.post(f"/clientes/{foreign_client.id}/consent-link")
    assert foreign_response.status_code == 404
    db.session.refresh(foreign_client)
    assert foreign_client.marketing_consent_token is None


def test_tenant_quick_consent_registers_all_valid_channels_with_evidence_and_is_tenant_scoped(app):
    from app import Client, Company, User, WhatsAppConsentEvent, db

    app.config["WTF_CSRF_ENABLED"] = False
    company = Company(name="Comercio permisos rápidos", active=True)
    other_company = Company(name="Comercio ajeno", active=True)
    db.session.add_all([company, other_company])
    db.session.flush()
    user = User(
        username="quick-consent-admin",
        email="quick-consent-admin@example.test",
        role="admin",
        active=True,
        company_id=company.id,
    )
    user.set_password("test-password")
    own_client = Client(
        name="Cliente autorización rápida",
        email="quick-consent@example.test",
        phone="3625551200",
        whatsapp="5493625551200",
        company_id=company.id,
        active=True,
    )
    foreign_client = Client(
        name="Cliente de otro comercio",
        email="foreign-quick-consent@example.test",
        whatsapp="5493625551201",
        company_id=other_company.id,
        active=True,
    )
    db.session.add_all([user, own_client, foreign_client])
    db.session.commit()
    own_client_id = own_client.id
    foreign_client_id = foreign_client.id

    http = app.test_client()
    _login_crm_user(http, user)

    profile = http.get(f"/clientes/edit/{own_client_id}")
    assert profile.status_code == 200
    assert b"Permisos comerciales" in profile.data
    assert b"Autorizar todos los disponibles" in profile.data

    missing_evidence = http.post(
        f"/clientes/{own_client_id}/marketing-consent",
        data={"action": "accept_all", "source": "in_person"},
    )
    assert missing_evidence.status_code == 302
    db.session.expire_all()
    assert db.session.get(Client, own_client_id).email_marketing_consent == "unknown"

    saved = http.post(
        f"/clientes/{own_client_id}/marketing-consent",
        data={
            "action": "accept_all",
            "source": "in_person",
            "evidence": "El cliente autorizó verbalmente en el mostrador el 20/11/2026.",
        },
    )
    assert saved.status_code == 302
    db.session.expire_all()
    stored = db.session.get(Client, own_client_id)
    assert stored.email_marketing_consent == "opted_in"
    assert stored.whatsapp_marketing_consent == "opted_in"
    assert stored.phone_marketing_consent == "opted_in"
    assert stored.email_marketing_consent_recorded_at is not None
    assert stored.email_marketing_consent_source == "in_person"
    assert "autorizó verbalmente" in stored.email_marketing_consent_evidence
    assert stored.phone_marketing_consent_recorded_at is not None
    assert stored.phone_marketing_consent_source == "in_person"
    event = WhatsAppConsentEvent.query.filter_by(
        company_id=company.id,
        client_id=own_client_id,
        new_status="opted_in",
    ).one()
    assert event.source == "in_person"
    assert "autorizó verbalmente" in event.evidence

    foreign = http.post(
        f"/clientes/{foreign_client_id}/marketing-consent",
        data={
            "action": "accept_all",
            "source": "in_person",
            "evidence": "El cliente autorizó verbalmente.",
        },
    )
    assert foreign.status_code == 404
    db.session.expire_all()
    assert db.session.get(Client, foreign_client_id).email_marketing_consent == "unknown"


def test_tenant_public_consent_accept_all_shortcut_authorizes_only_available_channels(app):
    from app import Client, Company, db

    app.config["WTF_CSRF_ENABLED"] = False
    company = Company(name="Comercio aceptar todos", active=True)
    db.session.add(company)
    db.session.flush()
    client_record = Client(
        name="Cliente aceptar todos",
        email="accept-all@example.test",
        phone="3625551300",
        whatsapp="5493625551300",
        company_id=company.id,
        active=True,
        marketing_consent_token="tenant-consent-accept-all",
    )
    db.session.add(client_record)
    db.session.commit()
    client_id = client_record.id

    http = app.test_client()
    page = http.get("/crm/consent/tenant-consent-accept-all")
    assert page.status_code == 200
    assert b"Aceptar todos" in page.data

    response = http.post(
        "/crm/consent/tenant-consent-accept-all",
        data={"action": "accept_all"},
    )
    assert response.status_code == 200
    assert b"Preferencias guardadas" in response.data

    db.session.expire_all()
    stored = db.session.get(Client, client_id)
    assert stored.email_marketing_consent == "opted_in"
    assert stored.whatsapp_marketing_consent == "opted_in"
    assert stored.phone_marketing_consent == "opted_in"
    assert stored.email_marketing_consent_recorded_at is not None
    assert stored.phone_marketing_consent_recorded_at is not None


def test_tenant_public_reject_all_revokes_channels_even_without_contact_values(app):
    from app import Client, Company, db

    app.config["WTF_CSRF_ENABLED"] = False
    company = Company(name="Comercio baja completa", active=True)
    db.session.add(company)
    db.session.flush()
    client_record = Client(
        name="Cliente baja completa",
        email="baja-total@example.test",
        company_id=company.id,
        active=True,
        email_marketing_consent="opted_in",
        phone_marketing_consent="opted_in",
        whatsapp_marketing_consent="opted_in",
        marketing_consent_token="tenant-consent-reject-missing",
    )
    db.session.add(client_record)
    db.session.commit()
    client_id = client_record.id

    response = app.test_client().post(
        "/crm/consent/tenant-consent-reject-missing",
        data={"action": "reject_all"},
    )
    assert response.status_code == 200
    db.session.expire_all()
    stored = db.session.get(Client, client_id)
    assert stored.email_marketing_consent == "opted_out"
    assert stored.phone_marketing_consent == "opted_out"
    assert stored.whatsapp_marketing_consent == "opted_out"
    assert stored.email_marketing_consent_recorded_at is not None
    assert stored.phone_marketing_consent_recorded_at is not None
    assert stored.whatsapp_marketing_consent_recorded_at is not None


def test_editing_client_details_without_consent_fields_preserves_existing_permissions(app):
    from app import Client, Company, User, db

    app.config["WTF_CSRF_ENABLED"] = False
    company = Company(name="Comercio conservar permisos", active=True)
    db.session.add(company)
    db.session.flush()
    user = User(
        username="preserve-consent-admin",
        email="preserve-consent-admin@example.test",
        role="admin",
        active=True,
        company_id=company.id,
    )
    user.set_password("test-password")
    client_record = Client(
        name="Cliente con permiso vigente",
        email="vigente@example.test",
        phone="3625551400",
        whatsapp="5493625551400",
        company_id=company.id,
        active=True,
        email_marketing_consent="opted_in",
        email_marketing_consent_source="in_person",
        email_marketing_consent_evidence="Aceptó en el local.",
        phone_marketing_consent="opted_in",
        phone_marketing_consent_source="in_person",
        phone_marketing_consent_evidence="Aceptó en el local.",
        whatsapp_marketing_consent="opted_in",
        whatsapp_marketing_consent_recorded_at=__import__("datetime").datetime.utcnow(),
        whatsapp_marketing_consent_source="in_person",
        whatsapp_marketing_consent_evidence="Aceptó en el local.",
    )
    db.session.add_all([user, client_record])
    db.session.commit()
    client_id = client_record.id

    http = app.test_client()
    _login_crm_user(http, user)
    response = http.post(
        f"/clientes/edit/{client_id}",
        data={
            "name": "Cliente editado",
            "email": "vigente@example.test",
            "phone": "3625551400",
            "whatsapp": "5493625551400",
            "customer_type": "minorista",
        },
    )
    assert response.status_code == 302
    db.session.expire_all()
    stored = db.session.get(Client, client_id)
    assert stored.name == "Cliente editado"
    assert stored.email_marketing_consent == "opted_in"
    assert stored.phone_marketing_consent == "opted_in"
    assert stored.whatsapp_marketing_consent == "opted_in"
    assert stored.whatsapp_marketing_consent_evidence == "Aceptó en el local."

def test_crm_activity_edit_page_renders_and_updates_followup(app):
    from datetime import datetime
    from app import Client, Company, User, db
    from crm_models import CRMActivity, CRMOpportunity

    app.config["WTF_CSRF_ENABLED"] = False
    company = Company(
        name="CRM Edición de seguimientos",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="crm_activity_edit_admin",
        email="crm-activity-edit@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    client_record = Client(
        name="Cliente seguimiento editable",
        company_id=company.id,
        active=True,
    )
    db.session.add_all([user, client_record])
    db.session.flush()
    opportunity = CRMOpportunity(
        company_id=company.id,
        client_id=client_record.id,
        title="Demo pendiente",
        stage="nuevo",
        status="open",
        value=25000,
        probability=20,
        owner_user_id=user.id,
        created_at=datetime(2026, 1, 1, 10, 0),
        updated_at=datetime(2026, 1, 1, 10, 0),
    )
    db.session.add(opportunity)
    db.session.flush()
    activity = CRMActivity(
        company_id=company.id,
        client_id=client_record.id,
        opportunity_id=opportunity.id,
        user_id=user.id,
        type="seguimiento",
        status="pending",
        subject="Llamar por la demo",
        description="Coordinar horario.",
        due_at=datetime(2030, 1, 10, 9, 0),
        created_at=datetime(2026, 1, 1, 10, 0),
        updated_at=datetime(2026, 1, 1, 10, 0),
    )
    db.session.add(activity)
    db.session.commit()

    http = app.test_client()
    _login_crm_user(http, user)

    page = http.get(f"/crm/activities/{activity.id}/edit")
    assert page.status_code == 200
    assert "Editar seguimiento".encode() in page.data
    assert "Llamar por la demo".encode() in page.data
    assert client_record.name.encode() in page.data

    response = http.post(
        f"/crm/activities/{activity.id}/edit",
        data={
            "client_id": str(client_record.id),
            "opportunity_id": str(opportunity.id),
            "type": "llamada",
            "subject": "Confirmar la demostración",
            "description": "Volver a llamar y coordinar el horario.",
            "due_at": "2030-01-15T10:45",
        },
    )
    assert response.status_code == 302
    db.session.refresh(activity)
    assert activity.client_id == client_record.id
    assert activity.opportunity_id == opportunity.id
    assert activity.type == "llamada"
    assert activity.subject == "Confirmar la demostración"
    assert activity.description == "Volver a llamar y coordinar el horario."
    assert activity.due_at == datetime(2030, 1, 15, 13, 45)
    assert b'value="2030-01-15T10:45"' in http.get(f"/crm/activities/{activity.id}/edit").data
    assert activity.status == "pending"


def test_crm_activity_edit_does_not_expose_another_tenant_activity(app):
    from datetime import datetime
    from app import Client, Company, User, db
    from crm_models import CRMActivity

    app.config["WTF_CSRF_ENABLED"] = False
    own_company = Company(
        name="CRM Empresa propia",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    foreign_company = Company(
        name="CRM Empresa ajena",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add_all([own_company, foreign_company])
    db.session.flush()
    own_user = User(
        username="crm_activity_own_admin",
        email="crm-activity-own@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=own_company.id,
    )
    foreign_client = Client(
        name="Cliente de otra empresa",
        company_id=foreign_company.id,
        active=True,
    )
    db.session.add_all([own_user, foreign_client])
    db.session.flush()
    foreign_activity = CRMActivity(
        company_id=foreign_company.id,
        client_id=foreign_client.id,
        user_id=None,
        type="tarea",
        status="pending",
        subject="Seguimiento privado",
        description=None,
        due_at=None,
        created_at=datetime(2026, 1, 1, 10, 0),
        updated_at=datetime(2026, 1, 1, 10, 0),
    )
    db.session.add(foreign_activity)
    db.session.commit()

    http = app.test_client()
    _login_crm_user(http, own_user)
    response = http.get(f"/crm/activities/{foreign_activity.id}/edit")
    assert response.status_code == 404

def test_crm_sale_cannot_be_linked_to_multiple_opportunities(app):
    from datetime import datetime
    from app import Client, Company, Sale, User, db
    from crm import _resolve_commercial_links
    from crm_models import CRMOpportunity

    company = Company(
        name="CRM Venta única",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="crm_sale_unique_admin",
        email="crm-sale-unique@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    client_record = Client(
        name="Cliente con venta vinculada",
        company_id=company.id,
        active=True,
    )
    db.session.add_all([user, client_record])
    db.session.flush()
    sale = Sale(
        company_id=company.id,
        client_id=client_record.id,
        seller_id=user.id,
        customer=client_record.name,
        status="confirmada",
        subtotal=12500,
        total_amount=12500,
        date=datetime(2026, 1, 1, 10, 0),
    )
    db.session.add(sale)
    db.session.flush()
    linked_opportunity = CRMOpportunity(
        company_id=company.id,
        client_id=client_record.id,
        title="Oportunidad ya vinculada",
        stage="ganado",
        status="closed",
        value=12500,
        probability=100,
        owner_user_id=user.id,
        sale_id=sale.id,
        created_at=datetime(2026, 1, 1, 10, 0),
        updated_at=datetime(2026, 1, 1, 10, 0),
    )
    db.session.add(linked_opportunity)
    db.session.flush()
    # Simulate a legacy duplicate: editing an already-linked opportunity
    # must remain possible even while new duplicate links are blocked.
    legacy_duplicate = CRMOpportunity(
        company_id=company.id,
        client_id=client_record.id,
        title="Vínculo duplicado legado",
        stage="ganado",
        status="closed",
        value=12500,
        probability=100,
        owner_user_id=user.id,
        sale_id=sale.id,
        created_at=datetime(2026, 1, 2, 10, 0),
        updated_at=datetime(2026, 1, 2, 10, 0),
    )
    db.session.add(legacy_duplicate)
    db.session.commit()

    with app.test_request_context("/"):
        login_user(user)
        try:
            _resolve_commercial_links(client_record.id, None, sale.id)
        except ValueError as exc:
            assert "ya está vinculada" in str(exc)
        else:
            raise AssertionError("Se permitió vincular la misma venta a dos oportunidades.")

        quote, resolved_sale = _resolve_commercial_links(
            client_record.id,
            None,
            sale.id,
            opportunity_id=linked_opportunity.id,
        )
        assert quote is None
        assert resolved_sale.id == sale.id

def test_crm_local_datetime_helpers_round_trip_in_company_timezone():
    from datetime import date
    from stockarmobile.helpers.dates import (
        format_local_datetime,
        local_day_bounds_utc_naive,
        parse_local_datetime_to_utc_naive,
    )

    timezone_name = "America/Argentina/Buenos_Aires"
    stored = parse_local_datetime_to_utc_naive("2026-11-24T10:15", timezone_name)
    assert stored == datetime(2026, 11, 24, 13, 15)
    assert format_local_datetime(stored, timezone_name, "%Y-%m-%dT%H:%M") == "2026-11-24T10:15"

    start_utc, end_utc = local_day_bounds_utc_naive(
        date(2026, 11, 24),
        timezone_name,
    )
    assert start_utc == datetime(2026, 11, 24, 3, 0)
    assert end_utc == datetime(2026, 11, 25, 3, 0)


def test_crm_datetime_migration_converts_existing_local_values_once(app):
    import importlib.util
    from pathlib import Path
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from app import Client, Company, User, db
    from crm_models import CRMActivity, CRMOpportunity

    company = Company(
        name="CRM Migración horaria",
        active=True,
        timezone="America/Argentina/Buenos_Aires",
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="crm_migration_timezone",
        email="crm-migration-timezone@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    client_record = Client(
        name="Cliente migración horaria",
        company_id=company.id,
        active=True,
    )
    db.session.add_all([user, client_record])
    db.session.flush()
    opportunity = CRMOpportunity(
        company_id=company.id,
        client_id=client_record.id,
        title="Oportunidad con fecha local",
        stage="nuevo",
        status="open",
        value=1000,
        probability=20,
        owner_user_id=user.id,
        expected_close_date=datetime(2030, 1, 15, 10, 45),
        created_at=datetime(2026, 1, 1, 12, 0),
        updated_at=datetime(2026, 1, 1, 12, 0),
    )
    activity = CRMActivity(
        company_id=company.id,
        client_id=client_record.id,
        user_id=user.id,
        type="tarea",
        status="pending",
        subject="Actividad con fecha local",
        due_at=datetime(2030, 1, 16, 9, 30),
        created_at=datetime(2026, 1, 1, 12, 0),
        updated_at=datetime(2026, 1, 1, 12, 0),
    )
    db.session.add_all([opportunity, activity])
    db.session.commit()

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "20261124_01_crm_datetime_utc.py"
    )
    spec = importlib.util.spec_from_file_location("crm_datetime_utc_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with db.engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    db.session.expire_all()
    assert db.session.get(CRMOpportunity, opportunity.id).expected_close_date == datetime(2030, 1, 15, 13, 45)
    assert db.session.get(CRMActivity, activity.id).due_at == datetime(2030, 1, 16, 12, 30)


def test_crm_pages_paginate_and_selector_search_finds_older_clients(app):
    from app import Client, Company, User, db
    from crm_models import CRMActivity, CRMOpportunity

    company = Company(
        name="CRM Listas paginadas",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="crm_pagination_admin",
        email="crm-pagination@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    db.session.add(user)
    db.session.flush()

    clients = [
        Client(
            name=f"Cliente CRM {index:03d}",
            company_id=company.id,
            active=True,
        )
        for index in range(41)
    ]
    db.session.add_all(clients)
    db.session.flush()
    stamp = datetime(2026, 1, 1, 12, 0)
    opportunities = [
        CRMOpportunity(
            company_id=company.id,
            client_id=clients[index].id,
            title=f"Oportunidad CRM {index:03d}",
            stage="nuevo",
            status="open",
            value=1000,
            probability=10,
            owner_user_id=user.id,
            created_at=stamp,
            updated_at=stamp,
        )
        for index in range(41)
    ]
    activities = [
        CRMActivity(
            company_id=company.id,
            client_id=clients[index].id,
            user_id=user.id,
            type="tarea",
            status="pending",
            subject=f"Seguimiento CRM {index:03d}",
            due_at=None,
            created_at=stamp,
            updated_at=stamp,
        )
        for index in range(31)
    ]
    db.session.add_all([*opportunities, *activities])
    db.session.commit()

    http = app.test_client()
    _login_crm_user(http, user)

    clients_page = http.get("/crm/clients?page=2")
    assert clients_page.status_code == 200
    assert b"Cliente CRM 040" in clients_page.data
    assert b"41 clientes" in clients_page.data

    pipeline_page = http.get("/crm/pipeline?page=2&status=all")
    assert pipeline_page.status_code == 200
    assert b"Oportunidad CRM 000" in pipeline_page.data
    assert b"41 oportunidad(es)" in pipeline_page.data

    activities_page = http.get("/crm/activities?page=2&status=all")
    assert activities_page.status_code == 200
    assert b"Seguimiento CRM 000" in activities_page.data
    assert b"31 registros" in activities_page.data

    searchable_form = http.get("/crm/opportunities/new?lookup=Cliente+CRM+040")
    assert searchable_form.status_code == 200
    assert b"Cliente CRM 040" in searchable_form.data

    owner_search = http.get("/crm/pipeline?q=crm_pagination_admin&status=all")
    assert owner_search.status_code == 200
    assert b"Cliente CRM 040" in owner_search.data

def test_crm_opted_out_whatsapp_requires_confirmation_and_keeps_opt_out(app):
    from datetime import datetime
    from app import Client, Company, User, db
    from crm_models import CRMActivity

    company = Company(
        name="CRM WhatsApp baja",
        active=True,
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="crm_opted_out_admin",
        email="crm-opted-out@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    client_record = Client(
        name="Cliente que rechazó marketing",
        company_id=company.id,
        whatsapp="3624228396",
        phone="3624228396",
        whatsapp_marketing_consent="opted_out",
        whatsapp_marketing_consent_recorded_at=datetime(2026, 1, 1, 12, 0),
        whatsapp_marketing_consent_source="public_consent_link",
        whatsapp_marketing_consent_evidence="El titular solicitó la baja.",
        active=True,
    )
    db.session.add_all([user, client_record])
    db.session.commit()

    http = app.test_client()
    _login_crm_user(http, user)

    first_attempt = http.post(f"/crm/clients/{client_record.id}/contact/whatsapp")
    assert first_attempt.status_code == 302
    assert first_attempt.headers["Location"].endswith("?confirm_whatsapp=1")
    assert CRMActivity.query.filter_by(company_id=company.id).count() == 0
    db.session.refresh(client_record)
    assert client_record.whatsapp_marketing_consent == "opted_out"

    confirmation = http.get(first_attempt.headers["Location"])
    assert confirmation.status_code == 200
    assert "El cliente rechazó los mensajes de marketing".encode() in confirmation.data

    response = http.post(
        f"/crm/clients/{client_record.id}/contact/whatsapp",
        data={"confirm_non_marketing": "1"},
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "https://wa.me/5493624228396"
    db.session.refresh(client_record)
    assert client_record.whatsapp_marketing_consent == "opted_out"
    assert client_record.whatsapp_marketing_consent_source == "public_consent_link"
    assert client_record.whatsapp_marketing_consent_evidence == "El titular solicitó la baja."

    activity = CRMActivity.query.filter_by(company_id=company.id).one()
    assert "opted_out" in activity.description
    assert "comunicación no promocional" in activity.description


def test_crm_opportunity_close_date_round_trips_in_company_local_time(app):
    from datetime import datetime
    from app import Client, Company, User, db
    from crm_models import CRMOpportunity

    company = Company(
        name="CRM Cierre local",
        active=True,
        timezone="America/Argentina/Buenos_Aires",
        preferences_json=json.dumps({"ai_agent": {"plan_code": "pro"}}),
    )
    db.session.add(company)
    db.session.flush()
    user = User(
        username="crm_close_date_admin",
        email="crm-close-date@test.local",
        password_hash="x",
        role="admin",
        active=True,
        company_id=company.id,
    )
    client_record = Client(
        name="Cliente cierre local",
        company_id=company.id,
        active=True,
    )
    db.session.add_all([user, client_record])
    db.session.commit()

    http = app.test_client()
    _login_crm_user(http, user)
    response = http.post(
        "/crm/opportunities/new",
        data={
            "client_id": str(client_record.id),
            "title": "Cierre en horario local",
            "stage": "nuevo",
            "value": "50000",
            "probability": "35",
            "expected_close_date": "2030-01-15T10:45",
            "source": "Prueba",
            "owner_user_id": str(user.id),
            "quote_id": "",
            "sale_id": "",
            "notes": "",
            "loss_reason": "",
        },
    )
    assert response.status_code == 302
    opportunity = CRMOpportunity.query.filter_by(company_id=company.id).one()
    assert opportunity.expected_close_date == datetime(2030, 1, 15, 13, 45)

    edit_page = http.get(f"/crm/opportunities/{opportunity.id}/edit")
    assert edit_page.status_code == 200
    assert b'value="2030-01-15T10:45"' in edit_page.data
    detail_page = http.get(f"/crm/opportunities/{opportunity.id}")
    assert detail_page.status_code == 200
    assert b"15/01/2030 10:45" in detail_page.data

