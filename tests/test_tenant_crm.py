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
    assert source_text.count("@tenant_required\n@ai_pro_crm_required") == 12


def test_tenant_crm_menu_is_plan_gated():
    template = open("templates/base_master.html", encoding="utf-8").read()
    assert "ai_feature_crm_allowed and (current_user.role == 'admin' or current_user_has_permission('crm'))" in template
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
