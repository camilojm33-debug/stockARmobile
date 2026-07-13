import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest

import app as stock_app
from app import Client, Company, Product, Subscription, User, db


@pytest.fixture(autouse=True)
def clean_database():
    stock_app.app.config["TESTING"] = True
    stock_app.app.config["WTF_CSRF_ENABLED"] = False
    with stock_app.app.app_context():
        db.drop_all()
        seed()
    yield
    with stock_app.app.app_context():
        db.session.remove()
        db.drop_all()


def seed():
    db.create_all()
    company = Company(name="Empresa Demo", active=True)
    db.session.add(company)
    db.session.flush()

    company_admin = User(username="empresa_admin", email="admin@test.local", role="admin", company_id=company.id, active=True)
    company_admin.set_password("admin123")
    db.session.add(company_admin)

    superadmin = User(username="superadmin", email="superadmin@test.local", role="superadmin", company_id=company.id, active=True)
    superadmin.set_password("admin123")
    db.session.add(superadmin)

    db.session.add(
        Product(
            barcode="123456789012",
            name="Yerba kilo",
            price=18000,
            cost_price=10000,
            stock=2.5,
            min_stock=0.5,
            active=True,
            sale_type="kilogramo",
            unit_measure="kg",
            company_id=company.id,
        )
    )
    db.session.add(Client(name="Cliente demo", email="cliente@test.local", active=True, whatsapp="549111111111", company_id=company.id))
    db.session.commit()


def test_core_routes_and_decimal_checkout():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    for path in [
        "/dashboard/",
        "/dashboard/inicio-rapido",
        "/productos/",
        "/clientes/",
        "/ventas/",
        "/qr/",
        "/compras/",
        "/caja/",
        "/gastos/",
        "/reportes/",
        "/admin/portal",
    ]:
        response = client.get(path)
        assert response.status_code == 200, path

    response = client.post("/ventas/api/checkout", json={"items": [{"productId": 1, "quantity": 0.350}], "metodo_pago": "EFECTIVO"})
    assert response.status_code == 200
    with stock_app.app.app_context():
        product = db.session.get(Product, 1)
        assert round(product.stock, 3) == 2.15

    assert client.get("/superadmin/").status_code == 403

    client.post("/auth/logout")
    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})
    assert client.get("/superadmin/").status_code == 200
    assert client.get("/superadmin/billing").status_code == 200
    superadmin_dashboard = client.get("/dashboard/", follow_redirects=False)
    assert superadmin_dashboard.status_code in (301, 302)


def test_exports_and_security_methods():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    assert client.get("/reportes/ventas.csv").status_code == 200
    assert client.get("/reportes/ventas.xlsx").status_code == 200
    assert client.get("/manifest.json").status_code == 200
    assert client.get("/service-worker.js").status_code == 200
    assert client.get("/api/search?q=Yerba").status_code == 200
    assert client.get("/api/notifications").status_code == 200
    assert client.get("/ventas/api/recent").status_code == 200
    assert client.get("/productos/export.xlsx").status_code == 200
    assert client.get("/productos/1/kardex").status_code == 200
    assert client.get("/superadmin/metrics.xlsx").status_code == 403
    client.post("/auth/logout")
    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})
    assert client.get("/superadmin/metrics.xlsx").status_code == 200
    assert client.get("/qr/print-all").status_code == 405
    assert client.get("/productos/delete/1").status_code == 405


def test_subscription_state_guard():
    with stock_app.app.app_context():
        company = Company(name="Test company")
        db.session.add(company)
        db.session.flush()
        subscription = Subscription(company_id=company.id, plan_id=1, status="suspended")
        db.session.add(subscription)
        db.session.commit()

        from app import get_company_access_state

        state = get_company_access_state(company.id)
        assert state["status"] == "suspended"
        assert state["can_access"] is False


