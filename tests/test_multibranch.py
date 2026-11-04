from decimal import Decimal
from datetime import datetime, timedelta
import json

import pytest
from werkzeug.exceptions import NotFound

from app import Branch, BranchStock, BranchStockMovement, BranchStockTransfer, Company, Payment, Plan, Product, Subscription, User, db
from stockarmobile.branch_inventory import (
    adjust_branch_stock, available_stock, get_branch_stock, set_branch_stock,
    reserve_branch_stock, transfer_branch_stock_batch,
)
from services.payment_flow import expire_stale_pos_drafts, release_pos_payment_reservation
from services.ai_agent.usage_service import can_use_commercial_feature


def _company(name):
    company = Company(name=name, active=True)
    db.session.add(company)
    db.session.flush()
    return company


def _branch(company, name, code, *, default=False):
    branch = Branch(
        company_id=company.id,
        name=name,
        code=code,
        active=True,
        is_default=default,
    )
    db.session.add(branch)
    db.session.flush()
    return branch


def test_branch_stock_is_independent_and_keeps_company_total(app):
    with app.app_context():
        company = _company("Empresa Multisucursal")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(
            company_id=company.id,
            barcode="MS-001",
            name="Producto multisucursal",
            stock=Decimal("12"),
            min_stock=Decimal("2"),
            active=True,
        )
        db.session.add(product)
        db.session.flush()

        set_branch_stock(product, central.id, Decimal("7"), movement_type="migration")
        set_branch_stock(product, north.id, Decimal("5"), movement_type="opening")
        db.session.flush()
        assert available_stock(product, central.id) == 7.0
        assert available_stock(product, north.id) == 5.0
        assert float(product.stock) == 12.0

        adjust_branch_stock(
            product,
            central.id,
            Decimal("-2"),
            movement_type="sale",
            source_type="sale",
            source_id=101,
            update_company_total=True,
        )
        db.session.flush()
        assert available_stock(product, central.id) == 5.0
        assert available_stock(product, north.id) == 5.0
        assert float(product.stock) == 10.0
        movement = BranchStockMovement.query.filter_by(
            company_id=company.id, branch_id=central.id, movement_type="sale"
        ).one()
        assert Decimal(str(movement.quantity_delta)) == Decimal("-2")
        assert Decimal(str(movement.stock_after)) == Decimal("5")
        db.session.rollback()


def test_branch_stock_rejects_branch_from_another_company(app):
    with app.app_context():
        company_a = _company("Empresa A")
        company_b = _company("Empresa B")
        branch_b = _branch(company_b, "Sucursal B", "B")
        product_a = Product(
            company_id=company_a.id,
            barcode="A-001",
            name="Producto A",
            stock=3,
            active=True,
        )
        db.session.add(product_a)
        db.session.flush()

        with pytest.raises(ValueError, match="no pertenece"):
            get_branch_stock(product_a, branch_b.id, create=True)
        db.session.rollback()


def test_historical_sale_reversal_and_edit_work_on_inactive_branch(app):
    from types import SimpleNamespace
    from services.sales.inventory_service import InventoryService

    with app.app_context():
        company = _company("Empresa reversión histórica")
        branch = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(
            company_id=company.id,
            barcode="HIST-001",
            name="Producto histórico",
            stock=Decimal("5"),
            price=Decimal("10"),
            discount=Decimal("0"),
            active=True,
        )
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, branch.id, Decimal("5"))
        db.session.flush()

        # Deactivated branches cannot receive new operational stock movements.
        branch.active = False
        db.session.flush()
        with pytest.raises(ValueError, match="inactiva"):
            adjust_branch_stock(
                product, branch.id, Decimal("-1"), movement_type="sale"
            )

        # Reversing an existing sale is a historical correction and must restore
        # stock to the original branch, not to the current/default branch.
        old_item = SimpleNamespace(product_id=product.id, quantity=Decimal("2"), product=product)
        InventoryService.reverse_stock(
            sale_items=[old_item],
            resolve_product_func=lambda item: item.product,
            branch_id=branch.id,
            source_id=7001,
        )
        assert available_stock(product, branch.id, allow_inactive=True) == 7.0
        assert Decimal(str(product.stock)) == Decimal("7")

        # Editing the same historical sale re-applies its corrected quantity
        # against that inactive branch, while ordinary sale paths remain blocked.
        lines = InventoryService.build_edit_lines(
            product_ids=[str(product.id)],
            quantities=["3"],
            prices=["10"],
            discounts=["0"],
            row_deletes=[],
            products_by_id={product.id: product},
            to_decimal=lambda value: Decimal(str(value)),
            is_truthy=lambda value: bool(value),
            branch_id=branch.id,
            allow_inactive=True,
        )
        InventoryService.apply_stock(
            lines=lines,
            branch_id=branch.id,
            source_type="sale_edit",
            source_id=7001,
            allow_inactive=True,
        )

        assert available_stock(product, branch.id, allow_inactive=True) == 4.0
        assert Decimal(str(product.stock)) == Decimal("4")
        movements = BranchStockMovement.query.filter_by(
            company_id=company.id, branch_id=branch.id, source_id=7001
        ).order_by(BranchStockMovement.id.asc()).all()
        assert [movement.movement_type for movement in movements] == [
            "sale_reversal", "sale"
        ]
        assert [Decimal(str(movement.quantity_delta)) for movement in movements] == [
            Decimal("2"), Decimal("-3")
        ]
        db.session.rollback()


