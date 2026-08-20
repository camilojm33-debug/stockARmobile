import os
import gzip
import io
import json
from decimal import Decimal
from urllib.parse import unquote
import re
from datetime import timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("MP_OAUTH_ENCRYPTION_KEY", "test-oauth-encryption-key")

import pytest
from flask_login import login_user, logout_user
from sqlite_test_db import clear_test_data

import app as stock_app
from app import CashMovement, CashSession, Client, Company, Product, Quote, ReferralAttribution, SaaSAlert, SaaSLead, SaaSTask, Sale, SaleItem, SaleModificationHistory, Subscription, User, db
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
        db.session.rollback()
        db.session.remove()
        clear_test_data(db)
        seed()
        yield
        db.session.rollback()
        db.session.remove()
        clear_test_data(db)
        db.session.remove()


def seed():
    company = Company(
        name="Empresa Demo",
        active=True,
        trial_ends_at=stock_app.utcnow() + timedelta(days=10),
    )
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


def open_cash_session(client, opening_amount="0"):
    response = client.post(
        "/caja/",
        data={"action": "open", "opening_amount": opening_amount, "note": "Caja de prueba"},
        follow_redirects=True,
    )
    assert response.status_code == 200


def enable_quotes_module(company, *, enabled=True):
    company.preferences_json = json.dumps({"quotes_module_enabled": enabled})
    db.session.commit()


def grant_quote_permissions(user):
    user.permissions_json = json.dumps([
        "quotes_view",
        "quotes_create",
        "quotes_edit",
        "quotes_delete",
        "quotes_convert",
        "quotes_print",
    ])
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
        "/caja/",
        "/gastos/",
        "/reportes/",
        "/admin/portal",
    ]:
        response = client.get(path)
        assert response.status_code == 200, path

    assert client.get("/compras/").status_code == 403

    client.post("/auth/logout")
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    assert client.get("/compras/").status_code == 200

    open_cash_session(client)

    response = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": 1, "quantity": 0.350}], "metodo_pago": "EFECTIVO"},
        headers={"X-Cart-Tenant": "1:2"},
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
    assert client.get("/superadmin/crm").status_code == 200
    superadmin_dashboard = client.get("/dashboard/", follow_redirects=False)
    assert superadmin_dashboard.status_code in (301, 302)


def test_superadmin_crm_center_creates_and_updates_items():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    crm_page = client.get("/superadmin/crm")
    assert crm_page.status_code == 200

    lead_response = client.post(
        "/superadmin/crm",
        data={
            "entity": "lead",
            "company_name": "Nuevo cliente",
            "contact_name": "Ana Demo",
            "email": "ana@test.local",
            "phone": "5491111222233",
            "source": "whatsapp",
            "status": "nuevo",
            "priority": "alta",
            "notes": "Interesada en demo.",
        },
        follow_redirects=False,
    )
    assert lead_response.status_code in (302, 303)

    with stock_app.app.app_context():
        lead = SaaSLead.query.order_by(SaaSLead.id.desc()).first()
        assert lead is not None
        assert lead.company_name == "Nuevo cliente"
        lead_id = lead.id

    update_response = client.post(
        f"/superadmin/crm/leads/{lead_id}/status",
        data={"status": "ganado"},
        follow_redirects=False,
    )
    assert update_response.status_code in (302, 303)

    with stock_app.app.app_context():
        lead = db.session.get(SaaSLead, lead_id)
        assert lead is not None
        assert lead.status == "ganado"
        assert lead.converted_at is not None

    task_response = client.post(
        "/superadmin/crm",
        data={
            "entity": "task",
            "title": "Llamar al prospecto",
            "description": "Coordinar demo comercial.",
            "status": "pendiente",
            "priority": "media",
            "lead_id": lead_id,
        },
        follow_redirects=False,
    )
    assert task_response.status_code in (302, 303)

    alert_response = client.post(
        "/superadmin/crm",
        data={
            "entity": "alert",
            "title": "Seguimiento comercial",
            "message": "Pendiente de confirmar demo.",
            "severity": "media",
            "status": "abierta",
            "lead_id": lead_id,
        },
        follow_redirects=False,
    )
    assert alert_response.status_code in (302, 303)

    with stock_app.app.app_context():
        task = SaaSTask.query.order_by(SaaSTask.id.desc()).first()
        alert = SaaSAlert.query.order_by(SaaSAlert.id.desc()).first()
        assert task is not None
        assert alert is not None
        assert task.lead_id == lead_id
        assert alert.lead_id == lead_id


def test_login_and_dashboard_survive_missing_notification_table():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        db.session.execute(db.text("DROP TABLE notification_read_states"))
        db.session.commit()

    login_response = client.post(
        "/auth/login",
        data={"username": "empresa_admin", "password": "admin123"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200
    assert "Panel principal" in login_response.data.decode("utf-8")


def test_sales_pages_render_search_and_notifications_components():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.get("/ventas/")
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert 'id="spotlightModal"' in html
    assert 'id="notificationCenter"' in html


def test_checkout_requires_open_cash_session():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": 1, "quantity": 1}], "metodo_pago": "EFECTIVO"},
        headers={"X-Cart-Tenant": "1:1"},
    )
    assert response.status_code == 409
    assert "Debes abrir una caja antes de comenzar a vender." in response.get_json()["error"]


def test_cash_session_links_sales_and_movement():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    open_cash_session(client, opening_amount="100")

    response = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": 1, "quantity": 1}], "metodo_pago": "EFECTIVO", "monto_pago": 18000},
        headers={"X-Cart-Tenant": "1:1"},
    )
    assert response.status_code == 200

    with stock_app.app.app_context():
        sale = Sale.query.order_by(Sale.id.desc()).first()
        assert sale is not None
        assert sale.cash_session_id is not None
        session = CashSession.query.get(sale.cash_session_id)
        assert session is not None
        assert session.status == "abierta"
        movement = CashMovement.query.filter_by(sale_id=sale.id).first()
        assert movement is not None
        assert float(movement.amount) == 18000.0

    close_response = client.post(
        "/caja/",
        data={"action": "close", "counted_amount": "18100", "closing_note": "Cierre de prueba"},
        follow_redirects=True,
    )
    assert close_response.status_code == 200

    with stock_app.app.app_context():
        session = CashSession.query.order_by(CashSession.id.desc()).first()
        assert session is not None
        assert session.status == "cerrada"
        assert float(session.expected_amount) == 18100.0
        assert float(session.difference_amount) == 0.0


def test_cash_close_breakdown_counts_tarjeta_sales():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    open_cash_session(client, opening_amount="100")

    response = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": 1, "quantity": 1}], "metodo_pago": "TARJETA"},
        headers={"X-Cart-Tenant": "1:1"},
    )
    assert response.status_code == 200

    cash_page = client.get("/caja/")
    assert cash_page.status_code == 200
    html = cash_page.data.decode("utf-8")
    assert "Ventas débito" in html
    assert "$18000.00" in html


def test_device_clock_and_cash_midnight_reminder_are_rendered():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    open_cash_session(client)

    products_page = client.get("/productos/")
    assert products_page.status_code == 200
    assert "data-device-clock" in products_page.data.decode("utf-8")

    cash_page = client.get("/caja/")
    assert cash_page.status_code == 200
    html = cash_page.data.decode("utf-8")
    assert "cash-midnight-reminder" in html
    assert "stockarmobile:device-clock" in html


def test_sale_persists_required_comprobante_fields():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    open_cash_session(client)

    response = client.post(
        "/ventas/api/checkout",
        json={
            "items": [{"productId": 1, "quantity": 1}],
            "metodo_pago": "EFECTIVO",
            "client_id": 1,
            "requiere_comprobante": True,
            "tipo_comprobante": "factura_c",
            "observacion_comprobante": "Solicita CUIT en cabecera",
        },
        headers={"X-Cart-Tenant": "1:1"},
    )
    assert response.status_code == 200

    with stock_app.app.app_context():
        sale = Sale.query.order_by(Sale.id.desc()).first()
        assert sale is not None
        assert sale.requiere_comprobante is True
        assert sale.tipo_comprobante == "factura_c"
        assert sale.observacion_comprobante == "Solicita CUIT en cabecera"
        assert sale.comprobante_emitido is False


def test_sale_infers_comprobante_request_from_document_type():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    open_cash_session(client)

    response = client.post(
        "/ventas/api/checkout",
        json={
            "items": [{"productId": 1, "quantity": 1}],
            "metodo_pago": "EFECTIVO",
            "document_type": "factura_a",
            "client_id": 1,
        },
        headers={"X-Cart-Tenant": "1:1"},
    )
    assert response.status_code == 200

    with stock_app.app.app_context():
        sale = Sale.query.order_by(Sale.id.desc()).first()
        assert sale is not None
        assert sale.document_type == "factura_a"
        assert sale.requiere_comprobante is True
        assert sale.tipo_comprobante == "factura_a"


def test_sale_blocks_factura_without_client_selection():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    open_cash_session(client)

    response = client.post(
        "/ventas/api/checkout",
        json={
            "items": [{"productId": 1, "quantity": 1}],
            "metodo_pago": "EFECTIVO",
            "document_type": "factura_b",
            "client_id": "",
        },
        headers={"X-Cart-Tenant": "1:1"},
    )
    assert response.status_code == 400
    assert "debés seleccionar un cliente" in (response.get_json().get("error") or "")


