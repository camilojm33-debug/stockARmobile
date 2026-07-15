import os
import re
from datetime import timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest

import app as stock_app
from app import Client, Company, Product, Subscription, User, db
from sqlalchemy.exc import ProgrammingError

try:
    from psycopg2.errors import UndefinedTable as PGUndefinedTable
except ImportError:  # pragma: no cover
    PGUndefinedTable = None


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

    company_admin = User(username="empresa_admin", email="admin@test.local", role="user", company_id=company.id, active=True)
    company_admin.set_password("admin123")
    db.session.add(company_admin)

    business_admin = User(username="negocio_admin", email="negocio_admin@test.local", role="admin", company_id=company.id, active=True)
    business_admin.set_password("admin123")
    db.session.add(business_admin)

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

    response = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": 1, "quantity": 0.350}], "metodo_pago": "EFECTIVO"},
        headers={"X-Cart-Tenant": "1:1"},
    )
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


def test_superadmin_subscriptions_actions_visibility_and_flows():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        plan_basic = Plan(code="basic_sub_test", name="Plan Basic Test", price=1000, currency="ARS", duration_days=30, active=True)
        plan_pro = Plan(code="pro_sub_test", name="Plan Pro Test", price=2000, currency="ARS", duration_days=30, active=True)
        db.session.add(plan_basic)
        db.session.add(plan_pro)
        db.session.flush()

        active_sub = Subscription(
            company_id=company.id,
            plan_id=plan_basic.id,
            status="active",
            renewal_enabled=True,
            auto_renew=True,
            cancel_at_period_end=False,
        )
        suspended_sub = Subscription(
            company_id=company.id,
            plan_id=plan_basic.id,
            status="suspended",
            renewal_enabled=False,
            auto_renew=False,
            cancel_at_period_end=False,
        )
        expired_sub = Subscription(
            company_id=company.id,
            plan_id=plan_basic.id,
            status="expired",
            renewal_enabled=False,
            auto_renew=False,
            cancel_at_period_end=False,
        )
        cancelled_sub = Subscription(
            company_id=company.id,
            plan_id=plan_basic.id,
            status="cancelled",
            renewal_enabled=False,
            auto_renew=False,
            cancel_at_period_end=True,
        )
        db.session.add_all([active_sub, suspended_sub, expired_sub, cancelled_sub])
        db.session.commit()

        plan_basic_id = plan_basic.id
        plan_pro_id = plan_pro.id
        active_id = active_sub.id
        suspended_id = suspended_sub.id
        expired_id = expired_sub.id
        cancelled_id = cancelled_sub.id
        company_id = company.id

    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    panel = client.get("/superadmin/subscriptions")
    assert panel.status_code == 200
    html = panel.data.decode("utf-8")

    active_block = re.search(rf'id="subActions{active_id}".*?</div>', html, re.DOTALL)
    assert active_block is not None
    assert 'data-action="modify"' in active_block.group(0)
    assert 'data-action="suspend"' in active_block.group(0)
    assert 'data-action="cancel"' in active_block.group(0)
    assert 'data-action="reactivate"' not in active_block.group(0)
    assert 'data-action="renew_now"' not in active_block.group(0)

    suspended_block = re.search(rf'id="subActions{suspended_id}".*?</div>', html, re.DOTALL)
    assert suspended_block is not None
    assert 'data-action="reactivate"' in suspended_block.group(0)
    assert 'data-action="modify"' not in suspended_block.group(0)
    assert 'data-action="suspend"' not in suspended_block.group(0)
    assert 'data-action="cancel"' not in suspended_block.group(0)
    assert 'data-action="renew_now"' not in suspended_block.group(0)

    expired_block = re.search(rf'id="subActions{expired_id}".*?</div>', html, re.DOTALL)
    assert expired_block is not None
    assert 'data-action="renew_now"' in expired_block.group(0)
    assert 'data-action="modify"' not in expired_block.group(0)
    assert 'data-action="suspend"' not in expired_block.group(0)
    assert 'data-action="cancel"' not in expired_block.group(0)

    cancelled_block = re.search(rf'id="subActions{cancelled_id}".*?</div>', html, re.DOTALL)
    assert cancelled_block is not None
    assert 'data-action="reactivate"' in cancelled_block.group(0)
    assert 'data-action="renew_now"' in cancelled_block.group(0)
    assert 'data-action="modify"' not in cancelled_block.group(0)
    assert 'data-action="suspend"' not in cancelled_block.group(0)

    assert "¿Cancelar esta suscripción?" in html
    assert "¿Suspender esta suscripción?" in html

    create_resp = client.post(
        "/superadmin/subscriptions/create",
        data={
            "company_id": company_id,
            "plan_id": plan_basic_id,
            "status": "pending",
            "renewal_enabled": "1",
        },
        follow_redirects=True,
    )
    assert create_resp.status_code == 200
    assert "Suscripción creada correctamente." in create_resp.data.decode("utf-8")

    update_resp = client.post(
        f"/superadmin/subscriptions/{active_id}/update",
        data={
            "plan_id": plan_pro_id,
            "status": "active",
            "renewal_enabled": "1",
        },
        follow_redirects=True,
    )
    assert update_resp.status_code == 200
    assert "Suscripción modificada correctamente." in update_resp.data.decode("utf-8")

    suspend_resp = client.post(
        f"/superadmin/subscriptions/{active_id}/action",
        data={"action": "suspend"},
        follow_redirects=True,
    )
    assert suspend_resp.status_code == 200
    assert "Suscripción suspendida." in suspend_resp.data.decode("utf-8")

    invalid_cancel_resp = client.post(
        f"/superadmin/subscriptions/{active_id}/action",
        data={"action": "cancel"},
        follow_redirects=True,
    )
    assert invalid_cancel_resp.status_code == 200
    assert "La acción no está permitida para el estado actual de la suscripción." in invalid_cancel_resp.data.decode("utf-8")

    reactivate_resp = client.post(
        f"/superadmin/subscriptions/{active_id}/action",
        data={"action": "reactivate"},
        follow_redirects=True,
    )
    assert reactivate_resp.status_code == 200
    assert "Suscripción reactivada." in reactivate_resp.data.decode("utf-8")

    cancel_resp = client.post(
        f"/superadmin/subscriptions/{active_id}/action",
        data={"action": "cancel"},
        follow_redirects=True,
    )
    assert cancel_resp.status_code == 200
    assert "Suscripción cancelada." in cancel_resp.data.decode("utf-8")

    renew_resp = client.post(
        f"/superadmin/subscriptions/{active_id}/action",
        data={"action": "renew_now"},
        follow_redirects=True,
    )
    assert renew_resp.status_code == 200
    assert "Suscripción renovada." in renew_resp.data.decode("utf-8")

    with stock_app.app.app_context():
        refreshed = db.session.get(Subscription, active_id)
        assert refreshed is not None
        assert refreshed.status == "active"


