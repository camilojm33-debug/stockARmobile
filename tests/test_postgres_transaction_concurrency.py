"""True multi-session transaction regressions; executed against PostgreSQL in CI."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

import app as stock_app
from app import Company, Product, PurchaseOrder, Supplier, User, db
from purchases import _apply_product_purchase_totals
from services.invoice_purchase_service import InvoicePurchaseError, InvoicePurchaseService


def _require_postgresql():
    if db.engine.dialect.name != "postgresql":
        pytest.skip("Este test requiere PostgreSQL real para verificar bloqueos entre transacciones.")


def test_concurrent_manual_purchases_serialize_product_stock_and_average_cost(app):
    _require_postgresql()
    with app.app_context():
        company = Company(name="PG concurrent manual purchase", active=True)
        product = Product(
            company_id=None, barcode="PG-PURCHASE-LOCK", name="Producto concurrencia",
            stock=0, cost_price=0, price=30, active=True,
        )
        db.session.add(company)
        db.session.flush()
        product.company_id = company.id
        db.session.add(product)
        db.session.commit()
        company_id, product_id = company.id, product.id

        start = Barrier(2)

        def apply_purchase(unit_cost):
            with app.app_context():
                db.session.remove()
                start.wait(timeout=15)
                _apply_product_purchase_totals(
                    {
                        product_id: [
                            SimpleNamespace(company_id=company_id),
                            1.0,
                            float(unit_cost),
                        ]
                    },
                    company_id=company_id,
                    branch_id=None,
                )
                db.session.commit()
                db.session.remove()
                return unit_cost

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(apply_purchase, 10), executor.submit(apply_purchase, 20)]
            assert sorted(future.result(timeout=45) for future in futures) == [10, 20]

        db.session.expire_all()
        persisted = Product.query.filter_by(id=product_id, company_id=company_id).one()
        assert float(persisted.stock) == 2.0
        assert float(persisted.cost_price) == 15.0


def test_concurrent_ai_invoice_confirmation_creates_only_one_purchase(app):
    _require_postgresql()
    with app.app_context():
        company = Company(name="PG concurrent AI invoice", active=True)
        db.session.add(company)
        db.session.flush()
        supplier = Supplier(name="Proveedor PG IA", company_id=company.id, active=True)
        user = User(
            username="pg-ai-invoice-admin",
            email="pg-ai-invoice-admin@example.test",
            password_hash="test-hash",
            role="admin",
            active=True,
            company_id=company.id,
        )
        product = Product(
            company_id=company.id, barcode="PG-AI-INVOICE-001",
            name="Producto factura concurrente", stock=0, cost_price=0,
            price=30, active=True,
        )
        db.session.add_all([supplier, user, product])
        db.session.commit()
        company_id, supplier_id, user_id, product_id = company.id, supplier.id, user.id, product.id

        document_hash = "c" * 64
        start = Barrier(2)

        def confirm_invoice():
            with app.app_context():
                db.session.remove()
                start.wait(timeout=15)
                invoice = {
                    "status": "LISTA_PARA_CONFIRMAR",
                    "document_hash": document_hash,
                    "currency": "ARS",
                    "subtotal": "12",
                    "tax": "0",
                    "total": "12",
                    "supplier_match": {
                        "status": "MATCH_EXACTO",
                        "supplier_id": supplier_id,
                    },
                    "matches": [{
                        "line_number": 1,
                        "matching_status": "MATCH_EXACTO",
                        "product_id": product_id,
                        "quantity": "1",
                        "unit_cost": "12",
                        "line_total": "12",
                    }],
                }
                try:
                    result = InvoicePurchaseService.confirm(
                        company_id=company_id,
                        user_id=user_id,
                        invoice=invoice,
                        conversation=None,
                        branch_id=None,
                    )
                    return ("applied", result)
                except InvoicePurchaseError as exc:
                    return ("duplicate", str(exc))
                finally:
                    db.session.remove()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(confirm_invoice), executor.submit(confirm_invoice)]
            results = [future.result(timeout=60) for future in futures]

        assert sum(result[0] == "applied" for result in results) == 1
        assert sum(result[0] == "duplicate" for result in results) == 1

        db.session.expire_all()
        persisted = Product.query.filter_by(id=product_id, company_id=company_id).one()
        purchases = PurchaseOrder.query.filter_by(
            company_id=company_id,
            source_document_hash=document_hash,
        ).all()
        assert float(persisted.stock) == 1.0
        assert len(purchases) == 1
