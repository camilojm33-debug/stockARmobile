import json

import pytest
from sqlalchemy import inspect, text

import app as stock_app
from app import Company, db
from services.ai_agent.config_service import ensure_default_agents


def _drop_migration_only_wholesale_foreign_keys():
    """Let ORM metadata teardown drop the disposable PostgreSQL test schema.

    The migration test verifies physical FKs that aren't represented in the legacy
    bootstrap metadata. Remove those FK constraints only in the test DB before
    db.drop_all(); production migrations and runtime constraints remain untouched.
    """
    if db.engine.dialect.name != "postgresql":
        return
    inspector = inspect(db.engine)
    constraints = (
        ("clients", "fk_clients_price_list_id_price_lists"),
        ("sale_items", "fk_sale_items_price_list_id_price_lists"),
        ("quote_items", "fk_quote_items_price_list_id_price_lists"),
    )
    with db.engine.begin() as connection:
        for table_name, constraint_name in constraints:
            if inspector.has_table(table_name):
                connection.execute(text(
                    f'ALTER TABLE "{table_name}" DROP CONSTRAINT IF EXISTS "{constraint_name}"'
                ))


@pytest.fixture
def app():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False

    with stock_app.app.app_context():
        db.session.remove()
        _drop_migration_only_wholesale_foreign_keys()
        db.drop_all()
        db.create_all()
        yield stock_app.app
        db.session.remove()
        _drop_migration_only_wholesale_foreign_keys()
        db.drop_all()


@pytest.fixture
def qa_ai_database():
    """Minimal isolated AI database used by cross-module vendor security tests."""
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False
    with stock_app.app.app_context():
        db.session.remove()
        _drop_migration_only_wholesale_foreign_keys()
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
        _drop_migration_only_wholesale_foreign_keys()
        db.drop_all()
