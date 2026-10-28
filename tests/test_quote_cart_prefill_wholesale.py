from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_quote_to_pos_prefill_preserves_saved_wholesale_price_and_savings():
    from quotes import _quote_cart_prefill_item

    quote_item = SimpleNamespace(
        id=7,
        product_id=42,
        description="Aceite de Girasol 900 ml",
        quantity=3,
        unit_price=Decimal("2173.50"),
        price_source="mayorista",
        price_list_id=12,
        wholesale_discount_percent=Decimal("10.00"),
        wholesale_reference_price=Decimal("2415.00"),
    )
    product = SimpleNamespace(
        id=42,
        name="Aceite de Girasol 900 ml",
        price=Decimal("2415.00"),
        stock=10,
        barcode="779000000001",
        unit_measure="u",
    )

    line = _quote_cart_prefill_item(quote_item, product, quantity=3)

    assert line["price"] == 2173.50
    assert line["retailPrice"] == 2415.00
    assert line["priceSource"] == "mayorista"
    assert line["priceListId"] == 12
    assert line["wholesaleDiscountPercent"] == 10.0
    assert line["wholesaleReferencePrice"] == 2415.00
    assert line["wholesaleDiscountAmount"] == 724.50
    assert line["quantity"] == 3.0


def test_quote_cart_prefill_does_not_start_retail_pricing_before_client_selection():
    cart_manager = (REPO_ROOT / "static/assets/js/cart-manager.js").read_text(encoding="utf-8")
    pos_template = (REPO_ROOT / "templates/ventas/index.html").read_text(encoding="utf-8")
    service_worker = (REPO_ROOT / "static/service-worker.js").read_text(encoding="utf-8")

    load_start = cart_manager.index("function loadCart()")
    load_end = cart_manager.index("function saveCart()", load_start)
    load_cart = cart_manager[load_start:load_end]

    assert "hasPendingQuotePrefill" in load_cart
    assert "!hasPendingQuotePrefill" in load_cart
    assert "wholesaleDiscountPercent" in cart_manager
    assert "wholesaleReferencePrice" in cart_manager
    assert "wholesaleDiscountAmount" in cart_manager
    assert "wholesaleDiscountPercent" in pos_template
    assert "wholesaleReferencePrice" in pos_template
    assert "wholesaleDiscountAmount" in pos_template
    assert "/static/assets/js/cart-manager.js?v=20261028-wholesale-quote-prefill-fix" in pos_template
    assert "/static/assets/js/cart-manager.js?v=20261028-wholesale-quote-prefill-fix" in service_worker