def test_multibranch_feature_is_premium_only(app):
    with app.app_context():
        business = _company("Empresa Negocio")
        business_plan = Plan(
            code="business-test",
            name="Negocio QA",
            price=1,
            max_branches=1,
            features_json="inventario,ventas,clientes",
            state="active",
            active=True,
        )
        premium = Plan(
            code="premium-test",
            name="Premium QA",
            price=1,
            max_branches=5,
            features_json="all",
            state="active",
            active=True,
        )
        db.session.add_all([business_plan, premium])
        db.session.flush()
        db.session.add(Subscription(company_id=business.id, plan_id=business_plan.id, status="active"))
        db.session.commit()

        assert can_use_commercial_feature(business, "multi_branch").allowed is False

        db.session.query(Subscription).filter_by(company_id=business.id).delete()
        db.session.add(Subscription(company_id=business.id, plan_id=premium.id, status="active"))
        db.session.commit()
        assert can_use_commercial_feature(business, "multi_branch").allowed is True
        db.session.rollback()


def test_branch_stock_cannot_become_negative(app):
    with app.app_context():
        company = _company("Empresa Stock")
        branch = _branch(company, "Casa central", "CENTRAL", default=True)
        product = Product(company_id=company.id, barcode="NEG-001", name="Negativo", stock=1, active=True)
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, branch.id, Decimal("1"))
        with pytest.raises(ValueError, match="Stock insuficiente"):
            adjust_branch_stock(product, branch.id, Decimal("-2"), movement_type="sale")
        assert available_stock(product, branch.id) == 1.0
        assert float(product.stock) == 1.0
        db.session.rollback()


def test_business_documents_use_the_pos_assigned_to_the_sale_branch(app):
    from types import SimpleNamespace
    from services.business_billing_service import BusinessBillingService

    with app.app_context():
        company = _company("Empresa fiscal multisucursal")
        _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        db.session.flush()
        sale = SimpleNamespace(
            id=9001,
            branch_id=north.id,
            tipo_comprobante="factura_b",
            customer="Cliente de Norte",
            total_amount=Decimal("12500.00"),
            comprobante_emitido=False,
        )
        config = {
            "active_pos": "00001",
            "points_of_sale": [
                {"number": "00001", "description": "Casa central", "branch": "Casa central", "active": True},
                {"number": "00002", "description": "Sucursal Norte", "branch": "Sucursal Norte", "active": True},
            ],
            "numbering": {"factura_b": "00001-00000124"},
            "emission": {"currency": "ARS"},
        }
        document = BusinessBillingService.issue_sale_document(
            db.session, company=company, sale=sale, config=config
        )
        assert document.pos_number == "00002"
        assert document.document_number == "00002-00000001"
        assert document.branch_id == north.id
        assert document.branch_label == "Sucursal Norte"
        db.session.rollback()


def test_casa_central_keeps_legacy_pos_after_adding_second_branch(app):
    from types import SimpleNamespace
    from services.business_billing_service import BusinessBillingService

    with app.app_context():
        company = _company("Empresa POS legado")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        _branch(company, "Sucursal Norte", "NORTE")
        sale = SimpleNamespace(
            id=9003,
            branch_id=central.id,
            tipo_comprobante="factura_b",
            customer="Cliente central",
            total_amount=Decimal("250.00"),
            comprobante_emitido=False,
        )
        # A legacy company may have a working active POS without a branch mapping.
        config = {
            "active_pos": "00007",
            "points_of_sale": [
                {"number": "00007", "description": "Punto de venta histórico", "active": True},
            ],
            "numbering": {"factura_b": "00007-00000124"},
            "emission": {"currency": "ARS"},
        }
        document = BusinessBillingService.issue_sale_document(
            db.session, company=company, sale=sale, config=config
        )
        assert document.pos_number == "00007"
        assert document.branch_id == central.id
        db.session.rollback()


def test_business_documents_refuse_unmapped_branch_pos(app):
    from types import SimpleNamespace
    from services.business_billing_service import BusinessBillingService

    with app.app_context():
        company = _company("Empresa fiscal sin punto asignado")
        _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        sale = SimpleNamespace(
            id=9002,
            branch_id=north.id,
            tipo_comprobante="factura_b",
            customer="Cliente",
            total_amount=Decimal("100.00"),
            comprobante_emitido=False,
        )
        config = {
            "active_pos": "00001",
            "points_of_sale": [
                {"number": "00001", "description": "Casa central", "branch": "Casa central", "active": True},
            ],
            "numbering": {"factura_b": "00001-00000001"},
            "emission": {"currency": "ARS"},
        }
        with pytest.raises(ValueError, match="punto de venta fiscal"):
            BusinessBillingService.issue_sale_document(
                db.session, company=company, sale=sale, config=config
            )
        db.session.rollback()


