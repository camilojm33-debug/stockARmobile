from decimal import Decimal

import pytest

from app import Branch, BranchStock, BranchStockMovement, Company, Plan, Product, Subscription, db
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