def test_quick_create_client_api_creates_and_returns_client():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.post(
        "/clientes/api/quick-create",
        json={"name": "Cliente Express", "email": "express@test.local", "whatsapp": "549111111111"},
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload
    assert payload.get("client", {}).get("name") == "Cliente Express"

    with stock_app.app.app_context():
        created = Client.query.filter_by(email="express@test.local").first()
        assert created is not None
        assert created.name == "Cliente Express"


def test_cash_page_shows_pending_comprobantes_card():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    open_cash_session(client)

    response = client.post(
        "/ventas/api/checkout",
        json={
            "items": [{"productId": 1, "quantity": 1}],
            "metodo_pago": "EFECTIVO",
            "client_id": 1,
            "requiere_comprobante": True,
            "tipo_comprobante": "factura_b",
        },
        headers={"X-Cart-Tenant": "1:1"},
    )
    assert response.status_code == 200

    cash_page = client.get("/caja/")
    assert cash_page.status_code == 200
    html = cash_page.data.decode("utf-8")
    assert "Comprobantes pendientes" in html
    assert "#1" in html
    assert "Factura B" in html
    assert "$18000.00" in html


def test_mark_sale_comprobante_as_issued():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    open_cash_session(client)

    response = client.post(
        "/ventas/api/checkout",
        json={
            "items": [{"productId": 1, "quantity": 1}],
            "metodo_pago": "EFECTIVO",
            "client_id": 1,
            "requiere_comprobante": True,
            "tipo_comprobante": "factura_a",
        },
        headers={"X-Cart-Tenant": "1:1"},
    )
    assert response.status_code == 200
    sale_id = response.get_json()["sale_id"]

    mark_response = client.post(f"/ventas/{sale_id}/comprobante-emitido", follow_redirects=True)
    assert mark_response.status_code == 200

    with stock_app.app.app_context():
        sale = db.session.get(Sale, sale_id)
        assert sale is not None
        assert sale.comprobante_emitido is True


def test_final_audit_full_sales_flow_consistency():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

    with stock_app.app.app_context():
        second_product = Product(
            barcode="AUD-0002",
            name="Producto auditoria 2",
            price=5000,
            cost_price=2500,
            stock=10,
            min_stock=1,
            active=True,
            sale_type="unidad",
            unit_measure="u",
            company_id=1,
        )
        db.session.add(second_product)
        db.session.commit()
        second_product_id = second_product.id

    open_cash_session(client, opening_amount="100")

    # 1) Venta simple de un producto.
    sale_one_response = client.post(
        "/ventas/api/checkout",
        json={
            "items": [{"productId": 1, "quantity": 1}],
            "metodo_pago": "EFECTIVO",
            "checkout_token": "audit-flow-sale-1",
            "monto_pago": 20000,
        },
        headers={"X-Cart-Tenant": "1:2"},
    )
    assert sale_one_response.status_code == 200
    sale_one_id = sale_one_response.get_json()["sale_id"]

    # Reintento con mismo token: no debe crear venta duplicada.
    sale_one_retry = client.post(
        "/ventas/api/checkout",
        json={
            "items": [{"productId": 1, "quantity": 1}],
            "metodo_pago": "EFECTIVO",
            "checkout_token": "audit-flow-sale-1",
            "monto_pago": 20000,
        },
        headers={"X-Cart-Tenant": "1:2"},
    )
    assert sale_one_retry.status_code == 200
    assert sale_one_retry.get_json()["sale_id"] == sale_one_id

    # 2 y 3) Segunda venta con varios productos y cantidades modificadas.
    sale_two_response = client.post(
        "/ventas/api/checkout",
        json={
            "items": [
                {"productId": 1, "quantity": 0.75},
                {"productId": second_product_id, "quantity": 2},
            ],
            "metodo_pago": "EFECTIVO",
            "checkout_token": "audit-flow-sale-2",
            "descuento_general": 100,
            "recargo": 50,
            "monto_pago": 25000,
        },
        headers={"X-Cart-Tenant": "1:2"},
    )
    assert sale_two_response.status_code == 200
    sale_two_id = sale_two_response.get_json()["sale_id"]

    # 4) Cancelar venta 1.
    cancel_response = client.post(f"/ventas/{sale_one_id}/delete", follow_redirects=False)
    assert cancel_response.status_code in (301, 302)

    with stock_app.app.app_context():
        confirmed_sales = Sale.query.filter_by(company_id=1, status="confirmada").order_by(Sale.id.asc()).all()
        assert len(confirmed_sales) == 1
        assert confirmed_sales[0].id == sale_two_id

        # 7 y 8) SaleItem y Sale.total consistentes.
        sale_two = confirmed_sales[0]
        assert len(sale_two.items) == 2
        sale_two_items_total = sum(float(item.total_amount or 0) for item in sale_two.items)
        assert round(float(sale_two.total_amount or 0), 2) == round(sale_two_items_total, 2)

        # 6) Stock final consistente con ventas confirmadas (venta 1 fue cancelada).
        product_one = Product.query.get(1)
        product_two = Product.query.get(second_product_id)
        assert product_one is not None
        assert product_two is not None
        assert round(float(product_one.stock), 3) == 1.75
        assert round(float(product_two.stock), 3) == 8.0

        # Sin ventas duplicadas por token idempotente.
        sale_rows_token_one = Sale.query.filter_by(company_id=1, client_txn_id="audit-flow-sale-1").count()
        sale_rows_token_two = Sale.query.filter_by(company_id=1, client_txn_id="audit-flow-sale-2").count()
        assert sale_rows_token_one <= 1
        assert sale_rows_token_two == 1

    # 9) Dashboard debe reflejar solo ventas confirmadas.
    dashboard_response = client.get("/dashboard/")
    assert dashboard_response.status_code == 200
    dashboard_html = dashboard_response.data.decode("utf-8")
    assert "Ingreso" in dashboard_html or "Ingresos" in dashboard_html
    assert "$23450.00" in dashboard_html

    # 10) Cierre de caja: esperado = apertura + suma ventas confirmadas en efectivo.
    close_response = client.post(
        "/caja/",
        data={"action": "close", "counted_amount": "23550", "closing_note": "Cierre auditoria final"},
        follow_redirects=True,
    )
    assert close_response.status_code == 200

    with stock_app.app.app_context():
        session = CashSession.query.order_by(CashSession.id.desc()).first()
        assert session is not None
        assert session.status == "cerrada"
        assert round(float(session.expected_amount or 0), 2) == 23550.00
        assert round(float(session.counted_amount or 0), 2) == 23550.00
        assert round(float(session.difference_amount or 0), 2) == 0.00


def test_admin_can_edit_sale_and_track_history():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

    open_cash_session(client)

    sale_response = client.post(
        "/ventas/api/checkout",
        json={
            "items": [{"productId": 1, "quantity": 1}],
            "metodo_pago": "EFECTIVO",
            "checkout_token": "edit-sale-test-1",
            "monto_pago": 18000,
        },
        headers={"X-Cart-Tenant": "1:2"},
    )
    assert sale_response.status_code == 200
    sale_id = sale_response.get_json()["sale_id"]

    edit_response = client.post(
        f"/ventas/{sale_id}/edit",
        data={
            "product_id": "1",
            "quantity": "0.5",
            "price": "18000",
            "discount": "0",
            "payment_method": "TRANSFERENCIA",
            "note": "Correccion manual",
            "order_discount": "0",
            "change_reason": "Cambio de metodo y cantidad",
        },
        follow_redirects=False,
    )
    assert edit_response.status_code in (301, 302)

    with stock_app.app.app_context():
        sale = db.session.get(Sale, sale_id)
        assert sale is not None
        assert sale.payment_method == "TRANSFERENCIA"
        assert round(float(sale.total_amount or 0), 2) == 9000.00
        product = db.session.get(Product, 1)
        assert product is not None
        assert round(float(product.stock), 3) == 2.0
        assert CashMovement.query.filter_by(sale_id=sale_id).count() == 0
        history_rows = SaleModificationHistory.query.filter_by(sale_id=sale_id).all()
        assert len(history_rows) == 1
        assert "Cambio de metodo" in history_rows[0].reason

    view_response = client.get(f"/ventas/{sale_id}")
    assert view_response.status_code == 200
    assert "Historial de modificaciones" in view_response.data.decode("utf-8")


def test_admin_can_switch_to_employee_session_from_sidebar_flow():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        target = User.query.filter_by(username="empresa_admin").first()
        assert target is not None
        target_id = target.id

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    switched = client.post(
        "/auth/switch-user",
        data={"target_user_id": str(target_id), "next": "/dashboard/"},
        follow_redirects=True,
    )
    assert switched.status_code == 200
    html = switched.data.decode("utf-8")
    assert "Sesión cambiada a empresa_admin." in html
    assert "empresa_admin" in html

    # Ya como empleado, los endpoints admin-only deben quedar bloqueados.
    blocked = client.post(
        "/admin/company-settings/users/create",
        data={"username": "x", "email": "x@test.local"},
        follow_redirects=False,
    )
    assert blocked.status_code == 403


def test_dashboard_economic_metrics_are_permission_protected():
    client = stock_app.app.test_client()

    # Empleado sin permiso economico: vista restringida y sin tarjetas financieras.
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    restricted = client.get("/dashboard/")
    restricted_html = restricted.data.decode("utf-8")
    assert restricted.status_code == 200
    assert "Información restringida" in restricted_html
    assert "Las métricas económicas solo pueden ser visualizadas por el Administrador de la empresa." in restricted_html
    assert "Ingresos hoy" not in restricted_html
    assert "Ganancia hoy" not in restricted_html
    assert "Rentabilidad" not in restricted_html

    client.post("/auth/logout")

    # Administrador de empresa: mantiene dashboard economico completo.
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    admin_dashboard = client.get("/dashboard/")
    admin_html = admin_dashboard.data.decode("utf-8")
    assert admin_dashboard.status_code == 200
    assert "Ingresos hoy" in admin_html
    assert "Ganancia hoy" in admin_html
    assert "Rentabilidad" in admin_html

    client.post("/auth/logout")

    # Permiso manual desde Mi Empresa (simulado por persistencia directa): habilita visualizacion economica.
    with stock_app.app.app_context():
        employee = User.query.filter_by(username="empresa_admin").first()
        assert employee is not None
        employee.permissions_json = json.dumps(["economic_stats"])
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    granted_dashboard = client.get("/dashboard/")
    granted_html = granted_dashboard.data.decode("utf-8")
    assert granted_dashboard.status_code == 200
    assert "Ingresos hoy" in granted_html
    assert "Ganancia hoy" in granted_html


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


def test_offline_first_shell_and_critical_forms_are_wired():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

    pages = {
        "/productos/": ["offlineSyncNow", "offlineSyncProgressBar", "offline-manager.js", 'action="/productos/add"'],
        "/clientes/": ["offlineSyncNow", "Nuevo cliente", "Guardar cliente"],
        "/compras/": ["Registrar compra", "Guardar proveedor"],
        "/gastos/": ["Costos operativos", 'name="amount"'],
        "/caja/": ["Apertura", "Abrir caja"],
    }

    for path, expected_snippets in pages.items():
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.data.decode("utf-8")
        for snippet in expected_snippets:
            assert snippet in html, (path, snippet)

    worker = client.get("/service-worker.js")
    assert worker.status_code == 200
    worker_js = worker.data.decode("utf-8")
    assert "stockarmobile-pwa-v7" in worker_js
    assert "OFFLINE_QUEUE_STATUS" in worker_js


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

    with stock_app.app.app_context():
        subscriptions_before_update = Subscription.query.filter_by(company_id=company_id).count()

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

    with stock_app.app.app_context():
        # Regression guard: "Modificar" debe hacer UPDATE in-place, nunca crear una
        # Subscription nueva (ver test dedicado test_subscription_modify_never_duplicates_records).
        subscriptions_after_update = Subscription.query.filter_by(company_id=company_id).count()
        assert subscriptions_after_update == subscriptions_before_update
        refreshed_active = db.session.get(Subscription, active_id)
        assert refreshed_active is not None
        assert refreshed_active.plan_id == plan_pro_id

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
    assert "Eliminar empresa definitivamente" in html
    assert f"/superadmin/companies/{company_id}/delete" in html
    assert "hard-delete-confirm-input" in html


def test_superadmin_company_hard_delete_removes_company_tree():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company_id = company.id

    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    delete_response = client.post(
        f"/superadmin/companies/{company_id}/delete",
        data={"next": "/superadmin/companies", "confirm_company_name": "Empresa Demo"},
        follow_redirects=False,
    )
    assert delete_response.status_code in (302, 303)

    with stock_app.app.app_context():
        assert db.session.get(Company, company_id) is None
        assert User.query.filter_by(company_id=company_id).count() == 0
        assert Product.query.filter_by(company_id=company_id).count() == 0
        assert Client.query.filter_by(company_id=company_id).count() == 0


def test_superadmin_company_hard_delete_requires_exact_name_match():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company_id = company.id

    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    wrong_name_resp = client.post(
        f"/superadmin/companies/{company_id}/delete",
        data={"next": "/superadmin/companies", "confirm_company_name": "Nombre Incorrecto"},
        follow_redirects=True,
    )
    assert wrong_name_resp.status_code == 200
    assert "escribí el nombre exacto de la empresa" in wrong_name_resp.data.decode("utf-8")

    with stock_app.app.app_context():
        # La empresa debe seguir existiendo: no se ejecuta el DELETE sin confirmación exacta.
        assert db.session.get(Company, company_id) is not None


def test_superadmin_company_hard_delete_rejected_for_non_superadmin():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company_id = company.id

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

    resp = client.post(
        f"/superadmin/companies/{company_id}/delete",
        data={"next": "/superadmin/companies", "confirm_company_name": "Empresa Demo"},
        follow_redirects=False,
    )
    assert resp.status_code == 403

    with stock_app.app.app_context():
        assert db.session.get(Company, company_id) is not None


def test_subscription_modify_never_duplicates_records():
    """Regresión del bug: 'Modificar' debe hacer UPDATE in-place y jamás crear
    una Company/Subscription/User adicional, sin importar cuántas veces se repita."""
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        plan_a = Plan(code="plan_a_dup_test", name="Plan A", price=1500, currency="ARS", duration_days=30, active=True)
        plan_b = Plan(code="plan_b_dup_test", name="Plan B", price=3000, currency="ARS", duration_days=30, active=True)
        db.session.add_all([plan_a, plan_b])
        db.session.flush()

        subscription = Subscription(
            company_id=company.id,
            plan_id=plan_a.id,
            status="active",
            renewal_enabled=True,
            auto_renew=True,
            cancel_at_period_end=False,
        )
        db.session.add(subscription)
        db.session.commit()

        company_id = company.id
        subscription_id = subscription.id
        plan_a_id = plan_a.id
        plan_b_id = plan_b.id

        company_count_before = Company.query.count()
        subscription_count_before = Subscription.query.count()
        user_count_before = User.query.filter_by(company_id=company_id).count()

    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    # Test 2/3: modificar el plan (incluso a uno pago) varias veces no debe crear registros nuevos.
    for target_plan_id in (plan_b_id, plan_a_id, plan_b_id):
        resp = client.post(
            f"/superadmin/subscriptions/{subscription_id}/update",
            data={"plan_id": target_plan_id, "status": "active", "renewal_enabled": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Suscripción modificada correctamente." in resp.data.decode("utf-8")

        with stock_app.app.app_context():
            assert Company.query.count() == company_count_before
            assert Subscription.query.count() == subscription_count_before
            assert User.query.filter_by(company_id=company_id).count() == user_count_before
            refreshed = db.session.get(Subscription, subscription_id)
            assert refreshed is not None
            assert refreshed.company_id == company_id
            assert refreshed.plan_id == target_plan_id

    # Test 9: doble-submit (mismos datos dos veces seguidas) tampoco debe duplicar.
    resp_again = client.post(
        f"/superadmin/subscriptions/{subscription_id}/update",
        data={"plan_id": plan_b_id, "status": "active", "renewal_enabled": "1"},
        follow_redirects=True,
    )
    assert resp_again.status_code == 200
    with stock_app.app.app_context():
        assert Subscription.query.count() == subscription_count_before


def test_subscription_lifecycle_actions_never_delete_company():
    """Cancelar/Suspender/Reactivar solo afectan la Subscription, jamás la Company."""
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        other_company = Company(name="Empresa Aislada Otra", active=True)
        db.session.add(other_company)
        db.session.flush()

        subscription = Subscription(company_id=company.id, plan_id=None, status="active", renewal_enabled=True, auto_renew=True)
        other_subscription = Subscription(company_id=other_company.id, plan_id=None, status="active", renewal_enabled=True, auto_renew=True)
        db.session.add_all([subscription, other_subscription])
        db.session.commit()

        company_id = company.id
        other_company_id = other_company.id
        subscription_id = subscription.id
        other_subscription_id = other_subscription.id

    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    for action in ("cancel", "suspend"):
        resp = client.post(
            f"/superadmin/subscriptions/{subscription_id}/action",
            data={"action": "reactivate" if action == "cancel" else action},
            follow_redirects=True,
        )
        assert resp.status_code == 200

    with stock_app.app.app_context():
        # La empresa objetivo sigue existiendo pese a las transiciones de estado de su suscripción.
        assert db.session.get(Company, company_id) is not None
        # Otra empresa no debe verse afectada por operaciones sobre subscription_id ajeno.
        assert db.session.get(Company, other_company_id) is not None
        other_refreshed = db.session.get(Subscription, other_subscription_id)
        assert other_refreshed is not None
        assert other_refreshed.status == "active"


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


def test_default_superadmin_bootstrap_creates_missing_account():
    with stock_app.app.app_context():
        clear_test_data(db)

        created = stock_app.ensure_default_superadmin_user()

        assert created is True
        user = User.query.filter_by(username="superadmin").first()
        assert user is not None
        assert user.email == "superadmin@stockarmobile.local"
        assert user.role == "superadmin"
        assert user.active is True
        assert user.check_password("admin123")
        assert User.query.filter_by(role="superadmin").count() == 1


def test_default_superadmin_bootstrap_skips_duplicates_when_username_or_email_exists():
    with stock_app.app.app_context():
        clear_test_data(db)

        conflict = User(username="superadmin", email="otro_correo@test.local", role="admin", active=True)
        conflict.set_password("otra123")
        db.session.add(conflict)
        db.session.commit()

        created = stock_app.ensure_default_superadmin_user()

        assert created is False
        assert User.query.filter_by(username="superadmin").count() == 1
        assert User.query.filter_by(email="superadmin@stockarmobile.local").count() == 0
        assert User.query.filter_by(role="superadmin").count() == 0


def test_scope_query_to_company_fails_closed_without_company_context():
    with stock_app.app.app_context():
        clear_test_data(db)

        company_a = Company(name="Empresa A", active=True)
        company_b = Company(name="Empresa B", active=True)
        db.session.add_all([company_a, company_b])
        db.session.flush()

        user = User(username="sin_empresa", email="sin_empresa@test.local", role="user", active=True, company_id=None)
        user.set_password("abc12345")
        db.session.add(user)
        db.session.add(Product(barcode="P-A", name="Producto A", price=100, stock=1, min_stock=0, company_id=company_a.id, active=True))
        db.session.add(Product(barcode="P-B", name="Producto B", price=100, stock=1, min_stock=0, company_id=company_b.id, active=True))
        db.session.commit()

        with stock_app.app.test_request_context("/"):
            login_user(user)
            rows = stock_app.scope_query_to_company(Product.query, Product).all()
            logout_user()

        assert rows == []


def test_referral_attribute_blocks_self_referral_by_same_user():
    from services.referral_service import ReferralService

    with stock_app.app.app_context():
        clear_test_data(db)

        seller_user = User(username="seller_self", email="same@test.local", role="seller", active=True)
        seller_user.set_password("seller123")
        db.session.add(seller_user)
        db.session.flush()

        seller = ReferralService.create_or_update_seller(
            db.session,
            user=seller_user,
            profile_data={"dni": "30111222", "active": True},
        )
        db.session.flush()

        company = Company(name="Empresa Referida", active=True)
        db.session.add(company)
        db.session.flush()

        attribution = ReferralService.attribute_company(
            db.session,
            seller=seller,
            company=company,
            user=seller_user,
            referral_code=seller.referral_code,
        )
        db.session.commit()

        assert attribution is None
        assert ReferralAttribution.query.filter_by(company_id=company.id).first() is None


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


def test_quotes_module_toggle_and_permissions():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        enable_quotes_module(company, enabled=False)

    assert client.get("/presupuestos/").status_code == 200


def test_quotes_builder_form_renders_productive_layout():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.get("/presupuestos/nuevo")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Constructor de presupuesto" in html
    assert "quoteProductSearch" in html
    assert "items_json" in html
    assert "Convertir en venta" in html

    payload = {
        "client_id": 1,
        "expires_at": "2026-08-05",
        "status": "BORRADOR",
        "items_json": json.dumps([
            {"product_id": 1, "description": "Yerba kilo", "quantity": 1, "unit_price": 18000, "discount": 0},
        ]),
        "submit_action": "save",
    }
    create_response = client.post("/presupuestos/nuevo", data=payload, follow_redirects=False)
    assert create_response.status_code in {302, 303}

    with stock_app.app.app_context():
        quote = Quote.query.order_by(Quote.id.desc()).first()
        assert quote is not None

    edit_response = client.get(f"/presupuestos/{quote.id}/editar")
    assert edit_response.status_code == 200
    edit_html = edit_response.get_data(as_text=True)
    assert f'formaction="/presupuestos/{quote.id}/convertir"' in edit_html
    assert 'formmethod="post"' in edit_html

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        enable_quotes_module(company, enabled=True)
        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None
        user.permissions_json = None
        db.session.commit()

    assert client.get("/presupuestos/").status_code == 200


def test_quotes_create_convert_pdf_and_stock_flow():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        enable_quotes_module(company, enabled=True)
        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None
        grant_quote_permissions(user)
        tenant_header = f"{user.company_id}:{user.id}"
        initial_stock = Product.query.get(1).stock

    payload = {
        "client_id": 1,
        "expires_at": "2026-08-05",
        "observations": "Presupuesto de prueba",
        "status": "BORRADOR",
        "discount": "50",
        "surcharge": "25",
        "items_json": json.dumps([
            {"product_id": 1, "description": "Yerba kilo", "quantity": 2, "unit_price": 18000, "discount": 100},
        ]),
        "submit_action": "save",
    }
    response = client.post("/presupuestos/nuevo", data=payload, follow_redirects=False)
    assert response.status_code in {302, 303}

    with stock_app.app.app_context():
        quote = Quote.query.order_by(Quote.id.desc()).first()
        assert quote is not None
        assert quote.status == "BORRADOR"
        assert float(quote.total_amount) == 35875.0
        assert float(Product.query.get(1).stock) == float(initial_stock)

    pdf_response = client.get(f"/presupuestos/{quote.id}/pdf")
    assert pdf_response.status_code == 200
    assert pdf_response.mimetype == "application/pdf"

    convert_response = client.post(f"/presupuestos/{quote.id}/convertir", follow_redirects=False)
    assert convert_response.status_code in {302, 303}
    assert "/ventas/" in (convert_response.headers.get("Location") or "")

    with client.session_transaction() as session_payload:
        prefill_key = f"quote_cart_prefill_{tenant_header}"
        prefill = session_payload.get(prefill_key)
        assert isinstance(prefill, dict)
        assert prefill.get("quote_id") == quote.id
        assert prefill.get("checkout_token") == f"quote-cart-{quote.id}"
        assert isinstance(prefill.get("items"), list)
        assert len(prefill["items"]) == 1

    open_cash_session(client)
    checkout_response = client.post(
        "/ventas/api/checkout",
        json={
            "items": prefill["items"],
            "metodo_pago": "EFECTIVO",
            "checkout_token": prefill["checkout_token"],
            "client_id": prefill.get("client_id") or "",
            "note": prefill.get("note") or "",
            "descuento_general": prefill.get("general_discount") or 0,
            "recargo": prefill.get("surcharge") or 0,
            "monto_pago": 40000,
        },
        headers={"X-Cart-Tenant": tenant_header},
    )
    assert checkout_response.status_code == 200
    sale_id = checkout_response.get_json()["sale_id"]

    with stock_app.app.app_context():
        quote = Quote.query.get(quote.id)
        sale = Sale.query.get(sale_id)
        assert quote is not None
        assert sale is not None
        assert quote.status == "CONVERTIDO"
        assert quote.converted_sale_id == sale.id
        assert float(sale.total_amount) == float(quote.total_amount)
        assert float(Product.query.get(1).stock) == float(initial_stock) - 2.0
        movement = CashMovement.query.filter_by(sale_id=sale.id).first()
        assert movement is not None
        assert float(movement.amount) == float(sale.total_amount)
        assert len(sale.items) == 1
        assert float(sale.items[0].quantity) == 2.0
        assert float(sale.items[0].total_amount) == float(sale.total_amount)


def test_quote_conversion_preserves_structured_adjustments_without_double_discount():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        user = User.query.filter_by(username="empresa_admin").first()
        assert company is not None
        assert user is not None
        enable_quotes_module(company, enabled=True)
        grant_quote_permissions(user)
        tenant_header = f"{user.company_id}:{user.id}"

    response = client.post(
        "/presupuestos/nuevo",
        data={
            "client_id": 1,
            "expires_at": "2026-08-05",
            "discount_type": "percentage",
            "discount_value": "10",
            "discount_reason": "Mayorista",
            "surcharge_type": "fixed",
            "surcharge_value": "25",
            "surcharge_reason": "Envío",
            "items_json": json.dumps([
                {"product_id": 1, "description": "Yerba kilo", "quantity": 2, "unit_price": 18000, "discount": 100},
            ]),
            "submit_action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}

    with stock_app.app.app_context():
        quote = Quote.query.order_by(Quote.id.desc()).first()
        assert quote is not None
        quote_id = quote.id
        quote_total = float(quote.total_amount)

    client.post(f"/presupuestos/{quote_id}/convertir", follow_redirects=False)
    with client.session_transaction() as session_payload:
        prefill = session_payload[f"quote_cart_prefill_{tenant_header}"]

    open_cash_session(client)
    checkout = client.post(
        "/ventas/api/checkout",
        json={
            "items": prefill["items"],
            "metodo_pago": "EFECTIVO",
            "checkout_token": prefill["checkout_token"],
            "monto_pago": 40000,
        },
        headers={"X-Cart-Tenant": tenant_header},
    )
    assert checkout.status_code == 200

    with stock_app.app.app_context():
        sale = Sale.query.get(checkout.get_json()["sale_id"])
        assert sale is not None
        assert float(sale.total_amount) == quote_total
        assert sale.discount_type == "percentage"
        assert float(sale.discount_value) == 10.0
        assert sale.discount_reason == "Mayorista"
        assert sale.surcharge_type == "fixed"
        assert float(sale.surcharge_value) == 25.0
        assert sale.surcharge_reason == "Envío"


def test_quotes_convert_redirects_to_sales_and_prefills_cart_without_open_cash():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        enable_quotes_module(company, enabled=True)
        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None
        grant_quote_permissions(user)
        tenant_header = f"{user.company_id}:{user.id}"

    payload = {
        "client_id": 1,
        "expires_at": "2026-08-05",
        "status": "BORRADOR",
        "items_json": json.dumps([
            {"product_id": 1, "description": "Yerba kilo", "quantity": 1, "unit_price": 18000, "discount": 0},
        ]),
        "submit_action": "save",
    }
    create_response = client.post("/presupuestos/nuevo", data=payload, follow_redirects=False)
    assert create_response.status_code in {302, 303}

    with stock_app.app.app_context():
        quote = Quote.query.order_by(Quote.id.desc()).first()
        assert quote is not None

    convert_response = client.post(f"/presupuestos/{quote.id}/convertir", follow_redirects=False)
    assert convert_response.status_code in {302, 303}
    assert "/ventas/" in (convert_response.headers.get("Location") or "")

    with client.session_transaction() as session_payload:
        prefill_key = f"quote_cart_prefill_{tenant_header}"
        prefill = session_payload.get(prefill_key)
        assert isinstance(prefill, dict)
        assert prefill.get("quote_id") == quote.id
        assert len(prefill.get("items") or []) == 1


def test_quotes_convert_rejects_when_no_stock_available_for_all_lines():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        enable_quotes_module(company, enabled=True)
        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None
        grant_quote_permissions(user)
        product = Product.query.get(1)
        assert product is not None
        product.stock = 0
        db.session.commit()

    payload = {
        "client_id": 1,
        "expires_at": "2026-08-05",
        "status": "BORRADOR",
        "items_json": json.dumps([
            {"product_id": 1, "description": "Yerba kilo", "quantity": 1, "unit_price": 18000, "discount": 0},
        ]),
        "submit_action": "save",
    }
    create_response = client.post("/presupuestos/nuevo", data=payload, follow_redirects=False)
    assert create_response.status_code in {302, 303}

    with stock_app.app.app_context():
        quote = Quote.query.order_by(Quote.id.desc()).first()
        assert quote is not None

    convert_response = client.post(f"/presupuestos/{quote.id}/convertir", follow_redirects=True)
    assert convert_response.status_code == 200
    html = convert_response.data.decode("utf-8")
    assert "no hay stock disponible" in html.lower()


def test_quotes_whatsapp_allows_empty_or_custom_number():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        enable_quotes_module(company, enabled=True)
        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None
        grant_quote_permissions(user)

    payload = {
        "client_id": "",
        "consumer_name": "Comprador Mostrador",
        "expires_at": "2026-08-05",
        "status": "BORRADOR",
        "discount": "0",
        "surcharge": "0",
        "items_json": json.dumps([
            {"product_id": 1, "description": "Yerba kilo", "quantity": 1, "unit_price": 18000, "discount": 0},
        ]),
        "submit_action": "save",
    }
    response = client.post("/presupuestos/nuevo", data=payload, follow_redirects=False)
    assert response.status_code in {302, 303}

    with stock_app.app.app_context():
        quote = Quote.query.order_by(Quote.id.desc()).first()
        assert quote is not None
        quote_id = quote.id

    dialog = client.get(f"/presupuestos/{quote_id}/whatsapp")
    assert dialog.status_code == 200
    html = dialog.data.decode("utf-8")
    assert "Número de WhatsApp (opcional)" in html

    empty_phone = client.post(f"/presupuestos/{quote_id}/whatsapp", data={"whatsapp_phone": ""}, follow_redirects=False)
    assert empty_phone.status_code in (301, 302)
    empty_location = empty_phone.headers.get("Location", "")
    assert empty_location.startswith("https://api.whatsapp.com/send?text=")
    assert "/presupuestos/publico/" in unquote(empty_location)

    custom_phone = client.post(f"/presupuestos/{quote_id}/whatsapp", data={"whatsapp_phone": "+54 9 11 2222 3333"}, follow_redirects=False)
    assert custom_phone.status_code in (301, 302)
    custom_location = custom_phone.headers.get("Location", "")
    assert custom_location.startswith("https://api.whatsapp.com/send?phone=5491122223333")
    assert "/presupuestos/publico/" in unquote(custom_location)


def test_quotes_allows_manual_consumer_name_without_client():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    payload = {
        "client_id": "",
        "consumer_name": "Juan Perez",
        "expires_at": "2026-08-05",
        "status": "BORRADOR",
        "discount": "0",
        "surcharge": "0",
        "items_json": json.dumps([
            {"product_id": 1, "description": "Yerba kilo", "quantity": 1, "unit_price": 18000, "discount": 0},
        ]),
        "submit_action": "save",
    }
    response = client.post("/presupuestos/nuevo", data=payload, follow_redirects=False)
    assert response.status_code in {302, 303}

    with stock_app.app.app_context():
        quote = Quote.query.order_by(Quote.id.desc()).first()
        assert quote is not None
        assert quote.client_id is None
        assert quote.consumer_name == "Juan Perez"


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


def test_qr_ordered_a4_flows_render_without_overlap_errors():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    bulk_ordered = client.post(
        "/qr/print-all",
        data={
            "label_format": "current",
            "ordered_a4": "1",
            "size": "standard",
            "copies": 2,
        },
    )
    assert bulk_ordered.status_code == 200
    assert bulk_ordered.mimetype == "application/pdf"

    single_ordered = client.post(
        "/qr/label-sheet/1",
        data={
            "label_size": "50x25",
            "quantity": 5,
            "include_name": "1",
            "include_price": "1",
            "include_code": "1",
            "include_qr": "1",
            "include_code128": "1",
            "ordered_a4": "1",
        },
    )
    assert single_ordered.status_code == 200
    assert single_ordered.mimetype == "application/pdf"


def test_qr_label_sheet_compact_non_ordered_with_code128_renders_pdf():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.post(
        "/qr/label-sheet/1",
        data={
            "label_size": "50x25",
            "quantity": 4,
            "include_name": "1",
            "include_price": "1",
            "include_code": "1",
            "include_qr": "1",
            "include_code128": "1",
            "include_date": "1",
        },
    )
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"


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


def test_suppliers_module_isolated_by_company():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Supplier

        company_one = Company.query.filter_by(name="Empresa Demo").first()
        assert company_one is not None

        db.session.add_all(
            [
                Supplier(company_id=company_one.id, name="Proveedor A1", email="a1@test.local", active=True),
                Supplier(company_id=company_one.id, name="Proveedor A2", email="a2@test.local", active=True),
                Supplier(company_id=company_one.id, name="Proveedor A3", email="a3@test.local", active=True),
            ]
        )

        company_two = Company(name="Empresa B Proveedores", active=True)
        db.session.add(company_two)
        db.session.flush()

        user_two = User(username="empresa_b_admin", email="empresa_b_admin@test.local", role="admin", company_id=company_two.id, active=True)
        user_two.set_password("admin123")
        db.session.add(user_two)
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_b_admin", "password": "admin123"})
    suppliers_page = client.get("/compras/proveedores")
    assert suppliers_page.status_code == 200
    html = suppliers_page.data.decode("utf-8")
    assert "Proveedor A1" not in html
    assert "Proveedor A2" not in html
    assert "Proveedor A3" not in html
    assert "No hay proveedores para mostrar." in html


def test_suppliers_module_blocks_cross_tenant_url_access():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Supplier

        company_one = Company.query.filter_by(name="Empresa Demo").first()
        assert company_one is not None
        supplier_one = Supplier(company_id=company_one.id, name="Proveedor Privado A", active=True)
        db.session.add(supplier_one)

        company_two = Company(name="Empresa B URL", active=True)
        db.session.add(company_two)
        db.session.flush()

        user_two = User(username="empresa_b_url_admin", email="empresa_b_url_admin@test.local", role="admin", company_id=company_two.id, active=True)
        user_two.set_password("admin123")
        db.session.add(user_two)
        db.session.commit()

        foreign_supplier_id = supplier_one.id

    client.post("/auth/login", data={"username": "empresa_b_url_admin", "password": "admin123"})

    update_response = client.post(
        f"/compras/proveedores/{foreign_supplier_id}/update",
        data={"name": "Intento editar ajeno"},
        follow_redirects=False,
    )
    assert update_response.status_code == 404

    toggle_response = client.post(
        f"/compras/proveedores/{foreign_supplier_id}/toggle",
        data={},
        follow_redirects=False,
    )
    assert toggle_response.status_code == 404


def test_checkout_rejects_foreign_tenant_product_and_client():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        company_two = Company(name="Empresa Dos Checkout", active=True)
        db.session.add(company_two)
        db.session.flush()

        user_two = User(username="checkout_empresa_dos", email="checkout_empresa_dos@test.local", role="admin", company_id=company_two.id, active=True)
        user_two.set_password("admin123")
        db.session.add(user_two)

        product_two = Product(
            barcode="CHECKOUT-TWO-001",
            name="Producto Empresa Dos Checkout",
            price=9500,
            cost_price=5000,
            stock=5,
            min_stock=1,
            active=True,
            company_id=company_two.id,
        )
        db.session.add(product_two)
        db.session.commit()

        company_two_id = company_two.id
        user_two_id = user_two.id
        product_two_id = product_two.id

    client.post("/auth/login", data={"username": "checkout_empresa_dos", "password": "admin123"})
    open_cash_session(client)

    # Producto de empresa 1 no debe poder cobrarse desde empresa 2.
    foreign_product_checkout = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": 1, "quantity": 1}], "metodo_pago": "EFECTIVO"},
        headers={"X-Cart-Tenant": f"{company_two_id}:{user_two_id}"},
    )
    assert foreign_product_checkout.status_code == 400
    assert "Producto no encontrado" in (foreign_product_checkout.get_json() or {}).get("error", "")

    # Cliente de empresa 1 no debe poder asociarse a una venta de empresa 2.
    foreign_client_checkout = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": product_two_id, "quantity": 1}], "metodo_pago": "EFECTIVO", "client_id": 1},
        headers={"X-Cart-Tenant": f"{company_two_id}:{user_two_id}"},
    )
    assert foreign_client_checkout.status_code == 400
    assert "no pertenece a tu empresa" in (foreign_client_checkout.get_json() or {}).get("error", "")


def test_dashboard_metrics_do_not_leak_between_companies():
    from services.dashboard_service import build_dashboard_context

    with stock_app.app.app_context():
        from app import Sale, utcnow

        company_a = Company.query.filter_by(name="Empresa Demo").first()
        assert company_a is not None

        company_b = Company(name="Empresa B Dashboard", active=True)
        db.session.add(company_b)
        db.session.flush()

        user_b = User(
            username="dashboard_b_admin",
            email="dashboard_b_admin@test.local",
            role="admin",
            company_id=company_b.id,
            active=True,
        )
        user_b.set_password("admin123")
        db.session.add(user_b)

        for _ in range(5):
            db.session.add(
                Sale(
                    date=utcnow(),
                    customer="Cliente A",
                    subtotal=20000,
                    discount=0,
                    tax=0,
                    total_amount=20000,
                    payment_method="EFECTIVO",
                    status="confirmada",
                    company_id=company_a.id,
                )
            )
        db.session.commit()

        admin_a = User.query.filter_by(username="negocio_admin").first()
        assert admin_a is not None

        with stock_app.app.test_request_context("/dashboard/"):
            login_user(admin_a)
            context_a = build_dashboard_context()
            logout_user()

        with stock_app.app.test_request_context("/dashboard/"):
            login_user(user_b)
            context_b = build_dashboard_context()
            logout_user()

        assert float(context_a["ingresos_mes"] or 0) == 100000.0
        assert float(context_a["ganancia_mes"] or 0) == 100000.0

        assert float(context_b["ingresos_mes"] or 0) == 0.0
        assert float(context_b["ganancia_mes"] or 0) == 0.0
        assert int(context_b["ventas_mes"] or 0) == 0
        assert all(float(value or 0) == 0.0 for value in context_b["chart_sales"])


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


def test_product_barcode_preserves_leading_zeroes_on_create():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.post(
        "/productos/add",
        data={
            "barcode": "0012345678905",
            "name": "Producto codigo escaneado",
            "sale_type": "unidad",
            "unit_measure": "u",
            "price": "100",
            "cost_price": "50",
            "stock": "2",
            "min_stock": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        product = Product.query.filter_by(barcode="0012345678905").first()
        assert product is not None
        assert product.barcode == "0012345678905"


def test_product_edit_keeps_own_barcode_and_rejects_tenant_duplicate():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        target = Product.query.filter_by(company_id=company.id, barcode="123456789012").first()
        assert target is not None
        duplicate = Product(
            barcode="EDIT-DUPLICATE-001",
            name="Producto duplicado",
            price=100,
            cost_price=50,
            stock=1,
            min_stock=0,
            active=True,
            company_id=company.id,
        )
        db.session.add(duplicate)
        db.session.commit()
        target_id = target.id

    base_form = {
        "name": "Yerba kilo",
        "sale_type": "unidad",
        "unit_measure": "u",
        "cost_price": "100",
        "price": "18000",
        "margin": "17900",
        "profit_percent": "17900",
        "pricing_source": "price",
        "stock": "2.5",
        "min_stock": "0.5",
    }
    same_code = client.post(
        f"/productos/edit/{target_id}",
        data={**base_form, "barcode": "123456789012"},
        follow_redirects=False,
    )
    assert same_code.status_code in (301, 302)

    changed_code = client.post(
        f"/productos/edit/{target_id}",
        data={**base_form, "barcode": "0012345678905"},
        follow_redirects=False,
    )
    assert changed_code.status_code in (301, 302)

    duplicate_code = client.post(
        f"/productos/edit/{target_id}",
        data={**base_form, "barcode": "EDIT-DUPLICATE-001"},
        follow_redirects=False,
    )
    assert duplicate_code.status_code in (301, 302)

    with stock_app.app.app_context():
        target = db.session.get(Product, target_id)
        assert target is not None
        assert target.barcode == "0012345678905"


def test_pos_barcode_lookup_is_tenant_scoped_and_preserves_string_code():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        company_a = Company.query.filter_by(name="Empresa Demo").first()
        assert company_a is not None
        company_b = Company(name="Empresa Scanner B", active=True)
        db.session.add(company_b)
        db.session.flush()
        user_b = User(username="scanner_tenant_b", email="scanner_tenant_b@test.local", role="admin", company_id=company_b.id, active=True)
        user_b.set_password("admin123")
        db.session.add_all([
            user_b,
            Product(barcode="0012345678905", name="Producto Scanner A", price=100, cost_price=50, stock=3, min_stock=0, active=True, company_id=company_a.id),
            Product(barcode="0012345678905", name="Producto Scanner B", price=200, cost_price=100, stock=3, min_stock=0, active=True, company_id=company_b.id),
        ])
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    first = client.get("/productos/api/0012345678905")
    assert first.status_code == 200
    assert first.get_json()["name"] == "Producto Scanner A"
    assert client.get("/productos/api/CODIGO-INEXISTENTE").status_code == 404

    client.post("/auth/logout")
    client.post("/auth/login", data={"username": "scanner_tenant_b", "password": "admin123"})
    second = client.get("/productos/api/0012345678905")
    assert second.status_code == 200
    assert second.get_json()["name"] == "Producto Scanner B"


def test_pos_loads_reusable_barcode_scanner_component():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.get("/ventas/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "pos-barcode-camera-button" in html
    assert "assets/js/barcode-scanner.js" in html
    assert "StockArBarcodeScanner.openScanner" in html


def test_checkout_does_not_apply_automatic_tax():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    open_cash_session(client)

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
        sum_items = sum(float(item.total_amount or 0) for item in sale.items)
        assert round(sum_items, 2) == round(float(sale.total_amount), 2)


def test_checkout_is_idempotent_with_checkout_token():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    open_cash_session(client)

    payload = {
        "items": [{"productId": 1, "quantity": 1}],
        "metodo_pago": "EFECTIVO",
        "checkout_token": "dup-token-001",
    }
    first = client.post("/ventas/api/checkout", json=payload, headers={"X-Cart-Tenant": "1:1"})
    second = client.post("/ventas/api/checkout", json=payload, headers={"X-Cart-Tenant": "1:1"})

    assert first.status_code == 200
    assert second.status_code == 200
    first_data = first.get_json()
    second_data = second.get_json()
    assert first_data["sale_id"] == second_data["sale_id"]

    with stock_app.app.app_context():
        sales = Sale.query.filter_by(company_id=1).all()
        assert len(sales) == 1
        assert sales[0].client_txn_id == "dup-token-001"


def test_dashboard_ignores_non_confirmed_sales_in_metrics():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        user = User.query.filter_by(username="negocio_admin").first()
        assert user is not None

        confirmed_sale = Sale(
            customer="Cliente confirmado",
            subtotal=1000,
            discount=0,
            tax=0,
            total_amount=1000,
            payment_method="EFECTIVO",
            status="confirmada",
            seller_id=user.id,
            company_id=1,
        )
        cancelled_sale = Sale(
            customer="Cliente anulada",
            subtotal=9000,
            discount=0,
            tax=0,
            total_amount=9000,
            payment_method="EFECTIVO",
            status="anulada",
            seller_id=user.id,
            company_id=1,
        )
        db.session.add(confirmed_sale)
        db.session.add(cancelled_sale)
        db.session.flush()

        db.session.add(SaleItem(sale_id=confirmed_sale.id, product_id=1, quantity=1, price=1000, discount=0, cost_price=100))
        db.session.add(SaleItem(sale_id=cancelled_sale.id, product_id=1, quantity=2, price=4500, discount=0, cost_price=100))
        db.session.commit()

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    response = client.get("/dashboard/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "$1000.00" in html
    assert "$10000.00" not in html


def test_dashboard_sum_uses_confirmed_sales_aggregate():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        user = User.query.filter_by(username="negocio_admin").first()
        assert user is not None

        db.session.add(
            Sale(
                customer="A",
                subtotal=100,
                discount=0,
                tax=0,
                total_amount=100,
                payment_method="EFECTIVO",
                status="confirmada",
                seller_id=user.id,
                company_id=1,
            )
        )
        db.session.add(
            Sale(
                customer="B",
                subtotal=250,
                discount=0,
                tax=0,
                total_amount=250,
                payment_method="EFECTIVO",
                status="confirmada",
                seller_id=user.id,
                company_id=1,
            )
        )
        db.session.add(
            Sale(
                customer="C",
                subtotal=999,
                discount=0,
                tax=0,
                total_amount=999,
                payment_method="EFECTIVO",
                status="anulada",
                seller_id=user.id,
                company_id=1,
            )
        )
        db.session.commit()

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    dashboard = client.get("/dashboard/")
    assert dashboard.status_code == 200
    html = dashboard.data.decode("utf-8")
    assert "$350.00" in html
    assert "$1349.00" not in html


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
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

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


def test_employee_can_add_products_but_not_edit_prices_or_delete_them():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    created_response = client.post(
        "/productos/add",
        data={
            "barcode": "EMP-ADD-001",
            "name": "Producto empleado",
            "sale_type": "unidad",
            "unit_measure": "u",
            "price": "500",
            "cost_price": "300",
            "stock": "4",
            "min_stock": "1",
        },
        follow_redirects=False,
    )
    assert created_response.status_code in (301, 302)

    with stock_app.app.app_context():
        created = Product.query.filter_by(barcode="EMP-ADD-001").first()
        assert created is not None
        product_id = created.id

    blocked_edit = client.post(
        f"/productos/edit/{product_id}",
        data={
            "barcode": "EMP-ADD-001",
            "name": "Producto empleado",
            "sale_type": "unidad",
            "unit_measure": "u",
            "price": "999",
            "cost_price": "300",
            "stock": "4",
            "min_stock": "1",
            "pricing_source": "price",
        },
        follow_redirects=False,
    )
    assert blocked_edit.status_code in (301, 302)

    blocked_delete = client.post(f"/productos/delete/{product_id}", follow_redirects=False)
    assert blocked_delete.status_code in (301, 302)

    with stock_app.app.app_context():
        created = db.session.get(Product, product_id)
        assert created is not None
        assert float(created.price) == 500.0
        assert created.active is True


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
    assert "company-day-calendar" in html
    assert "company-settings/day-activity" in html

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


def test_my_company_day_activity_is_pin_protected_and_tenant_scoped():
    from services.company_security_service import CompanySecurityService

    client = stock_app.app.test_client()
    with stock_app.app.app_context():
        from app import CashMovement, Company, Expense, Sale, utcnow

        company_a = Company.query.filter_by(name="Empresa Demo").first()
        admin_a = User.query.filter_by(username="negocio_admin").first()
        assert company_a is not None
        assert admin_a is not None
        company_b = Company(name="Empresa Calendario B", active=True)
        db.session.add(company_b)
        db.session.flush()
        admin_b = User(username="calendar_admin_b", email="calendar_admin_b@test.local", role="admin", company_id=company_b.id, active=True)
        admin_b.set_password("admin123")
        db.session.add(admin_b)
        CompanySecurityService.set_pin(company_a, "1234")

        db.session.add_all(
            [
                Sale(customer="Cliente A", subtotal=100, discount=0, tax=0, total_amount=100, payment_method="EFECTIVO", seller_id=admin_a.id, company_id=company_a.id, date=utcnow()),
                Expense(category="Servicios", description="Gasto A", amount=20, company_id=company_a.id, user_id=admin_a.id, date=utcnow()),
                CashMovement(user_id=admin_a.id, company_id=company_a.id, movement_type="ingreso", category="manual", amount=15, description="Ingreso A", created_at=utcnow()),
                Sale(customer="Cliente B", subtotal=999, discount=0, tax=0, total_amount=999, payment_method="EFECTIVO", seller_id=admin_b.id, company_id=company_b.id, date=utcnow()),
            ]
        )
        db.session.commit()
        selected_date = utcnow().date().isoformat()

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    locked = client.get(f"/admin/company-settings/day-activity?date={selected_date}")
    assert locked.status_code == 403

    client.post("/admin/company-settings/pin/verify", data={"access_pin": "1234"})
    response = client.get(f"/admin/company-settings/day-activity?date={selected_date}")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["summary"]["sales_count"] == 1
    assert payload["summary"]["sales_total"] == 100.0
    assert payload["summary"]["expenses_total"] == 20.0
    assert payload["summary"]["cash_movements_total"] == 15.0
    assert all("Cliente B" not in row["detail"] for row in payload["activities"])


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
        subscription = SubscriptionService.start_or_change_plan(db.session, company=company, plan=plan, user_id=admin_user.id)
        SubscriptionService.apply_payment_status(subscription, "approved")
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

    custom_password = client.post(
        f"/admin/company-settings/users/{created_id}/reset-password",
        data={
            "new_password": "cajero123",
            "confirm_password": "cajero123",
        },
        follow_redirects=True,
    )
    assert custom_password.status_code == 200
    assert "Contrasena actualizada correctamente" in custom_password.data.decode("utf-8")

    with stock_app.app.app_context():
        created = User.query.filter_by(username="cajero_nuevo").first()
        assert created is not None
        assert created.must_change_password is False
        assert created.check_password("cajero123")

    client.post("/auth/logout")
    login_new_password = client.post(
        "/auth/login",
        data={"username": "cajero_nuevo", "password": "cajero123"},
        follow_redirects=False,
    )
    assert login_new_password.status_code in (301, 302)


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
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "Número de WhatsApp (opcional)" in html
    assert "549111111111" in html


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
    assert "Número de WhatsApp (opcional)" in html

    send_without_phone = client.post(
        f"/ventas/{sale_id}/share-whatsapp",
        data={"whatsapp_phone": ""},
        follow_redirects=False,
    )
    assert send_without_phone.status_code in (301, 302)
    send_without_phone_location = send_without_phone.headers.get("Location", "")
    assert send_without_phone_location.startswith("https://api.whatsapp.com/send?text=")
    assert "/ventas/publico/" in unquote(send_without_phone_location)

    send_once = client.post(
        f"/ventas/{sale_id}/share-whatsapp",
        data={"whatsapp_phone": "5491122233344"},
        follow_redirects=False,
    )
    assert send_once.status_code in (301, 302)
    send_once_location = send_once.headers.get("Location", "")
    assert send_once_location.startswith("https://api.whatsapp.com/send?phone=5491122233344")
    assert "/ventas/publico/" in unquote(send_once_location)

    with stock_app.app.app_context():
        unchanged_client = db.session.get(Client, client_id)
        assert unchanged_client is not None
        assert not (unchanged_client.whatsapp or "").strip()


def test_share_whatsapp_accepts_phone_with_symbols_without_blocking():
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

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    send_custom = client.post(
        f"/ventas/{sale_id}/share-whatsapp",
        data={"whatsapp_phone": "+54 9 11 5556-6677"},
        follow_redirects=False,
    )
    assert send_custom.status_code in (301, 302)
    send_custom_location = send_custom.headers.get("Location", "")
    assert send_custom_location.startswith("https://api.whatsapp.com/send?phone=5491155566677")
    assert "/ventas/publico/" in unquote(send_custom_location)


def test_login_has_no_google_button_and_has_forgot_password_link():
    client = stock_app.app.test_client()
    response = client.get("/auth/login")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "Continuar con Google" not in html
    assert "/auth/google" not in html
    assert "auth.google_login" not in html
    assert "¿Olvidaste tu contrasena?" in html
    # Visita anonima: no debe exponer atributos ni shell del panel autenticado.
    assert "data-user-id" not in html
    assert "data-company-id" not in html


def test_authenticated_login_visit_redirects_and_hides_dashboard_shell():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

    # Con sesion activa, /auth/login no debe renderizar el formulario ni el shell del panel:
    # debe redirigir de inmediato, sin mezclar contenido de login con el dashboard autenticado.
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code in (301, 302, 303)
    assert response.headers["Location"].rstrip("/").endswith("/dashboard")
    redirect_body = response.data.decode("utf-8")
    assert "data-user-id" not in redirect_body
    assert 'name="username"' not in redirect_body


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


def test_password_recovery_request_reopens_attended_request_for_superadmin():
    client = stock_app.app.test_client()

    first_request = client.post(
        "/auth/forgot-password",
        data={"email": "admin@test.local"},
        follow_redirects=True,
    )
    assert first_request.status_code == 200
    first_token = stock_app.app.config.get("_LAST_PASSWORD_RESET_TOKEN")
    assert first_token
    assert first_token not in first_request.data.decode("utf-8")

    with stock_app.app.app_context():
        from app import PasswordRecoveryRequest

        item = PasswordRecoveryRequest.query.filter_by(email="admin@test.local").one()
        item.status = "atendida"
        item.processed_at = stock_app.utcnow()
        item.processed_by_user_id = User.query.filter_by(username="superadmin").one().id
        db.session.commit()
        request_id = item.id

    repeated_request = client.post(
        "/auth/forgot-password",
        data={"email": "admin@test.local"},
        follow_redirects=True,
    )
    assert repeated_request.status_code == 200
    repeated_token = stock_app.app.config.get("_LAST_PASSWORD_RESET_TOKEN")
    assert repeated_token
    assert repeated_token not in repeated_request.data.decode("utf-8")

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    assert client.get("/superadmin/password-recovery").status_code == 403
    client.post("/auth/logout")
    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})
    panel = client.get("/superadmin/password-recovery?status=pendiente")
    assert panel.status_code == 200
    assert "admin@test.local" in panel.data.decode("utf-8")

    with stock_app.app.app_context():
        from app import PasswordRecoveryRequest

        item = db.session.get(PasswordRecoveryRequest, request_id)
        assert item is not None
        assert item.status == "pendiente"
        assert item.processed_at is None
        assert item.processed_by_user_id is None


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
        "Sistema de gestión para comercios: stock, ventas y caja en un solo lugar",
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
    assert "Mi Suscripción" in portal_html
    assert "Uso del plan" in portal_html
    assert "Plan contratado" in portal_html
    assert ("Actualizar plan" in portal_html) or ("Renovar plan" in portal_html)


def test_landing_seo_phase2_copy_and_single_h1():
    client = stock_app.app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")

    assert "<title>StockArmobile | Sistema de gestión para comercios en Argentina</title>" in html
    assert (
        'name="description" content="Sistema de gestión para comercios en Argentina: control de stock, ventas, caja y clientes '
        'en una sola plataforma. Funciona desde el celular, con modo offline y prueba gratuita de 10 días."' in html
    )
    assert html.count("<h1") == 1
    assert "Sistema de gestión para comercios: stock, ventas y caja en un solo lugar" in html
    assert (
        "Controlá tu negocio desde la PC o el celular: ventas, stock, clientes, caja, QR, etiquetas y reportes "
        "en una sola plataforma pensada para comercios argentinos." in html
    )
    assert "Beneficios para el control de stock y ventas de tu comercio" in html
    assert "Pensado para comercios, ferreterías, kioscos y negocios en crecimiento" in html
    assert "Mercado Pago" in html
    assert "presupuestos" in html.lower()
    assert "códigos de barras" in html.lower()

    # Fase 1 no debe romperse: canonical unico, robots ausente (indexable), lang y JSON-LD intactos.
    assert html.count('<link rel="canonical"') == 1
    assert 'lang="es-AR"' in html
    assert html.count("application/ld+json") == 3
    assert '"@type": "SoftwareApplication"' in html
    assert '"@type": "Organization"' in html
    assert '"@type": "WebSite"' in html
    assert "priceCurrency" not in html
    assert '"@type": "FAQPage"' not in html
    assert 'property="og:title" content="StockArmobile | Sistema de gestión para comercios en Argentina"' in html
    assert 'name="twitter:title" content="StockArmobile | Sistema de gestión para comercios en Argentina"' in html


def test_landing_public_ranking_no_longer_exposes_referrer_identity():
    """Regresión Fase 2.5: la landing publica no debe exponer nombres/emails/metricas
    individuales de referidores, pero el programa de referidos debe seguir presente."""
    with stock_app.app.app_context():
        from app import Company, ReferralSeller, User, db, utcnow

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        distinctive_username = "referidor_publico_test"
        distinctive_email = "referidor_publico_test@test.local"
        seller_user = User(
            username=distinctive_username,
            email=distinctive_email,
            first_name="Referidor",
            last_name="Publico",
            role="seller",
            company_id=company.id,
            active=True,
        )
        seller_user.set_password("seller123")
        db.session.add(seller_user)
        db.session.flush()

        db.session.add(
            ReferralSeller(
                user_id=seller_user.id,
                dni="30999888",
                referral_code="REFPUB1",
                referral_url="https://www.stockarmobile.com/?ref=REFPUB1",
                active=True,
                created_at=utcnow(),
            )
        )
        db.session.commit()

    client = stock_app.app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    html = response.data.decode("utf-8")

    # No debe exponer identidad ni metricas individuales del referidor.
    assert "referidor_publico_test" not in html
    assert "Referidor Publico" not in html
    assert "REFPUB1" not in html
    assert "Top referidores" not in html
    assert "Ventas referidas:" not in html
    assert "Clientes:" not in html

    # El programa de referidos sigue presente, en forma generica.
    assert "Programa de Referidos" in html
    assert "mensual recomendando StockArmobile" in html
    assert "Ya sos cliente? Iniciar sesion y activar Referidos" in html
    assert "No sos cliente? Crear cuenta de vendedor" in html

    # SEO de Fase 1/2 intacto.
    assert html.count("<h1") == 1
    assert "<title>StockArmobile | Sistema de gestión para comercios en Argentina</title>" in html
    assert html.count('<link rel="canonical"') == 1
    assert html.count("application/ld+json") == 3


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
    assert "Programa de Referidos" in html
    assert "mensual recomendando StockArmobile" in html


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
    assert "Actualizar plan" in portal_html or "Renovar plan" in portal_html

    status_page = client.get("/access-status")
    assert status_page.status_code == 200
    status_html = status_page.data.decode("utf-8")
    assert "Tu prueba finalizó" in status_html or "trial_expired" in status_html


def test_trial_without_explicit_limit_blocks_after_ten_days():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Subscription

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company.created_at = stock_app.utcnow() - timedelta(days=11)
        company.trial_ends_at = None
        Subscription.query.filter_by(company_id=company.id).delete(synchronize_session=False)
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    blocked_dashboard = client.get("/dashboard/", follow_redirects=False)
    assert blocked_dashboard.status_code in (301, 302)
    assert "/access-status" in (blocked_dashboard.headers.get("Location") or "")


def test_register_with_paid_plan_keeps_trial_of_ten_days():
    client = stock_app.app.test_client()

    response = client.post(
        "/auth/register",
        data={
            "username": "trial_paid_user",
            "email": "trial_paid_user@test.com",
            "password": "trial123",
            "selected_plan": "entrepreneur",
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import Company, Subscription

        company = Company.query.filter_by(name="Empresa trial_paid_user").first()
        assert company is not None
        assert company.trial_ends_at is not None

        subscription = Subscription.query.filter_by(company_id=company.id).order_by(Subscription.id.desc()).first()
        assert subscription is not None
        assert (subscription.status or "").lower() == "trial"
        assert subscription.trial_end == company.trial_ends_at
        assert subscription.next_billing_date == company.trial_ends_at
        assert subscription.ends_at == company.trial_ends_at

        remaining_days = (company.trial_ends_at - stock_app.utcnow()).total_seconds() / 86400
        assert remaining_days <= 10.05
        assert remaining_days >= 9.80


def test_checkout_during_trial_does_not_switch_to_pending_or_30_days():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        trial_plan = Plan.query.filter_by(code="trial").first()
        paid_plan = Plan.query.filter_by(code="entrepreneur").first()
        assert trial_plan is not None
        assert paid_plan is not None

        company.trial_ends_at = stock_app.utcnow() + timedelta(days=10)
        Subscription.query.filter_by(company_id=company.id).delete(synchronize_session=False)
        db.session.commit()

        SubscriptionService.ensure_company_trial(db.session, company=company, trial_plan=trial_plan)
        db.session.commit()

    login = client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"}, follow_redirects=False)
    assert login.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import Plan

        paid_plan = Plan.query.filter_by(code="entrepreneur").first()
        assert paid_plan is not None
        plan_id = paid_plan.id

    checkout = client.post("/admin/checkout", data={"plan_id": plan_id}, follow_redirects=False)
    assert checkout.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import Subscription

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        subscription = Subscription.query.filter_by(company_id=company.id).order_by(Subscription.id.desc()).first()
        assert subscription is not None
        assert (subscription.status or "").lower() == "trial"
        assert subscription.trial_end == company.trial_ends_at
        assert subscription.next_billing_date == company.trial_ends_at
        assert subscription.ends_at == company.trial_ends_at


def test_subscription_portal_get_does_not_create_or_mutate_subscription():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Subscription

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company.trial_ends_at = stock_app.utcnow() + timedelta(days=5)
        Subscription.query.filter_by(company_id=company.id).delete(synchronize_session=False)
        db.session.commit()

        before_count = Subscription.query.filter_by(company_id=company.id).count()
        assert before_count == 0

    login = client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"}, follow_redirects=False)
    assert login.status_code in (301, 302)

    portal = client.get("/admin/portal", follow_redirects=False)
    assert portal.status_code == 200

    with stock_app.app.app_context():
        from app import Subscription

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        after_count = Subscription.query.filter_by(company_id=company.id).count()
        assert after_count == 0


def test_select_plan_does_not_mutate_until_confirm():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company.trial_ends_at = stock_app.utcnow() + timedelta(days=10)

        trial_plan = Plan.query.filter_by(code="trial").first()
        paid_plan = Plan.query.filter_by(code="entrepreneur").first()
        assert trial_plan is not None
        assert paid_plan is not None

        Subscription.query.filter_by(company_id=company.id).delete(synchronize_session=False)
        db.session.commit()
        subscription = SubscriptionService.ensure_company_trial(db.session, company=company, trial_plan=trial_plan)
        db.session.commit()

        previous_id = subscription.id
        previous_plan_id = subscription.plan_id
        previous_status = (subscription.status or "").lower()
        paid_plan_id = paid_plan.id

    login = client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"}, follow_redirects=False)
    assert login.status_code in (301, 302)

    select_response = client.post("/admin/checkout", data={"plan_id": paid_plan_id}, follow_redirects=False)
    assert select_response.status_code in (301, 302)

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        rows = Subscription.query.filter_by(company_id=company.id).order_by(Subscription.id.asc()).all()
        assert len(rows) == 1
        assert rows[0].id == previous_id
        assert rows[0].plan_id == previous_plan_id
        assert (rows[0].status or "").lower() == previous_status


def test_subscription_change_confirm_rolls_back_on_error(monkeypatch):
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company.trial_ends_at = stock_app.utcnow() + timedelta(days=10)

        trial_plan = Plan.query.filter_by(code="trial").first()
        assert trial_plan is not None

        backup_free_plan = Plan.query.filter_by(code="trial_alt").first()
        if backup_free_plan is None:
            backup_free_plan = Plan(
                code="trial_alt",
                name="Trial alternativo",
                price=0,
                currency="ARS",
                duration_days=10,
                max_users=1,
                max_products=50,
                max_clients=100,
                active=True,
            )
            db.session.add(backup_free_plan)
            db.session.flush()

        Subscription.query.filter_by(company_id=company.id).delete(synchronize_session=False)
        db.session.commit()
        previous = SubscriptionService.ensure_company_trial(db.session, company=company, trial_plan=trial_plan)
        db.session.commit()
        previous_id = previous.id
        backup_free_plan_id = backup_free_plan.id

    def _raise_commission_error(*args, **kwargs):
        raise RuntimeError("forced commission failure")

    monkeypatch.setattr("company_billing.ReferralService.create_commission_for_sale", _raise_commission_error)

    login = client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"}, follow_redirects=False)
    assert login.status_code in (301, 302)

    response = client.post("/admin/subscription/change", data={"plan_id": backup_free_plan_id}, follow_redirects=False)
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        rows = Subscription.query.filter_by(company_id=company.id).order_by(Subscription.id.asc()).all()
        assert len(rows) == 1
        assert rows[0].id == previous_id
        assert (rows[0].status or "").lower() == "trial"


def test_subscription_change_confirm_double_post_is_idempotent():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company.trial_ends_at = stock_app.utcnow() + timedelta(days=10)

        trial_plan = Plan.query.filter_by(code="trial").first()
        assert trial_plan is not None

        free_plan = Plan.query.filter_by(code="trial_alt_idempotent").first()
        if free_plan is None:
            free_plan = Plan(
                code="trial_alt_idempotent",
                name="Plan idempotente",
                price=0,
                currency="ARS",
                duration_days=10,
                max_users=1,
                max_products=50,
                max_clients=100,
                active=True,
            )
            db.session.add(free_plan)
            db.session.flush()

        Subscription.query.filter_by(company_id=company.id).delete(synchronize_session=False)
        db.session.commit()
        SubscriptionService.ensure_company_trial(db.session, company=company, trial_plan=trial_plan)
        db.session.commit()
        free_plan_id = free_plan.id

    login = client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"}, follow_redirects=False)
    assert login.status_code in (301, 302)

    first = client.post("/admin/subscription/change", data={"plan_id": free_plan_id}, follow_redirects=False)
    assert first.status_code in (301, 302)
    second = client.post("/admin/subscription/change", data={"plan_id": free_plan_id}, follow_redirects=False)
    assert second.status_code in (301, 302)

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        rows = Subscription.query.filter_by(company_id=company.id).order_by(Subscription.id.asc()).all()
        assert len(rows) == 2
        assert rows[-1].plan_id == free_plan_id


def test_subscription_commands_create_execution_log_row():
    with stock_app.app.app_context():
        from app import Plan, SubscriptionCommandExecution
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        paid_plan = Plan.query.filter_by(code="entrepreneur").first()
        assert paid_plan is not None

        command = SubscriptionService.ChangePlanCommand(
            company_id=company.id,
            plan_id=paid_plan.id,
            actor_user_id=1,
            actor_role="admin",
            origin="test",
            idempotency_key=f"test-command-log:{company.id}:{paid_plan.id}",
        )

        SubscriptionService.run_command(db.session, command)
        db.session.commit()

        row = SubscriptionCommandExecution.query.filter_by(command_key=f"test-command-log:{company.id}:{paid_plan.id}").first()
        assert row is not None
        assert row.command_name == "ChangePlanCommand"
        assert row.company_id == company.id


def test_superadmin_create_subscription_keeps_trial_when_company_is_new():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        company.created_at = stock_app.utcnow()
        company.trial_ends_at = stock_app.utcnow() + timedelta(days=10)

        Subscription.query.filter_by(company_id=company.id).delete(synchronize_session=False)
        db.session.commit()

        paid_plan = Plan.query.filter_by(code="entrepreneur").first()
        assert paid_plan is not None
        company_id = company.id
        paid_plan_id = paid_plan.id

    login = client.post("/auth/login", data={"username": "superadmin", "password": "admin123"}, follow_redirects=False)
    assert login.status_code in (301, 302)

    response = client.post(
        "/superadmin/subscriptions/create",
        data={
            "company_id": company_id,
            "plan_id": paid_plan_id,
            "status": "pending",
            "renewal_enabled": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import Subscription

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        subscription = (
            Subscription.query.filter_by(company_id=company.id)
            .order_by(Subscription.id.desc())
            .first()
        )
        assert subscription is not None
        assert (subscription.status or "").lower() == "trial"
        assert subscription.trial_end == company.trial_ends_at
        assert subscription.next_billing_date == company.trial_ends_at
        assert subscription.ends_at == company.trial_ends_at


def test_active_subscription_with_past_billing_date_is_blocked():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        paid_plan = Plan.query.filter_by(code="business").first()
        assert paid_plan is not None

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)

        subscription.plan_id = paid_plan.id
        subscription.status = "active"
        subscription.trial_end = None
        subscription.start_date = stock_app.utcnow() - timedelta(days=45)
        subscription.starts_at = stock_app.utcnow() - timedelta(days=45)
        subscription.ends_at = stock_app.utcnow() - timedelta(days=6)
        subscription.next_billing_date = stock_app.utcnow() - timedelta(days=6)
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    blocked_dashboard = client.get("/dashboard/", follow_redirects=False)
    assert blocked_dashboard.status_code in (301, 302)
    assert "/access-status" in (blocked_dashboard.headers.get("Location") or "")


def test_active_subscription_within_grace_period_keeps_access():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        paid_plan = Plan.query.filter_by(code="business").first()
        assert paid_plan is not None

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)

        subscription.plan_id = paid_plan.id
        subscription.status = "active"
        subscription.trial_end = None
        subscription.start_date = stock_app.utcnow() - timedelta(days=45)
        subscription.starts_at = stock_app.utcnow() - timedelta(days=45)
        subscription.ends_at = stock_app.utcnow() - timedelta(days=2)
        subscription.next_billing_date = stock_app.utcnow() - timedelta(days=2)
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    dashboard = client.get("/dashboard/", follow_redirects=False)
    assert dashboard.status_code == 200


def test_manual_subscription_with_past_billing_date_is_blocked():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        paid_plan = Plan.query.filter_by(code="business").first()
        assert paid_plan is not None

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)

        subscription.plan_id = paid_plan.id
        subscription.status = "active"
        subscription.trial_end = None
        subscription.start_date = stock_app.utcnow() - timedelta(days=45)
        subscription.starts_at = stock_app.utcnow() - timedelta(days=45)
        subscription.ends_at = stock_app.utcnow() - timedelta(days=1)
        subscription.next_billing_date = stock_app.utcnow() - timedelta(days=1)
        subscription.metadata_json = '{"is_manual": true, "managed_by": "admin"}'
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    blocked_dashboard = client.get("/dashboard/", follow_redirects=False)
    assert blocked_dashboard.status_code in (301, 302)
    assert "/access-status" in (blocked_dashboard.headers.get("Location") or "")


def test_manual_subscription_is_unblocked_after_approved_payment_webhook():
    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        paid_plan = Plan.query.filter_by(code="business").first()
        assert paid_plan is not None

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)

        now_ref = stock_app.utcnow()
        subscription.plan_id = paid_plan.id
        subscription.status = "active"
        subscription.start_date = now_ref - timedelta(days=45)
        subscription.starts_at = now_ref - timedelta(days=45)
        subscription.next_billing_date = now_ref - timedelta(days=1)
        subscription.ends_at = now_ref - timedelta(days=1)
        subscription.metadata_json = '{"is_manual": true, "managed_by": "admin"}'
        db.session.commit()

        state_before = SubscriptionService.resolve_company_access_state(company, subscription=subscription, now=now_ref)
        assert state_before["can_access"] is False

        SubscriptionService.run_command(
            db.session,
            SubscriptionService.RenewSubscriptionCommand(
                company_id=company.id,
                subscription_id=subscription.id,
                payment_status="approved",
                origin="webhook",
                actor_role="system",
                idempotency_key=f"test-manual-webhook-approved:{company.id}:{subscription.id}",
            ),
        )
        db.session.commit()

        refreshed = Subscription.query.filter_by(id=subscription.id).first()
        assert refreshed is not None
        state_after = SubscriptionService.resolve_company_access_state(company, subscription=refreshed, now=stock_app.utcnow())
        assert state_after["can_access"] is True
        assert state_after["status"] == SubscriptionService.STATE_ACTIVE
        assert refreshed.next_billing_date is not None
        assert refreshed.next_billing_date > now_ref


def test_active_subscription_blocks_at_exact_five_day_overdue_cutoff():
    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        paid_plan = Plan.query.filter_by(code="business").first()
        assert paid_plan is not None

        now_ref = stock_app.utcnow()
        paid_limit = now_ref - timedelta(days=5)

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)

        subscription.plan_id = paid_plan.id
        subscription.status = "active"
        subscription.trial_end = None
        subscription.start_date = now_ref - timedelta(days=45)
        subscription.starts_at = now_ref - timedelta(days=45)
        subscription.ends_at = paid_limit
        subscription.next_billing_date = paid_limit
        db.session.commit()

        access = SubscriptionService.resolve_company_access_state(company, subscription=subscription, now=now_ref)
        assert access["can_access"] is False
        assert access["status"] == SubscriptionService.STATE_EXPIRED


def test_active_subscription_without_due_dates_uses_derived_plan_limit():
    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        paid_plan = Plan.query.filter_by(code="business").first()
        assert paid_plan is not None

        now_ref = stock_app.utcnow()
        overdue_days = int(paid_plan.duration_days or 30) + 6
        start_ref = now_ref - timedelta(days=overdue_days)

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)

        subscription.plan_id = paid_plan.id
        subscription.status = "active"
        subscription.trial_end = None
        subscription.start_date = start_ref
        subscription.starts_at = start_ref
        subscription.ends_at = None
        subscription.next_billing_date = None
        db.session.commit()

        access = SubscriptionService.resolve_company_access_state(company, subscription=subscription, now=now_ref)
        assert access["can_access"] is False
        assert access["status"] == SubscriptionService.STATE_EXPIRED


def test_effective_status_active_with_future_due_date_is_active():
    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        plan = Plan.query.filter_by(code="business").first()
        assert plan is not None

        now_ref = stock_app.utcnow()
        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)

        subscription.plan_id = plan.id
        subscription.status = "active"
        subscription.start_date = now_ref - timedelta(days=2)
        subscription.starts_at = now_ref - timedelta(days=2)
        subscription.next_billing_date = now_ref + timedelta(days=2)
        subscription.ends_at = now_ref + timedelta(days=2)
        subscription.metadata_json = '{"is_manual": true, "managed_by": "admin"}'
        db.session.commit()

        effective = SubscriptionService.get_effective_subscription_status(subscription, company=company, now=now_ref)
        assert effective == SubscriptionService.STATE_ACTIVE


def test_effective_status_manual_subscription_expires_exactly_now():
    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        plan = Plan.query.filter_by(code="business").first()
        assert plan is not None

        now_ref = stock_app.utcnow()
        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)

        subscription.plan_id = plan.id
        subscription.status = "active"
        subscription.start_date = now_ref - timedelta(days=30)
        subscription.starts_at = now_ref - timedelta(days=30)
        subscription.next_billing_date = now_ref
        subscription.ends_at = now_ref
        subscription.metadata_json = '{"is_manual": true, "managed_by": "admin"}'
        db.session.commit()

        effective = SubscriptionService.get_effective_subscription_status(subscription, company=company, now=now_ref)
        assert effective == SubscriptionService.STATE_EXPIRED


def test_effective_status_trial_expires_exactly_now():
    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService
        from services.subscription_service import SubscriptionService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        plan = Plan.query.filter_by(code="trial").first()
        assert plan is not None

        now_ref = stock_app.utcnow()
        company.trial_ends_at = now_ref
        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)

        subscription.plan_id = plan.id
        subscription.status = "trial"
        subscription.trial_end = now_ref
        subscription.start_date = now_ref - timedelta(days=10)
        subscription.starts_at = now_ref - timedelta(days=10)
        subscription.next_billing_date = now_ref
        subscription.ends_at = now_ref
        db.session.commit()

        effective = SubscriptionService.get_effective_subscription_status(subscription, company=company, now=now_ref)
        assert effective == SubscriptionService.STATE_TRIAL_EXPIRED