def test_ai_stock_tools_only_reveal_the_employee_active_branch(app, monkeypatch):
    from flask_login import login_user
    from services.ai_agent.tools.product_search import BuscarProductoTool
    from services.ai_agent.tools.stock_query import ConsultarStockTool

    with app.app_context():
        company = _company("Empresa IA sucursales")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(
            company_id=company.id, barcode="AI-MS-001", name="Producto IA",
            stock=19, min_stock=2, active=True,
        )
        employee = User(
            username="ai-branch-employee", email="ai-branch-employee@example.test",
            password_hash="test-hash", role="user", active=True, company_id=company.id,
        )
        db.session.add_all([product, employee])
        db.session.flush()
        set_branch_stock(product, central.id, 7)
        set_branch_stock(product, north.id, 12)
        db.session.commit()

        monkeypatch.setattr(
            "stockarmobile.branch_context.get_active_branch_id",
            lambda: central.id,
        )
        with app.test_request_context("/"):
            login_user(employee)
            searched = BuscarProductoTool(company_id=company.id).execute(query="Producto IA")
            queried = ConsultarStockTool(company_id=company.id).execute(product_id=product.id)
            assert searched["success"] is True
            assert searched["items"][0]["stock"] == 7.0
            assert [item["branch_id"] for item in searched["items"][0]["stock_by_branch"]] == [central.id]
            assert queried["success"] is True
            assert queried["product"]["stock"] == 7.0
            assert [item["branch_id"] for item in queried["product"]["stock_by_branch"]] == [central.id]


def test_ai_sales_metrics_only_aggregate_employee_active_branch(app, monkeypatch):
    from flask_login import login_user
    from services.ai_agent.tools.business_metrics import ResumenVentasTool

    with app.app_context():
        company = _company("Empresa IA métricas")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        employee = User(
            username="ai-metrics-employee", email="ai-metrics-employee@example.test",
            password_hash="test-hash", role="user", active=True, company_id=company.id,
        )
        db.session.add(employee)
        db.session.flush()
        sale_central = __import__("app").Sale(
            company_id=company.id, branch_id=central.id, customer="Central",
            subtotal=100, total_amount=100, status="confirmada",
        )
        sale_north = __import__("app").Sale(
            company_id=company.id, branch_id=north.id, customer="Norte",
            subtotal=900, total_amount=900, status="confirmada",
        )
        db.session.add_all([sale_central, sale_north])
        db.session.commit()

        monkeypatch.setattr(
            "stockarmobile.branch_context.get_active_branch_id",
            lambda: central.id,
        )
        with app.test_request_context("/"):
            login_user(employee)
            result = ResumenVentasTool(company_id=company.id).execute(days=2)
            assert result["sales_count"] == 1
            assert result["sales_total"] == 100.0
        db.session.rollback()



def test_backup_inventory_restore_preserves_branch_balances_and_aggregate(app):
    from services.backup_service import BackupService

    with app.app_context():
        company = _company("Empresa backup multisucursal")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(
            company_id=company.id, barcode="BKP-MS-001", name="Producto backup",
            stock=12, min_stock=2, active=True,
        )
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, central.id, Decimal("7"))
        set_branch_stock(product, north.id, Decimal("5"))
        db.session.flush()

        from app import BranchStockMovement, BranchStockTransfer
        transfer = BranchStockTransfer(
            company_id=company.id, source_branch_id=central.id,
            target_branch_id=north.id, product_id=product.id,
            quantity=Decimal("1"), note="Transferencia backup",
        )
        db.session.add(transfer)
        db.session.flush()
        snapshot = {
            "company_id": company.id,
            "branch_stock": [
                {"company_id": company.id, "branch_id": central.id, "product_id": product.id,
                 "stock": "7.000", "min_stock": "2.000", "reserved_stock": "0.000"},
                {"company_id": company.id, "branch_id": north.id, "product_id": product.id,
                 "stock": "5.000", "min_stock": "2.000", "reserved_stock": "0.000"},
            ],
            "branch_stock_movements": [
                BackupService._row_to_dict(row)
                for row in BranchStockMovement.query.filter_by(company_id=company.id).order_by(BranchStockMovement.id).all()
            ],
            "branch_stock_transfers": [
                BackupService._row_to_dict(transfer)
            ],
        }
        set_branch_stock(product, central.id, Decimal("1"))
        set_branch_stock(product, north.id, Decimal("2"))
        BackupService._restore_branch_inventory(company_id=company.id, payload=snapshot)
        db.session.flush()
        assert available_stock(product, central.id) == 7.0
        assert available_stock(product, north.id) == 5.0
        assert float(product.stock) == 12.0
        assert BranchStockTransfer.query.filter_by(company_id=company.id).count() == 1
        restored_transfer = BranchStockTransfer.query.filter_by(company_id=company.id).one()
        restored_movements = BranchStockMovement.query.filter_by(company_id=company.id).count()
        assert restored_transfer.note == "Transferencia backup"
        assert restored_movements == 2
        db.session.rollback()


