from decimal import Decimal
from types import SimpleNamespace

import app as stock_app
import services.sales.sale_service as sale_service_module

from services.sales import AuditService, InventoryService, PricingService, SaleService, TotalsService, ValidationService


class _FakeProduct:
    def __init__(self, product_id, company_id=1, stock=10, price="100.00", discount="0.00", name="Prod", unit_measure="u"):
        self.id = product_id
        self.company_id = company_id
        self.stock = stock
        self.price = Decimal(str(price))
        self.discount = Decimal(str(discount))
        self.name = name
        self.unit_measure = unit_measure


def test_validation_service_checkout_token_and_comprobante_payload():
    assert ValidationService.clean_comprobante_type("FACTURA_A") == "factura_a"
    assert ValidationService.clean_comprobante_type("invalid") is None

    payload = {
        "document_type": "factura_b",
        "observacion_comprobante": "  observacion larga  ",
    }
    requiere, tipo, obs = ValidationService.resolve_comprobante_payload(payload)
    assert requiere is True
    assert tipo == "factura_b"
    assert obs == "observacion larga"

    assert ValidationService.requires_identified_client(payload, requiere, tipo) is True
    assert ValidationService.sanitize_checkout_token("abc_123") == "abc_123"


def test_inventory_service_calculate_lines_ok_and_failures():
    current_user = SimpleNamespace(company_id=1)
    products = {
        1: _FakeProduct(1, company_id=1, stock=5, price="50.00", discount="2.00", name="A"),
        2: _FakeProduct(2, company_id=1, stock=20, price="10.00", discount="0.50", name="B"),
    }

    def fetch_products(ids):
        return {i: products[i] for i in ids if i in products}

    lines = InventoryService.calculate_lines(
        items={"1": 2, "2": 3},
        current_user=current_user,
        fetch_products_func=fetch_products,
        discount_overrides={"2": "1.00"},
    )
    assert len(lines) == 2
    assert lines[0]["product"].id == 1
    assert lines[1]["discount"] == Decimal("3.00")

    try:
        InventoryService.calculate_lines(
            items={"1": 100},
            current_user=current_user,
            fetch_products_func=fetch_products,
        )
        assert False, "Expected stock insufficiency"
    except ValueError as exc:
        assert "Stock insuficiente" in str(exc)


def test_totals_and_pricing_services_keep_shapes():
    lines = [
        {"price": Decimal("100.00"), "quantity": 2, "line_discount": Decimal("5.00")},
        {"price": Decimal("50.00"), "quantity": 1, "line_discount": Decimal("0.00")},
    ]
    totals = TotalsService.calculate(lines, general_discount=Decimal("10.00"), surcharge=Decimal("0.00"))
    assert totals["subtotal"] == Decimal("250.00")
    assert totals["line_discount_total"] == Decimal("5.00")
    assert totals["general_discount"] == Decimal("10.00")
    assert totals["total"] == Decimal("235.00")

    totals2 = PricingService.calculate(lines=lines, data={"descuento_general": "10", "recargo": "0"})
    assert totals2["total"] == Decimal("235.00")


def test_totals_support_structured_percentage_and_fixed_adjustments():
    lines = [{"price": Decimal("100000.00"), "quantity": 1, "line_discount": Decimal("0.00")}]

    percentage = TotalsService.calculate(
        lines,
        discount_type="percentage",
        discount_value=Decimal("30.00"),
        discount_reason="Cliente mayorista",
        surcharge_type="percentage",
        surcharge_value=Decimal("15.00"),
        surcharge_reason="Pago con tarjeta",
    )
    assert percentage["general_discount"] == Decimal("30000.00")
    assert percentage["surcharge"] == Decimal("10500.00")
    assert percentage["total"] == Decimal("80500.00")
    assert percentage["discount_adjustment"]["reason"] == "Cliente mayorista"
    assert percentage["surcharge_adjustment"]["reason"] == "Pago con tarjeta"

    fixed = TotalsService.calculate(
        lines,
        surcharge_type="fixed",
        surcharge_value=Decimal("5000.00"),
        surcharge_reason="Envío",
    )
    assert fixed["surcharge"] == Decimal("5000.00")
    assert fixed["total"] == Decimal("105000.00")


def test_totals_calculate_percentage_and_fixed_surcharge_from_structured_values():
    lines = [{"price": Decimal("29498.00"), "quantity": 1, "line_discount": Decimal("0.00")}]

    percentage = PricingService.calculate(
        lines,
        {
            "surcharge_type": "percentage",
            "surcharge_value": "30",
            "surcharge_reason": "Pago con tarjeta",
            "recargo": "8849.40",
        },
    )
    assert percentage["surcharge"] == Decimal("8849.40")
    assert percentage["total"] == Decimal("38347.40")
    assert percentage["surcharge_adjustment"]["reason"] == "Pago con tarjeta"

    fixed = PricingService.calculate(
        lines,
        {"surcharge_type": "fixed", "surcharge_value": "5000", "recargo": "5000"},
    )
    assert fixed["surcharge"] == Decimal("5000.00")
    assert fixed["total"] == Decimal("34498.00")

    discounted = PricingService.calculate(
        lines,
        {
            "discount_type": "percentage",
            "discount_value": "10",
            "surcharge_type": "percentage",
            "surcharge_value": "30",
        },
    )
    assert discounted["general_discount"] == Decimal("2949.80")
    assert discounted["surcharge"] == Decimal("7964.46")
    assert discounted["total"] == Decimal("34512.66")