def test_superadmin_update_subscription_date_to_future_keeps_effective_active():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        plan = Plan.query.filter_by(code="business").first()
        assert plan is not None

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)
        subscription.plan_id = plan.id
        subscription.status = "active"
        subscription.metadata_json = '{"is_manual": true, "managed_by": "admin"}'
        db.session.commit()

        subscription_id = subscription.id
        plan_id = plan.id

    login = client.post("/auth/login", data={"username": "superadmin", "password": "admin123"}, follow_redirects=False)
    assert login.status_code in (301, 302)

    future_local = (stock_app.utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    response = client.post(
        f"/superadmin/subscriptions/{subscription_id}/update",
        data={
            "plan_id": plan_id,
            "status": "active",
            "next_billing_date": future_local,
            "renewal_enabled": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import Subscription
        from services.subscription_service import SubscriptionService

        refreshed = Subscription.query.filter_by(id=subscription_id).first()
        company = Company.query.filter_by(id=refreshed.company_id).first()
        assert refreshed is not None
        assert company is not None
        effective = SubscriptionService.get_effective_subscription_status(refreshed, company=company, now=stock_app.utcnow())
        assert effective == SubscriptionService.STATE_ACTIVE


def test_superadmin_update_subscription_date_to_past_sets_effective_expired():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        plan = Plan.query.filter_by(code="business").first()
        assert plan is not None

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)
        subscription.plan_id = plan.id
        subscription.status = "active"
        subscription.metadata_json = '{"is_manual": true, "managed_by": "admin"}'
        db.session.commit()

        subscription_id = subscription.id
        plan_id = plan.id

    login = client.post("/auth/login", data={"username": "superadmin", "password": "admin123"}, follow_redirects=False)
    assert login.status_code in (301, 302)

    past_local = (stock_app.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    start_local = (stock_app.utcnow() - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M")
    response = client.post(
        f"/superadmin/subscriptions/{subscription_id}/update",
        data={
            "plan_id": plan_id,
            "status": "active",
            "start_date": start_local,
            "next_billing_date": past_local,
            "renewal_enabled": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import Subscription
        from services.subscription_service import SubscriptionService

        refreshed = Subscription.query.filter_by(id=subscription_id).first()
        company = Company.query.filter_by(id=refreshed.company_id).first()
        assert refreshed is not None
        assert company is not None
        effective = SubscriptionService.get_effective_subscription_status(refreshed, company=company, now=stock_app.utcnow())
        assert effective == SubscriptionService.STATE_EXPIRED


def test_superadmin_subscriptions_filter_uses_effective_status():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import Plan, Subscription
        from services.plan_service import PlanService

        PlanService.ensure_defaults(db.session)
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        plan = Plan.query.filter_by(code="business").first()
        assert plan is not None

        subscription = Subscription.query.filter_by(company_id=company.id).first()
        if subscription is None:
            subscription = Subscription(company_id=company.id)
            db.session.add(subscription)

        now_ref = stock_app.utcnow()
        subscription.plan_id = plan.id
        subscription.status = "active"
        subscription.start_date = now_ref - timedelta(days=15)
        subscription.starts_at = now_ref - timedelta(days=15)
        subscription.next_billing_date = now_ref - timedelta(days=1)
        subscription.ends_at = now_ref - timedelta(days=1)
        subscription.metadata_json = '{"is_manual": true, "managed_by": "admin"}'
        db.session.commit()

    login = client.post("/auth/login", data={"username": "superadmin", "password": "admin123"}, follow_redirects=False)
    assert login.status_code in (301, 302)

    response = client.get("/superadmin/subscriptions?status=expired&q=Empresa%20Demo", follow_redirects=False)
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "Empresa Demo" in html
    assert "Vencida" in html


def test_business_billing_hub_allows_admin_and_shows_core_sections():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

    response = client.get("/admin/facturacion")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Administración profesional de comprobantes" in html
    assert "Configuración fiscal" in html
    assert "Facturación electrónica" in html


def test_business_billing_hub_denies_user_without_billing_permissions():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.get("/admin/facturacion", follow_redirects=False)
    assert response.status_code == 403


def test_business_billing_sale_annul_marks_sale_status_for_admin():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    open_cash_session(client)

    checkout = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": 1, "quantity": 1}], "metodo_pago": "EFECTIVO"},
        headers={"X-Cart-Tenant": "1:2"},
    )
    assert checkout.status_code == 200
    sale_id = int(checkout.get_json()["sale_id"])

    response = client.post(f"/admin/facturacion/comprobantes/venta/{sale_id}/anular", follow_redirects=False)
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        sale = db.session.get(Sale, sale_id)
        assert sale is not None
        assert (sale.status or "").lower() == "anulada"


def test_business_billing_sale_annul_denies_non_admin():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    with stock_app.app.app_context():
        sale = Sale(
            customer="Cliente demo",
            subtotal=10,
            discount=0,
            tax=0,
            total_amount=10,
            payment_method="EFECTIVO",
            company_id=1,
            seller_id=1,
            status="confirmada",
        )
        db.session.add(sale)
        db.session.commit()
        sale_id = sale.id

    response = client.post(f"/admin/facturacion/comprobantes/venta/{sale_id}/anular", follow_redirects=False)
    assert response.status_code == 403


def test_business_billing_issue_sale_document_persists_numbering_sequence():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    open_cash_session(client)

    response_a = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": 1, "quantity": 1}], "metodo_pago": "EFECTIVO"},
        headers={"X-Cart-Tenant": "1:2"},
    )
    assert response_a.status_code == 200
    sale_id_a = int(response_a.get_json()["sale_id"])

    response_b = client.post(
        "/ventas/api/checkout",
        json={"items": [{"productId": 1, "quantity": 0.5}], "metodo_pago": "EFECTIVO"},
        headers={"X-Cart-Tenant": "1:2"},
    )
    assert response_b.status_code == 200
    sale_id_b = int(response_b.get_json()["sale_id"])

    issue_a = client.post(f"/admin/facturacion/comprobantes/venta/{sale_id_a}/emitir", follow_redirects=False)
    issue_b = client.post(f"/admin/facturacion/comprobantes/venta/{sale_id_b}/emitir", follow_redirects=False)
    assert issue_a.status_code in (301, 302)
    assert issue_b.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import BusinessDocument

        docs = (
            BusinessDocument.query.filter_by(company_id=1, source_type="sale")
            .order_by(BusinessDocument.seq_number.asc())
            .all()
        )
        assert len(docs) == 2
        assert docs[0].document_number == "00001-00000001"
        assert docs[1].document_number == "00001-00000002"
        assert docs[0].source_id == sale_id_a
        assert docs[1].source_id == sale_id_b


def test_plan_catalog_sync_updates_existing_plan_values():
    with stock_app.app.app_context():
        from app import Plan
        from services.plan_service import PlanService

        PlanService.ensure_defaults(db.session)
        plan = Plan.query.filter_by(code="entrepreneur").first()
        assert plan is not None

        plan.price = 1
        plan.max_users = 99
        plan.features_json = "legacy"
        db.session.commit()

        PlanService.ensure_defaults(db.session)
        db.session.refresh(plan)

        assert float(plan.price or 0) == 12999.0
        assert int(plan.max_users or 0) == 3
        assert plan.features_json == "inventario,ventas,clientes,reportes,excel"


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
            referral_url="https://www.stockarmobile.com/?ref=REF7777",
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
        owner_user = User.query.filter_by(username="nuevo_ref").first()
        assert owner_user is not None
        assert owner_user.role == "admin"


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
            referral_url="https://www.stockarmobile.com/?ref=REF8888",
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
                referral_url="https://www.stockarmobile.com/?ref=REF1234",
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


def test_referral_commission_uses_seller_specific_percent():
    from services.referral_service import ReferralService

    with stock_app.app.app_context():
        from app import Plan, ReferralSeller, ReferralCommission

        company = Company.query.filter_by(name="Empresa Demo").first()
        user = User.query.filter_by(username="empresa_admin").first()
        assert company is not None
        assert user is not None

        seller_user = User(username="seller_percent", email="seller_percent@test.local", role="seller", active=True)
        seller_user.set_password("seller123")
        db.session.add(seller_user)
        db.session.flush()

        seller = ReferralSeller(
            user_id=seller_user.id,
            dni="30999111",
            referral_code="REFPCT1",
            referral_url="https://www.stockarmobile.com/?ref=REFPCT1",
            commission_percent=0.4500,
            active=True,
        )
        db.session.add(seller)
        db.session.flush()

        db.session.add(
            ReferralAttribution(
                seller_id=seller.id,
                company_id=company.id,
                user_id=user.id,
                referral_code="REFPCT1",
            )
        )

        plan = Plan(code="plan_ref_45", name="Plan Ref 45", price=10000, currency="ARS", duration_days=30, active=True)
        db.session.add(plan)
        db.session.commit()

        commission = ReferralService.create_commission_for_sale(db.session, company_id=company.id, plan=plan)
        db.session.commit()

        persisted = ReferralCommission.query.filter_by(id=commission.id).first()
        assert persisted is not None
        assert float(persisted.commission_percent) == 0.45
        assert float(persisted.commission_amount) == 4500.00


def test_superadmin_can_update_seller_commission_and_seller_cannot():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        from app import ReferralSeller

        seller_company = Company(name="Empresa Seller Comision", active=True)
        db.session.add(seller_company)
        db.session.flush()

        seller_user = User(
            username="seller_edit_comm",
            email="seller_edit_comm@test.local",
            role="seller",
            company_id=seller_company.id,
            active=True,
        )
        seller_user.set_password("seller123")
        db.session.add(seller_user)
        db.session.flush()

        seller = ReferralSeller(
            user_id=seller_user.id,
            dni="30111999",
            referral_code="REFEDIT1",
            referral_url="https://www.stockarmobile.com/?ref=REFEDIT1",
            active=True,
        )
        db.session.add(seller)
        db.session.commit()
        seller_id = seller.id

    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})
    response = client.post(
        f"/superadmin/referrals/sellers/{seller_id}/commission",
        data={"commission_percent": "42"},
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import ReferralSeller

        updated = ReferralSeller.query.filter_by(id=seller_id).first()
        assert updated is not None
        assert float(updated.commission_percent) == 0.42

    client.post("/auth/logout")
    client.post("/auth/login", data={"username": "seller_edit_comm", "password": "seller123"})
    forbidden = client.post(
        f"/superadmin/referrals/sellers/{seller_id}/commission",
        data={"commission_percent": "55"},
        follow_redirects=False,
    )
    assert forbidden.status_code == 403


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
            referral_url="https://www.stockarmobile.com/?ref=REF9999",
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
        assert subscription.status == "pending_payment"

        payment_row = Payment.query.filter_by(payment_id="mp-pay-pending").first()
        assert payment_row is not None
        assert payment_row.status == "pending"


def test_paid_plan_change_keeps_current_subscription_until_payment_is_approved():
    from services.subscription_service import SubscriptionService

    with stock_app.app.app_context():
        from app import Plan, Subscription, User

        company = Company.query.filter_by(name="Empresa Demo").first()
        user = User.query.filter_by(username="empresa_admin").first()
        assert company is not None
        assert user is not None

        current_plan = Plan(code="current_paid_plan", name="Plan actual", price=10000, currency="ARS", duration_days=30, active=True)
        next_plan = Plan(code="next_paid_plan", name="Plan nuevo", price=20000, currency="ARS", duration_days=30, active=True)
        db.session.add_all([current_plan, next_plan])
        db.session.flush()

        current = Subscription(
            company_id=company.id,
            plan_id=current_plan.id,
            status="active",
            start_date=stock_app.utcnow(),
            starts_at=stock_app.utcnow(),
            next_billing_date=stock_app.utcnow() + timedelta(days=30),
            ends_at=stock_app.utcnow() + timedelta(days=30),
            renewal_enabled=True,
            auto_renew=True,
        )
        db.session.add(current)
        db.session.flush()
        original_end = current.ends_at

        command = SubscriptionService.ChangePlanCommand(
            company_id=company.id,
            plan_id=next_plan.id,
            actor_user_id=user.id,
            actor_role="user",
            origin="test",
            idempotency_key=f"pending-plan-change:{company.id}:{next_plan.id}",
        )
        result = SubscriptionService.run_command(db.session, command)
        candidate = db.session.get(Subscription, result.subscription_id)
        db.session.flush()

        assert candidate is not None
        assert candidate.status == "pending_payment"
        assert SubscriptionService._metadata_dict(candidate)["pending_plan_change"] is True
        assert current.status == "active"
        assert current.plan_id == current_plan.id
        assert current.ends_at == original_end
        assert SubscriptionService.active_subscription_for_company(company.id).id == current.id

        SubscriptionService.run_command(
            db.session,
            SubscriptionService.ExpireSubscriptionCommand(
                company_id=company.id,
                subscription_id=candidate.id,
                actor_user_id=user.id,
                origin="webhook",
                reason="webhook_rejected",
                idempotency_key=f"reject-plan-change:{candidate.id}",
            ),
        )
        assert current.status == "active"
        assert SubscriptionService.active_subscription_for_company(company.id).id == current.id

        approved_candidate = SubscriptionService.run_command(
            db.session,
            SubscriptionService.ChangePlanCommand(
                company_id=company.id,
                plan_id=next_plan.id,
                actor_user_id=user.id,
                actor_role="user",
                origin="test",
                idempotency_key=f"approved-plan-change:{company.id}:{next_plan.id}",
            ),
        )
        replacement = db.session.get(Subscription, approved_candidate.subscription_id)
        SubscriptionService.run_command(
            db.session,
            SubscriptionService.RenewSubscriptionCommand(
                company_id=company.id,
                subscription_id=replacement.id,
                payment_status="approved",
                actor_user_id=user.id,
                origin="webhook",
                idempotency_key=f"approve-plan-change:{replacement.id}",
            ),
        )

        assert current.status == "cancelled"
        assert replacement.status == "active"
        assert replacement.plan_id == next_plan.id
        assert SubscriptionService.active_subscription_for_company(company.id).id == replacement.id


def test_pos_qr_create_generates_qr_and_reuses_pending_draft(monkeypatch):
    from services.mercadopago_oauth_service import MercadoPagoOAuthService
    from services.mercadopago_service import MercadoPagoService

    with stock_app.app.app_context():
        from app import Payment, User

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        client = stock_app.app.test_client()
        client.post("/auth/login", data={"username": user.username, "password": "admin123"})
        open_cash_session(client)

        monkeypatch.setattr(
            MercadoPagoService,
            "create_pos_checkout_preference",
            lambda self, **kwargs: {
                "id": "pref-pos-1",
                "init_point": "https://mercadopago.test/checkout/pref-pos-1",
                "sandbox_init_point": "https://sandbox.mercadopago.test/checkout/pref-pos-1",
            },
        )
        monkeypatch.setattr(MercadoPagoOAuthService, "ensure_access_token", lambda self, *, company_id: "company-access-token")

        payload = {
            "items": [{"productId": 1, "quantity": 1, "name": "Yerba kilo", "price": 18000, "barcode": "123456789012"}],
            "client_id": "",
            "document_type": "venta",
            "note": "",
        }
        headers = {"X-Cart-Tenant": f"{company.id}:{user.id}"}

        first_response = client.post("/ventas/api/mp-qr/create", json=payload, headers=headers)
        assert first_response.status_code == 200
        first_data = first_response.get_json()
        assert first_data["status"] == "created"
        assert first_data["total"] == 18000.0
        assert first_data["qr_data_uri"].startswith("data:image/png;base64,")

        payment_row = Payment.query.filter_by(id=first_data["payment_id"], company_id=company.id).first()
        assert payment_row is not None
        assert payment_row.preference_id == "pref-pos-1"
        assert payment_row.external_reference
        assert str(payment_row.external_reference).startswith("flow:pos_sale|draft_payment_id:")

        status_response = client.get(f"/ventas/api/mp-qr/status?draft_id={first_data['payment_id']}")
        assert status_response.status_code == 200
        assert status_response.get_json()["status"] == "pending"

        second_response = client.post("/ventas/api/mp-qr/create", json=payload, headers=headers)
        assert second_response.status_code == 200
        second_data = second_response.get_json()
        assert second_data["status"] == "reused"
        assert second_data["payment_id"] == first_data["payment_id"]

        assert Payment.query.filter_by(company_id=company.id).count() == 1


def test_pos_qr_points_lists_company_catalog(monkeypatch):
    from services.mercadopago_oauth_service import MercadoPagoOAuthService
    from services.mercadopago_service import MercadoPagoService

    with stock_app.app.app_context():
        from app import User

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        client = stock_app.app.test_client()
        client.post("/auth/login", data={"username": user.username, "password": "admin123"})

        monkeypatch.setattr(MercadoPagoOAuthService, "ensure_access_token", lambda self, *, company_id: "company-access-token")
        monkeypatch.setattr(
            MercadoPagoService,
            "debug_fetch_pos_catalog",
            lambda self, **kwargs: {
                "path": "/pos?limit=50&offset=0",
                "status_code": 200,
                "pos_count": 1,
                "response": {
                    "results": [
                        {
                            "id": "POS-1",
                            "name": "Caja principal",
                            "external_id": "EXT-1",
                            "store_id": "STORE-1",
                            "store_name": "Sucursal Centro",
                            "status": "active",
                        }
                    ]
                },
            },
        )

        response = client.get("/ventas/api/mp-qr/points")
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "ok"
        assert data["count"] == 1
        assert data["points"][0]["id"] == "POS-1"


def test_pos_qr_create_requires_selected_pos_when_catalog_available(monkeypatch):
    from services.mercadopago_oauth_service import MercadoPagoOAuthService
    from services.mercadopago_service import MercadoPagoService

    with stock_app.app.app_context():
        from app import User

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        client = stock_app.app.test_client()
        client.post("/auth/login", data={"username": user.username, "password": "admin123"})
        open_cash_session(client)

        monkeypatch.setattr(MercadoPagoOAuthService, "ensure_access_token", lambda self, *, company_id: "company-access-token")
        monkeypatch.setattr(
            MercadoPagoService,
            "debug_fetch_pos_catalog",
            lambda self, **kwargs: {
                "path": "/pos?limit=50&offset=0",
                "status_code": 200,
                "pos_count": 1,
                "response": {
                    "results": [
                        {
                            "id": "POS-REQ-1",
                            "name": "Caja mostrador",
                        }
                    ]
                },
            },
        )
        monkeypatch.setattr(
            MercadoPagoService,
            "create_pos_checkout_preference",
            lambda self, **kwargs: {
                "id": "pref-pos-req",
                "init_point": "https://mercadopago.test/checkout/pref-pos-req",
                "sandbox_init_point": "https://sandbox.mercadopago.test/checkout/pref-pos-req",
            },
        )

        headers = {"X-Cart-Tenant": f"{company.id}:{user.id}"}
        base_payload = {
            "items": [{"productId": 1, "quantity": 1, "name": "Yerba kilo", "price": 18000, "barcode": "123456789012"}],
            "client_id": "",
            "document_type": "venta",
            "note": "",
        }

        without_pos = client.post("/ventas/api/mp-qr/create", json=base_payload, headers=headers)
        assert without_pos.status_code == 400
        assert "seleccionar" in (without_pos.get_json() or {}).get("error", "").lower()

        with_pos = client.post(
            "/ventas/api/mp-qr/create",
            json={**base_payload, "mp_pos_id": "POS-REQ-1"},
            headers=headers,
        )
        assert with_pos.status_code == 200
        assert (with_pos.get_json() or {}).get("status") == "created"


def test_pos_qr_create_returns_real_exception_traceback(monkeypatch):
    from services.mercadopago_oauth_service import MercadoPagoOAuthService
    from services.mercadopago_service import MercadoPagoService

    with stock_app.app.app_context():
        from app import User

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin".first()) if False else User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        client = stock_app.app.test_client()
        client.post("/auth/login", data={"username": user.username, "password": "admin123"})
        open_cash_session(client)

        monkeypatch.setattr(MercadoPagoOAuthService, "ensure_access_token", lambda self, *, company_id: "company-access-token")
        monkeypatch.setattr(
            MercadoPagoService,
            "debug_fetch_pos_catalog",
            lambda self, **kwargs: {
                "path": "/pos?limit=50&offset=0",
                "status_code": 200,
                "pos_count": 1,
                "response": {"results": [{"id": "POS-ERR-1", "name": "Caja principal", "status": "active"}]},
            },
        )
        monkeypatch.setattr(
            MercadoPagoService,
            "create_pos_checkout_preference",
            lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("MP preference failed at line test")),
        )

        headers = {"X-Cart-Tenant": f"{company.id}:{user.id}"}
        payload = {
            "items": [{"productId": 1, "quantity": 1, "name": "Yerba kilo", "price": 18000, "barcode": "123456789012"}],
            "client_id": "",
            "document_type": "venta",
            "note": "",
            "mp_pos_id": "POS-ERR-1",
        }

        response = client.post("/ventas/api/mp-qr/create", json=payload, headers=headers)
        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert data["exception"] == "RuntimeError"
        assert "MP preference failed at line test" in data["error"]
        assert "traceback" in data and "create_pos_checkout_preference" in data["traceback"]


def test_pos_qr_create_early_exception_breaks_before_json_return():
    with stock_app.app.app_context():
        from app import User

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        client = stock_app.app.test_client()
        client.post("/auth/login", data={"username": user.username, "password": "admin123"})
        open_cash_session(client)

        headers = {"X-Cart-Tenant": f"{company.id}:{user.id}"}
        payload = {
            "items": [{"productId": 999999, "quantity": 1, "name": "Fantasma", "price": 10, "barcode": "000"}],
            "client_id": "",
            "document_type": "venta",
            "note": "",
        }

        response = client.post("/ventas/api/mp-qr/create", json=payload, headers=headers)
        assert response.status_code == 400
        data = response.get_json()
        assert data is not None
        assert data["success"] is False
        assert data["exception"] == "ValueError"
        assert "traceback" in data


def test_pos_qr_webhook_approved_updates_single_draft_payment(monkeypatch):
    from services.webhook_service import WebhookService

    with stock_app.app.app_context():
        from app import Payment, User, WebhookEvent

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        draft_payment = Payment(
            payment_id=None,
            preference_id="pref-pos-2",
            external_reference=f"flow:pos_sale|draft_payment_id:1|company_id:{company.id}|user_id:{user.id}|cart_hash:abc123",
            company_id=company.id,
            user_id=user.id,
            amount=18000,
            currency="ARS",
            status="pending",
            payment_method="QR Mercado Pago",
            provider="mercadopago_pos",
            reference="pos_draft:1",
            payload_json='{"flow": "pos_sale", "snapshot": {"cart_hash": "abc123", "items": [{"productId": 1, "quantity": 1}]}}',
        )
        db.session.add(draft_payment)
        db.session.flush()

        draft_payment.external_reference = f"flow:pos_sale|draft_payment_id:{draft_payment.id}|company_id:{company.id}|user_id:{user.id}|cart_hash:abc123"
        db.session.commit()

        approved_payment = {
            "id": "mp-pos-1",
            "status": "approved",
            "date_last_updated": "2026-07-14T11:00:00Z",
            "date_approved": "2026-07-14T11:00:00Z",
            "transaction_amount": 18000,
            "currency_id": "ARS",
            "payment_method_id": "qr",
            "external_reference": draft_payment.external_reference,
            "metadata": {
                "flow": "pos_sale",
                "company_id": company.id,
                "user_id": user.id,
                "draft_payment_id": draft_payment.id,
                "total_amount": 18000,
            },
        }

        service = WebhookService()
        monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
        monkeypatch.setattr(service.mp_service, "get_payment", lambda payment_id: approved_payment)

        result = service.process(
            db_session=db.session,
            headers={"x-request-id": "rq-pos-1", "x-signature": "ts=1,v1=abc"},
            payload={"id": "evt-pos-1", "type": "payment", "data": {"id": "mp-pos-1"}},
        )

        assert result["status"] == "processed"

        db.session.refresh(draft_payment)
        assert draft_payment.payment_id == "mp-pos-1"
        assert draft_payment.status == "approved"
        assert draft_payment.provider == "mercadopago_pos"
        assert Payment.query.count() == 1
        assert WebhookEvent.query.count() == 1

        duplicate = service.process(
            db_session=db.session,
            headers={"x-request-id": "rq-pos-1", "x-signature": "ts=1,v1=abc"},
            payload={"id": "evt-pos-1", "type": "payment", "data": {"id": "mp-pos-1"}},
        )
        assert duplicate["status"] == "duplicate"


def test_mercado_pago_oauth_connection_lifecycle(monkeypatch):
    from services.mercadopago_oauth_service import MercadoPagoOAuthService

    with stock_app.app.app_context():
        from app import MercadoPagoConnection

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        service = MercadoPagoOAuthService()
        token_payload = {
            "access_token": "access-token-1",
            "refresh_token": "refresh-token-1",
            "expires_in": 60,
            "scope": "offline_access read write",
            "token_type": "bearer",
        }
        profile = {
            "id": "mp-user-1",
            "first_name": "Juan Pérez",
            "email": "juan@email.com",
            "country_id": "AR",
        }

        connection = service.save_connection(company_id=company.id, token_payload=token_payload, profile=profile)
        db.session.commit()

        summary = service.summarize_connection(connection)
        assert summary["connected"] is True
        assert summary["account_name"] == "Juan Pérez"
        assert summary["account_email"] == "juan@email.com"
        assert summary["country"] == "AR"

        assert service.decrypt_value(connection.access_token_encrypted) == "access-token-1"
        assert service.decrypt_value(connection.refresh_token_encrypted) == "refresh-token-1"

        monkeypatch.setattr(service, "refresh_tokens", lambda *, refresh_token: {
            "access_token": "access-token-2",
            "refresh_token": refresh_token,
            "expires_in": 120,
            "scope": "offline_access read write",
            "token_type": "bearer",
        })
        monkeypatch.setattr(service, "fetch_user_profile", lambda *, access_token: {
            "id": "mp-user-1",
            "first_name": "Juan Pérez",
            "email": "juan@email.com",
            "country_id": "AR",
        })

        connection.token_expires_at = stock_app.utcnow() - timedelta(minutes=1)
        db.session.commit()

        refreshed = service.refresh_connection(company_id=company.id)
        assert service.decrypt_value(refreshed.access_token_encrypted) == "access-token-2"
        assert refreshed.status == "connected"

        disconnected = service.disconnect(company_id=company.id)
        assert disconnected.status == "disconnected"
        assert disconnected.access_token_encrypted is None
        assert disconnected.refresh_token_encrypted is None

        stored = MercadoPagoConnection.query.filter_by(company_id=company.id).first()
        assert stored is not None
        assert stored.status == "disconnected"


def test_mercado_pago_refresh_failure_disconnects_company(monkeypatch):
    from services.mercadopago_oauth_service import MercadoPagoOAuthService

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        service = MercadoPagoOAuthService()
        connection = service.save_connection(
            company_id=company.id,
            token_payload={
                "access_token": "access-token-expired",
                "refresh_token": "refresh-token-expired",
                "expires_in": 1,
                "token_type": "bearer",
                "scope": "read write",
            },
            profile={"id": "mp-user-expired", "first_name": "Juan", "email": "juan@email.com", "country_id": "AR"},
        )
        db.session.commit()

        connection.token_expires_at = stock_app.utcnow() - timedelta(minutes=10)
        db.session.commit()

        monkeypatch.setattr(service, "refresh_tokens", lambda *, refresh_token: (_ for _ in ()).throw(RuntimeError("refresh denied")))
        monkeypatch.setattr(service, "fetch_user_profile", lambda *, access_token: {})

        with pytest.raises(RuntimeError, match="Mercado Pago requiere una nueva autorización"):
            service.ensure_access_token(company_id=company.id)

        refreshed = service.get_connection(company.id)
        assert refreshed is not None
        assert refreshed.status == "disconnected"
        assert refreshed.access_token_encrypted is None
        assert refreshed.refresh_token_encrypted is None


def test_mercado_pago_oauth_route_starts_and_completes(monkeypatch):
    from services.mercadopago_oauth_service import MercadoPagoOAuthService
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setenv("MP_CLIENT_ID", "client-id-123")
    monkeypatch.setenv("MP_CLIENT_SECRET", "client-secret-123")

    with stock_app.app.app_context():
        from app import MercadoPagoConnection

        client = stock_app.app.test_client()
        client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

        start_response = client.post("/admin/mercado-pago")
        assert start_response.status_code in (301, 302)
        location = start_response.headers.get("Location") or ""
        parsed = urlsplit(location)
        params = parse_qs(parsed.query)
        assert parsed.scheme == "https"
        assert parsed.netloc == "auth.mercadopago.com.ar"
        assert parsed.path == "/authorization"
        assert params["client_id"] == ["client-id-123"]
        assert params["response_type"] == ["code"]
        assert params["platform_id"] == ["mp"]
        assert params["state"]
        assert params["redirect_uri"] == ["https://www.stockarmobile.com/admin/mercado-pago/callback"]

        with client.session_transaction() as sess:
            state = sess.get("mp_oauth_state_1")
        assert state

        monkeypatch.setattr(MercadoPagoOAuthService, "exchange_code", lambda self, *, code, redirect_uri: {
            "access_token": "oauth-access-token",
            "refresh_token": "oauth-refresh-token",
            "expires_in": 1800,
            "scope": "offline_access read write",
            "token_type": "bearer",
        })
        monkeypatch.setattr(MercadoPagoOAuthService, "fetch_user_profile", lambda self, *, access_token: {
            "id": "mp-user-99",
            "first_name": "Juan Pérez",
            "email": "juan@email.com",
            "country_id": "AR",
        })

        callback_response = client.get(f"/admin/mercado-pago/callback?code=auth-code-123&state={state}", follow_redirects=False)
        assert callback_response.status_code in (301, 302)

        connection = MercadoPagoConnection.query.filter_by(company_id=1).first()
        assert connection is not None
        assert connection.status == "connected"
        assert connection.mp_user_id == "mp-user-99"
        assert connection.account_email == "juan@email.com"


def test_mercado_pago_oauth_uses_configured_redirect_uri(monkeypatch):
    from services.mercadopago_oauth_service import MercadoPagoOAuthService

    monkeypatch.setenv("MP_CLIENT_ID", "client-id-123")
    monkeypatch.setenv("MP_CLIENT_SECRET", "client-secret-123")
    monkeypatch.setenv("MP_OAUTH_ENCRYPTION_KEY", "oauth-encryption-test-key")
    monkeypatch.setenv("MP_OAUTH_REDIRECT_URI", "https://www.stockarmobile.com/admin/mercado-pago/callback")

    captured = {}

    def fake_build_authorization_url(self, *, state, redirect_uri):
        captured["redirect_uri"] = redirect_uri
        captured["state"] = state
        return f"https://auth.mercadopago.com.ar/authorization?client_id={self._client_id()}&state={state}&redirect_uri={redirect_uri}"

    monkeypatch.setattr(MercadoPagoOAuthService, "build_authorization_url", fake_build_authorization_url)

    with stock_app.app.app_context():
        client = stock_app.app.test_client()
        client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

        start_response = client.post("/admin/mercado-pago")
        assert start_response.status_code in (301, 302)
        assert captured["redirect_uri"] == "https://www.stockarmobile.com/admin/mercado-pago/callback"


def test_pos_qr_create_uses_company_connected_token(monkeypatch):
    from services.mercadopago_oauth_service import MercadoPagoOAuthService
    from services.mercadopago_service import MercadoPagoService

    with stock_app.app.app_context():
        from app import User

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        client = stock_app.app.test_client()
        client.post("/auth/login", data={"username": user.username, "password": "admin123"})
        open_cash_session(client)

        monkeypatch.setattr(MercadoPagoOAuthService, "ensure_access_token", lambda self, *, company_id: "company-access-token")

        captured = {}

        def fake_create_pos_checkout_preference(self, **kwargs):
            captured.update(kwargs)
            return {
                "id": "pref-pos-connection",
                "init_point": "https://mercadopago.test/checkout/pref-pos-connection",
                "sandbox_init_point": "https://sandbox.mercadopago.test/checkout/pref-pos-connection",
            }

        monkeypatch.setattr(MercadoPagoService, "create_pos_checkout_preference", fake_create_pos_checkout_preference)

        payload = {
            "items": [{"productId": 1, "quantity": 1, "name": "Yerba kilo", "price": 18000, "barcode": "123456789012"}],
            "client_id": "",
            "document_type": "venta",
            "note": "",
        }
        headers = {"X-Cart-Tenant": f"{company.id}:{user.id}"}

        response = client.post("/ventas/api/mp-qr/create", json=payload, headers=headers)
        assert response.status_code == 200
        assert captured["access_token"] == "company-access-token"
        assert captured["company_id"] == company.id
        assert captured["user_id"] == user.id


def test_backup_service_supports_import_and_selective_restore():
    from services.backup_service import BackupService

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        product = Product.query.filter_by(company_id=company.id).first()
        assert product is not None

        company.name = "Empresa Backup"
        product.stock = 9.5
        product.min_stock = 2.0
        product.category = "Bebidas"
        db.session.commit()

        backup, plan = BackupService.create_manual_backup(company.id, user_id=1)
        assert plan["limit"] >= 1
        assert backup.company_id == company.id

        company.name = "Empresa Modificada"
        product.stock = 1.0
        product.min_stock = 0.2
        product.category = "Otro"
        db.session.commit()

        restored = BackupService.restore_backup(backup, expected_company_id=company.id, restored_by_user_id=1, sections=["inventory", "categories"])
        db.session.commit()

        refreshed_company = Company.query.get(company.id)
        refreshed_product = Product.query.get(product.id)
        assert restored.status == "restored"
        assert refreshed_company.name == "Empresa Modificada"
        assert round(float(refreshed_product.stock or 0), 2) == 9.5
        assert round(float(refreshed_product.min_stock or 0), 2) == 2.0
        assert refreshed_product.category == "Bebidas"


def test_backup_import_route_rejects_cross_company_file(monkeypatch):
    from services.backup_service import BackupService

    with stock_app.app.app_context():
        source_company = Company.query.filter_by(name="Empresa Demo").first()
        assert source_company is not None

        other_company = Company(name="Otra Empresa", active=True)
        db.session.add(other_company)
        db.session.flush()

        backup, _plan = BackupService.create_manual_backup(source_company.id, user_id=1)
        with open(backup.path, "rb") as file_handle:
            raw_bytes = file_handle.read()

        with pytest.raises(ValueError, match="no corresponde a la empresa seleccionada"):
            BackupService.import_backup_file(company_id=other_company.id, file_storage=type("Upload", (), {"filename": "backup.json.gz", "read": lambda self=None: raw_bytes})(), created_by_user_id=1)


def test_company_backup_import_route_creates_preview():
    from services.backup_service import BackupService

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        backup, _plan = BackupService.create_manual_backup(company.id, user_id=1)
        with open(backup.path, "rb") as file_handle:
            raw_bytes = file_handle.read()

        client = stock_app.app.test_client()
        client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
        with client.session_transaction() as sess:
            sess["company_pin_verified_1"] = stock_app.utcnow().timestamp()
        response = client.post(
            "/admin/company-settings/backups/import",
            data={"csrf_token": "", "backup_file": (io.BytesIO(raw_bytes), "backup.json.gz")},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        assert response.status_code in (301, 302)
        location = response.headers.get("Location") or ""
        assert "panel=backups" in location
        assert "preview_id=" in location


def test_webhook_invalid_signature_is_rejected(monkeypatch):
    from services.webhook_service import WebhookService

    with stock_app.app.app_context():
        service = WebhookService()
        monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: False)

        with pytest.raises(RuntimeError, match="Firma de webhook invalida"):
            service.process(
                db_session=db.session,
                headers={"x-request-id": "rq-invalid", "x-signature": "ts=1,v1=invalid"},
                payload={"id": "evt-invalid", "type": "payment", "data": {"id": "mp-invalid"}},
            )


def test_webhook_approved_amount_mismatch_does_not_activate_subscription(monkeypatch):
    from services.subscription_service import SubscriptionService
    from services.webhook_service import WebhookService

    with stock_app.app.app_context():
        from app import Payment, Plan, User

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        plan = Plan(code="negocio_mismatch", name="Negocio Mismatch", price=15000, currency="ARS", duration_days=30, active=True)
        db.session.add(plan)
        db.session.flush()

        subscription = SubscriptionService.start_or_change_plan(db.session, company=company, plan=plan, user_id=user.id)
        db.session.flush()
        subscription.external_reference = (
            f"company_id:{company.id}|plan_id:{plan.id}|subscription_id:{subscription.id}|"
            f"user_id:{user.id}|ts:125"
        )
        db.session.commit()

        service = WebhookService()
        monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)

        approved_wrong_amount = {
            "id": "mp-pay-mismatch",
            "status": "approved",
            "date_last_updated": "2026-07-14T10:00:00Z",
            "date_approved": "2026-07-14T10:00:00Z",
            "transaction_amount": 12000,
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
        monkeypatch.setattr(service.mp_service, "get_payment", lambda payment_id: approved_wrong_amount)

        result = service.process(
            db_session=db.session,
            headers={"x-request-id": "rq-3", "x-signature": "ts=1,v1=abc"},
            payload={"id": "evt-3", "type": "payment", "data": {"id": "mp-pay-mismatch"}},
        )
        assert result["status"] == "processed"

        db.session.refresh(subscription)
        assert subscription.status == "expired"

        payment_row = Payment.query.filter_by(payment_id="mp-pay-mismatch").first()
        assert payment_row is not None
        assert payment_row.status == "rejected"


def test_public_rate_limit_blocks_excessive_login_attempts():
    client = stock_app.app.test_client()
    stock_app.app.config["ENABLE_RATE_LIMITS_IN_TESTS"] = True
    stock_app._PUBLIC_RATE_LIMIT_BUCKETS.clear()

    try:
        last_response = None
        for _ in range(11):
            last_response = client.post(
                "/auth/login",
                data={"username": "empresa_admin", "password": "incorrecta"},
                follow_redirects=False,
            )
        assert last_response is not None
        assert last_response.status_code == 429
        assert last_response.headers.get("Retry-After")
    finally:
        stock_app.app.config["ENABLE_RATE_LIMITS_IN_TESTS"] = False
        stock_app._PUBLIC_RATE_LIMIT_BUCKETS.clear()


def test_public_rate_limit_blocks_excessive_landing_contact_posts():
    client = stock_app.app.test_client()
    stock_app.app.config["ENABLE_RATE_LIMITS_IN_TESTS"] = True
    stock_app._PUBLIC_RATE_LIMIT_BUCKETS.clear()

    payload = {
        "name": "Contacto Demo",
        "email": "contacto@test.local",
        "message": "Hola, necesito informacion comercial detallada.",
    }
    try:
        last_response = None
        for _ in range(6):
            last_response = client.post("/landing/contact", data=payload, follow_redirects=False)
        assert last_response is not None
        assert last_response.status_code == 429
    finally:
        stock_app.app.config["ENABLE_RATE_LIMITS_IN_TESTS"] = False
        stock_app._PUBLIC_RATE_LIMIT_BUCKETS.clear()


def test_company_user_cannot_create_backup_even_with_pin_session():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    with client.session_transaction() as sess:
        sess["company_pin_verified_1"] = stock_app.utcnow().timestamp()

    response = client.post("/admin/company-settings/backups/create", data={"csrf_token": ""}, follow_redirects=False)
    assert response.status_code == 403


def test_company_backup_delete_requires_explicit_confirmation():
    from services.backup_service import BackupService

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None
        backup, _plan = BackupService.create_manual_backup(company.id, user_id=1)
        backup_id = backup.id
        db.session.commit()

    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    with client.session_transaction() as sess:
        sess["company_pin_verified_1"] = stock_app.utcnow().timestamp()

    response = client.post(
        f"/admin/company-settings/backups/{backup_id}/delete",
        data={"csrf_token": ""},
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import BackupLog

        still_exists = BackupLog.query.filter_by(id=backup_id).first()
        assert still_exists is not None


def test_superadmin_company_hard_delete_requires_company_name_confirmation():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    response = client.post(
        "/superadmin/companies/1/delete",
        data={"csrf_token": "", "next": "/superadmin/companies"},
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        company = Company.query.filter_by(id=1).first()
        assert company is not None


def test_backup_import_rejects_oversized_file():
    from services.backup_service import BackupService

    with stock_app.app.app_context():
        stock_app.app.config["BACKUP_MAX_COMPRESSED_BYTES"] = 32
        try:
            payload = {
                "schema_version": 2,
                "company_id": 1,
                "company": {"name": "Empresa Demo"},
                "products": [],
                "clients": [],
                "sales": [],
                "users": [],
            }
            compressed = io.BytesIO()
            with gzip.GzipFile(fileobj=compressed, mode="wb") as gz_file:
                gz_file.write(json.dumps(payload).encode("utf-8"))

            oversized = compressed.getvalue() + (b"A" * 64)
            with pytest.raises(ValueError, match="excede el tamaño permitido"):
                BackupService.import_backup_file(
                    company_id=1,
                    file_storage=type("Upload", (), {"filename": "backup.json.gz", "read": lambda self=None: oversized})(),
                    created_by_user_id=1,
                )
        finally:
            stock_app.app.config.pop("BACKUP_MAX_COMPRESSED_BYTES", None)


def test_purchases_fail_closed_when_admin_has_no_company_context():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        admin = User.query.filter_by(username="negocio_admin").first()
        assert admin is not None
        admin.company_id = None
        db.session.commit()

    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})

    purchases_response = client.get("/compras/")
    suppliers_response = client.get("/compras/proveedores")
    assert purchases_response.status_code == 403
    assert suppliers_response.status_code == 403


def test_dashboard_stats_ignores_cross_tenant_saleitem_links():
    client = stock_app.app.test_client()

    with stock_app.app.app_context():
        company_a = Company.query.filter_by(name="Empresa Demo").first()
        assert company_a is not None

        company_b = Company(name="Empresa B", active=True)
        db.session.add(company_b)
        db.session.flush()

        seller_b = User(
            username="empresa_b_admin",
            email="empresa_b_admin@test.local",
            role="admin",
            company_id=company_b.id,
            active=True,
        )
        seller_b.set_password("admin123")
        db.session.add(seller_b)
        db.session.flush()

        leak_product = Product(
            barcode="LEAK-001",
            name="Producto Leak",
            category="CAT_LEAK_TEST",
            price=100,
            cost_price=50,
            stock=20,
            min_stock=1,
            active=True,
            sale_type="unidad",
            unit_measure="u",
            company_id=company_a.id,
        )
        db.session.add(leak_product)
        db.session.flush()

        cross_sale = Sale(
            date=stock_app.utcnow(),
            customer="Cliente B",
            subtotal=700,
            total_amount=700,
            paid_amount=700,
            payment_method="efectivo",
            status="confirmada",
            seller_id=seller_b.id,
            company_id=company_b.id,
        )
        db.session.add(cross_sale)
        db.session.flush()

        db.session.add(
            SaleItem(
                sale_id=cross_sale.id,
                product_id=leak_product.id,
                quantity=7,
                price=100,
                cost_price=50,
            )
        )
        db.session.commit()

    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})
    response = client.get("/dashboard/stats")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "CAT_LEAK_TEST" not in html