def test_legacy_backup_inventory_restores_into_casa_central(app):
    from services.backup_service import BackupService

    with app.app_context():
        company = _company("Empresa backup legacy")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(company_id=company.id, barcode="BKP-OLD-001", name="Legacy", stock=9, active=True)
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, north.id, Decimal("4"))
        BackupService._restore_branch_inventory(
            company_id=company.id,
            payload={"company_id": company.id, "products": [{"id": product.id, "stock": 9}]},
        )
        db.session.flush()
        assert available_stock(product, central.id) == 9.0
        assert available_stock(product, north.id) == 0.0
        assert float(product.stock) == 9.0
        db.session.rollback()



def test_branch_transfer_moves_stock_without_changing_company_total(app):
    from app import BranchStockTransfer
    from stockarmobile.branch_inventory import transfer_branch_stock

    with app.app_context():
        company = _company("Empresa transferencias")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(
            company_id=company.id, barcode="TR-001", name="Producto transferible",
            stock=10, min_stock=1, active=True,
        )
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, central.id, Decimal("8"))
        set_branch_stock(product, north.id, Decimal("2"))
        transfer = transfer_branch_stock(
            product, central.id, north.id, Decimal("3.5"), note="Reposición semanal"
        )
        db.session.flush()
        assert available_stock(product, central.id) == 4.5
        assert available_stock(product, north.id) == 5.5
        assert float(product.stock) == 10.0
        assert transfer.company_id == company.id
        assert transfer.source_branch_id == central.id
        assert transfer.target_branch_id == north.id
        movements = BranchStockMovement.query.filter_by(
            company_id=company.id, source_type="branch_transfer", source_id=transfer.id
        ).all()
        assert {row.movement_type for row in movements} == {"transfer_out", "transfer_in"}
        db.session.rollback()


def test_branch_transfer_rejects_more_than_three_decimals(app):
    from stockarmobile.branch_inventory import transfer_branch_stock

    with app.app_context():
        company = _company("Empresa precisión transferencia")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(company_id=company.id, barcode="TR-003", name="Precisión", stock=2, active=True)
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, central.id, Decimal("2"))
        set_branch_stock(product, north.id, Decimal("0"))
        with pytest.raises(ValueError, match="tres decimales"):
            transfer_branch_stock(product, central.id, north.id, Decimal("0.0001"))
        assert available_stock(product, central.id) == 2.0
        assert available_stock(product, north.id) == 0.0
        db.session.rollback()


def test_branch_transfer_rejects_insufficient_source_stock(app):
    from stockarmobile.branch_inventory import transfer_branch_stock

    with app.app_context():
        company = _company("Empresa transferencia sin stock")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(company_id=company.id, barcode="TR-002", name="Stock corto", stock=1, active=True)
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, central.id, Decimal("1"))
        set_branch_stock(product, north.id, Decimal("0"))
        with pytest.raises(ValueError, match="Stock insuficiente"):
            transfer_branch_stock(product, central.id, north.id, Decimal("2"))
        assert available_stock(product, central.id) == 1.0
        assert available_stock(product, north.id) == 0.0
        assert float(product.stock) == 1.0
        db.session.rollback()


def test_employees_only_see_assigned_active_branches(app):
    from flask_login import login_user
    from app import UserBranchAccess
    from stockarmobile.branch_context import accessible_branches

    with app.app_context():
        company = _company("Empresa permisos por sede")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        employee = User(
            username="branch-access-user", email="branch-access-user@example.test",
            password_hash="test-hash", role="user", active=True, company_id=company.id,
        )
        db.session.add(employee)
        db.session.flush()
        db.session.add(UserBranchAccess(company_id=company.id, user_id=employee.id, branch_id=north.id))
        db.session.commit()
        with app.test_request_context("/"):
            login_user(employee)
            visible = accessible_branches(company.id)
            assert [row.id for row in visible] == [north.id]
        db.session.rollback()



