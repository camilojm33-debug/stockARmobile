from services.whatsapp_share_service import _professionalize_whatsapp_message


def test_quote_single_quantity_shows_only_customer_price():
    message = "- machimbre | Cant: 1 | Precio: ARS 5600.00 | Subtotal: ARS 5600.00"
    assert _professionalize_whatsapp_message(message) == "- machimbre: ARS 5600.00"


def test_quote_multiple_quantity_keeps_unit_and_total_without_duplicate_labels():
    message = "- machimbre | Cant: 2 | Precio: ARS 5600.00 | Subtotal: ARS 11200.00"
    assert _professionalize_whatsapp_message(message) == "- machimbre: 2 x ARS 5600.00 = ARS 11200.00"


def test_ticket_single_quantity_shows_only_customer_price():
    message = "pan frances: $2500.00 x 1 docena = $2500.00"
    assert _professionalize_whatsapp_message(message) == "pan frances: $2500.00"


def test_ticket_multiple_quantity_keeps_measure_and_total():
    message = "pan frances: $2500.00 x 2 docena = $5000.00"
    assert _professionalize_whatsapp_message(message) == "pan frances: 2 x $2500.00 docena = $5000.00"
