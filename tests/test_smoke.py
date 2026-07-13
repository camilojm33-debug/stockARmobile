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
    stock_app.ensure_database_schema()
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
