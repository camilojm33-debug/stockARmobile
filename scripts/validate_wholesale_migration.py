"""Validate the new wholesale-pricing migration against a disposable PostgreSQL schema.

This CI-only script avoids replaying older legacy migrations that bootstrap current ORM
columns and then try to add some of those same columns again. It constructs a schema
equivalent to the pre-wholesale state, stamps that revision, and runs the new migration.
"""

from __future__ import annotations

import os
from sqlalchemy import inspect, text

from app import Plan, app, db
from flask_migrate import stamp, upgrade


BASE_REVISION = "20261024_01_release_readiness"
HEAD_REVISION = "20261026_02_wholesale_disc_snap"


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        raise SystemExit("This destructive schema-validation script is restricted to an isolated GitHub Actions database.")

    with app.app_context():
        # CI supplies a disposable PostgreSQL service container. Start from an empty
        # schema, create the current model shape, then remove only fields introduced
        # by wholesale pricing to simulate the schema immediately before the migration.
        with db.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
        db.drop_all()
        db.create_all()

        # Emulate an existing Negocio plan so the data migration is validated,
        # not merely the schema revision.
        db.session.add(Plan(
            code="business",
            name="Negocio",
            price=29999,
            currency="ARS",
            duration_days=30,
            max_users=8,
            max_products=12000,
            max_clients=12000,
            features_json="inventario,ventas,clientes,compras,caja,reportes,excel,kardex,pricing_controller,promotions",
            state="active",
            active=True,
        ))
        db.session.commit()

        with db.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS price_list_items CASCADE"))
            connection.execute(text("DROP TABLE IF EXISTS price_lists CASCADE"))
            for table_name, columns in (
                ("clients", ("customer_type", "price_list_id")),
                ("sale_items", ("price_source", "price_list_id", "wholesale_discount_percent", "wholesale_reference_price")),
                ("quote_items", ("price_source", "price_list_id", "wholesale_discount_percent", "wholesale_reference_price")),
            ):
                for column_name in columns:
                    connection.execute(
                        text(f'ALTER TABLE "{table_name}" DROP COLUMN IF EXISTS "{column_name}" CASCADE')
                    )
            connection.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))

        db.session.remove()
        stamp(directory="migrations", revision=BASE_REVISION)
        upgrade(directory="migrations", revision="head")
        db.session.remove()

        inspector = inspect(db.engine)
        expected_columns = {
            "clients": {"customer_type", "price_list_id"},
            "sale_items": {"price_source", "price_list_id", "wholesale_discount_percent", "wholesale_reference_price"},
            "quote_items": {"price_source", "price_list_id", "wholesale_discount_percent", "wholesale_reference_price"},
            "price_list_items": {"discount_percent"},
        }
        for table_name, expected in expected_columns.items():
            present = {column["name"] for column in inspector.get_columns(table_name)}
            missing = expected - present
            if missing:
                raise AssertionError(f"{table_name} missing columns after migration: {sorted(missing)}")

        expected_tables = {"price_lists", "price_list_items"}
        missing_tables = {name for name in expected_tables if not inspector.has_table(name)}
        if missing_tables:
            raise AssertionError(f"Missing wholesale tables after migration: {sorted(missing_tables)}")

        expected_foreign_keys = {
            "clients": "fk_clients_price_list_id_price_lists",
            "sale_items": "fk_sale_items_price_list_id_price_lists",
            "quote_items": "fk_quote_items_price_list_id_price_lists",
        }
        for table_name, fk_name in expected_foreign_keys.items():
            present = {item.get("name") for item in inspector.get_foreign_keys(table_name)}
            if fk_name not in present:
                raise AssertionError(f"{table_name} missing FK {fk_name}")

        version = db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if version != HEAD_REVISION:
            raise AssertionError(f"Unexpected Alembic revision after upgrade: {version}")

        business_features = db.session.execute(
            text("SELECT features_json FROM plans WHERE code = :code"),
            {"code": "business"},
        ).scalar_one()
        feature_tokens = {
            item.strip().lower()
            for item in str(business_features or "").replace(";", ",").split(",")
            if item.strip()
        }
        if "wholesale_pricing" not in feature_tokens:
            raise AssertionError("Existing Negocio plan did not receive wholesale_pricing entitlement.")
        for expected in ("pricing_controller", "promotions", "inventario", "ventas"):
            if expected not in feature_tokens:
                raise AssertionError(f"Existing Negocio plan lost expected feature: {expected}")

        print("PASS: wholesale-pricing, percentage-discount, sale/quote snapshot, and Negocio-plan migrations upgraded the pre-feature PostgreSQL schema.")
        print("PASS: wholesale tables, percentage discounts, sale/quote discount snapshots, customer assignment, foreign keys, and latest Alembic revision exist.")
        print("PASS: existing Negocio plan gains wholesale_pricing while preserving other commercial features.")


if __name__ == "__main__":
    main()
