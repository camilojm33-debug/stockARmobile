import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url


def _configure_isolated_test_database():
    """Never let destructive pytest fixtures touch a developer or production database."""
    if "pytest" not in sys.modules and not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if os.environ.get("RENDER") or os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError("Las pruebas no pueden ejecutarse dentro de un entorno Render/producción.")
    raw_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not raw_url:
        target = Path(tempfile.gettempdir()) / f"stockarmobile-pytest-{os.getpid()}.db"
        os.environ["DATABASE_URL"] = "sqlite:////" + str(target).lstrip("/")
        return
    parsed = make_url(raw_url)
    if parsed.get_backend_name() == "sqlite":
        database = parsed.database
        if database in (None, "", ":memory:"):
            return
        path = Path(database)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        try:
            path.relative_to(temp_root)
            safe = True
        except ValueError:
            safe = False
        if not safe:
            target = temp_root / f"stockarmobile-pytest-{os.getpid()}.db"
            os.environ["DATABASE_URL"] = "sqlite:////" + str(target).lstrip("/")
        return
    if parsed.get_backend_name() in {"postgresql", "postgres"}:
        host = (parsed.host or "").lower()
        database = (parsed.database or "").lower()
        if host not in {"localhost", "127.0.0.1", "::1"} or "test" not in database:
            raise RuntimeError("DATABASE_URL de pytest debe apuntar a PostgreSQL local con nombre de base de pruebas.")
        return
    raise RuntimeError("DATABASE_URL no permitido para pytest; use SQLite temporal o PostgreSQL local de pruebas.")


_configure_isolated_test_database()

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
def company_membership_app():
    """Give tenant-switch tests normal, independent Flask contexts per request."""
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False

    with stock_app.app.app_context():
        db.session.remove()
        _drop_migration_only_wholesale_foreign_keys()
        db.drop_all()
        db.create_all()

    yield stock_app.app

    with stock_app.app.app_context():
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