def test_superadmin_company_detail_exposes_delete_button():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company_id = company.id

    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    detail = client.get(f"/superadmin/companies/{company_id}")
    assert detail.status_code == 200
    html = detail.data.decode("utf-8")
    assert "Eliminar empresa" in html
    assert f"/superadmin/companies/{company_id}/delete" in html


def test_superadmin_login_survives_admin_bootstrap_with_different_env_owner(monkeypatch):
    with stock_app.app.app_context():
        monkeypatch.setenv("ADMIN_USERNAME", "otro_admin")
        monkeypatch.setenv("ADMIN_EMAIL", "otro_admin@test.local")

        super_user = User.query.filter_by(username="superadmin").first()
        assert super_user is not None
        assert super_user.role == "superadmin"

        stock_app.create_admin_user()

        refreshed = User.query.filter_by(username="superadmin").first()
        assert refreshed is not None
        assert refreshed.role == "superadmin"
        assert refreshed.active is True

    client = stock_app.app.test_client()
    login = client.post("/auth/login", data={"username": "superadmin", "password": "admin123"}, follow_redirects=False)
    assert login.status_code in (301, 302)

    panel = client.get("/superadmin/")
    assert panel.status_code == 200


def test_qr_print_all_supports_square_5x5_a4_format():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.post(
        "/qr/print-all",
        data={
            "label_format": "square_5x5",
            "copies": 1,
        },
    )
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.headers.get("Content-Disposition", "").find("etiquetas_5x5_a4.pdf") >= 0


def test_qr_print_all_supports_selected_and_single_scope():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    selected_response = client.post(
        "/qr/print-all",
        data={
            "label_format": "square_5x5",
            "print_scope": "selected",
            "selected_product_ids": ["1"],
            "copies": 1,
        },
    )
    assert selected_response.status_code == 200
    assert selected_response.mimetype == "application/pdf"

    single_response = client.post(
        "/qr/print-all",
        data={
            "label_format": "square_5x5",
            "print_scope": "single",
            "single_product_id": "1",
            "fill_page": "1",
            "copies": 1,
        },
    )
    assert single_response.status_code == 200
    assert single_response.mimetype == "application/pdf"


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
        headers={"X-Cart-Tenant": "1:1"},
    )
    assert response.status_code == 200

    with stock_app.app.app_context():
        from app import Sale

        sale = Sale.query.order_by(Sale.id.desc()).first()
        assert sale is not None
        assert float(sale.subtotal) == 18000.0
        assert float(sale.tax or 0) == 0.0
        assert float(sale.total_amount) == 17700.0


def test_checkout_rejects_stale_tenant_cart():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": 1, "quantity": 1}], "metodo_pago": "EFECTIVO"},
        headers={"X-Cart-Tenant": "999:999"},
    )
    assert response.status_code == 409


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
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

    response = client.post(
        "/admin/payment-qr-settings",
        data={
            "website": "https://empresa-demo.com",
            "social_facebook": "https://facebook.com/empresa-demo",
            "social_instagram": "https://instagram.com/empresa-demo",
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
        company_id = company.id
        assert company.payment_alias == "negocio.demo"
        assert company.payment_cbu == "1234567890123456789012"
        assert company.payment_cvu == "0001234500001234500001"
        assert company.payment_qr_text == "Cobro caja principal"
        assert company.payment_qr_url == "https://example.com/pago"
        assert company.website == "https://empresa-demo.com"
        assert company.social_facebook == "https://facebook.com/empresa-demo"
        assert company.social_instagram == "https://instagram.com/empresa-demo"


def test_my_company_module_requires_pin_and_shows_tenant_admin_features():
    client = stock_app.app.test_client()
    company_id = None

    with stock_app.app.app_context():
        from app import CashMovement, Company, Sale, utcnow

        company = Company.query.filter_by(name="Empresa Demo").first()
        admin_user = User.query.filter_by(username="negocio_admin").first()
        regular_user = User.query.filter_by(username="empresa_admin").first()
        assert company is not None
        company_id = company.id
        assert admin_user is not None
        assert regular_user is not None
        company.business_pin_hash = None

        db.session.add(
            Sale(
                customer="Cliente demo",
                subtotal=1000,
                discount=0,
                tax=0,
                total_amount=1000,
                payment_method="EFECTIVO",
                seller_id=regular_user.id,
                company_id=company.id,
            )
        )
        db.session.add(
            CashMovement(
                user_id=regular_user.id,
                company_id=company.id,
                movement_type="ingreso",
                category="venta",
                amount=300,
                description="Ingreso prueba",
                created_at=utcnow(),
            )
        )
        db.session.add(
            CashMovement(
                user_id=regular_user.id,
                company_id=company.id,
                movement_type="egreso",
                category="gasto",
                amount=50,
                description="Egreso prueba",
                created_at=utcnow(),
            )
        )
        db.session.commit()

    # Sin PIN asignado por superadmin, no se permite validar acceso.
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    no_pin = client.post("/admin/company-settings/pin/verify", data={"access_pin": "1234"}, follow_redirects=True)
    assert no_pin.status_code == 200
    assert "no esta configurado" in no_pin.data.decode("utf-8").lower()

    client.post("/auth/logout")
    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})
    assign_pin = client.post(f"/superadmin/companies/{company_id}/pin/assign", data={"admin_pin": "1234"}, follow_redirects=True)
    assert assign_pin.status_code == 200
    assert "PIN asignado correctamente" in assign_pin.data.decode("utf-8")

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    locked_page = client.get("/admin/company-settings")
    assert locked_page.status_code == 200
    assert "Validar PIN" in locked_page.data.decode("utf-8")

    bad_pin = client.post("/admin/company-settings/pin/verify", data={"access_pin": "9999"}, follow_redirects=True)
    assert bad_pin.status_code == 200
    assert "PIN incorrecto" in bad_pin.data.decode("utf-8")

    ok_pin = client.post("/admin/company-settings/pin/verify", data={"access_pin": "1234"}, follow_redirects=True)
    assert ok_pin.status_code == 200
    html = ok_pin.data.decode("utf-8")
    assert "Usuarios del negocio" in html
    assert "Caja por usuario" in html
    assert "panel=company" in html
    assert "panel=employees" in html
    assert "panel=schedules" in html
    assert "panel=branches" in html
    assert "panel=security" in html
    assert "panel=general" in html
    assert "panel=stats" in html
    assert "panel=billing" in html

    for panel_name in ["company", "employees", "schedules", "branches", "billing", "security", "general", "stats"]:
        panel_response = client.get(f"/admin/company-settings?panel={panel_name}")
        assert panel_response.status_code == 200

    with stock_app.app.app_context():
        target_user = User.query.filter_by(username="empresa_admin").first()
        assert target_user is not None
        target_user_id = target_user.id

    update_user = client.post(
        f"/admin/company-settings/users/{target_user_id}/update",
        data={"full_name": "Cajero Uno"},
        follow_redirects=True,
    )
    assert update_user.status_code == 200

    toggle_user = client.post(f"/admin/company-settings/users/{target_user_id}/toggle", follow_redirects=True)
    assert toggle_user.status_code == 200
    toggle_user_back = client.post(f"/admin/company-settings/users/{target_user_id}/toggle", follow_redirects=True)
    assert toggle_user_back.status_code == 200

    filtered = client.get("/admin/company-settings?from=2026-01-01&to=2026-12-31")
    assert filtered.status_code == 200
    filtered_html = filtered.data.decode("utf-8")
    assert "1000.00" in filtered_html
    assert "300.00" in filtered_html
    assert "50.00" in filtered_html

    lock_response = client.post("/admin/company-settings/pin/logout", data={"access_pin": "1234"}, follow_redirects=False)
    assert lock_response.status_code in (301, 302)
    assert "/dashboard/" in (lock_response.headers.get("Location") or "")

    # Usuario regular puede acceder al modulo y validar PIN.
    client.post("/auth/logout")
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    user_access = client.get("/admin/company-settings")
    assert user_access.status_code == 200
    assert "Validar PIN" in user_access.data.decode("utf-8")


