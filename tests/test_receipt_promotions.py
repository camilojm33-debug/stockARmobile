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
