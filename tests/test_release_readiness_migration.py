from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20261024_01_release_readiness.py"
)


def _load_migration():
    spec = spec_from_file_location("release_readiness_migration", MIGRATION_PATH)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(connection):
    migration = _load_migration()
    migration.op = Operations(MigrationContext.configure(connection))
    migration.upgrade()


def test_release_readiness_migration_creates_missing_tables_idempotently():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY)"))

        _run_upgrade(connection)
        _run_upgrade(connection)

        inspector = inspect(connection)
        assert "webhook_events" in inspector.get_table_names()
        assert "product_modifications" in inspector.get_table_names()
        assert {i["name"] for i in inspector.get_indexes("webhook_events")} >= {
            "ix_webhook_events_event_key",
            "ix_webhook_events_status",
            "ix_webhook_events_created_at",
        }
        assert "ix_product_modifications_created_at" in {
            i["name"] for i in inspector.get_indexes("product_modifications")
        }


def test_release_readiness_migration_preserves_existing_webhook_rows():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE companies (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY)"))
        connection.execute(text(
            "CREATE TABLE webhook_events (id INTEGER PRIMARY KEY, provider VARCHAR(40) NOT NULL, "
            "event_key VARCHAR(180) NOT NULL, event_type VARCHAR(80) NOT NULL, status VARCHAR(30), "
            "payload_json TEXT, created_at DATETIME)"
        ))
        connection.execute(text(
            "INSERT INTO webhook_events (id, provider, event_key, event_type, status) "
            "VALUES (1, 'mercadopago', 'qa-existing-event', 'payment', 'processed')"
        ))

        _run_upgrade(connection)

        row = connection.execute(
            text("SELECT event_key, status FROM webhook_events WHERE id = 1")
        ).one()
        assert row == ("qa-existing-event", "processed")
        assert "product_modifications" in inspect(connection).get_table_names()