def test_my_company_module_supports_employee_create_and_reset():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from services.company_security_service import CompanySecurityService
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        company = Company.query.filter_by(name="Empresa Demo").first()
        admin_user = User.query.filter_by(username="negocio_admin").first()
        assert company is not None
        assert admin_user is not None

        CompanySecurityService.set_pin(company, "1234")
        PlanService.ensure_defaults(db.session)
        plan = PlanService.get_plan(code="entrepreneur")
        assert plan is not None
        SubscriptionService.start_or_change_plan(db.session, company=company, plan=plan, user_id=admin_user.id)
        db.session.commit()

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    client.post("/admin/company-settings/pin/verify", data={"access_pin": "1234"}, follow_redirects=True)

    create_user = client.post(
        "/admin/company-settings/users/create",
        data={
            "username": "cajero_nuevo",
            "email": "cajero_nuevo@test.local",
            "full_name": "Cajero Nuevo",
            "role": "user",
        },
        follow_redirects=True,
    )
    assert create_user.status_code == 200

    with stock_app.app.app_context():
        created = User.query.filter_by(username="cajero_nuevo").first()
        assert created is not None
        assert created.role == "user"
        assert created.must_change_password is True
        created_id = created.id

    reset_password = client.post(
        f"/admin/company-settings/users/{created_id}/reset-password",
        follow_redirects=True,
    )
    assert reset_password.status_code == 200
    assert "Contrasena restablecida" in reset_password.data.decode("utf-8")


def test_my_company_module_employee_permissions_delete_and_billing_pdf():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Invoice, Payment
        from services.company_security_service import CompanySecurityService

        company = Company.query.filter_by(name="Empresa Demo").first()
        admin_user = User.query.filter_by(username="negocio_admin").first()
        target_user = User.query.filter_by(username="empresa_admin").first()
        assert company is not None
        assert admin_user is not None
        assert target_user is not None

        CompanySecurityService.set_pin(company, "1234")

        invoice = Invoice(
            company_id=company.id,
            status="issued",
            amount=1500,
            currency="ARS",
            invoice_number=f"INV-TEST-{company.id}",
        )
        db.session.add(invoice)
        db.session.flush()
        payment = Payment(
            company_id=company.id,
            user_id=admin_user.id,
            invoice_id=invoice.id,
            amount=1500,
            currency="ARS",
            status="approved",
            payment_method="transferencia",
            reference="TEST-REF",
        )
        db.session.add(payment)

        deletable = User(
            username="empleado_borrable",
            email="empleado_borrable@test.local",
            company_id=company.id,
            role="user",
            active=True,
            auth_provider="local",
        )
        deletable.set_password("admin123")
        db.session.add(deletable)
        db.session.commit()
        invoice_id = invoice.id
        payment_id = payment.id
        target_user_id = target_user.id
        deletable_id = deletable.id

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    client.post("/admin/company-settings/pin/verify", data={"access_pin": "1234"}, follow_redirects=True)

    set_permissions = client.post(
        f"/admin/company-settings/users/{target_user_id}/permissions",
        data={"permissions": ["sales", "clients"]},
        follow_redirects=True,
    )
    assert set_permissions.status_code == 200

    remove_user = client.post(
        f"/admin/company-settings/users/{deletable_id}/delete",
        follow_redirects=True,
    )
    assert remove_user.status_code == 200

    invoice_pdf = client.get(f"/admin/company-settings/billing/invoice/{invoice_id}/pdf")
    assert invoice_pdf.status_code == 200
    assert invoice_pdf.mimetype == "application/pdf"

    payment_pdf = client.get(f"/admin/company-settings/billing/payment/{payment_id}/pdf")
    assert payment_pdf.status_code == 200
    assert payment_pdf.mimetype == "application/pdf"

    with stock_app.app.app_context():
        target_user = db.session.get(User, target_user_id)
        deleted_user = db.session.get(User, deletable_id)
        assert target_user is not None
        assert target_user.permissions_json is not None
        assert "sales" in target_user.permissions_json
        assert "clients" in target_user.permissions_json
        assert deleted_user is None


def test_my_company_module_role_update_and_schedule_assignment():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from services.company_security_service import CompanySecurityService

        company = Company.query.filter_by(name="Empresa Demo").first()
        admin_user = User.query.filter_by(username="negocio_admin").first()
        target_user = User.query.filter_by(username="empresa_admin").first()
        assert company is not None
        assert admin_user is not None
        assert target_user is not None
        CompanySecurityService.set_pin(company, "1234")
        db.session.commit()
        target_user_id = target_user.id

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    client.post("/admin/company-settings/pin/verify", data={"access_pin": "1234"}, follow_redirects=True)

    role_change = client.post(
        f"/admin/company-settings/users/{target_user_id}/role",
        data={"role": "admin"},
        follow_redirects=True,
    )
    assert role_change.status_code == 200

    assign_schedule = client.post(
        "/admin/company-settings/schedules/assign",
        data={
            "user_id": str(target_user_id),
            "day": "lunes",
            "start": "09:00",
            "end": "13:00",
        },
        follow_redirects=True,
    )
    assert assign_schedule.status_code == 200

    with stock_app.app.app_context():
        target_user = db.session.get(User, target_user_id)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert target_user is not None
        assert target_user.role == "admin"
        assert company is not None
        assert company.schedules_json is not None
        assert "09:00" in company.schedules_json


def test_subscription_option_hidden_for_non_admin_user():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    sidebar = client.get("/dashboard/")
    assert sidebar.status_code == 200
    html = sidebar.data.decode("utf-8")
    assert "Suscripción" not in html

    portal = client.get("/admin/portal")
    assert portal.status_code == 200