def test_ai_marketing_tools_use_employee_branch_inventory_and_sales(app):
    from datetime import datetime
    from flask_login import login_user
    from app import BranchStock, Sale, UserBranchAccess
    from services.ai_agent.business_intelligence import ProductRecommendationsTool, InventoryTurnoverTool
    from services.ai_agent.tools.analyst_marketing import _branch_stock_products, _scope_sale_query

    with app.app_context():
        company = _company("Empresa IA por sucursal")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        premium_plan = Plan(
            code="premium-ai-marketing-test", name="Premium IA QA", price=1,
            max_branches=5, features_json="all", state="active", active=True,
        )
        db.session.add(premium_plan)
        db.session.flush()
        db.session.add(Subscription(company_id=company.id, plan_id=premium_plan.id, status="active"))
        employee = User(
            username="branch-ai-user", email="branch-ai-user@example.test",
            password_hash="test-hash", role="user", active=True, company_id=company.id,
            permissions_json='["ai_access"]',
        )
        product = Product(
            company_id=company.id, barcode="AI-BR-001", name="Producto IA",
            stock=15, min_stock=1, active=True,
        )
        db.session.add_all([employee, product])
        db.session.flush()
        set_branch_stock(product, central.id, Decimal("12"))
        set_branch_stock(product, north.id, Decimal("3"))
        db.session.add_all([
            Sale(company_id=company.id, branch_id=central.id, total_amount=100, date=datetime.utcnow(), status="confirmada"),
            Sale(company_id=company.id, branch_id=north.id, total_amount=40, date=datetime.utcnow(), status="confirmada"),
        ])
        db.session.add(UserBranchAccess(company_id=company.id, user_id=employee.id, branch_id=north.id))
        db.session.commit()

        with app.test_request_context("/"):
            login_user(employee)
            recommended = ProductRecommendationsTool(company_id=company.id).execute()
            assert recommended["success"] is True
            assert recommended["items"][0]["stock"] == 3.0
            local_rows = _branch_stock_products(company.id, limit=20)
            assert [row.id for row in local_rows] == [product.id]
            local_sales = _scope_sale_query(Sale.query.filter_by(company_id=company.id), company.id).all()
            assert len(local_sales) == 1
            assert local_sales[0].branch_id == north.id
            turnover = InventoryTurnoverTool(company_id=company.id).execute(days=30)
            assert turnover["success"] is True
            assert turnover["items"][0]["stock"] == 3.0
        db.session.rollback()



def test_inactive_branch_keeps_its_original_fiscal_pos_when_issuing_sale_document(app):
    from types import SimpleNamespace
    from services.business_billing_service import BusinessBillingService

    with app.app_context():
        company = _company("Empresa fiscal sucursal inactiva")
        _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        north.active = False
        db.session.flush()
        sale = SimpleNamespace(
            id=9101,
            branch_id=north.id,
            tipo_comprobante="factura_b",
            customer="Cliente Norte",
            total_amount=Decimal("1200.00"),
            comprobante_emitido=False,
        )
        config = {
            "active_pos": "00001",
            "points_of_sale": [
                {"number": "00001", "branch": "Casa central", "active": True},
                {"number": "00002", "branch": "Sucursal Norte", "active": True},
            ],
            "numbering": {"factura_b": "00001-00000001"},
            "emission": {"currency": "ARS"},
        }

        document = BusinessBillingService.issue_sale_document(
            db.session, company=company, sale=sale, config=config
        )

        assert document.pos_number == "00002"
        assert document.branch_id == north.id
        assert document.branch_label == "Sucursal Norte"
        db.session.rollback()


def test_pending_invoice_sales_are_detected_before_branch_deactivation(app):
    from app import Sale
    from branches import _has_pending_invoice_sales

    with app.app_context():
        company = _company("Empresa ventas pendientes")
        branch = _branch(company, "Sucursal Norte", "NORTE")
        sale = Sale(
            company_id=company.id,
            branch_id=branch.id,
            customer="Cliente",
            total_amount=100,
            status="confirmada",
            requiere_comprobante=True,
            comprobante_emitido=False,
        )
        db.session.add(sale)
        db.session.flush()

        assert _has_pending_invoice_sales(company.id, branch.id) is True
        sale.comprobante_emitido = True
        db.session.flush()
        assert _has_pending_invoice_sales(company.id, branch.id) is False
        db.session.rollback()


def test_branch_stock_rejects_more_than_three_decimals_without_rounding(app):
    from stockarmobile.branch_inventory import validate_stock_quantity

    with app.app_context():
        assert validate_stock_quantity("1.234") == Decimal("1.234")
        assert validate_stock_quantity("1.0000") == Decimal("1.000")
        with pytest.raises(ValueError, match="tres decimales"):
            validate_stock_quantity("1.2345")

        company = _company("Empresa precisión stock")
        branch = _branch(company, "Casa central", "CENTRAL", default=True)
        product = Product(
            company_id=company.id, barcode="PREC-001", name="Producto precisión",
            stock=10, active=True,
        )
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, branch.id, Decimal("10"))
        with pytest.raises(ValueError, match="tres decimales"):
            adjust_branch_stock(product, branch.id, Decimal("-0.0001"), movement_type="sale")
        assert available_stock(product, branch.id) == 10.0
        assert float(product.stock) == 10.0
        db.session.rollback()


def test_sales_and_purchase_inputs_reject_more_than_three_stock_decimals(app):
    from types import SimpleNamespace
    from services.sales.inventory_service import InventoryService
    from purchases import _parse_purchase_lines

    with app.app_context():
        company = _company("Empresa precisión operaciones")
        branch = _branch(company, "Casa central", "CENTRAL", default=True)
        product = Product(
            company_id=company.id, barcode="PREC-002", name="Producto precisión",
            price=10, discount=0, stock=10, active=True,
        )
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, branch.id, Decimal("10"))
        current_user = SimpleNamespace(company_id=company.id)

        with pytest.raises(ValueError, match="tres decimales"):
            InventoryService.calculate_lines(
                items={product.id: "1.2345"},
                current_user=current_user,
                fetch_products_func=lambda ids: {product.id: product},
                branch_id=branch.id,
            )

        with app.test_request_context(
            "/",
            method="POST",
            data={"product_id[]": str(product.id), "quantity[]": "1.2345", "unit_cost[]": "5"},
        ):
            with pytest.raises(ValueError, match="tres decimales"):
                _parse_purchase_lines(enforce_branch_precision=True)
        db.session.rollback()



