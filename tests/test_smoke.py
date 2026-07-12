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
    user = User(username="admin", email="admin@test.local", role="admin")
    user.set_password("admin123")
    db.session.add(user)
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
        )
    )
    db.session.add(Client(name="Cliente demo", email="cliente@test.local", active=True, whatsapp="549111111111"))
    db.session.commit()


def test_core_routes_and_decimal_checkout():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})

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
        "/admin/",
        "/admin/billing",
    ]:
        response = client.get(path)
        assert response.status_code == 200, path

    response = client.post("/ventas/api/checkout", json={"items": [{"productId": 1, "quantity": 0.350}], "metodo_pago": "EFECTIVO"})
    assert response.status_code == 200
    with stock_app.app.app_context():
        product = db.session.get(Product, 1)
        assert round(product.stock, 3) == 2.15


def test_exports_and_security_methods():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    assert client.get("/reportes/ventas.csv").status_code == 200
    assert client.get("/reportes/ventas.xlsx").status_code == 200
    assert client.get("/manifest.json").status_code == 200
    assert client.get("/service-worker.js").status_code == 200
    assert client.get("/api/search?q=Yerba").status_code == 200
    assert client.get("/api/notifications").status_code == 200
    assert client.get("/ventas/api/recent").status_code == 200
    assert client.get("/productos/export.xlsx").status_code == 200
    assert client.get("/productos/1/kardex").status_code == 200
    assert client.get("/admin/metrics.xlsx").status_code == 200
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