def test_my_company_module_blocks_create_when_plan_user_limit_is_reached():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from services.company_security_service import CompanySecurityService
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        company = Company.query.filter_by(name="Empresa Demo").first()
        admin_user = User.query.filter_by(username="negocio_admin").first()
        assert company is not None
        assert admin_user is not None

        CompanySecurityService.set_pin(company, "1234")
        PlanService.ensure_defaults(db.session)
        plan = PlanService.get_plan(code="trial")
        assert plan is not None
        SubscriptionService.start_or_change_plan(db.session, company=company, plan=plan, user_id=admin_user.id)
        db.session.commit()

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    client.post("/admin/company-settings/pin/verify", data={"access_pin": "1234"}, follow_redirects=True)

    create_user = client.post(
        "/admin/company-settings/users/create",
        data={
            "username": "extra_trial",
            "email": "extra_trial@test.local",
            "full_name": "Extra Trial",
            "role": "user",
        },
        follow_redirects=True,
    )
    assert create_user.status_code == 200

    with stock_app.app.app_context():
        denied = User.query.filter_by(username="extra_trial").first()
        assert denied is None


def test_my_company_module_allows_one_time_initial_pin_generation():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        assert not company.business_pin_hash

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    initial_page = client.get("/admin/company-settings")
    assert initial_page.status_code == 200
    assert "Generar PIN inicial" in initial_page.data.decode("utf-8")

    generated = client.post("/admin/company-settings/pin/bootstrap", follow_redirects=True)
    assert generated.status_code == 200
    generated_html = generated.data.decode("utf-8")
    assert "PIN inicial generado" in generated_html
    assert "mostrar solo una vez" in generated_html

    with stock_app.app.app_context():
        company_after = Company.query.filter_by(name="Empresa Demo").first()
        assert company_after is not None
        assert company_after.business_pin_hash is not None

    second_attempt = client.post("/admin/company-settings/pin/bootstrap", follow_redirects=True)
    assert second_attempt.status_code == 200
    assert "ya esta configurado" in second_attempt.data.decode("utf-8")


def test_security_headers_are_present():
    client = stock_app.app.test_client()

    response = client.get("/auth/login")
    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "Content-Security-Policy" in response.headers
    assert "Permissions-Policy" in response.headers


def test_support_ticket_flow_and_temp_password_generation():
    client = stock_app.app.test_client()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    create_ticket = client.post(
        "/soporte/nuevo",
        data={
            "email": "cliente.soporte@test.local",
            "reason": "Problemas con ventas",
            "description": "No puedo cerrar una venta desde checkout.",
        },
        follow_redirects=True,
    )
    assert create_ticket.status_code == 200
    assert "Mis tickets" in create_ticket.data.decode("utf-8")

    with stock_app.app.app_context():
        from app import SupportTicket

        ticket = SupportTicket.query.order_by(SupportTicket.id.desc()).first()
        assert ticket is not None
        assert ticket.reason == "Problemas con ventas"
        assert ticket.status == "pendiente"
        ticket_id = ticket.id

    client.post("/auth/logout")
    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    admin_list = client.get("/soporte/admin")
    assert admin_list.status_code == 200
    assert "Soporte" in admin_list.data.decode("utf-8")

    detail = client.get(f"/soporte/admin/{ticket_id}")
    assert detail.status_code == 200

    generate_temp = client.post(
        f"/soporte/admin/{ticket_id}/temp-password",
        data={"require_password_change": "1"},
        follow_redirects=True,
    )
    assert generate_temp.status_code == 200
    detail_html = generate_temp.data.decode("utf-8")
    assert "Contrasena temporal" in detail_html

    # Visible una sola vez en la siguiente carga.
    second_detail = client.get(f"/soporte/admin/{ticket_id}")
    assert second_detail.status_code == 200
    assert "visible una sola vez" not in second_detail.data.decode("utf-8")

    resolve = client.post(
        f"/soporte/admin/{ticket_id}/resolve",
        data={"resolved_note": "Se reseteo password y se confirmo acceso."},
        follow_redirects=True,
    )
    assert resolve.status_code == 200

    with stock_app.app.app_context():
        from app import SupportTicket

        updated = db.session.get(SupportTicket, ticket_id)
        assert updated is not None
        assert updated.status == "resuelto"


