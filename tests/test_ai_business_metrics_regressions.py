from datetime import datetime, timedelta

from app import Client, Company, Product, Sale, SaleItem, db
from services.ai_agent.tools.analyst_marketing import ClientesInactivosTool
from services.ai_agent.tools.business_metrics import ResumenVentasTool


def _sale(company_id, client_id, total, status, when):
    sale = Sale(
        company_id=company_id,
        client_id=client_id,
        customer="Cliente QA",
        total_amount=total,
        subtotal=total,
        paid_amount=total,
        payment_method="efectivo",
        status=status,
        date=when,
    )
    db.session.add(sale)
    db.session.flush()
    return sale


def test_resumen_ventas_excludes_cancelled_and_annulled_sales(app):
    with app.app_context():
        company = Company(name="Empresa Métricas", active=True)
        client = Client(name="Cliente Métricas", active=True, company_id=company.id)
        product = Product(
            company_id=company.id,
            barcode="MET-001",
            name="Producto Métricas",
            price=100,
            cost_price=50,
            stock=10,
            min_stock=1,
            active=True,
        )
        db.session.add_all([company, client, product])
        db.session.flush()

        now = datetime.utcnow()
        valid_sale = _sale(company.id, client.id, 100, "confirmada", now - timedelta(hours=1))
        cancelled = _sale(company.id, client.id, 900, "cancelada", now - timedelta(hours=2))
        annulled = _sale(company.id, client.id, 700, "anulada", now - timedelta(hours=3))

        for sale, qty in ((valid_sale, 1), (cancelled, 9), (annulled, 7)):
            db.session.add(
                SaleItem(
                    sale_id=sale.id,
                    product_id=product.id,
                    quantity=qty,
                    price=100,
                    cost_price=50,
                )
            )
        db.session.commit()

        result = ResumenVentasTool(company_id=company.id).execute(days=2)

        assert result["sales_count"] == 1
        assert result["sales_total"] == 100.0
        assert result["average_ticket"] == 100.0
        assert result["units_sold"] == 1.0


def test_clientes_inactivos_ignores_cancelled_and_annulled_sales(app):
    with app.app_context():
        company = Company(name="Empresa Inactivos", active=True)
        client = Client(name="Cliente Inactivo", active=True, company_id=company.id)
        product = Product(
            company_id=company.id,
            barcode="INA-001",
            name="Producto Inactivo",
            price=100,
            cost_price=50,
            stock=10,
            min_stock=1,
            active=True,
        )
        db.session.add_all([company, client, product])
        db.session.flush()

        now = datetime.utcnow()
        _sale(company.id, client.id, 500, "cancelada", now - timedelta(days=20))
        _sale(company.id, client.id, 400, "anulada", now - timedelta(days=30))
        db.session.commit()

        result = ClientesInactivosTool(company_id=company.id).execute(days=10, limit=10)

        assert result["count"] == 0
        assert result["items"] == []