def test_business_billing_refuses_inactive_pos_instead_of_using_another_branch(app):
    from types import SimpleNamespace
    from services.business_billing_service import BusinessBillingService

    with app.app_context():
        company = _company("Empresa POS inactivo")
        _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        sale = SimpleNamespace(
            id=9102,
            branch_id=north.id,
            tipo_comprobante="factura_b",
            customer="Cliente Norte",
            total_amount=Decimal("100.00"),
            comprobante_emitido=False,
        )
        config = {
            "active_pos": "00001",
            "points_of_sale": [
                {"number": "00001", "branch": "Casa central", "active": True},
                {"number": "00002", "branch": "Sucursal Norte", "active": False},
            ],
            "numbering": {"factura_b": "00001-00000001"},
            "emission": {"currency": "ARS"},
        }

        with pytest.raises(ValueError, match="está inactivo"):
            BusinessBillingService.issue_sale_document(
                db.session, company=company, sale=sale, config=config
            )
        db.session.rollback()


def test_manual_branch_stock_adjustment_reconciles_company_total_and_checks_precision(app):
    from stockarmobile.branch_inventory import set_branch_stock_and_reconcile

    with app.app_context():
        company = _company("Empresa ajuste manual consolidado")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(
            company_id=company.id, barcode="ADJ-001", name="Ajuste consolidado",
            stock=12, active=True,
        )
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, central.id, Decimal("7"))
        set_branch_stock(product, north.id, Decimal("5"))

        set_branch_stock_and_reconcile(
            product, central.id, Decimal("4.125"),
            movement_type="manual_adjustment", note="Prueba ajuste",
        )

        assert available_stock(product, central.id) == 4.125
        assert available_stock(product, north.id) == 5.0
        assert Decimal(str(product.stock)) == Decimal("9.125")

        with pytest.raises(ValueError, match="tres decimales"):
            set_branch_stock_and_reconcile(product, north.id, Decimal("5.0001"))
        assert available_stock(product, north.id) == 5.0
        assert Decimal(str(product.stock)) == Decimal("9.125")
        db.session.rollback()


def test_ai_invoice_purchase_and_ai_sale_reject_excess_branch_precision(app):
    from app import PurchaseOrder, Supplier
    from services.invoice_purchase_service import InvoicePurchaseService
    from services.sales.inventory_service import InventoryService

    with app.app_context():
        company = _company("Empresa precision IA")
        branch = _branch(company, "Casa central", "CENTRAL", default=True)
        user = User(
            username="precision-ai-admin",
            email="precision-ai-admin@example.test",
            password_hash="test-hash",
            role="admin",
            company_id=company.id,
        )
        supplier = Supplier(name="Proveedor IA", company_id=company.id, active=True)
        product = Product(
            company_id=company.id, barcode="AI-PREC-001", name="Producto IA",
            stock=10, cost_price=2, price=5, active=True,
        )
        db.session.add_all([user, supplier, product])
        db.session.flush()
        set_branch_stock(product, branch.id, Decimal("10"))
        # The service owns and rolls back its transaction on validation failure;
        # commit the fixture so that rollback only removes the attempted purchase.
        db.session.commit()

        invoice = {
            "status": "LISTA_PARA_CONFIRMAR",
            "currency": "ARS",
            "subtotal": "6.1725",
            "tax": "0",
            "total": "6.1725",
            "supplier_match": {"status": "MATCH_EXACTO", "supplier_id": supplier.id},
            "matches": [{
                "line_number": 1,
                "matching_status": "MATCH_EXACTO",
                "product_id": product.id,
                "quantity": "1.2345",
                "unit_cost": "5",
                "line_total": "6.1725",
            }],
        }
        with pytest.raises(ValueError, match="tres decimales"):
            InvoicePurchaseService.confirm(
                company_id=company.id,
                user_id=user.id,
                invoice=invoice,
                conversation=None,
                branch_id=branch.id,
            )
        assert PurchaseOrder.query.filter_by(company_id=company.id).count() == 0
        assert available_stock(product, branch.id) == 10.0
        assert float(product.stock) == 10.0

        with pytest.raises(ValueError, match="tres decimales"):
            InventoryService.apply_stock(
                lines=[{"product": product, "quantity": Decimal("1.2345")}],
                branch_id=branch.id,
                source_type="vendor_ai_order",
            )
        assert available_stock(product, branch.id) == 10.0
        assert float(product.stock) == 10.0
        db.session.rollback()