def test_webhook_pos_existing_payment_rejects_company_mismatch(monkeypatch):
    from services.webhook_service import WebhookService

    with stock_app.app.app_context():
        company = Company.query.filter_by(name="Empresa Demo").first()
        user = User.query.filter_by(username="empresa_admin").first()
        assert company is not None
        assert user is not None

        payment = stock_app.Payment(
            payment_id="mp-pos-existing-1",
            company_id=company.id,
            user_id=user.id,
            amount=100,
            currency="ARS",
            status="pending",
            payment_method="QR Mercado Pago",
            provider="mercadopago_pos",
            reference="pos_draft",
            payload_json=json.dumps({"flow": "pos_sale"}),
        )
        db.session.add(payment)
        db.session.commit()

        service = WebhookService()
        monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
        monkeypatch.setattr(
            service.mp_service,
            "get_payment",
            lambda _payment_id: {
                "id": "mp-pos-existing-1",
                "status": "approved",
                "date_last_updated": "2026-08-06T10:00:00Z",
                "date_approved": "2026-08-06T10:00:00Z",
                "transaction_amount": 100,
                "currency_id": "ARS",
                "payment_method_id": "account_money",
                "external_reference": f"flow:pos_sale|company_id:{company.id + 1}|draft_payment_id:{payment.id}",
                "metadata": {
                    "flow": "pos_sale",
                    "company_id": company.id + 1,
                    "draft_payment_id": payment.id,
                },
            },
        )

        with pytest.raises(RuntimeError, match="company_id inconsistente"):
            service.process(
                db_session=db.session,
                headers={"x-request-id": "rq-pos-mismatch", "x-signature": "ts=1,v1=ok"},
                payload={"id": "evt-pos-mismatch", "type": "payment", "data": {"id": "mp-pos-existing-1"}},
            )