def test_cross_tenant_id_url_access_is_blocked():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Sale

        company_two = Company(name="Empresa Dos", active=True)
        db.session.add(company_two)
        db.session.flush()

        user_two = User(username="empresa_dos", email="empresa2@test.local", role="admin", company_id=company_two.id, active=True)
        user_two.set_password("admin123")
        db.session.add(user_two)

        product_two = Product(
            barcode="223456789012",
            name="Producto Empresa Dos",
            price=9500,
            cost_price=5000,
            stock=5,
            min_stock=1,
            active=True,
            company_id=company_two.id,
        )
        db.session.add(product_two)
        db.session.flush()

        client_two = Client(name="Cliente Empresa Dos", email="cliente2@test.local", active=True, company_id=company_two.id)
        db.session.add(client_two)
        db.session.flush()

        sale_two = Sale(
            customer="Cliente Empresa Dos",
            subtotal=1000,
            discount=0,
            tax=210,
            total_amount=1210,
            payment_method="EFECTIVO",
            seller_id=user_two.id,
            company_id=company_two.id,
        )
        db.session.add(sale_two)
        db.session.commit()

        foreign_product_id = product_two.id
        foreign_client_id = client_two.id
        foreign_sale_id = sale_two.id

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    assert client.get(f"/clientes/api/{foreign_client_id}").status_code == 404
    assert client.get(f"/qr/image/{foreign_product_id}").status_code == 404
    assert client.get(f"/ventas/api/ventas/{foreign_sale_id}").status_code == 404
    assert client.get(f"/ventas/{foreign_sale_id}").status_code == 404
    assert client.get(f"/ventas/{foreign_sale_id}/imprimir-ticket").status_code == 404
    assert client.get(f"/qr/ticket/{foreign_sale_id}.pdf").status_code == 404
    assert client.get(f"/ventas/success/{foreign_sale_id}").status_code == 404
    assert client.get(f"/clientes/show/{foreign_client_id}").status_code == 404

    product_edit_response = client.get(f"/productos/edit/{foreign_product_id}", follow_redirects=False)
    assert product_edit_response.status_code in (302, 404)

    kardex_response = client.get(f"/productos/{foreign_product_id}/kardex", follow_redirects=False)
    assert kardex_response.status_code in (302, 404)

    products_html = client.get("/productos/")
    assert products_html.status_code == 200
    assert "Producto Empresa Dos" not in products_html.data.decode("utf-8")

    clients_html = client.get("/clientes/")
    assert clients_html.status_code == 200
    assert "Cliente Empresa Dos" not in clients_html.data.decode("utf-8")

    sales_csv = client.get("/ventas/exportar-ventas/csv")
    assert sales_csv.status_code == 200
    assert "Cliente Empresa Dos" not in sales_csv.data.decode("utf-8")

    report_csv = client.get("/reportes/ventas.csv")
    assert report_csv.status_code == 200
    assert "Cliente Empresa Dos" not in report_csv.data.decode("utf-8")


def test_product_barcode_is_unique_per_company():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        company_one = Company.query.filter_by(name="Empresa Demo").first()
        company_two = Company(name="Empresa Dos", active=True)
        db.session.add(company_two)
        db.session.flush()

        user_two = User(username="tenant_conflict", email="tenant_conflict@test.local", role="admin", company_id=company_two.id, active=True)
        user_two.set_password("admin123")
        db.session.add(user_two)

        db.session.add(
            Product(
                barcode="DUPLICADO-001",
                name="Producto Empresa Uno",
                price=100,
                cost_price=50,
                stock=5,
                min_stock=1,
                active=True,
                company_id=company_one.id,
            )
        )
        db.session.commit()
        company_two_id = company_two.id

    client.post("/auth/login", data={"username": "tenant_conflict", "password": "admin123"})

    post_response = client.post(
        "/productos/add",
        data={
            "barcode": "DUPLICADO-001",
            "name": "Producto Empresa Dos",
            "sale_type": "unidad",
            "unit_measure": "u",
            "price": "200",
            "cost_price": "120",
            "stock": "3",
            "min_stock": "1",
        },
        follow_redirects=False,
    )
    assert post_response.status_code in (301, 302)

    with stock_app.app.app_context():
        created_for_company_two = Product.query.filter_by(company_id=company_two_id, barcode="DUPLICADO-001").count()
        assert created_for_company_two == 1

    # Same company must still reject duplicated barcode.
    post_response_same_company = client.post(
        "/productos/add",
        data={
            "barcode": "DUPLICADO-001",
            "name": "Producto Empresa Dos Duplicado",
            "sale_type": "unidad",
            "unit_measure": "u",
            "price": "210",
            "cost_price": "120",
            "stock": "2",
            "min_stock": "1",
        },
        follow_redirects=False,
    )
    assert post_response_same_company.status_code in (301, 302)

    with stock_app.app.app_context():
        still_one_for_company_two = Product.query.filter_by(company_id=company_two_id, barcode="DUPLICADO-001").count()
        assert still_one_for_company_two == 1

    # Session must remain authenticated; if it was lost this route would redirect to login.
    products_response = client.get("/productos/", follow_redirects=False)
    assert products_response.status_code == 200


def test_checkout_does_not_apply_automatic_tax():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.post(
        "/ventas/api/checkout",
        json={
            "items": [{"productId": 1, "quantity": 1}],
            "metodo_pago": "EFECTIVO",
            "descuento_general": 500,
            "recargo": 200,
        },
    )
    assert response.status_code == 200

    with stock_app.app.app_context():
        from app import Sale

        sale = Sale.query.order_by(Sale.id.desc()).first()
        assert sale is not None
        assert float(sale.subtotal) == 18000.0
        assert float(sale.tax or 0) == 0.0
        assert float(sale.total_amount) == 17700.0


