from decimal import Decimal
from types import SimpleNamespace

from services.sales.receipt_service import ReceiptService


def make_sale(*, sale_discount=Decimal("0.00")):
    product = SimpleNamespace(name="coca", unit_measure="u")
    item = SimpleNamespace(
        product=product,
        product_id=1,
        quantity=6,
        price=Decimal("5800.00"),
        discount=Decimal("6960.00"),
    )
    return SimpleNamespace(
        id=770,
        date=None,
        company=None,
        customer="Consumidor final",
        items=[item],
        subtotal=Decimal("34800.00"),
        discount=sale_discount,
        discount_type=None,
        discount_value=None,
        discount_reason="Promoción: coca",
        surcharge=Decimal("0.00"),
        surcharge_type=None,
        surcharge_value=None,
        surcharge_reason=None,
        tax=Decimal("0.00"),
        total_amount=Decimal("27840.00"),
        note=None,
    )


def test_ticket_reports_promotion_discount_from_line_even_when_sale_discount_is_zero():
    sale = make_sale()

    ticket = ReceiptService.ticket_text(sale, "STOCK ARMOBILE")

    assert "coca: $5800.00 x 6 u = $34800.00" in ticket
    assert "Descuento promoción: -$6960.00" in ticket
    assert "Total línea: $27840.00" in ticket
    assert "Descuento: -$6960.00" in ticket
    assert "Motivo descuento: Promoción: coca" in ticket
    assert "TOTAL: $27840.00" in ticket


def test_ticket_keeps_stored_general_discount_when_it_exceeds_line_discount():
    sale = make_sale(sale_discount=Decimal("8000.00"))

    ticket = ReceiptService.ticket_text(sale, "STOCK ARMOBILE")

    assert "Descuento: -$8000.00" in ticket


def test_ticket_rows_expose_promotion_discount_and_gross_amount():
    sale = make_sale()

    row = ReceiptService.ticket_rows(sale)[0]

    assert row["gross"] == Decimal("34800.00")
    assert row["discount"] == Decimal("6960.00")
    assert row["total"] == Decimal("27840.00")



def test_ai_order_ticket_note_uses_business_name_without_changing_internal_marker():
    sale = make_sale()
    sale.note = "Pedido generado por el Vendedor 24 hs de StockARmobile."
    sale.company = SimpleNamespace(name="Maderas Panambiseñas")

    visible_note = ReceiptService.ticket_note(sale, ticket_brand="Maderas Panambiseñas")
    ticket = ReceiptService.ticket_text(sale, "Maderas Panambiseñas")

    assert visible_note == "Pedido generado por el Vendedor IA de Maderas Panambiseñas."
    assert "Obs.: Pedido generado por el Vendedor IA de Maderas Panambiseñas." in ticket
    assert "Pedido generado por el Vendedor 24 hs de StockARmobile." not in ticket
    assert sale.note == "Pedido generado por el Vendedor 24 hs de StockARmobile."


def test_ticket_note_preserves_customer_notes_unchanged():
    sale = make_sale()
    sale.note = "Entregar por la puerta lateral."
    sale.company = SimpleNamespace(name="Maderas Panambiseñas")

    assert ReceiptService.ticket_note(sale) == "Entregar por la puerta lateral."


def test_ticket_renders_persisted_quote_charge_breakdown():
    sale = make_sale()
    sale.charges_json = """[{"id":"shipping","name":"Envío a domicilio","type":"fixed","value":"50.00","amount":"50.00"},{"id":"iva","name":"IVA","type":"percentage","value":"21","amount":"42.00"}]"""
    sale.surcharge = Decimal("50.00")
    sale.tax = Decimal("42.00")
    sale.total_amount = Decimal("320.00")

    ticket = ReceiptService.ticket_text(sale, "STOCK ARMOBILE")

    assert "Envío a domicilio: +$50.00" in ticket
    assert "IVA: 21.00%" in ticket
    assert "IVA aplicado: $42.00" in ticket
    assert "Recargo: " not in ticket
    assert "Impuestos: " not in ticket
    assert "TOTAL: $320.00" in ticket