def test_share_whatsapp_keeps_existing_phone_flow():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Sale, SaleItem, utcnow

        seller = User.query.filter_by(username="empresa_admin").first()
        cli = Client.query.filter_by(name="Cliente demo").first()
        prod = Product.query.filter_by(name="Yerba kilo").first()
        assert seller is not None
        assert cli is not None
        assert prod is not None
        cli.whatsapp = "549111111111"

        sale = Sale(
            customer=cli.name,
            subtotal=100,
            discount=0,
            tax=0,
            total_amount=100,
            payment_method="EFECTIVO",
            seller_id=seller.id,
            client_id=cli.id,
            company_id=seller.company_id,
            date=utcnow(),
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(SaleItem(sale_id=sale.id, product_id=prod.id, quantity=1, price=100, cost_price=70, discount=0))
        db.session.commit()
        sale_id = sale.id

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    response = client.get(f"/ventas/{sale_id}/share-whatsapp", follow_redirects=False)
    assert response.status_code in (301, 302)
    location = response.headers.get("Location", "")
    assert location.startswith("https://wa.me/")
    assert "text=" in location


def test_share_whatsapp_shows_dialog_and_allows_send_once_without_saving():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Sale, SaleItem, utcnow

        seller = User.query.filter_by(username="empresa_admin").first()
        cli = Client.query.filter_by(name="Cliente demo").first()
        prod = Product.query.filter_by(name="Yerba kilo").first()
        assert seller is not None
        assert cli is not None
        assert prod is not None
        cli.whatsapp = None
        cli.phone = None

        sale = Sale(
            customer=cli.name,
            subtotal=100,
            discount=0,
            tax=0,
            total_amount=100,
            payment_method="EFECTIVO",
            seller_id=seller.id,
            client_id=cli.id,
            company_id=seller.company_id,
            date=utcnow(),
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(SaleItem(sale_id=sale.id, product_id=prod.id, quantity=1, price=100, cost_price=70, discount=0))
        db.session.commit()
        sale_id = sale.id
        client_id = cli.id

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    dialog = client.get(f"/ventas/{sale_id}/share-whatsapp")
    assert dialog.status_code == 200
    html = dialog.data.decode("utf-8")
    assert "No existe un numero de WhatsApp asociado a esta venta." in html

    invalid = client.post(
        f"/ventas/{sale_id}/share-whatsapp",
        data={"whatsapp_phone": "123", "phone_action": "send_once"},
    )
    assert invalid.status_code == 200
    assert "Numero de WhatsApp invalido" in invalid.data.decode("utf-8")

    send_once = client.post(
        f"/ventas/{sale_id}/share-whatsapp",
        data={"whatsapp_phone": "5491122233344", "phone_action": "send_once"},
        follow_redirects=False,
    )
    assert send_once.status_code in (301, 302)
    assert send_once.headers.get("Location", "").startswith("https://wa.me/5491122233344")

    with stock_app.app.app_context():
        unchanged_client = db.session.get(Client, client_id)
        assert unchanged_client is not None
        assert not (unchanged_client.whatsapp or "").strip()


def test_share_whatsapp_allows_save_and_send():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Sale, SaleItem, utcnow

        seller = User.query.filter_by(username="empresa_admin").first()
        cli = Client.query.filter_by(name="Cliente demo").first()
        prod = Product.query.filter_by(name="Yerba kilo").first()
        assert seller is not None
        assert cli is not None
        assert prod is not None
        cli.whatsapp = None

        sale = Sale(
            customer=cli.name,
            subtotal=100,
            discount=0,
            tax=0,
            total_amount=100,
            payment_method="EFECTIVO",
            seller_id=seller.id,
            client_id=cli.id,
            company_id=seller.company_id,
            date=utcnow(),
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(SaleItem(sale_id=sale.id, product_id=prod.id, quantity=1, price=100, cost_price=70, discount=0))
        db.session.commit()
        sale_id = sale.id
        client_id = cli.id

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    save_and_send = client.post(
        f"/ventas/{sale_id}/share-whatsapp",
        data={"whatsapp_phone": "5491155566677", "phone_action": "save_and_send"},
        follow_redirects=False,
    )
    assert save_and_send.status_code in (301, 302)
    assert save_and_send.headers.get("Location", "").startswith("https://wa.me/5491155566677")

    with stock_app.app.app_context():
        saved_client = db.session.get(Client, client_id)
        assert saved_client is not None
        assert saved_client.whatsapp == "5491155566677"


def test_login_has_no_google_button_and_has_forgot_password_link():
    client = stock_app.app.test_client()
    response = client.get("/auth/login")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "Continuar con Google" not in html
    assert "/auth/google" not in html
    assert "auth.google_login" not in html
    assert "¿Olvidaste tu contrasena?" in html


def test_password_recovery_request_and_superadmin_reset_flow():
    client = stock_app.app.test_client()

    # Usuario solicita recuperacion por correo.
    forgot = client.post(
        "/auth/forgot-password",
        data={"email": "admin@test.local"},
        follow_redirects=True,
    )
    assert forgot.status_code == 200

    with stock_app.app.app_context():
        from app import PasswordRecoveryRequest

        req = PasswordRecoveryRequest.query.order_by(PasswordRecoveryRequest.id.desc()).first()
        assert req is not None
        assert req.status == "pendiente"
        request_id = req.id

    # SuperAdmin visualiza y restablece.
    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})
    panel = client.get("/superadmin/password-recovery")
    assert panel.status_code == 200
    assert "Recuperación de contraseñas" in panel.data.decode("utf-8")

    reset = client.post(
        f"/superadmin/password-recovery/{request_id}/reset",
        follow_redirects=True,
    )
    assert reset.status_code == 200
    reset_html = reset.data.decode("utf-8")
    assert "Contrasena temporal" in reset_html
    match = re.search(r"<code class=\"fs-6\">([^<]+)</code>", reset_html)
    assert match is not None
    temp_password = match.group(1).strip()
    assert temp_password

    # Se muestra una sola vez.
    panel_again = client.get("/superadmin/password-recovery")
    assert panel_again.status_code == 200
    assert "Contrasena temporal" not in panel_again.data.decode("utf-8")

    with stock_app.app.app_context():
        from app import PasswordRecoveryRequest

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None
        assert user.must_change_password is True
        assert user.password_hash != temp_password
        assert user.check_password(temp_password)

        req = db.session.get(PasswordRecoveryRequest, request_id)
        assert req is not None
        assert req.status == "atendida"

    # Usuario inicia con temporal y queda obligado a cambiar contrasena.
    client.post("/auth/logout")
    login_with_temp = client.post(
        "/auth/login",
        data={"username": "empresa_admin", "password": temp_password},
        follow_redirects=False,
    )
    assert login_with_temp.status_code in (301, 302)
    assert "/auth/force-password-change" in (login_with_temp.headers.get("Location") or "")

    blocked_dashboard = client.get("/dashboard/", follow_redirects=False)
    assert blocked_dashboard.status_code in (301, 302)
    assert "/auth/force-password-change" in (blocked_dashboard.headers.get("Location") or "")

    change_password = client.post(
        "/auth/force-password-change",
        data={"new_password": "nueva123", "confirm_password": "nueva123"},
        follow_redirects=False,
    )
    assert change_password.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import PasswordRecoveryRequest

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None
        assert user.must_change_password is False
        assert user.check_password("nueva123")

        req = db.session.get(PasswordRecoveryRequest, request_id)
        assert req is not None
        assert req.status == "cerrada"


def test_referral_user_password_reset_token_flow():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        seller_user = User(username="seller_recovery", email="seller_recovery@test.local", role="seller", active=True)
        seller_user.set_password("inicio123")
        db.session.add(seller_user)
        db.session.commit()

    forgot = client.post(
        "/auth/forgot-password",
        data={"email": "seller_recovery@test.local"},
        follow_redirects=True,
    )
    assert forgot.status_code == 200

    token = stock_app.app.config.get("_LAST_PASSWORD_RESET_TOKEN")
    assert token

    reset_page = client.get(f"/auth/reset-password/{token}")
    assert reset_page.status_code == 200
    assert "Restablecer contrasena" in reset_page.data.decode("utf-8")

    reset_submit = client.post(
        f"/auth/reset-password/{token}",
        data={"new_password": "nueva123", "confirm_password": "nueva123"},
        follow_redirects=True,
    )
    assert reset_submit.status_code == 200
    assert "Contrasena actualizada correctamente" in reset_submit.data.decode("utf-8")

    reused = client.get(f"/auth/reset-password/{token}", follow_redirects=True)
    assert reused.status_code == 200
    assert "invalido o expiro" in reused.data.decode("utf-8")

    login_new_password = client.post(
        "/auth/login",
        data={"username": "seller_recovery", "password": "nueva123"},
        follow_redirects=False,
    )
    assert login_new_password.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import PasswordRecoveryRequest, PasswordResetToken

        user = User.query.filter_by(username="seller_recovery").first()
        assert user is not None
        assert user.must_change_password is False
        assert user.check_password("nueva123")

        token_row = PasswordResetToken.query.filter_by(user_id=user.id).order_by(PasswordResetToken.id.desc()).first()
        assert token_row is not None
        assert token_row.used_at is not None

        req = PasswordRecoveryRequest.query.filter_by(user_id=user.id).order_by(PasswordRecoveryRequest.id.desc()).first()
        assert req is not None
        assert req.status == "cerrada"


def test_landing_and_subscription_use_same_plan_catalog():
    client = stock_app.app.test_client()

    landing = client.get("/")
    assert landing.status_code == 200
    landing_html = landing.data.decode("utf-8")
    for value in [
        "Trial",
        "Emprendedor",
        "Negocio",
        "Premium",
        "12.999",
        "29.999",
        "54.999",
        "Controla tu negocio desde cualquier lugar",
        "Comparación comercial completa",
        "Gana dinero recomendando StockArmobile",
        "Comisión configurada",
        "Prueba StockArmobile GRATIS",
        "Sin tarjeta de crédito",
        "Ya sos cliente? Iniciar sesion y activar Referidos",
        "No sos cliente? Crear cuenta de vendedor",
    ]:
        assert value in landing_html

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    portal = client.get("/admin/portal")
    assert portal.status_code == 200
    portal_html = portal.data.decode("utf-8")
    assert "Uso del plan" in portal_html
    assert "Plan contratado" in portal_html
    assert "Comenzar suscripción" in portal_html


def test_landing_contact_form_endpoint():
    client = stock_app.app.test_client()

    invalid = client.post(
        "/landing/contact",
        data={"name": "", "email": "", "message": ""},
        follow_redirects=True,
    )
    assert invalid.status_code == 200
    assert "Completa nombre, email y mensaje" in invalid.data.decode("utf-8")

    ok = client.post(
        "/landing/contact",
        data={"name": "Lead Demo", "email": "lead@test.com", "message": "Quiero una demo."},
        follow_redirects=True,
    )
    assert ok.status_code == 200
    assert "Gracias por comunicarte con StockArmobile" in ok.data.decode("utf-8")


def test_landing_testimonials_visibility_with_real_data_only():
    client = stock_app.app.test_client()

    empty_state = client.get("/")
    assert empty_state.status_code == 200
    assert "Experiencias reales de clientes" not in empty_state.data.decode("utf-8")

    with stock_app.app.app_context():
        from app import LandingTestimonial

        db.session.add(
            LandingTestimonial(
                author_name="Cliente Real",
                company_name="Tienda Centro",
                quote="Mejoramos el control de ventas y stock en la primera semana.",
                active=True,
            )
        )
        db.session.commit()

    populated_state = client.get("/")
    assert populated_state.status_code == 200
    html = populated_state.data.decode("utf-8")
    assert "Experiencias reales de clientes" in html
    assert "Cliente Real" in html
    assert "Tienda Centro" in html


def test_superadmin_can_update_landing_testimonial():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import LandingTestimonial

        row = LandingTestimonial(
            author_name="Autor Inicial",
            company_name="Empresa Inicial",
            quote="Texto inicial",
            active=True,
        )
        db.session.add(row)
        db.session.commit()
        testimonial_id = row.id

    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})
    updated = client.post(
        f"/superadmin/landing/testimonials/{testimonial_id}/update",
        data={
            "author_name": "Autor Editado",
            "company_name": "Empresa Editada",
            "quote": "Texto editado real",
            "active": "0",
        },
        follow_redirects=True,
    )
    assert updated.status_code == 200
    assert "Testimonio actualizado correctamente" in updated.data.decode("utf-8")

    with stock_app.app.app_context():
        from app import LandingTestimonial

        refreshed = LandingTestimonial.query.filter_by(id=testimonial_id).first()
        assert refreshed is not None
        assert refreshed.author_name == "Autor Editado"
        assert refreshed.company_name == "Empresa Editada"
        assert refreshed.quote == "Texto editado real"
        assert refreshed.active is False


