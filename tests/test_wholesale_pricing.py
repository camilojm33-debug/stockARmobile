from decimal import Decimal

import pytest
from flask_login import login_user, logout_user

from app import Client, Company, Product, User, db
from services.wholesale_pricing_service import (
    apply_wholesale_prices,
    product_price_catalog,
    resolve_product_price,
)
from wholesale_pricing import PriceList, PriceListItem


@pytest.fixture
def wholesale_database(app):
    company = Company(name="Empresa mayorista QA", active=True)
    other_company = Company(name="Otra empresa QA", active=True)
    db.session.add_all([company, other_company])
    db.session.flush()

    admin = User(
        username="wholesale_admin",
        email="wholesale-admin@test.local",
        password_hash="test-password-hash",
        role="admin",
        active=True,
        company_id=company.id,
    )
    employee = User(
        username="wholesale_employee",
        email="wholesale-employee@test.local",
        password_hash="test-password-hash",
        role="user",
        active=True,
        company_id=company.id,
    )
    db.session.add_all([admin, employee])
    db.session.flush()

    product = Product(
        company_id=company.id,
        barcode="WHOLESALE-001",
        name="Producto mayorista QA",
        price=Decimal("100.00"),
        cost_price=Decimal("50.00"),
        discount=Decimal("5.00"),
        stock=100,
        min_stock=1,
        active=True,
        sale_type="unidad",
        unit_measure="u",
    )
    other_product = Product(
        company_id=other_company.id,
        barcode="OTHER-001",
        name="Producto de otra empresa",
        price=Decimal("300.00"),
        cost_price=Decimal("100.00"),
        discount=Decimal("0.00"),
        stock=10,
        min_stock=1,
        active=True,
        sale_type="unidad",
        unit_measure="u",
    )
    db.session.add_all([product, other_product])
    db.session.flush()

    price_list = PriceList(
        company_id=company.id,
        name="Mayorista QA",
        code="mayorista-qa",
        active=True,
        created_by_user_id=admin.id,
    )
    db.session.add(price_list)
    db.session.flush()
    db.session.add_all([
        PriceListItem(
            company_id=company.id,
            price_list_id=price_list.id,
            product_id=product.id,
            min_quantity=Decimal("1.000"),
            unit_price=Decimal("80.00"),
            active=True,
        ),
        PriceListItem(
            company_id=company.id,
            price_list_id=price_list.id,
            product_id=product.id,
            min_quantity=Decimal("12.000"),
            unit_price=Decimal("70.00"),
            active=True,
        ),
    ])
    wholesale_client = Client(
        company_id=company.id,
        name="Cliente mayorista QA",
        active=True,
        customer_type="mayorista",
        price_list_id=price_list.id,
    )
    retail_client = Client(
        company_id=company.id,
        name="Cliente minorista QA",
        active=True,
        customer_type="minorista",
        price_list_id=price_list.id,
    )
    db.session.add_all([wholesale_client, retail_client])
    db.session.commit()
    return {
        "company": company,
        "other_company": other_company,
        "admin": admin,
        "employee": employee,
        "product": product,
        "other_product": other_product,
        "price_list": price_list,
        "wholesale_client": wholesale_client,
        "retail_client": retail_client,
    }


def test_resolver_uses_the_highest_quantity_tier_and_retail_fallback(wholesale_database):
    data = wholesale_database
    company_id = data["company"].id
    product = data["product"]
    wholesale_client = data["wholesale_client"]
    retail_client = data["retail_client"]

    one = resolve_product_price(
        company_id=company_id, product=product, quantity=1, client=wholesale_client
    )
    dozen = resolve_product_price(
        company_id=company_id, product=product, quantity=12, client=wholesale_client
    )
    retail = resolve_product_price(
        company_id=company_id, product=product, quantity=12, client=retail_client
    )
    wrong_tenant = resolve_product_price(
        company_id=company_id, product=product, quantity=12,
        client=Client(company_id=data["other_company"].id, customer_type="mayorista", price_list_id=data["price_list"].id),
    )

    assert one["source"] == "mayorista"
    assert one["price"] == Decimal("80.00")
    assert dozen["source"] == "mayorista"
    assert dozen["price"] == Decimal("70.00")
    assert dozen["price_list_id"] == data["price_list"].id
    assert retail["source"] == "minorista"
    assert retail["price"] == Decimal("100.00")
    assert wrong_tenant["source"] == "minorista"
    assert wrong_tenant["price"] == Decimal("100.00")


