"""Focused regression tests for purchase exports and multi-line purchase support."""

import io
from openpyxl import load_workbook

from app import PurchaseItem, PurchaseOrder, Product, Supplier, db


def _login_as_admin(client, stock_app):
    user = stock_app.User(email="purchase-test@example.com", role="admin", company_id=1, name="Purchase Test")
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)


def test_supplier_purchase_excel_contains_rows_and_multiple_items(stock_app):
    app = stock_app.app
    client = app.test_client()
    with app.app_context():
        from app import Company

        company = Company.query.first()
        if company is None:
            company = Company(name="Test Company", active=True)
            db.session.add(company)
            db.session.flush()
        supplier = Supplier(company_id=company.id, name="Proveedor Test", active=True)
        p1 = Product(company_id=company.id, name="Producto Uno", stock=5, cost_price=100, price=150, active=True)
        p2 = Product(company_id=company.id, name="Producto Dos", stock=8, cost_price=200, price=300, active=True)
        db.session.add_all([supplier, p1, p2])
        db.session.flush()
        purchase = PurchaseOrder(
            supplier_id=supplier.id,
            company_id=company.id,
            status="recibida",
            subtotal=1400,
            total_amount=1400,
        )
        db.session.add(purchase)
        db.session.flush()
        db.session.add_all([
            PurchaseItem(purchase_order_id=purchase.id, product_id=p1.id, quantity=4, unit_cost=100),
            PurchaseItem(purchase_order_id=purchase.id, product_id=p2.id, quantity=5, unit_cost=200),
        ])
        db.session.commit()

        user = type("UserObj", (), {"id": 1, "role": "admin", "company_id": company.id, "is_authenticated": True})()
        # Use the app's normal login machinery in environments that provide a helper.
        stock_app.login_client(client, user)
        response = client.get(f"/proveedores/{supplier.id}/compras/export.xlsx?company_id={company.id}")
        assert response.status_code == 200
        assert response.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        workbook = load_workbook(io.BytesIO(response.data), read_only=True, data_only=True)
        rows = list(workbook["Compras"].iter_rows(values_only=True))
        workbook.close()

        assert rows[0][0:4] == ("Compra", "Fecha", "Proveedor", "Producto")
        assert any(row[3] == "Producto Uno" for row in rows[1:])
        assert any(row[3] == "Producto Dos" for row in rows[1:])


def test_purchase_model_supports_multiple_items(stock_app):
    app = stock_app.app
    with app.app_context():
        from app import Company

        company = Company.query.first()
        if company is None:
            company = Company(name="Test Company", active=True)
            db.session.add(company)
            db.session.flush()
        supplier = Supplier(company_id=company.id, name="Proveedor Test", active=True)
        p1 = Product(company_id=company.id, name="Producto Uno", stock=0, cost_price=0, price=150, active=True)
        p2 = Product(company_id=company.id, name="Producto Dos", stock=0, cost_price=0, price=300, active=True)
        db.session.add_all([supplier, p1, p2])
        db.session.flush()
        purchase = PurchaseOrder(supplier_id=supplier.id, company_id=company.id, status="recibida", subtotal=100, total_amount=100)
        db.session.add(purchase)
        db.session.flush()
        db.session.add_all([
            PurchaseItem(purchase_order_id=purchase.id, product_id=p1.id, quantity=2, unit_cost=25),
            PurchaseItem(purchase_order_id=purchase.id, product_id=p2.id, quantity=1, unit_cost=50),
        ])
        db.session.commit()
        refreshed = db.session.get(PurchaseOrder, purchase.id)
        assert len(refreshed.items) == 2
