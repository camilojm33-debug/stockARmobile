from local_print_agent import _build_ticket_bytes


def test_build_ticket_contains_sale_and_total():
    payload = _build_ticket_bytes(
        {
            "brand": "Mi Comercio",
            "sale_id": 520,
            "date": "2026-08-25 23:43",
            "customer": "Consumidor final",
            "payment_method": "Efectivo",
            "items": [
                {"name": "machimbre", "quantity": 1, "unit_price": 5600, "total": 5600},
                {"name": "coca", "quantity": 2, "unit_price": 5300, "total": 10600},
            ],
            "subtotal": 16200,
            "discount": 0,
            "surcharge": 0,
            "tax": 0,
            "total": 16200,
        }
    )
    assert b"Mi Comercio" in payload
    assert b"Venta: #520" in payload
    assert b"machimbre: $5.600.00" in payload
    assert b"2 x $5.300.00 = $10.600.00" in payload
    assert b"TOTAL: $16.200.00" in payload
    assert payload.startswith(b"\x1b@")
    assert payload.endswith(b"\x1dV\x00")