def test_product_price_margin_profit_reciprocal_calculation():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    by_margin_response = client.post(
        "/productos/add",
        data={
            "barcode": "RECIP-001",
            "name": "Producto por margen",
            "sale_type": "unidad",
            "unit_measure": "u",
            "cost_price": "100",
            "profit_percent": "50",
            "stock": "2",
            "min_stock": "1",
        },
        follow_redirects=False,
    )
    assert by_margin_response.status_code in (301, 302)

    by_price_response = client.post(
        "/productos/add",
        data={
            "barcode": "RECIP-002",
            "name": "Producto por precio final",
            "sale_type": "unidad",
            "unit_measure": "u",
            "cost_price": "80",
            "price": "100",
            "stock": "2",
            "min_stock": "1",
        },
        follow_redirects=False,
    )
    assert by_price_response.status_code in (301, 302)

    with stock_app.app.app_context():
        prod_margin = Product.query.filter_by(barcode="RECIP-001").first()
        prod_price = Product.query.filter_by(barcode="RECIP-002").first()
        assert prod_margin is not None
        assert prod_price is not None

        assert float(prod_margin.price) == 150.0
        assert float(prod_margin.margin) == 50.0
        assert float(prod_margin.profit_percent) == 50.0

        assert float(prod_price.price) == 100.0
        assert float(prod_price.margin) == 20.0
        assert float(prod_price.profit_percent) == 25.0


def test_product_edit_reciprocal_calculation():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    with stock_app.app.app_context():
        product = Product.query.filter_by(barcode="123456789012").first()
        assert product is not None
        product_id = product.id

    edit_by_percent = client.post(
        f"/productos/edit/{product_id}",
        data={
            "barcode": "123456789012",
            "name": "Yerba kilo",
            "sale_type": "unidad",
            "unit_measure": "u",
            "cost_price": "200",
            "price": "18000",
            "margin": "100",
            "profit_percent": "50",
            "pricing_source": "profit_percent",
            "tax": "21",
            "stock": "2.5",
            "min_stock": "0.5",
        },
        follow_redirects=False,
    )
    assert edit_by_percent.status_code in (301, 302)

    with stock_app.app.app_context():
        edited = db.session.get(Product, product_id)
        assert edited is not None
        assert float(edited.cost_price) == 200.0
        assert float(edited.price) == 300.0
        assert float(edited.margin) == 100.0
        assert float(edited.profit_percent) == 50.0
        assert float(edited.tax) == 21.0

    edit_by_price = client.post(
        f"/productos/edit/{product_id}",
        data={
            "barcode": "123456789012",
            "name": "Yerba kilo",
            "sale_type": "unidad",
            "unit_measure": "u",
            "cost_price": "200",
            "price": "260",
            "margin": "777",
            "profit_percent": "777",
            "pricing_source": "price",
            "tax": "10.5",
            "stock": "2.5",
            "min_stock": "0.5",
        },
        follow_redirects=False,
    )
    assert edit_by_price.status_code in (301, 302)

    with stock_app.app.app_context():
        edited = db.session.get(Product, product_id)
        assert edited is not None
        assert float(edited.cost_price) == 200.0
        assert float(edited.price) == 260.0
        assert float(edited.margin) == 60.0
        assert float(edited.profit_percent) == 30.0
        assert float(edited.tax) == 10.5

    edit_by_margin = client.post(
        f"/productos/edit/{product_id}",
        data={
            "barcode": "123456789012",
            "name": "Yerba kilo",
            "sale_type": "unidad",
            "unit_measure": "u",
            "cost_price": "200",
            "price": "260",
            "margin": "90",
            "profit_percent": "999",
            "pricing_source": "margin",
            "tax": "0",
            "stock": "2.5",
            "min_stock": "0.5",
        },
        follow_redirects=False,
    )
    assert edit_by_margin.status_code in (301, 302)

    with stock_app.app.app_context():
        edited = db.session.get(Product, product_id)
        assert edited is not None
        assert float(edited.cost_price) == 200.0
        assert float(edited.price) == 290.0
        assert float(edited.margin) == 90.0
        assert float(edited.profit_percent) == 45.0
        assert float(edited.tax) == 0.0


def test_company_can_save_qr_payment_settings():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.post(
        "/admin/payment-qr-settings",
        data={
            "payment_alias": "negocio.demo",
            "payment_cbu": "1234567890123456789012",
            "payment_cvu": "0001234500001234500001",
            "payment_qr_text": "Cobro caja principal",
            "payment_qr_url": "https://example.com/pago",
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        assert company.payment_alias == "negocio.demo"
        assert company.payment_cbu == "1234567890123456789012"
        assert company.payment_cvu == "0001234500001234500001"
        assert company.payment_qr_text == "Cobro caja principal"
        assert company.payment_qr_url == "https://example.com/pago"


def test_security_headers_are_present():
    client = stock_app.app.test_client()

    response = client.get("/auth/login")
    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "Content-Security-Policy" in response.headers
    assert "Permissions-Policy" in response.headers