def test_webhook_cancelled_marks_subscription_cancelled(monkeypatch):
    from services.subscription_service import SubscriptionService
    from services.webhook_service import WebhookService

    with stock_app.app.app_context():
        from app import Plan, User

        company = Company.query.filter_by(name="Empresa Demo").first()
        assert company is not None

        user = User.query.filter_by(username="empresa_admin").first()
        assert user is not None

        plan = Plan(code="negocio_cancelled", name="Negocio Cancelled", price=13999, currency="ARS", duration_days=30, active=True)
        db.session.add(plan)
        db.session.flush()

        subscription = SubscriptionService.start_or_change_plan(db.session, company=company, plan=plan, user_id=user.id)
        db.session.flush()
        subscription.external_reference = (
            f"company_id:{company.id}|plan_id:{plan.id}|subscription_id:{subscription.id}|"
            f"user_id:{user.id}|ts:126"
        )
        db.session.commit()

        service = WebhookService()
        monkeypatch.setattr(service.mp_service, "validate_webhook_signature", lambda **kwargs: True)
        monkeypatch.setattr(
            service.mp_service,
            "get_payment",
            lambda payment_id: {
                "id": "mp-pay-cancelled",
                "status": "cancelled",
                "date_last_updated": "2026-07-14T10:00:00Z",
                "transaction_amount": 13999,
                "currency_id": "ARS",
                "external_reference": subscription.external_reference,
                "metadata": {
                    "company_id": company.id,
                    "subscription_id": subscription.id,
                    "plan_id": plan.id,
                    "user_id": user.id,
                },
            },
        )

        result = service.process(
            db_session=db.session,
            headers={"x-request-id": "rq-4", "x-signature": "ts=1,v1=abc"},
            payload={"id": "evt-4", "type": "payment", "data": {"id": "mp-pay-cancelled"}},
        )
        assert result["status"] == "processed"

        db.session.refresh(subscription)
        assert subscription.status == "cancelled"


