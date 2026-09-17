import app as stock_app
from app import Company, Product, User, db
from flask_login import login_user


def _rules(product_query="", supplier=""):
    from pricing_controller import _parse_rules

    return _parse_rules(
        {
            "name": "QA precios",
            "adjustment_type": "percent",
            "direction": "increase",
            "adjustment_value": "10",
            "product_query": product_query,
            "supplier": supplier,
        }
    )


def test_global_price_controller_includes_products_without_supplier(app):
    with app.app_context():
        company = Company(name="QA precios", active=True)
        db.session.add(company)
        db.session.flush()

        user = User(
            username="qa_price_admin",
            email="qa_price_admin@test.local",
            password_hash="x",
            role="admin",
            active=True,
            company_id=company.id,
        )
        products = [
            Product(
                barcode="QA-001",
                name="Producto sin proveedor",
                price=100,
                cost_price=50,
                stock=10,
                active=True,
                company_id=company.id,
                supplier=None,
            ),
            Product(
                barcode="QA-002",
                name="Producto con proveedor",
                price=200,
                cost_price=100,
                stock=10,
                active=True,
                company_id=company.id,
                supplier="Proveedor Demo",
            ),
            Product(
                barcode="QA-003",
                name="Producto inactivo",
                price=300,
                cost_price=150,
                stock=10,
                active=False,
                company_id=company.id,
                supplier="Otro",
            ),
        ]
        db.session.add(user)
        db.session.add_all(products)
        db.session.commit()

        with app.test_request_context("/precios/"):
            login_user(user)
            from pricing_controller import _products_for_rules

            rows = _products_for_rules(_rules()).all()
            assert {row.name for row in rows} == {"Producto sin proveedor", "Producto con proveedor"}


def test_global_price_controller_can_filter_a_specific_product_without_supplier(app):
    with app.app_context():
        company = Company(name="QA filtro producto", active=True)
        db.session.add(company)
        db.session.flush()

        user = User(
            username="qa_price_admin_2",
            email="qa_price_admin_2@test.local",
            password_hash="x",
            role="admin",
            active=True,
            company_id=company.id,
        )
        products = [
            Product(
                barcode="SKU-COLA",
                name="Gaseosa Cola",
                brand="Marca QA",
                price=100,
                cost_price=50,
                stock=10,
                active=True,
                company_id=company.id,
                supplier=None,
            ),
            Product(
                barcode="SKU-AGUA",
                name="Agua",
                brand="Otra",
                price=80,
                cost_price=40,
                stock=10,
                active=True,
                company_id=company.id,
                supplier="Proveedor Demo",
            ),
        ]
        db.session.add(user)
        db.session.add_all(products)
        db.session.commit()

        with app.test_request_context("/precios/"):
            login_user(user)
            from pricing_controller import _products_for_rules

            rows = _products_for_rules(_rules(product_query="SKU-COLA")).all()
            assert [row.name for row in rows] == ["Gaseosa Cola"]