@pytest.mark.skipif(PGUndefinedTable is None, reason="psycopg2 UndefinedTable no disponible")
def test_landing_survives_optional_referral_ranking_table_missing(monkeypatch):
    client = stock_app.app.test_client()
    original_query = stock_app.db.session.query

    def broken_query(*args, **kwargs):
        if args and args[0] is stock_app.ReferralSeller:
            raise ProgrammingError("SELECT ...", {}, PGUndefinedTable("relation referral_sellers does not exist"))
        return original_query(*args, **kwargs)

    monkeypatch.setattr(stock_app.db.session, "query", broken_query)

    response = client.get("/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "Top referidores" in html
    assert "Aún no hay datos suficientes para mostrar ranking" in html


@pytest.mark.skipif(PGUndefinedTable is None, reason="psycopg2 UndefinedTable no disponible")
def test_landing_survives_optional_testimonials_table_missing(monkeypatch):
    client = stock_app.app.test_client()

    class BrokenTestimonialsQuery:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def all(self):
            raise ProgrammingError("SELECT ...", {}, PGUndefinedTable("relation landing_testimonials does not exist"))

    with stock_app.app.app_context():
        monkeypatch.setattr(stock_app.LandingTestimonial, "query", BrokenTestimonialsQuery(), raising=False)

    response = client.get("/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "Experiencias reales de clientes" not in html


def test_plan_limits_block_create_products_and_clients_without_breaking_portal():
    from services.plan_service import PlanService

    client = stock_app.app.test_client()
    with stock_app.app.app_context():
        from app import Plan

        PlanService.ensure_defaults(db.session)
        trial = Plan.query.filter_by(code="trial").first()
        assert trial is not None
        trial.max_products = 1
        trial.max_clients = 1
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    product_response = client.post(
        "/productos/add",
        data={
            "barcode": "LIM-001",
            "name": "Producto limite",
            "sale_type": "unidad",
            "unit_measure": "u",
            "price": "100",
            "cost_price": "50",
            "stock": "1",
            "min_stock": "1",
            "pricing_source": "price",
        },
        follow_redirects=True,
    )
    assert product_response.status_code == 200
    product_html = product_response.data.decode("utf-8")
    assert "Has alcanzado el limite de productos permitido por tu plan" in product_html

    client_response = client.post(
        "/clientes/add",
        data={
            "name": "Cliente limite",
            "email": "limite@test.local",
        },
        follow_redirects=True,
    )
    assert client_response.status_code == 200
    client_html = client_response.data.decode("utf-8")
    assert "Has alcanzado el limite de clientes permitido por tu plan" in client_html

    portal = client.get("/admin/portal")
    assert portal.status_code == 200
    portal_html = portal.data.decode("utf-8")
    assert "Está próximo a alcanzar el límite de su plan." in portal_html
    assert "Por el crecimiento de su negocio le recomendamos actualizar al Plan" in portal_html


def test_trial_has_priority_over_pending_subscription_and_keeps_dashboard_open():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService

        PlanService.ensure_defaults(db.session)
        trial_plan = Plan.query.filter_by(code="trial").first()
        assert trial_plan is not None

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company.trial_ends_at = stock_app.utcnow() + timedelta(days=3)

        pending_plan = Plan.query.filter_by(code="entrepreneur").first()
        if pending_plan is None:
            pending_plan = Plan(code="entrepreneur", name="Emprendedor", price=12999, currency="ARS", duration_days=30, active=True)
            db.session.add(pending_plan)
            db.session.flush()

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id, plan_id=pending_plan.id, status="pending")
            db.session.add(subscription)
        subscription.plan_id = pending_plan.id
        subscription.status = "pending"
        subscription.trial_end = company.trial_ends_at
        subscription.start_date = stock_app.utcnow()
        subscription.starts_at = stock_app.utcnow()
        subscription.ends_at = company.trial_ends_at
        subscription.next_billing_date = company.trial_ends_at
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    dashboard = client.get("/dashboard/", follow_redirects=False)
    assert dashboard.status_code == 200
    assert "Estado de acceso" not in dashboard.data.decode("utf-8")


def test_expired_trial_allows_subscription_portal_and_blocks_dashboard():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService

        PlanService.ensure_defaults(db.session)

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company.trial_ends_at = stock_app.utcnow() - timedelta(days=1)

        pending_plan = Plan.query.filter_by(code="entrepreneur").first()
        assert pending_plan is not None

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id, plan_id=pending_plan.id, status="pending")
            db.session.add(subscription)
        subscription.plan_id = pending_plan.id
        subscription.status = "pending"
        subscription.trial_end = company.trial_ends_at
        subscription.start_date = stock_app.utcnow() - timedelta(days=5)
        subscription.starts_at = stock_app.utcnow() - timedelta(days=5)
        subscription.ends_at = stock_app.utcnow() - timedelta(days=1)
        subscription.next_billing_date = stock_app.utcnow() - timedelta(days=1)
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    blocked_dashboard = client.get("/dashboard/", follow_redirects=False)
    assert blocked_dashboard.status_code in (301, 302)
    assert "/access-status" in (blocked_dashboard.headers.get("Location") or "")

    portal = client.get("/admin/portal")
    assert portal.status_code == 200
    portal_html = portal.data.decode("utf-8")
    assert "Uso del plan" in portal_html
    assert "Comenzar suscripcion" in portal_html or "Plan actual" in portal_html

    status_page = client.get("/access-status")
    assert status_page.status_code == 200
    status_html = status_page.data.decode("utf-8")
    assert "Pago pendiente" in status_html or "Tu prueba finalizo" in status_html