def test_apply_wholesale_price_does_not_mutate_product_or_stack_legacy_discount(wholesale_database):
    data = wholesale_database
    product = data["product"]
    original_price = Decimal(str(product.price))
    lines = [{
        "product": product,
        "quantity": Decimal("12"),
        "price": Decimal(str(product.price)),
        "discount": Decimal("60.00"),
    }]

    applied = apply_wholesale_prices(
        lines, company_id=data["company"].id, client=data["wholesale_client"]
    )

    assert applied[0]["price"] == Decimal("70.00")
    assert applied[0]["discount"] == Decimal("0.00")
    assert applied[0]["price_source"] == "mayorista"
    assert applied[0]["price_list_id"] == data["price_list"].id
    assert product.price == original_price


def test_product_price_catalog_is_company_scoped_and_returns_quantity_tiers(wholesale_database):
    data = wholesale_database
    catalog = product_price_catalog(
        company_id=data["company"].id,
        product_ids=[data["product"].id, data["other_product"].id],
    )

    assert data["product"].id in catalog
    tiers = catalog[data["product"].id][str(data["price_list"].id)]
    assert tiers == [
        {"min_quantity": 1.0, "price": 80.0},
        {"min_quantity": 12.0, "price": 70.0},
    ]
    assert data["other_product"].id not in catalog


def test_quote_parser_applies_assigned_list_and_removes_legacy_discount(wholesale_database, app):
    import quotes

    data = wholesale_database
    with app.test_request_context("/presupuestos/nuevo"):
        login_user(data["employee"])
        try:
            items = quotes._parse_items(
                {"items": [{
                    "product_id": data["product"].id,
                    "description": data["product"].name,
                    "quantity": 12,
                    "unit_price": 100,
                    "discount": 60,
                }]},
                client=data["wholesale_client"],
            )
        finally:
            logout_user()

    assert len(items) == 1
    assert items[0]["unit_price"] == Decimal("70.00")
    assert items[0]["discount"] == Decimal("0.00")
    assert items[0]["price_source"] == "mayorista"
    assert items[0]["price_list_id"] == data["price_list"].id


def test_retail_quote_keeps_existing_price_and_product_discount(wholesale_database, app):
    import quotes

    data = wholesale_database
    with app.test_request_context("/presupuestos/nuevo"):
        login_user(data["employee"])
        try:
            items = quotes._parse_items(
                {"items": [{
                    "product_id": data["product"].id,
                    "description": data["product"].name,
                    "quantity": 2,
                    "unit_price": 100,
                    "discount": 10,
                }]},
                client=data["retail_client"],
            )
        finally:
            logout_user()

    assert items[0]["unit_price"] == Decimal("100")
    assert items[0]["discount"] == Decimal("10")
    assert items[0]["price_source"] == "minorista"
    assert items[0]["price_list_id"] is None


def test_admin_can_create_price_list_and_add_a_product_price(wholesale_database, app):
    data = wholesale_database
    browser = app.test_client()
    # Set a deterministic password for this test administrator.
    with app.app_context():
        data["admin"].set_password("test-password-123")
        db.session.commit()
    login = browser.post(
        "/auth/login",
        data={"username": "wholesale_admin", "password": "test-password-123"},
    )
    assert login.status_code in (301, 302)

    response = browser.post(
        "/precios-mayoristas/listas",
        data={"name": "Lista de prueba", "code": "lista-prueba"},
    )
    assert response.status_code in (301, 302)
    with app.app_context():
        price_list = PriceList.query.filter_by(
            company_id=data["company"].id, code="lista-prueba"
        ).first()
        assert price_list is not None
        list_id = price_list.id

    response = browser.post(
        f"/precios-mayoristas/listas/{list_id}/items",
        data={
            "product_id": str(data["product"].id),
            "min_quantity": "1",
            "unit_price": "75.50",
        },
    )
    assert response.status_code in (301, 302)
    with app.app_context():
        saved = PriceListItem.query.filter_by(
            company_id=data["company"].id,
            price_list_id=list_id,
            product_id=data["product"].id,
            min_quantity=Decimal("1.000"),
        ).first()
        assert saved is not None
        assert Decimal(str(saved.unit_price)) == Decimal("75.50")