def test_pos_session_carts_and_quote_prefills_are_isolated_by_active_branch(app, monkeypatch):
    from flask import session
    from flask_login import login_user
    from sales import _cart_key, _cart_tenant_key, _quote_cart_prefill_session_key

    with app.app_context():
        company = _company("Empresa carrito multisucursal")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        user = User(
            username="branch-cart-admin",
            email="branch-cart-admin@example.test",
            password_hash="test-hash",
            role="admin",
            active=True,
            company_id=company.id,
        )
        db.session.add(user)
        db.session.commit()

        selected = {"branch_id": central.id}
        monkeypatch.setattr(
            "stockarmobile.branch_context.get_active_branch_id",
            lambda: selected["branch_id"],
        )
        with app.test_request_context("/"):
            login_user(user)
            tenant_key = _cart_tenant_key()
            central_cart_key = _cart_key()
            central_quote_key = _quote_cart_prefill_session_key()
            session[central_cart_key] = {"items": {"101": 2}}
            session[central_quote_key] = {"quote_id": 101}

            selected["branch_id"] = north.id
            north_cart_key = _cart_key()
            north_quote_key = _quote_cart_prefill_session_key()
            assert _cart_tenant_key() == tenant_key
            assert north_cart_key != central_cart_key
            assert north_quote_key != central_quote_key
            assert north_cart_key not in session
            assert north_quote_key not in session

            session[north_cart_key] = {"items": {"202": 1}}
            selected["branch_id"] = central.id
            assert _cart_key() == central_cart_key
            assert _quote_cart_prefill_session_key() == central_quote_key
            assert session[central_cart_key]["items"] == {"101": 2}
            assert session[central_quote_key]["quote_id"] == 101
        db.session.rollback()


def test_pos_browser_cart_and_quote_storage_are_scoped_to_active_branch():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    base_template = (root / "templates" / "base_master.html").read_text(encoding="utf-8")
    cart_manager = (root / "static" / "assets" / "js" / "cart-manager.js").read_text(encoding="utf-8")
    sales_template = (root / "templates" / "ventas" / "index.html").read_text(encoding="utf-8")

    assert 'data-active-branch-id="{{ active_branch_id or \'\' }}"' in base_template
    assert "function getCartStorageScopeKey()" in cart_manager
    assert "document.body?.dataset?.activeBranchId" in cart_manager
    assert "CART_KEY_PREFIX}_${getCartStorageScopeKey()}" in cart_manager
    assert "QUOTE_CONTEXT_STORAGE_PREFIX}_${getCartStorageScopeKey()}" in cart_manager
    assert "v='20261103-branch-aware-cart-scope'" in sales_template


def test_company_settings_branch_panel_keeps_render_context_and_jinja_defaults_safe():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    company_billing = (root / "company_billing.py").read_text(encoding="utf-8")
    settings_template = (root / "templates" / "company_billing" / "settings.html").read_text(encoding="utf-8")

    assert "active_branch_count=active_branch_count" in company_billing
    assert "branch_user_access.get(branch.id, [])" in settings_template
    assert "branch_user_access.get(branch.id, set())" not in settings_template


def test_reserved_stock_is_not_available_to_sales_or_transfers_and_releases_once(app):
    with app.app_context():
        company = _company("Empresa reservas QR")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(
            company_id=company.id,
            barcode="QR-RES-01",
            name="Producto reservado",
            stock=Decimal("5"),
            min_stock=Decimal("0"),
            active=True,
        )
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, central.id, 5)
        set_branch_stock(product, north.id, 0)
        reserve_branch_stock(product, central.id, 3, note="QR pendiente")
        db.session.flush()

        row = get_branch_stock(product, central.id)
        assert Decimal(str(row.stock)) == Decimal("5")
        assert Decimal(str(row.reserved_stock)) == Decimal("3")
        assert available_stock(product, central.id) == 2.0

        with pytest.raises(ValueError, match="Stock insuficiente"):
            adjust_branch_stock(
                product, central.id, -3, movement_type="sale",
                update_company_total=True,
            )
        with pytest.raises(ValueError, match="Stock insuficiente"):
            transfer_branch_stock_batch(
                [(product, 3)], central.id, north.id,
                operation_key="reserve-protect-qr",
            )
        db.session.rollback()

        # Recreate reservation after the expected failed operations rolled back.
        reserve_branch_stock(product, central.id, 3, note="QR pendiente")
        payment = Payment(
            company_id=company.id,
            branch_id=central.id,
            amount=300,
            currency="ARS",
            status="pending",
            provider="mercadopago_pos",
            reference="pos_draft:reserve",
            payload_json=json.dumps({
                "flow": "pos_sale",
                "snapshot": {
                    "branch_id": central.id,
                    "items": [{"productId": product.id, "quantity": 3}],
                    "stock_reservation_released": False,
                },
            }),
        )
        db.session.add(payment)
        db.session.flush()

        assert release_pos_payment_reservation(db.session, payment) is True
        assert release_pos_payment_reservation(db.session, payment) is False
        db.session.flush()
        assert Decimal(str(get_branch_stock(product, central.id).reserved_stock)) == Decimal("0")
        assert available_stock(product, central.id) == 5.0
        db.session.rollback()