def test_sales_edit_delete_lifecycle_preserves_audit_stock_and_views():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "negocio_admin", "password": "admin123"})
    open_cash_session(client)

    sale_response = client.post(
        "/ventas/api/checkout",
        json={
            "items": [{"productId": 1, "quantity": 1}],
            "metodo_pago": "EFECTIVO",
            "checkout_token": "lifecycle-refactor-1",
            "monto_pago": 18000,
        },
        headers={"X-Cart-Tenant": "1:2"},
    )
    assert sale_response.status_code == 200
    sale_id = int(sale_response.get_json()["sale_id"])

    edit_response = client.post(
        f"/ventas/{sale_id}/edit",
        data={
            "product_id": "1",
            "quantity": "0.5",
            "price": "18000",
            "discount": "0",
            "payment_method": "TRANSFERENCIA",
            "note": "Ajuste integración",
            "order_discount": "0",
            "change_reason": "Ajuste integración",
        },
        follow_redirects=False,
    )
    assert edit_response.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import AuditLog

        sale = db.session.get(Sale, sale_id)
        assert sale is not None
        assert sale.payment_method == "TRANSFERENCIA"
        assert round(float(sale.total_amount or 0), 2) == 9000.00

        product = db.session.get(Product, 1)
        assert product is not None
        assert round(float(product.stock or 0), 3) == 2.0

        update_log = AuditLog.query.filter_by(action="sale_update", entity_id=sale_id).first()
        assert update_log is not None

    delete_response = client.post(f"/ventas/{sale_id}/delete", follow_redirects=False)
    assert delete_response.status_code in (301, 302)

    with stock_app.app.app_context():
        from app import AuditLog

        deleted_sale = db.session.get(Sale, sale_id)
        assert deleted_sale is None

        product = db.session.get(Product, 1)
        assert product is not None
        assert round(float(product.stock or 0), 3) == 2.5

        delete_log = AuditLog.query.filter_by(action="sale_delete", entity_id=sale_id).first()
        assert delete_log is not None

    dashboard = client.get("/dashboard/")
    reports = client.get("/reportes/")
    assert dashboard.status_code == 200
    assert reports.status_code == 200