def test_referral_capture_and_register_attribution_flow():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Company, ReferralSeller

        seller_company = Company(name="Empresa Seller", active=True)
        db.session.add(seller_company)
        db.session.flush()

        seller_user = User(
            username="seller_user",
            email="seller@test.local",
            role="seller",
            company_id=seller_company.id,
            active=True,
        )
        seller_user.set_password("seller123")
        db.session.add(seller_user)
        db.session.flush()

        seller_profile = ReferralSeller(
            user_id=seller_user.id,
            dni="30111222",
            referral_code="REF7777",
            referral_url="https://stockarmobile.com/?ref=REF7777",
            active=True,
        )
        db.session.add(seller_profile)
        db.session.commit()

    landing = client.get("/?ref=ref7777")
    assert landing.status_code == 200
    set_cookie = landing.headers.get("Set-Cookie", "")
    assert "stockarmobile_ref=REF7777" in set_cookie

    register = client.post(
        "/auth/register",
        data={
            "username": "nuevo_ref",
            "email": "nuevo_ref@test.com",
            "password": "nuevo123",
            "selected_plan": "trial",
        },
        follow_redirects=False,
    )
    assert register.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import Company, ReferralAttribution

        company = Company.query.filter_by(name="Empresa nuevo_ref").first()
        assert company is not None
        attribution = ReferralAttribution.query.filter_by(company_id=company.id).first()
        assert attribution is not None
        assert attribution.referral_code == "REF7777"


def test_referral_commission_lifecycle_and_payout_are_persistent():
    with stock_app.app.app_context():
        from app import Company, Plan, ReferralAttribution, ReferralCommission, ReferralPayout, ReferralSeller, utcnow
        from services.plan_service import PlanService
        from services.referral_service import ReferralService

        PlanService.ensure_defaults(db.session)

        seller_company = Company(name="Empresa Seller 2", active=True)
        referred_company = Company(name="Empresa Referida", active=True)
        db.session.add_all([seller_company, referred_company])
        db.session.flush()

        seller_user = User(
            username="seller_lifecycle",
            email="seller_lifecycle@test.local",
            role="seller",
            company_id=seller_company.id,
            active=True,
        )
        seller_user.set_password("seller123")
        db.session.add(seller_user)
        db.session.flush()

        profile = ReferralSeller(
            user_id=seller_user.id,
            dni="30999888",
            referral_code="REF8888",
            referral_url="https://stockarmobile.com/?ref=REF8888",
            active=True,
        )
        db.session.add(profile)
        db.session.flush()

        attribution = ReferralAttribution(
            seller_id=profile.id,
            company_id=referred_company.id,
            user_id=seller_user.id,
            referral_code="REF8888",
        )
        db.session.add(attribution)
        db.session.flush()

        paid_plan = Plan.query.filter_by(code="entrepreneur").first()
        assert paid_plan is not None

        commission = ReferralService.create_commission_for_sale(
            db.session,
            company_id=referred_company.id,
            payment=None,
            subscription=None,
            plan=paid_plan,
        )
        assert commission is not None
        assert float(commission.commission_amount) > 0
        assert commission.status == "pendiente"

        commission.available_at = utcnow() - timedelta(days=1)
        ReferralService.refresh_commission_states(db.session)
        assert commission.status == "disponible"

        superadmin = User.query.filter_by(username="superadmin").first()
        assert superadmin is not None

        payout = ReferralService.register_payout(
            db.session,
            seller_id=profile.id,
            commission_ids=[commission.id],
            processed_by_user_id=superadmin.id,
            transfer_date=utcnow(),
            receipt="comp-001",
            transfer_number="tx-001",
            observations="Pago de prueba",
        )
        db.session.commit()

        persisted_commission = ReferralCommission.query.filter_by(id=commission.id).first()
        persisted_payout = ReferralPayout.query.filter_by(id=payout.id).first()
        assert persisted_commission is not None
        assert persisted_commission.status == "pagada"
        assert persisted_payout is not None
        assert float(persisted_payout.amount) == float(persisted_commission.commission_amount)


def test_referral_role_isolation_between_seller_and_superadmin():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Company, ReferralSeller

        seller_company = Company(name="Empresa Seller 3", active=True)
        db.session.add(seller_company)
        db.session.flush()

        seller_user = User(
            username="seller_portal",
            email="seller_portal@test.local",
            role="seller",
            company_id=seller_company.id,
            active=True,
        )
        seller_user.set_password("seller123")
        db.session.add(seller_user)
        db.session.flush()

        db.session.add(
            ReferralSeller(
                user_id=seller_user.id,
                dni="30123123",
                referral_code="REF1234",
                referral_url="https://stockarmobile.com/?ref=REF1234",
                active=True,
            )
        )
        db.session.commit()

    client.post("/auth/login", data={"username": "seller_portal", "password": "seller123"})
    seller_portal = client.get("/referidos")
    assert seller_portal.status_code == 200

    seller_forbidden_admin = client.get("/superadmin/referrals")
    assert seller_forbidden_admin.status_code == 403

    client.post("/auth/logout")
    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    admin_referrals = client.get("/superadmin/referrals")
    assert admin_referrals.status_code == 200

    admin_forbidden_seller = client.get("/referidos")
    assert admin_forbidden_seller.status_code == 403