def test_expiring_stale_pos_draft_releases_branch_reservation(app):
    with app.app_context():
        company = _company("Empresa QR vencimiento")
        branch = _branch(company, "Casa central", "CENTRAL", default=True)
        product = Product(
            company_id=company.id,
            barcode="QR-EXP-01",
            name="Producto vencido",
            stock=Decimal("4"),
            active=True,
        )
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, branch.id, 4)
        reserve_branch_stock(product, branch.id, 2, note="QR expirado")
        payment = Payment(
            company_id=company.id,
            branch_id=branch.id,
            amount=200,
            currency="ARS",
            status="pending",
            provider="mercadopago_pos",
            reference="pos_draft:expired",
            created_at=datetime.utcnow() - timedelta(hours=80),
            payload_json=json.dumps({
                "flow": "pos_sale",
                "snapshot": {
                    "branch_id": branch.id,
                    "items": [{"productId": product.id, "quantity": 2}],
                    "stock_reservation_released": False,
                },
            }),
        )
        db.session.add(payment)
        db.session.commit()

        count = expire_stale_pos_drafts(
            db.session,
            company_id=company.id,
            now=datetime.utcnow(),
        )
        db.session.commit()

        assert count == 1
        assert db.session.get(Payment, payment.id).status == "expired"
        assert Decimal(str(get_branch_stock(product, branch.id).reserved_stock)) == Decimal("0")
        assert available_stock(product, branch.id) == 4.0
        db.session.rollback()


def test_branch_transfer_batch_is_idempotent_for_retried_operation_key(app):
    with app.app_context():
        company = _company("Empresa transferencias idempotentes")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        product = Product(
            company_id=company.id,
            barcode="TR-IDEMP-01",
            name="Producto idempotente",
            stock=Decimal("10"),
            active=True,
        )
        db.session.add(product)
        db.session.flush()
        set_branch_stock(product, central.id, 10)
        set_branch_stock(product, north.id, 0)
        db.session.commit()

        first = transfer_branch_stock_batch(
            [(product, Decimal("3"))],
            central.id,
            north.id,
            operation_key="same-client-operation-001",
        )
        db.session.commit()
        original_ids = [row.id for row in first]
        original_movement_count = BranchStockMovement.query.filter_by(
            company_id=company.id,
            source_type="branch_transfer",
        ).count()
        assert original_movement_count == 2

        repeated = transfer_branch_stock_batch(
            [(product, Decimal("3"))],
            central.id,
            north.id,
            operation_key="same-client-operation-001",
        )
        db.session.commit()
        assert [row.id for row in repeated] == original_ids
        assert available_stock(product, central.id) == 7.0
        assert available_stock(product, north.id) == 3.0
        assert BranchStockMovement.query.filter_by(
            company_id=company.id,
            source_type="branch_transfer",
        ).count() == 2

        with pytest.raises(ValueError, match="clave de esta solicitud"):
            transfer_branch_stock_batch(
                [(product, Decimal("2"))],
                central.id,
                north.id,
                operation_key="same-client-operation-001",
            )
        db.session.rollback()
        assert available_stock(product, central.id) == 7.0
        assert available_stock(product, north.id) == 3.0


def test_qr_payment_status_is_hidden_from_other_users_and_branches(app, monkeypatch):
    from flask_login import login_user
    from sales import _authorized_pos_qr_payment

    with app.app_context():
        company = _company("Empresa autorización QR")
        central = _branch(company, "Casa central", "CENTRAL", default=True)
        north = _branch(company, "Sucursal Norte", "NORTE")
        owner = User(
            username="qr-owner",
            email="qr-owner@example.test",
            password_hash="test-hash",
            role="user",
            active=True,
            company_id=company.id,
        )
        other = User(
            username="qr-other",
            email="qr-other@example.test",
            password_hash="test-hash",
            role="user",
            active=True,
            company_id=company.id,
        )
        payment = Payment(
            company_id=company.id,
            branch_id=central.id,
            user_id=owner.id,
            amount=100,
            currency="ARS",
            status="approved",
            provider="mercadopago_pos",
            payload_json=json.dumps({
                "flow": "pos_sale",
                "snapshot": {"branch_id": central.id, "items": [{"productId": 1, "quantity": 1}]},
            }),
        )
        db.session.add_all([owner, other, payment])
        db.session.commit()

        monkeypatch.setattr(
            "stockarmobile.branch_context.get_active_branch_id",
            lambda: central.id,
        )
        with app.test_request_context("/"):
            login_user(owner)
            assert _authorized_pos_qr_payment(payment.id).id == payment.id

        with app.test_request_context("/"):
            login_user(other)
            with pytest.raises(NotFound):
                _authorized_pos_qr_payment(payment.id)

        monkeypatch.setattr(
            "stockarmobile.branch_context.get_active_branch_id",
            lambda: north.id,
        )
        with app.test_request_context("/"):
            login_user(owner)
            with pytest.raises(NotFound):
                _authorized_pos_qr_payment(payment.id)
        db.session.rollback()