def test_sale_service_cancel_sale_sets_anulada_and_audits():
    from services.sales import SaleService

    with stock_app.app.app_context():
        from app import AuditLog

        user = User.query.filter_by(username="negocio_admin").first()
        company = Company.query.filter_by(name="Empresa Demo").first()
        assert user is not None
        assert company is not None

        sale = Sale(
            customer="Cancel test",
            subtotal=100,
            discount=0,
            tax=0,
            total_amount=100,
            paid_amount=100,
            payment_method="EFECTIVO",
            company_id=company.id,
            seller_id=user.id,
            status="confirmada",
        )
        db.session.add(sale)
        db.session.commit()

        service = SaleService(
            require_open_cash_session=lambda json_response=False: None,
            calculate_lines=lambda items, lock_for_update=False, discount_overrides=None: [],
            mark_quote_as_converted=lambda checkout_token, sale_id: None,
            cart_key=lambda: "cart_test",
            to_decimal=lambda value: Decimal("0.00"),
        )
        with stock_app.app.test_request_context("/"):
            login_user(user)
            service.cancel_sale(sale=sale, detail="Anulación de prueba")
            logout_user()

        db.session.refresh(sale)
        assert (sale.status or "").lower() == "anulada"

        log = AuditLog.query.filter_by(action="sale_cancel", entity_id=sale.id).first()
        assert log is not None


