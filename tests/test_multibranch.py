from decimal import Decimal

import pytest

from app import Branch, BranchStock, BranchStockMovement, Company, Plan, Product, Subscription, User, db
from stockarmobile.branch_inventory import adjust_branch_stock, available_stock, get_branch_stock, set_branch_stock
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
                {"number": "00002", "description": "Sucursal Norte", "branch": "Sucursal Norte", "active": False},
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

        snapshot = {
            "company_id": company.id,
            "branch_stock": [
                {"company_id": company.id, "branch_id": central.id, "product_id": product.id,
                 "stock": "7.000", "min_stock": "2.000", "reserved_stock": "0.000"},
                {"company_id": company.id, "branch_id": north.id, "product_id": product.id,
                 "stock": "5.000", "min_stock": "2.000", "reserved_stock": "0.000"},
            ],
        }
        set_branch_stock(product, central.id, Decimal("1"))
        set_branch_stock(product, north.id, Decimal("2"))
        BackupService._restore_branch_inventory(company_id=company.id, payload=snapshot)
        db.session.flush()
        assert available_stock(product, central.id) == 7.0
        assert available_stock(product, north.id) == 5.0
        assert float(product.stock) == 12.0
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