def test_existing_customer_can_activate_referrals_without_duplicate_account():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Company, ReferralSeller

        target_user = User.query.filter_by(username="empresa_admin").first()
        assert target_user is not None
        assert target_user.role == "user"
        assert target_user.email == "admin@test.local"
        assert ReferralSeller.query.filter_by(user_id=target_user.id).first() is None

        base_user_count = User.query.count()
        base_company_count = Company.query.count()

    register_attempt = client.post(
        "/auth/register",
        data={
            "username": "vendedor_existente",
            "email": "admin@test.local",
            "password": "seller123",
            "selected_plan": "trial",
            "mode": "seller",
        },
        follow_redirects=False,
    )
    assert register_attempt.status_code in (301, 302)
    location = register_attempt.headers.get("Location") or ""
    assert "/auth/login" in location

    login = client.post(
        "/auth/login?next=/referidos/activar",
        data={"username": "empresa_admin", "password": "admin123"},
        follow_redirects=False,
    )
    assert login.status_code in (301, 302)
    assert "/referidos/activar" in (login.headers.get("Location") or "")

    activate = client.post("/referidos/activar", data={"dni": "30123456"}, follow_redirects=True)
    assert activate.status_code == 200
    assert "Panel de Referidos" in activate.data.decode("utf-8")

    with stock_app.app.app_context():
        from app import Company, ReferralSeller

        target_user = User.query.filter_by(username="empresa_admin").first()
        assert target_user is not None
        assert User.query.filter_by(email="admin@test.local").count() == 1
        assert User.query.count() == base_user_count
        assert Company.query.count() == base_company_count

        seller_profile = ReferralSeller.query.filter_by(user_id=target_user.id).first()
        assert seller_profile is not None
        assert seller_profile.referral_code.startswith("REF")


def test_webhook_approved_activates_subscription_and_creates_commission_automatically(monkeypatch):
    from services.subscription_service import SubscriptionService
    from services.webhook_service import WebhookService

    with stock_app.app.app_context():
        from app import Payment, Plan, ReferralAttribution, ReferralCommission, ReferralSeller, User, WebhookEvent

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        seller_user = User(username="seller_demo", email="seller_demo@test.local", role="seller", active=True)
        seller_user.set_password("seller123")
        db.session.add(seller_user)
        db.session.flush()

        seller = ReferralSeller(
            user_id=seller_user.id,
            referral_code="REF9999",
            referral_url="https://stockarmobile.com/?ref=REF9999",
            active=True,
            dni="12345678",
        )
        db.session.add(seller)
        db.session.flush()

        db.session.add(
            ReferralAttribution(
                seller_id=seller.id,
                company_id=company.id,
                user_id=user.id,
                referral_code="REF9999",
            )
        )

        plan = Plan(code="biz_mp", name="Negocio MP", price=12999, currency="ARS", duration_days=30, max_users=5, max_products=5000, max_clients=5000, active=True)
        db.session.add(plan)
        db.session.flush()

        subscription = SubscriptionService.start_or_change_plan(db.session, company=company, plan=plan, user_id=user.id)
        db.session.flush()

        subscription.external_reference = (
            f"company_id:{company.id}|plan_id:{plan.id}|subscription_id:{subscription.id}|"
            f"user_id:{user.id}|ts:123"
        )
        db.session.commit()

        approved_payload = {
            "id": "evt-1",
            "type": "payment",
            "data": {"id": "mp-pay-1"},
        }
        approved_payment = {
            "id": "mp-pay-1",
            "status": "approved",
            "date_last_updated": "2026-07-14T10:00:00Z",
            "date_approved": "2026-07-14T10:00:00Z",
            "transaction_amount": 12999,
            "currency_id": "ARS",
            "payment_method_id": "visa",
            "external_reference": subscription.external_reference,
            "metadata": {
                "company_id": company.id,
                "subscription_id": subscription.id,
                "plan_id": plan.id,
                "user_id": user.id,
            },
        }

        service = WebhookService()
        monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
        monkeypatch.setattr(service.mp_service, "get_payment", lambda payment_id: approved_payment)

        result = service.process(
            db_session=db.session,
            headers={"x-request-id": "rq-1", "x-signature": "ts=1,v1=abc"},
            payload=approved_payload,
        )

        assert result["status"] == "processed"

        db.session.refresh(subscription)
        assert subscription.status == "active"
        assert subscription.start_date is not None
        assert subscription.ends_at is not None
        assert subscription.next_billing_date is not None
        assert subscription.renewal_enabled is True

        payment_row = Payment.query.filter_by(payment_id="mp-pay-1").first()
        assert payment_row is not None
        assert payment_row.status == "approved"

        commission = ReferralCommission.query.filter_by(payment_id=payment_row.id).first()
        assert commission is not None
        assert float(commission.commission_percent) == 0.3

        duplicate = service.process(
            db_session=db.session,
            headers={"x-request-id": "rq-1", "x-signature": "ts=1,v1=abc"},
            payload=approved_payload,
        )
        assert duplicate["status"] == "duplicate"

        assert Payment.query.filter_by(payment_id="mp-pay-1").count() == 1
        assert ReferralCommission.query.filter_by(payment_id=payment_row.id).count() == 1
        assert WebhookEvent.query.count() == 1


def test_webhook_pending_or_rejected_does_not_activate_subscription(monkeypatch):
    from services.subscription_service import SubscriptionService
    from services.webhook_service import WebhookService

    with stock_app.app.app_context():
        from app import Payment, Plan, User

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        plan = Plan(code="negocio_pending", name="Negocio Pending", price=12999, currency="ARS", duration_days=30, active=True)
        db.session.add(plan)
        db.session.flush()

        subscription = SubscriptionService.start_or_change_plan(db.session, company=company, plan=plan, user_id=user.id)
        db.session.flush()
        subscription.external_reference = (
            f"company_id:{company.id}|plan_id:{plan.id}|subscription_id:{subscription.id}|"
            f"user_id:{user.id}|ts:124"
        )
        db.session.commit()

        service = WebhookService()
        monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)

        pending_payment = {
            "id": "mp-pay-pending",
            "status": "pending",
            "date_last_updated": "2026-07-14T10:00:00Z",
            "transaction_amount": 12999,
            "currency_id": "ARS",
            "external_reference": subscription.external_reference,
            "metadata": {
                "company_id": company.id,
                "subscription_id": subscription.id,
                "plan_id": plan.id,
                "user_id": user.id,
            },
        }
        monkeypatch.setattr(service.mp_service, "get_payment", lambda payment_id: pending_payment)

        pending_result = service.process(
            db_session=db.session,
            headers={"x-request-id": "rq-2", "x-signature": "ts=1,v1=abc"},
            payload={"id": "evt-2", "type": "payment", "data": {"id": "mp-pay-pending"}},
        )
        assert pending_result["status"] == "processed"

        db.session.refresh(subscription)
        assert subscription.status == "pending"

        payment_row = Payment.query.filter_by(payment_id="mp-pay-pending").first()
        assert payment_row is not None
        assert payment_row.status == "pending"