def test_superadmin_home_renders_ops_sections_and_health_checks():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "superadmin", "password": "admin123"})

    response = client.get("/superadmin/", follow_redirects=True)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Centro de Operaciones SaaS" in html
    assert "Salud del sistema" in html
    assert "Empresas que necesitan atención" in html
    assert "Embudo de ventas SaaS" in html
    assert "Métricas SaaS" in html
    assert "Actividad en tiempo real" in html
    assert "Acciones rápidas" in html
    assert "Preparación para Copilot" in html
    assert "Prioridad alta" in html
    assert "Acción" in html


def test_register_creates_automatic_saas_ops_records():
    client = stock_app.app.test_client()

    register = client.post(
        "/auth/register",
        data={
            "username": "empresa_auto_ops",
            "email": "empresa_auto_ops@test.com",
            "password": "admin123",
        },
        follow_redirects=False,
    )
    assert register.status_code in (301, 302)

    with stock_app.app.app_context():
        lead = SaaSLead.query.filter_by(email="empresa_auto_ops@test.com").first()
        assert lead is not None
        assert lead.source == "registro"

        task = SaaSTask.query.filter(SaaSTask.title.ilike("Onboarding inicial -%"))\
            .order_by(SaaSTask.id.desc()).first()
        assert task is not None

        alert = SaaSAlert.query.filter(SaaSAlert.title.ilike("Nueva empresa registrada:%"))\
            .order_by(SaaSAlert.id.desc()).first()
        assert alert is not None


def test_landing_contact_creates_automatic_lead_task_alert():
    client = stock_app.app.test_client()

    response = client.post(
        "/landing/contact",
        data={
            "name": "Lead Landing Auto",
            "email": "lead_landing_auto@test.local",
            "message": "Necesito una demo guiada de la plataforma para evaluar compra.",
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        lead = SaaSLead.query.filter_by(email="lead_landing_auto@test.local").first()
        assert lead is not None
        assert lead.source == "landing_form"

        task = SaaSTask.query.filter(SaaSTask.title.ilike("Responder lead landing:%"))\
            .order_by(SaaSTask.id.desc()).first()
        assert task is not None

        alert = SaaSAlert.query.filter_by(title="Nuevo lead desde landing").order_by(SaaSAlert.id.desc()).first()
        assert alert is not None


def test_support_ticket_creates_automatic_saas_ops_records():
    client = stock_app.app.test_client()
    client.post("/auth/login", data={"username": "empresa_admin", "password": "admin123"})

    response = client.post(
        "/soporte/nuevo",
        data={
            "reason": "Problemas con suscripcion",
            "description": "No puedo finalizar el pago de renovación.",
            "email": "empresa_admin@test.local",
        },
        follow_redirects=False,
    )
    assert response.status_code in (301, 302)

    with stock_app.app.app_context():
        task = SaaSTask.query.filter(SaaSTask.title.ilike("Atender ticket soporte #%"))\
            .order_by(SaaSTask.id.desc()).first()
        assert task is not None

        alert = SaaSAlert.query.filter(SaaSAlert.title.ilike("Soporte abierto:%"))\
            .order_by(SaaSAlert.id.desc()).first()
        assert alert is not None
