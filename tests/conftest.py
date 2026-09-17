import json

import pytest

import app as stock_app
from app import Company, db
from services.ai_agent.config_service import ensure_default_agents


@pytest.fixture
def app():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False

    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()
        yield stock_app.app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def qa_ai_database():
    """Minimal isolated AI database used by cross-module vendor security tests."""
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False
    with stock_app.app.app_context():
        db.drop_all()
        db.create_all()
        company = Company(
            name="Empresa QA Vendedor",
            active=True,
            preferences_json=json.dumps({"ai_agent": {"plan_code": "inicio"}}),
        )
        db.session.add(company)
        db.session.flush()
        ensure_default_agents(company.id)
        db.session.commit()
        yield {"companies": {"vendedor": company}}
        db.session.rollback()
        db.session.remove()
        db.drop_all()
