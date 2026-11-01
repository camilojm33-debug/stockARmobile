"""Validate the new wholesale-pricing migration against a disposable PostgreSQL schema.

This CI-only script avoids replaying older legacy migrations that bootstrap current ORM
columns and then try to add some of those same columns again. It constructs a schema
equivalent to the pre-wholesale state, stamps that revision, and runs the new migration.
"""

from __future__ import annotations

import os
from sqlalchemy import inspect, text

from app import Company, Plan, Product, User, app, db
from flask_migrate import stamp, upgrade


BASE_REVISION = "20261024_01_release_readiness"
HEAD_REVISION = "20261101_01_multibranch"


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
            max_branches=1,
            features_json="inventario,ventas,clientes,compras,caja,reportes,excel,kardex,pricing_controller,promotions",
            state="active",
            active=True,
        ))
        db.session.add(Plan(
            code="premium", name="Premium", price=54999, currency="ARS",
            duration_days=30, max_users=50, max_products=100000,
            max_clients=100000, max_branches=5, features_json="all",
            state="active", active=True,
        ))
        company = Company(name="Empresa migración", address="Calle 123", city="Resistencia", province="Chaco", phone="123")
        db.session.add(company)
        db.session.flush()
        user = User(username="migration-admin", email="migration-admin@example.test",
                    password_hash="test-hash", role="admin", company_id=company.id)
        product = Product(barcode="MIG-001", name="Producto migración", stock=17, min_stock=3,
                          active=True, company_id=company.id)
        db.session.add_all([user, product])
        db.session.commit()
        expected_company_id = company.id
        expected_user_id = user.id
        expected_product_id = product.id

        with db.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS price_list_discount_rules CASCADE"))
            connection.execute(text("DROP TABLE IF EXISTS price_list_items CASCADE"))
            connection.execute(text("DROP TABLE IF EXISTS price_lists CASCADE"))
            # Remove tables/columns introduced by multisucursal so the revision must
            # create them as it would on an existing production tenant.
            for table_name in ("branch_stock_transfers", "branch_stock_movements", "branch_stock", "user_branch_access", "branches"):
                connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
            for table_name in ("sales", "purchase_orders", "cash_sessions", "cash_movements", "expenses", "business_documents"):
                connection.execute(text(f'ALTER TABLE "{table_name}" DROP COLUMN IF EXISTS "branch_id" CASCADE'))
            connection.execute(text('ALTER TABLE "plans" DROP COLUMN IF EXISTS "max_branches" CASCADE'))
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
            "quote_deliveries": {"pickup_status", "picked_up_at", "picked_up_by_user_id"},
            "sales": {"branch_id"},
            "purchase_orders": {"branch_id"},
            "cash_sessions": {"branch_id"},
            "cash_movements": {"branch_id"},
            "expenses": {"branch_id"},
            "business_documents": {"branch_id"},
            "plans": {"max_branches"},
        }
        for table_name, expected in expected_columns.items():
            present = {column["name"] for column in inspector.get_columns(table_name)}
            missing = expected - present
            if missing:
                raise AssertionError(f"{table_name} missing columns after migration: {sorted(missing)}")

        expected_tables = {"price_lists", "price_list_items", "price_list_discount_rules", "branches", "user_branch_access", "branch_stock", "branch_stock_movements", "branch_stock_transfers"}
        missing_tables = {name for name in expected_tables if not inspector.has_table(name)}
        if missing_tables:
            raise AssertionError(f"Missing wholesale tables after migration: {sorted(missing_tables)}")

        expected_foreign_keys = {
            "clients": "fk_clients_price_list_id_price_lists",
            "sale_items": "fk_sale_items_price_list_id_price_lists",
            "quote_items": "fk_quote_items_price_list_id_price_lists",
            "quote_deliveries": "fk_quote_deliveries_picked_up_by_user_id_users",
        }
        for table_name, fk_name in expected_foreign_keys.items():
            present = {item.get("name") for item in inspector.get_foreign_keys(table_name)}
            if fk_name not in present:
                raise AssertionError(f"{table_name} missing FK {fk_name}")

        version = db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if version != HEAD_REVISION:
            raise AssertionError(f"Unexpected Alembic revision after upgrade: {version}")

        transfer_columns = {column["name"] for column in inspector.get_columns("branch_stock_transfers")}
        if not {"source_branch_id", "target_branch_id", "product_id", "quantity", "created_by_user_id"}.issubset(transfer_columns):
            raise AssertionError("Branch stock transfer table is missing required fields.")
        migrated_branch = db.session.execute(
            text("SELECT id FROM branches WHERE company_id = :company_id AND is_default = TRUE"),
            {"company_id": expected_company_id},
        ).scalar_one()
        migrated_access = db.session.execute(
            text("SELECT count(*) FROM user_branch_access WHERE company_id = :company_id AND user_id = :user_id AND branch_id = :branch_id"),
            {"company_id": expected_company_id, "user_id": expected_user_id, "branch_id": migrated_branch},
        ).scalar_one()
        migrated_stock = db.session.execute(
            text("SELECT stock FROM branch_stock WHERE company_id = :company_id AND branch_id = :branch_id AND product_id = :product_id"),
            {"company_id": expected_company_id, "branch_id": migrated_branch, "product_id": expected_product_id},
        ).scalar_one()
        if int(migrated_access) != 1 or float(migrated_stock) != 17.0:
            raise AssertionError("Existing employee access or product stock was not migrated to Casa central.")
        premium_limit = db.session.execute(
            text("SELECT max_branches FROM plans WHERE code = 'premium'")
        ).scalar_one()
        if int(premium_limit) != 5:
            raise AssertionError(f"Premium max_branches should be 5, got {premium_limit}")

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

        pickup_statuses = db.session.execute(text("SELECT DISTINCT pickup_status FROM quote_deliveries")).scalars().all()
        if any(status == "picked_up" for status in pickup_statuses):
            raise AssertionError("Legacy quote deliveries were incorrectly classified as picked up.")

        print("PASS: wholesale, pickup-tracking and multisucursal migrations upgraded the pre-feature PostgreSQL schema.")
        print("PASS: wholesale tables, general discount rules, percentage discounts, sale/quote discount snapshots, customer assignment, foreign keys, and latest Alembic revision exist.")
        print("PASS: existing Negocio plan gains wholesale_pricing; Premium keeps a configurable limit of five branches.")
        print("PASS: existing tenants, users and product stock are migrated to Casa central.")


if __name__ == "__main__":
    main()