def test_sale_service_init_signature_stable():
    service = SaleService(
        require_open_cash_session=lambda json_response=False: None,
        calculate_lines=lambda items, lock_for_update=False, discount_overrides=None: [],
        mark_quote_as_converted=lambda checkout_token, sale_id: None,
        cart_key=lambda: "cart_test",
        to_decimal=lambda value: Decimal("0.00"),
    )
    assert hasattr(service, "create_sale_from_items")


def test_inventory_service_reverse_and_apply_stock():
    product_a = _FakeProduct(1, stock=5)
    product_b = _FakeProduct(2, stock=8)
    sale_items = [
        SimpleNamespace(product_id=1, quantity=1.5, product=product_a),
        SimpleNamespace(product_id=2, quantity=2, product=product_b),
    ]
    InventoryService.reverse_stock(sale_items=sale_items, resolve_product_func=lambda item: item.product)
    assert float(product_a.stock) == 6.5
    assert float(product_b.stock) == 10.0

    lines = [
        {"product": product_a, "quantity": Decimal("0.5")},
        {"product": product_b, "quantity": Decimal("1.0")},
    ]
    InventoryService.apply_stock(lines=lines)
    assert float(product_a.stock) == 6.0
    assert float(product_b.stock) == 9.0


def test_audit_service_lifecycle_events():
    calls = []

    def record_audit(**kwargs):
        calls.append(kwargs)

    AuditService.record_update(record_audit, sale_id=10, reason="correccion")
    AuditService.record_cancel(record_audit, sale_id=10, detail="Venta anulada")
    AuditService.record_delete(record_audit, sale_id=10, total_amount=Decimal("100.00"))

    assert calls[0]["action"] == "sale_update"
    assert "correccion" in calls[0]["detail"]
    assert calls[1]["action"] == "sale_cancel"
    assert calls[2]["action"] == "sale_delete"


def test_validation_service_lifecycle_rules():
    try:
        ValidationService.validate_edit_reason("   ")
        assert False, "Expected missing reason error"
    except ValueError as exc:
        assert "motivo" in str(exc)

    try:
        ValidationService.validate_edit_lines([])
        assert False, "Expected missing lines error"
    except ValueError as exc:
        assert "al menos un producto" in str(exc)

    try:
        ValidationService.validate_delete_role("user")
        assert False, "Expected forbidden role"
    except PermissionError:
        assert True

    try:
        ValidationService.validate_cancel_status("anulada")
        assert False, "Expected already cancelled error"
    except ValueError as exc:
        assert "ya se encuentra anulada" in str(exc)


def test_sale_service_update_sale_rejects_missing_reason_early():
    service = SaleService(
        require_open_cash_session=lambda json_response=False: None,
        calculate_lines=lambda items, lock_for_update=False, discount_overrides=None: [],
        mark_quote_as_converted=lambda checkout_token, sale_id: None,
        cart_key=lambda: "cart_test",
        to_decimal=lambda value: Decimal("0.00"),
    )
    fake_sale = SimpleNamespace(items=[])
    fake_form = SimpleNamespace(getlist=lambda _key: [], get=lambda _key: "")

    try:
        service.update_sale(
            sale=fake_sale,
            form=fake_form,
            reason="",
            current_open_cash_session=lambda: None,
            sale_snapshot=lambda _sale: {},
            json_dumps=lambda payload: str(payload),
        )
        assert False, "Expected validation error"
    except ValueError as exc:
        assert "motivo" in str(exc)


def test_sale_service_cancel_sale_and_delete_sale_unit(monkeypatch):
    class _FakeSession:
        def __init__(self):
            self.deleted = []
            self.commits = 0

        def delete(self, row):
            self.deleted.append(row)

        def commit(self):
            self.commits += 1

    class _FakeFilter:
        def __init__(self):
            self.deleted_calls = []

        def delete(self, synchronize_session=False):
            self.deleted_calls.append(synchronize_session)

    class _FakeCashMovement:
        query = SimpleNamespace(filter_by=lambda **kwargs: _FakeFilter())

    session = _FakeSession()
    audit_calls = []

    monkeypatch.setattr(stock_app, "db", SimpleNamespace(session=session))
    monkeypatch.setattr(stock_app, "record_audit", lambda **kwargs: audit_calls.append(kwargs))
    monkeypatch.setattr(stock_app, "CashMovement", _FakeCashMovement)
    monkeypatch.setattr(sale_service_module, "current_user", SimpleNamespace(role="admin"))

    service = SaleService(
        require_open_cash_session=lambda json_response=False: None,
        calculate_lines=lambda items, lock_for_update=False, discount_overrides=None: [],
        mark_quote_as_converted=lambda checkout_token, sale_id: None,
        cart_key=lambda: "cart_test",
        to_decimal=lambda value: Decimal("0.00"),
    )

    sale_cancel = SimpleNamespace(id=7, status="confirmada")
    cancelled = service.cancel_sale(sale=sale_cancel, detail="Anulación de prueba")
    assert cancelled.status == "anulada"

    product = _FakeProduct(1, stock=3)
    item = SimpleNamespace(product_id=1, quantity=2, product=product)
    sale_delete = SimpleNamespace(id=8, total_amount=Decimal("22.00"), items=[item])
    deleted = service.delete_sale(sale=sale_delete, resolve_product_for_item=lambda _item: _item.product)
    assert deleted.id == 8
    assert float(product.stock) == 5.0
    assert sale_delete in session.deleted
    assert session.commits >= 2
    assert any(call.get("action") == "sale_cancel" for call in audit_calls)
    assert any(call.get("action") == "sale_delete" for call in audit_calls)
