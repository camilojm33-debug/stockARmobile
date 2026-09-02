import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("MP_OAUTH_ENCRYPTION_KEY", "test-oauth-encryption-key")

import pytest

import app as stock_app
import wsgi  # noqa: F401
from app import Company, User, db
from services.company_security_service import CompanySecurityService
from stockarmobile.models.conversations import Agent, AgentConfiguration


@pytest.fixture
def company_access_data():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False

    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()

        company_a = Company(name="Empresa Agentes A", active=True)
        company_b = Company(name="Empresa Agentes B", active=True)
        db.session.add_all([company_a, company_b])
        db.session.flush()

        admin_a = User(
            username="agentes_admin_a",
            email="agentes_admin_a@test.local",
            role="admin",
            active=True,
            company_id=company_a.id,
        )
        admin_a.set_password("admin123")
        admin_b = User(
            username="agentes_admin_b",
            email="agentes_admin_b@test.local",
            role="admin",
            active=True,
            company_id=company_b.id,
        )
        admin_b.set_password("admin123")
        CompanySecurityService.set_pin(company_a, "1234")
        CompanySecurityService.set_pin(company_b, "5678")
        db.session.add_all([admin_a, admin_b])
        db.session.commit()

        yield {
            "company_a": company_a,
            "company_b": company_b,
            "admin_a": admin_a,
            "admin_b": admin_b,
        }

        db.session.remove()
        db.drop_all()


def _login(client, username):
    response = client.post("/auth/login", data={"username": username, "password": "admin123"})
    assert response.status_code in {302, 303}


def _verify_pin_for(client, company_id):
    with client.session_transaction() as session:
        session[f"company_pin_verified_{company_id}"] = stock_app.utcnow().timestamp()


def test_ai_agent_tile_requires_company_pin(company_access_data):
    data = company_access_data
    client = stock_app.app.test_client()
    _login(client, data["admin_a"].username)

    locked = client.get("/admin/company-settings")
    assert locked.status_code == 200
    assert "Agentes IA" not in locked.data.decode("utf-8")

    _verify_pin_for(client, data["company_a"].id)
    unlocked = client.get("/admin/company-settings")
    html = unlocked.data.decode("utf-8")
    assert unlocked.status_code == 200
    assert "Agentes IA" in html
    assert 'href="/dashboard/ai-agent"' in html


def test_ai_agent_route_requires_pin_for_active_company(company_access_data):
    data = company_access_data
    client = stock_app.app.test_client()
    _login(client, data["admin_a"].username)

    blocked = client.get("/dashboard/ai-agent", follow_redirects=False)
    assert blocked.status_code in {301, 302, 303}
    assert blocked.headers["Location"].endswith("/admin/company-settings")

    _verify_pin_for(client, data["company_a"].id)
    allowed = client.get("/dashboard/ai-agent")
    html = allowed.data.decode("utf-8")
    assert allowed.status_code == 200
    assert "Vendedor 24 hs" in html
    assert "Asistente empresarial" in html


def test_ai_agent_pin_is_isolated_by_company_and_save_remains_available(company_access_data):
    data = company_access_data
    client = stock_app.app.test_client()
    _login(client, data["admin_a"].username)
    _verify_pin_for(client, data["company_a"].id)

    _login(client, data["admin_b"].username)
    blocked = client.get("/dashboard/ai-agent", follow_redirects=False)
    assert blocked.status_code in {301, 302, 303}
    assert blocked.headers["Location"].endswith("/admin/company-settings")

    _verify_pin_for(client, data["company_b"].id)
    saved = client.post(
        "/dashboard/ai-agent/save",
        data={
            "ai_enabled": "1",
            "vendor_enabled": "1",
            "vendor_model": "vendor-test-model",
            "vendor_language": "es-AR",
            "vendor_max_tokens": "512",
            "vendor_prompt": "Atendé ventas.",
            "vendor_temperature": "0.20",
            "business_enabled": "1",
            "business_model": "business-test-model",
            "business_language": "es-AR",
            "business_max_tokens": "640",
            "business_prompt": "Mostrá métricas.",
            "business_temperature": "0.30",
            "whatsapp_enabled": "0",
        },
        follow_redirects=False,
    )

    assert saved.status_code in {301, 302, 303}
    assert saved.headers["Location"].endswith("/dashboard/ai-agent")
    with stock_app.app.app_context():
        agents = Agent.query.filter_by(company_id=data["company_b"].id).all()
        names = {agent.name for agent in agents}
        configs = AgentConfiguration.query.filter_by(company_id=data["company_b"].id).all()
        assert names == {"Vendedor 24 hs", "Asistente empresarial"}
        assert {config.model for config in configs} == {"vendor-test-model", "business-test-model"}
