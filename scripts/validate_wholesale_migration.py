"""Validate the new wholesale-pricing migration against a disposable PostgreSQL schema.

This CI-only script avoids replaying older legacy migrations that bootstrap current ORM
columns and then try to add some of those same columns again. It constructs a schema
equivalent to the pre-wholesale state, stamps that revision, and runs the new migration.
"""

from __future__ import annotations

import faulthandler
import importlib.util
import os
import threading
import time
from sqlalchemy import inspect, text
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from app import Company, Plan, Product, User, app, db
from flask_migrate import stamp, upgrade


BASE_REVISION = "20261024_01_release_readiness"
LEGACY_MULTIBRANCH_REVISION = "20261101_01_multibranch"
HEAD_REVISION = "20261106_01_branch_vendor"


_VALIDATION_STARTED_AT = time.monotonic()


def _progress(message: str) -> None:
    elapsed = time.monotonic() - _VALIDATION_STARTED_AT
    print(f"[postgres-validation +{elapsed:0.1f}s] {message}", flush=True)


def main() -> None:
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        raise SystemExit("This destructive schema-validation script is restricted to an isolated GitHub Actions database.")

    _progress("BEGIN: isolated PostgreSQL migration validation")
    with app.app_context():
        _progress("Connected to the disposable PostgreSQL database")
        # CI supplies a disposable PostgreSQL service container. Start from an empty
        # schema, create the current model shape, then remove only fields introduced
        # by wholesale pricing to simulate the schema immediately before the migration.
        _progress("Resetting disposable schema: dropping Alembic version table")
        with db.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
        _progress("Dropping ORM tables")
        db.drop_all()
        _progress("Creating current ORM schema")
        db.create_all()
        _progress("Current ORM schema created")

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
        db.session.flush()
        # Capture primary keys before commit expires ORM instances and a later
        # attribute read opens a new transaction that can lock the tables during DDL.
        expected_company_id = company.id
        expected_user_id = user.id
        expected_product_id = product.id
        db.session.commit()
        db.session.remove()
        _progress("Seeded representative company, Premium/Negocio plans, user and product; ORM session closed")

        _progress("Rewinding disposable schema to the pre-feature state")
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
            connection.execute(text("DROP TABLE IF EXISTS company_memberships CASCADE"))
            # Rewind branch Vendor IA too, so the latest migration must create its
            # settings and license tables on the simulated pre-feature schema.
            connection.execute(text("DROP TABLE IF EXISTS branch_vendor_licenses CASCADE"))
            connection.execute(text("DROP TABLE IF EXISTS saas_settings CASCADE"))
            connection.execute(text('ALTER TABLE "conversations" DROP COLUMN IF EXISTS "branch_id" CASCADE'))
            connection.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))

            connection.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
        _progress("Schema rewind complete")

        db.session.remove()
        _progress(f"Stamping baseline revision {BASE_REVISION}")
        stamp(directory="migrations", revision=BASE_REVISION)
        _progress(f"Applying already-released multisucursal revision {LEGACY_MULTIBRANCH_REVISION}")
        upgrade(directory="migrations", revision=LEGACY_MULTIBRANCH_REVISION)
        db.session.remove()

        # Simulate a real sale recorded after the first multisucursal migration but
        # before the additive opening-ledger migration is deployed.
        with db.engine.begin() as connection:
            migrated_branch_id = connection.execute(
                text("SELECT id FROM branches WHERE company_id=:company_id AND is_default=TRUE"),
                {"company_id": expected_company_id},
            ).scalar_one()
            connection.execute(
                text(
                    "UPDATE branch_stock SET stock=14.000 "
                    "WHERE company_id=:company_id AND branch_id=:branch_id AND product_id=:product_id"
                ),
                {
                    "company_id": expected_company_id,
                    "branch_id": migrated_branch_id,
                    "product_id": expected_product_id,
                },
            )
            connection.execute(
                text("UPDATE products SET stock=14.0 WHERE company_id=:company_id AND id=:product_id"),
                {"company_id": expected_company_id, "product_id": expected_product_id},
            )
            connection.execute(
                text(
                    "INSERT INTO branch_stock_movements("
                    "company_id,branch_id,product_id,movement_type,quantity_delta,stock_after,"
                    "source_type,source_id,user_id,note,created_at"
                    ") VALUES(:company_id,:branch_id,:product_id,'sale',-3.000,14.000,"
                    "'sale',9001,:user_id,'Venta posterior a la migración original',CURRENT_TIMESTAMP)"
                ),
                {
                    "company_id": expected_company_id,
                    "branch_id": migrated_branch_id,
                    "product_id": expected_product_id,
                    "user_id": expected_user_id,
                },
            )

        # Seed an already-existing non-default branch with a post-release movement.
        # The additive Kardex migration must backfill every branch, not only Casa central.
        with db.engine.begin() as connection:
            secondary_branch_id = connection.execute(
                text(
                    "INSERT INTO branches(company_id,name,code,active,is_default,created_at,updated_at) "
                    "VALUES(:company_id,'Sucursal Norte','NORTE',TRUE,FALSE,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
                    "RETURNING id"
                ),
                {"company_id": expected_company_id},
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO branch_stock(company_id,branch_id,product_id,stock,min_stock,reserved_stock,updated_at) "
                    "VALUES(:company_id,:branch_id,:product_id,6.000,1.000,0.000,CURRENT_TIMESTAMP)"
                ),
                {"company_id": expected_company_id, "branch_id": secondary_branch_id, "product_id": expected_product_id},
            )
            connection.execute(
                text(
                    "INSERT INTO branch_stock_movements("
                    "company_id,branch_id,product_id,movement_type,quantity_delta,stock_after,"
                    "source_type,source_id,user_id,note,created_at"
                    ") VALUES(:company_id,:branch_id,:product_id,'sale',-2.000,6.000,"
                    "'sale',9002,:user_id,'Venta en sucursal secundaria',CURRENT_TIMESTAMP)"
                ),
                {
                    "company_id": expected_company_id,
                    "branch_id": secondary_branch_id,
                    "product_id": expected_product_id,
                    "user_id": expected_user_id,
                },
            )

        _progress(f"Applying additive opening-ledger revision {HEAD_REVISION}")
        upgrade(directory="migrations", revision="head")
        _progress("Alembic upgrade returned successfully")
        db.session.remove()

        # Re-run the migration function directly against the same schema to prove
        # its data backfill is idempotent, not merely protected by alembic_version.
        migration_path = os.path.join(os.path.dirname(__file__), "..", "migrations", "versions", "20261102_01_multibranch_opening.py")
        migration_spec = importlib.util.spec_from_file_location("multibranch_opening_migration", migration_path)
        migration_module = importlib.util.module_from_spec(migration_spec)
        migration_spec.loader.exec_module(migration_module)
        with db.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration_module.upgrade()
        _progress("Direct second execution of opening-ledger migration completed")

        _progress("Inspecting migrated schema, columns, tables and constraints")
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
            "conversations": {"branch_id"},
            "branches": {"vendor_ai_json"},
            "branch_vendor_licenses": {
                "company_id", "branch_id", "status", "payment_mode", "amount",
                "currency", "starts_at", "ends_at", "preapproval_id",
                "external_reference", "auto_renew", "cancel_at_period_end",
                "last_payment_id", "last_payment_at",
            },
            "saas_settings": {"key", "value", "updated_at", "updated_by_user_id"},
        }
        for table_name, expected in expected_columns.items():
            present = {column["name"] for column in inspector.get_columns(table_name)}
            missing = expected - present
            if missing:
                raise AssertionError(f"{table_name} missing columns after migration: {sorted(missing)}")

        expected_tables = {
            "price_lists", "price_list_items", "price_list_discount_rules",
            "branches", "user_branch_access", "branch_stock", "branch_stock_movements",
            "branch_stock_transfers", "company_memberships", "branch_vendor_licenses",
            "saas_settings",
        }
        missing_tables = {name for name in expected_tables if not inspector.has_table(name)}
        if missing_tables:
            raise AssertionError(f"Missing wholesale tables after migration: {sorted(missing_tables)}")

        expected_foreign_keys = {
            "clients": "fk_clients_price_list_id_price_lists",
            "sale_items": "fk_sale_items_price_list_id_price_lists",
            "quote_items": "fk_quote_items_price_list_id_price_lists",
            "quote_deliveries": "fk_quote_deliveries_picked_up_by_user_id_users",
            "quotes": "fk_quotes_branch_id_branches",
        }
        for table_name, fk_name in expected_foreign_keys.items():
            present = {item.get("name") for item in inspector.get_foreign_keys(table_name)}
            if fk_name not in present:
                raise AssertionError(f"{table_name} missing FK {fk_name}")

        version = db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if version != HEAD_REVISION:
            raise AssertionError(f"Unexpected Alembic revision after upgrade: {version}")

        license_indexes = {item.get("name") for item in inspector.get_indexes("branch_vendor_licenses")}
        required_license_indexes = {
            "ix_branch_vendor_licenses_company_id",
            "ix_branch_vendor_licenses_branch_id",
            "ix_branch_vendor_licenses_company_status",
            "ix_branch_vendor_licenses_preapproval_id",
            "ix_branch_vendor_licenses_external_reference",
            "ix_branch_vendor_licenses_last_payment_id",
        }
        if not required_license_indexes.issubset(license_indexes):
            raise AssertionError(
                f"Branch Vendor IA license indexes missing: {sorted(required_license_indexes - license_indexes)}"
            )
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
        secondary_opening = db.session.execute(
            text(
                "SELECT quantity_delta,stock_after FROM branch_stock_movements "
                "WHERE company_id=:company_id AND branch_id=:branch_id AND product_id=:product_id "
                "AND movement_type='opening' AND source_type='multibranch_opening_backfill'"
            ),
            {"company_id": expected_company_id, "branch_id": secondary_branch_id, "product_id": expected_product_id},
        ).one_or_none()
        secondary_movement_total = db.session.execute(
            text(
                "SELECT COALESCE(SUM(quantity_delta),0) FROM branch_stock_movements "
                "WHERE company_id=:company_id AND branch_id=:branch_id AND product_id=:product_id"
            ),
            {"company_id": expected_company_id, "branch_id": secondary_branch_id, "product_id": expected_product_id},
        ).scalar_one()
        opening_count = db.session.execute(
            text(
                "SELECT count(*) FROM branch_stock_movements "
                "WHERE company_id=:company_id AND product_id=:product_id "
                "AND movement_type='opening' AND source_type='multibranch_opening_backfill'"
            ),
            {"company_id": expected_company_id, "product_id": expected_product_id},
        ).scalar_one()
        migrated_opening = db.session.execute(
            text(
                "SELECT quantity_delta,stock_after FROM branch_stock_movements "
                "WHERE company_id=:company_id AND branch_id=:branch_id AND product_id=:product_id "
                "AND movement_type='opening' AND source_type='multibranch_opening_backfill'"
            ),
            {"company_id": expected_company_id, "branch_id": migrated_branch, "product_id": expected_product_id},
        ).one_or_none()
        migrated_company_stock = db.session.execute(
            text("SELECT stock FROM products WHERE company_id=:company_id AND id=:product_id"),
            {"company_id": expected_company_id, "product_id": expected_product_id},
        ).scalar_one()
        movement_total = db.session.execute(
            text(
                "SELECT COALESCE(SUM(quantity_delta),0) FROM branch_stock_movements "
                "WHERE company_id=:company_id AND branch_id=:branch_id AND product_id=:product_id"
            ),
            {"company_id": expected_company_id, "branch_id": migrated_branch, "product_id": expected_product_id},
        ).scalar_one()
        if int(migrated_access) != 1 or float(migrated_stock) != 14.0:
            raise AssertionError("Existing employee access or product stock was not migrated to Casa central.")
        if float(migrated_company_stock) != 20.0 or float(movement_total) != 14.0 or float(secondary_movement_total) != 6.0:
            raise AssertionError("The consolidated stock does not reconcile with every branch movement ledger.")
        if migrated_opening is None or float(migrated_opening.quantity_delta) != 17.0 or float(migrated_opening.stock_after) != 17.0:
            raise AssertionError("Casa central opening balance was not reconstructed before post-migration movements.")
        if secondary_opening is None or float(secondary_opening.quantity_delta) != 8.0 or float(secondary_opening.stock_after) != 8.0:
            raise AssertionError("The non-default branch opening balance was not reconstructed.")
        if int(opening_count) != 2:
            raise AssertionError(f"Opening migration is not idempotent; expected two opening movements, got {opening_count}.")
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

        _progress("Checking company row lock serializes concurrent branch-limit operations")
        from branches import _lock_company_for_branch_limit
        first_acquired = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_acquired = threading.Event()
        lock_errors = []

        def _hold_company_lock():
            with app.app_context():
                try:
                    _lock_company_for_branch_limit(expected_company_id)
                    first_acquired.set()
                    if not release_first.wait(10):
                        raise AssertionError("Timed out while holding the company row lock.")
                    db.session.rollback()
                except Exception as exc:
                    lock_errors.append(exc)
                    db.session.rollback()
                finally:
                    db.session.remove()

        def _wait_for_company_lock():
            with app.app_context():
                try:
                    second_started.set()
                    _lock_company_for_branch_limit(expected_company_id)
                    second_acquired.set()
                    db.session.rollback()
                except Exception as exc:
                    lock_errors.append(exc)
                    db.session.rollback()
                finally:
                    db.session.remove()

        first_thread = threading.Thread(target=_hold_company_lock, daemon=True)
        second_thread = threading.Thread(target=_wait_for_company_lock, daemon=True)
        first_thread.start()
        if not first_acquired.wait(5):
            release_first.set()
            raise AssertionError("First branch-limit operation did not acquire the company row lock.")
        second_thread.start()
        if not second_started.wait(5):
            release_first.set()
            raise AssertionError("Second branch-limit operation did not start.")
        try:
            if second_acquired.wait(0.35):
                raise AssertionError("A concurrent branch-limit operation bypassed the company row lock.")
        finally:
            release_first.set()
        first_thread.join(5)
        second_thread.join(5)
        if first_thread.is_alive() or second_thread.is_alive():
            raise AssertionError("Concurrent company row-lock test did not finish.")
        if lock_errors:
            raise AssertionError(f"Company row-lock concurrency test failed: {lock_errors!r}")
        if not second_acquired.is_set():
            raise AssertionError("Second branch-limit operation did not proceed after the first transaction released its lock.")

        _progress("All schema and data assertions passed")
        print("PASS: wholesale, pickup-tracking and multisucursal migrations upgraded the pre-feature PostgreSQL schema.", flush=True)
        print("PASS: wholesale tables, general discount rules, percentage discounts, sale/quote discount snapshots, customer assignment, foreign keys, and latest Alembic revision exist.", flush=True)
        print("PASS: existing Negocio plan gains wholesale_pricing; Premium keeps a configurable limit of five branches.", flush=True)
        print("PASS: existing tenants, users and product stock are migrated to Casa central.", flush=True)
        print("PASS: branch Vendor IA configuration, license and SaaS pricing tables are created with required indexes.", flush=True)
        print("PASS: every existing branch balance receives exactly one idempotent opening movement in the branch Kardex.", flush=True)
        print("PASS: concurrent branch-limit operations serialize on the company row lock.", flush=True)
        _progress("END: PostgreSQL migration validation passed")


if __name__ == "__main__":
    # Periodically emit Python stacks if a PostgreSQL operation stalls.
    faulthandler.enable()
    faulthandler.dump_traceback_later(90, repeat=True)
    try:
        main()
    finally:
        faulthandler.cancel_dump_traceback_later()
