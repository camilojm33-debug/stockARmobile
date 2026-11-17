from types import SimpleNamespace

import app as stock_app

from purchases import _apply_product_purchase_totals


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows
        self.populate_existing_called = False
        self.filter_calls = 0
        self.order_by_called = False

    def filter(self, *_args, **_kwargs):
        self.filter_calls += 1
        return self

    def order_by(self, *_args, **_kwargs):
        self.order_by_called = True
        return self

    def with_for_update(self):
        return self

    def populate_existing(self):
        self.populate_existing_called = True
        return self

    def all(self):
        return self.rows


class _FakeSession:
    def __init__(self, locked_rows):
        self.locked_rows = locked_rows
        self.lock_called = False
        self.last_query = None

    def query(self, _model):
        self.last_query = _FakeQuery(self.locked_rows)
        return self.last_query


def test_purchase_totals_lock_product_rows_before_mutation(monkeypatch):
    stale = SimpleNamespace(id=1, company_id=7, stock=10.0, cost_price=5.0, price=10.0, margin=0.0, profit_percent=0.0)
    locked = SimpleNamespace(id=1, company_id=7, stock=20.0, cost_price=6.0, price=10.0, margin=0.0, profit_percent=0.0)
    session = _FakeSession([locked])
    monkeypatch.setattr(stock_app, "db", SimpleNamespace(session=session))

    _apply_product_purchase_totals({
        1: [stale, 2.0, 20.0],
    }, company_id=7)

    # The calculation must use the freshly locked database row, not the stale
    # object loaded before the transaction acquired the row lock.
    assert locked.stock == 22.0
    assert locked.cost_price == (20.0 * 6.0 + 20.0) / 22.0
    assert session.last_query.populate_existing_called is True
    assert session.last_query.order_by_called is True
    assert session.last_query.filter_calls == 1
    assert stale.stock == 10.0



def test_purchase_product_lock_refreshes_stock_and_scopes_to_company(app):
    from sqlalchemy import update
    from app import Company, Product, db
    from purchases import _lock_purchase_products

    with app.app_context():
        company = Company(name="Purchase lock company", active=True)
        foreign_company = Company(name="Foreign purchase lock company", active=True)
        db.session.add_all([company, foreign_company])
        db.session.flush()
        product = Product(
            company_id=company.id, barcode="PURCHASE-LOCK-001", name="Producto propio",
            stock=10, cost_price=5, price=10, active=True,
        )
        foreign = Product(
            company_id=foreign_company.id, barcode="PURCHASE-LOCK-002", name="Producto ajeno",
            stock=50, cost_price=25, price=40, active=True,
        )
        db.session.add_all([product, foreign])
        db.session.commit()

        own_id, foreign_id = product.id, foreign.id
        stale_product = db.session.get(Product, own_id)
        assert stale_product.stock == 10
        db.session.execute(
            update(Product).where(Product.id == own_id).values(stock=4, cost_price=8)
            .execution_options(synchronize_session=False)
        )
        assert stale_product.stock == 10

        locked = _lock_purchase_products(company.id, [foreign_id, own_id, own_id])
        assert list(locked) == [own_id]
        assert locked[own_id].stock == 4
        assert locked[own_id].cost_price == 8
        assert foreign_id not in locked
        db.session.rollback()


def test_ai_invoice_purchase_refreshes_locked_product_and_requires_sha256(app):
    import pytest
    from sqlalchemy import update
    from app import Company, Product, Supplier, db
    from services.invoice_purchase_service import InvoicePurchaseError, InvoicePurchaseService

    with app.app_context():
        company = Company(name="AI invoice purchase locking", active=True)
        db.session.add(company)
        db.session.flush()
        supplier = Supplier(name="Proveedor IA lock", company_id=company.id, active=True)
        product = Product(
            company_id=company.id, barcode="AI-INV-LOCK-001", name="Producto factura",
            stock=5, cost_price=10, price=20, active=True,
        )
        db.session.add_all([supplier, product])
        db.session.commit()

        product_id, supplier_id = product.id, supplier.id
        stale_product = db.session.get(Product, product_id)
        db.session.execute(
            update(Product).where(Product.id == product_id)
            .values(stock=3, cost_price=8)
            .execution_options(synchronize_session=False)
        )
        assert stale_product.stock == 5

        invoice = {
            "status": "LISTA_PARA_CONFIRMAR",
            "document_hash": "a" * 64,
            "currency": "ARS",
            "subtotal": "12",
            "tax": "0",
            "total": "12",
            "supplier_match": {"status": "MATCH_EXACTO", "supplier_id": supplier_id},
            "matches": [{
                "line_number": 1,
                "matching_status": "MATCH_EXACTO",
                "product_id": product_id,
                "quantity": "1",
                "unit_cost": "12",
                "line_total": "12",
            }],
        }
        result = InvoicePurchaseService.confirm(
            company_id=company.id,
            user_id=1,
            invoice=invoice,
            conversation=None,
            branch_id=None,
        )
        assert result["status"] == "APLICADA"
        assert stale_product.stock == 4
        assert stale_product.cost_price == 9

        no_hash_invoice = {
            **invoice,
            "status": "LISTA_PARA_CONFIRMAR",
            "document_hash": None,
        }
        with pytest.raises(InvoicePurchaseError, match="SHA-256"):
            InvoicePurchaseService.confirm(
                company_id=company.id,
                user_id=1,
                invoice=no_hash_invoice,
                conversation=None,
                branch_id=None,
            )
        assert Product.query.filter_by(id=product_id).first().stock == 4
        db.session.rollback()
